# File Type Detection

nvlink classifies every file listed on its command line by a two-stage
pipeline: the input loop splits the path into `(dir, base, ext)` with
`sub_462620`, then uses the **extension** as the primary dispatch key.
For every extension-matched branch, a corresponding **magic-number**
check validates that the file content actually matches the claimed
format; a mismatch is a hard error. When no extension matches, the
loop falls through to two content-driven fallbacks (`sub_487A90` for
archive magic, `sub_43D9B0` for a relocatable ELF) before giving up
and printing `ignore input %s`.

A separate, fully content-driven classifier lives at `sub_4CE070` --
it is used exclusively to classify members *inside* a fatbin
container, where file extensions do not exist.

> Confidence: High. The dispatch tree, magic constants, and error
> strings were read directly from `decompiled/main_0x409800.c`
> (lines ~600-900) and cross-checked against `nvlink_strings.json`
> xrefs for every literal string.

| | |
|---|---|
| **Path parser** | `sub_462620` at `0x462620` -- `path_split(path, dir_out, base_out, ext_out)` |
| **Input loop caller** | `main()` at `0x409800` -- extension test chain starts at `0x40A59E` |
| **Header probe size** | 56 bytes (`fread(header, 1, 56, fp)`) |
| **Extension-tested format count** | 7 (`cubin`, `ptx`, `fatbin`, `nvvm`, `ltoir`, `bc`, `o`) |
| **Content-tested fallbacks** | archive (`sub_487A90`), relocatable ELF (`sub_43D9B0`) |
| **Fatbin-member classifier** | `sub_4CE070` at `0x4CE070` -- classifies NVVM/nested-fatbin/cubin/PTX |

## 1. Header Probe

Before any dispatch decision, the input loop opens the file and reads
exactly 56 bytes:

```c
// main, around 0x40A540
v79 = fopen(v74, "rb");
if (!v79) error(...);
v80 = fread(ptr, 1, 0x38, v79);                     // 56-byte probe
if (v80 == 56)              goto dispatch;          // full probe
else if (v80 > 0)           /* short file */
  if (!is_archive(ptr, 56)) error(...);             // short-file archive sanity
  goto dispatch;
else if (ferror(v79))       /* abort */
```

56 bytes is exactly enough to cover:

- ELF32 header (52 bytes) -- `e_ident..e_shstrndx`
- ELF64 header (64 bytes) through `e_flags` at offset 48 (required by `sub_43DA40`)
- The 8-byte `!<arch>\n` / `!<thin>\n` archive magic
- The 8-byte `.version` PTX magic
- The fatbin magic (4 bytes) plus its optional version byte
- The NVVM IR magic (4 bytes) plus 4-byte padding variant

The header bytes live in the local buffer `ptr` on `main`'s stack and
are reused as a sliding scratch area throughout the dispatch chain.
After classification, the full file is read a second time by the
format-specific loader (`sub_476BF0`, `sub_476E80`, `sub_43E100`).

## 2. Path Split: `sub_462620`

| | |
|---|---|
| **Address** | `0x462620` |
| **Size** | 3,579 bytes |
| **Signature** | `int sub_462620(const char *path, char **dir_out, char **base_out, char **ext_out)` |
| **Returns** | Via `sub_431000(working_copy)` (arena free) |

The function copies the input path into an arena buffer, then uses
two `strrchr` calls to locate the last `/` (0x2F) and the last `.`
(0x2E). If the last dot appears before the last slash, it is treated
as part of a directory name and the extension output becomes NULL --
this correctly handles paths like `/home/user/my.dir/binary` (no
extension) vs. `/home/user/mydir/binary.cubin` (extension `cubin`).

```c
// Reconstructed from sub_462620 (decompiled/sub_462620_0x462620.c)
int path_split(const char *path,
               char **dir_out, char **base_out, char **ext_out) {
    char *copy  = arena_strdup(path);              // sub_4307C0
    char *dot   = strrchr(copy, '.');              // last '.'
    char *slash = strrchr(copy, '/');              // last '/'

    if (dot < slash) dot = NULL;                   // dot belongs to dir

    if (dir_out) {
        if (!slash) { *dir_out = NULL; goto base; }
        *slash = 0;
        *dir_out = arena_strdup(copy);
        *slash = '/';
    }
base:
    if (base_out) {
        if (dot) *dot = 0;
        const char *start = slash ? slash + 1 : copy;
        *base_out = arena_strdup(start);
        if (dot) *dot = '.';
    }
    if (ext_out)
        *ext_out = dot ? arena_strdup(dot + 1) : NULL;

    arena_free(copy);                              // sub_431000
    return 0;
}
```

Three properties of the returned extension matter for the dispatcher:

- **No leading dot.** For `foo.cubin`, the extension is `"cubin"`, not `".cubin"`.
- **Case-sensitive.** The dispatcher uses byte-for-byte comparisons, so `.CUBIN` will not match.
- **May be NULL.** Files with no extension (e.g. a tarball extract named `libm-release`) go straight to the content-driven fallbacks.

In `main`, Hex-Rays decompiles the call as `sub_462620(v74)` with only
one argument because the other three are passed in registers that
Hex-Rays lost track of. The effective call is
`sub_462620(path, &dir, &base, &ext)`, and the extension pointer lands
in the local variable `s1`.

## 3. Dispatch Tree (actual order from `main`)

The dispatch sequence in `main` is **extension-first, magic-validating**.
After `path_split` populates `s1` with the extension, the loop performs
an ordered chain of extension comparisons. The first matching branch
is taken; a failed magic validation within a branch is a hard error,
not a fallthrough.

```text
main() input loop, per-file dispatch (main_0x409800.c line ~612-847)
|
|-- fread(header, 1, 56, fp)                       // 56-byte probe
|
|-- if short read AND !is_archive(header,56):      // sub_487A90
|        error "link input %s"
|
|-- path_split(path, &dir, &base, &ext)             // sub_462620 (s1 = ext)
|
|-- if ext == NULL: goto LABEL_131 "ignore input"
|
|-- if memcmp(ext, "cubin\0", 6) == 0:              // inline byte loop
|      if !is_elf(header):       error "cubin not an elf?"
|      if e_machine != 190:      error "cubin not a device elf?"
|      --> has_sass path (sub_43DA40)  --> LABEL_178: sub_43E100 + sub_426570
|      --> !has_sass path               --> LABEL_184: sub_43E100 + sub_426570
|
|-- else if memcmp(ext, "ptx\0", 4) == 0:           // inline byte loop
|      --> sub_476BF0(path, 1)         // load file body
|      --> sub_4BD760(...)             // embedded ptxas JIT
|      --> sub_4297B0(status)
|      --> sub_426570 register cubin
|
|-- else if memcmp(ext, "fatbin\0", 7) == 0:        // inline byte loop
|      if *(u32*)header != 0xBA55ED50: error "fatbin wrong format?"
|      --> sub_476BF0(path, 0)
|      --> sub_42AF40(...)             // fatbin walker
|
|-- else if strcmp(ext, "nvvm") == 0 || strcmp(base, "ltoir") == 0:
|      if !byte_2A5F288 (-lto not set): error "should only see nvvm files when -lto"
|      --> sub_476BF0(path, 0)
|      --> sub_427A10(...)             // register LTO IR module
|
|-- else if ext[0]=='b' && ext[1]=='c' && ext[2]==0:
|      error "should never see bc files"             // unreachable in prod
|
|-- else if is_archive(header, 56):                  // sub_487A90
|      if path contains "cudadevrt" and !v353: skip
|      --> sub_476BF0(path, 0)
|      --> sub_4BDAC0 archive walker loop:
|            while sub_4BDAF0(&member) {
|              sub_4BDB70(&content, member, memname)
|              if content: sub_42AF40(...)           // recursive fatbin walker
|              else if qword_2A5F2E0: sub_4298C0(member, name, ...)
|            }
|
|-- else if memcmp(ext, "so", 2) != 0:                // not a .so
|      if is_relocatable(header):                     // sub_43D9B0 (ET_REL)
|         if ext == "o" && is_elf && e_machine==190:  // plain .o that IS a cubin
|            --> same cubin path (LABEL_178/184)
|         else:                                       // host ELF: dispatch via search
|            --> sub_476E80(path)                     // extract embedded fatbin
|            --> sub_4BDB70, sub_42AF40               // walk fatbin sections
|
|-- else (ext=="so" or ET_REL check failed):
|      goto LABEL_131: fprintf(stderr, "ignore input %s\n")
```

### Dispatch order observations

1. **Extension wins.** A file named `kernel.cubin` that starts with
   archive magic `!<arch>\n` is treated as a malformed cubin (it
   fails `is_elf` and errors with `cubin not an elf?`). It is **not**
   silently reclassified as an archive.
2. **Magic validates but does not reclassify.** Each extension branch
   performs exactly the magic check appropriate for its claimed
   format and errors out if the check fails. There is no
   "try-next-format" logic.
3. **Archive fallback is content-driven.** Archives can be detected
   without an extension: the chain falls through to `sub_487A90`
   regardless of whether `ext` is `a`, `lib`, or missing entirely.
   This is how `cudadevrt.a` and `libcudadevrt.a` are both picked up.
4. **`.o` is ambiguous and specially handled.** A `.o` file is
   allowed to be either a host ELF (with an embedded `.nv_fatbin`
   section walked by `sub_476E80`) or a plain cubin (if `e_machine`
   is 190). This is checked inline at the ET_REL branch.
5. **PTX has no extension-independent fallback.** PTX is detected
   only when the extension is exactly `ptx`. The content-driven
   `is_ptx` predicate (`sub_4CDF80`) is used only by the fatbin
   member classifier `sub_4CE070`; `main` never calls it.
6. **`.ltoir` uses the basename, not the extension.** Line 761 of
   `main` does `!strcmp(v84, "ltoir")` where `v84` is the *basename*
   (result of `a3` out-parameter), not the extension. This handles
   files where the producer wrote `kernel.ltoir.some.ext` or passed
   the literal basename. In practice the combined test
   `!strcmp(ext,"nvvm") || !strcmp(base,"ltoir")` catches both
   conventions.
7. **`bc` is actively rejected.** The binary contains the error
   string "should never see bc files". nvlink refuses to take LLVM
   bitcode directly; LTO IR must be the NVVM-wrapped form produced
   by libnvvm.
8. **`so` is silently skipped.** A file with extension `so` is
   treated as "ignore input" unconditionally -- shared objects never
   contribute device code.

## 4. Magic Bytes Reference

The following table is the authoritative set of all magic constants
nvlink recognises. Hex values in the first column are the exact
constants that appear in decompiled code (little-endian x86-64), and
the decoding in column three is the resulting byte sequence at offset
0 of the file.

| Constant (LE u32/u64) | ASCII / bytes | Size | Format | Check site |
|---|---|---|---|---|
| `0x464C457F` | `7F 45 4C 46` (`\x7fELF`) | 4 B | ELF (any) | `sub_43D970` (`is_elf`) |
| `0xBA55ED50` | `50 ED 55 BA` (`P.U.`) | 4 B | Fatbin archive (top-level `.fatbin` file) | `main` inline `LODWORD(ptr[0]) != -1168773808` |
| `0x1BA55ED50` | `50 ED 55 BA 01` | 5 B | Nested fatbin inside a fatbin container (magic + version 1) | `sub_4CE070` packed-u64 compare against lower 48 bits |
| `0x464243BC` | `BC 43 42 46` | 4 B | Fatbin container header (`__nv_relfatbin` section, JSON-style wrapper) | `sub_4CE070` (as part of 8-byte check) |
| `0x1464243BC` | `BC 43 42 46 01` | 5 B | Fatbin container + version byte `0x01` | `sub_4CE070` `*(u64*)a1 != 0x1464243BCLL` |
| `0x1EE55A01` | `01 5A E5 1E` | 4 B | NVVM IR wrapper (no padding) | `sub_4CE070` inline (`518347265`) |
| `0x1EE55A01` @ offset 4 | `00 00 00 00 01 5A E5 1E` | 8 B | NVVM IR wrapper (4-byte zero padding) | `sub_4CE070` second branch |
| `"!<arch>\n"` | `21 3C 61 72 63 68 3E 0A` | 8 B | Standard ar archive | `sub_487A90` |
| `"!<thin>\n"` | `21 3C 74 68 69 6E 3E 0A` | 8 B | Thin ar archive (members by path) | `sub_487A90` |
| `".version"` | `2E 76 65 72 73 69 6F 6E` | 8 B | PTX assembly source (first non-whitespace non-comment token) | `sub_4CDF80` |

Notes on the fatbin pair:

- **`0xBA55ED50`** is the **outer** fatbin archive magic used when a
  file is a standalone `.fatbin`, an embedded `.nv_fatbin` ELF
  section, or the payload of a `__nv_relfatbin` container. It is a
  reversible pun for "BASSED 50".
- **`0x464243BC`** is the **container** magic that wraps one or more
  such fatbin archives together with metadata (text JSON-like
  descriptor, plus per-entry headers). It sits in
  `__nv_relfatbin` ELF sections produced by host compilation.
  `sub_4CE070` tests a packed 8-byte compare that simultaneously
  verifies the 4-byte magic and the version byte `0x01` at offset 4,
  yielding the 64-bit constant `0x1464243BC`.
- The `0x1BA55ED50` packed constant in `sub_4CE070`'s nested-fatbin
  check has the same structure: 4-byte magic plus version byte `0x01`
  at offset 4, masked to the lower 48 bits so higher header bytes
  are ignored.

Notes on NVVM IR:

- The constant `518347265` that appears in `sub_4CE070` is decimal
  for `0x1EE55A01`. Decoded little-endian, the first four bytes are
  `01 5A E5 1E`, a reversible pun for "LEESA" (the LLVM-bitcode-like
  NVVM IR wrapper).
- The second check (`!LODWORD(v5->__jmpbuf[0]) && HIDWORD(...) == 518347265`)
  handles NVVM IR payloads that have been aligned to an 8-byte
  boundary by a 4-byte zero prefix. This layout is generated when
  nvvm bitcode is embedded inside a fatbin container alongside other
  payload types that require 8-byte alignment.

## 5. Detection Predicates

### 5.1 `sub_43D970` - is_elf (ELF magic)

| | |
|---|---|
| **Address** | `0x43D970` |
| **Size** | 17 bytes |
| **Signature** | `bool is_elf(const void *buf)` |

```c
// decompiled/sub_43D970_0x43d970.c
bool is_elf(uint32_t *buf) {
    if (!buf) return false;
    return *buf == 0x464C457F;                     // 1179403647
}
```

Plain 4-byte compare. Does not validate any further fields; the
caller is responsible for running `is_elf64` and the `e_machine`
check if it wants a CUDA ELF specifically.

### 5.2 `sub_43D9A0` - is_elf64 (ELFCLASS check)

| | |
|---|---|
| **Address** | `0x43D9A0` |
| **Size** | 17 bytes |
| **Signature** | `bool is_elf64(const void *buf)` |

```c
// decompiled/sub_43D9A0_0x43d9a0.c
bool is_elf64(const uint8_t *buf) {
    if (!buf) return false;
    return buf[4] == 2;                            // EI_CLASS == ELFCLASS64
}
```

nvlink requires 64-bit ELFs for all modern SM architectures. A
legacy 32-bit cubin passes `is_elf` but fails this predicate.

### 5.3 `sub_43D9B0` - is_relocatable (ET_REL check)

| | |
|---|---|
| **Address** | `0x43D9B0` |
| **Size** | 55 bytes |
| **Signature** | `bool is_relocatable(const void *buf)` |

```c
// decompiled/sub_43D9B0_0x43d9b0.c
bool is_relocatable(const uint8_t *buf) {
    if (!buf) return false;
    Elf_Ehdr *ehdr = (buf[4] == 2)
        ? (Elf_Ehdr *)sub_448360(buf)              // ELF64 accessor
        : (Elf_Ehdr *)sub_46B590(buf);             // ELF32 accessor
    return ehdr->e_type == ET_REL;                 // 1
}
```

`ET_REL` is the expected value for cubin inputs to a linker. Shared
objects (`ET_DYN = 3`) and executables (`ET_EXEC = 2`) are rejected
by the input loop even if their `e_machine` were `190`.

### 5.4 `sub_43DA40` - has_sass (SASS-present flag)

| | |
|---|---|
| **Address** | `0x43DA40` |
| **Size** | 48 bytes |
| **Signature** | `bool has_sass(const void *buf)` |

```c
// decompiled/sub_43DA40_0x43da40.c
bool has_sass(const uint8_t *buf) {
    if (!buf || buf[4] != 2) return false;         // require ELF64
    Elf64_Ehdr *e = (Elf64_Ehdr *)sub_448360(buf);
    uint32_t mask = (e->e_ident[EI_OSABI] == 0x41) ? 0x0002 : 0x4000;
    return (e->e_flags & mask) != 0;
}
```

`e_flags` lives at offset 48 in the ELF64 header. The mask depends on
the OSABI byte:

| `EI_OSABI` | Mask | Meaning |
|---|---|---|
| `0x41` (`'A'`, legacy) | `0x0002` | SASS-present flag at bit 1 |
| otherwise (current) | `0x4000` | SASS-present flag at bit 14 |

When `has_sass` returns true, the cubin has already been assembled
and can be linked without re-running ptxas. When it returns false,
the loop dispatches to a ptxas rerun path through `sub_4275C0`.

### 5.5 `sub_487A90` - is_archive (ar magic)

| | |
|---|---|
| **Address** | `0x487A90` |
| **Size** | 51 bytes |
| **Signature** | `bool is_archive(const void *buf, size_t size)` |

```c
// decompiled/sub_487A90_0x487a90.c
bool is_archive(const void *buf, size_t size) {
    if (size <= 7) return false;
    if (memcmp(buf, "!<arch>\n", 8) == 0) return true;
    return memcmp(buf, "!<thin>\n", 8) == 0;
}
```

Called twice in `main`: once as a short-read sanity check
(line 629), once as the extension-independent fallback (line 789).
The same predicate is used by the archive iterator `sub_487E10` to
validate the header of each embedded archive.

### 5.6 `sub_4CDF80` - is_ptx (`.version` detector)

| | |
|---|---|
| **Address** | `0x4CDF80` |
| **Size** | 121 bytes |
| **Signature** | `bool is_ptx(const char *buf)` |

```c
// decompiled/sub_4CDF80_0x4cdf80.c
bool is_ptx(const char *buf) {
    if (!buf) return false;
    const unsigned short **ct = __ctype_b_loc();
    const char *p = buf;
    int c = *p;
    while (c) {
        while (c && ((*ct)[c] & 0x2000) != 0) {    // _ISspace in glibc ctype
            p++;
            c = *p;
        }
        if (memcmp(p, "//", 2) == 0 || memcmp(p, "/*", 2) == 0) {
            sub_45CB90(&p);                        // skip_comment
            c = *p;
            continue;
        }
        break;
    }
    return memcmp(p, ".version", 8) == 0;
}
```

The `0x2000` bitmask in the ctype table is glibc's internal
`_ISspace` flag, so the whitespace skip is locale-aware only in the
pedantic sense -- in practice it matches space, tab, CR, LF, VT, FF.

Comments are skipped via `sub_45CB90` (at `0x45CB90`), which handles
both line comments (`//...\n`) and block comments (`/*...*/`). This
lets a PTX file begin with a copyright banner before its
`.version` directive.

Despite this predicate's sophistication, **`main` never calls it**.
Its sole caller is the fatbin-member classifier `sub_4CE070`.

## 6. Fatbin Member Classifier: `sub_4CE070`

| | |
|---|---|
| **Address** | `0x4CE070` |
| **Size** | 461 bytes |
| **Signature** | `int classify_fatbin_member(fatbin_entry *entry, void *content)` |

This function is the *content-driven* classifier used for fatbin
container members. Fatbin containers (`__nv_relfatbin` / `.nv_fatbin`
sections) store a list of payloads with no file names and no
extensions, so content sniffing is the only option.

### 6.1 Preconditions

```c
if (!entry)  return 1;                             // null entry
if (*(uint64_t*)entry != 0x1464243BCLL) return 2;  // bad container magic + version
```

The 64-bit compare simultaneously verifies:

- Bytes `[0..3]`: `0x464243BC` (fatbin container magic, "BCBF" pun backwards)
- Byte `[4]`: `0x01` (version)
- Bytes `[5..7]`: must be zero (natural consequence of the 64-bit compare)

### 6.2 Classification sequence

```c
// Reconstructed from decompiled/sub_4CE070_0x4ce070.c
int classify_fatbin_member(fatbin_entry *entry, void *content) {
    if (!entry)  return 1;
    if (*(uint64_t*)entry != 0x1464243BCLL) return 2;

    entry->content_ptr = content;                  // offset +72

    if (!content) return 1;

    // 1. Nested fatbin: 0xBA55ED50 + version byte 0x01 (mask lower 48 bits)
    if ((*(uint64_t*)content & 0xFFFFFFFFFFFFLL) == 0x1BA55ED50LL) {
        entry->type = 2;                           // offset +80
        return 0;
    }

    // 2. Cubin: ELF magic + e_machine == EM_CUDA (190)
    if (is_elf(content)) {
        Elf64_Ehdr *e = sub_448360(content);
        if (e->e_machine == 190) {
            entry->type = 3;
            return 0;
        }
    }

    // 3. NVVM IR wrapper: magic at offset 0 OR at offset 4 (after padding)
    uint32_t first  = *(uint32_t*)content;
    uint32_t second = *((uint32_t*)content + 1);
    if (first == 0x1EE55A01 || (first == 0 && second == 0x1EE55A01)) {
        entry->type = 1;
        return 0;
    }

    // 4. PTX: leading whitespace/comments then .version
    if (is_ptx((char*)content)) {
        entry->type = 4;
        return 0;
    }

    // 5. Fallthrough: emit diagnostic, return 2
    sub_467460(dword_2A5BFA0, 0, 30675157);
    return 2;
}
```

### 6.3 Type codes (stored at offset 80 in the entry struct)

| Code | Format | Detection |
|---|---|---|
| 1 | NVVM IR wrapper | `0x1EE55A01` at offset 0 or 4 |
| 2 | Nested fatbin | `0xBA55ED50` + `0x01` at offset 4 (packed check) |
| 3 | CUDA device ELF | ELF magic + `e_machine == 190` |
| 4 | PTX assembly | `.version` after whitespace/comments |

### 6.4 Return codes

| Code | Meaning |
|---|---|
| 0 | Content successfully classified |
| 1 | NULL entry pointer or NULL content pointer |
| 2 | Bad container magic or unrecognised content format |
| 5 | Error recovery via `_setjmp`/`longjmp` (access fault during classification) |

The function installs a `_setjmp` guard around the actual content
access so that a malformed fatbin member cannot SIGSEGV the
linker -- instead the function returns 5 and the caller emits a
diagnostic. This is one of the few places in nvlink that uses
`_setjmp`/`longjmp` for structured error recovery.

### 6.5 Priority order in `sub_4CE070`

The classifier tries formats in this **fixed** order:

```
nested fatbin --> cubin --> NVVM IR (with padding variant) --> PTX --> error
```

This order matters for ambiguous payloads. For example, an NVVM IR
payload that happens to begin with bytes matching a cubin's
`e_machine` field would never actually match the cubin branch
because `is_elf` requires the exact ELF magic first. In practice the
magics are disjoint, so every valid member matches exactly one
branch.

## 7. ELF Sub-Classification Constants

Once the ELF magic is detected, these constants drive deeper
classification:

| Constant | Value | Field | Offset (ELF64) | Meaning |
|---|---|---|---|---|
| `ELFCLASS64` | 2 | `e_ident[EI_CLASS]` | 4 | 64-bit ELF (required for modern CUDA) |
| `EI_OSABI` legacy | `0x41` (`'A'`) | `e_ident[EI_OSABI]` | 7 | Legacy NVIDIA OSABI (affects SASS flag mask) |
| `ET_REL` | 1 | `e_type` | 16 | Relocatable object (expected for cubin input) |
| `EM_CUDA` | 190 (`0xBE`) | `e_machine` | 18 | NVIDIA CUDA device ELF |
| SASS flag (legacy) | `0x00000002` | `e_flags` | 48 | SASS present, when `EI_OSABI == 0x41` |
| SASS flag (current) | `0x00004000` | `e_flags` | 48 | SASS present, when `EI_OSABI != 0x41` |

`EM_CUDA = 190` is the definitive test that separates a cubin from a
host object file. For reference, common host values are
`EM_X86_64 = 62`, `EM_AARCH64 = 183`, `EM_RISCV = 243`. Seeing
anything other than 190 in a file the user named `.cubin` triggers
the `"cubin not a device elf?"` diagnostic.

## 8. Error Handling

Detection functions are uniformly defensive about NULL pointers --
every predicate (`sub_43D970`, `sub_43D9A0`, `sub_43D9B0`,
`sub_43DA40`, `sub_487A90`, `sub_4CDF80`) returns false on NULL
input. `sub_487A90` additionally checks `size > 7` before the first
`memcmp` to avoid reading past a short buffer.

Hard errors raised by the input loop (all via `sub_467460`, the
diagnostic emitter):

| Error string | Condition | Decompiled site |
|---|---|---|
| `link input %s` | `fread` returned zero AND file is not an archive | `main` line 629, xref `0x1d3427b` target |
| `cubin not an elf?` | Extension `cubin` but `is_elf` false | `main` line 652 |
| `cubin not a device elf?` | Extension `cubin`, ELF magic OK, but `e_machine != 190` | `main` line 654 |
| `fatbin wrong format?` | Extension `fatbin` but first u32 != `0xBA55ED50` | `main` line 750 |
| `should only see nvvm files when -lto` | Extension `nvvm`/`ltoir` without `-lto` | `main` line 767 |
| `should never see bc files` | Extension `bc` | `main` line 785 |
| `ignore input %s` | All dispatch branches failed; warning when verbose | `main` line 845 |

The `ignore input` path is the only soft failure: nvlink prints it
to stderr only when the verbose bit `(v55[64] & 1)` is set and
continues to the next file. All other errors abort the link.

Inside `sub_4CE070`, error recovery uses `_setjmp`/`longjmp` to
trap any access faults during content inspection:

```c
if (_setjmp(env)) {
    // Access faulted inside is_ptx or is_elf on a truncated/malicious member
    restore_handlers();
    return 5;                                      // classification aborted
}
```

The `env` jump buffer is installed into the per-thread error handler
chain (`sub_44F410` returns the TLS error slot), which is why the
classifier returns `5` rather than `2` for faulting members --
return-code `5` flags the caller to emit a different diagnostic and
skip the member instead of rejecting the entire container.

## 9. Function Map

| Address | Size | Identity | Description |
|---|---|---|---|
| `0x409800` | ~84 KB | `main` | Entry point containing the input-file dispatch chain |
| `0x462620` | 3,579 B | `path_split` | Splits path into `(dir, base, ext)` |
| `0x43D970` | 17 B | `is_elf` | 4-byte ELF magic `0x464C457F` |
| `0x43D9A0` | 17 B | `is_elf64` | `e_ident[EI_CLASS] == ELFCLASS64 (2)` |
| `0x43D9B0` | 55 B | `is_relocatable` | `e_type == ET_REL (1)` |
| `0x43DA40` | 48 B | `has_sass` | SASS-present flag in `e_flags` |
| `0x487A90` | 51 B | `is_archive` | `!<arch>\n` or `!<thin>\n` |
| `0x4CDF80` | 121 B | `is_ptx` | Skip whitespace/comments then `.version` |
| `0x45CB90` | ~100 B | `skip_comment` | `//...` and `/*...*/` skip used by `is_ptx` |
| `0x4CE070` | 461 B | `classify_fatbin_member` | Content-only fatbin payload classifier |
| `0x448360` | -- | `elf64_header_accessor` | Returns `Elf64_Ehdr *` from probe buffer |
| `0x46B590` | -- | `elf32_header_accessor` | Returns `Elf32_Ehdr *` from probe buffer |
| `0x467460` | -- | `emit_diagnostic` | Formats and prints a diagnostic from a descriptor |
| `0x476BF0` | -- | `load_file_body` | Second-pass full-file read used after classification |
| `0x476E80` | -- | `extract_host_fatbin` | Walks a host ELF `.nv_fatbin` / `__nv_relfatbin` section |

## 10. Cross-References

- [Input File Loop](../pipeline/input-loop.md) -- the `main` dispatch chain that calls every predicate on this page
- [Entry Point & `main`](../pipeline/entry.md) -- containing function and phase-0 initialisation
- [Cubin Loading](cubin-loading.md) -- `sub_426570` path taken after `is_elf` + `e_machine == 190` passes
- [ELF Parsing](elf-parsing.md) -- the ELF header accessors (`sub_448360`, `sub_46B590`) and deeper field parsing
- [Fatbin Extraction](fatbin-extraction.md) -- `sub_42AF40` fatbin walker that owns fatbin container iteration and calls `classify_fatbin_member`
- [Host ELF Handling](host-elf.md) -- the `sub_476E80` path for `.o`/`.so` host object files with embedded `.nv_fatbin`
- [Archive Processing](archives.md) -- regular and thin archive iteration via `sub_4BDAC0`/`sub_4BDAF0` after `is_archive` passes
- [PTX Input & JIT](ptx-input.md) -- `sub_4BD760` ptxas JIT path taken after the `ptx` extension branch
- [NVVM IR / LTO IR Input](nvvm-ir-input.md) -- `sub_427A10` IR registration path taken after the `nvvm`/`ltoir` branch (requires `-lto`)
- [Error Reporting](../infra/error-reporting.md) -- `sub_467460` diagnostic descriptor format and the error strings listed above
