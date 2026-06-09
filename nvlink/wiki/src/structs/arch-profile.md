# Architecture Profile

The `ArchProfile` struct is a 136-byte heap-allocated descriptor that encodes everything nvlink needs to know about a single GPU architecture target. Each recognized architecture (e.g. `sm_100`) produces three profile instances — a real profile (`sm_`), a virtual profile (`compute_`), and an LTO profile (`lto_`) — all stored in a global hash map keyed by name string. The struct is created by `sub_484DB0` and consumed throughout the linking, finalization, and output pipelines.

This page documents the byte-level layout derived from the constructor (`sub_484DB0`), the database initializer (`sub_484F50`), and the two finalization compatibility checkers (`sub_4709E0`, `sub_470DA0`).

## Constructor: sub_484DB0

```c
Prototype (reconstructed):
    ArchProfile* ArchProfile::create(
        uint8_t  is_virtual,       // a1: 0=real (sm_), 1=virtual (compute_/lto_)
        uint8_t  is_lto,           // a2: 0=not LTO, 1=LTO variant
        char*    arch_name,        // a3: "sm_100", "compute_100", "lto_100"
        char*    display_name,     // a4: display name (same as arch_name for base archs)
        char*    isa_class_name,   // a5: "Turing", "Ampere", "Blackwell", "Ada", or
                                   //     "(profile_sm_NNN)->isaClass" for suffix variants
                                   //     NULL for LTO variants
        char*    cuda_arch_define, // a6: "-D__CUDA_ARCH__=1000"
        char*    canonical_name    // a7: same as arch_name for sm_/compute_;
                                   //     points to compute_ name for lto_
    )

Address: 0x484DB0
Size: ~400 bytes
```

The constructor allocates 136 bytes via `sub_4307C0`, zeros the entire allocation (using an SSE-aligned `memset` loop), then writes the seven arguments into their respective offsets. It also creates three linked-list-head objects at offsets 48, 56, and 64 via `sub_465020`, and registers the profile into two ordered lists (`qword_2A5F8E0`, `qword_2A5F8E8`).

### Allocation and Zeroing

```c
profile = alloc(allocator, 136);
if (!profile) oom_handler(allocator, 136);

// Zero entire struct (SSE-aligned zeroing pattern)
*(uint16_t*)(profile + 2) = 0;      // bytes 2-3
profile[16] = 0;                      // qword at offset 128
memset_aligned(profile + 10, 0, ...); // bulk zero from ~offset 10 to 136
```

The zeroing is somewhat redundant with the memset but ensures no stale data in any field before explicit assignment.

### Field Assignment

```c
profile->byte[0]   = is_virtual;        // a1
profile->byte[1]   = is_lto;            // a2
profile->qword[1]  = arch_name;         // a3 -> offset 8
profile->qword[2]  = display_name;      // a4 -> offset 16
profile->qword[3]  = isa_class_name;    // a5 -> offset 24
profile->qword[4]  = canonical_name;    // a7 -> offset 32
profile->qword[5]  = cuda_arch_define;  // a6 -> offset 40

// Create three linked list heads for compatibility tracking
profile->qword[6]  = list_create(str_hash, str_equal, 8);  // offset 48
profile->qword[7]  = list_create(str_hash, str_equal, 8);  // offset 56
profile->qword[8]  = list_create(str_hash, str_equal, 8);  // offset 64

// Post-assignment clearing
profile->word[1]    = 0;    // bytes 2-3 (re-zeroed)
profile->byte[128]  = 0;    // byte 128
profile->word[2]    = 0;    // bytes 4-5
```

Note the argument ordering anomaly: `canonical_name` (a7) goes to offset 32 while `cuda_arch_define` (a6) goes to offset 40. This means offset 32 holds the "identity" name (what this profile "is"), while offset 40 holds the compiler define string.

### Ordered List Registration

After construction, the profile is registered into global ordered lists:

```c
// Register into qword_2A5F8E0 (all real+virtual profiles, 128-capacity)
if (list_needs_resize(qword_2A5F8E0))
    list_resize(qword_2A5F8E0, 0x2C);  // grow by 44
list_insert(qword_2A5F8E0, arch_name);

// For non-virtual profiles, also register into qword_2A5F8E8
if (!is_virtual) {
    if (list_needs_resize(qword_2A5F8E8))
        list_resize(qword_2A5F8E8, 0x2C);
    list_insert(qword_2A5F8E8, arch_name);
}
```

## Struct Layout

```text
ArchProfile (136 bytes, 8-byte aligned, heap-allocated)
==========================================================================
Offset  Size  Type      Field                 Description
--------------------------------------------------------------------------
  0      1    uint8     is_virtual            0 = real (sm_), 1 = virtual
                                              (compute_ or lto_)
  1      1    uint8     is_lto                0 = not LTO, 1 = LTO variant
  2      1    uint8     feature_byte_a        Finalization compatibility
                                              bitmask. Bits [1:0] and
                                              [3:2] checked by sub_4709E0
                                              for sm_100/sm_102/sm_103
                                              cross-finalization
  3      1    uint8     finalization_class     0-4. Indexes into
                                              dword_1D40660[5] lookup
                                              table. Set to 1 for sm_89
                                              (Ada tessellation flag).
                                              Controls finalization
                                              compatibility rules
  4      1    uint8     suffix_a              1 if 'a' variant (sm_90a,
                                              sm_100a, ...). Set on all
                                              three profiles (sm/compute/
                                              lto) for 'a' architectures
  5      1    uint8     suffix_f              1 if 'f' variant (sm_100f,
                                              sm_103f, ...). Set on all
                                              three profiles for 'f'
                                              architectures
  6      2    uint16    version_limit         Checked in sub_4709E0:
                                              if value > 0x101, returns
                                              error 25 (version too high).
                                              Always 0 for CUDA 13.0
                                              profiles
  8      8    char*     arch_name             "sm_100", "compute_100a",
                                              "lto_100f", etc.
 16      8    char*     display_name          Display/UI name. Same as
                                              arch_name for base archs.
                                              For LTO: points to the
                                              compute_ name string
 24      8    char*     isa_class_name        ISA family: "Turing",
                                              "Ampere", "Hopper", "Ada",
                                              "Blackwell". For suffix
                                              variants: literal string
                                              "(profile_sm_NNN)->isaClass".
                                              NULL for LTO profiles
 32      8    char*     canonical_name        Identity name. For sm_ and
                                              compute_: same as arch_name.
                                              For lto_: same as arch_name
                                              (the "lto_NNN" string)
 40      8    char*     cuda_arch_define      Preprocessor define passed
                                              to cicc/ptxas:
                                              "-D__CUDA_ARCH__=750",
                                              "-D__CUDA_ARCH__=100a0",
                                              etc.
 48      8    List*     compat_list_0         Linked list: cross-variant
                                              compatibility. Links real
                                              <-> virtual profiles and
                                              suffix variants to their
                                              base arch
 56      8    List*     compat_list_1         Linked list: same-generation
                                              family. Links all archs in
                                              the same generation (e.g.
                                              sm_80 links to sm_86/87/88)
 64      8    List*     compat_list_2         Linked list: additional
                                              cross-references. For
                                              compute_ profiles: links
                                              to corresponding real arch.
                                              For real profiles: links
                                              to compute_ arch
 72      8    ArchProfile*  virtual_ptr       For real (sm_) profiles:
                                              pointer to the compute_
                                              profile. For compute_
                                              profiles: self-pointer.
                                              For lto_ profiles: pointer
                                              to the compute_ profile
 80     16    xmm128    capability_vec_0      Generation base capabilities.
                                              Loaded from xmmword_1D40F10
                                              for all current archs
 96     16    xmm128    capability_vec_1      Extended feature set. Varies
                                              by architecture. Determines
                                              cross-arch finalization
                                              compatibility
112     16    xmm128    capability_vec_2      Architecture-specific
                                              features. Two distinct
                                              values: xmmword_1D40F30
                                              (pre-Blackwell) or
                                              xmmword_1D40F70 (Blackwell+)
128      1    uint8     reserved              Always 0 in CUDA 13.0
129      7    --        padding               Zero
```

## Capability Vectors (Offsets 80-127)

The three 128-bit vectors at offsets +80, +96, and +112 encode hardware capabilities as bitmasks. They are loaded from read-only data constants during `sub_484F50` initialization. Suffix variants (`'a'`, `'f'`) inherit vectors by SSE copy (`_mm_loadu_si128`) from their base arch rather than loading from rodata independently.

### Vector Assignment by Architecture

| Architecture | Vec 0 (+80) | Vec 1 (+96) | Vec 2 (+112) | Notes |
|---|---|---|---|---|
| sm_75 | `1D40F10` | `1D40F20` | `1D40F30` | Turing: unique vec 1 |
| sm_80 | `1D40F10` | `1D40F40` | `1D40F30` | Ampere base |
| sm_86 | `1D40F10` | `1D40F50` | `1D40F30` | Ampere: different vec 1 |
| sm_87, sm_88 | `1D40F10` | `1D40F50` | `1D40F30` | Inherit sm_86 pattern |
| sm_89 | `1D40F10` | `1D40F60` | `1D40F30` | Ada: distinct vec 1 |
| sm_90 | `1D40F10` | `1D40F40` | `1D40F30` | Hopper: shares sm_80 vec 1 |
| sm_100 | `1D40F10` | `1D40F40` | `1D40F70` | Blackwell: new vec 2 |
| sm_103 | `1D40F10` | `1D40F40` | `1D40F70` | Shares sm_100 pattern |
| sm_110 | `1D40F10` | `1D40F60` | `1D40F70` | Ada vec 1 + Blackwell vec 2 |
| sm_120 | `1D40F10` | `1D40F60` | `1D40F70` | Same as sm_110 |
| sm_121 | `1D40F10` | `1D40F60` | `1D40F70` | Same as sm_120 |

Key observations:

- **Vec 0** is identical for all architectures — a universal base capability set.
- **Vec 1** has five distinct values, grouping architectures by instruction set similarity: Turing alone (F20), Ampere-base/Hopper/sm_100/sm_103 (F40), Ampere-extended (F50), and Ada/sm_110/sm_120/sm_121 (F60).
- **Vec 2** has two values: `1D40F30` for pre-Blackwell (sm_75 through sm_90a) and `1D40F70` for Blackwell-generation (sm_100+).

### Capability Vector Usage

The finalization function `sub_470DA0` (`can_finalize_with_capability_mask`) reads the capability data through a pointer at profile offset +16 to check bitmask compatibility. It maps architecture family codes to bitmask values:

```c
switch (target_arch_code) {
    case 'd' (100): mask = 1;    // sm_100 (datacenter Blackwell)
    case 'g' (103): mask = 8;    // sm_103 (Blackwell Ultra)
    case 'n' (110): mask = 2;    // sm_110 (Jetson Thor)
    case 'y' (121): mask = 64;   // sm_121 (DGX Spark)
    default:        return 0;    // not capable
}
if ((mask & *capability_ptr) != mask)
    return 0;  // target capabilities not satisfied
```

## Finalization Class Field (Byte 3)

The `finalization_class` byte at offset 3 indexes into the `dword_1D40660[5]` lookup table. The `sub_4709E0` (`can_finalize_architecture_check`) function interprets it as follows:

| dword_1D40660 value | Meaning | Behavior |
|---|---|---|
| 0 | Default | suffix_a must be 0; sm_110 cross-arch not allowed |
| 1 | Base-only | suffix_a blocks finalization (if class=1 and suffix_a set, error 26) |
| 2 | Family-compatible | Same-decade rule: `target/10 == source/10` required |
| 3 | Cross-family | Allows cross-family within certain conditions (sm_110/sm_121 special cases) |
| 4 | Full-compat | Broadest compatibility; handles sm_110 cross-arch |

The only architecture that explicitly sets byte 3 during initialization is **sm_89 (Ada)**, where `profile->byte[3] = 1` is assigned after the compatibility lists are built. All other architectures leave byte 3 at its zero-initialized value.

## Feature Byte A (Byte 2)

Byte 2 at offset +2 is checked in `sub_4709E0` with a specific bit-field test:

```c
if (((profile->byte[2] >> 2) & 3) == 1 && (profile->byte[2] & 3) == 1)
    return 0;  // compatible
return 28;     // error
```

This extracts two 2-bit fields from byte 2:
- Bits [1:0]: low field, must equal 1
- Bits [3:2]: high field, must equal 1

The combined value 0x05 (binary 0b00000101) passes the check. This test appears only in the sm_100/sm_102/sm_103 cross-finalization path (source=100, target in range 102-103). In CUDA 13.0, byte 2 is zero-initialized for all profiles and no initialization code sets it, suggesting this field is either set dynamically at runtime or reserved for future use.

## Version Limit Field (Bytes 6-7)

The 16-bit word at offset 6 (`*((_WORD *)profile + 3)`) is checked early in `sub_4709E0`:

```c
if (profile->version_limit > 0x101)
    return 25;  // error: version too high
```

All profiles are zero-initialized, so this check always passes in CUDA 13.0. The `0x101` threshold (257 decimal) suggests this was designed as a forward-compatibility guard — if a profile's version exceeds the linker's known maximum, finalization is rejected.

## Linked List Heads (Offsets 48-64)

Each of the three linked list pointers at offsets 48, 56, and 64 is a full hash-set object created by `sub_465020` with string hashing and comparison functions. They are **not** simple singly-linked lists but hash-based sets that support O(1) membership testing. The `sub_465720` (`list_append`) function used to populate them is the same hash-set insertion function used throughout nvlink.

### compat_list_0 (Offset 48): Cross-Variant Links

For base architectures, this list connects the real profile to its virtual counterpart and self:

```text
sm_100.compat_list_0 -> { compute_100, sm_100 }
compute_100.compat_list_0 -> { sm_100 }
```

For suffix variants, the base arch is also linked:

```text
sm_100a.compat_list_0 -> { compute_100a, sm_100a, sm_100 }
sm_100f.compat_list_0 -> { compute_100f, sm_100f, sm_100 }
```

### compat_list_1 (Offset 56): Same-Generation Family

Links all architectures within the same generation. For Ampere:

```text
sm_80.compat_list_1 -> { sm_80, sm_86, sm_87, sm_88, sm_89 }
```

The sm_89 (Ada) profile is appended to sm_80's family list despite being classified as "Ada" rather than "Ampere" — this reflects hardware backward compatibility.

For Blackwell, both intra-family and cross-family links exist:

```text
sm_120.compat_list_1 -> { sm_120, sm_121, sm_121a }
sm_121.compat_list_1 -> { sm_121, sm_120 }
```

### compat_list_2 (Offset 64): Compute-to-Real Mapping

For compute_ profiles, this list links to the corresponding real (sm_) profile. For real profiles, it links to the compute_ profile. This provides bidirectional real<->virtual navigation.

## Virtual Pointer (Offset 72)

The `virtual_ptr` field at offset 72 establishes the primary profile cross-reference:

| Profile Type | virtual_ptr Value |
|---|---|
| Real (sm_) | Pointer to corresponding compute_ profile |
| Virtual (compute_) | Self-pointer (points to itself) |
| LTO (lto_) | Pointer to corresponding compute_ profile |

The self-pointer for compute_ profiles allows code that follows `profile->virtual_ptr` to always reach a compute_ profile regardless of the input profile type. This simplifies the finalization pipeline, which needs the compute_ profile's `cuda_arch_define` string.

## Destructor: sub_484D00

```c
void ArchProfile::destroy(ArchProfile* profile) {
    char* arch_name = profile->arch_name;  // offset 8

    // Remove from global hash map
    LinkerHash::remove(qword_2A5F8D8, arch_name);

    // Destroy three linked list heads
    list_destroy(profile->compat_list_0, arch_name);   // offset 48
    list_destroy(profile->compat_list_1, arch_name);   // offset 56
    list_destroy(profile->compat_list_2, arch_name);   // offset 64

    // Free the profile allocation itself
    free(profile, arch_name);
}
```

The destructor is called indirectly through `sub_484D40` (the database teardown function registered via `atexit`). Teardown walks the hash map calling `destroy` on each entry, then destroys the hash map itself and both ordered lists.

## Database Teardown: sub_484D40

```c
void ArchProfileDB::teardown() {
    if (!byte_2A5F8D0) return;  // not initialized
    byte_2A5F8D0 = 0;

    // Walk hash map, call destroy on each value, then destroy map
    LinkerHash::for_each(qword_2A5F8D8, ArchProfile::destroy, 0);
    LinkerHash::destroy(qword_2A5F8D8, ArchProfile::destroy);
    qword_2A5F8D8 = 0;

    // Destroy ordered lists
    OrderedList::destroy(qword_2A5F8E0, ArchProfile::destroy);
    OrderedList::destroy(qword_2A5F8E8, ArchProfile::destroy);
}
```

## Profile-to-ParseResult: sub_486DC0

Given a profile pointer (obtained from the hash map), `sub_486DC0` constructs a 12-byte `ArchParseResult`:

```c
ArchParseResult* profile_to_parse_result(ArchProfile* profile) {
    if (!profile) return NULL;

    ArchParseResult* result = alloc(allocator, 12);
    memset(result, 0, 12);

    result->is_compute_or_lto = profile->is_virtual;       // byte[4] <- byte[0]

    char* name = profile->arch_name;                        // offset 8
    uint32_t sm_num = arch_extract_sm_number(name);

    bool is_sass_capable;
    if (arch_is_virtual(name)) {
        is_sass_capable = false;
    } else if (sm_num >= dword_2A5F8C8) {
        is_sass_capable = (memcmp(name, "sass_", 5) != 0);
    } else {
        is_sass_capable = false;
    }
    result->is_sass_capable = is_sass_capable;              // byte[5]
    result->sm_number = arch_extract_sm_number(name);       // dword[0]
    result->has_suffix_a = arch_has_suffix_a(name);         // byte[7]
    result->has_suffix_f = arch_has_suffix_f(name);         // byte[8]

    return result;
}
```

The `ArchParseResult` layout:

```text
ArchParseResult (12 bytes)
==========================================================================
Offset  Size  Type      Field               Description
--------------------------------------------------------------------------
  0      4    uint32    sm_number           Numeric SM (75, 80, 100, ...)
  4      1    uint8     is_compute_or_lto   1 if virtual profile
  5      1    uint8     is_sass_capable     1 if real + sm >= 100 + not "sass_"
  6      1    uint8     (unused)            Always 0 from this path
  7      1    uint8     has_suffix_a        1 if name ends with 'a'
  8      1    uint8     has_suffix_f        1 if name ends with 'f'
  9-11   3    --        padding             Zero
```

## Key Functions

| Address | Size | Name | Role |
|---|---|---|---|
| `sub_484DB0` | 400 B | `ArchProfile::create` | Constructor: allocates 136 bytes, fills fields, creates list heads |
| `sub_484D00` | 56 B | `ArchProfile::destroy` | Destructor: removes from hash map, destroys lists, frees |
| `sub_484D40` | 112 B | `ArchProfileDB::teardown` | atexit handler: destroys all profiles and global state |
| `sub_484F50` | 53,974 B | `ArchProfileDB::init` | Lazy singleton initializer: registers all 22+ architectures |
| `sub_486DC0` | 528 B | `profile_to_parse_result` | Extracts a 12-byte parse result from a profile pointer |
| `sub_4709E0` | 2,609 B | `can_finalize_arch_check` | Checks arch compatibility for finalization (reads bytes 2-4, word 6) |
| `sub_470DA0` | 2,074 B | `can_finalize_with_caps` | Checks capability bitmask compatibility (reads offset +16) |

## Cross-References

- [Architecture Profiles (overview)](../targets/arch-profiles.md) — database initialization sequence, complete architecture table, name parsing
- [Compatibility](../targets/compatibility.md) — finalization compatibility rules
- [Finalize](../pipeline/finalize.md) — how profiles flow through the finalization pipeline
- [CLI Options](../pipeline/cli-options.md) — `--arch` option triggers profile lookup

## Confidence Assessment

Verified against decompiled `sub_484DB0_0x484db0.c` (constructor), `sub_484F50_0x484f50.c` (database init, lines 240-1280), `sub_4709E0_0x4709e0.c` (finalization check), `sub_470DA0_0x470da0.c` (capability mask check), `sub_484D00_0x484d00.c` (destructor), `sub_484D40_0x484d40.c` (teardown), and `nvlink_strings.json`.

### Struct Size and Allocation

| Claim | Confidence | Evidence |
|---|---|---|
| ArchProfile struct size = 136 bytes | HIGH | `sub_484DB0` line 24: `v14 = sub_4307C0(v11, 136);` and OOM fallback `sub_45CAC0(v11, 136, ...)` |
| Heap/arena allocation via `sub_4307C0` | HIGH | `sub_484DB0` line 23-24: `v11 = *((_QWORD *)sub_44F410(a1, a2) + 3); v14 = sub_4307C0(v11, 136);` |
| Constructor at `0x484DB0` | HIGH | `sub_484DB0_0x484db0.c` header: `// Address: 0x484db0` |
| Constructor takes 7 arguments | HIGH | `sub_484DB0_0x484db0.c` line 4-11: `(__int64 a1, pthread_mutexattr_t *a2, pthread_mutexattr_t *a3, __int64 a4, __int64 a5, __int64 a6, __int64 a7)` |
| SSE-aligned memset-to-zero for body | HIGH | `sub_484DB0` line 33-36: `memset(((unsigned __int64)v14 + 10) & 0xFFFFFFFFFFFFFFF8LL, 0, 8LL * (...))` |

### Byte Fields (offsets 0-7)

| Claim | Confidence | Evidence |
|---|---|---|
| Byte 0: `is_virtual` flag stored from a1 | HIGH | `sub_484DB0` line 38: `*(_BYTE *)v14 = v8;` where `v8 = a1`. Initialization in `sub_484F50` always passes 0 (for `sm_`) or 1 (for `compute_`/`lto_`) |
| Byte 1: `is_lto` flag stored from a2 | HIGH | `sub_484DB0` line 39: `*((_BYTE *)v14 + 1) = (_BYTE)a2;`. `sub_484F50` passes 0 for `sm_`/`compute_` and 1 for `lto_` |
| Byte 2: `feature_byte_a` with two 2-bit fields, zero-init in CUDA 13.0 | HIGH | `sub_4709E0` line 143: `if (((unsigned __int8)a1[2] >> 2) & 3) == 1 && (a1[2] & 3) == 1) return 0;`. No write to byte 2 observed in `sub_484F50` |
| Byte 3: `finalization_class` index into `dword_1D40660[5]` | HIGH | `sub_4709E0` lines 56-60: `v8 = (unsigned __int8)a1[3]; result = 26; if ((unsigned __int8)v8 > 4u) return result; v9 = dword_1D40660[v8];` |
| sm_89 is the ONLY arch setting byte 3 = 1 | HIGH | `sub_484F50` line 511: `v47->m128i_i8[3] = 1;` (only occurrence in entire init; `v47` is sm_89 real profile) |
| Byte 4: `suffix_a` flag | HIGH | `sub_484F50` line 682: `v79->m128i_i8[4] = 1;` (sm_100a); line 683: `*(_BYTE *)(v82 + 4) = 1;` (compute_100a). `sub_4709E0` line 65: `v10 = a1[4]; if (v10) { if (v9 == 1) return result; }` |
| Byte 5: `suffix_f` flag | HIGH | `sub_484F50` line 732: `v88->m128i_i8[5] = 1;` (sm_100f); line 733: `*(_BYTE *)(v91 + 5) = 1;` (compute_100f); line 734: `v94[5] = 1;` (lto_100f) |
| Version limit at word offset 3 (bytes 6-7) | HIGH | `sub_4709E0` line 51: `if ( *((_WORD *)a1 + 3) > 0x101u ) return result;` with `result = 25` |
| Error code 25 for version overflow | HIGH | `sub_4709E0` line 50-52 |
| Word at offset 2 re-zeroed after `v14[4] = a7` | HIGH | `sub_484DB0` line 47: `*((_WORD *)v14 + 1) = 0;` |
| Word at offset 4 re-zeroed at end | HIGH | `sub_484DB0` line 59: `*((_WORD *)v14 + 2) = 0;` |

### Pointer Fields (offsets 8-40)

| Claim | Confidence | Evidence |
|---|---|---|
| `arch_name` at offset 8 (qword, from a3) | HIGH | `sub_484DB0` line 40: `v14[1] = a3;` |
| `display_name` at offset 16 (qword, from a4) | HIGH | `sub_484DB0` line 41: `v14[2] = a4;` |
| `isa_class_name` at offset 24 (qword, from a5) | HIGH | `sub_484DB0` line 37: `v14[3] = v16;` where `v16 = a5` (line 25) |
| `canonical_name` at offset 32 (qword, from a7) | HIGH | `sub_484DB0` line 48: `v14[4] = a7;` |
| `cuda_arch_define` at offset 40 (qword, from a6) | HIGH | `sub_484DB0` line 42: `v14[5] = a6;` |
| Argument ordering anomaly (a6 to offset 40, a7 to offset 32) | HIGH | Constructor literally writes `v14[4] = a7; v14[5] = a6;` in that order |
| isa_class strings "Turing", "Ampere", "Hopper", "Blackwell" present in binary | HIGH | `nvlink_strings.json` lines 17340 (Turing), 17620 (Ampere), 18410 (Hopper), 18850 (Blackwell). Passed directly as a5 in `sub_484F50` lines 251, 293, 517, 609 |
| "Ada" ISA class string | MEDIUM | `sub_484F50` lines 468, 476 pass literal `(__int64)"Ada"` to `sub_484DB0`, but the string does not appear as a standalone entry in `nvlink_strings.json` (likely merged with another string in the extraction) |
| Suffix-variant isa_class = `"(profile_sm_NNN)->isaClass"` | HIGH | `sub_484F50` lines 650, 658, 699, 707, 792, 800, etc. pass literal `"(profile_sm_NNN)->isaClass"` strings |
| LTO variants pass NULL isa_class | HIGH | `sub_484F50` lines 273, 315, 489, 539, 630, 672, 722, etc. all pass `0` as the a5 argument for `lto_*` calls |
| LTO variants pass `compute_*` as display_name (a4) | HIGH | `sub_484F50` line 272: `(__int64)"compute_75"` for `lto_75`; line 314 for `lto_80`; line 488 for `lto_89`; etc. |

### List Heads (offsets 48-64)

| Claim | Confidence | Evidence |
|---|---|---|
| `compat_list_0` at offset 48 (v14[6]) | HIGH | `sub_484DB0` line 43: `v14[6] = sub_465020(sub_44E1C0, sub_44E1E0, 8u);`. Destructor `sub_484D00` line 10: `sub_4650A0(a1[6], v1);` |
| `compat_list_1` at offset 56 (v14[7]) | HIGH | `sub_484DB0` line 44: `v14[7] = sub_465020(...);`. Destructor line 11: `sub_4650A0(a1[7], v1);` |
| `compat_list_2` at offset 64 (v14[8]) | HIGH | `sub_484DB0` line 45: `v14[8] = sub_465020(...);`. Destructor line 12: `sub_4650A0(a1[8], v1);` |
| Three list heads use string hash/compare (sub_44E1C0/sub_44E1E0) | HIGH | Exact function pointers `sub_44E1C0`, `sub_44E1E0` in all three `sub_465020` calls |
| Initial list capacity = 8 | HIGH | Third arg `8u` in all three calls |
| compat_list_2 (offset 64) used for compute-to-real linking | HIGH | `sub_484F50` line 279: `sub_465720(*(_QWORD *)(v9 + 64), (unsigned __int64)v6);` where `v9` = compute_75 and `v6` = sm_75 |

### Virtual Pointer (offset 72)

| Claim | Confidence | Evidence |
|---|---|---|
| `virtual_ptr` at offset 72 (index [9] in `_QWORD*`) | HIGH | `sub_484F50` line 264: `v6[4].m128i_i64[1] = (__int64)v7;` (offset 72 on `sm_75`, points to `compute_75`) |
| Compute profile's virtual_ptr is self | HIGH | `sub_484F50` line 265: `v7[9] = v7;` where `v7` is `compute_75` (offset 72 = self) |
| LTO profile's virtual_ptr points to compute profile | HIGH | `sub_484F50` line 277: `*((_QWORD *)v10 + 9) = v9;` where `v10` is `lto_75` and `v9` is `compute_75` |
| Offset 72 is NOT written by the constructor | HIGH | `sub_484DB0` writes offsets 0, 1, 8, 16, 24, 32, 40, 48, 56, 64, 128 only — zeroed by memset prior. Field populated by `sub_484F50` after construction |

### Capability Vectors (offsets 80-127)

| Claim | Confidence | Evidence |
|---|---|---|
| Three XMM128 vectors at offsets 80, 96, 112 | HIGH | `sub_484F50` line 285: `v6[5] = _mm_load_si128((const __m128i *)&xmmword_1D40F10);` (offset 80); line 286: `v6[6] = si128;` (offset 96); line 287: `v6[7] = v13;` (offset 112) |
| sm_75 vec addresses: F10, F20, F30 | HIGH | `sub_484F50` lines 283-287: F20 -> `v6[6]`, F30 -> `v6[7]`, F10 -> `v6[5]` |
| sm_80 vec addresses: F10, F40, F30 | HIGH | `sub_484F50` lines 325-331: F30 -> `v14[7]`, F10 -> `v14[5]`, F40 -> `v14[6]` (via v212) |
| sm_86 vec addresses: F10, F50, F30 | HIGH | `sub_484F50` lines 370-372: F50 -> `v22[6]`, F10 -> `v22[5]`, F30 -> `v22[7]` |
| sm_89 vec addresses: F10, F60, F30 | HIGH | `sub_484F50` lines 499-505: F60 -> `v47[6]`, F30 -> `v47[7]`, F10 -> `v47[5]` |
| sm_90 vec addresses: F10, F40, F30 | HIGH | `sub_484F50` lines 549-554: F30 -> `v56[7]`, F10 -> `v56[5]`, F40 (via `v212`) -> `v56[6]` |
| sm_100 vec addresses: F10, F40, F70 | HIGH | `sub_484F50` lines 640-644: F10 -> `v73[5]`, `v208` (which holds F40) -> `v73[6]`, F70 -> `v73[7]` |
| sm_103 vec addresses: F10, F40, F70 | HIGH | `sub_484F50` lines 922-926: F10 -> `v128`, F40 -> `v123[6]`, F70 -> `v205` (then into vec 2) |
| sm_120/sm_121 vec addresses: F10, F60, F70 | HIGH | `sub_484F50` line 1064: F60 -> `v153` (loaded into sm_120 vec 1) |
| Suffix variants inherit vecs via `_mm_loadu_si128` copy | HIGH | `sub_484F50` line 595-599: `v71 = _mm_loadu_si128(v56 + 6); ... v64[5] = _mm_loadu_si128(v56 + 5); v64[6] = v71; v64[7] = v72;` (sm_90a inherits from sm_90) |
| `sub_470DA0` reads capability via pointer at +16 in its argument buffer | MEDIUM | `sub_470DA0` line 91: `v11 = *(_DWORD **)(a1 + 16);` — but this `a1` is NOT an ArchProfile (callers pass local `__m128i` buffers like `v204`, `v205`, `v387`). The claim "profile offset +16" in the wiki body is misleading: this is a different struct |
| Capability mask values (d=1, g=8, n=2, y=64) | HIGH | `sub_470DA0` lines 96-108: `case 'd': v12 = 1; case 'g': v12 = 8; case 'n': v12 = 2; case 'y': v12 = 64;` |

### Registration & Globals

| Claim | Confidence | Evidence |
|---|---|---|
| Byte 128 zero-init | HIGH | `sub_484DB0` line 58: `*((_BYTE *)v14 + 128) = 0;` |
| Global hash map at `qword_2A5F8D8` | HIGH | `sub_484F50` line 240: `qword_2A5F8D8 = (__int64)sub_4489C0(...)`; also `sub_448E70(qword_2A5F8D8, "sm_75", ...)` line 267 |
| Ordered list `qword_2A5F8E0` (real+virtual, 128-cap) | HIGH | `sub_484DB0` line 46: `v17 = qword_2A5F8E0;` and line 51: `sub_44FE60(qword_2A5F8E0, a3);`. Created at `sub_484F50` line 244: `sub_44FB20(128, ...)` |
| Ordered list `qword_2A5F8E8` (non-virtual only) | HIGH | `sub_484DB0` lines 52-56: `if (!v8) { ... sub_44FE60(qword_2A5F8E8, a3); }`. Created at `sub_484F50` line 245 |
| Resize check via `sub_4504A0` + `sub_44FF90` | HIGH | `sub_484DB0` line 49-50: `if (sub_4504A0(v17)) sub_44FF90(qword_2A5F8E0, (pthread_mutexattr_t *)0x2C);` |
| Resize step = 0x2C (44) | HIGH | Literal `0x2C` passed to `sub_44FF90` lines 50 and 55 |

### Destructor & Teardown

| Claim | Confidence | Evidence |
|---|---|---|
| Destructor at `0x484D00`, reads arch_name from offset 8 | HIGH | `sub_484D00` line 8: `v1 = a1[1];` (offset 8 = arch_name) |
| Destructor removes from hash map qword_2A5F8D8 | HIGH | `sub_484D00` line 9: `sub_449860(qword_2A5F8D8, v1);` |
| Destructor destroys all three list heads | HIGH | `sub_484D00` lines 10-12: `sub_4650A0(a1[6], v1); sub_4650A0(a1[7], v1); sub_4650A0(a1[8], v1);` |
| Destructor frees profile allocation | HIGH | `sub_484D00` line 13: `sub_431000((unsigned __int64)a1, v1);` |
| Teardown at `0x484D40` guarded by `byte_2A5F8D0` | HIGH | `sub_484D40` lines 8-10: `if (byte_2A5F8D0) { byte_2A5F8D0 = 0; ... }` |
| Teardown destroys hash map then both ordered lists | HIGH | `sub_484D40` lines 11-15: `sub_448DA0(qword_2A5F8D8, sub_484D00, 0); sub_448A40(qword_2A5F8D8, sub_484D00); sub_44FB90(qword_2A5F8E0, sub_484D00); sub_44FB90(qword_2A5F8E8, sub_484D00);` |

### Finalization Check Semantics (sub_4709E0)

| Claim | Confidence | Evidence |
|---|---|---|
| `can_finalize_arch_check` at `sub_4709E0` | HIGH | Decompiled file exists with matching address header |
| Error codes 24, 25, 26, 27, 28, 29, 30, 0x1A | HIGH | All return values visible in `sub_4709E0` branches |
| Architecture aliasing: 104->120, 130->107, 101->110 | HIGH | `sub_4709E0` lines 23-46: two identical switches on `a2` and `a3` |
| Same-arch self-check returns 0 | HIGH | `sub_4709E0` line 54: `if (v4 == a3) return result;` with `result = 0` |
| Error path for cross-arch sm_110 | HIGH | `sub_4709E0` lines 71-77: `v11 = v4 == 110 \|\| a3 == 110; if (v11) { if (v9 != 4) return 26; }` |
| Same-decade rule `a3/10 == v4/10` | HIGH | `sub_4709E0` line 95: `if (a3 / 10 != v4 / 10) return 29;` and line 106 |
| sm_100/sm_103 feature_byte_a gate | HIGH | `sub_4709E0` lines 135-145: `if (v4 != 100) {...} if ((unsigned int)(a3 - 102) <= 1) { if ((((unsigned __int8)a1[2] >> 2) & 3) == 1 && (a1[2] & 3) == 1) return 0; return 28; }` |
| `CAN_FINALIZE_DEBUG` env var | HIGH | `sub_4709E0` line 18: `v6 = getenv("CAN_FINALIZE_DEBUG");`. Also `sub_470DA0` line 16 |

### Discrepancies With Other Wiki Pages

| Finding | Confidence | Evidence |
|---|---|---|
| `targets/arch-profiles.md` has offsets 32/40 swapped (says 32=cuda_arch_define, 40=canonical_name) | HIGH | Decompiled constructor definitively shows `v14[4] = a7 (canonical_name)` and `v14[5] = a6 (cuda_arch_define)`. THIS PAGE is correct; the targets page needs fixing |
| `targets/arch-profiles.md` says offset 64 = virtual_profile_ptr, offset 72 = lto_profile_ptr | HIGH | Constructor and `sub_484F50` show offset 64 = list head (compat_list_2) and offset 72 = virtual_ptr. THIS PAGE is correct; the targets page needs fixing |

### Unverified / Approximate Claims

| Claim | Confidence | Evidence |
|---|---|---|
| Constructor size claim "400 B" | MEDIUM | Approximate; exact byte size not re-measured in this pass |
| Database init at `0x484F50` (53,974 B) | MEDIUM | File exists and is 1,330 decompiled lines; exact binary size not re-measured |
| `profile_to_parse_result` at `sub_486DC0` | MEDIUM | File `sub_486DC0_0x486dc0.c` exists but not decompiled in this pass |
| ArchParseResult size = 12 bytes | MEDIUM | Derived from `sub_486DC0` allocation; not re-verified here |
| `dword_1D40660[5]` lookup table values (0-4 meanings) | MEDIUM | Table exists (referenced by `sub_4709E0`); rodata content not re-extracted in this pass |
| Per-vector address table mapping to F10-F70 rodata constants | HIGH | All vector assignments directly observed in `sub_484F50` via `xmmword_1D40F10` through `xmmword_1D40F70` load-immediate calls |
