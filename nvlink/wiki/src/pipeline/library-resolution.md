# Library Resolution

nvlink resolves `-l` library names to filesystem paths using a search algorithm modeled on traditional Unix linker behavior: build a search path list from `-L` flags and environment variables, then probe each directory for files matching the library name. The implementation departs from `ld` in several ways -- it searches only for static archives (`.a`), never shared objects or bare object files; it processes archive members through an architecture-matching callback that silently skips incompatible objects; and it special-cases `libcudadevrt` for both archive validation suppression and LTO removal.

Library resolution runs once, early in `main`, after option parsing and before the input-file dispatch loop. It is skipped entirely in host-linker-script-only mode (`-ghls`) and augmented mode. Resolution is deferred in the sense that the archive contents are not extracted at this point -- only the filesystem path to the `.a` file is resolved and appended to the input list. Actual member extraction happens later during the [input loop](input-loop.md).

## Key Facts

| Property | Value |
|---|---|
| Entry function | `sub_4622D0` (0x4622D0) -- creates search context |
| Path append | `sub_462500` (0x462500) -- appends a directory to search list |
| Env var callback | `sub_462520` (0x462520) -- callback for LIBRARY_PATH parsing |
| Env parser | `sub_44EC40` (0x44EC40) -- splits string on delimiter, invokes callback per token |
| Search function | `sub_462870` (0x462870) -- searches directories for a file, with optional acceptance callback |
| Path split | `sub_462620` (0x462620) -- splits path into directory, basename, extension |
| Dir+file join | `sub_462550` (0x462550) -- constructs `dir/basename.ext` path |
| Name transform | `sub_429AA0` (0x429AA0) -- converts `-l` name to `lib<name>.a` |
| Archive callback | `sub_42A2D0` (0x42A2D0) -- opens archive, iterates members, validates arch |
| Cleanup | `sub_462320` (0x462320) -- destroys search context |
| Search path global | `qword_2A5F300` -- linked list of `-L` directories |
| Library list global | `qword_2A5F2F8` -- linked list of `-l` library names |
| Input file list | `qword_2A5F330` -- linked list of resolved input files |
| Mode guard | `dword_2A77DC0` -- linker mode (resolution skipped for modes 1 and 2) |

## CLI Flags That Affect Resolution

Four command-line flags control library resolution directly, and two additional flags interact with the resolved libraries downstream:

| Flag | Long form | Global | Type | Description |
|---|---|---|---|---|
| `-l` | `--library` | `qword_2A5F2F8` | string, mult=2, flags=16 | Library name to search for. Accumulated into a linked list in command-line order |
| `-L` | `--library-path` | `qword_2A5F300` | string, mult=2, flags=16 | Directory to prepend to the search path. Accumulated into a linked list |
| | `--cpu-arch` | `qword_2A5F2A0` | string | Host CPU architecture for archive member validation (e.g., `X86_64`, `AARCH64`) |
| | `--keep-system-libraries` | `byte_2A5F2C2` | bool | When set, prevents `libcudadevrt` from being removed during LTO post-processing |

Both `library` and `library-path` are registered with multiplicity 2 (multi-value) in `sub_427AE0` at lines 148--174, so repeated `-l`/`-L` flags accumulate into separate linked lists. The short forms `-l` and `-L` are aliases for `--library` and `--library-path` respectively. The help text for `-l` reads: `"Specify libraries to be used in the linking stage. The libraries are searched for on the library search paths that have been specified using option '-L'"`.

### Registration Details (from sub\_427AE0)

```c
// Line 148: -l / --library
sub_42F130(parser, "library", "l",
    /*type=*/2, /*mult=*/2, /*flags=*/16,
    0, 0, 0, 0,
    "<library>",
    "Specify libraries to be used in the linking stage. "
    "The libraries are searched for on the library search "
    "paths that have been specified using option '-L'");

// Line 162: -L / --library-path
sub_42F130(parser, "library-path", "L",
    /*type=*/2, /*mult=*/2, /*flags=*/16,
    0, 0, 0, 0,
    "<library-path>",
    "Specify library search paths");
```

After parsing completes, the extracted values are stored into their globals:

```c
sub_42E390(parser, "library",      &qword_2A5F2F8, 8);   // line 950
sub_42E390(parser, "library-path", &qword_2A5F300, 8);    // line 951
```

## When Library Resolution Runs

The library resolution block in `main` occupies lines 385--424 of `main_0x409800.c`. It is guarded by:

```c
if ((unsigned int)(dword_2A77DC0 - 1) > 1) {
    // library resolution
}
```

This unsigned comparison means resolution runs when `dword_2A77DC0` is 0 (device link, the default) or >= 3. It is skipped for mode 1 (`-ghls=lcs-aug`, augmented linker script) and mode 2 (`-ghls=lcs-abs`, absolute linker script), where no actual device object linking occurs. The complete block:

```c
// main_0x409800.c lines 385-424 -- library resolution phase
if ( (unsigned int)(dword_2A77DC0 - 1) > 1 )
{
    // Step 1: Create search context
    v182 = sub_4622D0();                          // search_context_create
    v183 = (_QWORD *)qword_2A5F300;               // -L path list head
    v184 = v182;

    // Step 2: Append -L directories
    if ( qword_2A5F300 )
    {
        do
        {
            sub_462500(v184, v183[1]);             // search_context_append(ctx, path)
            v183 = (_QWORD *)*v183;                // advance to next node
        }
        while ( v183 );
    }

    // Step 3: Append LIBRARY_PATH directories
    v185 = (unsigned int)getenv("LIBRARY_PATH");
    sub_44EC40(v185, ":", 0, 1,                    // split_and_callback
               sub_462520, v184, 1, 1);            //   callback=append_cb, arg=ctx

    // Step 4: Iterate -l libraries
    v186 = qword_2A5F2F8;                         // -l library list head
    for ( i = v337; v186; v186 = *(_QWORD *)v186 )
    {
        // Transform name to "lib<name>.a"
        v188 = sub_429AA0(*(char **)(v186 + 8));   // make_library_filename

        // Pass 1: stat-only search (no callback)
        if ( !sub_462870(v184, v188, 1, 0, 0, 0) )
        {
            // Pass 2: archive-validation search
            v188 = sub_429AA0(*(char **)(v186 + 8));
            v189 = sub_462870(v184, v188, 1, 0,
                              sub_42A2D0,          // archive_validate_callback
                              *(_QWORD *)(v186 + 8));
            v190 = v189;
            if ( v189 )
            {
                // Deduplicate: only add if not already in input list
                if ( !(unsigned __int8)sub_4646A0(
                        qword_2A5F330, v189, sub_44E180) )
                {
                    v191 = sub_464460(v190, 0);    // list_node_create
                    sub_4649B0(qword_2A5F330, v191); // list_append
                }
            }
        }

        // Free temporary filename if different from original name
        if ( v188 != *(_QWORD *)(v186 + 8) )
            sub_431000(v188);                      // arena_free
    }

    // Step 5: Cleanup
    a2 = 0;
    sub_462320(v184, 0, i);                        // search_context_destroy
}
```

This sequence shows that library resolution is a single, non-interruptible phase that runs between option parsing (`sub_427AE0`, line 384) and the input file dispatch loop (beginning around line 425). The resolved paths are appended to the same `qword_2A5F330` input file list that holds directly-specified input files from the command line.

## Search Context Data Structure

`sub_4622D0` allocates the search context -- a 16-byte structure that serves as the head of a singly-linked list of search directories:

```c
// sub_4622D0 -- search_context_create
search_ctx* search_context_create(arena) {
    search_ctx* ctx = arena_alloc(arena, 16);
    ctx->head = NULL;       // offset 0: pointer to first directory node
    ctx->tail = ctx;        // offset 8: pointer to tail (for O(1) append)
    return ctx;
}
```

The tail pointer is initialized to point at the context itself (i.e., `&ctx->head`), not to NULL. This means the first append writes directly into the `head` field via `*ctx->tail = new_node`, eliminating a special case for empty-list insertion. This is the same "good taste" linked-list idiom described by Linus Torvalds.

Each directory node in the list is a generic linked-list node allocated by `sub_464460`:

```c
struct search_dir_node {
    search_dir_node* next;  // offset 0: next node, or NULL
    char* path;             // offset 8: directory path string
};
```

The append function `sub_462500` links at the tail in O(1):

```c
// sub_462500 -- search_context_append
void search_context_append(search_ctx* ctx, char* dir_path) {
    node* n = list_node_create(dir_path, NULL);  // sub_464460
    *ctx->tail = n;     // link at end
    ctx->tail = n;      // advance tail to new node
}
```

## Search Path Construction Order

The search path is built in two phases: first from `-L` command-line flags, then from the `LIBRARY_PATH` environment variable. The `-L` paths appear first and take precedence.

### Phase 1: -L Paths

The option parser stores all `-L` arguments in `qword_2A5F300` as a linked list (multi-value option, multiplicity=2). After creating the search context, `main` iterates this list and appends each directory:

```c
search_ctx* ctx = search_context_create();   // sub_4622D0

// Append -L directories in command-line order
node* lpath = qword_2A5F300;  // -L path list
while (lpath) {
    search_context_append(ctx, lpath->value);    // sub_462500
    lpath = lpath->next;
}
```

### Phase 2: LIBRARY\_PATH Environment Variable

After `-L` paths, nvlink reads the `LIBRARY_PATH` environment variable and splits it on `:` delimiters. Each token is appended to the same search context:

```c
char* env = getenv("LIBRARY_PATH");
split_and_callback(env, ":", /*include_empty=*/0, /*keep_delim=*/1,
                   search_context_append_cb, ctx,
                   /*escape=*/1, /*bracket=*/1);
```

The `sub_44EC40` function is a general-purpose string tokenizer. It copies the input string, then repeatedly calls `sub_44E8B0` (a token extractor that handles quoting, escaping, and bracket syntax) to split on the delimiter. For each non-empty token, it invokes the callback. Empty path components (from consecutive `:` delimiters) are silently skipped because `include_empty` is 0.

The callback `sub_462520` is identical in logic to `sub_462500` -- it wraps the token in a list node and appends to the search context. The distinction exists because the two functions have swapped argument order: `sub_462500` takes `(ctx, path)` while `sub_462520` takes `(path, ctx)`, the latter matching the `(token, user_data)` callback signature expected by the tokenizer.

### Phase 3: No Built-in Paths

Unlike GNU `ld`, nvlink does **not** append any built-in search directories (such as `/usr/lib` or `/usr/local/lib`). If a library is not found via `-L` or `LIBRARY_PATH`, it is not found at all. In practice, `nvcc` always supplies `-L` pointing to the CUDA toolkit's `lib64/` directory.

### Resulting Search Order

The final search order for any library is:

1. `-L` directories, in the order they appear on the command line
2. `LIBRARY_PATH` directories, in left-to-right colon-separated order

This matches the convention of GNU `ld` and most Unix linkers.

## Library Name Transformation

When resolving a `-l<name>` flag, nvlink transforms the bare name into a filename using `sub_429AA0`. The transformation always prepends `lib` and appends `.a`:

```c
// sub_429AA0 -- make_library_filename
char* make_library_filename(char* name, bool shared) {
    // Step 1: Prepend "lib" using a DWORD write
    char* buf = arena_alloc(arena, strlen(name) + 4);
    *(uint32_t*)buf = 0x0062696C;  // "lib\0" as little-endian DWORD
    strcat(buf, name);             // buf = "lib<name>"

    // Step 2: Append extension
    if (shared) {
        char* result = arena_alloc(arena, strlen(buf) + 4);
        char* end = stpcpy(result, buf);
        *(uint32_t*)end = 0x006F732E;  // ".so\0" as little-endian DWORD
    } else {
        char* result = arena_alloc(arena, strlen(buf) + 3);
        strcpy(stpcpy(result, buf), ".a");
    }
    return result;
}
```

The integer constants decode as: `0x0062696C` = `"lib"` (little-endian bytes `6C 69 62 00`) and `0x006F732E` = `".so"` (bytes `2E 73 6F 00`). The function uses DWORD writes instead of `strcpy` for the short prefix/suffix strings -- a micro-optimization pattern seen throughout nvlink.

**Critical detail:** `main` always calls `sub_429AA0` with a single argument, meaning the `shared` parameter defaults to 0 (false). The `.a` code path is always taken; the `.so` code path is dead code in the current binary. nvlink is a device linker and only searches for static archives. The `.so` support may be inherited from a shared codebase with the host linker.

### Transformation Examples

| `-l` argument | Transformed filename | Extension |
|---|---|---|
| `-lcudadevrt` | `libcudadevrt.a` | `.a` |
| `-lcudart_static` | `libcudart_static.a` | `.a` |
| `-lnvToolsExt` | `libnvToolsExt.a` | `.a` |

## Search Algorithm

The search function `sub_462870` is the core of library resolution. It takes a search context, a candidate filename, flags controlling behavior, and an optional acceptance callback. It returns the full path to the first matching file, or NULL.

### Two-Pass Search Strategy

For each `-l` library, `main` performs two search passes with the same `lib<name>.a` filename:

**Pass 1 (stat-only):** `path_search(ctx, filename, 1, 0, NULL, 0)`. The function iterates search directories, constructs `<dir>/lib<name>.a` for each, and returns the first candidate where `stat()` succeeds. No archive validation occurs. This quickly resolves libraries that exist as plain files on disk.

**Pass 2 (archive validation):** `path_search(ctx, filename, 1, 0, archive_validate_callback, lib_name)`. Invoked only when Pass 1 returns NULL. The function finds the file via `stat()`, then invokes `sub_42A2D0` to open it as an archive and verify that at least one member has the correct CPU architecture. The callback returns 0 to accept, or non-zero to continue searching the next directory.

```
search_for("-lcudadevrt"):
    filename = "libcudadevrt.a"

    Pass 1: for each dir in [-L dirs, LIBRARY_PATH dirs]:
        if stat("<dir>/libcudadevrt.a") succeeds:
            return "<dir>/libcudadevrt.a"

    Pass 2: for each dir in [-L dirs, LIBRARY_PATH dirs]:
        if stat("<dir>/libcudadevrt.a") succeeds:
            if archive_validate("<dir>/libcudadevrt.a") == ACCEPT:
                return "<dir>/libcudadevrt.a"

    return NULL  (library not found)
```

The two-pass design optimizes the common case: most libraries are found in the first directory with the correct architecture, so the expensive archive-open-and-iterate path is only taken when the stat-only pass fails. This can happen when the search path contains directories that exist but hold archives for a different host architecture.

### path\_search Internals

The `sub_462870` function implements a multi-stage search with fallback:

```c
// sub_462870 -- path_search (simplified)
char* path_search(search_ctx* ctx, char* filename,
                  bool search_dirs, bool try_split,
                  accept_fn callback, uint64_t cb_arg) {

    // Stage 1: Check if filename contains a directory separator
    char* slash = strrchr(filename, '/');

    if (slash) {
        // Stage 2: Has directory component
        // If absolute path or search_dirs is false, check directly
        if (filename[0] == '/' || !search_dirs) {
            if (stat(filename) == 0)
                return arena_strdup(filename);
            goto try_split_path;
        }
    }

    // Stage 3: Iterate search directories
    node* dirnode = ctx->head;
    while (dirnode) {
        char* candidate = build_path(dirnode->path, filename);

        if (stat(candidate) == 0) {
            if (!callback)
                return candidate;          // no callback: accept
            if (!callback(candidate, cb_arg))
                return candidate;          // callback accepted
        }
        arena_free(candidate);
        dirnode = dirnode->next;
    }

    // Stage 4: Path decomposition fallback (not used for -l resolution)
    if (try_split) {
        // Decompose filename into dir/base.ext, reconstruct, retry
    }
    return NULL;
}
```

The path construction helper strips trailing slashes from the directory component and inserts a single `/` separator, normalizing paths like `/usr/lib//` to `/usr/lib/libfoo.a`.

### Deduplication

Before adding a resolved library path to the input file list (`qword_2A5F330`), nvlink checks for duplicates using `sub_4646A0`. This function performs a linear search through the existing list, calling the `sub_44E180` string equality comparator against each entry. If the path already exists, the library is not added again:

```c
if ( !(unsigned __int8)sub_4646A0(qword_2A5F330, resolved_path, sub_44E180) )
{
    node = sub_464460(resolved_path, 0);   // list_node_create
    sub_4649B0(qword_2A5F330, node);       // list_append
}
```

This prevents the same archive from being processed twice when it appears in multiple `-L` directories or when multiple `-l` names resolve to the same file.

## Deferred vs Immediate Resolution

Library resolution in nvlink is **deferred** -- the resolution phase identifies and validates the archive file's existence and architecture compatibility, but does not extract archive members or process their contents. The resolved path is appended to `qword_2A5F330`, the same input file list that holds directly-specified object files. Actual archive processing occurs later during the [input loop](input-loop.md), which:

1. Opens the archive (`sub_4BDAC0`)
2. Iterates members (`sub_4BDAF0`)
3. Extracts each member (`sub_4BDB30`, `sub_4BDB60`, `sub_4BDB70`)
4. Classifies and processes each member through the file-type dispatch table

The only "immediate" work done during resolution is the Pass 2 archive validation callback (`sub_42A2D0`), which opens the archive and scans for a member with the correct CPU architecture. This validation does not extract or retain any member data -- it is purely a compatibility check.

### Implication for Link Order

Because `-l` libraries are appended to the input file list after all directly-specified input files, they are processed last in the input loop. Within the set of `-l` libraries, processing order matches their command-line order. This matches the GNU `ld` convention: libraries are consulted after all object files, and library order matters for symbol resolution.

## Archive Search Callback

The acceptance callback `sub_42A2D0` implements architecture-aware archive validation:

```c
// sub_42A2D0 -- archive_validate_callback (simplified)
int archive_validate_callback(char* archive_path, int flags) {
    // 1. Open archive
    archive_handle = archive_open(archive_path);  // sub_4BDAC0

    // 2. Check open status -- status 7 = arch mismatch, 4 = format error
    if (open_status == 7 && !suppress_arch_warn
        && !strstr(archive_path, "cudadevrt"))
        warning("architecture mismatch in %s", archive_path);
    else if (open_status == 4)
        error("unsupported code in %s", archive_path);
    else if (open_status != 0)
        error(archive_status_string(open_status));

    // 3. Iterate members, check e_machine against --cpu-arch
    while (archive_next_member(&member, handle)) {
        uint16_t elf_machine = get_elf_header(member)->e_machine;
        int expected = cpu_arch_to_elf_machine(cpu_arch_string);
        if (elf_machine == expected) {
            archive_close(handle);
            return 0;  // accept: compatible member found
        }
    }

    // 4. No compatible member
    archive_close(handle);
    return 1;  // reject: try next search directory
}
```

The CPU architecture mapping supports:

| `--cpu-arch` value | Expected `e_machine` | Constant |
|---|---|---|
| `unknown` | 62 | `EM_X86_64` |
| `X86_64` | 62 | `EM_X86_64` |
| `X86` | 3 | `EM_386` |
| `ARMv7` | 40 | `EM_ARM` |
| `PPC64LE` | 21 | `EM_PPC64` |
| `AARCH64` | 183 | `EM_AARCH64` |

The `unknown` and `X86_64` cases are checked first (both map to `EM_X86_64`), which is the fast path for the overwhelmingly common x86-64 host environment. If none of the known strings match, the callback emits `"unexpected cpuArch"` (`0x1d34002`) and sets `e_machine` to 0, which never matches any valid ELF member.

## libcudadevrt Special Handling

`libcudadevrt` (the CUDA device runtime library) receives special treatment at four distinct points in the pipeline. It arrives as `-lcudadevrt` and is resolved through the normal search path mechanism to `libcudadevrt.a`.

### 1. Architecture Mismatch Suppression (Resolution Phase)

During the Pass 2 archive validation callback (`sub_42A2D0`), architecture mismatch warnings (status code 7) are silently suppressed for any archive path containing `"cudadevrt"`. The check `strstr(path, "cudadevrt")` appears at two points in the callback: once for the initial archive open status, and once for per-member iteration status. This prevents spurious warnings in cross-compilation scenarios where `libcudadevrt.a` is built for a different host architecture than specified by `--cpu-arch`.

### 2. Conditional Processing in Input Loop

During archive member iteration in the input loop (line 854 of `main`), the first `cudadevrt`-containing archive triggers a special code path. If the LTO object list (`v353`) is empty, and the current archive name contains `"cudadevrt"`, the archive is skipped entirely (`goto LABEL_131`). This prevents the pre-compiled device runtime from being loaded when LTO has not yet produced any objects that would need it.

If the LTO object list is non-empty (meaning other archives have already contributed IR), the cudadevrt archive is processed normally -- its members are extracted and passed to `sub_42AF40` for IR collection.

### 3. IR Extraction for LTO (sub\_42AF40)

When `sub_42AF40` encounters a member from an archive whose path contains `"cudadevrt"`, it extracts the NVVM IR and stores it in dedicated output parameters rather than the general IR collection:

```c
// sub_42AF40 (line 248-265) -- cudadevrt IR extraction
if (strstr(archive_path, "cudadevrt")) {
    if (verbose)
        fwrite("found IR for libcudadevrt\n", 1, 0x1A, stderr);
    *ir_out     = extracted_ir;
    *ir_size_out = extracted_size;
    // Store name via sub_46F0C0(member_handle, "libcudadevrt", ...)
}
```

After the input loop completes and LTO is active (line 922-938), the extracted cudadevrt IR is registered as a named module:

```c
sub_427A10(elfw_ctx, cudadevrt_ir, cudadevrt_ir_size, "libcudadevrt");

// Create an 80-byte object record, zero-initialized
object = sub_426AA0(80);
memset(object, 0, 80);
name = sub_426AA0(13);
strcpy(name, "libcudadevrt");
object->name = name;
object->data = cudadevrt_data;
list_append(object, &lto_object_list);
```

### 4. LTO Post-Processing Removal

After LTO compilation completes (line 1346-1366 of `main`), when all input objects were compiled through LTO (`byte_2A5F2C2` is false and `v353` is non-empty), nvlink removes the `libcudadevrt` object record from the link list:

```c
if ( (v55[64] & 1) != 0 )    // verbose flag
    fwrite("LTO on everything so remove libcudadevrt from list\n",
           1, 0x33, stderr);

// Sanity check
if ( !strstr((const char *)*v292, "cudadevrt") )
    fatal_error("expected libcudadevrt object");

// Free the object record
v297 = v292[2];             // data buffer
v353 = (_QWORD *)*v353;    // advance list head past cudadevrt
if ( v297 )
    sub_43D990(v297);       // buffer_free
sub_431000(v292[1]);        // arena_free(filename)
sub_431000(*v292);          // arena_free(name)
sub_431000(v292);           // arena_free(object)
```

The rationale: when all user code is compiled at link time, the device runtime functions from `libcudadevrt` are already inlined or linked at the IR level by `libnvvm`. The pre-compiled archive version is redundant and would cause duplicate symbol errors if merged.

This removal is **bypassed** when `--keep-system-libraries` (`byte_2A5F2C2`) is set. The flag prevents libcudadevrt from being dropped, which is necessary in partial-LTO scenarios where some objects are pre-compiled and need the device runtime's native code.

### 5. Late Ignore (Object Merge Phase)

During the object merge phase (line 1506 of `main`), if `--keep-system-libraries` is not set and the current object's name contains `"cudadevrt"`, and `sub_4448C0` returns false (indicating LTO absorbed the runtime), the object is silently removed:

```c
if ( !byte_2A5F2C2 && strstr(*v149, "cudadevrt")
     && !(unsigned __int8)sub_4448C0(v55) )
{
    if (verbose)
        fprintf(stderr, "ignore %s\n", *v149);
    // Free data, name, filename, object; unlink from list
}
```

This is a second cleanup pass that catches any `cudadevrt` member that survived the initial LTO removal.

## libnvvm.so Resolution for LTO

`libnvvm.so` is loaded through a completely separate path from the `-l` search infrastructure. It does not go through `path_search` or the search context. Instead, when LTO is enabled (`byte_2A5F288` / `-lto`), the library path is constructed directly from the `--nvvmpath` CLI option:

```c
if (lto_enabled) {
    if (!nvvmpath)
        fatal_error("-nvvmpath should be specified with -lto");

    char* dir = malloc(strlen(nvvmpath) + 7);
    strcpy(dir, nvvmpath);
    strcat(dir, "/lib64");
    int status = load_libnvvm(elfw_ctx, dir);   // sub_4BC470
    if (status)
        fatal_error(archive_status_string(status));
}
```

`sub_4BC470` internally calls `sub_5F5AC0(dir, "libnvvm.so", 0)` which constructs `<nvvmpath>/lib64/libnvvm.so` using `sub_462550` (the same `path_join` utility used by the search infrastructure), then loads it via `dlopen` with `RTLD_NOW`. In practice, `nvcc` always supplies `--nvvmpath` pointing to the CUDA toolkit's `nvvm/` directory, so the final path is typically `<toolkit>/nvvm/lib64/libnvvm.so`.

The `--nvvmpath` option is validated during option parsing: if `-lto` is active and `--nvvmpath` is not set, `sub_427AE0` emits a fatal error before reaching the library resolution phase. The error string is `"-nvvmpath should be specified with -lto"` at address `0x1d33dc8`.

## Error Handling for Unresolvable Libraries

The error handling for library resolution is intentionally sparse in the resolution phase itself:

### During Resolution (Pass 2 Callback)

If the archive validation callback (`sub_42A2D0`) opens an archive but finds no member with a matching CPU architecture, it emits a warning `"SM Arch not found in archive"` and returns 1 (reject), causing `path_search` to try the next directory. If all directories are exhausted, `path_search` returns NULL.

When the archive format is invalid (status code 4), the callback calls `error("unsupported code in <path>")`. For any other non-zero status code, it calls `error(archive_status_string(status))`.

### After Resolution (NULL Result)

If `path_search` returns NULL for both passes, the code **does not emit an immediate error**. The NULL result means no path is appended to the input file list, and the `-l` flag is effectively silently ignored. The error surfaces later: when the input loop processes objects and encounters unresolved symbols that the missing library should have provided, the linker emits undefined-symbol errors during the symbol resolution phase.

The binary contains unreferenced error strings that suggest more explicit error reporting existed in a prior version or is reachable through a table-driven diagnostic path that IDA's xref analysis did not resolve:

| String | Address | Context |
|---|---|---|
| `"Skipping incompatible '%s' when searching for -l%s"` | `0x1d34ab8` | Warning when a candidate exists but fails validation |
| `"Library file '%s' not found in paths"` | `0x1d34bf0` | Error when no candidate is found |
| `"Library file '%s' not recognized"` | `0x1d34c18` | Error when a file is found but is not a valid archive |

## Interaction with Linker Mode

The linker mode (`dword_2A77DC0`) affects library resolution at two levels:

### Top-Level Skip

Modes 1 and 2 (the `-ghls` host linker script modes) skip the entire library resolution block. These modes generate linker scripts for the host linker and do not perform device-code linking, so device-side `-l` flags are irrelevant.

### Relocatable vs Final Link

In relocatable mode (`-r` / `byte_2A5F1E8`), library resolution runs normally -- the mode guard `dword_2A77DC0` is still 0 (device link). However, the downstream behavior changes: in a relocatable link, unresolved symbols are permitted, so the consequences of a missing library are less severe (the undefined references are carried forward into the output `.o` file rather than causing hard errors).

In final (non-relocatable) link mode, any symbol left unresolved after all libraries are processed results in a fatal error during the symbol resolution phase.

### LTO Interaction

When LTO is active (`byte_2A5F288`), library resolution still runs identically -- it finds the `.a` files on disk. The difference manifests during the input loop: archives containing NVVM IR members are fed to the LTO compilation pipeline (`sub_4BC4A0` / `sub_4BC6F0`) rather than being directly merged. The `libcudadevrt` removal logic (described above) only activates when LTO is active and the `--keep-system-libraries` flag is not set.

## Function Map

| Address | Name (recovered) | Size | Role |
|---|---|---|---|
| `sub_4622D0` | `search_context_create` | 80 bytes | Allocates 16-byte search context with head/tail pointers |
| `sub_462500` | `search_context_append` | 48 bytes | Appends directory path to search context (direct call) |
| `sub_462520` | `search_context_append_cb` | 48 bytes | Same as above, callback-compatible signature for tokenizer |
| `sub_44EC40` | `split_and_callback` | 576 bytes | Tokenizes string on delimiter, calls callback per token |
| `sub_44E8B0` | `tokenize` | 4,780 bytes | Token extractor with quoting, escaping, bracket support |
| `sub_462870` | `path_search` | 4,905 bytes | Searches directory list for file, with optional acceptance callback |
| `sub_462620` | `path_split` | 3,579 bytes | Splits path into directory, basename, extension components |
| `sub_462550` | `path_join` | 288 bytes | Joins directory + basename + extension into path string |
| `sub_429AA0` | `make_library_filename` | 304 bytes | Converts `-l` name to `lib<name>.a` (always `.a`; `.so` path is dead code) |
| `sub_42A2D0` | `archive_validate_callback` | 5,008 bytes | Opens archive, validates member architecture, returns accept/reject |
| `sub_42AF40` | `process_ir_member` | ~2,500 bytes | Processes archive member IR; special-cases `cudadevrt` for LTO |
| `sub_427A10` | `register_lto_module` | ~200 bytes | Registers named IR module with the NVVM program |
| `sub_462320` | `destroy_search_context` | 112 bytes | Frees search context and directory node list |
| `sub_462C10` | `path_split_dir_file` | 512 bytes | Splits path into directory and filename (no extension) |
| `sub_4646A0` | `list_find` | 80 bytes | Checks if value exists in linked list via comparator |
| `sub_4649B0` | `list_append` | 64 bytes | Appends node to end of linked list |
| `sub_4BC470` | `load_libnvvm` | ~40 bytes | Constructs libnvvm.so path and loads via dlopen |

## Global Variables

| Address | Name (recovered) | Type | Description |
|---|---|---|---|
| `qword_2A5F300` | `library_path_list` | `node*` | Linked list from `-L` flags (multi-value option) |
| `qword_2A5F2F8` | `library_list` | `node*` | Linked list from `-l` flags (multi-value option) |
| `qword_2A5F330` | `input_file_list` | `node*` | Master input file list; resolved libraries are appended here |
| `qword_2A5F318` | `arch_string` | `char*` | Target GPU architecture string (e.g. `"sm_90a"`) |
| `qword_2A5F2A0` | `cpu_arch_string` | `char*` | Host CPU architecture (e.g. `"X86_64"`, `"AARCH64"`) |
| `qword_2A5F278` | `nvvmpath` | `char*` | Path to libnvvm installation (from `--nvvmpath`) |
| `dword_2A77DC0` | `linker_mode` | `int` | 0=device-link, 1=ghls-aug, 2=ghls-abs |
| `byte_2A5F298` | `suppress_arch_warn` | `bool` | Suppresses architecture mismatch warnings globally |
| `byte_2A5F288` | `lto_enabled` | `bool` | LTO mode flag (from `-lto`/`--link-time-opt`) |
| `byte_2A5F2C2` | `keep_system_libs` | `bool` | `--keep-system-libraries` flag; prevents cudadevrt LTO removal |

## Cross-References

- [Library Search (infra)](../infra/library-search.md) -- infrastructure-level documentation of the search context, tokenizer, path manipulation, and archive validation callback at reimplementation depth
- [CLI Options](cli-options.md) -- `-L`, `-l`, `--library`, `--library-path`, `--keep-system-libraries`, `--cpu-arch` option registration
- [Input Loop](input-loop.md) -- processes the resolved input file list (`qword_2A5F330`); extracts archive members that resolution identified
- [Archives](../input/archives.md) -- archive member iteration (`sub_4BDAC0`, `sub_4BDAF0`, `sub_4BDB30`)
- [libnvvm Integration](../lto/libnvvm-integration.md) -- `sub_4BC470` libnvvm.so loading; `--nvvmpath` requirement; the LTO compilation pipeline that consumes the IR collected from resolved libraries
- [LTO Overview](../lto/overview.md) -- libcudadevrt removal during whole-program LTO; the full LTO pipeline flow
- [Mode Dispatch](mode-dispatch.md) -- linker mode values (0/1/2) and their meaning; explains why modes 1 and 2 skip resolution
- [Memory Arenas](../infra/memory-arenas.md) -- `sub_4307C0` / `sub_431000` arena allocator used for all search context allocations
- [Error Reporting](../infra/error-reporting.md) -- `sub_467460` diagnostic emission; `unk_2A5B670` (fatal), `unk_2A5B610` (arch mismatch warning)
- [Environment Variables](../config/env-vars.md) -- `LIBRARY_PATH` getenv call

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| Library resolution block spans main lines 385--424 | **HIGH** | Direct decompiled code reading; guard condition and `sub_4622D0`/`sub_462320` brackets confirmed |
| `-l` registered as `"library"` with short form `"l"`, mult=2, flags=16 | **HIGH** | `sub_427AE0` line 148: `sub_42F130(parser, "library", "l", 2, 2, 16, ...)` |
| `-L` registered as `"library-path"` with short form `"L"`, mult=2, flags=16 | **HIGH** | `sub_427AE0` line 162: `sub_42F130(parser, "library-path", "L", 2, 2, 16, ...)` |
| Extracted to `qword_2A5F2F8` and `qword_2A5F300` respectively | **HIGH** | `sub_427AE0` lines 950-951: `sub_42E390(parser, "library", &qword_2A5F2F8, 8)` and `sub_42E390(parser, "library-path", &qword_2A5F300, 8)` |
| Two-pass search: stat-only then archive validation | **HIGH** | Main lines 404-408: two calls to `sub_462870` with and without `sub_42A2D0` callback |
| Main always produces `.a` (never `.so`) | **HIGH** | Both `sub_429AA0` calls pass one argument; default `shared=false` produces `.a` |
| `LIBRARY_PATH` environment variable used (not `LD_LIBRARY_PATH`) | **HIGH** | Main line 399: `getenv("LIBRARY_PATH")`; the string is at offset +3 within `"LD_LIBRARY_PATH"` at `0x225fcda` (standard string tail-sharing) |
| Search order: `-L` dirs first, then `LIBRARY_PATH` | **HIGH** | Matches code order in main (lines 390-400); standard Unix convention |
| Deduplication via `sub_4646A0` with `sub_44E180` comparator | **HIGH** | Main line 412: `sub_4646A0(qword_2A5F330, v189, sub_44E180)` |
| `libcudadevrt` arch-mismatch suppression via `strstr(path, "cudadevrt")` | **HIGH** | `sub_42A2D0` decompiled code: `strstr` check at two points |
| `libcudadevrt` IR extraction in `sub_42AF40` | **HIGH** | Decompiled line 249: `strstr(a3, "cudadevrt")` followed by `fwrite("found IR for libcudadevrt\n", ...)` |
| LTO removal message: `"LTO on everything so remove libcudadevrt from list"` | **HIGH** | Main line 1350: `fwrite` with exact string |
| `"expected libcudadevrt object"` sanity check | **HIGH** | Main line 1354: fatal error if `strstr` fails |
| `--keep-system-libraries` prevents cudadevrt removal | **HIGH** | Main lines 1346 and 1506: `byte_2A5F2C2` gates the removal logic |
| libnvvm.so loaded from `<nvvmpath>/lib64/libnvvm.so` | **HIGH** | `sub_4BC470` calls `sub_5F5AC0(dir, "libnvvm.so", 0)`; main builds dir as `nvvmpath + "/lib64"` |
| `--nvvmpath` required when `-lto` active | **HIGH** | `sub_427AE0` lines 1143-1150: fatal error if `qword_2A5F278` is NULL when `byte_2A5F288` set; error string at `0x1d33dc8` |
| No built-in search directories appended | **HIGH** | Search context only receives `-L` entries and `LIBRARY_PATH` tokens; no other `search_context_append` calls visible |
| Unreferenced error strings at `0x1d34ab8`, `0x1d34bf0`, `0x1d34c18` | **LOW** | Strings exist in binary; zero IDA xrefs; may be table-driven or dead code |
| `sub_42AF40` is ~2,500 bytes | **MEDIUM** | Size estimated from decompiled line count (150+ lines); exact byte size not confirmed via `stat` |
| Resolution is deferred (no member extraction during resolution) | **HIGH** | Resolution phase only calls `path_search` which uses `stat` + archive validation; extraction functions (`sub_4BDB30`, `sub_4BDB60`) are only called in the input loop |
