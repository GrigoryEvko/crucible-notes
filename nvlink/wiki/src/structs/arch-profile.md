# Architecture Profile

The `ArchProfile` struct is a 136-byte heap-allocated descriptor that encodes everything nvlink needs to know about a single GPU architecture target. Each recognized architecture (e.g. `sm_100`) produces three profile instances -- a real profile (`sm_`), a virtual profile (`compute_`), and an LTO profile (`lto_`) -- all stored in a global hash map keyed by name string. The struct is created by `sub_484DB0` and consumed throughout the linking, finalization, and output pipelines.

This page documents the byte-level layout derived from the constructor (`sub_484DB0`), the database initializer (`sub_484F50`), and the two finalization compatibility checkers (`sub_4709E0`, `sub_470DA0`).

## Constructor: sub_484DB0

```
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

```
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

- **Vec 0** is identical for all architectures -- a universal base capability set.
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

All profiles are zero-initialized, so this check always passes in CUDA 13.0. The `0x101` threshold (257 decimal) suggests this was designed as a forward-compatibility guard -- if a profile's version exceeds the linker's known maximum, finalization is rejected.

## Linked List Heads (Offsets 48-64)

Each of the three linked list pointers at offsets 48, 56, and 64 is a full hash-set object created by `sub_465020` with string hashing and comparison functions. They are **not** simple singly-linked lists but hash-based sets that support O(1) membership testing. The `sub_465720` (`list_append`) function used to populate them is the same hash-set insertion function used throughout nvlink.

### compat_list_0 (Offset 48): Cross-Variant Links

For base architectures, this list connects the real profile to its virtual counterpart and self:

```
sm_100.compat_list_0 -> { compute_100, sm_100 }
compute_100.compat_list_0 -> { sm_100 }
```

For suffix variants, the base arch is also linked:

```
sm_100a.compat_list_0 -> { compute_100a, sm_100a, sm_100 }
sm_100f.compat_list_0 -> { compute_100f, sm_100f, sm_100 }
```

### compat_list_1 (Offset 56): Same-Generation Family

Links all architectures within the same generation. For Ampere:

```
sm_80.compat_list_1 -> { sm_80, sm_86, sm_87, sm_88, sm_89 }
```

The sm_89 (Ada) profile is appended to sm_80's family list despite being classified as "Ada" rather than "Ampere" -- this reflects hardware backward compatibility.

For Blackwell, both intra-family and cross-family links exist:

```
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

```
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

- [Architecture Profiles (overview)](../targets/arch-profiles.md) -- database initialization sequence, complete architecture table, name parsing
- [Compatibility](../targets/compatibility.md) -- finalization compatibility rules
- [Finalize](../pipeline/finalize.md) -- how profiles flow through the finalization pipeline
- [CLI Options](../pipeline/cli-options.md) -- `--arch` option triggers profile lookup
