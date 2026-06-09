# Versions

This page documents the version identity of nvlink v13.0.88, its build metadata, the complete set of supported GPU architectures, and the embedded PTX ISA version. All data is extracted directly from the binary's string pool and the architecture profile initialization function at `0x484F50`.

## Tool Identity

nvlink identifies itself through three string constants stored in `.rodata`:

| Field | Value | Address |
|---|---|---|
| Tool name | `NVIDIA (R) Cuda linker` | `0x1D32984` |
| Version string | `Cuda compilation tools, release 13.0, V13.0.88` | `0x1D34568` |
| Build string | `Build cuda_13.0.r13.0/compiler.36424714_0` | `0x1D34538` |
| Copyright | `Copyright (c) 2005-2025 NVIDIA Corporation` | `0x1D33CE8` |
| Build timestamp (nvlink) | `Wed_Aug_20_01:58:59_PM_PDT_2025` | `0x1D33CC8` |
| Build timestamp (ptxas) | `Wed_Aug_20_01:55:12_PM_PDT_2025` | `0x1D42088` |
| PTX ISA version | 9.0 (major=9, minor=0) | hardcoded in `0x12AF950` |

The `--version` (`-V`) flag triggers `sub_427AE0` (nvlink_parse_options) to print the combined version and build strings. The binary stores the version and build as a single two-line string at `0x1D33D18`:

```text
Cuda compilation tools, release 13.0, V13.0.88
Build cuda_13.0.r13.0/compiler.36424714_0
```

This same string is referenced from both the option parser (`sub_427AE0` at `0x429740`) and the embedded ptxas version handler (`sub_1103030` at `0x1104932`).

The copyright string uses a format specifier for the end year: `Copyright (c) 2005-%s NVIDIA Corporation`. The `%s` is filled with the current year at runtime, though the binary was built in 2025.

The version string without the build line (`0x1D34568`) is referenced from five locations: `main` (`0x409E9B`), `sub_468560` (`0x468575`), `sub_4748F0` (`0x4764E9`), `sub_52E060` (`0x53090E`), and `sub_1406B40` (`0x1407016`). These correspond to the main entry point, the architecture compatibility checker, the knobs infrastructure, the embedded ptxas frontend, and an instruction selection initialization function.

### Build Identifier Decomposition

The build string `cuda_13.0.r13.0/compiler.36424714_0` encodes:

| Component | Value | Meaning |
|---|---|---|
| `cuda_13.0` | Toolkit series | CUDA Toolkit 13.0 |
| `r13.0` | Release branch | Release 13.0 branch in the internal build system |
| `compiler` | Component | Compiler team build artifact |
| `36424714` | Changelist / build number | Internal Perforce-style changelist or CI build ID |
| `_0` | Build variant | Default (non-debug) variant |

The internal build path `0x1D41468` confirms the Perforce depot structure: `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/common/utils/generic/impl/generic_knobs_impl.h`.

### Two Build Timestamps

The binary contains two distinct build timestamps, separated by approximately 3 minutes and 47 seconds:

- **`0x1D33CC8`**: `Wed_Aug_20_01:58:59_PM_PDT_2025` — the nvlink-specific timestamp, referenced from the option parser and main entry point context.
- **`0x1D42088`**: `Wed_Aug_20_01:55:12_PM_PDT_2025` — the embedded ptxas timestamp, located in the architecture profile region.

The earlier timestamp belongs to the ptxas component, the later to nvlink itself. This is consistent with a two-stage build: ptxas is compiled first as a static library, then linked into the nvlink binary approximately 4 minutes later.

## PTX ISA Version

The embedded ptxas backend hardcodes PTX ISA version 9.0. The initialization function at `0x12AF950` sets `PTX_MAJOR_VERSION = 9` and `PTX_MINOR_VERSION = 0` via hashmap insertion (`sub_448E70`). These values are referenced as string keys `"PTX_MAJOR_VERSION"` (`0x1F23AD0`) and `"PTX_MINOR_VERSION"` (`0x1F23AE2`).

PTX ISA 9.0 is the version introduced with CUDA Toolkit 13.0. It is the maximum version accepted by the embedded ptxas parser. PTX input files with `.version` directives specifying a later version will be rejected with the error `Unsupported .version %s; current version is '%s'` (`0x1F3F668`).

The `--version-ls` (`--list-version`) option in the embedded ptxas (`sub_1103030`) prints supported PTX ISA versions. This option is registered at address `0x11033A3` and handled at `0x11049E9`.

## Supported GPU Architectures

The architecture profile database is initialized by `sub_484F50` (gpu_architecture_profile_database_init), a 54 KB function at `0x484F50`. It registers every supported GPU architecture by creating three profile objects per architecture via `sub_484DB0` (profile_create):

1. **Real profile** (`sm_XX`) — targets physical GPU hardware, produces SASS binary
2. **Virtual profile** (`compute_XX`) — targets a virtual architecture, produces PTX that can be JIT-compiled to any compatible real target
3. **LTO profile** (`lto_XX`) — targets link-time optimization IR, compiled to SASS during the LTO pipeline

Profiles are stored in a hash map at global `qword_2A5F8D8` for O(1) lookup by name. The initialization is guarded by `byte_2A5F8D0` (init-once pattern). The default minimum architecture is set to sm_80 via `dword_2A5F8CC = 80`.

### Complete Architecture Table

The table below lists all 22 architecture entries in registration order, extracted from the profile initialization code and the string pool at `0x1D409C8`--`0x1D40F01`. The "Family" column comes from the family name strings embedded in the profile table. The "ISA class" column indicates which base architecture's ISA encoding tables and instruction selection backend are shared, determined by the `(profile_sm_XX)->isaClass` assertions.

| # | SM | `sm_` | `compute_` | `lto_` | `__CUDA_ARCH__` | Family | ISA Class | Variants |
|---|---|---|---|---|---|---|---|---|
| 1 | 75 | `sm_75` | `compute_75` | `lto_75` | `750` | Turing | sm_75 | — |
| 2 | 80 | `sm_80` | `compute_80` | `lto_80` | `800` | Ampere | sm_80 | — |
| 3 | 86 | `sm_86` | `compute_86` | `lto_86` | `860` | Ampere | sm_80 | — |
| 4 | 87 | `sm_87` | `compute_87` | `lto_87` | `870` | Ampere | sm_80 | — |
| 5 | 88 | `sm_88` | `compute_88` | `lto_88` | `880` | Ampere | sm_80 | — |
| 6 | 89 | (dynamic) | `compute_89` | `lto_89` | `890` | Ampere | sm_80 | — |
| 7 | 90 | `sm_90` | `compute_90` | `lto_90` | `900` | Hopper | sm_90 | `sm_90a` |
| 8 | 90a | `sm_90a` | `compute_90a` | `lto_90a` | `90a0` | Hopper | sm_90 | — |
| 9 | 100 | `sm_100` | `compute_100` | `lto_100` | `1000` | Blackwell | sm_100 | `sm_100a`, `sm_100f` |
| 10 | 100a | `sm_100a` | `compute_100a` | `lto_100a` | `100a0` | Blackwell | sm_100 | — |
| 11 | 100f | `sm_100f` | `compute_100f` | `lto_100f` | `100f0` | Blackwell | sm_100 | — |
| 12 | 103 | `sm_103` | `compute_103` | `lto_103` | `1030` | Blackwell | sm_103 | `sm_103a`, `sm_103f` |
| 13 | 103a | `sm_103a` | `compute_103a` | `lto_103a` | `103a0` | Blackwell | sm_103 | — |
| 14 | 103f | `sm_103f` | `compute_103f` | `lto_103f` | `103f0` | Blackwell | sm_103 | — |
| 15 | 110 | `sm_110` | `compute_110` | `lto_110` | `1100` | Blackwell | sm_110 | `sm_110a`, `sm_110f` |
| 16 | 110a | `sm_110a` | `compute_110a` | `lto_110a` | `110a0` | Blackwell | sm_110 | — |
| 17 | 110f | `sm_110f` | `compute_110f` | `lto_110f` | `110f0` | Blackwell | sm_110 | — |
| 18 | 120 | `sm_120` | `compute_120` | `lto_120` | `1200` | Blackwell | sm_120 | `sm_120a`, `sm_120f` |
| 19 | 120a | `sm_120a` | `compute_120a` | `lto_120a` | `120a0` | Blackwell | sm_120 | — |
| 20 | 120f | `sm_120f` | `compute_120f` | `lto_120f` | `120f0` | Blackwell | sm_120 | — |
| 21 | 121 | `sm_121` | `compute_121` | `lto_121` | `1210` | Blackwell | sm_121 | `sm_121a`, `sm_121f` |
| 22 | 121a | `sm_121a` | `compute_121a` | `lto_121a` | `121a0` | Blackwell | sm_121 | — |
| — | 121f | `sm_121f` | `compute_121f` | `lto_121f` | `121f0` | Blackwell | sm_121 | — |

**Counting note**: The "22 architectures" figure counts the base SM numbers (75, 80, 86, 87, 88, 89, 90, 100, 103, 110, 120, 121) plus the `a` and `f` sub-variants as distinct profile entries. The exact count depends on whether one counts each profile triplet (sm/compute/lto) as one architecture or each sub-variant independently. The profile database contains 66 total profile objects (22 base architectures x 3 profile types, approximately).

### Architecture Families

The profile table groups architectures into four named families, stored as literal strings in the profile data:

| Family | String Address | SM Members | ISA Generation |
|---|---|---|---|
| **Turing** | `0x1D409DC` | sm_75 | Pre-Ampere, minimum supported arch |
| **Ampere** | `0x1D40A0F` | sm_80, sm_86, sm_87, sm_88, sm_89 | GA100 / GA10x / AD10x |
| **Hopper** | `0x1D40AF0` | sm_90, sm_90a | GH100, cluster launch, WGMMA |
| **Blackwell** | `0x1D40B6E` | sm_100–sm_121 (all sub-variants) | Mercury ISA, capsule output |

Note that sm_89 (Ada Lovelace / AD102-AD104) is classified under the "Ampere" family in nvlink's profile database, not in a separate "Ada" family. The binary contains no "Ada" or "Lovelace" family name string. From the linker's perspective, sm_89 shares the Ampere ISA class and encoding tables, despite being a distinct physical GPU generation. Similarly, sm_103 through sm_121 are all classified as "Blackwell" regardless of their physical deployment (Blackwell Ultra, Jetson Thor, consumer RTX 50-series, DGX Spark).

### Sub-variant Semantics

The `a` and `f` suffixes on architecture names have specific meanings in the profile system:

- **`a` (architecture-specific)**: Targets a specific chip die with features not available on the base architecture. PTX compiled for `sm_XYa` can only execute on `sm_XYa` hardware, not on other members of the same family. Example: `sm_90a` targets GH100 with cluster launch features that `sm_90` does not require.

- **`f` (forward-compatible)**: A forward-compatible profile within the same family. PTX for `sm_XYf` can execute on `sm_XZ` and `sm_XZf` where Z >= Y and both belong to the same family. The `f` variants appear for all Blackwell-generation architectures: sm_100f, sm_103f, sm_110f, sm_120f, sm_121f.

These semantics are documented in the embedded ptxas help text at `0x1EEAF28`:

> PTX for .target sm_XY can be compiled to all GPU targets sm_MN, sm_MNa, SM_MNf where MN >= XY. PTX for .target sm_XYf can be compiled to GPU targets sm_XZ, sm_XZf, sm_XZa where Z >= Y and sm_XY and sm_XZ belong in same family. PTX with .target sm_XYa can only be compiled to GPU target sm_XYa.

### The sm_89 Anomaly

The profile table at `0x1D409C8`--`0x1D40F01` contains an explicit `sm_XX` string literal for every architecture except sm_89. The entries for sm_89 are:

| Field | Address | String |
|---|---|---|
| `__CUDA_ARCH__` | `0x1D40AB2` | `-D__CUDA_ARCH__=890` |
| `compute_` | `0x1D40ACA` | `compute_89` |
| `lto_` | `0x1D40AD5` | `lto_89` |
| `sm_` | — | (no literal string) |

The `sm_89` name does appear elsewhere in the binary at `0x1F4DB66` as part of a format string `%s on sm_89`, referenced from `sub_145EFB0` in the instruction selection region. This confirms the architecture exists. The profile initialization code at `sub_484F50` generates the `sm_89` string dynamically via the `sm_%d%c` format string (`0x1D321B7`) or the `sm_%2d%s` format (`0x1D40F01`), rather than storing it as a literal in the profile table.

The practical consequence is zero: sm_89 (Ada Lovelace) is a fully supported architecture with real, virtual, and LTO profiles. The string generation method is simply an implementation detail of the profile database initialization.

### ISA Class Sharing

The profile initialization code contains assertions of the form `(profile_sm_XX)->isaClass` at `0x1D40B0F` through `0x1D40E5F`. These assertions verify that sub-variants share the same ISA class as their base architecture:

| Assertion | Address | Meaning |
|---|---|---|
| `(profile_sm_90)->isaClass` | `0x1D40B0F` | sm_90a shares ISA with sm_90 |
| `(profile_sm_100)->isaClass` | `0x1D40B93` | sm_100a, sm_100f share ISA with sm_100 |
| `(profile_sm_110)->isaClass` | `0x1D40C46` | sm_110a, sm_110f share ISA with sm_110 |
| `(profile_sm_103)->isaClass` | `0x1D40CF9` | sm_103a, sm_103f share ISA with sm_103 |
| `(profile_sm_120)->isaClass` | `0x1D40DAC` | sm_120a, sm_120f share ISA with sm_120 |
| `(profile_sm_121)->isaClass` | `0x1D40E5F` | sm_121a, sm_121f share ISA with sm_121 |

"ISA class" determines which instruction encoding tables and instruction selection backend are used. Sub-variants within a family share the same SASS instruction set — the `a` and `f` variants differ in feature availability and compatibility semantics, not in instruction encoding.

Note the absence of an `(profile_sm_75)->isaClass` or `(profile_sm_80)->isaClass` assertion. Turing (sm_75) and Ampere (sm_80) have no sub-variants in this binary, so no sharing assertion is needed. The five Ampere sub-architectures (sm_80, sm_86, sm_87, sm_88, sm_89) all share the sm_80 ISA class implicitly through the Ampere family assignment.

### Mercury Mode

Architectures with SM >= 100 trigger Mercury mode in nvlink. The option parser at `sub_427AE0` checks whether the parsed `--arch` value exceeds 99 and, if so, sets two internal flags:

- `byte_2A5F222 = 1` — Mercury mode enabled
- `byte_2A5F225 = 1` — Related capability flag

Mercury mode changes the output format from traditional cubin (SASS in `.text` sections) to capsule mercury format, routes the output through the FNLZR (Finalizer) post-link binary rewriter at `sub_4275C0`, and enables R_MERCURY relocation types alongside R_CUDA relocations. See [Mercury Overview](mercury/overview.md) for details.

## Legacy Architectures

The architecture name parser at `sub_486FF0` and the format strings `sm_%2d%s` / `compute_%2d%s` / `sass_%2d%s` can parse any numeric architecture value. However, the option parser enforces a minimum:

```text
SM Arch ('%s') must be >= 20
```

This error string at `0x1D34F8E` is emitted when the parsed SM number is below 20. Architectures sm_10 through sm_19 (compute capabilities 1.0–1.3) are syntactically recognized but immediately rejected.

Architectures between sm_20 and sm_72 are parseable and do not trigger the `>= 20` error, but they are not present in the profile database initialized by `sub_484F50`. Attempting to use them produces architecture-not-found errors during profile lookup. The following legacy architecture names appear in the binary's string pool (in the embedded ptxas component) but have no corresponding profile entries:

| Legacy SM | Address | Context |
|---|---|---|
| `sm_11` | `0x1F4F0EE` | ptxas legacy support check |
| `sm_12` | `0x1F4F0F4` | ptxas legacy support check |
| `sm_20` | `0x1F4C7F4` | ptxas legacy target reference |
| `sm_21` | `0x1F56EA0` | ptxas legacy target reference |
| `sm_30` | `0x1F4B259` | ptxas legacy target reference |
| `sm_32` | `0x1F4F1C4` | ptxas legacy target reference |
| `sm_35` | `0x1F4CCFF` | ptxas legacy target reference |
| `sm_50` | `0x1F4C84E` | ptxas legacy target reference |
| `sm_53` | `0x1F4F312` | ptxas legacy target reference |
| `sm_60` | `0x1F4B23D` | ptxas legacy target reference |
| `sm_61` | `0x1F4FBEA` | ptxas legacy target reference |
| `sm_62` | `0x1F56EA6` | ptxas legacy target reference |
| `sm_70` | `0x1EECCE4` | `.target sm_70` (PTX directive) |
| `sm_72` | `0x1F4B64C` | ptxas legacy target reference |

These strings reside in the embedded ptxas address range (`0x1EE0000`--`0x1F60000`), not in the nvlink linker core. They are remnants of the ptxas codebase which historically supported older architectures. The linker core itself only references architectures that exist in the profile database (sm_75 and above).

The error message at `0x1D39488` provides additional context on cross-version compatibility:

> For kernel functions with parameter size higher than 4k bytes on sm_7x and sm_8x, all objects must be compiled with 12.1 or later

And the tcgen05 compatibility guard at `0x1D39330`:

> Object '%s' cannot be linked due to version mismatch. Objects using tcgen05 in 12.x cannot be linked with 13.0 or later, they must be rebuilt with latest compiler

## Version Validation

nvlink performs several version compatibility checks during linking:

### ABI Version Check

Input cubin objects carry an ABI version in their ELF headers. nvlink validates that the ABI version of each input matches the target:

```text
Input file '%s' ABI version '%u' is incompatible with target ABI version '%u'
```
(string at `0x1D34CF0`)

### CUDA API Version Check

The `--cuda-api-version` option is validated against the toolkit version:

```text
--cuda-api-version major number must be == toolkit version
```
(string at `0x1D33DF0`)

The option value is parsed as `%u.%u` (major.minor) and the major version must equal the toolkit major version (13).

### Sanitizer Version Check

Objects compiled with the sanitizer must be linked with objects from the same toolkit version:

```text
Cannot link sanitized object '%s' from version %d with sanitized object from a different toolkit version (%d)
```
(string at `0x1D393D8`)

### CUDA API Forward Compatibility Check

```text
Object '%s' has cuda-api-version of %d which is greater than version on link line (%d)
```
(string at `0x1D39638`)

### Version String Formatting

The function at `sub_468560` generates version strings dynamically using the format:

```text
Cuda compilation tools, release %d.%d,
```
(string at `0x1D3C778`)

This allows the binary to produce version-like output even for compatibility messages that reference versions other than its own.

## Architecture Name Parsing

Architecture names are parsed by `sub_486FF0` (architecture_parse_name_to_number) and formatted back to strings by `sub_487220` (architecture_name_format). The parsing accepts three prefixes:

| Prefix | Format string | Address | Description |
|---|---|---|---|
| `sm_` | `sm_%2d%s` | `0x1D40F01` | Real (physical GPU) target |
| `compute_` | `compute_%2d%s` | `0x1D40EE8` | Virtual (PTX-level) target |
| `sass_` | `sass_%2d%s` | `0x1D40EF6` | SASS binary target (used internally) |

The `%2d` portion extracts the numeric SM version. The `%s` suffix captures optional modifiers (`a`, `f`, or empty). The dynamic format `sm_%d%c` at `0x1D321B7` is used in the linker core for generating architecture names where the suffix is a single character.

Profile lookup is performed via hash map access at `qword_2A5F8D8`. The parsed name is used as the hash key. If the name is not found in the profile database, the error is:

```text
Arch 'sm_%d' not supported
```
(string at `0x1E5C353`, in the embedded ptxas region)

For nvlink specifically:

```text
Link target of '%s' is virtual target that is not JIT-able; use 'sm_' target instead
```
(string at `0x1D347F8`)

This error is emitted when the user specifies a `compute_XX` target for linking. nvlink requires a real `sm_XX` target because it produces SASS binary output, not PTX.
