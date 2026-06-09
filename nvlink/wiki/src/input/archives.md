# Archive Processing

nvlink processes Unix `ar(1)` archives — static libraries containing multiple object files bundled under a single `.a` path. When the input loop's 56-byte header probe matches the 8-byte magic `"!<arch>\n"` (regular archive) or `"!<thin>\n"` (thin archive), execution enters the archive subsystem. The subsystem allocates an 80-byte iterator context, then loops over every member entry in the archive. For each member it parses the standard 60-byte `ar` header, resolves the member name (with GNU long-name table support), builds a composite `"archive:member"` path string, extracts or opens the member's content, and re-enters the input loop's type dispatch so the member is classified and processed exactly as if it had been a standalone file on the command line.

The archive layer is split into two tiers: five low-level functions in the `0x487000` range that implement `ar` format parsing, and four thin wrapper functions in the `0x4BDA00` range that translate return codes through a dispatch table (`dword_1D48A50`) and provide the API surface called from `main()`.

> **Key behavioral fact**: nvlink implements **whole-archive** semantics exclusively. Every member of every archive on the command line is unconditionally loaded and processed, with no on-demand / symbol-directed extraction. There is no GNU-ld-style distinction between `-Wl,--whole-archive` and normal archive loading; the only behavior is what GNU ld calls `--whole-archive`. See the [Whole-Archive vs On-Demand](#whole-archive-vs-on-demand-loading) section below for the implications.

| | |
|---|---|
| **Magic detection** | `sub_487A90` at `0x487A90` (51 bytes) — tests `"!<arch>\n"` and `"!<thin>\n"` |
| **Archive open** | `sub_487C20` at `0x487C20` (2,549 bytes) — allocates 80-byte context |
| **Member iteration** | `sub_487E10` at `0x487E10` (5,592 bytes) — parses `ar` headers, advances cursor |
| **Name resolution** | `sub_487AD0` at `0x487AD0` (356 bytes) — builds `"archive:member"` path |
| **Member path accessor** | `sub_488290` at `0x488290` (8 bytes) — returns context+40 (current path) |
| **Context destroy** | `sub_488200` at `0x488200` (144 bytes) — frees context, member lists, path strings |
| **API wrappers** | `sub_4BDAC0` (open), `sub_4BDAF0` (next), `sub_4BDB30` (close), `sub_4BDB60` (get path) |
| **Caller** | `main()` at `0x409800`, archive dispatch branch at source line 850 |
| **Trigger** | First 8 bytes of file match `"!<arch>\n"` or `"!<thin>\n"` |

## Unix ar Format Overview

nvlink implements a parser for the standard Unix `ar` archive format (System V / GNU variant). The format has three structural layers:

### Global Header

Every archive begins with an 8-byte magic string. nvlink recognizes two variants:

| Magic (ASCII) | Hex bytes | Meaning |
|---|---|---|
| `!<arch>\n` | `21 3C 61 72 63 68 3E 0A` | Regular archive — member data is embedded inline |
| `!<thin>\n` | `21 3C 74 68 69 6E 3E 0A` | Thin archive — member data lives in external files |

The first member header begins immediately after this 8-byte global magic, at file offset 8. The old BSD `__.SYMDEF` symbol-table format is **not** recognized; nvlink only handles the GNU / System V `ar` variant. No reference to the `__.SYMDEF` string appears anywhere in the binary's string table (confirmed by scanning `nvlink_strings.json` for `SYMDEF`, which returns zero matches).

**Other formats nvlink does not recognize:**

| Format | Magic / Convention | nvlink behavior |
|---|---|---|
| BSD 4.4 / Mach-O `ar` | `!<arch>\n` global magic shared with GNU, but long names are encoded as `#1/N` in the 16-byte `ar_name` field (where `N` is a variable-width decimal byte length) with the actual filename occupying the first `N` bytes of the member data, followed by the real content | Detected as a regular member: `strchr(name, '/')` finds the `/` at offset 2 of `#1/N`, so `v12 > v10` and the iterator breaks out treating it as a normal entry. `sub_487AD0` then takes the direct-name branch (because `*name == '#'`, not `/`), `strchr` returns the same `/`, and the resolved member name is literally the two-byte string `"#1"`. No fatal error is raised. The member content pointer is `pos + 60`, but its first `N` bytes are the filename ASCII rather than ELF header, so `sub_476E90` rejects it (`0x7F454C46` magic check fails) and the member is silently dropped. The net effect is that BSD long-named members never reach the link set, while the diagnostic output shows the bogus identity `"libfoo.a:#1"`. BSD archives are effectively unsupported. |
| GNU 64-bit symbol table | `/SYM64/` member name (used when total archive size exceeds 4 GB, with 8-byte big-endian offsets in place of 4-byte) | The name starts with `/` followed by the non-digit `S`, so it lands in the same "symbol table" branch as the regular `/` armap and is skipped without inspection. The 64-bit-ness is irrelevant because nvlink never consults symbol-table contents. |
| GNU thin armap (`/SYM64/` inside thin archive) | Same name, same skip logic | Same behavior; thin archives with 64-bit armaps work transparently for nvlink because the armap is ignored either way. |
| AIX big-format `ar` (`<bigaf>\n`, `<aiaff>\n`) | Distinct global magics | Rejected at `sub_487A90`; the file is not an archive from nvlink's perspective and falls through to the next type predicate. |
| COFF import library (Microsoft `.lib`) | Uses `!<arch>\n` magic with special `/` and `//` linker members containing COFF-flavored symbol tables and import descriptors | Magic match succeeds; iteration treats the linker members as the symbol-table / long-name pair and skips them. The COFF object members that follow are then handed to `sub_4BDB70` for classification, which checks for ELF magic and fails, so they are quietly dropped. No diagnostic is emitted — the archive appears to contribute no inputs. |

### Member Header (60 bytes)

Each member is preceded by a fixed 60-byte ASCII header with the following layout:

| Offset | Size | Field | Format | Description |
|---|---|---|---|---|
| 0 | 16 | `ar_name` | ASCII, space-padded (`0x20`) | Member name, or `/` / `//` / `/nnn` special encoding |
| 16 | 12 | `ar_date` | Decimal ASCII | Modification timestamp (seconds since epoch) |
| 28 | 6 | `ar_uid` | Decimal ASCII | Owner user ID |
| 34 | 6 | `ar_gid` | Decimal ASCII | Owner group ID |
| 40 | 8 | `ar_mode` | Octal ASCII | File permissions |
| 48 | 10 | `ar_size` | Decimal ASCII | Member data size in bytes |
| 58 | 2 | `ar_fmag` | `` `\n `` (`0x60 0x0A`) | End-of-header magic (backtick + newline) |

**Fields nvlink actually parses**: only `ar_name` (offset 0, for identity) and `ar_size` (offset 48, for advancing the cursor). The timestamp, UID, GID, mode, and trailing `ar_fmag` bytes are never inspected — nvlink does not validate the header magic and does not extract file metadata. This is consistent with its role as a device-code linker: owner, permissions, and mtime are irrelevant to GPU code.

nvlink reads the size field at offset +48 by copying 10 bytes into a local buffer, NUL-terminating it, and calling `strtol(buf, NULL, 10)`. This matches the decompiled code in `sub_487E10`:

```c
// sub_487E10: size extraction from ar header (line 96 of decompiled output)
// v10 points to start of the 60-byte member header
strncpy(dest, (const char *)(v10 + 48), 10);   // ar_size field, 10 ASCII digits
dest[10] = '\0';
v6 = strtol(dest, NULL, 10);                   // parse decimal
```

Note that no radix check or overflow detection is performed. A malformed size field silently produces either `0` (on parse failure) or `LONG_MAX` (on overflow). A malicious archive with a size string like `"9999999999"` would be parsed as `9999999999`, and the cursor advance check then compares against the buffer size bound, so out-of-range sizes are contained by the bounds check below rather than by size validation.

### Member Data and 2-Byte Alignment

Member data immediately follows the 60-byte header. If the size is odd, a single padding byte (typically `\n`) is appended to maintain 2-byte alignment for the next header. The iteration logic implements this alignment:

```c
// sub_487E10: alignment to next member (lines 85-88 of decompiled output)
// v6 = parsed member size of CURRENT member
// v7 = cursor value (file offset of current member's header, ctx+16)
if ( v7 )                              // not the first iteration
{
    v11 = v6;                          // start with raw size
    if ( v6 % 2 )                      // odd?
        v11 = v6 - v6 % 2 + 2;         // round up to even: size + 1
    v10 = v7 + v11;                    // advance past data
}
else
{
    v10 = *(_QWORD *)a3 + 8LL;         // first iteration: skip 8-byte global magic
}
```

The expression `v6 - v6 % 2 + 2` is an overly-verbose way of writing `v6 + 1` when `v6` is odd, and equals `v6` when `v6` is even — equivalent to `(v6 + 1) & ~1`. The decompiler preserved the original source's branching structure rather than collapsing it.

Bounds checking: after computing the next header position, the function verifies `v10 < buffer + buffer_size`. If the new position is at or past the end of the archive, the iterator returns with `content_out = NULL`, signalling end-of-archive:

The check guards only the *start* of the next header, not its full 60-byte extent. If the archive is truncated such that `buffer + size` falls inside a member header (i.e. `v10 + 60 > buffer + size` but `v10 < buffer + size`), the subsequent `strncpy(dest, v10 + 48, 10)` reads up to 10 bytes past the buffer end into a stack-local. The read is bounded (`strncpy` with `n=10`), so it cannot overflow nvlink's stack, but it does touch unmapped memory if the archive is loaded via `mmap` rather than `sub_476BF0`'s `fread`-into-arena — in practice `sub_476BF0` always allocates the arena buffer with the file size plus arena alignment slop, so the read lands in arena padding and the parsed size becomes whatever those bytes happen to encode. The result is a non-fatal misparse rather than a crash, but a malformed truncated archive can therefore produce arbitrary `member_size` values that subsequent iterations then reject via the same bounds check.

```c
if ( v10 >= (unsigned __int64)&v8->__size[v9] )  // buffer + size
{
    *(_QWORD *)(a3 + 16) = 0;                    // cursor = 0
    *(_QWORD *)(a3 + 24) = 0;                    // member_size = 0
    goto LABEL_19;                               // return with *a1 = NULL
}
```

### Special Members

Several member names have special meaning in GNU `ar` archives. nvlink handles three:

| Name pattern | Identity | nvlink behavior |
|---|---|---|
| `/` followed by non-digit | GNU symbol table (armap), including `/SYM64/` 64-bit variant and any other `/<non-digit>` name | Detected: `name[0]=='/'` and `isdigit(name[1])==false`. Cursor advances past it; member content is **ignored**. The check is purely structural — it does not distinguish `/` from `/SYM64/`, and any future variant whose name happens to start with `/<non-digit>` would be silently absorbed here. |
| `//` (slash + slash) | GNU long-name string table | Detected: `v34 == 2` (the `v30 + 1` branch where `v30` equals `(name[1]==47)` returns 2). Pointer stored at `ctx+48` for subsequent long-name lookups. Cursor advances past it. Only the *first* `//` member is honored — if a malformed archive contains two, the second overwrites `ctx->longnames_ptr` and any earlier `/NNN` references that have already been resolved retain their (already-allocated) strings, but subsequent references resolve against the new table. |
| `__.LIBDEP` | Library dependency metadata | 9-byte prefix comparison (see `__.LIBDEP` Skipping below). Always skipped. |

Note that nvlink's detection of the symbol table is **structural, not semantic**: it simply skips any member whose name starts with `/` and is not a long-name reference. It never reads the armap contents, meaning it has no way to use the symbol index for on-demand loading even if it wanted to — which it doesn't (see [Whole-Archive vs On-Demand Loading](#whole-archive-vs-on-demand-loading)).

## Magic Detection (sub\_487A90)

The archive detection function is one of the simplest predicates in the binary. It is called twice from `main()` (at decompiled-source lines 629 and 789) as part of the file-type dispatch chain.

```c
// sub_487A90 at 0x487A90 -- 51 bytes
// Decompiled:
bool __fastcall sub_487A90(const void *a1, unsigned __int64 a2)
{
    bool result = 0;
    if ( a2 > 7 )                                    // need >= 8 bytes
    {
        result = 1;
        if ( memcmp(a1, "!<arch>\n", 8u) )           // not regular?
            return memcmp(a1, "!<thin>\n", 8u) == 0; // try thin
    }
    return result;
}
```

The function requires at least 8 bytes (`size > 7`). It checks for the regular archive magic first, then falls through to the thin archive magic. This means both archive types enter the same processing pipeline, with thin-vs-regular distinction deferred to the open function. A non-archive file (or a file shorter than 8 bytes) returns `false`, and `main()` then falls through to the next file-type predicate in the chain (see [File Type Detection](file-type-detection.md)).

## Archive Open (sub\_487C20)

Opening an archive allocates an 80-byte iterator context and initializes it from the raw file buffer. The function uses `setjmp`/`longjmp` error handling to gracefully roll back the context allocation on OOM.

### Signature

```c
// sub_487C20 at 0x487C20 -- 2,549 bytes
// Returns 0 on success, 1 on error (OOM / arena allocation failure)
int archive_open(void **ctx_out,     // a1: receives the allocated context pointer
                 void  *buffer,      // a2: raw file data (from sub_476BF0 read)
                 size_t size,        // a3: buffer size in bytes
                 const char *path);  // a4: archive file path (for diagnostics)
```

### Context Layout (80 bytes)

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `buffer` | Pointer to the raw archive data |
| 8 | 8 | `size` | Total buffer size in bytes |
| 16 | 8 | `cursor` | File offset of the current member's header (0 on first call) |
| 24 | 8 | `member_size` | Size of the current member's data (from `ar_size`) |
| 32 | 8 | `path` | Arena-allocated copy of the archive file path |
| 40 | 8 | `member_path` | Constructed `"archive:member"` path for the current member |
| 48 | 8 | `longnames_ptr` | Pointer to the `//` (GNU long-name table) member header, or NULL |
| 56 | 8 | `member_list` | Singly-linked list of extracted member records (tracking for cleanup) |
| 64 | 8 | `path_list` | Singly-linked list of allocated `"archive:member"` path strings |
| 72 | 1 | `is_thin` | Boolean: 1 if `"!<thin>\n"`, 0 if `"!<arch>\n"` |
| 73 | 7 | (padding) | Unused; zero-initialized via 16-byte stores |

Total: 80 bytes, confirmed by the literal `sub_4307C0(v12, 80)` call at source line 57 of `sub_487C20_0x487c20.c`.

The initialization sequence allocates the 80 bytes via `sub_4307C0` (arena allocator), zeros the 64-byte range from offset 16 to offset 79 with four 128-bit stores (the `*((_OWORD *)v15 + N) = 0` lines), copies the buffer pointer into slot 0 and the size into slot 1 (`v15[1] = a3`), then strdups the archive path into the arena and stores it at offset +32 (`v15[4] = v23`), then checks for thin archive format:

```c
// sub_487C20: thin archive detection (source lines 74-79)
v26 = 0;                                 // default: not thin
v27 = v15[1] <= 7u;                      // size <= 7?
v15[4] = v23;                            // store archive path
if ( !v27 )                              // size > 7
    v26 = memcmp((const void *)*v15, "!<thin>\n", 8u) == 0;
*((_BYTE *)v15 + 72) = v26;              // store is_thin flag at offset +72
```

Note that the flag is set **once** during open and never changes. An archive's thin-ness is a file-format property, not a runtime setting.

This re-test is redundant with the magic detection that already occurred in `sub_487A90` (called from `main()` before the archive dispatch branch was taken), but `sub_487C20` cannot trust that prior result because its calling contract takes only `(buffer, size, path)` without a "kind" tag. The retest is cheap (8-byte `memcmp`) and makes the open function self-contained. The asymmetry also means the regular-vs-thin distinction is encoded only in offset +72; nothing else in the iterator context records the global magic, so a buffer whose first 8 bytes are corrupted to `!<arch>\n` would still be processed as regular even if it originally was thin.

### Error Handling via setjmp/longjmp

`sub_487C20` installs a `setjmp` handler at entry. The local `env` buffer is linked into a per-thread context slot fetched via `sub_44F410` (a thread-local storage accessor that returns a pointer to a 16-byte per-thread structure). The two bytes at `*v30` and `v30[1]` represent error flags that are saved on entry and may be overwritten by `longjmp` callers. The 8-byte `v32 = v5` at offset +8 saves the previous `env` pointer so that nested archive operations can still unwind correctly.

If any arena allocation fails (OOM), `sub_4307C0` returns NULL, and `sub_45CAC0` is called with the allocation size. `sub_45CAC0` is the OOM diagnostic reporter, and typically ends by calling `longjmp(env, 1)` which unwinds back to `sub_487C20`'s `_setjmp` return. The `if ( _setjmp(env) )` branch at source line 49 handles this case by restoring the saved thread-local state and setting `v30[0] = 0x0101` (the `*(_WORD *)v30 = 257` store), which is the error-flag encoding that the wrapper `sub_4BDAC0` later translates to the API-level error code via `dword_1D48A50`.

On error, any partially-allocated 80-byte context at `v31` is freed via `sub_431000` (arena_free), the thread-local error byte is cleared, and the function returns 1. On success, `*ctx_out = v15` is assigned and the function returns 0.

## Member Iteration (sub\_487E10)

This is the core of the archive subsystem. Each call advances to the next member, parses its header, resolves its name, and returns a pointer to its content. At 5,592 bytes, it is the largest archive-handling function by a wide margin, and it also installs its own `setjmp` frame for OOM rollback.

### Signature

```c
// sub_487E10 at 0x487E10 -- 5,592 bytes
// Returns: 0 = no more members (success with *content=NULL)
//          1 = general error
//          2 = thin archive resolve failed (external file missing)
//          3 = NULL argument (invalid caller)
int archive_next_member(const char **content_out,   // a1: receives content pointer
                        void       *size_out,        // a2: receives member size (via opaque
                                                     //     pthread_mutexattr_t*; the first 8
                                                     //     bytes are used as a size slot)
                        void       *ctx);            // a3: the 80-byte iterator context
```

The `pthread_mutexattr_t *` type for `a2` is a decompiler artifact — the caller (`sub_4BDAF0`) passes a local `pthread_mutexattr_t v4[4]` array, and the function writes `*size = v6` to its first qword. This is an opaque by-reference `size_t` out-parameter, not an actual mutex-attribute structure.

### Iteration Algorithm

The function implements a loop that may skip multiple internal members (the `/` symbol table, the `//` long-name table, and any `__.LIBDEP` entries) before yielding a real member to the caller:

```text
archive_next_member(content_out, size_out, ctx):
    |
    +-- Validate: return 3 if ctx==NULL, size_out==NULL, or content_out==NULL
    |       if ( a3 == 0 || a2 == 0 || !a1 ) return 3;
    |
    +-- Save per-thread error state (setjmp frame install)
    |
    +-- LOOP:
    |   |
    |   +-- Compute next header position:
    |   |     v7 = ctx->cursor        (offset +16, 0 on first call)
    |   |     v6 = ctx->member_size   (offset +24, 0 on first call)
    |   |     if v7 != 0:
    |   |         pos = v7 + ((v6 + 1) & ~1)   // align to 2-byte boundary
    |   |     else:
    |   |         pos = ctx->buffer + 8         // skip the 8-byte global magic
    |   |
    |   +-- Check bounds: if pos >= ctx->buffer + ctx->size:
    |   |         ctx->cursor = 0
    |   |         ctx->member_size = 0
    |   |         *content_out = NULL
    |   |         return 0   (normal end-of-iteration)
    |   |
    |   +-- Parse size: strncpy(dest, pos+48, 10); dest[10]=0; v6 = strtol(dest, 0, 10)
    |   |
    |   +-- Classify member name at pos:
    |   |     v12 = strchr(pos, '/')
    |   |     |
    |   |     +-- v12 > pos (slash appears inside the name, not at position 0):
    |   |     |     --> Regular member, real name
    |   |     |         Name runs from pos to v12 (slash not included)
    |   |     |         break out of classification loop
    |   |     |
    |   |     +-- v12 == pos (name starts with '/'):
    |   |     |     --> Either symbol table, long-name table, or long-name reference
    |   |     |         Check isdigit(pos[1]) via __ctype_b_loc() & 0x800
    |   |     |         |
    |   |     |         +-- digit follows --> long-name reference "/NNN"
    |   |     |         |                     --> break: treat as real member; name resolved later
    |   |     |         |
    |   |     |         +-- non-digit follows --> symbol table or long-name table
    |   |     |               Compute v30 = (pos[1] == '/') ? 1 : 0
    |   |     |               Compute v34 = v30 + 1   // 2 for "//", 1 for "/"
    |   |     |               if (v34 == 2):   // long-name table "//"
    |   |     |                   ctx->longnames_ptr = pos   (offset +48)
    |   |     |               (otherwise "/" symbol table: stored longnames_ptr unchanged)
    |   |     |               advance cursor: ctx->cursor = pos + 60
    |   |     |                               ctx->member_size = v6
    |   |     |               continue loop (skip this member)
    |   |     |
    |   |     +-- After the break, check for "__.LIBDEP" prefix:
    |   |           byte-by-byte compare 9 chars against "__.LIBDEP"
    |   |           if match:
    |   |               advance cursor: ctx->cursor = pos + 60
    |   |                               ctx->member_size = v6
    |   |               continue loop
    |   |
    |   +-- Regular member found: build composite path via sub_487AD0
    |   |     new_path = sub_487AD0(ctx->path, pos, ctx->longnames_ptr)
    |   |     Free previous ctx->member_path if any (arena_free)
    |   |     ctx->member_path = new_path   (offset +40)
    |   |
    |   +-- Extract content:
    |   |     if ctx->is_thin == 0:   // regular archive
    |   |         ctx->cursor = pos + 60                  // Note: member size NOT added yet
    |   |         *size_out = ctx->member_size = v6
    |   |         *content_out = sub_476E90(pos + 60)     // validate ELF magic, return same ptr
    |   |
    |   |     else:   // thin archive
    |   |         Parse external path: strchr(new_path, ':') + 1
    |   |         buf = sub_476BF0(external_path, 0)       // fopen/fread into arena
    |   |         if buf == NULL:
    |   |             clear error flag, return 2           // thin resolve failure
    |   |         *size_out = <size from sub_476BF0>
    |   |         *content_out = sub_476E90(buf)           // validate ELF magic
    |   |
    |   +-- Append to member tracking list: sub_4644C0(content, ctx+56)
    |   +-- Return 0 (success)
```

The key observation is that the cursor at `ctx+16` is advanced to **`pos + 60`** (past the header, not past the data) upon yielding a regular member. The member-size is stored separately at `ctx+24`. On the next call, the loop's initial "compute next position" step reconstructs `pos = cursor + aligned(member_size)`, which correctly lands on the next header.

### GNU Long Name Resolution

Standard `ar` headers only provide 16 bytes for the member name. When a member's name exceeds this limit, GNU `ar` uses a two-part encoding:

1. The archive contains a special member named `//` (two forward slashes). Its data is a concatenated string table where each name is terminated by `/\n`.

2. Members with long names have their `ar_name` field set to `/NNN` where `NNN` is a decimal byte offset into the `//` string table.

nvlink detects this pattern in `sub_487E10` by checking whether the name field starts with `/` followed by a digit character (tested via `__ctype_b_loc()` with the isdigit bitmask `0x800`). When the test succeeds, the iteration loop breaks and treats the member as regular; the actual name resolution is then performed by `sub_487AD0`:

```c
// sub_487AD0: long-name resolution path (source lines 22-31)
if ( *a2 == 47 && ((*__ctype_b_loc())[a2[1]] & 0x800) != 0 )  // '/' + digit
{
    v20 = strtol(a2 + 1, 0, 10);                              // parse offset
    if ( !a3 )                                                // longnames_ptr NULL?
        sub_467460(dword_2A5BD80, "longnames header not found");
    v9 = (const char *)(a3 + v20 + 60);                       // skip "//" header
    v11 = (unsigned int)strchr(v9, 47) - (_DWORD)v9;          // name length to '/'
}
```

The pointer arithmetic `a3 + v20 + 60` is crucial: `a3` is the start of the `//` member's 60-byte header, so `a3 + 60` is the start of the long-name string table data, and adding `v20` (the decimal offset from the name field) gets to the specific filename. The name is terminated by a `/` character within the table (each entry in the GNU format ends with `/\n`), so `strchr(v9, '/')` locates the end.

If a long-name reference appears before the `//` member has been seen (i.e., the `//` member was placed *after* long-name members in the archive), `a3` is NULL and `sub_467460` is called with the diagnostic string `"longnames header not found"` — a fatal error. A well-formed archive always places the `//` table before any members that reference it.

The decimal offset parser is `strtol(a2 + 1, 0, 10)` with no upper bound and no validation against `//`-table size. A malicious archive whose `ar_name` reads `/4000000000` produces `v9 = longnames_ptr + 60 + 4_000_000_000`, which on a 64-bit host overflows past the arena buffer end. The subsequent `strchr(v9, '/')` then scans linearly through whatever memory follows — typically other arena allocations or unmapped pages, the latter producing a SIGSEGV. nvlink does not bound-check the offset against the `//` member's declared size. In practice, GNU `ar` produces offsets bounded by the long-name table size, so this is a robustness concern for hostile inputs rather than a correctness issue for honest toolchains.

## Name Resolution and Path Construction (sub\_487AD0)

Every archive member receives a composite path string in the format `"archive_path:member_name"`. This path serves as the member's identity throughout the linker pipeline — it appears in diagnostics, symbol records, and debug information.

```c
// sub_487AD0 at 0x487AD0 -- 356 bytes
// Returns: arena-allocated string "archive_path:member_name"
char *build_member_path(const char *archive_path,    // a1 (called "src"): the .a file path
                        const char *header_ptr,      // a2: points to ar_name field
                        void       *longnames_ptr);  // a3: the "//" string table, or NULL
```

The function handles two name formats:

**Direct name** (no long-name reference): The member name starts at `header_ptr` and extends to the first `/` character (which terminates `ar` names in GNU format). The terminating `/` is not included in the output. If no `/` is found at all (meaning the name fills all 16 bytes with no terminator), the function calls `sub_467460` with `"unexpected archive format"` — a fatal error indicating a malformed `ar` header.

**Long-name reference** (`/offset`): When the name starts with `/` followed by a digit, the function looks up the offset in the long-name string table. The resolved name starts at `longnames_ptr + offset + 60` (60 bytes past the `//` member header) and extends to the next `/`.

In both cases, the function allocates `strlen(archive_path) + 1 + name_length + 1` bytes via `sub_4307C0`, then constructs the composite string:

```c
// Source lines 51-54 of sub_487AD0_0x487ad0.c
memcpy(v16, src, v6);                          // "libfoo.a"
*((_BYTE *)v16 + v6) = 58;                     // ":"  (0x3A)
memcpy((char *)v16 + v6 + 1, v9, v11);         // "bar.o"
*((_BYTE *)v16 + v11 + v6 + 1) = 0;            // NUL terminator
```

An `ar` archive `libfoo.a` containing member `bar.o` produces the path `"libfoo.a:bar.o"`. This colon-separated format is consistent with how other linkers (GNU ld, lld) identify archive members in diagnostics. nvlink's diagnostics (e.g. "multiply defined symbol") display this path unchanged, making it easy to trace a linker error back to a specific member.

Memory ownership: each composite path is arena-allocated and tracked in `ctx->path_list` (offset +64 in the iterator context). When `sub_488200` tears down the context, it walks the list and frees each allocated path. The current `ctx->member_path` (offset +40) is the most recently built path and is also freed on iterator destruction.

## Thin Archive Support

Thin archives (`"!<thin>\n"` magic) differ from regular archives in one critical way: member data is not embedded in the archive file. Instead, each member header's data region is empty (or contains only the member's path), and the actual content lives in a separate file on disk. This is useful for large build systems where duplicating object files into an archive would double the disk footprint.

When `sub_487E10` encounters a member in a thin archive (`ctx->is_thin == 1` at offset +72), it takes a different extraction path:

1. Builds the composite path via `sub_487AD0` as usual — this produces `"libfoo.a:path/to/bar.o"` where the part after the `:` is the external filename recorded in the thin archive's name field.
2. Extracts the external file path from the composite path: `external = strchr(composite, ':') + 1`.
3. Opens and reads the external file via `sub_476BF0` — which internally calls `fopen(path, "rb")`, `fseek/ftell` to determine size, `sub_4307C0` to allocate a buffer, and `fread` to load the full content. The returned pointer is an arena-owned read buffer.
4. Validates the loaded content via `sub_476E90`, which checks for ELF magic (`0x7F454C46`) at offset 0 and returns the same pointer (or NULL on mismatch).
5. If `sub_476BF0` fails (file not found, read error), the iterator returns 2 — "thin archive resolve failure". The caller (`sub_4BDAF0` -> `main()`) translates this through `dword_1D48A50` into a fatal error.

For regular archives, the content pointer is computed as `header_ptr + 60` (immediately after the 60-byte `ar` header), pointing directly into the memory-mapped archive buffer. No additional I/O is required, and no extra allocation is performed. This makes regular-archive member access effectively free after the initial parse.

## __.LIBDEP Skipping

The `__.LIBDEP` pseudo-member is a GNU extension that records library dependency information — a list of libraries that should also be searched when this archive is used. nvlink explicitly skips this member; it never parses or acts on LIBDEP contents. The detection in `sub_487E10` uses a 9-byte comparison against the string `"__.LIBDEP"` at the start of the member name:

```c
// sub_487E10: LIBDEP detection (simplified from decompiled byte-compare loop at lines 149-162)
// v5 = "__.LIBDEP"
// v15 = 9
// a2 = header_ptr  (pointer into archive at start of member header)
// Loop compares bytes until mismatch or 9 bytes consumed
do {
    if ( !v15 ) break;
    v13 = a2->__size[0] < *v5;
    v14 = a2->__size[0] == *v5;
    a2 = (pthread_mutexattr_t *)((char *)a2 + 1);
    ++v5;
    --v15;
} while ( v14 );
if ( match )
    goto LABEL_15;      // ctx->cursor = pos+60, ctx->member_size = v6; continue
```

The comparison is a hand-rolled memcmp-like inline loop, preserved by the decompiler because the compiler inlined strncmp/memcmp. The effect is a 9-byte prefix check; anything starting with `"__.LIBDEP"` is skipped. This means dependency metadata members never reach the type-dispatch system and are invisible to the rest of the linker.

Two xrefs to the `"__.LIBDEP"` string exist in the binary (at `0x487f1d` and `0x4880ad`, both inside `sub_487E10`), reflecting the two code paths that may encounter it — one for regular members and one for the post-slash-check fallthrough, due to the decompiler's control-flow reconstruction producing two separate comparison blocks.

## Whole-Archive vs On-Demand Loading

A traditional Unix linker (GNU ld, lld, BSD ld) processes archives **on-demand**: at each archive encounter, it scans the archive's symbol table (armap, the `/` member), identifies members that define any currently-unresolved symbol, and loads only those members. Members whose symbols are not currently needed are skipped. The `-Wl,--whole-archive` flag overrides this behavior and unconditionally loads every member. ld also re-scans an archive when later inputs introduce new undefined references to it; `--start-group`/`--end-group` makes this re-scan iterate to a fixed point across a group of archives.

> **QUIRK vs GNU ld — archive traversal**: nvlink performs a **single pass** over each archive that loads **every** member. There is no symbol-index consultation, no on-demand extraction, no second pass when later inputs introduce new undefined symbols, no `--start-group`/`--end-group`, no `--whole-archive`/`--no-whole-archive`, and consequently no fixed-point convergence loop. The `qword_2A5F2F0` seen-archive set positively prevents an archive from being processed more than once even if it appears multiple times on the command line. The only state machine here is "have we seen this path? if not, iterate all members exactly once".

**nvlink does not implement on-demand loading.** The archive dispatch in `main()` (`main_0x409800.c` lines 746-790) unconditionally iterates every member of every archive and processes each one through `sub_42AF40` (the member handler), without ever consulting the symbol resolver to check if the member is needed:

```c
// main_0x409800.c lines 752-778 (condensed)
v273 = sub_476BF0(v68, 0);                            // load entire archive file
v223 = sub_4BDAC0(&v269, (pthread_mutexattr_t *)v273, v222, v68);  // open archive iterator
while ( 1 )
{
    v224 = sub_4BDAF0((const char **)&s1, (__int64)v269);  // next member
    if ( !s1 )   break;                                    // end of archive
    v225 = (char *)sub_4BDB60((__int64)v269);              // get composite path
    v226 = sub_4BDB70();                                   // file-type dispatch on member
    if ( ptr[0].tv_sec )
        sub_42AF40(ptr[0].tv_sec, (__int64)s1, v225, v52, 1, ...);  // process unconditionally
    else
        // untyped member -- tracked via qword_2A5F2E0
        byte_2A5F212 = 1;
}
v228 = sub_4BDB30(v269, (unsigned __int64)v68);            // close archive
sub_4644C0((__int64)v68, (pthread_mutexattr_t *)&qword_2A5F2F0);  // record seen path
```

There is **no** check against the symbol table, no "is this member needed?" query, and no symbol-directed pruning. Every member is loaded into the linker's intermediate representation and added to the merged symbol/section set. The symbol resolver (see [Symbol Resolution](../linker/symbol-resolution.md)) then deduplicates symbols, resolves weak-vs-strong conflicts, and handles multiple-definition diagnostics on the fully loaded set.

### How Unneeded Members Get Removed

Although every archive member is initially loaded, nvlink still produces lean outputs thanks to its separate **dead code elimination** pass (see [Dead Code Elimination](../linker/dead-code-elimination.md)). The flow is:

1. **Archive iteration** (this page): every member's cubin content is parsed, its symbols are added to the global symbol table, its sections are added to the merged ELF, and its callgraph edges are recorded.

2. **Symbol resolution** ([Symbol Resolution](../linker/symbol-resolution.md)): multiply-defined strong symbols produce errors; weak symbols are overridden by strong; undefined references are flagged. Archive members that defined duplicate symbols may trigger "multiply defined" errors here, unlike GNU ld where unneeded archive members would have been skipped silently.

3. **Dead code elimination** ([Dead Code Elimination](../linker/dead-code-elimination.md)): the DCE pass walks the callgraph starting from entry points (host-launched kernels identified via `--use-host-info` / `--kernels-used`) and marks every reachable function. Unreachable functions from any source — including archive members — are then swept out of the output. The gate function is `sub_426AE0` (`mark_used_symbols`), the core DCE function is `sub_44AD40` (`dead_code_eliminate`), and both emit verbose diagnostics like `"dead function %d(%s)"` and `"removed un-used section %s (%d)"` when `-v` is active.

The practical effect is that from the user's perspective, nvlink behaves similarly to a symbol-directed linker: if `libdevice.a` contains 200 device functions and the application only uses 5, the final cubin contains only those 5 (plus their transitive callees). But the internal mechanism is completely different — every function was momentarily present in the linker's state and was then deleted by DCE, rather than never being loaded at all.

### Consequences of Whole-Archive Loading

This design has several implications:

- **Multiple definition errors are harder to work around**: if two archives both contain a strong definition of the same symbol, GNU ld would pick the first one and ignore the second (because the second is never loaded). nvlink loads both and emits a multiply-defined diagnostic. Users must manually exclude conflicting archives.

- **Archive order is less significant for correctness**: since all members are loaded, the order in which archives appear on the command line doesn't affect which definition "wins" as much as it does in GNU ld. The weak/strong resolution rules (documented in [Weak Symbols](../linker/weak-symbols.md)) apply uniformly across the full set.

- **Link memory use is higher**: the intermediate state carries all symbols and all sections from all archives simultaneously, even those that will be deleted by DCE. For very large archives (libdevice family, PhysX shaders), this can consume noticeable memory.

- **DCE is not optional for lean output**: users who disable DCE (via `--ignore-host-info` without providing explicit `--kernels-used` / `--variables-used` lists) will end up with every archive function in their final cubin. The `byte_2A5F212` and `byte_2A5F213` flags gate this behavior; see [Dead Code Elimination](../linker/dead-code-elimination.md) for the guard logic.

- **The `/` symbol table is never consulted**: nvlink skips the symbol table member but never reads its contents. This is a deliberate simplification consistent with whole-archive semantics — without on-demand loading, there's nothing useful the armap could tell the linker.

## Worked Example: Processing libdevice.a

Consider the canonical CUDA device runtime archive `libdevice.a` (which in CUDA installations is actually shipped as `libdevice.10.bc`, but users can also pass archive-packaged variants via `-L` / `-l`). Suppose the archive contains:

```text
libdevice.a:
    / (symbol table, 512 bytes)
    // (long-name table, 96 bytes)          -- contains "__nv_sqrt_device_impl.o/\n__nv_fma_device_rounding.o/\n..."
    __nv_sqrt.o  (cubin, 2408 bytes)
    /0           (long-name reference, cubin, 3192 bytes)   -- resolves to "__nv_sqrt_device_impl.o"
    __nv_fma.o   (cubin, 1856 bytes)
    /24          (long-name reference, cubin, 4100 bytes)   -- resolves to "__nv_fma_device_rounding.o"
```

Here is the step-by-step processing trace:

**Step 1**: `main()` reads 56 bytes from `libdevice.a`, calls `sub_487A90(ptr, 56)`, which succeeds (matches `"!<arch>\n"`). Control enters the archive dispatch branch at source line 850.

**Step 2**: `main()` calls `sub_476BF0("libdevice.a", 0)` to load the full archive content into an arena buffer. Assume this returns a pointer `buffer` of size, say, 12288 bytes.

**Step 3**: `sub_4BDAC0(&ctx, buffer, 12288, "libdevice.a")` is called, which delegates to `sub_487C20`. This:
- Allocates an 80-byte context
- Stores `buffer` at offset 0, `12288` at offset 8
- Zeros offsets 16-71
- Strdups `"libdevice.a"` into the arena and stores the pointer at offset 32
- Tests `memcmp(buffer, "!<thin>\n", 8)` — false, so sets `is_thin = 0` at offset 72
- Returns 0 (success) via `dword_1D48A50[0]`

**Step 4**: First call to `sub_4BDAF0(&content, ctx)` -> `sub_487E10`:
- `cursor = 0`, so `pos = buffer + 8` (skip global magic)
- Parse `ar_size` at `pos + 48`: gets `512` (size of `/` member data)
- `strchr(pos, '/')` returns `pos` itself (name starts at `pos` and begins with `/`)
- `pos == v12`, so check `isdigit(pos[1])`. The character after `/` is a space (0x20), not a digit, so isdigit is false.
- `pos[1] != '/'`, so `v30 = 0`, `v34 = 1`. This is the `/` symbol table. `longnames_ptr` is NOT updated.
- Advance: `cursor = pos + 60`, `member_size = 512`
- `continue` loop (skip the member)

**Step 5**: Second iteration inside the first `sub_487E10` call:
- `pos = cursor + ((512 + 1) & ~1) = cursor + 512` (512 is already even, so no padding)
- Parse `ar_size` at new `pos + 48`: gets `96` (size of `//` long-name table)
- `strchr(pos, '/')` returns `pos` (again name starts with `/`)
- `pos[1]` is another `/`, so `v30 = 1`, `v34 = 2`. This is the `//` long-name table.
- `longnames_ptr = pos` (stored at ctx+48)
- Advance: `cursor = pos + 60`, `member_size = 96`
- `continue` loop

**Step 6**: Third iteration inside the first `sub_487E10` call:
- `pos = cursor + 96`
- Parse `ar_size`: gets `2408`
- `strchr(pos, '/')` returns some offset inside the name (e.g., `pos + 11` where the `/` terminator is after `__nv_sqrt.o`)
- `v12 > pos`, so this is a regular member with a direct (short) name
- Check `__.LIBDEP`: first byte is `_`, second is `_`, third is `n` — does NOT match `"__.LIBDEP"` at position 2. Fall through.
- Call `sub_487AD0("libdevice.a", pos, longnames_ptr)`:
    - `*pos == '_'`, not `/`, so take the direct-name branch
    - `v10 = strchr(pos, '/')`, returns the terminator
    - Length is 11 bytes: `"__nv_sqrt.o"`
    - Allocate `strlen("libdevice.a") + 1 + 11 + 1 = 12 + 1 + 11 + 1 = 25` bytes
    - Construct `"libdevice.a:__nv_sqrt.o"`
- `ctx->member_path = "libdevice.a:__nv_sqrt.o"` (offset +40)
- `is_thin == 0`, so `content = sub_476E90(pos + 60)` — validates ELF magic at the start of the member data and returns the pointer
- `*size_out = 2408`, `*content_out = pos + 60`
- Track in member list via `sub_4644C0(content, ctx+56)`
- Advance: `cursor = pos + 60`, `member_size = 2408`
- Return 0

**Step 7**: Back in `main()`, the member dispatch kicks in:
- `s1` is the content pointer, `v316 = sub_4BDB60(ctx)` returns `"libdevice.a:__nv_sqrt.o"`
- `sub_4BDB70(ptr, s1, v316)` classifies the content: ELF magic found, `e_machine == 190`, so it is classified as a cubin
- `sub_42AF40(ptr[0], s1, v316, v55, 1, &v365, &v355, &v353, &v354)` is called with the classification result
- The `1` argument indicates "from archive" — the member is processed and merged into the link set
- The member's path `"libdevice.a:__nv_sqrt.o"` is attached to its symbols, so any later linker diagnostic referencing these symbols will identify their origin

**Step 8**: Second call to `sub_4BDAF0` returns the second regular member, which is actually a long-name reference `/0`:
- `pos = previous + (2408+1)&~1 = previous + 2408`. (2408 is even.)
- `strchr(pos, '/')` returns `pos`
- `pos[1] = '0'`, which IS a digit (isdigit matches 0x800)
- This is a long-name reference — break out of classification, treat as regular member
- Check `__.LIBDEP`: first byte `/`, no match, fall through
- Call `sub_487AD0("libdevice.a", pos, longnames_ptr)`:
    - `*pos == '/'`, `pos[1] == '0'` is digit, take long-name branch
    - `v20 = strtol("0/__nv_sqrt_device_impl.o/...", NULL, 10) = 0` (stops at non-digit)
    - `v9 = longnames_ptr + 0 + 60` — start of the long-name table data, where the first entry is `"__nv_sqrt_device_impl.o"`
    - `strchr(v9, '/')` gives the terminator position, length = 24
    - Allocate, construct `"libdevice.a:__nv_sqrt_device_impl.o"`
- Continue exactly as step 6-7 above
- Return 0 with content at `pos + 60`

**Step 9**: Third regular member `__nv_fma.o` is processed analogously to step 6-7.

**Step 10**: Fourth regular member is `/24` — another long-name reference:
- Parse offset = 24 (decimal)
- `v9 = longnames_ptr + 24 + 60` — this skips past the first 24 bytes of the long-name table (`"__nv_sqrt_device_impl.o/"` which is 23 chars + `\n` = 24 bytes)
- Lands on `"__nv_fma_device_rounding.o"`
- Build path `"libdevice.a:__nv_fma_device_rounding.o"`
- Process as cubin member

**Step 11**: Fifth iteration: `pos` advances past the last member's data. New `pos >= buffer + 12288`, so the bounds check fails:
- `ctx->cursor = 0`
- `ctx->member_size = 0`
- `*content_out = NULL`
- Return 0

**Step 12**: `main()` sees `s1 == NULL` (because `content_out` was set to NULL), breaks out of the `while(1)` loop.

**Step 13**: `main()` calls `sub_4BDB30(ctx)` -> `sub_488200`:
- Clears `ctx->buffer`, `ctx->size`
- Frees `ctx->path` (archive path string)
- Frees `ctx->member_path` (last composite path string)
- Walks `ctx->member_list` at +56 calling `nullsub_4()` per entry (no-op — content pointers are not owned by this list), then frees the list container
- Walks `ctx->path_list` at +64, freeing each allocated composite path string, then frees the list container
- Frees the 80-byte context itself
- Returns 0

**Step 14**: Archive processing for `libdevice.a` is complete. The archive file path is appended to `qword_2A5F2F0` (the "seen archives" tracking set) via `sub_4644C0` so that `-l` resolution doesn't double-load it. `main()` continues to the next input file.

All four cubin members (`__nv_sqrt.o`, `__nv_sqrt_device_impl.o`, `__nv_fma.o`, `__nv_fma_device_rounding.o`) are now in the link set, regardless of whether the host actually uses `__nv_sqrt_device_impl` or `__nv_fma_device_rounding`. The dead code elimination pass ([Dead Code Elimination](../linker/dead-code-elimination.md)) will later remove the unreferenced ones from the final output.

## API Wrapper Layer

The functions called from `main()` are not the low-level parsers directly. Instead, four thin wrappers at `0x4BDAC0`-`0x4BDB60` provide a normalized API with return code translation through `dword_1D48A50`:

| Wrapper | Address | Delegates to | Description |
|---|---|---|---|
| `sub_4BDAC0` | `0x4BDAC0` | `sub_487C20` | Open: allocate context from buffer |
| `sub_4BDAF0` | `0x4BDAF0` | `sub_487E10` | Next: advance to next member |
| `sub_4BDB30` | `0x4BDB30` | `sub_488200` | Close: destroy context, free resources |
| `sub_4BDB60` | `0x4BDB60` | `sub_488290` | Get path: return current member's composite path (thunk) |

Each wrapper translates the internal return code through a lookup table:

```c
// sub_4BDAC0 (archive_open wrapper) -- verbatim from decompiled output
__int64 sub_4BDAC0(_QWORD *a1, pthread_mutexattr_t *a2, __int64 a3, const char *a4)
{
    __int64 v4 = (unsigned int)sub_487C20(a1, a2, a3, a4);
    __int64 result = 1;                      // default: error
    if ( (unsigned int)v4 <= 2 )
        return (unsigned int)dword_1D48A50[v4];   // translate
    return result;
}
```

The `dword_1D48A50` table maps internal codes 0/1/2 to the API-level codes that `main()` expects. For codes > 2, the wrapper returns 1 (error) directly without consulting the table. This indirection isolates the archive parser's internal error semantics from the linker's top-level error handling, and also allows the table to be shared across other subsystems (several other wrapper functions in the `0x4BD*` range use the same table).

`sub_4BDB60` is a pure thunk that forwards to `sub_488290` with no return code translation — `sub_488290` simply returns `*(ctx + 40)`, which is always a valid pointer or NULL, with no error semantics.

## Member Re-Entry into Type Dispatch

Each extracted archive member re-enters the input loop's type classification system via `sub_4BDB70` (the file-type classifier that the archive loop calls immediately after `sub_4BDAF0`). The member's content pointer (an in-memory pointer for regular archives, or a freshly-loaded buffer for thin archives) is classified by the same magic-number checks used for top-level input files:

- ELF magic (`0x7F454C46`) with `e_machine == 190` --> cubin handler
- Fatbin magic (`0xBA55ED50`) --> fatbin extraction (recursion into `sub_42AF40`)
- NVVM IR magic (`0x1EE55A01`) --> IR module registration
- PTX `.version` header --> ptxas JIT compilation (see embedded ptxas pipeline)
- Nested archive magic (`!<arch>\n`) --> recursive archive iteration

This means an archive can contain cubins, fatbins, NVVM IR modules, or even nested archives, and each will be handled correctly. In practice, CUDA static libraries (`.a` files) produced by `nvcc` most often contain cubin members (device ELF with `e_machine == 190`) or fatbin members (which in turn contain per-architecture cubins).

The member's composite path (`"libfoo.a:bar.o"`) is carried through the entire pipeline, appearing in error messages, symbol table entries, and debug information. This provides clear provenance when a linker error traces back to a specific member within an archive.

## Context Destruction (sub\_488200)

When iteration is complete (no more members or an error), `sub_4BDB30` delegates to `sub_488200` to tear down the iterator context. Full decompiled source:

```c
// sub_488200 at 0x488200 -- 144 bytes
__int64 __fastcall sub_488200(_QWORD *a1, unsigned __int64 a2)
{
    *a1 = 0;                                   // clear ctx->buffer
    a1[1] = 0;                                 // clear ctx->size
    sub_431000(a1[4], a2);                     // free ctx->path (offset +32)
    sub_431000(a1[5], a2);                     // free ctx->member_path (offset +40)

    // Walk member_list at offset +56 (a1[7]) and invoke nullsub_4 per node
    _QWORD *v2 = (_QWORD *)a1[7];
    if ( v2 )
    {
        do {
            nullsub_4();                       // no-op destructor
            v2 = (_QWORD *)*v2;
        } while ( v2 );
        v2 = (_QWORD *)a1[7];
    }
    sub_464520(v2, a2);                        // destroy list container

    // Walk path_list at offset +64 (a1[8]) and free each stored string
    _QWORD *v3 = (_QWORD *)a1[8];
    if ( v3 )
    {
        do {
            sub_431000(v3[1], a2);             // free path string in node+8
            v3 = (_QWORD *)*v3;
        } while ( v3 );
        v3 = (_QWORD *)a1[8];
    }
    sub_464520(v3, a2);                        // destroy list container

    sub_431000((unsigned __int64)a1, a2);      // free 80-byte context itself
    return 0;
}
```

The member tracking list at offset +56 uses a no-op destructor (`nullsub_4`), meaning the member data pointers are not freed — they point into the archive buffer (for regular archives) or into arena-allocated read buffers (for thin archives) that are cleaned up when the arena is destroyed. The list exists purely for future extensibility; currently it is walked only to count nodes for `sub_464520`.

## Integration with Library Resolution

Archives reach the input loop through two paths:

1. **Direct input**: The user passes `libfoo.a` on the command line. The path enters `qword_2A5F330` directly.

2. **Library search**: The user passes `-lfoo`. Library resolution (documented in [Library Resolution](../pipeline/library-resolution.md)) transforms this to `libfoo.a`, searches `-L` paths, and appends the resolved path to `qword_2A5F330`.

In the library search path, the two-pass search strategy first checks for file existence via `stat()`, then opens the archive via `sub_42A2D0` (archive search callback) which validates that at least one member matches the target CPU architecture (checking `e_machine` against the host machine type mapping).

After an archive has been processed, its path is recorded in `qword_2A5F2F0` (the set of already-seen archives, managed via `sub_464A80` / `sub_464A90` / `sub_464AA0` / `sub_4632F0`). This prevents `-l` resolution from re-loading the same archive if the same `-l` flag appears multiple times on the command line. The check at main source line 850 (`for ( j = sub_464A80(qword_2A5F2F0); ; ...`) walks this set to skip already-processed archives.

## Function Map

| Address | Size | Identity | Description |
|---|---|---|---|
| `0x487A90` | 51 B | `is_archive` (`sub_487A90`) | Tests buffer for `"!<arch>\n"` or `"!<thin>\n"` magic |
| `0x487AD0` | 356 B | `build_member_path` (`sub_487AD0`) | Constructs `"archive:member"` composite path string, resolving long names |
| `0x487C20` | 2,549 B | `archive_open` (`sub_487C20`) | Allocates 80-byte context, sets thin flag, copies path; setjmp-protected |
| `0x487E10` | 5,592 B | `archive_next_member` (`sub_487E10`) | Parses `ar` header, resolves name, extracts/opens content; setjmp-protected |
| `0x488200` | 144 B | `archive_close` (`sub_488200`) | Destroys context, frees path strings and member lists |
| `0x488290` | 8 B | `get_member_path` (`sub_488290`) | Returns `ctx + 40` (current member path pointer) |
| `0x4BDAC0` | 48 B | `archive_open_wrapper` (`sub_4BDAC0`) | API wrapper: delegates to `sub_487C20`, translates via `dword_1D48A50` |
| `0x4BDAF0` | 48 B | `archive_next_wrapper` (`sub_4BDAF0`) | API wrapper: delegates to `sub_487E10`, translates via `dword_1D48A50` |
| `0x4BDB30` | 48 B | `archive_close_wrapper` (`sub_4BDB30`) | API wrapper: delegates to `sub_488200`, translates via `dword_1D48A50` |
| `0x4BDB60` | 8 B | `archive_path_wrapper` (`sub_4BDB60`) | Thunk: forwards unchanged to `sub_488290` |
| `0x476BF0` | 384 B | `read_file` (`sub_476BF0`) | Opens file, reads entire content into arena buffer (thin-archive member loading and main archive load) |
| `0x476E90` | 32 B | `validate_elf_magic` (`sub_476E90`) | Returns input pointer if first 4 bytes are `0x7F454C46` (ELF magic); used to confirm cubin content inside archive members |
| `0x4644C0` | 112 B | `list_prepend` (`sub_4644C0`) | Prepends a node to a singly-linked list (member/path tracking, seen-archive set) |
| `0x4307C0` | — | `arena_alloc` (`sub_4307C0`) | Arena allocator, used for context and path allocation |
| `0x431000` | — | `arena_free` (`sub_431000`) | Arena free, called by `sub_488200` for cleanup |
| `0x45CAC0` | — | `oom_handler` (`sub_45CAC0`) | OOM diagnostic; may longjmp back to the archive open/next setjmp frame |
| `0x467460` | — | `fatal_error` (`sub_467460`) | Diagnostic reporter, called on archive format errors |

## Diagnostic Strings

| String | Address | Source function | Context |
|---|---|---|---|
| `"!<arch>\n"` | `0x1D40F80` | `sub_487A90` (one xref at `0x487AA3`) | Regular archive magic comparison |
| `"!<thin>\n"` | `0x1D40F89` | `sub_487A90`, `sub_487C20`, `sub_487E10` (three xrefs) | Thin archive magic comparison (detection and classification) |
| `"longnames header not found"` | `0x1D40F92` | `sub_487AD0` (xref at `0x487C07`) | Fatal: `/offset` reference before `//` member seen |
| `"unexpected archive format"` | `0x1D40FAD` | `sub_487AD0` (xref at `0x487BD7`) | Fatal: member name has no `/` terminator |
| `"__.LIBDEP"` | `0x1D40FC7` | `sub_487E10` (two xrefs at `0x487F1D`, `0x4880AD`) | Library dependency metadata member (skipped) |

Note: the old-BSD `__.SYMDEF` symbol-table name and the GNU `/SYMDEF` variant do **not** appear anywhere in the binary's string table. nvlink only recognizes the single-character `/` form of the symbol table (and unconditionally skips it without parsing).

## Cross-References

- [File Type Detection](file-type-detection.md) — The `sub_487A90` predicate in the detection function table; the 56-byte header probe that feeds the archive dispatch
- [Input File Loop](../pipeline/input-loop.md) — The dispatch branch that enters archive iteration at `main_0x409800.c` source line 850
- [Library Resolution](../pipeline/library-resolution.md) — How `-lfoo` resolves to `libfoo.a` and the two-pass search strategy; also the `qword_2A5F2F0` seen-archive set
- [Cubin Loading](cubin-loading.md) — Processing of ELF members extracted from archives (the usual case for `libdevice.a`)
- [Fatbin Extraction](fatbin-extraction.md) — Fatbin members found inside archives; recursive extraction via `sub_42AF40`
- [NVVM IR / LTO IR Input](nvvm-ir-input.md) — IR members in archives (when `-lto` is active)
- [Symbol Resolution](../linker/symbol-resolution.md) — Where archive members' symbols are deduplicated and resolved; how the whole-archive loading interacts with multiple-definition diagnostics
- [Dead Code Elimination](../linker/dead-code-elimination.md) — How the DCE pass removes unreachable archive members after whole-archive loading; `sub_44AD40` callgraph sweep
- [Weak Symbols](../linker/weak-symbols.md) — Weak-vs-strong resolution rules applied uniformly across the archive-loaded set

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| Archive magic strings `"!<arch>\n"`, `"!<thin>\n"` at `0x1D40F80`, `0x1D40F89` | **HIGH** | Directly extracted from `nvlink_strings.json`; xrefs confirm single caller (`sub_487A90`) for regular magic |
| Archive detection function `sub_487A90` at `0x487A90`, 51 bytes | **HIGH** | Full decompiled source read; matches the described 2-magic memcmp logic verbatim |
| Archive open function `sub_487C20` at `0x487C20`, 2,549 bytes | **HIGH** | Full decompiled source read; context size 80 literal visible at `sub_4307C0(v12, 80)` |
| Member iteration function `sub_487E10` at `0x487E10`, 5,592 bytes | **HIGH** | Full decompiled source read; algorithm reconstructed from loop structure and string xrefs |
| Name resolution function `sub_487AD0` at `0x487AD0`, 356 bytes | **HIGH** | Full decompiled source read; diagnostic strings `"longnames header not found"` and `"unexpected archive format"` confirm call sites |
| 80-byte iterator context layout (offsets 0-72) | **HIGH** | Offsets derived from decompiled accesses: `v15[0]`, `v15[1]`, `ctx+16`, `ctx+24`, `ctx+32`, `ctx+40`, `ctx+48`, `a1[7]`, `a1[8]`, `ctx+72` all visible in source |
| Member header layout (name/date/uid/gid/mode/size/fmag, offsets 0/16/28/34/40/48/58) | **HIGH** | Standard GNU `ar` format; nvlink only reads offset 0 and offset 48, which is directly visible in `sub_487E10` (`v10 + 48` for ar_size) |
| Only `ar_name` and `ar_size` are read by nvlink | **HIGH** | Exhaustive review of `sub_487E10` and `sub_487AD0` — no reads from offsets 16, 28, 34, 40, or 58 |
| 2-byte alignment for odd-sized members | **HIGH** | Formula `v6 - v6 % 2 + 2` visible at source line 87 of `sub_487E10_0x487e10.c` |
| `//` long-name table detection via `v34 == 2` branch | **HIGH** | Visible in decompiled source; assignment `ctx+48 = v10` only reached when `v30+1 == 2` |
| `__.LIBDEP` 9-byte prefix skip | **HIGH** | Two xrefs to the string at `0x1D40FC7` from `sub_487E10`; the 9-byte byte-compare loop is visible in decompiled output |
| Whole-archive loading semantics (no on-demand extraction) | **HIGH** | `main_0x409800.c` lines 756-777: unconditional `while(1)` over all members with mandatory `sub_42AF40(..., 1, ...)` call per member (line 768); no symbol-table consultation |
| `dword_1D48A50` return-code translation table used by all four wrappers | **HIGH** | All four wrappers (`sub_4BDAC0`, `sub_4BDAF0`, `sub_4BDB30`) use identical `(uint)v <= 2 ? dword_1D48A50[v] : 1` pattern; `sub_4BDB60` is a pure thunk |
| `dword_1D48A50` table maps 0->0 (success), 1->1 (error), 2->2 (thin-resolve) | **MEDIUM** | Inferred from the identity `result = 1` fallback and the expectation that success maps to 0; the actual dword_1D48A50 contents not independently verified |
| `setjmp`/`longjmp` error-handling in `sub_487C20` and `sub_487E10` | **HIGH** | `_setjmp(env)` calls visible at source line 49 of `sub_487C20_0x487c20.c` and line 62 of `sub_487E10_0x487e10.c`; OOM handler `sub_45CAC0` is present and called on `sub_4307C0` NULL returns |
| `sub_488200` context destruction walking member_list and path_list | **HIGH** | Full decompiled source read (40 lines); two `do-while` loops visible over `a1[7]` and `a1[8]` |
| The `nullsub_4` no-op destructor on member_list nodes | **HIGH** | Visible at source line 18 of `sub_488200_0x488200.c`; `nullsub_*` functions are stubs inserted by the decompiler for empty function bodies |
| `libdevice.a` worked example | **MEDIUM** | The iteration trace is mechanically derived from the decompiled algorithm; the specific member names and sizes are illustrative examples, not extracted from any actual archive |
| Archive members enter `sub_42AF40` with `from_archive=1` | **HIGH** | Visible at `main_0x409800.c` line 768: `sub_42AF40(ptr[0].tv_sec, (__int64)s1, v225, v52, 1, &v271, &v261, ...)` with fifth argument literal `1` |
| Seen-archive set at `qword_2A5F2F0` | **HIGH** | Walk at `main_0x409800.c` line 746 (`for ( j = sub_464A80(qword_2A5F2F0); ...`); append at line 782 (`sub_4644C0((__int64)v68, (pthread_mutexattr_t *)&qword_2A5F2F0)`) |
| No `__.SYMDEF` or `/SYMDEF` handling | **HIGH** | Exhaustive grep over `nvlink_strings.json` for `SYMDEF` returns zero matches |
| Thin archive support via `sub_476BF0` external file read | **HIGH** | Visible at source lines 235-247 of `sub_487E10_0x487e10.c`: `strchr(v23, 58) + 1` isolates path after `:`, `sub_476BF0(v24, 0)` loads it |
