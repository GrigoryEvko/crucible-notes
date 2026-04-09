# Architecture Profiles

> **Note:** This page defers to [structs/arch-profile.md](../structs/arch-profile.md) for the authoritative `ArchProfile` struct byte-level layout. The layout shown here is a condensed summary -- if the two pages ever disagree, `structs/arch-profile.md` is canonical (it was verified field-by-field against the decompiled constructor `sub_484DB0` at `0x484DB0`).

nvlink maintains a compile-time database of every GPU architecture it can target. The database is a lazily-initialized singleton hash map (`qword_2A5F8D8`) populated by `sub_484F50` (53,974 bytes, 1,330 decompiled lines). Each entry is a 136-byte **architecture profile** struct created by `sub_484DB0`. For every physical architecture (e.g. `sm_100`) the database stores three profile variants -- real (`sm_`), virtual (`compute_`), and LTO (`lto_`) -- interlinked by pointer chains. Companion functions parse architecture name strings into numeric IDs, detect suffix modifiers, and handle legacy/deprecated architectures.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_484F50` | `ArchProfileDB::init` | 53,974 B | Lazy singleton; populates the profile hash map with all 22 architectures |
| `sub_484DB0` | `ArchProfile::create` | 400 B | Allocates and fills one 136-byte profile struct |
| `sub_486FF0` | `arch_parse_and_lookup` | 2,665 B | Parses an architecture name string, returns a 12-byte result record |
| `sub_44E3E0` | `arch_extract_sm_number` | 288 B | Extracts the numeric SM number from `sm_`/`compute_`/`lto_` prefix |
| `sub_44E490` | `arch_is_virtual` | 48 B | Returns true if the name starts with `compute_` or `lto_` |
| `sub_44E4F0` | `arch_has_suffix_a` | 32 B | Returns true if the name ends with `'a'` (ASCII 97) |
| `sub_44E510` | `arch_has_suffix_f` | 32 B | Returns true if the name ends with `'f'` (ASCII 102) |
| `sub_44E530` | `arch_format_name` | 176 B | Formats `"sm_NNN"` / `"compute_NNN"` / `"compute_NNNa"` / `"compute_NNNf"` from parts |
| `sub_448E70` | `LinkerHash::insert` | 3,728 B | Inserts a profile into the hash map under its name key |
| `sub_465720` | `list_append` | ~96 B | Appends a profile pointer to a compatibility/family linked list |

## Global State

| Address | Type | Description |
|---|---|---|
| `byte_2A5F8D0` | `uint8` | Init-once guard. Zero on first call, set to 1 after `sub_484F50` completes. |
| `qword_2A5F8D8` | `LinkerHash*` | The profile hash map. Key = architecture name string (`"sm_100a"`), value = pointer to 136-byte profile. |
| `qword_2A5F8E0` | `OrderedList*` | 128-entry ordered list of all **real (sm_)** profiles. |
| `qword_2A5F8E8` | `OrderedList*` | 128-entry ordered list of all **real (sm_)** and **non-LTO virtual** profiles. |
| `dword_2A5F8C8` | `uint32` | Forward-compatibility threshold. Set to `100` after the sm_100 family is registered. Used by `sub_486FF0` to distinguish SASS-capable architectures. |
| `dword_2A5F8CC` | `uint32` | Default minimum architecture. Set to `80` (sm_80) during initialization. |

## Profile Struct Layout

Each profile is a 136-byte heap allocation. `sub_484DB0` takes seven arguments and fills the struct as follows:

```
ArchProfile (136 bytes, 8-byte aligned)  [summary -- see structs/arch-profile.md]
===================================================================================
Offset  Size  Field                 Description
-----------------------------------------------------------------------------------
  0      1    is_virtual            0 = real (sm_), 1 = virtual (compute_/lto_)
  1      1    is_lto                0 = not LTO, 1 = LTO variant
  2      1    feature_byte_a        Finalization compatibility bitmask (bits checked
                                    by sub_4709E0 for sm_100/sm_102/sm_103 cross-fin)
  3      1    finalization_class    0-4. Indexes dword_1D40660[5]. Set to 1 for sm_89
                                    (Ada). Controls finalization compatibility rules
  4      1    suffix_a_flag         1 if this is an 'a' variant (sm_90a, sm_100a, ...)
  5      1    suffix_f_flag         1 if this is an 'f' variant (sm_100f, sm_103f, ...)
  6      2    version_limit         uint16 checked by sub_4709E0: if > 0x101, error 25
  8      8    arch_name             Pointer to canonical name string ("sm_100", "compute_100a", ...)
 16      8    display_name          Pointer to display name string (same as arch_name for base archs)
 24      8    isa_class_name        Pointer to ISA class name ("Turing", "Ampere", "Hopper", "Blackwell", "Ada", or "(profile_sm_NNN)->isaClass")
 32      8    canonical_name        Identity name. For sm_/compute_: same as arch_name. For lto_: the "lto_NNN" string. (constructor arg a7 -> v14[4])
 40      8    cuda_arch_define      Pointer to preprocessor define ("-D__CUDA_ARCH__=750", etc.). (constructor arg a6 -> v14[5])
 48      8    compat_list_0         Linked list: cross-variant compatibility. Links real <-> virtual and suffix variants to their base arch
 56      8    compat_list_1         Linked list: same-generation family. Links all archs in the same generation
 64      8    compat_list_2         Linked list: compute-to-real bidirectional mapping. For compute_: links to the corresponding real (sm_); for real: links to compute_
 72      8    virtual_ptr           For real (sm_): pointer to corresponding compute_ profile. For compute_: self-pointer. For lto_: pointer to corresponding compute_ profile
 80     48    capability_data       Three 16-byte XMM vectors (offsets +80, +96, +112) loaded from read-only data. Encode hardware capability bitmasks.
128      1    reserved_byte         Initially 0
129-135  7    padding               Zero
```

**Verification:** The constructor `sub_484DB0` writes `v14[4] = a7` (canonical_name at offset 32) and `v14[5] = a6` (cuda_arch_define at offset 40). Offset 64 is the third `list_create` call (`v14[8] = sub_465020(...)`), confirming it is a list head, not a profile pointer. The `virtual_ptr` lives at offset 72 (`v7[9] = v7` for compute_75 self-ref; `*((_QWORD *)v10 + 9) = compute_75` for lto_75).

The `capability_data` region at offsets +80..+127 stores three 128-bit vectors loaded from rodata constants (`xmmword_1D40F10` through `xmmword_1D40F70`). These encode generation-specific feature bitmasks used by the finalization pipeline to check whether a given compilation unit is compatible with the target architecture.

## Profile Initialization Sequence

`sub_484F50` is called lazily -- every code path that needs architecture information calls it, but the `byte_2A5F8D0` guard ensures the body executes exactly once. The function uses `setjmp`/`longjmp` for error handling (the same pattern used throughout nvlink for OOM recovery).

The initialization proceeds in strict order:

### Step 1: Create Infrastructure

```c
// sub_4FFBF0(4) -- acquire thread-local error context
// Create the main hash map (string-keyed, MurmurHash3)
qword_2A5F8D8 = LinkerHash::create(murmur3_string_hash, strcmp_equal, 8);

// Create two 128-entry ordered lists for iteration
qword_2A5F8E0 = OrderedList::create(128, strcmp_equal);
qword_2A5F8E8 = OrderedList::create(128, strcmp_equal);
```

### Step 2: Register Each Architecture

For each architecture, the pattern is identical. Taking sm_100 as an example:

```c
// 1. Create the real profile (is_virtual=0, is_lto=0)
sm_100 = ArchProfile::create(
    0,                               // is_virtual = false
    0,                               // is_lto = false
    "sm_100",                        // arch_name
    "sm_100",                        // display_name
    "Blackwell",                     // isa_class_name
    "-D__CUDA_ARCH__=1000",          // cuda_arch_define
    "sm_100"                         // canonical_name
);

// 2. Create the virtual profile (is_virtual=1, is_lto=0)
compute_100 = ArchProfile::create(
    1,                               // is_virtual = true
    0,                               // is_lto = false
    "compute_100",                   // arch_name
    "compute_100",                   // display_name
    "Blackwell",                     // isa_class_name
    "-D__CUDA_ARCH__=1000",          // cuda_arch_define
    "compute_100"                    // canonical_name
);

// 3. Link sm_ <-> compute_  (virtual_ptr lives at offset +72)
sm_100->virtual_ptr      = compute_100;   // offset +72: real -> compute
compute_100->virtual_ptr = compute_100;   // offset +72: compute self-pointer

// 4. Insert both into the hash map
LinkerHash::insert(qword_2A5F8D8, "sm_100",      sm_100);
LinkerHash::insert(qword_2A5F8D8, "compute_100", compute_100);

// 5. Create the LTO profile (is_virtual=1, is_lto=1)
lto_100 = ArchProfile::create(
    1,                               // is_virtual = true
    1,                               // is_lto = true
    "lto_100",                       // arch_name
    "compute_100",                   // display_name (shares compute_ display name)
    NULL,                            // isa_class_name = NULL for LTO variants
    "-D__CUDA_ARCH__=1000",          // cuda_arch_define
    "lto_100"                        // canonical_name
);

// 6. Link lto_ -> compute_ profile (virtual_ptr at offset +72)
lto_100->virtual_ptr = compute_100;            // offset +72
LinkerHash::insert(qword_2A5F8D8, "lto_100", lto_100);

// 7. Build compatibility lists via list_append
list_append(compute_100->compat_list_2, sm_100);      // offset +64: compute knows its real arch
list_append(sm_100->compat_list_2,      compute_100); // offset +64: real arch knows its virtual
list_append(sm_100->compat_list_1,      sm_100);      // offset +56: self in family list
list_append(sm_100->compat_list_0,      sm_100);      // offset +48: self in cross-variant list

// 8. Copy capability vectors from rodata
sm_100->capability[0] = xmmword_1D40F10;   // generation base capabilities
sm_100->capability[1] = xmmword_1D40F40;   // extended feature set
sm_100->capability[2] = xmmword_1D40F70;   // sm_100/Blackwell-specific features
```

### Step 3: Register Suffix Variants

For architectures that have `'a'` and `'f'` suffixed variants (sm_90a, sm_100a, sm_100f, etc.), additional steps occur:

```c
// 'a' variant: sm_100a
sm_100a = ArchProfile::create(
    0, 0,
    "sm_100a", "sm_100a",
    "(profile_sm_100)->isaClass",       // inherits ISA class from base
    "-D__CUDA_ARCH__=1000",             // same __CUDA_ARCH__ as base
    "sm_100a"
);
// ... create compute_100a, lto_100a similarly ...
sm_100a->suffix_a_flag = 1;             // byte +4
compute_100a->suffix_a_flag = 1;

// Capability vectors are COPIED from the base (sm_100), not loaded independently
sm_100a->capability[0..2] = sm_100->capability[0..2];

// Cross-link 'a' variant into base family
list_append(sm_100->compat_list_head_1, sm_100a);
list_append(sm_100a->compat_list_head_1, sm_100);
list_append(sm_100a->compat_list_head_0, sm_100);
list_append(sm_100->compat_list_head_0, sm_100a);
```

The `'f'` variant follows the same pattern but sets `suffix_f_flag` (byte +5) on all three profiles (real, virtual, LTO):

```c
sm_100f->suffix_f_flag = 1;             // byte +5
compute_100f->suffix_f_flag = 1;
lto_100f->suffix_f_flag = 1;            // LTO also gets the flag
```

The ISA class name for all suffix variants is the string `"(profile_sm_NNN)->isaClass"` rather than a family name like `"Blackwell"`. This string is a literal in the binary -- it is not a macro expansion but rather a debug-friendly name indicating ISA inheritance. The LTO define string for 'a' variants appends `"0"` to the suffix: `"-D__CUDA_ARCH__=100a0"`, `"-D__CUDA_ARCH__=90a0"`, etc. Similarly 'f' variants get `"-D__CUDA_ARCH__=100f0"`.

### Step 4: Finalize

```c
// Register atexit cleanup handler
atexit(sub_484D40);

// Set the guard flag
byte_2A5F8D0 = 1;
```

## Complete Architecture Table

The 22 base architectures registered by `sub_484F50`, in order of registration:

| # | Real Profile | Virtual Profile | LTO Profile | ISA Class | `__CUDA_ARCH__` | Suffix Variants | Family |
|---|---|---|---|---|---|---|---|
| 1 | `sm_75` | `compute_75` | `lto_75` | Turing | 750 | -- | Turing |
| 2 | `sm_80` | `compute_80` | `lto_80` | Ampere | 800 | -- | Ampere |
| 3 | `sm_86` | `compute_86` | `lto_86` | Ampere | 860 | -- | Ampere |
| 4 | `sm_87` | `compute_87` | `lto_87` | Ampere | 870 | -- | Ampere |
| 5 | `sm_88` | `compute_88` | `lto_88` | Ampere | 880 | -- | Ampere |
| 6 | `sm_89` | `compute_89` | `lto_89` | Ada | 890 | -- | Ada |
| 7 | `sm_90` | `compute_90` | `lto_90` | Hopper | 900 | -- | Hopper |
| 8 | `sm_90a` | `compute_90a` | `lto_90a` | (sm_90) | 900 | a | Hopper |
| 9 | `sm_100` | `compute_100` | `lto_100` | Blackwell | 1000 | -- | Blackwell |
| 10 | `sm_100a` | `compute_100a` | `lto_100a` | (sm_100) | 1000 | a | Blackwell |
| 11 | `sm_100f` | `compute_100f` | `lto_100f` | (sm_100) | 1000 | f | Blackwell |
| 12 | `sm_110` | `compute_110` | `lto_110` | Blackwell | 1100 | -- | Blackwell |
| 13 | `sm_110a` | `compute_110a` | `lto_110a` | (sm_110) | 1100 | a | Blackwell |
| 14 | `sm_110f` | `compute_110f` | `lto_110f` | (sm_110) | 1100 | f | Blackwell |
| 15 | `sm_103` | `compute_103` | `lto_103` | Blackwell | 1030 | -- | Blackwell |
| 16 | `sm_103a` | `compute_103a` | `lto_103a` | (sm_103) | 1030 | a | Blackwell |
| 17 | `sm_103f` | `compute_103f` | `lto_103f` | (sm_103) | 1030 | f | Blackwell |
| 18 | `sm_120` | `compute_120` | `lto_120` | Blackwell | 1200 | -- | Blackwell |
| 19 | `sm_120a` | `compute_120a` | `lto_120a` | (sm_120) | 1200 | a | Blackwell |
| 20 | `sm_120f` | `compute_120f` | `lto_120f` | (sm_120) | 1200 | f | Blackwell |
| 21 | `sm_121` | `compute_121` | `lto_121` | Blackwell | 1210 | -- | Blackwell |
| 22 | `sm_121a` | `compute_121a` | `lto_121a` | (sm_121) | 1210 | a | Blackwell |
| -- | `sm_121f` | `compute_121f` | `lto_121f` | (sm_121) | 1210 | f | Blackwell |

Total hash map entries: 22 base architectures x 3 variants (sm/compute/lto) + suffix variants (8 'a' + 8 'f' x 3 each) = **66 base + 48 suffix = 114 profile entries** in `qword_2A5F8D8`.

Notable observations:

- **sm_88** is a new Ampere-family architecture first appearing in CUDA 13.0. It was not publicly documented in earlier toolkit releases.
- **sm_89** (Ada Lovelace) has a unique flag: `profile->byte[3] = 1` (tessellation_flag). No other architecture sets this byte. This is set after the compatibility lists are built but before sm_90.
- **Registration order** does not match numeric order: sm_103 is registered after sm_110, and sm_120/sm_121 come last. The internal order is: 75, 80, 86, 87, 88, 89, 90, 90a, 100, 100a, 100f, 110, 110a, 110f, 103, 103a, 103f, 120, 120a, 120f, 121, 121a, 121f.
- **The `dword_2A5F8C8 = 100` assignment** occurs immediately after the sm_100f block. This threshold is used in `sub_486FF0` to distinguish architectures that support SASS-level features.

## Family Linkage

The profile struct holds **three** hash-set compatibility lists at offsets +48, +56, and +64 (all created via `sub_465020` in the constructor as `v14[6]`, `v14[7]`, `v14[8]`). Each list serves a distinct purpose:

### compat_list_0 (Offset +48): Cross-Variant Compatibility

This list connects a real profile to its virtual counterpart and vice versa. For suffix variants, it additionally links back to the base architecture. Example for the sm_100 family:

```
sm_100.compat_0 -> { compute_100, sm_100 }
compute_100.compat_0 -> { sm_100 }
sm_100a.compat_0 -> { compute_100a, sm_100a, sm_100 }
sm_100f.compat_0 -> { compute_100f, sm_100f, sm_100 }
```

### compat_list_1 (Offset +56): Same-Generation Family

This list links all architectures in the same generation/family. For the Ampere generation, the sm_80 profile's `compat_list_1` accumulates links to sm_86, sm_87, sm_88. The sm_89 (Ada) profile's `compat_list_1` additionally links back to sm_80's family entries since Ada can run Ampere code.

For the Blackwell generation, the `compat_list_1` on sm_120/sm_121 also accumulates cross-links to sm_120 from sm_121 and vice versa:

```
sm_120.compat_1 -> { ..., sm_121, sm_121a }
sm_121.compat_1 -> { ..., sm_120 }  (via final append block)
```

### compat_list_2 (Offset +64): Compute <-> Real Bidirectional Map

For `compute_` profiles, this list links to the corresponding real (`sm_`) profile; for `sm_` profiles, it links to the corresponding `compute_`. Example:

```
sm_75.compat_2      -> { compute_75 }
compute_75.compat_2 -> { sm_75 }
```

Note: this offset used to be mis-documented on this page as `virtual_profile_ptr`. It is in fact a list head -- the virtual pointer lives at offset +72 (see `structs/arch-profile.md`).

## Architecture Name Parsing

### sub_44E3E0: Extract SM Number

Parses the numeric SM identifier from an architecture name string. Handles three prefixes:

```c
uint32_t arch_extract_sm_number(const char *name) {
    if (!name) goto error;

    if (memcmp(name, "sm_", 3) == 0)
        return strtol(name + 3, NULL, 10);

    if (memcmp(name, "compute_", 8) == 0 && strlen(name) > 9)
        return strtol(name + 8, NULL, 10);

    if (memcmp(name, "lto_", 4) == 0)
        return strtol(name + 4, NULL, 10);

    error:
    report_error(ERR_INVALID_ARCH, name);
    return 0;
}
```

The `compute_` path has a length check (`strlen > 9`). A 9-character `compute_` string would be `"compute_X"` (single digit) -- these are handled as valid only if the total length exceeds 9, meaning two-or-more-digit architecture numbers. Single-digit compute architectures (which would be ancient, pre-Fermi) fall through to the LTO check and ultimately to the error path.

### sub_44E490: Is Virtual

A minimal predicate that returns true for `compute_` and `lto_` prefixes:

```c
bool arch_is_virtual(const char *name) {
    if (memcmp(name, "compute_", 8) == 0) return true;
    return memcmp(name, "lto_", 4) == 0;
}
```

### sub_44E4F0 / sub_44E510: Suffix Detection

Single-expression functions checking the last character:

```c
bool arch_has_suffix_a(const char *name) {
    return name[strlen(name) - 1] == 'a';   // ASCII 97
}

bool arch_has_suffix_f(const char *name) {
    return name[strlen(name) - 1] == 'f';   // ASCII 102
}
```

### sub_44E530: Format Architecture Name

Reconstructs an architecture name string from its components:

```c
int arch_format_name(char *buf, int sm_number, bool is_virtual, bool suffix_a, bool suffix_f) {
    if (sm_number < 1 || sm_number > 999) {
        buf[0] = '\0';
        return 0;
    }

    const char *suffix = "";
    if (suffix_a)      suffix = "a";
    else if (suffix_f) suffix = "f";

    const char *prefix = is_virtual ? "compute" : "sm";

    int n = snprintf(buf, 13, "%s_%d%s", prefix, sm_number, suffix);
    if (n > 12) {
        buf[0] = '\0';
        return 0;
    }
    return n;
}
```

The 13-byte buffer limit accommodates the longest valid name: `"compute_121f"` (12 characters + null).

## Legacy/Deprecated Architectures: sub_486FF0

`sub_486FF0` is the public entry point for resolving an architecture name to a numeric result record. It is 2,665 bytes and handles both current and legacy architectures.

### Flow

1. **Null check**: Return NULL if input string is null.
2. **Parse components**: Extract the SM number via `sub_44E3E0`, detect `'a'` suffix via `sub_44E4F0`, detect `'f'` suffix via `sub_44E510`.
3. **Ensure database is initialized**: Call `sub_484F50` (the lazy init).
4. **Hash map lookup**: Look up the input string in `qword_2A5F8D8`. If found, proceed to step 6.
5. **Handle 'a' suffix fallback**: If the lookup failed and the name has an `'a'` suffix, try stripping the suffix and looking up the base name. If the base name exists, mark this as a synthetic 'a' variant but continue. If still not found, check the deprecated list.
6. **Deprecated architecture check**: If the SM number matches any of these values, the architecture is recognized but deprecated:

   ```
   10, 11, 12, 13, 20, 21, 30, 32, 35, 37, 50, 52, 53, 60, 61, 62, 69, 70
   ```

   For these, `sub_486FF0` does **not** return NULL but instead sets a `deprecated` flag (byte +6 in the result) to 1. If the SM number is not in either the hash map or the deprecated list, the function returns NULL (unrecognized architecture).

7. **Build result record**: Allocate a 12-byte result struct:

```
ArchParseResult (12 bytes)
=======================================================
Offset  Size  Field               Description
-------------------------------------------------------
  0      4    sm_number            Numeric SM value (e.g. 100)
  4      1    is_compute_or_lto    1 if input was compute_ or lto_ prefix
  5      1    is_sass_capable      1 if sm_number >= dword_2A5F8C8 (100) AND not virtual AND not "sass_" prefix
  6      1    is_deprecated        1 if the architecture is in the deprecated list
  7      1    has_suffix_a         1 if the name ends with 'a'
  8      1    has_suffix_f         1 if the name ends with 'f'
  9-11   3    padding              Zero
```

### Deprecated Architecture Numbers

These correspond to historical NVIDIA GPU architectures no longer supported for code generation but still recognized for error messages:

| SM Numbers | Architecture | Era |
|---|---|---|
| 10, 11, 12, 13 | Tesla (G80/GT200) | 2006-2009 |
| 20, 21 | Fermi (GF100/GF110) | 2010-2012 |
| 30, 32, 35, 37 | Kepler (GK104/GK110/GK210) | 2012-2014 |
| 50, 52, 53 | Maxwell (GM107/GM200/GM204) | 2014-2016 |
| 60, 61, 62 | Pascal (GP100/GP102/GP106) | 2016-2018 |
| 69 | (unknown/internal) | -- |
| 70 | Volta (GV100) | 2017-2019 |

Notably absent from both the active database and the deprecated list: sm_72 (Xavier Volta) and sm_71 (not a real arch). sm_69 is listed as deprecated but does not correspond to any known public GPU -- it is an internal test target.

### The SASS Capability Check

The `is_sass_capable` field (byte +5 in the result) is computed as:

```c
bool is_sass_capable;
if (is_virtual) {
    is_sass_capable = false;
} else if (sm_number >= dword_2A5F8C8) {  // >= 100
    is_sass_capable = (memcmp(name, "sass_", 5) != 0);
} else {
    is_sass_capable = false;
}
```

This indicates that only real (non-virtual) architectures with SM >= 100 are considered "SASS-capable" in the context of nvlink's internal classification, unless the input is literally a `"sass_"` prefixed string (which is handled differently). The `"sass_"` prefix appears to be used for raw SASS binary inputs that bypass the normal profile system.

## Capability Vectors

The three 128-bit capability vectors at profile offsets +80, +96, and +112 are loaded from read-only data section constants. Each architecture generation uses a different combination:

| Architecture | Vector 0 (offset +80) | Vector 1 (offset +96) | Vector 2 (offset +112) |
|---|---|---|---|
| sm_75 (Turing) | `xmmword_1D40F10` | `xmmword_1D40F20` | `xmmword_1D40F30` |
| sm_80 (Ampere) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F30` |
| sm_86 | `xmmword_1D40F10` | `xmmword_1D40F50` | `xmmword_1D40F30` |
| sm_87, sm_88 | `xmmword_1D40F10` | same as sm_86 | `xmmword_1D40F30` |
| sm_89 (Ada) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F30` |
| sm_90 (Hopper) | `xmmword_1D40F10` | `xmmword_1D40F40` (sm_80 set) | `xmmword_1D40F30` |
| sm_100 (Blackwell) | `xmmword_1D40F10` | `xmmword_1D40F40` (sm_80 set) | `xmmword_1D40F70` |
| sm_110 | `xmmword_1D40F10` | `xmmword_1D40F60` (sm_89 set) | `xmmword_1D40F70` |
| sm_103 | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F70` |
| sm_120, sm_121 | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F70` |

All suffix variants (`'a'`, `'f'`) inherit capability vectors from their base architecture by direct `_mm_loadu_si128` copy rather than loading from rodata.

The capability data is consumed by the finalization pipeline (`sub_4709E0`, `sub_470DA0`) through the `can_finalize_architecture_check` and `can_finalize_with_capability_mask` functions, which use bitmask operations on these vectors to determine whether a compilation unit compiled for one architecture can be finalized for another. See the [Compatibility](compatibility.md) page for details.

## Thread Safety

The init-once guard `byte_2A5F8D0` is protected by `sub_4FFBF0(4)` / `sub_4FFC10(4)`, which acquire and release the fourth slot in nvlink's global mutex array. This makes the lazy initialization thread-safe for the concurrent finalization (JIT API) path. Once initialized, the hash map and profile structs are immutable -- no locking is needed for read-only lookups.

## How Profiles Are Used

Architecture profiles flow through the entire nvlink pipeline:

1. **CLI parsing**: The `--arch` / `-arch` option string is looked up in `qword_2A5F8D8` to get the target profile.
2. **Input validation**: Each input cubin/fatbin's embedded architecture is parsed via `sub_44E3E0` and compared against the target.
3. **LTO compilation**: The LTO profile's `cuda_arch_define` string is passed to the embedded compiler (`-D__CUDA_ARCH__=NNN`).
4. **Finalization**: The `can_finalize` functions check capability vector compatibility between the compilation unit's source profile and the link target profile.
5. **Output ELF**: The target profile's SM number is written into the ELF header flags and `.nv.info` section attributes.

## Confidence Assessment

| Claim | Confidence | Verification |
|---|---|---|
| `sub_484F50` is 53,974 B lazy singleton initializer | HIGH | Decompiled file is 1,330 lines; address confirmed in binary |
| `sub_484DB0` creates 136-byte profile structs | HIGH | Decompiled code shows 7-arg signature matching wiki description |
| Struct field layout (offsets 32, 40, 48, 56, 64, 72) | CONFIRMED | Decompiled `sub_484DB0` assigns `v14[4]=a7` (canonical_name @32), `v14[5]=a6` (cuda_arch_define @40), `v14[6/7/8]=list_create(...)` (compat_list_0/1/2 @48/56/64); `sub_484F50` uses `v7[9]=v7` to set `virtual_ptr` at offset 72. See `structs/arch-profile.md` for authoritative layout. |
| ISA class strings: "Turing", "Ampere", "Ada", "Hopper", "Blackwell" | CONFIRMED | Decompiled `sub_484F50` at lines 251/293/468/517/609 uses these exact strings; all except "Ada" found in `nvlink_strings.json` at `0x1d409dc`/`0x1d40a0f`/`0x1d40af0`/`0x1d40b6e`; "Ada" at decompiled line 468 (3-char string not extracted separately by string dumper) |
| Suffix variant ISA class is `"(profile_sm_NNN)->isaClass"` | CONFIRMED | Strings at `0x1d40b0f`, `0x1d40b93`, `0x1d40c46`, `0x1d40cf9`, `0x1d40dac`, `0x1d40e5f` match exactly |
| `byte[3] = 1` set only for sm_89 | CONFIRMED | Decompiled line 511: `v47->m128i_i8[3] = 1;` immediately after sm_89 block; no other architecture sets this byte |
| 22 base architectures in registration order | HIGH | Decompiled code shows sm_75 at line 249, sm_80 at line 286, through sm_121 at line 1175; registration order matches wiki |
| `__CUDA_ARCH__` define values (750, 800, ..., 1210) | CONFIRMED | `nvlink_strings.json` lists all 23 defines at `0x1d409c8`--`0x1d40ec3`; suffix variants use `90a0`/`100a0`/`100f0` format as documented |
| `dword_2A5F8C8 = 100` forward-compat threshold | HIGH | Referenced in decompiled `sub_486FF0`; consistent with SASS capability check logic |
| Global `qword_2A5F8D8` hash map, `byte_2A5F8D0` guard | HIGH | Both referenced extensively in decompiled `sub_484F50` and `sub_4878A0` |
| sm_88 new in CUDA 13.0 | HIGH | `sm_88` string at `0x1d40a9a`; dispatch table registration confirmed in decompiled `sub_15C0CE0` line 96 |
| `sub_484DB0` 7-argument signature | CONFIRMED | Decompiled calls at lines 246-253 show exactly 7 args: `(is_virtual, is_lto, name, display, isa_class, cuda_arch, canonical)` |
| Capability vectors from `xmmword_1D40F10`--`xmmword_1D40F70` | HIGH | Decompiled `sub_484F50` shows `_mm_load_si128` from these addresses (e.g., lines 460, 499-505) |
| Total 114 profile entries (66 base + 48 suffix) | MEDIUM | Count derived from registration pattern; exact count not directly verified but consistent with 22 base x 3 + suffix variants x 3 |

For general architecture details (hardware specs, product lines), see the [ptxas wiki targets](../../ptxas/targets/index.html) and [cicc wiki targets](../../cicc/targets/index.html).

## Cross-References

### nvlink Internal
- [Compatibility](compatibility.md) -- architecture compatibility checking using profile data
- [SM100 Blackwell](sm100-blackwell.md) -- Blackwell-specific ISA and encoding details
- [SM103/110/120/121](sm103-121.md) -- extended Blackwell family profiles
- [Architecture Dispatch](../ptxas/arch-dispatch.md) -- embedded ptxas vtable dispatch (7 maps per SM)
- [Device ELF Format](../elf/device-elf-format.md) -- e_flags encoding derived from profiles

### Sibling Wikis
- [ptxas: SM Architecture Map](../../ptxas/targets/index.html) -- standalone ptxas profile construction (`sub_6765E0`, 54KB) and 7 parallel capability dispatch tables
- [ptxas: Turing/Ampere](../../ptxas/targets/turing-ampere.html) -- SM75/SM80/SM86/SM87/SM89 targets in standalone ptxas
- [ptxas: Ada/Hopper](../../ptxas/targets/ada-hopper.html) -- SM89/SM90 targets in standalone ptxas
- [ptxas: Blackwell](../../ptxas/targets/blackwell.html) -- SM100+ targets in standalone ptxas
- [cicc: Targets Index](../../cicc/targets/index.html) -- cicc compiler target definitions
- [cicc: SM70-89](../../cicc/targets/sm70-89.html) -- cicc Volta through Ada targets
- [cicc: SM90 Hopper](../../cicc/targets/sm90-hopper.html) -- cicc Hopper target
- [cicc: SM100 Blackwell](../../cicc/targets/sm100-blackwell.html) -- cicc Blackwell target
- [cicc: SM120](../../cicc/targets/sm120.html) -- cicc SM120 consumer target
