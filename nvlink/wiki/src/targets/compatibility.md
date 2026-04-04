# Compatibility Checking

nvlink enforces architecture compatibility at three distinct points: during input file validation, during finalization dispatch, and during capability mask evaluation. These three checks form a layered system -- the input validator decides whether a cubin can enter the link, the finalization checker decides whether a finalized object can be re-finalized for a different target, and the capability mask checker gates individual feature support within a finalization context. All three share a common architecture remapping table and a common "same-decade" rule for family grouping.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_4878A0` | `arch_string_match` | 328 B | Core compatibility checker: parses two arch strings, compares profiles |
| `sub_4876A0` | `arch_compat_check` | 2,115 B | Companion checker: virtual-vs-native and `a`-variant exact-match logic |
| `sub_4709E0` | `can_finalize_arch_check` | 2,609 B | Finalization compatibility: 5-level dispatch table, error codes 0/24--30 |
| `sub_470DA0` | `can_finalize_capability_mask` | 2,074 B | Capability bitmask compatibility within a finalization context |
| `sub_426570` | `validate_arch_and_add` | 7,427 B | Input file validation: arch match, toolkit version constraints, mode selection |
| `sub_486FF0` | `arch_parse_name_to_number` | 2,665 B | Parses `sm_%d%c` / `compute_%d%c` / `sass_%d%c` into structured record |
| `sub_487420` | `arch_canonicalize` | ~600 B | Canonicalizes arch string for profile hash lookup |
| `sub_465450` | `profile_family_match` | ~300 B | Compares two profiles via family linked list |

## Architecture Record Layout

`sub_486FF0` parses an architecture name string into a structured record. The format strings it recognizes are `sm_%2d%s`, `compute_%2d%s`, and `sass_%2d%s`. The parsed record contains:

| Offset | Size | Field | Meaning |
|---|---|---|---|
| +0 | 4 bytes | `arch_number` | SM version number (e.g. 100 for sm_100) |
| +4 | 1 byte | `has_suffix` | Nonzero if an `a` or `f` suffix was present |
| +5 | 1 byte | (padding) | |
| +6 | 1 byte | `is_virtual` | 1 if prefix was `compute_` |
| +7 | 1 byte | `is_native` | 1 if prefix was `sm_` or `sass_` |
| +8 | 1 byte | `same_decade_flag` | Controls whether same-decade rule applies |

The `arch_number` for multi-digit architectures maps directly to the SM value: sm_75 = 75, sm_100 = 100, sm_121 = 121.

## Core Compatibility Check (sub_4878A0)

`sub_4878A0` is the primary compatibility checker invoked during cubin loading. It takes two architecture name strings (the input cubin's architecture and the link target `--arch` value) and returns 1 (compatible) or 0 (incompatible).

### Algorithm

```
arch_string_match(input_arch_str, target_arch_str):
    record_a = arch_parse_name_to_number(input_arch_str)
    record_b = arch_parse_name_to_number(target_arch_str)
    if either parse fails:
        return 0

    // Both virtual: reject (cannot link two virtual targets)
    if record_a.is_virtual AND record_b.is_virtual:
        return 0

    // Canonicalize both arch strings and look up profiles in the hash map
    profile_a = profile_lookup(canonicalize(record_a))
    profile_b = profile_lookup(canonicalize(record_b))

    // Case 1: No suffix (base arch, e.g. sm_100 vs sm_100)
    if !record_a.has_suffix:
        return profile_family_match(profile_a.family_list, profile_b)

    // Case 2: Native 'a' variant -- requires exact arch match
    if record_a.is_native:
        return record_a.arch_number == record_b.arch_number

    // Case 3: Same-decade rule
    if !record_a.same_decade_flag:
        return record_b.arch_number >= record_a.arch_number

    // Case 4: Same-decade with cross-family exceptions
    // Special handling for SM 101 and SM 110
    if arch_a == 101 or arch_b == 101 or arch_a == 110 or arch_b == 110:
        if arch_b in {101, 110}:
            return arch_a != 101 AND arch_a != 110  // inverted logic
        return 0  // arch_a in {101, 110} but arch_b is not

    // General same-decade: arch_b >= arch_a AND same family
    if arch_b >= arch_a:
        return arch_a / 10 == arch_b / 10
    return 0
```

### The Same-Decade Rule

The same-decade rule is the fundamental family-grouping mechanism: two architectures are in the same family if their SM numbers, divided by 10, are equal. This produces the following families:

| SM / 10 | Family | Members |
|---|---|---|
| 7 | Turing | sm_75 |
| 8 | Ampere | sm_80, sm_86, sm_87, sm_88, sm_89 |
| 9 | Hopper | sm_90, sm_90a |
| 10 | Blackwell (100-series) | sm_100, sm_103 |
| 11 | Blackwell (110-series) | sm_110 |
| 12 | Blackwell (120-series) | sm_120, sm_121 |

The intent is that code compiled for a lower SM within a family can run on a higher SM in the same family (e.g. sm_80 code on sm_89 hardware), while code cannot cross family boundaries (e.g. sm_89 code on sm_90 hardware). The division is integer division, so sm_100 and sm_103 share decade 10, while sm_110 is in decade 11.

### SM 101 / SM 110 Cross-Mapping Exception

SM 101 and SM 110 receive special handling. In the decompiled code, when either the source or target is 101 or 110, the normal same-decade comparison is bypassed. If the target is 101 or 110, the check returns true only if the source is the other member of the pair (110 or 101 respectively). This implements a bidirectional compatibility bridge between these two architectures that would otherwise be in different decades. SM 101 is an internal/alternate designation that maps to the sm_10x family from the finalization perspective (see the 101->110 remapping in `sub_4709E0`).

### Virtual vs. Native Comparison

When neither record has the `has_suffix` flag set and the record is not `is_native`, the check falls through to `profile_family_match` (`sub_465450`). This function traverses the family linked list stored at `profile+56` in the profile database, checking whether the target profile appears in the source profile's family chain. This handles `compute_XX` to `sm_XX` compatibility -- a `compute_75` target is compatible with any `sm_XX` where XX is in the same family as 75.

### The `a` Variant: Exact Match

When the source record has `is_native` set and the `a` flag is detected (e.g. `sm_90a`, `sm_100a`), the check requires an exact numeric match: `arch_a == arch_b`. The `a` suffix denotes architecture-specific features that are not guaranteed to be forward-compatible within the same decade. An `sm_90a` cubin can only link against an `sm_90a` or `sm_90` target, not against `sm_91` (hypothetical) or any other SM in the same decade.

## Companion Check (sub_4876A0)

`sub_4876A0` is a closely related function that implements the same logic with slightly different entry conditions. It is used from code paths that already have parsed architecture records rather than raw strings. The function checks:

1. Both inputs must be non-null and the target must not have `is_virtual` set (byte at `a2+offset6`).
2. If the source is virtual (`a1[6]` set), a prefix-based check applies with different rules for suffix and native flags.
3. If the source is not virtual, the same `arch_number / 10` family check applies.
4. The SM 101/110 cross-mapping exception is replicated identically.

## Finalization Compatibility (sub_4709E0)

`sub_4709E0` determines whether a previously finalized object can be re-finalized for a different target architecture. This is used by the JIT / opportunistic finalization pipeline when nvlink receives a finalized cubin and needs to decide if it can produce output for the requested `--arch`. The function returns an integer error code rather than a boolean.

### Architecture Remapping

Before any comparison, both the source and target architecture numbers are remapped through a fixed table:

| Input | Output | Meaning |
|---|---|---|
| 104 | 120 | SM 104 (internal) maps to SM 120 family |
| 130 | 107 | SM 130 (internal) maps to SM 107 (i.e. SM 100 family, decade 10) |
| 101 | 110 | SM 101 maps to SM 110 |

This remapping collapses internal/experimental architecture numbers into their canonical families before the compatibility check runs. After remapping, the check proceeds with the remapped values.

### Return Codes

| Code | Meaning |
|---|---|
| 0 | Compatible -- finalization can proceed |
| 24 | Null input -- the compatibility record pointer `a1` is NULL |
| 25 | Version too high -- the record's version field (`a1` word at offset +6) exceeds `0x101` |
| 26 | Incompatible architecture -- family mismatch or disallowed cross-family link |
| 27 | Type 4 error -- finalization class 4 incompatibility |
| 28 | Type 3 error -- finalization class 3 incompatibility |
| 29 | Type 2 error -- finalization class 2, source >= target in same decade |
| 30 | Unknown type -- finalization class byte not in range 0--4 |

### Finalization Class Dispatch

The byte at `a1[3]` encodes the "finalization class" (0--4). This byte indexes into the dispatch table at `dword_1D40660`, which returns one of 5 compatibility levels:

| `a1[3]` | `dword_1D40660[a1[3]]` | Compatibility Level | Semantics |
|---|---|---|---|
| 0 | 0 | Exact | No variant allowed, no cross-family, no SM 110 bridging |
| 1 | 1 | Same-decade, same direction | Source < target within same decade |
| 2 | 2 | Same-decade, bidirectional | Source and target in same decade (class 2 error if source >= target) |
| 3 | 3 | Cross-family allowed | SM 110/121 cross-decade bridging permitted |
| 4 | 4 | Broadest | SM 110 bridging plus additional cross-family tolerance |

The `a1[4]` byte is the "variant flag" (corresponding to the `a` suffix). When the variant flag is set:

- **Level 0**: The variant flag is incompatible, returns 26.
- **Level 1**: The variant flag is incompatible with same-decade matching, returns 26.
- **Levels 2--3**: Variant flag permitted only if level >= 2, returns 0 if so, else returns 26 (`0x1A`).

### SM 110 Special Handling

When either the remapped source or target is 110, and the finalization class is not level 4, the check returns 26 (incompatible). Only level 4 permits SM 110 as a finalization target or source. Similarly, SM 121 receives special treatment at level 3+, allowing cross-decade links between 120 and 121.

### Class 3 Specific Rules: SM 100 / SM 120 / SM 121

At finalization class 3 (type byte `*a1 == 3`, return code 28), the variant flag and level interact:

- `sm_100` (remapped `d` = 100) with target `sm_102` or `sm_103`: allowed only if a specific bit pattern in `a1[2]` matches (bits [3:2] == 1 and bits [1:0] == 1).
- `sm_120` with target `sm_121`: allowed unconditionally at level 2+.
- All other class-3 combinations return 28.

## Capability Mask Check (sub_470DA0)

`sub_470DA0` is the third layer of compatibility checking. Where `sub_4709E0` returns an error code, this function returns a boolean and additionally gates on a per-architecture capability bitmask.

### Architecture Remapping

The same 104->120, 130->107, 101->110 remapping applies. Both source and target are remapped before comparison.

### Algorithm

```
can_finalize_capability_mask(record, source_arch, target_arch, flag):
    // Remap both architectures
    source = remap(source_arch)
    target = remap(target_arch)

    // Direct match: source == target (post-remap)
    result = flag & (source == target)
    if result: return 1

    // Capability mask check
    if record+24 (version word) == 0: return 0
    mask_ptr = *(record + 16)
    if mask_ptr == NULL: return 0

    // Map target arch to bitmask value
    switch (target):
        case 100 ('d'): bit = 1
        case 103 ('g'): bit = 8
        case 110 ('n'): bit = 2
        case 121 ('y'): bit = 64
        default: return 0

    // Check if the source's capability mask includes the target's bit
    if (bit & *mask_ptr) != bit: return 0
    return result
```

### Capability Bitmask Values

The capability bitmask encodes which target architectures a given source can be finalized for:

| Target Arch | ASCII | Bitmask Value | Binary |
|---|---|---|---|
| sm_100 | `'d'` (100) | 1 | `00000001` |
| sm_110 | `'n'` (110) | 2 | `00000010` |
| sm_103 | `'g'` (103) | 8 | `00001000` |
| sm_121 | `'y'` (121) | 64 | `01000000` |

A source architecture's capability mask at `*(record + 16)` is a bitfield indicating which target architectures it supports. For example, a mask of `0x09` (bits 0 and 3) would indicate compatibility with sm_100 and sm_103.

### Debug Environment Variable

Both `sub_4709E0` and `sub_470DA0` check for the environment variable `CAN_FINALIZE_DEBUG`. When set, its value is parsed as an integer via `strtol`. The exact effect is not fully reversed, but the presence of this variable enables diagnostic output from the finalization compatibility pipeline, likely printing the remapped architectures and compatibility decisions to stderr.

## Input File Validation (sub_426570)

`sub_426570` is the top-level validation function called from `main()` for every cubin that enters the link. It orchestrates architecture matching, toolkit version enforcement, and link mode selection.

### Validation Sequence

1. **Word size check**: The cubin's ELF class (32-bit vs 64-bit) must match the `--machine` setting (`dword_2A5F30C`, either 32 or 64).

2. **ELF type rejection**: `ET_DYN` (shared library, `e_type == 2`) cubins are rejected unconditionally.

3. **ELF class byte check**: For 32-bit links, the ELF class byte at `ehdr+8` must be 7 (legacy CUDA format) or 8 (sm > 72). Other values trigger an error.

4. **SM version extraction**: The SM version number is extracted from `e_flags` in a class-dependent manner:
   - **OSABI != 0x41 (legacy)**: `sm = e_flags & 0xFF` (low byte)
   - **OSABI == 0x41 (Mercury)**: `sm = (e_flags >> 8) & 0xFF` (second byte)

5. **Arch string construction**: The SM number and the ABI suffix flag (`sub_43E6F0`) are formatted into a 12-byte buffer:
   ```
   snprintf(buf, 12, "sm_%d%c", sm_version, has_abi ? 'a' : '\0')
   ```
   Or, if the cubin is virtual (`byte_2A5F2C1` set):
   ```
   snprintf(buf, 12, "compute_%d%c", sm_version, has_abi ? 'a' : '\0')
   ```

6. **Primary arch match**: `sub_4878A0` compares the constructed string against `qword_2A5F318` (the `--arch` value). If it returns 1, the cubin is accepted.

7. **Fallback: .nv.compat match**: If the primary match fails and `byte_2A5F221` (SASS mode flag) is set, the fallback path reads the `.nv.compat` section via `sub_43E610` and calls `sub_4709E0` to test finalization compatibility. If `sub_4709E0` returns 0 (compatible), the cubin is accepted.

8. **Final failure**: If both checks fail, the error `"SM Arch ('%s') not found in '%s'"` is emitted.

### Toolkit Version Constraints

After architecture matching, `sub_426570` enforces toolkit version requirements:

| Condition | Constraint | Error |
|---|---|---|
| General | Current toolkit version (`sub_468560()`) must be >= cubin's toolkit version | Toolkit too old for input cubin |
| SM 50 | Cubin toolkit version must be > 64 (`0x40`) | SM 50 requires CUDA 6.5+ cubins |
| SM 90 | Cubin toolkit version must be > 119 (`0x77`) | SM 90 requires CUDA 12.0+ cubins |

The SM 50 and SM 90 checks enforce minimum CUDA toolkit versions for specific architectures. SM 50 (Maxwell) requires cubins compiled with at least CUDA 6.5 (toolkit version 65), and SM 90 (Hopper) requires CUDA 12.0 (toolkit version 120). These prevent linking objects compiled with toolkit versions that predate the architecture's introduction.

### tcgen05 Cross-Version Incompatibility

A separate cross-version check at address `0x1D39330` enforces that objects using tcgen05 tensor core instructions (Blackwell-era) compiled with CUDA 12.x cannot be linked with objects from CUDA 13.0+. The tcgen05 instruction encoding changed between the 12.x preview support and the 13.0 production release, making the two incompatible at the binary level. This check is part of the broader version validation pipeline and fires before the architecture compatibility check runs.

### Mercury / EWP Mode Detection

When the cubin's `e_type` is `0xFF00` (Mercury executable with payload, "EWP"), the global flag `byte_2A5F229` is set. Once set, all subsequent cubins must be from the same toolkit version -- the error `"linking with -ewp objects requires using current toolkit"` fires if the toolkit version of any subsequent input does not match `sub_468560()`.

When `e_type` is not `0xFF00` and `byte_2A5F229` has not been set yet, the return value depends on the error list state -- the function returns true (continue) only if no errors have been recorded.

## Compatibility Matrix

The following matrix summarizes the compatibility rules for SM 100-series architectures:

| Source | sm_100 | sm_103 | sm_110 | sm_120 | sm_121 |
|---|---|---|---|---|---|
| **sm_100** | yes | yes (decade 10) | special | no | no |
| **sm_103** | no | yes | special | no | no |
| **sm_110** | special | special | yes | no | no |
| **sm_120** | no | no | no | yes | yes (decade 12) |
| **sm_121** | no | no | no | no | yes |

"special" = requires finalization class 4 or explicit 101/110 cross-mapping.

For the `a` variants (sm_100a, sm_120a, etc.), all entries become exact-match only: sm_100a is compatible only with sm_100 or sm_100a.

## Error Messages

| Error | Trigger | Source |
|---|---|---|
| `"SM Arch ('%s') not found in '%s'"` | Cubin arch does not match `--arch` and `.nv.compat` fallback fails | `sub_426570` |
| `"linking with -ewp objects requires using current toolkit"` | EWP cubin from different toolkit version | `sub_426570` |
| `"specified arch exceeds buffer length"` | `compute_%d%c` format exceeds 12-byte buffer | `sub_426570` |
| Return code 24 | NULL compatibility record | `sub_4709E0` |
| Return code 25 | Record version > 0x101 | `sub_4709E0` |
| Return code 26 | Architecture family mismatch | `sub_4709E0` |
| Return code 27 | Finalization class 4 incompatibility | `sub_4709E0` |
| Return code 28 | Finalization class 3 incompatibility | `sub_4709E0` |
| Return code 29 | Finalization class 2 incompatibility | `sub_4709E0` |
| Return code 30 | Unknown finalization class | `sub_4709E0` |

## Globals

| Address | Name | Type | Role |
|---|---|---|---|
| `qword_2A5F318` | `g_target_arch` | `char *` | The `--arch` value (e.g. `"sm_100"`) |
| `dword_2A5F314` | `g_sm_version` | `int` | Numeric SM version (e.g. 100) |
| `dword_2A5F30C` | `g_machine` | `int` | Word size: 32 or 64 |
| `byte_2A5F221` | `g_sass_mode` | `bool` | Set when any SASS cubin enters the link |
| `byte_2A5F229` | `g_ewp_mode` | `bool` | Set when an EWP (Mercury executable) cubin enters |
| `byte_2A5F2C1` | `g_virtual_mode` | `bool` | Set when linking virtual architectures |
| `qword_2A5F8D8` | `g_profile_hashmap` | `void *` | Hash map of architecture profiles (name -> profile struct) |
| `dword_1D40660` | `g_finalize_dispatch` | `int[5]` | Finalization class -> compatibility level mapping |

## Cross-References

- [Architecture Profiles](arch-profiles.md) -- the profile database initialization that populates `qword_2A5F8D8`
- [Cubin Loading](../input/cubin-loading.md) -- the full cubin validation path that calls `sub_4878A0` and `sub_426570`
- [Finalization Phase](../pipeline/finalize.md) -- the linker finalization phase that triggers `sub_4709E0`
- [Versions](../versions.md) -- toolkit version numbering, PTX ISA version, and the SM architecture table
- [Mercury / FNLZR](../mercury/fnlzr.md) -- the post-link finalizer that uses capability mask checks
- [SM 100--121 Targets](sm100-blackwell.md) -- per-architecture details for the Blackwell family
