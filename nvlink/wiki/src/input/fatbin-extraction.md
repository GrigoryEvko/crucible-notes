# Fatbin Extraction

NVIDIA's "fat binary" format bundles multiple device code representations — cubins for different SM architectures, PTX source, NVVM IR, and mercury objects — into a single file. When nvlink receives a `.fatbin` input, it must unwrap the container, locate the member that matches the target architecture, and convert the extracted content into a cubin suitable for linking. The extraction pipeline spans seven functions across three subsystem layers: the top-level dispatch in `sub_42AF40`, the container library (`sub_4BD0A0` through `sub_4CE8C0`), and the host ELF fatbin section scanner `sub_476D90`.

| | |
|---|---|
| **Entry point** | `sub_42AF40` at `0x42AF40` (11,143 bytes / 521 lines) |
| **Container library** | `sub_4BD0A0` at `0x4BD0A0` (extraction pipeline orchestrator) |
| **Content classifier** | `sub_4CE070` at `0x4CE070` (content type detection) |
| **Arch matcher** | `sub_4CE8C0` at `0x4CE8C0` (29,098 bytes / 1,071 lines) |
| **Content extractor** | `sub_4CE670` at `0x4CE670` (matching content extraction) |
| **PTX-to-cubin converter** | `sub_4BD240` at `0x4BD240` (compile PTX/convert extracted content) |
| **Host ELF scanner** | `sub_476D90` at `0x476D90` (searches ELF sections for embedded fatbins) |
| **Caller** | `main()` at `0x409800`, fatbin dispatch branch |
| **Trigger** | Input file's first 4 bytes match `0xBA55ED50` |

## Fatbin Format Structure

A fatbin file has a two-level structure: an outer **wrapper** and one or more inner **containers**, each holding a sequence of **members**.

### Wrapper Header (Magic `0xBA55ED50`)

The outermost structure is the fatbin wrapper. It is identified by the 4-byte magic `0xBA55ED50` (stored as signed int32 `-1168773808` in the decompiled code). The wrapper is the envelope that `main()` recognizes during file-type detection:

```c
// In main(), the fatbin detection check:
if (*(int32_t *)header == -1168773808) {   // 0xBA55ED50
    sub_42AF40(/* fatbin extraction parameters */);
}
```

The wrapper header contains at minimum:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `magic` | `0xBA55ED50` — fatbin wrapper magic, read as little-endian u32 (`int32_t -1168773808`) |
| 4 | 1 | `version` | Wrapper format version byte; required to be `0x01`. `sub_4CE070`'s nested probe folds this byte into the magic via the 48-bit masked compare against `0x1BA55ED50` |
| 5 | 1 | _reserved_ | Forced to zero by the 48-bit-mask compare in `sub_4CE070`; never read as data |
| 6 | 2 | `header_size` | Size of the wrapper header in bytes; read as `uint16_t` in `sub_4CE8C0` (`*(unsigned __int16 *)(content + 6)`) and used as the base offset for the member array |
| 8 | 4 | `data_size` | Total size of all member entries that follow; read as `int32_t` (signed 4-byte) in `sub_4CE8C0` (`*(int *)(content + 8)`). Negative or zero values exit the member loop immediately. **There is no 8-byte `data_size` field** — earlier wiki drafts described an 8-byte field here based on a misread of an unaligned `_QWORD` cast |

After the wrapper header, the data region contains one or more fatbin containers laid out sequentially.

### Container Header (Magic `0x464243BC`)

Each container within the wrapper is identified by the 4-byte magic `0x464243BC` (the ASCII bytes `BC`, `B`, `F` reversed — "FBC" for "Fat Binary Container"). In the decompiled code, the container library validates this magic as a 64-bit value `0x1_464243BC` where the upper byte encodes the container version:

```c
// sub_4CE070: container magic validation
if (*(uint64_t *)container != 0x1464243BCLL)
    return 2;   // error: not a valid container
```

The `0x1` prefix in the 64-bit comparison indicates container version 1. The container header structure:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 4 | `magic` | `0x464243BC` — container magic |
| 4 | 2 | `version` | Container version (typically `1`) |
| 6 | 2 | `header_size` | Size of the container header |
| 8 | 4 | `data_size` | Total size of all member entries that follow |
| 12 | 4 | `reserved` | Padding / flags |

Following the container header (at offset `header_size`), a sequence of member entries begins. Each member entry has its own sub-header describing the member type, target architecture, data offset, and data size.

### Container Member Entry

Each member within a container describes a single code object. The member sub-header layout (reconstructed from `sub_4CE8C0`):

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 2 | `type` | Member type code (see table below) |
| 2 | 2 | `reserved1` | Flags / padding |
| 4 | 4 | `data_offset` | Offset from member start to the payload data |
| 8 | 8 | `data_size` | Size of the payload in bytes |
| 16 | 8 | `flags_reserved` | Additional capability flags |
| 20 | 4 | `extra_offset` | Offset to supplementary data (ISA strings, options) |
| 24 | 4 | `reserved2` | Reserved |
| 28 | 4 | `sm_arch` | Target SM architecture number (e.g., 80, 89, 100) |
| 32 | 8 | `options_offset` | Offset to compiler options string |
| 40 | 8 | `flags` | Capability and feature flags (bit-packed) |
| 48 | 8 | `compressed_size` | If compressed, the size before decompression |

The member type code at offset 0 identifies what kind of device code the member holds:

| Type code | Symbolic name | Description |
|---|---|---|
| 1 | PTX | PTX assembly source text |
| 2 | Cubin (ELF) | Pre-compiled SASS binary (CUDA device ELF) |
| 8 | NVVM IR | NVVM bitcode (LLVM-based IR) |
| 16 | Mercury | Mercury/CapMerc format (sm >= 100 Blackwell+) |
| 32 | Obfuscated PTX | PTX source with obfuscation applied |
| 64 | Compressed PTX | LZ4 or ZSTD compressed PTX content |

The `flags` field at offset 40 contains bit-packed information about the member:

| Bit | Meaning |
|---|---|
| 20 | Debug flag (debug-compiled content) |
| 21 | Position-independent code flag |
| 24 | Compressed content marker |

Members are iterated by advancing through the container data region. The stride from one member to the next is computed as `data_offset + data_size` (the member sub-header plus its payload).

## Extraction Pipeline

The extraction flow is a five-stage pipeline orchestrated by `sub_4BD0A0`, which calls each stage in sequence and bails out on any failure:

```text
sub_4BD0A0 (pipeline orchestrator)
    |
    +-- sub_4CDD60  (1. parse wrapper header)
    +-- sub_4CE3B0  (2. validate wrapper version against runtime)
    +-- sub_4CE2F0  (3. set target SM architecture)
    +-- sub_4CE380  (4. optionally set 64-bit mode flag)
    +-- sub_4CE640  (5. optionally set debug mode)
    +-- sub_4CE070  (6. classify content type)
    +-- sub_4CE8C0  (7. find best matching member for target)
    +-- sub_4CE670  (8. extract matched content)
```

### Stage 1-2: Parse and Validate Wrapper

`sub_4CDD60` parses the fatbin wrapper header and builds an internal context object (allocated on the stack as `v22[8]` in the caller). `sub_4CE3B0` validates the wrapper version against the runtime's supported version range, using the global `dword_2A5B528` as the maximum supported version.

### Stage 3-5: Configure Target Parameters

Three configuration calls set the target parameters on the context:

- `sub_4CE2F0(ctx, sm_arch)` — sets the target SM architecture number (from `dword_2A5F314`)
- `sub_4CE380(ctx)` — optionally sets 64-bit addressing mode (from `byte_2A5F2C0`)
- `sub_4CE640(ctx, 1)` — optionally sets debug content preference (from `dword_2A5F30C == 64`)

These parameters control which member the architecture matcher will select in the next stage.

### Stage 6: Content Type Classification (sub\_4CE070)

`sub_4CE070` examines the raw content pointed to by the context and classifies it into one of four content types. The classification logic checks for multiple format signatures in priority order:

```c
// sub_4CE070 classification logic (simplified)
uint64_t *content = *(uint64_t **)(ctx + 72);

// Check 1: Nested fatbin wrapper?
if ((*content & 0xFFFFFFFFFFFF) == 0x1BA55ED50LL) {
    ctx->content_type = 2;    // nested fatbin -- unwrap recursively
    return;
}

// Check 2: CUDA device ELF (cubin)?
if (is_elf(content) && elf_machine(content) == 190) {
    ctx->content_type = 3;    // cubin
    return;
}

// Check 3: NVVM IR wrapper?
uint32_t first_word = *(uint32_t *)content;
if (first_word == 0x1EE55A01 ||
    (first_word == 0 && *(uint32_t *)(content + 4) == 0x1EE55A01)) {
    ctx->content_type = 1;    // NVVM IR (possibly with alignment padding)
    return;
}

// Check 4: PTX source?
if (is_ptx_header(content)) {
    ctx->content_type = 4;    // PTX assembly
    return;
}

// Unrecognized content
error("unrecognized fatbin content");
return 2;
```

The content type codes written to `ctx + 80`:

| Code | Content type | Description |
|---|---|---|
| 1 | NVVM IR / LTO IR wrapper | First u32 (or u32 at offset 4 after a 4-byte zero pad) is `0x1EE55A01` (LEESA). `sub_4CE8C0` case 1 then delegates to the wrapper directory walker `sub_11E96E0` |
| 2 | Fatbin / nested fatbin | First five bytes are `50 ED 55 BA 01` (BASSED + version `0x01`). `sub_4CE8C0` case 2 walks the container's member directory |
| 3 | Cubin (ELF) | Pre-compiled CUDA device ELF — `\x7fELF` magic plus `e_machine == 190` |
| 4 | PTX text | First non-whitespace, non-comment token is `.version`; `sub_4CE8C0` case 4 forwards the buffer as a NUL-terminated string |

### Stage 7: Architecture Matching (sub\_4CE8C0)

The architecture matcher is the largest and most complex function in the fatbin extraction pipeline at 29 KB / 1,071 decompiled lines. It handles four distinct content types through a top-level `switch` on `ctx->content_type` (offset +80), with the container-member scanning path (content\_type == 2) being the most complex branch. The function iterates all members in the container, comparing each member's SM architecture and flags against the target, and selects the best match according to a five-level priority system.

#### Top-Level Content Type Dispatch

Before entering the member iteration loop, `sub_4CE8C0` dispatches on `content_type`:

```c
// sub_4CE8C0 content type dispatch
switch (ctx->content_type) {    // *(int32_t *)(a1 + 80)
    case 1:   // NVVM IR / LTO IR wrapper -- delegate to sub_11E96E0 (wrapper directory walker)
    case 2:   // Fatbin container -- iterate members (main loop below)
    case 3:   // Cubin (ELF) -- single-object arch validation
    case 4:   // PTX text (optionally obfuscated) -- accept directly
}
```

**Case 3 (cubin/ELF):** For a raw cubin, the function extracts the member's SM architecture from the ELF header. It first checks for Mercury format via `sub_43DA00` (CapMerc magic detection). If Mercury, the type is set to 16 and the size is read via `sub_43DA80`. Otherwise, it checks `sub_43DA40` (AMDGPU-style ELF machine type 65 / `'A'`), and for standard cubins reads the SM number from the `e_flags` field. The function then builds an arch name string (e.g., `"sm_100"`) via `sub_44E530`, parses it via `sub_486FF0`, and compares the member's parsed version against the target's parsed version using `sub_4876A0` (the compatibility checker). If the member's architecture does not match the target, it reads the `.nv.compat` section via `sub_43E610` and calls `sub_4709E0` (finalization compatibility) and `sub_470DA0` (capability mask compatibility) to check cross-architecture translation. If both fail, the function returns 3 (no match). If `sub_470DA0` allows it (via ISA descriptor compatibility), the content is copied, its SM tag is rewritten via `sub_4CD880(content, target_arch)`, and the result is returned as type 2 (cubin).

**Case 4 (PTX source / obfuscated PTX):** The content is accepted directly. If `ctx->obfuscation_key` (offset +136) is nonzero, the function logs `"PTX Obfuscation"` via `sub_467460`. The content size is set to `strlen(content) + 1` and the output type is set to 1 (PTX).

**Case 1 (NVVM IR / LTO IR wrapper):** The function delegates to `sub_11E96E0`, the wrapper directory walker that selects the entry whose embedded arch tag matches the requested SM. It passes the target arch string, request mode, and receives back the matched payload. If the returned content starts with the ELF magic `0x7F454C46`, the output type is set to 2 (cubin); otherwise it is left as 1 (PTX produced by the wrapper). If `sub_11E96E0` finds no match, the function returns 3.

#### Member Iteration Loop (Content Type 2)

For fatbin containers (content\_type == 2), the function scans all members sequentially:

```c
// sub_4CE8C0 member iteration loop (reconstructed)
uint8_t *container = ctx->content;         // *(uint64_t *)(a1 + 72)
uint8_t *base = container + *(uint16_t *)(container + 6);  // skip container header
uint8_t *ptr = base;
uint16_t *best_match = NULL;              // v37

while (ptr - base < *(int32_t *)(container + 8)) {   // container data_size
    uint16_t member_type = *(uint16_t *)ptr;

    // Phase 1: Type gate
    // Phase 2: Version compatibility check
    // Phase 3: Tie-breaking against current best_match

    // Advance to next member
    ptr += *(uint64_t *)(ptr + 8) + *(uint32_t *)(ptr + 4);
}

if (!best_match)
    return 3;  // no match found
```

#### Phase 1: Type Gate

The first filter checks whether the member's type code is acceptable for the current request mode. The function uses a bitmask test on the type code:

```c
if (member_type > 0x20) goto skip;          // types > 32 rejected outright
if (((1LL << member_type) & 0x10106) == 0)  // bitmask: bits 1, 2, 4, 8, 16 set
    goto check_type_32;                     // special check for obfuscated PTX
```

The bitmask `0x10106` corresponds to binary `1_0000_0001_0000_0110`, which passes types 1 (PTX), 2 (cubin), 4 (reserved), 8 (NVVM), and 16 (mercury). Type 32 (obfuscated PTX) has a separate branch that accepts it only when `request_mode == 10`.

After passing the bitmask gate, the function applies a per-request-mode type filter:

| `request_mode` (ctx+12) | Accepted type | Additional constraint |
|---|---|---|
| 2 (default/cubin) | 1 (PTX) | None |
| 7 (mercury) | 16 (mercury) | None |
| 8 (NVVM IR) | 8 (NVVM) | None |
| 9 (debug) | 1 (PTX) | `flags[42] & 0x20 == 0` (debug bit must be clear) |
| 10 (obfuscated) | 32 (obfuscated PTX) | None |
| 13 (relocatable) | 1 (PTX) | `flags & 0x100000 == 0` (PIC flag must be clear) |
| default | any passing bitmask | `flags & 0x100000` check applied |

For request modes not in the table (the `default` label falls through), all types passing the bitmask are accepted, with the PIC flag (`bit 20`) and position-independent flag (`bit 21`) extracted from the member's `flags` field at offset +40.

#### Phase 2: Version String Construction and Compatibility

For each member that passes the type gate, the function builds two arch name strings and parses them into structured records:

```c
// Build version string for the member
uint32_t member_arch = *(uint32_t *)(ptr + 28);
uint64_t member_flags = *(uint64_t *)(ptr + 40);
bool member_pic = (member_flags & 0x100000) != 0;   // bit 20
bool member_pie = (member_flags & 0x200000) != 0;    // bit 21

sub_44E530(buf1, member_arch, 0, member_pic, member_pie);
    // produces e.g. "sm_100a" (if pic), "sm_100f" (if pie), "sm_100" (neither)
member_version = sub_486FF0(buf1, ...);   // parse into structured record

// For PTX members (type 1): upgrade to compute_ form
if (member_type == 1)
    member_version = sub_487220(member_version, member_arch);
        // converts "sm_100" to "compute_100" via sprintf("compute_%2d%s", ...)

// Build version string for the target
sub_44E530(buf2, target_arch, 0, ctx->target_pic, ctx->target_pie);
target_version = sub_486FF0(buf2, ...);
```

The function `sub_44E530` builds a string like `"sm_100"`, `"sm_100a"`, or `"sm_100f"` from the numeric SM value and the PIC/PIE flag bytes. The function `sub_487220` upgrades a native (`sm_`) record to a virtual (`compute_`) record for PTX members, reflecting that PTX is ISA-generic and matches via virtual architecture rules.

The capability flags gate (`ctx->required_caps` at offset +16) is then checked:

```c
uint64_t required_caps = *(uint64_t *)(ctx + 16);
if (required_caps && (required_caps & ~member_flags) != 0)
    goto skip;  // member lacks required capability flags
```

This rejects members that do not have all the capability flag bits required by the link context.

**Type-specific compatibility checks:**

For **mercury (type 16)** and **cubin (type 2 with compressed bit set)** members, when `member_arch != target_arch`, the function extracts the ISA descriptor from the member's `extra_offset` field and checks cross-architecture compatibility:

```c
if (member_arch != target_arch) {
    // Extract ISA descriptor from member
    uint32_t extra_off = *(uint32_t *)(ptr + 20);
    if (extra_off) {
        uint32_t *extra = (uint32_t *)(ptr + extra_off);
        uint32_t isa_data_off = extra[0];
        uint32_t isa_data_len = extra[1];
        size_t name_len = strnlen((char *)(ptr + isa_data_off), isa_data_len + 1);
        // ISA data follows the null-terminated name string
        isa_data_ptr = ptr + isa_data_off + name_len + 1;
        isa_remaining = isa_data_len - name_len;
    }
    sub_43E500(isa_data_ptr, isa_remaining, member_pic, &isa_info);
    compatible = sub_4709E0(&isa_info, member_arch, target_arch) == 0;
}
```

For **standard cubin (type 2, no compressed bit, no mercury)** members, when `request_mode == 5`, the function uses `sub_487570` (a stricter compatibility checker that requires same-decade and same suffix matching). For all other modes, it uses `sub_4876A0` (the general compatibility checker that also handles virtual-to-native matching).

For **NVVM (type 8)** members, the version is also upgraded via `sub_487220` to the virtual (`compute_`) form before comparison, matching PTX behavior.

#### Phase 3: Five-Level Tie-Breaking Priority

When a member passes both the type gate and the compatibility check and a `best_match` already exists, the function applies a cascade of five tie-breaking rules to decide whether to replace the current best. The rules are evaluated in order; the first rule that produces a decisive winner ends the comparison.

```c
best_match_selection(candidate, current_best, request_mode, target_arch):
    // RULE 0 (pre-check): Request mode 10 (obfuscated PTX) resets best on each match
    if request_mode == 10:
        best_match = NULL   // always take the last matching obfuscated PTX
        skip candidate

    // RULE 1: Request mode 12 -- PIE flag preference
    if request_mode == 12:
        if candidate has NO PIE flag AND current_best HAS PIE flag:
            keep current_best (skip candidate)
        if candidate HAS PIE flag AND current_best has NO PIE flag:
            replace with candidate
        // if both same, fall through to version comparison

    // RULE 2: NVVM type (8) preference toggle
    if current_best.type == 8 AND candidate.type != 8:
        if request_mode == 4 (LTO mode):
            replace with candidate   // non-NVVM beats NVVM in LTO mode
        else:
            keep current_best        // NVVM beats non-NVVM otherwise
    if current_best.type != 8 AND candidate.type == 8:
        if request_mode == 4:
            keep current_best
        else:
            replace with candidate

    // RULE 3: Version string comparison
    build_version(current_best) -> ver_best
    build_version(candidate) -> ver_candidate
    // For PTX members, both are upgraded to compute_ form
    if sub_4873A0(ver_best, ver_candidate):     // ver_best > ver_candidate
        keep current_best
    if sub_4873A0(ver_candidate, ver_best):     // ver_candidate > ver_best
        replace with candidate
    // Versions are equal -- fall through to type-based tie-breaking

    // RULE 4: Type hierarchy tie-breaking (when versions are equal)
    // Sub-rule 4a: Request mode 3 -- exact arch match preference
    if request_mode == 3:
        if target_arch == current_best.arch OR target_arch == candidate.arch:
            // prefer the one matching exactly
        else:
            if current_best.type == 1 (PTX): keep current_best
            if candidate.type == 1 (PTX): replace with candidate

    // Sub-rule 4b: Request mode 6 -- mercury preference
    if request_mode == 6:
        if candidate.type == current_best.type:
            if both type 2: fall through to compression check (Rule 4d)
            else: fall through to type hierarchy
        if current_best.type == 16: keep current_best (mercury beats all)
        if candidate.type == 16: replace with candidate

    // Sub-rule 4c: Type hierarchy cascade
    //   cubin (2) > all non-cubin types
    //   mercury (16) > all non-mercury non-cubin types
    //   PTX (1) > remaining types
    if current_best.type == 2:
        if candidate.type != 2: keep current_best
        // both cubin: fall through to compression check (Rule 4d)
    if candidate.type == 2: replace with candidate
    if current_best.type == 16:
        if candidate.type != 16: keep current_best
    if candidate.type == 16: replace with candidate
    if current_best.type == 1:
        if candidate.type != 1: keep current_best
    if candidate.type == 1: replace with candidate

    // Sub-rule 4d: Cubin compression tie-breaking
    // When both are type 2 (cubin) with equal versions:
    if current_best.flags & 0x1000000 == 0 AND candidate.flags & 0x1000000:
        keep current_best   // uncompressed beats compressed
    if current_best.flags & 0x1000000 AND candidate.flags & 0x1000000 == 0:
        replace with candidate
    // Both same compression state: compare cubin internal version via sub_4CD950
    sub_4CD950(&ver_a, current_best)
    sub_4CD950(&ver_b, candidate)
    if ver_b.minor > ver_a.minor:
        replace with candidate

    // RULE 5: Exact architecture match as final tie-breaker
    if target_arch == candidate.arch:
        replace with candidate
    // otherwise keep current_best
```

The function `sub_4873A0` performs version record comparison: it returns true if the first argument's `arch_number` is greater, or if equal, applies a cascade of suffix and flag comparisons (has\_suffix, is\_native, same\_decade\_flag).

The function `sub_4CD950` extracts a cubin's internal ISA version from its `extra_offset` data, parsing the ISA descriptor structure to produce a comparable version tuple.

#### Summary of the Priority Cascade

The five rules, in order of precedence:

| Priority | Rule | Description |
|---|---|---|
| 1 | PIE flag preference | Request mode 12: prefer PIE-flagged members |
| 2 | NVVM type toggle | Type 8 (NVVM) preferred in non-LTO mode; non-NVVM preferred in LTO mode 4 |
| 3 | Version ordering | Higher version string (`sub_4873A0` comparison) wins |
| 4 | Type hierarchy | cubin (2) > mercury (16) > PTX (1); within same type: uncompressed > compressed, higher internal cubin version wins |
| 5 | Exact arch match | `target_arch == member.arch` as final tie-breaker |

#### Cross-Architecture Compatibility

When no member has an exact `sm_arch` match for the target, the matcher evaluates cross-architecture compatibility using two functions:

**`sub_4709E0` (finalization architecture check):** This function determines whether content compiled for one SM architecture can be finalized (translated) for another. It applies an internal architecture remapping before comparison:

| Input SM | Remapped SM |
|---|---|
| 104 | 120 |
| 130 | 107 |
| 101 | 110 |

After remapping, the function reads the ISA descriptor's compatibility class byte (offset +3) and indexes into the `dword_1D40660[5]` dispatch table to determine the compatibility level. The check varies by compatibility type:

- **Type 1 (forward-compatible):** Member arch must be less than target arch. If in the same decade (`arch/10`), compatible. If in different decades, special exceptions apply for sm\_101/sm\_110/sm\_121 cross-family bridges. With compatibility level 4 (broadest), cross-decade is allowed for these special pairs. With level 3, cross-decade is allowed without special pairs.
- **Type 2 (same-decade only):** Member arch must be less than target arch AND `arch/10` must match.
- **Type 3 (exact match with suffix):** Only specific arch pairs are compatible (e.g., sm\_100 to sm\_102/sm\_103, sm\_120 to sm\_121), and only when ISA descriptor bits confirm it.

Return codes: 0 = compatible, 24 = null input, 25 = version too high, 26 = incompatible arch, 27-30 = type-specific incompatibility.

**`sub_470DA0` (capability mask check):** This function verifies that the target architecture's capability bitmask is a superset of the member's required capabilities. It also applies the 104->120, 130->107, 101->110 remapping. When source and target remap to the same value, direct compatibility is granted. When they differ, the function checks the ISA descriptor's `dword_array` (offset +16) against a per-architecture bitmask:

| Remapped arch | Bitmask value |
|---|---|
| 100 (`'d'`) | 1 |
| 103 (`'g'`) | 8 |
| 110 (`'n'`) | 2 |
| 121 (`'y'`) | 64 |

If the member's capability array has the target's bit set, the check passes.

#### Post-Selection Processing

After the loop completes and `best_match` is found, `sub_4CE8C0` performs several post-selection steps:

1. **Set result fields:** `ctx->member_type` (offset +96) is set from `best_match->type`, `ctx->matched_content` (offset +88) is set to `best_match + best_match->data_offset`, and `ctx->content_size` (offset +104) is set from `best_match->data_size`.

2. **Cubin compression handling:** If the matched member is type 2 (cubin) and has the compression flag (bit 24 of flags at offset +40), and the member's arch does not equal the target arch, the function extracts the ISA descriptor and calls `sub_470DA0` to verify cross-arch capability compatibility. If compatible, `ctx->member_type` is kept as 2 and a `needs_arch_rewrite` flag is set. If incompatible (request\_mode == 11), the type is set to 16 (mercury fallback).

3. **Compiler options extraction:** Based on the matched member's type, the function copies the compiler options string from the member's `extra_offset` data into the context:
   - Type 1 (PTX): options string copied to `ctx + 24`
   - Type 16 (mercury): options string copied to `ctx + 40`
   - Type 8 (NVVM): options string copied to `ctx + 56`

4. **Decompression:** If the member's flags indicate compression (`flags & 0xF000` is nonzero), the content is decompressed via `sub_4CD9E0`. For PTX (type 1) and compressed PTX (type 64), a null terminator is appended after decompression and the size is incremented by 1.

5. **Architecture tag rewriting:** If the `needs_arch_rewrite` flag was set (the selected member's arch differs from the target and the match was accepted via cross-architecture compatibility), the decompressed/copied content is patched via `sub_4CD880(content, target_arch)` to rewrite the embedded SM architecture number. The rewritten content is stored at `ctx + 128` and becomes the new `matched_content`.

If no member matched (`best_match == NULL`) after scanning all members, the function sets `ctx->matched_content = NULL` and returns 3 (no match). If the loop was never entered because `container->data_size <= 0`, the same no-match path is taken.

### Stage 8: Content Extraction (sub\_4CE670)

Once the best matching member is identified, `sub_4CE670` extracts its content:

```c
// sub_4CE670 extraction (simplified)
if (ctx->matched_content == NULL)
    return 1;   // nothing matched

*output_ptr   = ctx->matched_content;   // pointer to raw content
*type_out     = ctx->member_type;       // type code (1, 2, 8, 16)
*size_out     = ctx->content_size;      // size in bytes
return 0;
```

The extracted content is a raw pointer into the fatbin data (or into a decompressed copy if the member was compressed). The type code and size are passed back to the caller for dispatch.

## Top-Level Dispatch (sub\_42AF40)

`sub_42AF40` is the entry point called from `main()` for every fatbin input. It orchestrates the entire extract-classify-convert-register flow:

```c
// sub_42AF40 pseudocode (simplified)
void extract_and_process_fatbin(int fatbin_data, ...) {
    void *content;
    int   type;
    size_t size;

    // Step 1: Extract matching member from fatbin
    int rc = sub_4BD0A0(&ctx, &content, &type, &size,
                        fatbin_data, sm_arch, is_64bit,
                        is_debug, max_version);
    check_error(rc, filename);

    if (content == NULL)
        return;   // no matching member found

    // Step 2: Verbose-keep mode -- dump extracted content to disk
    if (byte_2A5F29B) {    // --verbose-keep flag
        const char *ext;
        switch (type) {
            case 1:  ext = "ptx";   break;
            case 8:  ext = "nvvm";  break;
            case 16: ext = "merc";  break;
            default: ext = "cubin"; break;
        }
        printf("nvlink -extract %s -m%d -arch=%s -o %s\n",
               filename, machine_width, arch_string, output_path);
        FILE *fp = fopen(output_path, strstr(output_path, ".ptx") ? "w" : "wb");
        fwrite(content, 1, size, fp);
        fclose(fp);
    }

    // Step 3: Dispatch by member type
    if (type == 8) {
        // NVVM IR -- register for LTO compilation
        handle_nvvm_ir(content, size, filename);
    } else if (type == 1) {
        // PTX -- compile to cubin via embedded ptxas
        void *cubin = sub_4BD240(&output, ctx, content, type, size,
                                 is_64bit, is_debug, extra_options);
        check_error(cubin_rc, filename);
        // Mercury post-link if sm > 89
        if (sm_arch > 0x59 && needs_mercury_transform(output))
            sub_4275C0(&output, filename, sm_arch, ...);
    } else {
        // Cubin or Mercury -- use directly
        void *cubin = sub_4BD240(&output, ctx, content, type, size,
                                 is_64bit, is_debug, extra_options);
        check_error(cubin_rc, filename);
        // Mercury post-link for sm > 99
        if (type == 16 || needs_mercury_transform(output))
            sub_4275C0(&output, filename, sm_arch, ...);
    }

    // Step 4: Register for linking
    if (validate_and_register(link_ctx, output, filename))
        register_module(module_list, filename, output, input_record);
}
```

### Member Type Dispatch

The dispatch after extraction branches on the type code returned by the container library:

**Type 1 (PTX):** The PTX source text is compiled to a cubin via `sub_4BD240`, which delegates to the embedded ptxas compiler. Before compilation, `sub_4BD240` checks for architecture-specific compiler option strings embedded alongside the PTX (`-m64`/`-m32` validation, extra options from `a8`). If compilation fails, it retrieves stderr output from the ptxas subprocess via `sub_4BE3D0` and writes it to `stderr`. The compiled cubin then follows the normal cubin registration path.

**Type 8 (NVVM IR):** The IR blob is registered for LTO (link-time optimization) rather than being compiled immediately. The function parses compiler options embedded in the NVVM metadata string, extracting flags like `-ftz=`, `-prec_div=`, `-prec_sqrt=`, `-fmad=`, `-maxreg`, `-split-compile`, `-generate-line-info`, and `-inline-info`. These are tracked in global state variables with a consensus mechanism — if all modules agree on a value, it is used; if they disagree, a "mixed" state is recorded. The NVVM content is added to the LTO module collection via `sub_4BD1F0`, and `sub_42A680` registers the module for the later LTO compilation phase. Special handling detects `libcudadevrt` by searching for "cudadevrt" in the filename, storing its IR content in a separate output parameter.

**Type 16 (Mercury):** Mercury objects (for sm >= 100 Blackwell and later) are processed similarly to cubins but may require an additional post-link transformation via `sub_4275C0` (the finalizer). The finalizer is called when the `byte_2A5F225` mercury flag is set and the object contains mercury-format sections.

**Type 2 (Cubin, default):** Pre-compiled cubins are extracted and passed directly through `sub_4BD240` for format validation and arena allocation. The cubin is then registered for linking. If the cubin targets a different SM architecture than the link target, the mercury post-link transformation rewrites it.

## PTX-to-Cubin Conversion (sub\_4BD240)

`sub_4BD240` handles the conversion of extracted fatbin content into a cubin in the linker's memory arena. For PTX content (type 1), it performs actual compilation; for cubin/mercury content, it performs a validated copy.

```c
// sub_4BD240 pseudocode (simplified)
int convert_to_cubin(void **output, void **ctx, void *content,
                     int type, size_t size, bool is_64bit,
                     bool is_debug, char *extra_options) {
    if (type == 1) {
        // PTX compilation path
        // Validate architecture-specific options
        if (sub_4CE3E0(ctx, target_arch_string))
            return 5;   // arch mismatch in PTX header

        // Check machine width
        if (is_64bit) {
            if (sub_4CE3E0(ctx, "-m64"))
                return 5;   // width mismatch
        } else {
            if (sub_4CE3E0(ctx, "-m32"))
                return 5;
        }

        // Check extra options compatibility
        if (extra_options && sub_4CE3E0(ctx, extra_options))
            return 5;

        // Compile PTX to cubin via embedded ptxas
        void *compiled;
        size_t compiled_size;
        if (sub_4BE350(ctx, &compiled, &compiled_size))
            return error_with_stderr();

        // Copy result into arena
        *output = arena_alloc(compiled_size);
        memcpy(*output, compiled, compiled_size);
        sub_4BE400(ctx);   // release compilation context
        return 0;
    }

    // Non-PTX: validated copy into arena
    *output = arena_alloc(size);
    memcpy(*output, content, size);
    sub_4BE400(ctx);
    return 0;
}
```

The return code 5 indicates an architecture or option mismatch, causing the member to be skipped without a fatal error.

## Host ELF Fatbin Extraction (sub\_476D90)

When nvlink encounters a host ELF object file (with `e_machine != 190`), it may need to extract embedded fatbin data from special ELF sections. Host-compiled CUDA code embeds device fatbins in the host object via specific section names. `sub_476D90` searches for these sections:

```c
// sub_476D90 pseudocode
void *extract_fatbin_from_host_elf(elf_handle elf, const char *filename) {
    if (!elf)
        error("no host ELF for fatbin extraction");

    // Search three known section names in priority order
    if (!find_section(elf, ".nvFatBinSegment")) {
        // Primary section not found, try alternatives
        if (!find_section(elf, "__nv_relfatbin")) {
            if (!find_section(elf, ".nv_fatbin"))
                error("cannot find fatbin section in %s", filename);
            return NULL;
        }
    } else {
        return NULL;   // .nvFatBinSegment found but handled differently
    }

    // Found __nv_relfatbin -- extract the fatbin data
    void *section_data = get_section_data(elf, "__nv_relfatbin");
    if (!section_data || *(uint32_t *)section_data != 0xBA55ED50)
        error("cannot find fatbin section in %s", filename);

    // Allocate and copy: header (16 bytes) + data_size
    size_t total = *(uint64_t *)(section_data + 8) + 16;
    void *copy = arena_alloc(total);
    memcpy(copy, section_data, total);
    return copy;
}
```

The three section names searched, in priority order:

| Section name | Origin | Description |
|---|---|---|
| `.nvFatBinSegment` | CUDA runtime | The standard fatbin segment created by `nvcc` for runtime registration |
| `__nv_relfatbin` | Relocatable compile | Fatbin data in relocatable device code objects |
| `.nv_fatbin` | Alternative layout | Alternative section name used in some compilation modes |

The extracted fatbin data (validated by the `0xBA55ED50` wrapper magic) is then processed through the normal fatbin extraction pipeline starting at `sub_42AF40`.

## NVVM IR Option Consensus Tracking

When multiple fatbin inputs contribute NVVM IR modules for LTO, `sub_42AF40` tracks compiler options across all modules to establish a consensus. For each tracked option, a four-state machine determines the final value:

| State | Value | Meaning |
|---|---|---|
| 0 | Not yet seen | No module has provided this option |
| 1 | Present without value | At least one module lacks this option |
| 2 | Present with value | All modules so far agree on the same value |
| 3 | Mixed presence | Some modules have the option, some do not |
| 4 | Conflicting values | Modules disagree on the value |

The tracked options and their global state variables:

| Option | State variable | Value variable |
|---|---|---|
| `-ftz=` | `dword_2A5F270` | `dword_2A5F274` |
| `-prec_div=` | `dword_2A5F26C` | `dword_2A5B524` |
| `-prec_sqrt=` | `dword_2A5F268` | `dword_2A5B520` |
| `-fmad=` | `dword_2A5F264` | `dword_2A5B51C` |
| `-maxreg` | `dword_2A5F250` | `dword_2A5F254` |
| `-split-compile` | `dword_2A5F260` | `dword_2A5B518` |
| `-generate-line-info` | `dword_2A5F248` | `byte_2A5F24C` (bool) |
| `-inline-info` | `dword_2A5F240` | `byte_2A5F244` (bool) |

This consensus is used during the LTO compilation phase to pass consistent flags to libnvvm.

## FatBinary Driver Library

For some fatbin operations, nvlink dynamically loads NVIDIA's fatbin driver library at runtime. The loading sequence (in the `0x15C0000` region):

| Function | Address | Description |
|---|---|---|
| `sub_15C3FD0` | `0x15C3FD0` | Searches `LD_LIBRARY_PATH` for the driver shared object |
| `sub_15C41C0` | `0x15C41C0` | Uses `glob("libfat*Driver.so")` to find the library file |
| `sub_15C41E0` | `0x15C41E0` | Calls `dlopen`/`dlsym` to load the `fatBinaryDriver` symbol |

The glob pattern `libfat*Driver.so` matches both `libfatBinaryDriver.so` and potential variant names. The loaded `fatBinaryDriver` function provides the runtime's canonical fatbin parsing implementation, which the container library functions (`sub_4CDD60` through `sub_4CE670`) wrap with error handling and architecture selection logic.

## Error Handling and Return Codes

The extraction pipeline uses integer return codes at each stage:

| Code | Meaning | Action |
|---|---|---|
| 0 | Success | Continue to next stage |
| 1 | NULL input / empty content | Skip (not an error) |
| 2 | Invalid magic / format error | Fatal: `"fatbin wrong format?"` |
| 3 | No matching architecture | Skip this fatbin member |
| 5 | Internal error / extraction failure | Fatal error via `sub_4BE400` cleanup |
| 7 | Architecture match returned "no match" (rc=3) | Passed to caller as-is, may be suppressed |
| 8 | PTX compilation failure (no stderr) | Fatal compilation error |

Return code 7 receives special treatment in `sub_42AF40`: when the `a5` flag (bit 0) is set, a return of 7 from `sub_4BD0A0` suppresses the error check, allowing the caller to handle a "no matching architecture" condition gracefully (e.g., when scanning an archive where not all members need to match).

## Diagnostic Strings

| String | Context |
|---|---|
| `"fatbin wrong format?"` | main(): fatbin magic validation failed |
| `"nvlink -extract %s -m%d -arch=%s -o %s"` | sub_42AF40: verbose-keep mode extraction trace |
| `"nvlink -lto-add-module %s.nvvm"` | sub_42AF40: verbose-keep mode LTO module trace |
| `"found IR for libcudadevrt"` | sub_42AF40: detected libcudadevrt NVVM IR |
| `"don't uplift %s"` | sub_42AF40: mercury uplift suppressed for this object |
| `"unrecognized fatbin content"` | sub_4CE070: content type classification failure |
| `"PTX Obfuscation"` | sub_4CE8C0: obfuscated PTX member detected |

## Cross-References

- [Input File Loop](../pipeline/input-loop.md) — how fatbin files are dispatched from `main()`'s file-type detection loop
- [File Type Detection](file-type-detection.md) — the 56-byte header probe that recognizes the `0xBA55ED50` fatbin magic
- [168-Byte Input Container](container-struct.md) — field-offset table, lifecycle, and writer/reader pairing for the opaque struct used by `sub_4BD0A0`
- [Cubin Loading](cubin-loading.md) — cubins extracted from fatbin containers follow the same validation path documented here
- [PTX Input & JIT](ptx-input.md) — how extracted PTX members (type 1) are compiled via embedded ptxas
- [NVVM IR / LTO IR Input](nvvm-ir-input.md) — how extracted NVVM IR members (type 8) are registered for LTO
- [Host ELF Embedding](host-elf.md) — how `sub_476D90` extracts embedded fatbins from host ELF `.nvFatBinSegment` / `__nv_relfatbin` sections
- [Archive Processing](archives.md) — fatbin extraction within archive members
- [Compatibility](../targets/compatibility.md) — the cross-architecture matching functions `sub_4709E0` and `sub_470DA0` used during architecture selection
- [Architecture Profiles](../targets/arch-profiles.md) — how SM numbers map to architecture capabilities checked during member matching
- [Mercury Overview](../mercury/overview.md) — Mercury member type (16) handling and the FNLZR post-link transform
- [LTO Overview](../lto/overview.md) — the batch LTO compilation phase that consumes registered IR modules from fatbin extraction
- [ELF Device Format](../elf/device-elf-format.md) — cubin ELF format that fatbin members contain
- **ptxas wiki**: [Output Phase](../../ptxas/pipeline/output.html) — ptxas cubin output format (the producer of the cubin/mercury members that fatbin containers hold)
- **ptxas wiki**: [Capsule Mercury](../../ptxas/codegen/capmerc.html) — capsule mercury format for fatbin type-16 members
