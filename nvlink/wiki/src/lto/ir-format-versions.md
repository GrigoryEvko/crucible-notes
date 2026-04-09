# LTO Profile Tags & Architecture Mapping

When nvcc compiles device code with `-dlto` (device link-time optimization), it emits NVVM IR bitcode instead of SASS machine code. This bitcode is tagged with an `lto_` profile name that encodes the target architecture. At link time, nvlink resolves each `lto_` tag to its corresponding `compute_` virtual profile, loads libNVVM, and compiles the IR down to SASS for the real `sm_` target. The `lto_` profile is the bridge between the architecture-independent IR and the architecture-specific code generator -- it determines which IR is compatible with which final target and controls how nvlink routes the compilation.

nvlink v13.0.88 registers 23 `lto_` profile variants in `sub_484F50` (`ArchProfileDB::init`, ~1,330 decompiled lines at `0x484F50`). Each `lto_` profile is created alongside the corresponding `sm_` (real) and `compute_` (virtual) profiles during a one-time initialization guarded by `byte_2A5F8D0`. The full function registers 69 profiles total (23 triplets of sm_/compute_/lto_).

> **Scope note.** This page documents **`lto_` profile tags** -- the architecture identifiers attached to LTO bitcode sections. For the NVVM IR wire format (magic number, header probe, detection logic), see [NVVM IR / LTO IR Input](../input/nvvm-ir-input.md). For the profile struct byte-level layout, see [Architecture Profiles](../targets/arch-profiles.md) and [Architecture Profile Struct](../structs/arch-profile.md). For compatibility checking rules, see [Compatibility Checking](../targets/compatibility.md).

## NVVM IR Wire Format (Summary)

The NVVM IR bitcode format used by LTO is identified by a 4-byte magic number:

| Field | Value |
|---|---|
| Magic | `0x1EE55A01` (little-endian on disk: `01 5A E5 1E`) |
| Padded variant | 4 zero bytes + `0x1EE55A01` at byte offset 4 (fatbin-wrapped form) |
| Extensions | `.nvvm`, `.ltoir` (content format identical) |
| Rejected | `.bc` (raw LLVM bitcode) -- fatal: `"should never see bc files"` |

Detection occurs at two layers: the outer `main()` dispatch classifies by file extension, while the inner embedded-ptxas engine (`sub_4CE070`) classifies by content magic. The full detection algorithm, fatbin-variant handling, and registration flow are documented on [NVVM IR / LTO IR Input](../input/nvvm-ir-input.md).

The IR format version proper is encoded inside the bitcode payload (LLVM 3.4 wrapper + NVVM version record) and is negotiated entirely inside `libnvvm.so`; nvlink is version-agnostic with respect to the bitcode interior. The `__nvvmHandle` dispatch codes (`0x2080`, `0xB0BA`, `0xF00D`, `0xBEEF`) documented on [libNVVM Integration](libnvvm-integration.md) are internal API dispatch tags, not IR format versions.

## Profile Registration Mechanics

### The Profile Triplet

For every supported GPU architecture, `sub_484F50` creates three profile objects via `sub_484DB0` (`ArchProfile::create`):

| Type | Prefix | Role | `is_virtual` | `is_lto` |
|---|---|---|---|---|
| Real | `sm_` | Physical GPU target for SASS emission | 0 | 0 |
| Virtual | `compute_` | PTX virtual architecture | 1 | 0 |
| LTO | `lto_` | NVVM IR bitcode tag for deferred compilation | 1 | 1 |

All three profiles for a given architecture share the same `-D__CUDA_ARCH__=NNN` define. The `lto_` profile's `display_name` is set to the corresponding `compute_` name (e.g., `lto_100` displays as `compute_100`), and its `isa_class_name` is NULL (inherited from the base profile at link time). Each `lto_` profile stores a back-pointer at offset +72 (`virtual_ptr`, slot [9]) to its associated `compute_` profile.

For the complete profile struct layout (136 bytes, all field offsets), see [Architecture Profile Struct](../structs/arch-profile.md).

### Registration Into the Global Map

After creation, each profile is inserted into the global hash map at `qword_2A5F8D8` via `sub_448E70` (`LinkerHash::insert`). The map is keyed by the profile name string (`"sm_75"`, `"compute_75"`, `"lto_75"`, etc.). When nvlink encounters an `lto_` tag in an input object's ELF metadata, it looks up this map to resolve the profile and route compilation.

### Family Linkage

After hash map insertion, `sub_465720` (`list_append`) is called 4 times per architecture to wire the profiles into linked lists that encode cross-variant compatibility, same-generation family membership, compute-to-real bidirectional mapping, and base-to-suffix variant chains. The full linked-list topology is documented on [Architecture Profiles](../targets/arch-profiles.md).

## Complete LTO Profile Table

The 23 `lto_` profiles registered in `sub_484F50`, listed in initialization order (which reflects chronological feature-set order, not numeric sm_ order):

| # | LTO Profile | Compute Profile | `__CUDA_ARCH__` | Family | Variant |
|---|---|---|---|---|---|
| 1 | `lto_75` | `compute_75` | 750 | Turing | -- |
| 2 | `lto_80` | `compute_80` | 800 | Ampere | -- |
| 3 | `lto_86` | `compute_86` | 860 | Ampere | -- |
| 4 | `lto_87` | `compute_87` | 870 | Ampere | -- |
| 5 | `lto_88` | `compute_88` | 880 | Ampere | -- |
| 6 | `lto_89` | `compute_89` | 890 | Ada | -- |
| 7 | `lto_90` | `compute_90` | 900 | Hopper | -- |
| 8 | `lto_90a` | `compute_90a` | 90a0 | Hopper | accelerated |
| 9 | `lto_100` | `compute_100` | 1000 | Blackwell | -- |
| 10 | `lto_100a` | `compute_100a` | 100a0 | Blackwell | accelerated |
| 11 | `lto_100f` | `compute_100f` | 100f0 | Blackwell | forward-compat |
| 12 | `lto_110` | `compute_110` | 1100 | Blackwell | -- |
| 13 | `lto_110a` | `compute_110a` | 110a0 | Blackwell | accelerated |
| 14 | `lto_110f` | `compute_110f` | 110f0 | Blackwell | forward-compat |
| 15 | `lto_103` | `compute_103` | 1030 | Blackwell | -- |
| 16 | `lto_103a` | `compute_103a` | 103a0 | Blackwell | accelerated |
| 17 | `lto_103f` | `compute_103f` | 103f0 | Blackwell | forward-compat |
| 18 | `lto_120` | `compute_120` | 1200 | Blackwell | -- |
| 19 | `lto_120a` | `compute_120a` | 120a0 | Blackwell | accelerated |
| 20 | `lto_120f` | `compute_120f` | 120f0 | Blackwell | forward-compat |
| 21 | `lto_121` | `compute_121` | 1210 | Blackwell | -- |
| 22 | `lto_121a` | `compute_121a` | 121a0 | Blackwell | accelerated |
| 23 | `lto_121f` | `compute_121f` | 121f0 | Blackwell | forward-compat |

The `__CUDA_ARCH__` suffix `a0` / `f0` are verbatim from the binary's string pool (e.g. `-D__CUDA_ARCH__=100a0`). The `a` and `f` suffixed defines allow `#ifdef` guards in device code to detect accelerated and forward-compatible variants at compile time.

### Sub-Variant Summary

- **Base profiles** (no suffix): Target the canonical SM architecture. Profile struct bytes at offsets +4 and +5 are both 0.
- **Accelerated variants** (`a` suffix, e.g. `lto_100a`): Profile struct byte at offset +4 (`suffix_a_flag`) is set to 1. ISA class is inherited from the base SM profile. The `a` variant was introduced with sm_90a (Hopper) and extended to all sm_1XX families.
- **Forward-compatible variants** (`f` suffix, e.g. `lto_100f`): Profile struct byte at offset +5 (`suffix_f_flag`) is set to 1 on all three profiles (sm_, compute_, lto_). The `f` variant exists only for sm_1XX architectures (Blackwell and later).
- **sm_89 (Ada)**: The profile struct byte at offset +3 (`finalization_class`) is set to 1, distinguishing Ada from Ampere-family architectures. This byte indexes the 5-entry dispatch table at `dword_1D40660` used by the finalization compatibility checker `sub_4709E0`. See [Compatibility Checking](../targets/compatibility.md) for the dispatch table semantics.

## Capability Vector Assignments

Each architecture receives three 16-byte SSE vectors (loaded via `_mm_load_si128` from `.rodata` constants) stored at profile struct offsets +80, +96, +112:

| Architecture(s) | Slot [5] (+80) | Slot [6] (+96) | Slot [7] (+112) |
|---|---|---|---|
| sm_75 (Turing) | `xmmword_1D40F10` | `xmmword_1D40F20` | `xmmword_1D40F30` |
| sm_80 (Ampere base) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F30` |
| sm_86, sm_87, sm_88 (Ampere) | `xmmword_1D40F10` | `xmmword_1D40F50` | `xmmword_1D40F30` |
| sm_89 (Ada) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F30` |
| sm_90 (Hopper) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F30` |
| sm_100, sm_103 (Blackwell DC) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F70` |
| sm_110 (Thor) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F70` |
| sm_120 (RTX 50) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F70` |
| sm_121 (DGX Spark) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F70` |

Slot [5] is constant across all architectures (`xmmword_1D40F10`), representing a base capability set common to all targets. Slot [6] differentiates feature sets within a generation. Slot [7] splits at the Blackwell boundary (`xmmword_1D40F30` for pre-Blackwell, `xmmword_1D40F70` for Blackwell+), encoding the Mercury/capsule-mercury capability bit (correlated with the SM >= 100 Mercury code path in `sub_4275C0`).

Sub-variants (`a`, `f`) inherit the capability vectors from their base architecture via `_mm_loadu_si128` copy from the parent profile.

The `xmmword_1D40F10` through `xmmword_1D40F70` constants are 16-byte SSE values stored in `.rodata` at 16-byte-aligned addresses, loaded via `_mm_load_si128` (aligned load).

## LTO Compilation Flow

When nvlink encounters an `lto_`-tagged input object:

```
Input: fatbin containing lto_100 bitcode section
                    |
                    v
1. Profile lookup:  hashmap["lto_100"] -> lto_profile
                    |
                    v
2. Resolve compute: lto_profile->virtual_ptr -> compute_100 profile
                    |
                    v
3. Resolve real:    compute_100->compat_list_2 -> sm_100 profile
                    |
                    v
4. Load libNVVM:    dlopen("libnvvm.so") via sub_4BC4A0
                    |
                    v
5. Compile IR:      nvvmCompileProgram() with -arch=sm_100
                    |
                    v
6. Extract PTX:     nvvmGetCompiledResult()
                    |
                    v
7. Assemble:        embedded ptxas compiles PTX -> SASS
                    |
                    v
8. Link:            SASS object enters normal linker merge path
```

The finalize phase orchestrator (`sub_471700`, ~2,541 decompiled lines) drives this flow. It reads the architecture version from the LTO profile, constructs compiler flags including the `-D__CUDA_ARCH__=NNN` define from the profile, and invokes libNVVM. See [LTO Overview](overview.md) for the full pipeline and [Finalization Phase](../pipeline/finalize.md) for the orchestrator.

## Cross-Version Linking Rules (Summary)

Architecture compatibility for LTO finalization is enforced by `sub_4709E0` (`can_finalize_arch_check`) and `sub_470DA0` (`can_finalize_capability_mask`). Full documentation of these functions, including the 5-level dispatch table, error codes 0/24-30, the same-decade family rule, internal remapping (104->120, 130->107, 101->110), and capability bitmask semantics, is on [Compatibility Checking](../targets/compatibility.md).

Key points specific to LTO profile resolution:

1. **Same-architecture match**: An `lto_100` object links with an `sm_100` target directly.

2. **Family matching**: Architectures in the same "decade" (integer division by 10 yields the same value) are family-compatible. For example, sm_100 and sm_103 both have `100/10 == 103/10 == 10`.

3. **Architecture -> ASCII curiosity**: The capability bitmask in `sub_470DA0` uses a `switch` on character codes that happen to equal the architecture number in decimal: `100 == 'd'`, `103 == 'g'`, `110 == 'n'`, `121 == 'y'`. This is not coincidence -- the architecture numbers were chosen to align with ASCII values for compact encoding.

4. **CAN_FINALIZE_DEBUG**: Both compatibility checkers read this environment variable via `getenv()` and parse it with `strtol()`. However, the `strtol` return value is discarded (no variable captures it). The variable likely exists as a breakpoint hook for manual debugging -- it does not override or log compatibility decisions despite what its name might suggest.

### Version-Mismatch Error Strings

The following diagnostic strings are emitted when LTO objects fail version or architecture checks:

| Address | Error String |
|---|---|
| `0x1D34AF0` | `"Input file '%s' must be recompiled with toolkit >= Cuda 12.0"` |
| `0x1D34B30` | `"Input file '%s' must be recompiled with toolkit >= Cuda 7.0"` |
| `0x1D34B70` | `"Input file '%s' newer than toolkit (%d vs %d)"` |
| `0x1D34C68` | `"Input file '%s' abi does not match"` |
| `0x1D34C90` | `"Input file '%s' size does not match target '%s'"` |
| `0x1D34CC0` | `"Input file '%s' arch does not match target '%s'"` |
| `0x1D34CF0` | `"Input file '%s' ABI version '%u' is incompatible with target ABI version '%u'"` |
| `0x1D39330` | `"Object '%s' cannot be linked due to version mismatch. Objects using tcgen05 in 12.x cannot be linked with 13.0 or later, they must be rebuilt with latest compiler"` |
| `0x1D393D8` | `"Cannot link sanitized object '%s' from version %d with sanitized object from a different toolkit version (%d)"` |
| `0x1D39638` | `"Object '%s' has cuda-api-version of %d which is greater than version on link line (%d)"` |
| `0x1D321E8` | `"linking with -ewp objects requires using current toolkit"` |
| `0x1DFD088` | `"Version mismatch for device code binary for cuda source file '%s'; found version=%d, current version=%d"` |

The tcgen05 barrier at `0x1D39330` is the most common version-mismatch failure in practice: the tcgen05 instruction encoding changed between CUDA 12.x and 13.0, making objects produced by the two toolkits unlinkable.

## Key Implementation Details

### Init-Once Guard

The entire profile database is initialized exactly once. `byte_2A5F8D0` serves as the guard:

```c
if (!byte_2A5F8D0) {
    // ... register all 69 profiles (23 triplets) ...
    byte_2A5F8D0 = 1;
}
```

A `setjmp`/`longjmp` mechanism wraps the initialization for error handling. If any allocation fails during profile creation, the `longjmp` restores state.

### Default Minimum Architecture

After registering sm_80, the function sets:

```c
dword_2A5F8CC = 80;  // default minimum architecture
```

After registering sm_100:

```c
dword_2A5F8C8 = 100;  // Blackwell minimum (Mercury threshold)
```

The first value controls the minimum acceptable architecture for general linking. The second marks the Mercury format transition point -- SM >= 100 routes through the capsule-mercury output path.

## String Pool Layout

The `lto_` profile name strings occupy a contiguous region in the `.rodata` section, interleaved with their `sm_`, `compute_`, and `-D__CUDA_ARCH__=` counterparts:

| Address | String |
|---|---|
| `0x1D409F4` | `lto_75` |
| `0x1D40A27` | `lto_80` |
| `0x1D40A53` | `lto_86` |
| `0x1D40A7F` | `lto_87` |
| `0x1D40AAB` | `lto_88` |
| `0x1D40AD5` | `lto_89` |
| `0x1D40B08` | `lto_90` |
| `0x1D40B51` | `lto_90a` |
| `0x1D40B8B` | `lto_100` |
| `0x1D40BD9` | `lto_100a` |
| `0x1D40C0D` | `lto_100f` |
| `0x1D40C3E` | `lto_110` |
| `0x1D40C8C` | `lto_110a` |
| `0x1D40CC0` | `lto_110f` |
| `0x1D40CF1` | `lto_103` |
| `0x1D40D3F` | `lto_103a` |
| `0x1D40D73` | `lto_103f` |
| `0x1D40DA4` | `lto_120` |
| `0x1D40DF2` | `lto_120a` |
| `0x1D40E26` | `lto_120f` |
| `0x1D40E57` | `lto_121` |
| `0x1D40EA5` | `lto_121a` |
| `0x1D40ED9` | `lto_121f` |

The interleaved lto/sm/compute/`__CUDA_ARCH__` pool spans addresses `0x1D409C8` through `0x1D40EDC`. The strings immediately following this pool (`compute_%2d%s` at `0x1D40EE8` and `sm_%2d%s` at `0x1D40F01`) are format strings used by `sub_44E530` (`arch_format_name`), not profile entries.

## Cross-References

- [NVVM IR / LTO IR Input](../input/nvvm-ir-input.md) -- magic number `0x1EE55A01`, detection logic, registration flow
- [Architecture Profiles](../targets/arch-profiles.md) -- profile struct layout, full registration sequence, capability vectors
- [Architecture Profile Struct](../structs/arch-profile.md) -- byte-level struct layout (136 bytes, authoritative)
- [Compatibility Checking](../targets/compatibility.md) -- `sub_4709E0` / `sub_470DA0` full treatment
- [SM89 Ada](../targets/sm89-ada.md) -- Ada-specific backend and feature flags
- [LTO Overview](overview.md) -- high-level LTO pipeline
- [libNVVM Integration](libnvvm-integration.md) -- the NVVM compilation step
- [Option Forwarding](option-forwarding.md) -- how compiler flags reach libNVVM
- [Whole vs Partial LTO](whole-vs-partial.md) -- how `lto_` profile presence/absence drives the whole-vs-partial decision
- [Finalization Phase](../pipeline/finalize.md) -- the finalization orchestrator
- [Fatbin Extraction](../input/fatbin-extraction.md) -- how `sub_4CE070` detects NVVM IR in fatbin members
- [File Type Detection](../input/file-type-detection.md) -- the 56-byte header probe in `main()`
- [Versions](../versions.md) -- tool identity, complete architecture table

## Confidence Assessment

| Section | Confidence | Evidence |
|---|---|---|
| Profile triplet model (3 per arch) | **Verified** | `sub_484F50` lines 246-282 (sm_75 triplet) |
| 23 lto_ profiles (69 total) | **Verified** | String pool enumeration `0x1D409F4`-`0x1D40ED9` + decompiled source |
| All `__CUDA_ARCH__` values | **Verified** | Binary string pool at `0x1D409C8`-`0x1D40EC3` |
| `virtual_ptr` back-pointer at +72 | **Verified** | `sub_484F50` line 277: `*((_QWORD *)v10 + 9) = v9` |
| sm_89 family = "Ada" (not "Ampere") | **Verified** | `sub_484F50` line 468: `sub_484DB0(..., "Ada", ...)` |
| sm_89 `finalization_class` = 1 at +3 | **Verified** | `sub_484F50` sm_89 registration block |
| Capability slot [5] constant | **Verified** | Lines 285, 327, 371, 416, 459: all load `xmmword_1D40F10` |
| Capability slot [7] Blackwell split | **Inferred** | Pattern: `xmmword_1D40F30` pre-Blackwell, `xmmword_1D40F70` post; verified for sm_75-sm_90, inferred for sm_1XX |
| Blackwell capability vectors (sm_110+) | **Inferred** | Pattern from sm_100 extrapolated; `sub_484F50` lines 550-1330 not individually audited |
| LTO compilation flow (8-step) | **Verified** | Consistent with `lto/overview.md` and `pipeline/finalize.md` |
| `CAN_FINALIZE_DEBUG` strtol discarded | **Verified** | `sub_4709E0` lines 18-20: `strtol` result not captured |
| String pool upper bound `0x1D40EDC` | **Verified** | Last lto_ string `lto_121f` at `0x1D40ED9` + 7 bytes |
| Version-mismatch error strings | **Verified** | Each address confirmed present in binary `.rodata` |
| `dword_2A5F8C8 = 100` (Mercury threshold) | **Inferred** | Asserted from pattern; assignment occurs after sm_100 block in `sub_484F50` but not directly audited in this pass |
