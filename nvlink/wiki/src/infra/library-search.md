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
| `sub_429AA0` | `make_library_filename` | 304 B | Transforms `-l` name into `lib<name>.a` (or `lib<name>.so` if shared flag set; main always passes shared=false) |
| `sub_42A2D0` | `archive_validate_callback` | 5,008 B | Opens archive, iterates members, validates CPU architecture |
| `sub_4297B0` | `archive_status_report` | ~200 B | Reports archive open/iterate status codes via diagnostic system |
| `sub_464460` | `list_node_create` | 96 B | Allocates a 16-byte linked-list node (next + data) |
| `sub_4646A0` | `list_find` | ~80 B | Linear search through linked list using comparator callback |
| `sub_4BC470` | `load_libnvvm` | ~40 B | Constructs `<nvvmpath>/lib64/libnvvm.so` path and loads via dlopen |
| `sub_5F5AC0` | `path_join_with_fallback` | ~50 B | Joins dir + filename; if dir is NULL, returns filename directly |

## Search Context Data Structure

### 16-Byte Context Header (sub\_4622D0)

`search_context_create` allocates a 16-byte structure that serves as the anchor for a singly-linked list of search directories. The structure uses a tail-pointer idiom for O(1) append.

```text
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

```text
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

The entire library search block in `main` is gated by:

```c
if ( (unsigned int)(dword_2A77DC0 - 1) > 1 )  // unsigned subtraction + compare
```

This unsigned comparison passes when `dword_2A77DC0` (linker mode) is 0 or >= 3, and blocks when it is 1 or 2. Modes 1 and 2 skip library resolution entirely.

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
    /*include_empty=*/0, /*keep_delimiters=*/1,
    search_context_append_cb, (uintptr_t)ctx,
    /*handle_escapes=*/1, /*handle_brackets=*/1);
```

## String Tokenizer (sub\_44EC40)

`split_and_callback` is a general-purpose tokenizer used by multiple subsystems (library path parsing, nvinfo parsing, option response files). It copies the input string into arena memory, then repeatedly calls `sub_44E8B0` (the token extractor) to split on a delimiter, invoking a callback for each token.

```c
// sub_44EC40 -- split_and_callback
void split_and_callback(
    char*    input,           // string to tokenize (a1)
    char*    delimiters,      // delimiter characters (a2, e.g. ":")
    bool     include_empty,   // if true, invoke callback even for empty tokens (a3)
    bool     keep_delimiters, // passed through to tokenize() (a4)
    void   (*callback)(char* token, uintptr_t arg),  // (a5)
    uintptr_t callback_arg,   // (a6)
    bool     handle_escapes,  // tokenizer handles backslash escapes (a7)
    bool     handle_brackets  // tokenizer handles [...] bracket nesting (a8)
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

The `include_empty` flag controls whether zero-length tokens (from consecutive delimiters like `::`) invoke the callback. For `LIBRARY_PATH` parsing, `include_empty` is 0, so empty path components are silently skipped. The `keep_delimiters` flag is passed through to the token extractor and controls whether delimiter characters are preserved as separate tokens (set to 1 for `LIBRARY_PATH` parsing, though this mainly affects multi-character delimiter scenarios). The `handle_escapes` and `handle_brackets` flags are both 1, enabling backslash-escaped characters and `[...]`-delimited literal blocks within path components (though these features are primarily used for nvinfo parsing, not paths).

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

For each `-l` library, `main` calls `make_library_filename` to produce `lib<name>.a` (always with `shared=false`; nvlink is a device linker and only searches for static archives), then invokes `path_search` twice with the **same** filename:

1. **Pass 1 (stat-only):** `callback=NULL`. The function returns the first candidate path where `stat()` succeeds. No archive validation occurs. This quickly resolves libraries that exist as plain files.

2. **Pass 2 (archive validation):** `callback=archive_validate_callback` (`sub_42A2D0`). Invoked only when Pass 1 returns NULL (file not found by stat alone). The function finds the file via `stat()`, then invokes the callback to open it as an archive and verify that at least one member has the correct CPU architecture. The callback returns 0 to accept, or non-zero to continue searching the next directory.

After resolution, a duplicate check (`sub_4646A0`, linear list search using string comparator `sub_44E180`) prevents the same resolved path from appearing twice in the input file list (`qword_2A5F330`).

```c
v188 = make_library_filename(lib_name);               // always produces "lib<name>.a"
if ( !path_search(ctx, v188, 1, 0, NULL, 0) )         // Pass 1: stat-only
{
    v188 = make_library_filename(lib_name);            // same filename again
    resolved = path_search(ctx, v188, 1, 0,
                           archive_validate_callback,
                           lib_name);                  // Pass 2: with validation
    if (resolved) {
        if (!list_find(input_file_list, resolved, strcmp_cb))
        {
            node = list_node_create(resolved, NULL);
            list_append_tail(input_file_list, node);   // sub_4649B0
        }
    }
}
```

The two-pass design optimizes the common case: most libraries are found in the first directory with the correct architecture, so the expensive archive-open-and-iterate path is only taken when the stat-only pass fails.

> **Note:** `make_library_filename` supports a `shared` parameter that produces `lib<name>.so` when true, but `main` never passes `shared=true`. The `.so` code path may be reserved for future use or inherited from a shared codebase with the host linker.

## Archive Validation Callback (sub\_42A2D0)

When the stat-only pass fails (Pass 1 finds no matching file) or the library needs architecture validation, `main` invokes Pass 2 with `sub_42A2D0` as the acceptance callback.

### Algorithm

```c
// sub_42A2D0 -- archive_validate_callback
int archive_validate_callback(char* archive_path, int flags) {
    // 1. Open archive
    archive_handle_t handle;
    int open_status = archive_open(&handle, archive_path);      // sub_4BDAC0

    // 2. Check initial open status
    if (open_status == 7 && !suppress_arch_warn
        && !strstr(archive_path, "cudadevrt"))
        warning("architecture mismatch in %s", archive_path);   // sub_467460 via unk_2A5B660
    else if (open_status == 4)
        error("unsupported code in " + archive_path);
    else if (open_status != 0)
        error(archive_status_string(open_status));               // sub_4BC270

    // 3. Iterate archive members
    member_t member;
    while (1) {
        int status = archive_next_member(&member, handle);       // sub_4BDAF0
        if (status == 0) break;  // end of archive -- fall through to member check
        if (status == 7) {       // arch mismatch for this member
            if (!suppress_arch_warn && !strstr(archive_path, "cudadevrt"))
                warning("...");
            goto check_member;
        }
        if (status == 4)
            error("unsupported code in " + archive_path);
        else if (status != 0)
            error(archive_status_string(status));

check_member:
        if (!member) goto no_match;

        // 4. Validate CPU architecture via e_machine
        uint16_t elf_machine = get_elf_header(member)->e_machine; // sub_448360
        int expected = cpu_arch_to_elf_machine(cpu_arch_string);

        if (elf_machine == expected) {
            // 5. Match found -- close archive, report status, accept
            int close_status = archive_close(handle);             // sub_4BDB30
            archive_status_report(close_status, archive_path);    // sub_4297B0
            return 0;  // accept
        }
    }

no_match:
    // 6. No compatible member found
    int close_status = archive_close(handle);                     // sub_4BDB30
    archive_status_report(close_status, archive_path);            // sub_4297B0
    warning("SM Arch not found in archive", archive_path);        // via unk_2A5B610
    return 1;  // reject
}
```

`sub_4297B0` (`archive_status_report`) is NOT a member processor -- it checks the status code returned by `archive_close` and emits diagnostics accordingly: status 0 is a no-op, status 7 emits an architecture mismatch warning (suppressed for `cudadevrt` paths), status 4 emits a format error, and any other status is converted to an error string via `sub_4BC270`.

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
| `qword_2A5F278` | `char*` | `nvvmpath` | Path to libnvvm installation (from `--nvvmpath` flag) |
| `byte_2A5F288` | `bool` | `lto_enabled` | LTO mode flag (from `--link-time-opt`); gates libnvvm loading |

## Special Library Handling

### libnvvm.so Loading (sub\_4BC470)

`libnvvm.so` is loaded via a dedicated path, completely separate from the `-l` search infrastructure. When LTO is enabled (`byte_2A5F288` / `--link-time-opt`):

```c
if (lto_enabled) {
    if (!nvvmpath)
        fatal_error("-nvvmpath should be specified with -lto");  // 0x1d33dc8

    char* dir = malloc(strlen(nvvmpath) + 7);
    strcpy(dir, nvvmpath);
    strcat(dir, "/lib64");                                       // 0x1d34176
    int status = load_libnvvm(linker_ctx, dir);                  // sub_4BC470
    if (status)
        fatal_error(archive_status_string(status));
}
```

`sub_4BC470` internally calls `sub_5F5AC0(dir, "libnvvm.so", 0)` which uses `path_join` (`sub_462550`) to construct `<nvvmpath>/lib64/libnvvm.so`, then passes the result to `sub_4BC290` (the dlopen wrapper). The `--nvvmpath` option is registered as string type 8 (path) in `sub_427AE0` with help text `"Path to libnvvm library."` (`0x1d32784`).

### libcudadevrt.a Resolution

`libcudadevrt.a` is resolved through the normal `-l` search path mechanism (it arrives as `-lcudadevrt`). Two special behaviors apply:

1. **Architecture mismatch suppression:** The `strstr(archive_path, "cudadevrt")` check in `sub_42A2D0` suppresses status-7 warnings for this library. This prevents noise in cross-compilation scenarios where the host-arch members don't match `--cpu-arch`.

2. **LTO IR extraction:** During LTO processing, `sub_42AF40` detects `"cudadevrt"` in the filename and extracts IR content specially, printing the debug trace `"found IR for libcudadevrt\n"` (`0x1d340a8`). The extracted IR is stored in output parameters for later LTO compilation. When LTO absorbs all objects, the message `"LTO on everything so remove libcudadevrt from list\n"` (`0x1d34658`) is printed and libcudadevrt is removed from the link list. If the archive member doesn't contain the expected format, the fatal error `"expected libcudadevrt object"` (`0x1d34316`) is emitted.

### Error Messages for Missing Libraries

The binary contains these library-search error strings:

| String | Address | Xrefs | Context |
|---|---|---|---|
| `"Skipping incompatible '%s' when searching for -l%s"` | `0x1d34ab8` | 0 | Probable warning when a candidate file exists but fails validation |
| `"Library file '%s' not found in paths"` | `0x1d34bf0` | 0 | Probable error when no candidate file is found in any search directory |
| `"Library file '%s' not recognized"` | `0x1d34c18` | 0 | Probable error when a file is found but is not a valid archive format |

These strings have zero cross-references in IDA's analysis. They may be referenced via table-driven diagnostic descriptors that IDA's xref analysis did not resolve, or they may be dead code from a prior version.

## Implementation Notes

### Tail-Pointer Linked List

The search context's tail-pointer design eliminates branching in the append path. Traditional singly-linked lists require a conditional to handle the empty-list case (`if (head == NULL) head = new; else tail->next = new`). By initializing `tail` to `&head`, the generic `*tail = new` write handles both empty and non-empty lists identically. This is the same idiom used by the Linux kernel's `list_head` and Linus Torvalds' "good taste" linked list example.

### String Builder for Path Construction

Path construction uses the shared string builder infrastructure (`sub_44FB20` create, `sub_44FF90` append char, `sub_44FE60` append string, `sub_44FDC0` finalize). The builder is initialized with a 128-byte buffer and grows as needed. `path_search` uses it inline rather than calling `path_join`, directly appending directory characters (with trailing-slash stripping) and the filename. The finalized string is arena-allocated and returned to the caller.

### Search Context Destruction (sub\_462320)

After the library resolution loop completes, `main` calls `sub_462320(ctx, 0)`. The destroy function has two cleanup modes selected by the second parameter (a byte flag):

- **Flag = 0 (used by main):** calls `sub_464520` to iterate and free the linked-list node structures only. The path strings stored in each node are NOT individually freed -- they remain in arena memory until the arena is destroyed.
- **Flag = 1:** calls `sub_464550` which iterates nodes and invokes a callback (`sub_45CAD0`) to free both the data pointer and the node.

After freeing the list contents, the context structure itself is freed via `sub_431000`.

### Arena Memory Management

Every string allocation in the search subsystem goes through the arena allocator (`sub_4307C0`). Temporary copies (e.g., the working copy in `path_split`) are freed via `sub_431000`. The search context itself, its directory nodes, and the path strings all live in arena memory, ensuring cleanup is handled when the arena is destroyed rather than requiring individual `free` calls.

## Cross-References

- [Library Resolution (pipeline)](../pipeline/library-resolution.md) -- pipeline-level view of when and how library search runs
- [CLI Option Parsing](../pipeline/cli-options.md) -- `-L`, `-l`, `--cpu-arch`, `--nvvmpath`, `--keep-system-libraries` registration
- [Archive Processing](../input/archives.md) -- `sub_4BDAC0`, `sub_4BDAF0`, `sub_4BDB30` archive member API
- [Input File Loop](../pipeline/input-loop.md) -- consumes the resolved input file list
- [Memory Arenas](memory-arenas.md) -- `sub_4307C0` / `sub_431000` arena allocator used throughout
- [Error Reporting](error-reporting.md) -- `sub_467460` diagnostic emission; `unk_2A5B610` (arch mismatch), `unk_2A5B660` (arch warn), `unk_2A5B670` (fatal)
- [libnvvm Integration](../lto/libnvvm-integration.md) -- `sub_4BC470` libnvvm.so loading; `--nvvmpath` requirement
- [LTO Overview](../lto/overview.md) -- libcudadevrt IR extraction during LTO compilation
- [Environment Variables](../config/env-vars.md) -- `LIBRARY_PATH` getenv call

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
| `LIBRARY_PATH` environment variable used for search path | HIGH | Decompiled main line 399: `getenv("LIBRARY_PATH")`; the string is at `0x225fcdd` (offset +3 within `"LD_LIBRARY_PATH\0"` at `0x225fcda` -- standard string tail-sharing). `LD_LIBRARY_PATH` is referenced only by `sub_15C3FD0` (embedded ld-linux), not nvlink code |
| String tokenizer at `sub_44EC40` (576 B) with `sub_44E8B0` (4,780 B) | HIGH | Both decompiled files exist with matching sizes; `sub_44EC40` calls `sub_44E8B0` in a loop |
| Tail-pointer linked list idiom | HIGH | Confirmed by `sub_4622D0` decompiled code: `result[1] = result` is the self-referencing tail initialization |
| Two-pass search strategy (stat-only then archive validation) | HIGH | Confirmed in main lines 404--408: Pass 1 (`callback=NULL`), Pass 2 (`callback=sub_42A2D0`); both passes use same `lib<name>.a` filename |
| Main always searches for `.a` (never `.so`) | HIGH | Both `sub_429AA0` calls in main pass only one argument; default `a2=0` produces `.a` suffix. No caller passes `a2=1` |
| `--cpu-arch` option string | HIGH | String `"cpu-arch"` at `0x1d326cd` in strings JSON |
| libnvvm.so loaded from `<nvvmpath>/lib64/libnvvm.so` | HIGH | `sub_4BC470` calls `sub_5F5AC0(path, "libnvvm.so", 0)`; main builds `path = nvvmpath + "/lib64"` |
| `--nvvmpath` required when `-lto` is used | HIGH | Decompiled `sub_427AE0` line 1143--1150: fatal error if `qword_2A5F278` is NULL when `byte_2A5F288` is set |
| Duplicate path check before appending to input list | HIGH | Main line 412: `sub_4646A0(qword_2A5F330, v189, sub_44E180)` -- linear list search with comparator |
| `sub_4297B0` is `archive_status_report`, not `process_member` | HIGH | Decompiled `sub_4297B0`: checks status codes 0/7/4/other and emits diagnostics; no member processing logic |
| Unreferenced error strings ("Skipping incompatible", "Library file not found") | LOW | Strings at `0x1d34ab8`, `0x1d34bf0`, `0x1d34c18` have zero IDA xrefs; may be table-driven or dead code |
