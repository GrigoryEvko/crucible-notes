# LTO IR Format Versions

When nvcc compiles device code with `-dlto` (device link-time optimization), it emits NVVM IR bitcode instead of SASS machine code. This bitcode is tagged with an `lto_` profile name that encodes the target architecture. At link time, nvlink resolves each `lto_` tag to its corresponding `compute_` virtual profile, loads libNVVM, and compiles the IR down to SASS for the real `sm_` target. The `lto_` profile is the bridge between the architecture-independent IR and the architecture-specific code generator -- it determines which IR is compatible with which final target and controls how nvlink routes the compilation.

nvlink v13.0.88 registers 22 `lto_` profile variants in `sub_484F50` (gpu_architecture_profile_database_init, 54KB at `0x484F50`). Each `lto_` profile is created alongside the corresponding `sm_` (real) and `compute_` (virtual) profiles during a one-time initialization guarded by `byte_2A5F8D0`.

## Profile Registration Mechanics

### The Profile Triplet

For every supported GPU architecture, `sub_484F50` creates three profile objects via `sub_484DB0` (profile_create):

| Type | Prefix | Role | `is_virtual` | `is_lto` |
|---|---|---|---|---|
| Real | `sm_` | Physical GPU target for SASS emission | 0 | 0 |
| Virtual | `compute_` | PTX virtual architecture | 1 | 0 |
| LTO | `lto_` | NVVM IR bitcode tag for deferred compilation | 1 | 1 |

The `sub_484DB0` signature is:

```
profile_create(is_virtual, is_lto, name, display_name, family_name, cuda_arch_define, canonical_name)
```

All three profiles for a given architecture share the same `-D__CUDA_ARCH__=NNN` define. The `lto_` profile's `display_name` is set to the corresponding `compute_` name (e.g., `lto_100` displays as `compute_100`), and its `family_name` is NULL (inherited from the base profile at link time). Each `lto_` profile stores a back-pointer (offset +72, `qword slot [9]`) to its associated `compute_` profile.

### Registration Into the Global Map

After creation, each profile is inserted into a global hash map at `qword_2A5F8D8` via `sub_448E70` (hashmap_insert). The map is keyed by the profile name string (`"sm_75"`, `"compute_75"`, `"lto_75"`, etc.). When nvlink encounters an `lto_` tag in an input object's ELF metadata, it looks up this map to resolve the profile and route compilation.

### Family Linkage

After hash map insertion, `sub_465720` (linked_list_append) is called 4 times per architecture to wire the profiles into linked lists that encode:

1. The compute-to-sm mapping (which real target a virtual arch compiles for)
2. The family chain (which architectures belong to the same generation)
3. Forward-compatibility chains (for `a` and `f` sub-variants)
4. Backward pointers for ISA class sharing

## Complete LTO Profile Table

The 22 `lto_` profiles registered in `sub_484F50`, in initialization order:

| # | LTO Profile | Compute Profile | `__CUDA_ARCH__` | Family | SM Base | Variant |
|---|---|---|---|---|---|---|
| 1 | `lto_75` | `compute_75` | 750 | Turing | sm_75 | -- |
| 2 | `lto_80` | `compute_80` | 800 | Ampere | sm_80 | -- |
| 3 | `lto_86` | `compute_86` | 860 | Ampere | sm_86 | -- |
| 4 | `lto_87` | `compute_87` | 870 | Ampere | sm_87 | -- |
| 5 | `lto_88` | `compute_88` | 880 | Ampere | sm_88 | -- |
| 6 | `lto_89` | `compute_89` | 890 | Ada | sm_89 | -- |
| 7 | `lto_90` | `compute_90` | 900 | Hopper | sm_90 | -- |
| 8 | `lto_90a` | `compute_90a` | 90a0 | Hopper | sm_90a | accelerated |
| 9 | `lto_100` | `compute_100` | 1000 | Blackwell | sm_100 | -- |
| 10 | `lto_100a` | `compute_100a` | 100a0 | Blackwell | sm_100a | accelerated |
| 11 | `lto_100f` | `compute_100f` | 100f0 | Blackwell | sm_100f | forward-compat |
| 12 | `lto_110` | `compute_110` | 1100 | Blackwell | sm_110 | -- |
| 13 | `lto_110a` | `compute_110a` | 110a0 | Blackwell | sm_110a | accelerated |
| 14 | `lto_110f` | `compute_110f` | 110f0 | Blackwell | sm_110f | forward-compat |
| 15 | `lto_103` | `compute_103` | 1030 | Blackwell | sm_103 | -- |
| 16 | `lto_103a` | `compute_103a` | 103a0 | Blackwell | sm_103a | accelerated |
| 17 | `lto_103f` | `compute_103f` | 103f0 | Blackwell | sm_103f | forward-compat |
| 18 | `lto_120` | `compute_120` | 1200 | Blackwell | sm_120 | -- |
| 19 | `lto_120a` | `compute_120a` | 120a0 | Blackwell | sm_120a | accelerated |
| 20 | `lto_120f` | `compute_120f` | 120f0 | Blackwell | sm_120f | forward-compat |
| 21 | `lto_121` | `compute_121` | 1210 | Blackwell | sm_121 | -- |
| 22 | `lto_121a` | `compute_121a` | 121a0 | Blackwell | sm_121a | accelerated |

Note: `lto_121f` is also registered (23 total including it), making the table exhaustive. The numbering in the table follows the initialization sequence in `sub_484F50`, not numeric order.

### Corrected Count

The full function registers 23 `lto_` profiles (22 listed above plus `lto_121f` with `__CUDA_ARCH__=121f0`). The string evidence from the binary confirms all 23 entries in the string pool at addresses `0x1D409F4` through `0x1D40ED9`.

## Sub-Variant Semantics

### Base Profiles (no suffix)

The base profile (e.g., `lto_100`) targets the canonical SM architecture. Code compiled with this profile uses the standard instruction set for that SM generation. The profile struct byte at offset +3 is 0, byte at offset +4 is 0, byte at offset +5 is 0.

### Accelerated Variants (`a` suffix)

The `a` variants (e.g., `lto_100a`, `lto_90a`) target the accelerated sub-variant of an SM architecture. In the profile struct, byte offset +4 is set to 1 for both the `sm_` and `compute_` profiles:

```c
// From sub_484F50, sm_100a registration:
v79->m128i_i8[4] = 1;       // sm_100a profile: variant byte = 1
*(_BYTE *)(v82 + 4) = 1;    // compute_100a profile: variant byte = 1
```

The `__CUDA_ARCH__` define appends `a0` (e.g., `-D__CUDA_ARCH__=100a0`), which allows `#ifdef` guards in device code to detect the accelerated variant. The ISA class is inherited from the base SM profile -- `(profile_sm_100)->isaClass` is used for both `sm_100` and `sm_100a`. This means the instruction encoding tables are shared; the "accelerated" designation controls feature enablement flags rather than fundamental ISA differences.

The `a` variant was introduced with sm_90a (Hopper) and extended to all sm_1XX families in Blackwell.

### Forward-Compatible Variants (`f` suffix)

The `f` variants (e.g., `lto_100f`, `lto_120f`) represent forward-compatible profiles. In the profile struct, byte offset +5 is set to 1 for the `sm_`, `compute_`, and `lto_` profiles:

```c
// From sub_484F50, sm_100f registration:
v88->m128i_i8[5] = 1;       // sm_100f: forward-compat flag = 1
*(_BYTE *)(v91 + 5) = 1;    // compute_100f: forward-compat flag = 1
v94[5] = 1;                  // lto_100f: forward-compat flag = 1
```

The `__CUDA_ARCH__` define appends `f0` (e.g., `-D__CUDA_ARCH__=100f0`). Forward-compatible profiles generate code that can run on future architectures within the same family. They avoid using features that might not be available in later silicon revisions, at the cost of not exploiting all hardware capabilities.

The `f` variant exists only for sm_1XX architectures (Blackwell and later). It does not exist for sm_90a or any pre-Blackwell architecture.

### The sm_89 Exception

sm_89 (Ada Lovelace) has an additional flag at byte offset +3 set to 1:

```c
v47->m128i_i8[3] = 1;  // sm_89 only
```

This flag distinguishes Ada from the other Ampere-family architectures (sm_80, sm_86, sm_87, sm_88). The flag likely controls ISA feature availability specific to Ada's extensions (e.g., SER instructions, micro-mesh shaders).

## Profile Struct Layout (Relevant Fields)

Based on the `sub_484DB0` allocation and `sub_484F50` initialization:

```
Offset   Size   Field
+0       1      is_virtual (0 = real/sm_, 1 = virtual/compute_ or lto_)
+1       1      is_lto (0 = sm_ or compute_, 1 = lto_)
+3       1      ada_flag (1 for sm_89 only)
+4       1      is_accelerated (1 for "a" variants)
+5       1      is_forward_compat (1 for "f" variants)
+8       8      name string pointer ("sm_100", "compute_100", "lto_100")
+16      8      display_name pointer (same as name for sm_/compute_; compute_ name for lto_)
+24      8      family_name pointer ("Turing", "Ampere", "Hopper", "Blackwell", or NULL)
+32      8      cuda_arch_define pointer ("-D__CUDA_ARCH__=1000")
+40      8      canonical_name pointer (same as name for sm_/compute_; lto_ name for lto_)
+64      8      linked_list: forward pointer (next in family chain)
+72      8      back-pointer to associated compute_ profile (slot [9])
+80      16     capability vector slot [5] (XMM-loaded from xmmword_1D40F10+)
+96      16     capability vector slot [6] (architecture-specific feature mask)
+112     16     capability vector slot [7] (ISA version / compatibility flags)
```

The capability vectors at offsets +80/+96/+112 are loaded from static constants (`xmmword_1D40F10` through `xmmword_1D40F70`) via SSE intrinsics. Different architectures get different combinations of these vectors, encoding their hardware feature sets.

## Capability Vector Assignments

The XMM constants assigned to profile slots [5], [6], [7] cluster architectures into ISA families:

| Architecture(s) | Slot [5] | Slot [6] | Slot [7] |
|---|---|---|---|
| sm_75 (Turing) | `xmmword_1D40F10` | `xmmword_1D40F20` | `xmmword_1D40F30` |
| sm_80 (Ampere base) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F30` |
| sm_86 (Ampere) | `xmmword_1D40F10` | `xmmword_1D40F50` | `xmmword_1D40F30` |
| sm_87, sm_88 (Ampere) | `xmmword_1D40F10` | `xmmword_1D40F50` | `xmmword_1D40F30` |
| sm_89 (Ada) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F30` |
| sm_90 (Hopper) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F30` |
| sm_100, sm_103 (Blackwell) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F70` |
| sm_110 (Thor) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F70` |
| sm_120 (RTX 50) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F70` |
| sm_121 (DGX Spark) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F70` |

Slot [5] is constant across all architectures (`xmmword_1D40F10`), suggesting a base capability set. Slot [6] differentiates feature sets within a generation. Slot [7] splits at the Blackwell boundary (`xmmword_1D40F30` for pre-Blackwell, `xmmword_1D40F70` for Blackwell+), likely encoding the Mercury/capsule-mercury capability bit.

Sub-variants (`a`, `f`) inherit the capability vectors from their base architecture via `_mm_loadu_si128` copy from the parent profile.

## LTO Compilation Flow

When nvlink encounters an `lto_`-tagged input object:

```
Input: fatbin containing lto_100 bitcode section
                    |
                    v
1. Profile lookup:  hashmap["lto_100"] -> lto_profile
                    |
                    v
2. Resolve compute: lto_profile->back_ptr -> compute_100 profile
                    |
                    v
3. Resolve real:    compute_100->linked_sm -> sm_100 profile
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

The finalization orchestrator (`sub_471700`, 78KB) drives this flow. It reads the architecture version from the LTO profile, constructs compiler flags including the `-D__CUDA_ARCH__=NNN` define from the profile, and invokes libNVVM.

## Cross-Version Linking Rules

### Family Compatibility

The architecture compatibility checker `sub_4709E0` (can_finalize_architecture_check) enforces these rules:

1. **Same-architecture match**: An `lto_100` object links with an `sm_100` target directly.

2. **Family matching**: Architectures in the same "decade" (integer division by 10 yields the same value) are in the same family. For example, sm_100 and sm_103 both have `100/10 == 103/10 == 10`, so they are family-compatible.

3. **Internal remapping**: Before comparison, certain architecture codes are remapped:
   - `104` -> `120` (internal code 'h' maps to sm_120)
   - `130` -> `107` (maps to sm_100 family range)
   - `101` -> `110` (maps to sm_110)

4. **Version ceiling**: Architecture version must be <= `0x101` (257 decimal), rejecting invalid/future values.

5. **Error codes** from `sub_4709E0`:
   - 0: compatible
   - 24: null input
   - 25: version too high
   - 26: incompatible architecture
   - 27-30: various type/class mismatches

### Capability-Based Compatibility

The companion function `sub_470DA0` (can_finalize_with_capability_mask) adds a bitmask check on top of the architecture match. Each target architecture maps to a capability bit:

| Arch code | ASCII | Bitmask |
|---|---|---|
| 100 | 'd' | 1 |
| 103 | 'g' | 8 |
| 110 | 'n' | 2 |
| 121 | 'y' | 64 |

The check reads a capability mask from the profile object at offset +16 and verifies that the required bits are set: `(required & *capability_ptr) == required`. This prevents linking code that requires capabilities the target does not support.

### Cross-Toolkit Version Restrictions

Beyond architecture matching, nvlink v13.0.88 enforces toolkit version compatibility:

- **tcgen05 instruction barrier**: Objects compiled with CUDA 12.x that use tcgen05 instructions cannot link with CUDA 13.0+ objects (error at string `0x1D39330`). The tcgen05 encoding changed between 12.x and 13.0.

- **ABI version check**: The ABI version embedded in the object must match the linker's expected version (error at string `0x1D34CF0`).

- **Sanitizer version**: Sanitizer-instrumented objects must match the toolkit's sanitizer version exactly (error at string `0x1D393D8`).

## Architecture Families

### Turing (sm_75)

Single architecture, no sub-variants. The oldest generation supported by nvlink v13.0.88. Uses `__CUDA_ARCH__=750`.

### Ampere (sm_80, sm_86, sm_87, sm_88, sm_89)

Five architectures. sm_80 is the base (GA100), sm_86 is GA102/GA104, sm_87 is Orin (Jetson), sm_88 is a new Ampere variant appearing for the first time in CUDA 13.0 (not previously documented publicly), and sm_89 is Ada Lovelace (classified under Ampere family for code generation purposes despite being a separate GPU generation). sm_89 sets an additional flag (byte offset +3) distinguishing its feature set.

No `a` or `f` sub-variants exist for Ampere.

### Hopper (sm_90, sm_90a)

Two profiles. sm_90 is the base H100, sm_90a is the accelerated variant enabling additional features (e.g., DPX, FP8 acceleration). The `a` suffix was introduced with this generation. `__CUDA_ARCH__=900` for base, `__CUDA_ARCH__=90a0` for accelerated.

No `f` sub-variant for Hopper.

### Blackwell (sm_100, sm_103, sm_110, sm_120, sm_121)

Five base architectures, each with `a` (accelerated) and `f` (forward-compatible) sub-variants, totaling 15 profiles. All are classified under the "Blackwell" family string.

| SM | `__CUDA_ARCH__` | Segment |
|---|---|---|
| sm_100 | 1000 | Datacenter (B100/B200) |
| sm_103 | 1030 | Blackwell Ultra (GB300) |
| sm_110 | 1100 | Jetson Thor |
| sm_120 | 1200 | Consumer (RTX 50-series) / Enterprise (RTX Pro) |
| sm_121 | 1210 | DGX Spark |

All sm_1XX architectures use the same ISA class as their base (`(profile_sm_XXX)->isaClass` assertion strings in the binary). The `a` and `f` variants share instruction encoding tables with the base but differ in feature enablement.

## String Pool Layout

The `lto_` profile name strings occupy a contiguous region in the `.rodata` section:

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

The strings interleave with their `sm_` and `compute_` counterparts. The pool spans addresses `0x1D409C8` through `0x1D40F01`.

## Key Implementation Details

### Init-Once Guard

The entire profile database is initialized exactly once. `byte_2A5F8D0` serves as the guard:

```c
if (!byte_2A5F8D0) {
    // ... register all 66+ profiles ...
    byte_2A5F8D0 = 1;
}
```

A `setjmp`/`longjmp` mechanism wraps the initialization for exception safety. If any allocation fails during profile creation, the `longjmp` restores state and marks the initialization as failed.

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

### Environment Variable Debug Override

The compatibility checkers (`sub_4709E0`, `sub_470DA0`) read the `CAN_FINALIZE_DEBUG` environment variable via `getenv()`. When set, `strtol` parses it to override or log compatibility decisions. This is a debugging aid not documented in public CUDA documentation.

## Cross-References

- [Versions](../versions.md) -- tool identity, complete architecture table
- [Architecture Profiles](../targets/arch-profiles.md) -- profile struct layout
- [LTO Overview](overview.md) -- high-level LTO pipeline
- [libNVVM Integration](libnvvm-integration.md) -- the NVVM compilation step
- [Option Forwarding](option-forwarding.md) -- how compiler flags reach libNVVM
- [Finalization Phase](../pipeline/finalize.md) -- the finalization orchestrator
