# Module ID & Registration

When CUDA programs are compiled with separate compilation (`-rdc=true`), each `.cu` translation unit is compiled independently and later linked by nvlink. The host-side registration code emitted by cudafe++ must associate its `__cudaRegisterFatBinary` call with the correct device fatbinary, and anonymous namespace device symbols must receive globally unique mangled names. The **module ID** is a string identifier computed by `make_module_id` (`sub_5AF830`, host_envir.c, ~450 lines) that provides this uniqueness. It is derived from a CRC32 hash of the compiler options and source filename, combined with the output filename and process ID. Once computed, the module ID is cached in `qword_126F0C0` and referenced throughout the backend code generator -- in `_NV_ANON_NAMESPACE` construction, `_GLOBAL__N_` mangling, `_INTERNAL` prefixing, host reference array scoped names, and the module ID file written for nvlink consumption.

## Key Facts

| Property | Value |
|---|---|
| Generator function | `sub_5AF830` (`make_module_id`, ~450 lines, host_envir.c) |
| Setter | `sub_5AF7F0` (`set_module_id`, host_envir.c, line 3387 assertion) |
| Getter | `sub_5AF820` (`get_module_id`, host_envir.c) |
| File writer | `sub_5B0180` (`write_module_id_to_file`, host_envir.c) |
| Entity-based selector | `sub_5CF030` (`use_variable_or_routine_for_module_id_if_needed`, il.c, line 31969) |
| Anon namespace constructor | `sub_6BC7E0` (nv_transforms.c, ~20 lines) |
| Cached module ID global | `qword_126F0C0` (8 bytes, initially NULL) |
| Selected entity global | `qword_126F140` (8 bytes, IL entity pointer) |
| Selected entity kind | `byte_126F138` (1 byte, 7=variable or 11=routine) |
| Module ID file path global | `qword_106BF80` (set by `--module_id_file_name`, flag 87) |
| Generate-module-ID-file flag | `--gen_module_id_file` (flag 83, no argument) |
| Module ID file path flag | `--module_id_file_name` (flag 87, has argument) |
| Options hash input global | `qword_106C038` (string, command-line options to hash) |
| Output filename global | `qword_106C040` (display filename override) |
| Emit-symbol-table flag | `dword_106BFB8` (triggers `write_module_id_to_file` in backend) |
| CRC32 polynomial | `0xEDB88320` (CRC-32/ISO-HDLC, reflected) |
| CRC32 initial value | `0xFFFFFFFF` |
| Debug trace topic | `"module_id"` (gated by `dword_126EFC8`) |
| Debug format strings | `"make_module_id: str1 = %s, str2 = %s, pid = %ld\n"` at `0xA5DA48` |
| | `"make_module_id: final string = %s\n"` at `0xA5DA80` |

## Algorithm Overview

The module ID generator has three source modes, tried in priority order. The result is always cached in `qword_126F0C0` -- the function returns immediately if the cache is populated.

### Mode 1: Module ID File

If `qword_106BF80` (set by the `--module_id_file_name` CLI flag) is non-NULL and `dword_106BFB8` is clear, the function opens the specified file, reads its entire contents into a heap-allocated buffer, null-terminates it, and uses that as the module ID verbatim. This allows build systems to inject deterministic, reproducible identifiers from external sources (e.g., a content hash of the source file computed by the build system).

```c
// sub_5AF830, mode 1: read module ID from file
if (!dword_106BFB8 && qword_106BF80) {
    FILE *f = open_file(qword_106BF80, "r");  // sub_4F4870
    if (!f) fatal("unable to open module id file for reading");

    fseek(f, 0, SEEK_END);
    size_t len = ftell(f);
    rewind(f);

    char *buf = allocate(len + 1);             // sub_6B7340
    if (fread(buf, 1, len, f) != len)
        fatal("unable to read module id from file");

    buf[len] = '\0';
    fclose(f);
    qword_126F0C0 = buf;
    return buf;
}
```

### Mode 2: Explicit Token (Caller-Provided String)

If the caller passes a non-NULL first argument (`src`), the function enters the default computation path using that string as the source filename component. When a secondary string argument (`nptr`) is provided instead (used by `use_variable_or_routine_for_module_id_if_needed`), it is first parsed with `strtoul`. If the parse succeeds (the entire string was consumed as a number), the numeric value is formatted as an 8-digit hex string. If the parse fails (the string is not purely numeric), the string is CRC32-hashed and the hash is used as the hex token. The working directory (`qword_126EEA0`) is used as an extra component, and the PID is always appended.

### Mode 3: Default Computation (stat + ctime + getpid)

When no caller-provided string is available, the function `stat()`s the output file. If the stat succeeds and the file is a regular file (`S_IFREG`), the modification time (`st_mtime`) is converted to a string via `ctime()`, and the PID is obtained via `getpid()`. If the stat fails or the result is not a regular file, only the PID is used, with the compilation timestamp string (`qword_126EB80`) as the source component.

## Complete Generation Pseudocode

```python
function make_module_id(src_arg):
    // Check cache
    if qword_126F0C0 != NULL:
        return qword_126F0C0

    // Mode 1: read from file
    if !dword_106BFB8 AND qword_106BF80 != NULL:
        return read_file_contents(qword_106BF80)

    // Determine the output filename base
    if dword_126EE48:                    // multi-TU mode
        output_name = **(qword_106BA10 + 184)   // from TU descriptor
    else:
        output_name = xmmword_126EB60[0]         // primary source file
    if qword_106C040 != NULL:
        output_name = qword_106C040              // display name override

    // Determine source string and extra string
    pid = 0
    extra = NULL

    if src_arg != NULL:
        src = src_arg
        // skip nptr processing, fall through to assembly

    else if nptr != NULL:                // caller-provided numeric token
        (value, endptr) = strtoul(nptr, 0)
        if endptr <= nptr OR *endptr != '\0':
            value = crc32(nptr)          // not a pure number, hash it
        src = sprintf("%08lx", value)
        pid = getpid()
        extra = qword_126EEA0           // working directory

    else:                                // default: stat the output file
        if stat(output_name) succeeds AND is regular file:
            mtime = stat.st_mtime
            src = ctime(mtime)
            pid = getpid()
            extra = qword_126EEA0
        else:
            pid = getpid()
            src = qword_126EB80         // compilation timestamp
            extra = qword_126EEA0

    // --- Assemble the module ID string ---

    // Step 1: CRC32 of command-line options
    if qword_106C038 != NULL:
        options_crc = crc32(qword_106C038)
        options_hex = sprintf("_%08lx", options_crc)
    else:
        options_hex = sprintf("_%08lx", 0)

    // Step 2: source name compression
    name_len = strlen(src) + (extra ? strlen(extra) + 1 : 0)
    if name_len > 8:
        // Source name too long -- replace with CRC32
        combined_crc = crc32(src)
        if extra:
            combined_crc = crc32_continue(combined_crc, extra)
        src = sprintf("%08lx", combined_crc)
        // extra is consumed into the hash, set to NULL
        extra = NULL

    // Step 3: PID suffix
    if pid != 0:
        pid_suffix = sprintf("_%ld", pid)
    else:
        pid_suffix = ""

    // Step 4: extract basename of output file
    basename = strip_directory_prefix(output_name)   // sub_5AC1F0
    basename_len = strlen(basename)

    // Step 5: concatenate all components
    result = options_hex + "_" + basename_len + "_" + basename + "_" + src
    if extra:
        result += "_" + extra
    if pid != 0 AND nptr == NULL:
        result += pid_suffix

    // Step 6: sanitize -- replace all non-alphanumeric with '_'
    for each character c in result:
        if !isalnum(c):
            c = '_'

    // Cache and return
    qword_126F0C0 = result
    return result
```

## Module ID Format

The final module ID string follows this structure:

```text
_{options_crc}_{basename_len}_{basename}_{source_or_crc}[_{extra}][_{pid}]
```

All non-alphanumeric characters are replaced with underscores after assembly. A concrete example for a file `kernel.cu` compiled with `nvcc -arch=sm_89 -rdc=true`:

```text
_a1b2c3d4_9_kernel_cu_5e6f7890_1234
  |          |  |        |         |
  |          |  |        |         +-- PID (getpid())
  |          |  |        +------------ CRC32 of source name (> 8 chars compressed)
  |          |  +--------------------- output basename ("kernel.cu", dot -> "_")
  |          +------------------------ basename length (9, "kernel.cu")
  +----------------------------------- CRC32 of options string
```

The leading underscore comes from the `options_hex` format (`"_%08lx"`). All dots, slashes, dashes, and other non-alphanumeric characters are uniformly replaced with underscores, making the result safe for use as a C identifier suffix.

## CRC32 Implementation

The function contains an inline CRC32 implementation that appears **three times** in the decompiled output -- once for the options string hash, once for the source filename hash, and once for the extra string hash. All three are byte-identical in the binary, indicating the compiler inlined a shared helper (likely a `static inline` function or macro) at each call site.

The algorithm is the standard bit-by-bit reflected CRC-32 used by ISO 3309, ITU-T V.42, Ethernet, PNG, and zlib. The polynomial `0xEDB88320` is the bit-reversed form of the generator polynomial `0x04C11DB7`.

### CRC32 Pseudocode

```python
function crc32(data: byte_string) -> uint32:
    crc = 0xFFFFFFFF                    // initialization vector

    for each byte in data:
        for bit_index in 0..7:
            // XOR the lowest bit of crc with the current data bit
            if ((crc ^ (byte >> bit_index)) & 1) != 0:
                crc = (crc >> 1) ^ 0xEDB88320
            else:
                crc = crc >> 1

    return crc ^ 0xFFFFFFFF             // final inversion
```

### CRC32 Decompiled (Single Instance)

This is one of the three identical inline copies from `sub_5AF830`, processing the options string at `qword_106C038`:

```c
// sub_5AF830, lines 121-165 (options CRC32)
uint64_t crc = 0xFFFFFFFF;
uint8_t *ptr = (uint8_t *)qword_106C038;

if (ptr) {
    while (*ptr) {
        uint8_t byte = *ptr;
        while (1) {
            ++ptr;
            // Bit 0
            uint64_t tmp = crc >> 1;
            if (((uint8_t)crc ^ byte) & 1) tmp ^= 0xEDB88320;
            // Bit 1
            uint64_t tmp2 = tmp >> 1;
            if (((uint8_t)tmp ^ (byte >> 1)) & 1) tmp2 ^= 0xEDB88320;
            // Bit 2
            uint64_t tmp3 = tmp2 >> 1;
            if (((uint8_t)tmp2 ^ (byte >> 2)) & 1) tmp3 ^= 0xEDB88320;
            // Bit 3
            uint64_t tmp4 = tmp3 >> 1;
            if (((uint8_t)tmp3 ^ (byte >> 3)) & 1) tmp4 ^= 0xEDB88320;
            // Bit 4
            uint64_t tmp5 = tmp4 >> 1;
            if (((uint8_t)tmp4 ^ (byte >> 4)) & 1) tmp5 ^= 0xEDB88320;
            // Bit 5
            uint64_t tmp6 = tmp5 >> 1;
            if (((uint8_t)tmp5 ^ (byte >> 5)) & 1) tmp6 ^= 0xEDB88320;
            // Bit 6
            uint64_t tmp7 = tmp6 >> 1;
            if (((uint8_t)tmp6 ^ (byte >> 6)) & 1) tmp7 ^= 0xEDB88320;
            // Bit 7
            crc = tmp7 >> 1;
            if (((uint8_t)tmp7 ^ (byte >> 7)) & 1) == 0)
                break;
            byte = *ptr;
            crc ^= 0xEDB88320;
            if (!*ptr) goto done;
        }
    }
done:
    sprintf(options_hex, "_%08lx", crc ^ 0xFFFFFFFF);
}
```

The unrolled 8-iteration loop processes one byte at a time without a lookup table. Each iteration shifts the CRC right by one bit and conditionally XORs the polynomial. The final XOR with `0xFFFFFFFF` is the standard CRC-32 finalization step. The compiler fully unrolled the inner 8-bit loop, turning what was originally a counted `for (int i = 0; i < 8; i++)` loop into 8 sequential if-shift-xor blocks. The three copies in the function differ only in which input string they process and which output variable receives the result.

### Why Three Inline Copies

The CRC32 code appears at three locations within `sub_5AF830`:

| Copy | Input | Output | Purpose |
|---|---|---|---|
| 1 (lines 121-164) | `qword_106C038` (options string) | `options_hex` | Hash compiler flags into the module ID prefix |
| 2 (lines 186-273) | `src` + `extra` (source + extra strings) | `src` (overwritten with hex) | Compress long source filenames (> 8 chars) into a fixed-width hash |
| 3 (lines 361-407) | `nptr` (explicit token string) | `v67` | Hash non-numeric caller-provided tokens |

Copy 2 is a two-pass CRC: it first hashes the source filename string, then continues the CRC state into the extra string (working directory), producing a single combined hash. This is why the code between copies 2a and 2b checks `if (extra_len != 0)` before starting the second pass.

The original C source almost certainly had a single `crc32_string()` helper function (or macro) that the compiler inlined at each call site during optimization. The EDG front-end codebase uses similar inline expansion patterns elsewhere (e.g., the 9 copies of UTF-8 decoding logic in the same file).

## Module ID Source Modes -- Decision Tree

```text
make_module_id(src)
    |
    +-- qword_126F0C0 set? --> return cached
    |
    +-- File mode available?
    |   (qword_106BF80 != NULL && !dword_106BFB8)
    |   YES --> read file, cache, return
    |
    +-- Caller provided src argument?
    |   YES --> use src as source component, no PID
    |
    +-- nptr set (explicit token)?
    |   YES --> strtoul(nptr)
    |           |
    |           +-- parse OK? --> use numeric value
    |           +-- parse fail? --> CRC32 hash nptr
    |           extra = working_directory
    |           pid = getpid()
    |
    +-- Default (no src, no nptr)
        stat(output_file)
        |
        +-- stat OK && regular file?
        |   src = ctime(st_mtime)
        |   pid = getpid()
        |   extra = working_directory
        |
        +-- stat fail
            src = qword_126EB80 (compilation timestamp)
            pid = getpid()
            extra = working_directory
```

## Entity-Based Module ID Selection

An alternative entry path into the module ID system is `use_variable_or_routine_for_module_id_if_needed` (`sub_5CF030`, il.c, line 31969, ~65 lines). Instead of computing a hash from file metadata, this function selects a representative entity (variable or function) from the current translation unit whose mangled name serves as a stable identifier. The mangled name is then passed to `sub_5AF830` as the `src` argument.

### Selection Criteria

The function is invoked during IL processing. It first checks `sub_5AF820` (`get_module_id`) -- if a module ID is already cached, it returns immediately. Otherwise, it evaluates the candidate entity:

```c
// sub_5CF030, simplified
char *use_variable_or_routine_for_module_id_if_needed(entity, kind) {
    if (get_module_id())
        return get_module_id();      // already computed

    if (qword_126F140) {
        // Already selected an entity, extract its name
        assert(dword_106BF10 || dword_106BEF8);  // il.c:32064
        goto extract_name;
    }

    // Validate entity kind: must be 7 (variable) or 11 (routine)
    assert(entity && ((kind - 7) & 0xFB) == 0);   // il.c:31969

    // Check if entity is unsuitable (member of TU scope, etc.)
    if (entity->scope == primary_scope
        || (entity->flags_81 & 0x04)       // unnamed namespace
        || (entity->scope && entity->scope->kind == 3))
    {
        // Skip: entity in primary scope, unnamed namespace, or class scope
        ...
        return NULL;
    }

    if (kind == 7) {   // Variable
        // Must have: no storage class, has definition, not template-related,
        // not inline, not constexpr, not thread-local
        if (entity->storage_class == 0
            && entity->has_definition          // offset +169
            && !(entity->flags_162 & 0x10)     // not explicit specialization
            && !(entity->flags_164 & 0x10)     // not partial specialization
            && entity->flags_148 >= 0          // not extern template
            && !(entity->flags_160 & 0x08)     // not inline variable
            && entity->flags_165 >= 0)         // not constexpr
        {
            qword_126F140 = entity;
            byte_126F138 = 7;
        }
    }
    else {   // Routine (kind == 11)
        // Must have: no specialization, no builtin return type,
        // no template parameters, not defaulted/deleted
        if (!entity->flags_164
            && entity->flags_176 >= 0          // not defaulted
            && !(entity->flags_179 & 0x02)     // not deleted
            && !(entity->flags_180 & 0x38)     // not template-related
            && !(entity->flags_184 & 0x20))    // not consteval
        {
            // Additional checks: return type not builtin, not coroutine
            if (!is_builtin_type(entity->return_type)
                && !is_generic_function(entity)
                && !is_concept_function(entity->return_type_entry))
            {
                qword_126F140 = entity;
                byte_126F138 = 11;
            }
        }
    }

extract_name:
    // Get the entity's mangled name
    char *name;
    if (byte_126F138 == 7) {
        // Variable: check unnamed namespace, use mangled or lowered name
        if ((entity->flags_81 & 0x04) || (entity->scope && entity->scope->kind == 3))
            name = get_lowered_name();      // sub_6A70C0
        else
            name = entity->name;            // offset +8
    } else {
        // Routine: similar checks, use name or lowered name
        assert(byte_126F138 == 11);         // il.c:32079
        if (dword_126EFB4 == 2)             // C++20 mode
            name = get_mangled_name();      // sub_6A76C0
        else
            name = entity->name;
    }

    assert(name != NULL);                   // il.c:32086
    return make_module_id(name);            // sub_5AF830(name)
}
```

The strict filtering ensures the selected entity is one whose mangled name is deterministic across compilations of the same source. Template instantiations, inline variables, and unnamed namespace entities are excluded because their names may vary or conflict.

## set_module_id and get_module_id

The module ID cache has a setter/getter pair for use by external callers that compute the ID through other means:

```c
// sub_5AF7F0 -- set_module_id (host_envir.c, line 3387)
void set_module_id(char *id) {
    assert(qword_126F0C0 == NULL);   // "set_module_id" -- must not be set already
    qword_126F0C0 = id;
}

// sub_5AF820 -- get_module_id (host_envir.c)
char *get_module_id(void) {
    return qword_126F0C0;
}
```

The setter asserts that the module ID has not been previously set. This is a safety guard: the module ID must be computed exactly once per compilation. Any attempt to set it twice indicates a logic error in the pipeline.

## write_module_id_to_file

The `write_module_id_to_file` function (`sub_5B0180`, host_envir.c, ~30 lines) is called during the backend output phase when `dword_106BFB8` (emit-symbol-table flag) is set. It generates the module ID (via `sub_5AF830(0)`) and writes the raw string to a file:

```c
// sub_5B0180 -- write_module_id_to_file
void write_module_id_to_file(void) {
    char *id = make_module_id(NULL);       // sub_5AF830(0)
    char *path = qword_106BF80;            // module ID file path

    if (!path)
        fatal("module id filename not specified");

    FILE *f = open_file_for_writing(path); // sub_4F48F0
    size_t len = strlen(id);

    if (fwrite(id, 1, len, f) != len)
        fatal("error writing module id to file");

    fclose(f);
}
```

The module ID file is a plain text file containing nothing but the module ID string (no newline, no header). This file is consumed by the fatbinary linker (`fatbinary`) and nvlink during the device linking phase.

## Downstream Consumers

The module ID is referenced in seven distinct locations across the cudafe++ binary:

### 1. Anonymous Namespace Mangling (sub_6BC7E0)

Constructs the `_GLOBAL__N_<module_id>` string used as the `_NV_ANON_NAMESPACE` macro value in the `.int.c` trailer:

```c
// sub_6BC7E0 (nv_transforms.c, ~20 lines)
if (qword_1286A00)                      // cached?
    return qword_1286A00;

char *id = make_module_id(NULL);        // sub_5AF830(0)
char *buf = allocate(strlen(id) + 12);  // "_GLOBAL__N_" = 11 chars + NUL
strcpy(buf, "_GLOBAL__N_");
strcpy(buf + 11, id);
qword_1286A00 = buf;                   // cache for reuse
return buf;
```

This string appears in the `.int.c` output as:

```c
#define _NV_ANON_NAMESPACE _GLOBAL__N_a1b2c3d4e5f67890
#ifdef _NV_ANON_NAMESPACE
#endif
#include "kernel.cu"
#undef _NV_ANON_NAMESPACE
```

### 2. Scoped Name Prefix Builder (sub_6BD2F0)

The recursive `nv_build_scoped_name_prefix` function uses the same `_GLOBAL__N_<module_id>` string when building scope-qualified names for internal-linkage device entities in host reference arrays. If the entity is in an anonymous namespace and `qword_1286A00` is not yet computed, it calls `sub_5AF830(0)` directly to generate the module ID.

### 3. Internal Linkage Prefix (sub_69DAA0)

Constructs `_INTERNAL<module_id>` for internal-linkage entities during name lowering:

```c
// sub_69DAA0 (lower_name.c context)
char *id = make_module_id(NULL);
char *buf = allocate(strlen(id) + 10);
strcpy(buf, "_INTERNAL");              // 0x414E5245544E495F in little-endian
strcpy(buf + 9, id);
```

### 4. Unnamed Namespace Naming (sub_69ED40, give_unnamed_namespace_a_name)

When the name lowering pass encounters an unnamed (anonymous) namespace entity, it calls `sub_5AF830(0)` to obtain the module ID and constructs a `_GLOBAL__N_<module_id>` name for the namespace. The function is confirmed as `give_unnamed_namespace_a_name` from assert strings at lower_name.c lines 7880 and 7889.

### 5. Frontend Wrapup (sub_588E90)

The `translation_unit_wrapup` function (`sub_588E90`, fe_wrapup.c) calls `sub_5AF830(0)` unconditionally during frontend finalization. This ensures the module ID is computed and cached before the backend code generator needs it, even if no earlier consumer triggered computation.

### 6. Entity-Based Selection (sub_5CF030)

As described above, `use_variable_or_routine_for_module_id_if_needed` selects a representative entity and passes its mangled name to `sub_5AF830`, which then uses the name as the `src` component instead of file metadata.

### 7. Module ID File Output (sub_5B0180)

Writes the raw module ID string to a file for consumption by fatbinary and nvlink.

## Integration with the Compilation Pipeline

The module ID is computed at multiple points during compilation, but only the first computation persists (all subsequent calls return the cached value):

```text
Pipeline stage                    Module ID action
--------------------------------------------------------------
CLI parsing                       Flags 83/87 set qword_106BF80
                                  Options string stored in qword_106C038
Frontend processing               sub_5CF030 may select entity-based ID
Frontend wrapup (sub_588E90)      sub_5AF830(0) ensures ID is computed
Backend output (sub_489000)       sub_6BC7E0 uses ID for _NV_ANON_NAMESPACE
                                  sub_6BCF80 uses ID in host reference arrays
                                  sub_5B0180 writes ID to file (if dword_106BFB8)
```

The `--gen_module_id_file` flag (83) controls whether a module ID file is generated at all. The `--module_id_file_name` flag (87) specifies its path. Both are set by nvcc when invoking cudafe++ with `-rdc=true`.

## PID Incorporation

The `getpid()` call ensures that concurrent compilations of the same source file produce different module IDs. Without the PID, two parallel `nvcc` invocations compiling the same `.cu` file with the same flags would generate identical module IDs, causing runtime registration collisions when the resulting objects are linked together. The PID is appended as the final underscore-separated component and is only included in modes 2 and 3 (not when the caller provides a `src` argument directly, and not when the module ID is read from a file). This means reproducible builds require mode 1 (file-based) or entity-based selection.

## Global Variables

| Address | Size | Name | Description |
|---|---|---|---|
| `qword_126F0C0` | 8 | `cached_module_id` | Cached module ID string (computed once, never freed) |
| `qword_106BF80` | 8 | `module_id_file_path` | Path from `--module_id_file_name` (flag 87) |
| `qword_106C038` | 8 | `options_hash_input` | Command-line options string for CRC32 hashing |
| `qword_106C040` | 8 | `display_filename` | Output filename override (used as basename source) |
| `qword_126F140` | 8 | `selected_entity` | Entity chosen by `use_variable_or_routine_for_module_id_if_needed` |
| `byte_126F138` | 1 | `selected_entity_kind` | Kind of selected entity (7=variable, 11=routine) |
| `dword_106BFB8` | 4 | `emit_symbol_table` | Flag: write module ID file + symbol table in backend |
| `qword_1286A00` | 8 | `cached_anon_namespace_hash` | Cached `_GLOBAL__N_<module_id>` string |
| `qword_126EEA0` | 8 | `working_directory` | Current working directory (set during `host_envir_early_init`) |
| `qword_126EB80` | 8 | `compilation_timestamp` | `ctime()` of compilation start (IL header) |
| `dword_126EFC8` | 4 | `debug_trace_flag` | Enables debug trace output to FILE `s` |

## Function Map

| Address | Name | Source File | Lines | Role |
|---|---|---|---|---|
| `sub_5AF830` | `make_module_id` | host_envir.c | ~450 | CRC32-based unique TU identifier generator |
| `sub_5AF7F0` | `set_module_id` | host_envir.c | ~10 | Setter with assert guard (must be called once) |
| `sub_5AF820` | `get_module_id` | host_envir.c | ~3 | Returns `qword_126F0C0` |
| `sub_5B0180` | `write_module_id_to_file` | host_envir.c | ~30 | Writes module ID to file for nvlink |
| `sub_5CF030` | `use_variable_or_routine_for_module_id_if_needed` | il.c:31969 | ~65 | Selects representative entity for stable ID |
| `sub_6BC7E0` | (anon namespace hash) | nv_transforms.c | ~20 | Constructs `_GLOBAL__N_<module_id>` |
| `sub_6BD2F0` | `nv_build_scoped_name_prefix` | nv_transforms.c | ~95 | Recursive scope-qualified name builder |
| `sub_69DAA0` | (internal linkage prefix) | lower_name.c | ~60 | Constructs `_INTERNAL<module_id>` prefix |
| `sub_69ED40` | `give_unnamed_namespace_a_name` | lower_name.c:7880 | ~80 | Names anonymous namespaces with module ID |
| `sub_588E90` | `translation_unit_wrapup` | fe_wrapup.c | ~30 | Ensures module ID is computed during wrapup |

## Cross-References

- [.int.c File Format](./int-c-format.md) -- `_NV_ANON_NAMESPACE` trailer section that consumes the module ID
- [CUDA Runtime Boilerplate](./cuda-runtime.md) -- managed memory registration that uses the fatbinary handle
- [Host Reference Arrays](./host-reference-arrays.md) -- `.nvHR*` sections where scoped names include the module ID
- [RDC Mode](../cuda/rdc-mode.md) -- separate compilation mode that requires module IDs for cross-TU linking
- [CLI Flag Inventory](../config/cli-flags.md) -- flags 83 (`gen_module_id_file`) and 87 (`module_id_file_name`)
- [Backend Code Generation](../pipeline/backend.md) -- output phase where `write_module_id_to_file` is called
- [Frontend Wrapup](../pipeline/fe-wrapup.md) -- `translation_unit_wrapup` triggers early module ID computation
