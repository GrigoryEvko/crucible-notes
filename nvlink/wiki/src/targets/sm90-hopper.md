# SM90 Hopper

SM90 (Hopper, H100/H200) is the first Mercury-capable architecture in nvlink v13.0.88 and the last architecture for which SASS remains the default binary format. SM90 shares its backend implementation with SM89 (Ada Lovelace): the two use the same instruction selector mega-hub (`sub_119BF40`, 231 KB), the same ~750 shared instruction encoder templates, the same compilation driver, and the same scheduling pipeline. Although all six dispatch table callback slots have distinct function addresses between sm_89 and sm_90, only the backend init slot (A8) produces different behavior: Hopper uses a shared memory resource limit of `0x8000` (32768) vs Ada's `0x7005` (28677), and Hopper's init includes a `--blocks-are-clusters` conditional that Ada lacks entirely. The remaining five slots are functionally identical duplicates. The `sm_90a` variant uses the same function pointers as base sm_90 in all slots -- the "a" suffix enables architecture-specific features at runtime through the feature flag configurator, not through separate code paths. See [SM89 Ada: Ada vs Hopper Differences](sm89-ada.md#ada-vs-hopper-concrete-binary-differences) for the complete catalog.

SM90's Mercury significance: the FNLZR (finalizer) pre-link path fires for every architecture with sm > 89 (`dword_2A5F314 > 0x59`), making SM90 the first target subjected to the FNLZR pipeline. However, SM90's default binary kind remains `sass` -- the `capmerc` (Capsule Mercury) default only kicks in at SM100+. The MercExpand engine runs in the backend compiler pipeline for SM90, processing Mercury instruction builtins (the 667 ZREPHEL-encoded templates), but the output ELF is standard SASS rather than capmerc format.

This page documents the SM90-specific instruction codec at `0xA70000`--`0xB80000`, the shared SM89/90 backend driver at `0x1100000`--`0x11EA000`, the Hopper-specific tensor core (HMMA/WMMA) encoding and decoding infrastructure, and SM90's relationship to the Mercury pipeline.

## SM90 as First Mercury Architecture

SM90 occupies a transitional position in the Mercury architecture story. Three pieces of evidence establish this:

1. **FNLZR pre-link guard**: The FNLZR front-end dispatcher (`sub_4275C0`) checks `dword_2A5F314 > 0x59` (sm > 89) before invoking pre-link finalization. SM90 (value 90, `0x5A`) is the first architecture to pass this gate. The pre-link path processes individual cubins before the merge phase, running the full `sub_4748F0` engine (48,730 bytes) for architecture validation, ELF rewriting, and compilation unit setup.

2. **SASS mode flag**: The global flag `byte_2A5F225` is set to 1 when sm > 89 (`sub_427AE0` line 1058). This switches the output pipeline from PTX to native SASS, which means the embedded ptxas backend always runs for SM90+. The compilation mode (`dword_2A5B528`) becomes 6 (SASS output).

3. **MercExpand backend**: The MercExpand dispatch at `sub_5FDDB0` runs for SM90 targets. The capability check at the top of the dispatch reads `target_desc[9]->byte_216` and `target_desc[9]->byte_864` to determine whether Mercury expansion is needed. For SM90, Mercury expansion processes the warpgroup MMA builtins (40 ZREPHEL entries covering `MERCURY_warpgroup_mma_sync_*`), barrier builtins (124+86 entries), and the fence/redux/elect builtins, producing SASS output rather than capmerc.

The key difference from SM100+: SM90 runs Mercury in the backend but outputs standard SASS. SM100+ outputs capmerc by default. The `--binary-kind` CLI flag at `0x1D41D94` makes this explicit:

```
--binary-kind <mercury|capmerc|sass>

Default on sm100+ is capmerc.
```

For SM90, `sass` is the default. The mercury and capmerc modes are available but not standard.

| Architecture | FNLZR Pre-Link | MercExpand Active | Default Binary Kind | Mercury Builtins |
|---|---|---|---|---|
| SM89 (Ada) | No (sm = 89, fails > 89 check) | No | PTX/cubin | None |
| **SM90 (Hopper)** | **Yes** (sm = 90, passes > 89) | **Yes** | **sass** | 667 ZREPHEL entries |
| SM100 (Blackwell) | Yes | Yes | capmerc | 667+ tcgen05 |

## Architecture Identity

### Profile Registration in sub_484F50

The profile registration function `sub_484F50` (53,974 bytes) creates three profile objects per SM target via `sub_484DB0`. The SM90 registration at lines 512--602 of the decompiled source constructs:

```c
// Base sm_90 profile (line 512)
sm_90 = sub_484DB0(
    0,                              // is_virtual = false
    0,                              // is_lto = false
    "sm_90",                        // profile name
    "sm_90",                        // display name
    "Hopper",                       // ISA class
    "-D__CUDA_ARCH__=900",          // CUDA arch define
    "sm_90"                         // canonical name
);

// Virtual compute_90 profile (line 520)
compute_90 = sub_484DB0(
    1, 0,
    "compute_90", "compute_90",
    "Hopper",
    "-D__CUDA_ARCH__=900",
    "compute_90"
);

// LTO lto_90 profile (line 534)
lto_90 = sub_484DB0(
    1, 1,
    "lto_90", "compute_90",         // display = compute_90
    NULL,                            // no ISA class for LTO
    "-D__CUDA_ARCH__=900",
    "lto_90"
);
```

The sm_90a variant (lines 555--602):

```c
// Accelerated sm_90a profile
sm_90a = sub_484DB0(
    0, 0,
    "sm_90a", "sm_90a",
    "(profile_sm_90)->isaClass",     // inherits ISA class from base
    "-D__CUDA_ARCH__=900",           // SAME __CUDA_ARCH__ as sm_90
    "sm_90a"
);

// Accelerated compute_90a
compute_90a = sub_484DB0(
    1, 0,
    "compute_90a", "compute_90a",
    "(profile_sm_90)->isaClass",
    "-D__CUDA_ARCH__=900",
    "compute_90a"
);

// Accelerated lto_90a (note: uses "90a0" suffix)
lto_90a = sub_484DB0(
    1, 1,
    "lto_90a", "compute_90a",
    NULL,
    "-D__CUDA_ARCH__=90a0",          // "a0" suffix on LTO define
    "lto_90a"
);
```

After construction, the code sets the suffix flags (line 592):

```c
sm_90a->byte[4] = 1;        // suffix_a_flag
compute_90a->byte[4] = 1;   // suffix_a_flag on virtual profile too
```

### Profile Summary

| Field | sm_90 | sm_90a | compute_90 | compute_90a |
|---|---|---|---|---|
| Name | `"sm_90"` | `"sm_90a"` | `"compute_90"` | `"compute_90a"` |
| ISA class | `"Hopper"` | `"(profile_sm_90)->isaClass"` | `"Hopper"` | `"(profile_sm_90)->isaClass"` |
| `__CUDA_ARCH__` | 900 | 900 | 900 | 900 |
| LTO define | `-D__CUDA_ARCH__=900` | `-D__CUDA_ARCH__=90a0` | -- | -- |
| byte[3] (graphics) | 0 | 0 | -- | -- |
| byte[4] (suffix_a) | 0 | **1** | 0 | **1** |
| Is virtual | No | No | Yes | Yes |
| Forward-compatible | **Yes** | **No** (arch-locked) | Yes | No |

The `"(profile_sm_90)->isaClass"` string for sm_90a is a literal in the binary at `0x1d40b0f`, not a macro expansion. It is a debug-friendly name indicating ISA inheritance from the base sm_90 profile.

### Dispatch Table Registration

SM90 is identified as ISA class "Hopper" in the dispatch table registered by `sub_15C0CE0`. The seven callback slots for sm_90 and sm_90a point to the same functions:

| Slot | Callback | Role |
|---|---|---|
| 0 | nv.info emitter | Per-kernel EIATTR record generation |
| 1 | Resource usage table | Register/shared-memory accounting |
| 2 | Instruction encoding table | SASS binary encoding initializers |
| 3 | Compute capability array | CC version constants |
| 4 | Perf-stats handler | Performance statistics emission |
| 5 | cpf_optx handler | Compiler pass framework integration |
| 6 | Codegen options | SM-specific optimization knobs |

The detailed callback addresses for sm_89 vs sm_90 vs sm_90a:

| Slot | sm_89 | sm_90 / sm_90a | Functional Difference |
|---|---|---|---|
| B8 (Pre-compilation) | `sub_15C2D40` | `sub_15C2CE0` | Identical behavior |
| B0 (Compilation) | `sub_15C2C20` | `sub_15C2B30` | Identical behavior |
| A8 (Backend init) | `sub_15C3740` | `sub_15C3520` | **Different** -- resource limits and cluster support |
| A0 (Internal version) | 29 | 30 | Different integer constant |
| 90 (Perf-stats) | `sub_15C1F90` | `sub_15C1ED0` | Identical behavior |
| 88 (Resource calc) | `sub_15C2370` | `sub_15C2290` | Identical algorithm |

The `sm_90a` variant is not distinguished at the dispatch-table level. Instead, `sub_1100E50` (the feature flag configurator) tests the parsed SM version number and sets per-feature booleans. For sm_89/90, feature 33 is enabled when the SM internal version equals 29 or 30 and flag 618 (suppress-debug-info) is not set. This corresponds to the HMMA/WMMA tensor core extensions.

## The "a" Suffix -- Architecture-Accelerated

SM90a is the first architecture to carry the `a` (accelerated) suffix. The meaning, confirmed across all three NVIDIA tools:

**sm_90a SASS executes only on the specific silicon it was compiled for (H100/H200) and will not run on any future architecture.** This trades forward compatibility for access to features that may not survive to the next generation.

Evidence from the binary:

1. **Profile byte[4]**: Set to 1 for sm_90a (decompiled line 592: `v64->m128i_i8[4] = 1`). Not set for sm_90 base. This byte propagates through the ELF output pipeline and marks the cubin as arch-locked.

2. **Capability vector inheritance**: sm_90a copies capability vectors from sm_90 via `_mm_loadu_si128` (lines 595--599) rather than loading independent vectors from rodata. The suffix variant inherits the base's capabilities exactly.

3. **Compatibility list linking**: Lines 600--603 cross-link sm_90a into sm_90's compatibility lists. The `a` variant is linked bidirectionally with the base, but only within the sm_90 family. It is never linked to sm_100 or later families.

4. **__CUDA_ARCH__ identity**: Both sm_90 and sm_90a define `__CUDA_ARCH__=900`. The distinction is in LTO mode only: sm_90a uses `-D__CUDA_ARCH__=90a0` for LTO compilation, where the `a0` suffix triggers accelerated-mode code paths in the compiler.

In ptxas (standalone), sm_90a appears in the accelerated validation table (`unk_1D161C0`, 7 entries). sm_90 and sm_90a share all 7 dispatch-table handler functions. The `a` suffix does not produce different handler code paths -- it produces different compatibility metadata in the output cubin. The ELF header records whether the binary is forward-compatible (base) or arch-locked (accelerated), and the CUDA driver enforces this at load time.

In cicc, the sm_90a variant is the only pre-Blackwell SM that uses PTX version 6; all sm_20 through sm_90 base variants use PTX version 5. The `a` flag is stored in `unk_4D045E4` and read in exactly one location: `sub_6C4D80` line 167, where the check `unk_4D045E8 != 90 || !unk_4D045E4` gates a specific sm_90a-only feature (error code 0xE90 = 3728).

Compilation rules:
- `sm_90a` PTX **must** be compiled to `sm_90a` SASS (no cross-arch)
- `sm_90` PTX can compile to `sm_90` or any later SASS target
- No `sm_90f` variant exists; the `f` suffix starts with Blackwell (sm_100f)

## Capability Vectors

The three 128-bit capability vectors at profile offsets +80, +96, and +112 control finalization compatibility and feature gating. SM90's vectors differ from SM89 (Ada) in a significant way:

| Architecture | Vector 0 (+80) | Vector 1 (+96) | Vector 2 (+112) |
|---|---|---|---|
| sm_89 (Ada) | `xmmword_1D40F10` | `xmmword_1D40F60` | `xmmword_1D40F30` |
| **sm_90 (Hopper)** | `xmmword_1D40F10` | **`xmmword_1D40F40`** | `xmmword_1D40F30` |
| sm_100 (Blackwell) | `xmmword_1D40F10` | `xmmword_1D40F40` | `xmmword_1D40F70` |

**Critical observation**: sm_90 shares Vector 1 (`xmmword_1D40F40`) with sm_80 (Ampere) and sm_100 (Blackwell), not with sm_89 (Ada). Ada uses `xmmword_1D40F60`, a distinct capability set. This has a direct consequence for the finalization pipeline: code compiled for sm_80 can be finalized for sm_90 (same Vector 1), but code compiled for sm_89 cannot be directly finalized for sm_90 without the compatibility check in `sub_470DA0` mapping it through the capability bitmask.

The source of this asymmetry: sm_89 (Ada) is a graphics-capable architecture (tessellation, geometry shading, rasterization) while sm_90 (Hopper) is datacenter-only. Ada's extended Vector 1 encodes graphics pipeline capabilities that Hopper does not have. Hopper's Vector 1 matches Ampere because both are compute-focused architectures with compatible feature sets.

Vector 2 (`xmmword_1D40F30`) is shared across all Turing through Hopper architectures. Blackwell (sm_100+) switches to `xmmword_1D40F70` for Vector 2, reflecting the Mercury encoding format change.

All suffix variants (sm_90a) inherit vectors by `_mm_loadu_si128` copy from their base, not from independent rodata loads.

## FNLZR Finalization for SM90

The FNLZR (finalizer) subsystem processes SM90 cubins through the pre-link path. The architecture guard in `sub_4275C0` is `dword_2A5F314 > 0x59` (sm > 89), placing SM90 as the first architecture to undergo finalization.

### Pre-Link Path

When processing an SM90 cubin input:

1. **Architecture gate**: `dword_2A5F314` (= 90) > `0x59` (= 89) passes.
2. **ELF flags check**: The dispatcher reads `e_flags` from the ELF header. For standard cubins (type `0x07`), bit 14 (`0x4000`) and bit 31 are checked. For Mercury ELFs (type `0x41`), bit 0 is checked.
3. **Engine invocation**: `sub_4748F0` runs the 10-phase pipeline: environment setup, architecture validation, fastpath optimization, compilation unit initialization, per-function compilation via the embedded ptxas, and ELF output.
4. **Fastpath optimization**: If source and target architectures share compatible capability bitmasks (via `sub_470DA0`), the engine copies the input ELF verbatim, patching only the architecture field in the header.

### Architecture Validation

Phase 2 of the FNLZR engine validates the input ELF:

- **ELF type byte**: `0x41` ('A') = Mercury ELF, `0x07` = standard device cubin. SM90 cubins in standard SASS mode use type `0x07`.
- **Profile version ceiling**: Version > `0x101` (1.1) causes rejection with error code 25.
- **Subtype validation**: Must be Mercury (`0xFF00`) with subtype 1 or 2.

### Post-Link Path

SM90 cubins do not undergo post-link capmerc transformation by default. The post-link path (`a5=1` in `sub_4275C0`) is gated by `byte_2A5F222`, which is set only when sm > 99. SM90 (= 90) does not pass this gate. The post-link transformation that converts SASS to capmerc format is strictly a Blackwell+ operation.

## Mercury Detection for SM90 Cubins

nvlink detects Mercury format through multiple complementary mechanisms:

1. **ELF type byte at offset +7**: Mercury ELFs use `0x41` ('A'). Standard cubins use `0x07`. SM90 cubins in default `sass` mode use `0x07` but may contain Mercury sections if explicitly compiled with `--binary-kind mercury`.

2. **`sub_43DA40` (Mercury detection)**: Called from the main input loop (line ~727) as part of the guard `sm > 0x59 && (!sass_mode || sub_43DA40(elf))`. This function inspects the ELF for Mercury-format sections (`.nv.merc.*` prefixed sections).

3. **ELF flags encoding**: The architecture is encoded differently in Mercury vs standard ELFs:
   - Standard cubin: arch in low byte of `e_flags`
   - Mercury ELF: arch in bits 8..23 of `e_flags`

4. **EIATTR_MERCURY_ISA_VERSION**: SM90 cubins that use Mercury builtins contain this EIATTR record, which specifies the Mercury ISA version used for the instructions.

## sm_90 vs sm_89 Differences

Although SM89 and SM90 share the same 1.9 MB backend, the binary distinguishes them at several levels. This table summarizes the complete catalog (detailed analysis in [SM89 Ada](sm89-ada.md)):

| Aspect | SM89 Ada | SM90 Hopper |
|---|---|---|
| ISA class string | `"Ada"` | `"Hopper"` |
| Profile byte[3] (graphics) | **1** (graphics-capable) | 0 |
| Profile byte[4] (suffix_a) | 0 | 0 (1 for sm_90a) |
| Internal SM version | 29 | 30 |
| Backend init +348 (resource limit) | `0x7005` (28677) | `0x8000` (32768) |
| Capability Vector 1 | `xmmword_1D40F60` | `xmmword_1D40F40` (same as sm_80) |
| Thread Block Clusters | Not supported | Supported (`--blocks-are-clusters`) |
| Fixed-function graphics | Supported (tessellation, raster) | Not supported (datacenter only) |
| FNLZR pre-link eligible | No (sm = 89, fails > 89) | **Yes** (sm = 90, passes > 89) |
| MercExpand active | No | **Yes** |
| SASS mode (`byte_2A5F225`) | Not set | Set |
| `-D__CUDA_ARCH__` | `890` | `900` |
| Feature 33 | Enabled (unless debug) | Enabled (unless debug) |
| `--blocks-are-clusters` gate | Absent from backend init | Present: zeroes context[107] when set |
| Shared memory limit | 100 KB/SM | 228 KB/SM |

## Instruction Format

SM90 uses 128-bit (16-byte) instruction words, consistent with the format introduced in SM80 (Ampere). Every instruction occupies exactly two 64-bit words written to the output buffer at `*(a1+40)`:

```
Bit layout (128-bit SASS instruction word):

Word 0 (bits 0-63):
  [15:18]   Destination register number (8-bit, encoded via sub_A50D10)
  [12:14]   Register bank / sign bit (3-bit, via sub_A50CF0)
  [varies]  First and second source register fields
  [varies]  Opcode template bits (OR'd from xmmword constant)

Word 1 (bits 64-127):
  [varies]  Additional source operands
  [varies]  Modifier fields (rounding mode, saturation, FTZ, data type)
  [varies]  Predicate destination and source fields
```

The opcode template is loaded as a 128-bit SSE constant (`xmmword_1E5B2F0`, `xmmword_1E5B2C0`, etc.) and OR'd into the output buffer via `_mm_or_si128`. Each encoder function uses a unique template constant that establishes the base opcode bits, with operand fields packed on top.

## Register Encoding

SM90 uses the same register sentinel scheme as SM80+:

| Field | Width | Valid Range | Sentinel | Internal Mapping |
|---|---|---|---|---|
| GPR (general purpose) | 8 bits | 0--254 | 255 | Maps to 1023 (= RZ, zero register) |
| Predicate register | 5 bits | 0--6 | 7 | Maps to 31 (= PT, true predicate) |
| Uniform register | 8 bits | 0--62 | 63 | Maps to URZ |

The encoding helper `sub_A50D10(arch, value)` packs a register number into the destination field. `sub_A50CF0(arch, value)` encodes the bank select bit. `sub_A50CD0(arch, value)` encodes a flag bit (negate or absolute-value modifier). The sentinel value 1023 at operand offsets +36, +68, +100, or +132 in the operand array triggers substitution from the architecture context at `a1+8` / `a1+12`, which provides architecture-specific default register values.

## Operand Structure

Each operand is a 32-byte record within an operand array at `*(a2+32)`, indexed by `*(a2+40)`:

```
struct operand {             // 32 bytes
    uint32_t  kind;          // [+0]  operand type (register, immediate, const bank, ...)
    uint32_t  reg_num;       // [+4]  register number (1023 = unused sentinel)
    uint32_t  field_a;       // [+8]  secondary field (modifier, bank index)
    uint32_t  field_b;       // [+12] tertiary field
    uint32_t  field_c;       // [+16] type / class
    uint32_t  stride;        // [+20] register pair stride (1=single, 2=pair, 3=triple, 4=quad)
    uint64_t  imm_or_ptr;    // [+24] immediate value or pointer to constant
};
```

Source operands are accessed at base + 32*index, so operand 0 is at offset +0, operand 1 at +32, operand 2 at +64, and so on. The stride field at offset +20 (also found at operand+52, +84, +116, +148 in the full instruction descriptor) is critical for register allocation: a stride of 2 means the instruction requires consecutive register pairs (R0:R1, R2:R3, ...), stride 3 means triples, and stride 4 means quads. The WMMA/HMMA decoders set these extensively.

## Instruction Codec (0xA70000 -- 0xB80000)

The 1.1 MB region from `0xA70000` to `0xB80000` implements the complete instruction codec for SM90 -- the paired encoder/decoder functions that translate between the high-level IR representation and 128-bit binary machine words. This region contains no register allocation, no scheduling, and no peephole optimization code.

### Component Breakdown

| Range | Size | Count | Identity |
|---|---|---|---|
| `0xA709F0` | 54 KB | 1 | Field offset query (`sub_A709F0`, 6,491 lines) |
| `0xA7DE70` | 50 KB | 1 | Field presence query (`sub_A7DE70`, 6,240 lines) |
| `0xA853F0` | 3 KB | 1 | Operand type compatibility checker |
| `0xA87CE0`--`0xB25D50` | ~630 KB | ~164 | Per-opcode encoders |
| `0xACECF0`--`0xB77B60` | ~700 KB | ~139 | Per-opcode decoders |

### Field Query Functions

`sub_A709F0` and `sub_A7DE70` are the two largest functions in the codec. They implement giant switch tables mapping `(opcode_class, field_id)` pairs to either bit offsets or presence booleans.

`sub_A709F0` (InstrFieldOffset_Query): takes an instruction pointer and a field ID, switches on the opcode class at `*(a1+12)` (covering opcode classes 0x00 through 0x171, approximately 370 instruction classes), and for each valid `(class, field_id)` combination, returns the bit offset within the 128-bit instruction word where that field is encoded. The offset is computed as `sub_A4D370(a1+48, bitfield_index) + base_constant`, where the base constants (e.g., 790, 1278, 1942, 2476) represent bit positions. Returns `0xFFFFFFFF` (-1) when the field does not exist for the given opcode class.

`sub_A7DE70` (InstrFieldPresent_Query): identical switch structure, but every case body returns `sub_A4Dxxx(a1+48, idx) != 0` -- a boolean "does this field have a non-zero value" test. This is the `hasField` companion to `sub_A709F0`'s `getFieldOffset`.

Four bitfield extraction helpers are used by both functions, corresponding to different field widths:
- `sub_A4D270`: extract narrow bitfield
- `sub_A4D2F0`: extract medium bitfield (type B)
- `sub_A4D370`: extract medium bitfield (type A)
- `sub_A4D3F0`: extract wide bitfield
- `sub_A4D470`: extract extra-wide bitfield

### Operand Type Compatibility

`sub_A853F0` (259 lines) implements a pure type algebra function that determines valid register type combinations for paired operands. It takes `(type_a, type_b, query_mode)` and returns a compatibility code:

| Return | Meaning |
|---|---|
| 0 | Compatible |
| 4, 5, 6, 7, 8 | Specific incompatibility type |
| 10, 12 | Required conversion |

The type values 1--5 correspond to GPR, predicate, uniform, special register, and constant bank reference (inferred from the dispatch logic and register file size constants at each branch). The `query_mode` parameter (`a3`) selects between two interpretation modes.

### Encoder Functions (0xA87CE0 -- 0xB25D50)

The ~164 encoder functions follow a uniform pattern:

1. Load a 128-bit opcode template constant via `_mm_or_si128` (or scalar `|=` for some variants).
2. Extract operands from the 32-byte-stride operand array at `*(a2+32)`.
3. Pack register numbers, modifiers, and immediate values into specific bit positions in the 128-bit output word.
4. Handle register sentinel substitution (1023 -> architecture default).
5. Encode modifier bits (rounding mode, saturation, FTZ, data type, comparison predicate, memory ordering) via shared modifier-setter functions.

Size distribution of encoders:

| Line Count | Typical Instructions | Operand Count |
|---|---|---|
| 106--114 | Simple ALU, shifts, moves | 2--3 source operands |
| 118--136 | FP operations with rounding | 3--4 operands + modifiers |
| 143--170 | FMA, MAD, predicated ops | 5--7 operands |
| 216--335 | DMMA, paired-register ops | 6+ operands + pairing logic |

The encoder clusters are organized by instruction family:

| Range | Functions | Family |
|---|---|---|
| `0xA87CE0`--`0xA9E770` | ~25 | Core ALU / register-register |
| `0xAA0000`--`0xAAF000` | ~60 | Dense ALU cluster (integer, shift, logical) |
| `0xAB0000`--`0xABFF00` | ~52 | Memory operations (load, store, atomic) |
| `0xAC0000`--`0xACF000` | ~32 | Special / miscellaneous |
| `0xB00000`--`0xB0CC00` | ~36 | Complex multi-operand (texture, surface) |
| `0xB25000`--`0xB26300` | ~4 | Atomic shared-memory operations |

**Example: `sub_A87CE0` (Encode_3OpRRR_TypeA)**. This encoder handles a 3-operand register-register-register instruction. It OR's the 128-bit constant `xmmword_1E5B2F0` into the output, encodes the destination register at bits [15:18] via `sub_A50D10`, encodes the bank select at bits [12:14] via `<< 12 & 0x7000`, and processes three source operands at offsets +32, +64, and +96 from the operand array base. Helper functions `sub_A59C60`, `sub_A51200`, and `sub_A51220` extract operand values.

**Example: `sub_B0AA80` (Encode_DMMA_PairedReg, 335 lines)**. The largest encoder in this range handles double-precision MMA with paired register encoding. It contains a 40-entry if-else chain mapping register pairs: `if (result==1 && v59==0)`, `if (result==3 && v59==2)`, up to `(result==79 && v59==78)`. Each branch encodes an even:odd register pair (R0:R1, R2:R3, ..., R78:R79) as a single compact field. A 3-level modifier combination logic (`v48`, `v52`, `v54`) selects cache control bits.

### Decoder Functions (0xACECF0 -- 0xB77B60)

The ~139 decoder functions reverse the encoding process: they extract bit fields from a 128-bit instruction word and populate the IR instruction descriptor. The common decoder helpers are:

| Function | Role |
|---|---|
| `sub_4FF010` | Set up register operand (operand_idx, reg_class, is_dst, operand_type, reg_num) |
| `sub_4FF150` | Set up predicate operand (operand_idx, reg_class, is_dst, type, pred_num) |
| `sub_4FF280` | Set up immediate/constant operand (operand_idx, class, is_dst, type, imm_val) |
| `sub_4FF390` | Set up 5-bit immediate field |
| `sub_4FF480` | Set up 17-bit immediate field |
| `sub_50C790` | Decode predicate condition |

Modifier decoder functions configure instruction modifiers:

| Function | Modifier |
|---|---|
| `sub_5096E0` | Flush-to-zero (FTZ) |
| `sub_5095F0` | Negate |
| `sub_50A670` | Rounding mode |
| `sub_50C0F0` | Data type |
| `sub_509760` | Saturation |
| `sub_509200` | Saturation (variant) |
| `sub_50BD20` | Rounding (variant) |
| `sub_50C000` | Comparison mode |
| `sub_50C4F0` | Flush-to-zero (variant) |
| `sub_50B500` | Data type (variant) |

### Decoder Clusters

| Range | Functions | Identity |
|---|---|---|
| `0xACECF0` | 1 | **HMMA** (tensor core MMA, class 35) |
| `0xAF6000`--`0xB00000` | ~20 | FADD/FMUL/FP decoders (class 180) |
| `0xB00000`--`0xB0CC00` | ~10 | LDS/STS shared memory (classes 232, 191) |
| `0xB2A000`--`0xB2F000` | ~15 | ALU / LDGSTS (async copy, class 205) |
| `0xB30000`--`0xB39000` | ~12 | IMMA / tensor op decoders (classes 296, 297) |
| `0xB3A000`--`0xB40000` | ~15 | DFMA / DSET / HMMA_Large (class 295, 297) |
| `0xB40000`--`0xB4B000` | ~25 | SFU / TEX / TLD4 decoders |
| `0xB4C000`--`0xB54000` | ~22 | Miscellaneous ALU decoders |
| `0xB53000`--`0xB63000` | 3 | **WMMA monster decoders** (class 296, 2490--2842 lines each) |
| `0xB6B000`--`0xB7C000` | ~18 | Uniform register decoders (UIMAD, UFMA, UMOV) |

## Hopper Tensor Core Support (HMMA/WMMA)

The SM90 codec dedicates substantial code to tensor core instruction encoding and decoding, reflecting Hopper's enhanced tensor operations. Three categories of tensor instructions are present:

### HMMA (Hopper Matrix Multiply-Accumulate)

`sub_ACECF0` (128 lines) decodes the HMMA instruction (opcode class 35). It sets format bytes `*(_BYTE*)(a2+14) = 18` and `*(_BYTE*)(a2+15) = 19`, then calls MMA-specific modifier decoders (`sub_50F2B0`, `sub_50F2D0`, `sub_50C630`, `sub_50F570`, `sub_50F550`). The instruction has 6 register operands (operands 0--5), with operand class 10 indicating shared memory / matrix register type. Post-decode fixups set `operand[n].reg+1` for paired register allocation constraints. Opcodes 2038--2041 trigger variant-specific register dependency fixups.

### WMMA (Warp Matrix Multiply-Accumulate)

The three largest functions in the entire codec region are WMMA decoders, all for opcode class 296:

| Function | Lines | Format | Identity |
|---|---|---|---|
| `sub_B53830` | 2,490 | format 3 | WMMA (warp MMA) |
| `sub_B5AB00` | 2,837 | format variant | WMMA Extended |
| `sub_B62DE0` | 2,842 | format variant | WMMA Maximum |

Each decoder processes 7 register operands plus 1 predicate output and contains **hundreds** of post-decode register pairing fixup checks. Each check is a 5-way conjunction:

```
if (sub_A58D30() == X &&    // instruction variant
    sub_A58D50() == Y &&    // data type
    sub_A58C90() == Z &&    // precision
    sub_A58BC0() == W &&    // matrix layout
    sub_A58CD0() == V)      // accumulation mode
{
    operand[n].stride = 2;  // or 3, or 4
}
```

The five query functions retrieve the instruction variant, data type, precision mode, matrix layout, and accumulation mode respectively. When a combination matches, the stride field at operand offset +116 is set to 2, 3, or 4, constraining the register allocator to assign consecutive register pairs, triples, or quads. Referenced opcode variants include 2129--2134, 2532--2534, 2669, 2681--2683, and 2840--2841.

The combinatorial explosion in these decoders reflects the number of WMMA variants in the Hopper ISA: every combination of data type (FP16, BF16, TF32, FP64, INT8, INT4), matrix layout (row-major, column-major), precision (full, reduced), and accumulation mode generates a distinct register pairing constraint.

### IMMA (Integer Matrix Multiply-Accumulate)

Opcode class 297 handles integer MMA variants. Decoders at `0xB30940` (188 lines) and `0xB30FF0` (201 lines) handle the basic variants. The extended MMA decoder at `0xB40C30` (517 lines, the largest single decoder) handles all MMA modifiers including warp group configuration, data format, layout, and an extensive register pairing fixup section.

## WGMMA and TMA -- Hopper-Unique Features

Hopper introduces two hardware subsystems that are exposed in nvlink through the Mercury backend and the embedded ptxas:

### WGMMA (Warpgroup Matrix Multiply-Accumulate)

WGMMA operates on warpgroups (4 consecutive warps) rather than single warps, and executes asynchronously. In standalone ptxas (`sub_5D4190`), four PTX instructions implement WGMMA:

| PTX Instruction | ptxas Codegen Handler | Formatter Size |
|---|---|---|
| `wgmma.mma_async` | `sub_50AC70` | 295B |
| `wgmma.fence` | `sub_4DA380` | 295B |
| `wgmma.commit_group` | `sub_4DA4B0` | 311B |
| `wgmma.wait_group` | `sub_4DA5E0` | 1066B |

In cicc, four WGMMA builtins are registered (`sub_90AEE0`, lines 2941--2944):

| Builtin | ID | Accumulator Type |
|---|---|---|
| `__wgmma_mma_async_f16` | 765 | FP16 |
| `__wgmma_mma_async_bf16` | 766 | BF16 |
| `__wgmma_mma_async_tf32` | 767 | TF32 |
| `__wgmma_mma_async_f8` | 768 | FP8 |

In nvlink's Mercury backend, WGMMA maps to the 40 `ZREPHEL_jnectebhc_zzn_flap_*` (decoded: `MERCURY_warpgroup_mma_sync_*`) instruction templates. These are expanded by MercExpand into the concrete SASS warpgroup MMA sequences, with fence/commit/wait synchronization automatically injected.

The WGMMA pipeline optimizer in standalone ptxas spans ~100 KB across 15+ functions (`0xACE000`--`0xAE6000`) and is the largest single-architecture compiler subsystem. It is active only for SM90+ targets. The fence insertion pass (`sub_ADEB40`, 43.1 KB) automatically injects `warpgroup.arrive` and `warpgroup.wait` instructions to manage register ownership between the warpgroup's register file and the tensor core's accumulator registers.

### TMA (Tensor Memory Accelerator)

TMA provides hardware-accelerated bulk data movement between global and shared memory. In standalone ptxas, the `cp.async.bulk.tensor` codegen handler (`sub_5AB460`, 45 KB) is one of the largest single-instruction handlers, supporting 1D through 5D tensors in tile and im2col modes with unicast/multicast variants.

In cicc, TMA is exposed through `cp.async.bulk.tensor` intrinsics spanning 16+ tile/im2col combinations from 1D to 5D (intrinsic opcodes 8324--8331, 9213--9226), plus unstructured bulk copies (`cp.async.bulk.global.to.shared.cluster`, opcode 8315). The CpAsyncBulkTensor G2S lowering at `sub_36EC510` (27 KB, 1185 lines) gates features by architecture: SM90 unlocks tile mode (1D--5D) and Im2Col mode (3D--5D); SM100+ adds 2CTA mode, Im2Col_W, and Im2Col_W128.

In nvlink's Mercury backend, TMA operations map to `MERCURY_mbarrier_arrive` (124 templates) and fence instructions (`MERCURY_fence_mbarriers`, 32 templates) for the asynchronous synchronization protocol.

## Thread Block Clusters

Hopper introduces thread-block clusters -- groups of cooperating CTAs that access each other's shared memory (distributed shared memory). This is the feature gated by the `--blocks-are-clusters` flag at offset a2+355 in the SM90 backend init (`sub_15C3520`), absent from SM89's init (`sub_15C3740`).

In nvlink, the `EIATTR_BLOCKS_ARE_CLUSTERS` attribute (code 91, `0x5B`) records cluster configuration in the per-kernel `.nv.info` section. The cluster support infrastructure flows through:

1. **PTX directives**: `.blocksareclusters`, `.explicitcluster`, `.reqnctapercluster X,Y,Z`, `.maxclusterrank N`
2. **Special registers**: `%clusterid`, `%nclusterid`, `%cluster_ctaid`, `%cluster_nctaid`, `%cluster_ctarank`, `%cluster_nctarank`, `%is_explicit_cluster`, `%aggr_smem_size`
3. **Distributed shared memory**: `.shared::cta` (CTA-local) vs `.shared::cluster` (cross-CTA within cluster)
4. **Atomic cluster scope**: `atom.*.cluster` operations for intra-cluster synchronization (scope value 2 resolves to `"cluster"` on sm_90+, vs `"gpu"` on sm_70--89)

In cicc, all cluster functionality is gated at `arch_id >= 90` (`unk_4D045E8 > 89`). Three cluster-related kernel attributes are recognized: `__cluster_dims__`, `__launch_bounds__` 3rd parameter, and `__block_size__` with cluster dimension. On sm_89 and below, these emit warning diagnostics (3687, 3704, 3790).

## Uniform Register Decoders (0xB6B000 -- 0xB7C000)

Fourteen decoders in this range handle uniform-register instructions (UIMAD, UFMA, UIADD, UMOV). These use a distinctive bitmap-based register class membership test: each decoder loads 24 x 128-bit constants (384 bytes of bitmap data) from read-only data and tests register numbers against the bitmap using bit-shift operations:

```c
// Bitmap membership test for register class
bool is_class_member = (0x1668166816681660ull >> reg_num) & 1;
```

Functions `sub_403941` and `sub_4038C0` implement the bitmap membership test on packed 128-bit bitset arrays. The bitmap determines the required operand stride:

| Stride | Register Width | Use Case |
|---|---|---|
| 2 | 64-bit (paired) | Standard double-width operations |
| 3 | 96-bit (triple) | Triple-wide uniform registers |
| 4 | 128-bit (quad) | Quad-wide uniform registers (256-bit) |

### Uniform Instruction Classes

| Opcode Class | Mnemonic | Decoders | Line Range |
|---|---|---|---|
| 211 | UIMAD | `sub_B6B0F0`, `sub_B6B9F0`, `sub_B6C310` | 229--248 |
| 230 | UFMA | `sub_B6CC70`, `sub_B6EE10`, `sub_B71020`, `sub_B75640`--`sub_B77B60` | 324--389 |
| 285 | UIADD | `sub_B6D790`, `sub_B6E2D0`, `sub_B6F960`, `sub_B704C0`, `sub_B71B70` | 324--331 |
| 34 | UMOV | `sub_B726D0`, `sub_B732A0`, `sub_B73E70`, `sub_B74A50` | 320--326 |

## Instruction Class Reference

The following instruction classes have been identified in the SM90 codec through decoder analysis:

| Class ID | Mnemonic | Type | Notes |
|---|---|---|---|
| 2 | MOV | Data movement | Register-to-register move |
| 34 | UMOV | Uniform data movement | Uniform register move |
| 35 | HMMA | Tensor core | Half-precision matrix multiply-accumulate |
| 90 | PRMT | Bit manipulation | Byte permute |
| 121 | BRA | Control flow | Branch |
| 126 | BAR | Synchronization | Barrier |
| 143 | NOP | Control | No-operation |
| 173 | RET | Control flow | Return |
| 180 | FADD/FMUL | Floating point | FP add / multiply |
| 191 | STS | Memory | Store to shared memory |
| 195 | DEPBAR | Scheduling | Dependency barrier |
| 205 | LDGSTS | Memory | Load-global-store-shared (async copy) |
| 211 | UIMAD | Uniform integer | Uniform integer multiply-add |
| 227 | VOTE | Warp | Warp vote |
| 230 | UFMA | Uniform FP | Uniform FP multiply-add |
| 232 | LDS | Memory | Load from shared memory |
| 280 | EXIT | Control flow | Kernel exit |
| 285 | IADD/UIADD | Integer | Integer add / uniform integer add |
| 289 | HMMA_ALU | Tensor core | Hopper matrix ALU |
| 290 | DFMA_DP | Floating point | Double-precision FMA |
| 292 | MUFU | Special function | Multi-function unit (sin, cos, rsq, ...) |
| 293 | I2F/F2I | Conversion | Integer-float conversion |
| 295 | DFMA | Floating point | Double-precision FMA (extended) |
| 296 | WMMA | Tensor core | Warp matrix multiply-accumulate |
| 297 | IMMA | Tensor core | Integer matrix multiply-accumulate |
| 298 | QSPC | Special | Quasispecific operation |
| 299 | DP4A | Tensor core | Dot-product 4-element accumulate |
| 300 | HADD2 | Floating point | Half-precision add x2 |
| 301 | TEX | Texture | Texture fetch |
| 303 | TLD | Texture | Texture load |
| 315 | YIELD | Control | Thread yield |
| 316 | SSY | Control flow | Set synchronization point |
| 319 | CAL | Control flow | Call |
| 325 | PBK | Control flow | Push breakpoint |
| 327 | PCNT | Control flow | Push counter |
| 368 | BSSY | Synchronization | Barrier set synchronization |

## Shared SM89/90 Backend (0x100C000 -- 0x11EA000)

The 1.9 MB region at `0x100C000`--`0x11EA000` contains the complete backend for both SM89 and SM90 architectures. It decomposes into five functional layers:

### Backend Address Map

| Range | Size | Functions | Identity |
|---|---|---|---|
| `0x100C000`--`0x10FFFFF` | ~1.0 MB | ~750 | Shared instruction encoder templates |
| `0x1100000`--`0x1120000` | ~128 KB | ~30 | Backend driver (option parsing, codegen orchestration, ELF output) |
| `0x1120000`--`0x119BF40` | ~496 KB | ~160 | ISel pattern matchers |
| `0x119BF40` | ~231 KB | 1 | **ISel mega-hub** (too large for Hex-Rays) |
| `0x11D4680`--`0x11EA000` | ~90 KB | ~16 | Instruction scheduling + emission |

### Instruction Encoder Templates (0x100C000 -- 0x10FFFFF)

Approximately 750 functions, each 4--8.5 KB, implement instruction encoding table initializers. Every function follows the same template:

1. `sub_4C28B0(a1, offset, fieldlen, value)` -- set bitfield parameters (5--8 calls per function).
2. SSE load from global constant table (`xmmword_1F46xxx`) -- instruction signature.
3. Copy loop: 3 parallel arrays (10 entries each) from read-only data into the instruction descriptor at `a1+24` through `a1+140`.
4. `sub_4C60F0(a1, a2, slot, offset, type)` -- configure control code slots.
5. `sub_4C5F90(a1, a2)` -- finalize the descriptor.
6. `sub_50xxxx` family calls -- set modifier bits (predicate via `sub_50C790`, rounding via `sub_50E300`, FTZ via `sub_50E320`, etc.).

Size clusters by instruction complexity:

| Size Range | Instruction Type | Count |
|---|---|---|
| 4,700--6,200 bytes | Simple (moves, branches, simple math) | ~100 |
| 7,400--7,700 bytes | Standard 3-source ALU | ~400 |
| 7,800--8,100 bytes | ALU with extra modifiers (rounding, saturate) | ~150 |
| 8,300--8,500 bytes | Complex (texture, surface, atomics) | ~100 |

The constant tables reside in `.rodata` at `0x1F460E0`--`0x1F47400`. Each table contains 10 source-register-class entries (40-byte stride), 10 destination-register-class entries, 10 control-code entries, and a 16-byte SSE header with the instruction signature.

### Backend Driver (0x1100000 -- 0x1120000)

The ~30 functions in this range implement the compilation pipeline controller for SM89/90 targets:

**`sub_1112F30` (65,018 bytes, 2,164 lines) -- Main Compilation Driver.** This is the top-level per-module compilation entry point. It reads command-line options (`def-load-cache`, `force-load-cache`, `position-independent-code`), writes PTX headers (`.version`, `.target`, `.entry __cuda_dummy_entry__ { ret; }`), validates SM version compatibility (`--legacy-bar-warp-wide-behavior` requires sm_70+, tensor-memory-access-check is gated by target arch), and dispatches per-function codegen. The function selects codegen callbacks based on mode flags: `sub_110CD20` for compile-only, `sub_110D110` for multi-function, and `sub_110CBA0` / `sub_110D0B0` for standard mode. Multi-threaded compilation is supported via `sub_464AE0` (thread pool) and `sub_464C30`.

**`sub_1116890` (59,847 bytes, 1,998 lines) -- ELF Output and Metadata Generator.** Handles CUBIN output, builds JSON metadata trees (`version`, `metadata`, `type`, `min`, `max`), and integrates with `sub_1CFA200` / `sub_1CFA220` / `sub_1CFA2D0` for JSON object creation. Uses `setjmp`/`longjmp` for error recovery.

**`sub_1104950` (37,578 bytes, 1,208 lines) -- Option Parser.** Registers approximately 60 ptxas options via `sub_42E390`: `warn-on-double-precision-use`, `maxrregcount`, `opt-level`, `fast-compile`, `device-stack-protector`, `sanitize`, `position-independent-code`, `g-tensor-memory-access-check`, `query-controls`, `apply-controls`, and others. Validates option compatibility with target architecture and computes SM architecture family from `dword_1EED2E0` lookup table.

**`sub_110AA30` (18,774 bytes, 661 lines) -- Per-Function Codegen Init.** Sets up virtual table pointers (5 callback slots at offsets 24, 1544, 1568, 1584, 1600), the `"NVIDIA"` vendor string (offset 1200), and `"ptxocg.0.0"` producer string (offset 64). Magic value `38156003` at offset 48 serves as a tool ID. Feature flags are enabled by SM version: SM >= 14 enables extended features, SM >= 17 enables SM100-specific paths.

**`sub_1100E50` (13,759 bytes, 451 lines) -- Feature Flag Configurator.** Reads the SM version via `sub_15C3DD0(gpu_name)` and configures approximately 30 boolean feature flags. SM version 29 or 30 (corresponding to sm_89/sm_90) enables feature 33 (tensor core extensions) when debug suppression is not active. Uses `sub_16E3AA0` to set flags in the feature table at `a1+1096`.

**`sub_110BC90` (18,111 bytes, 763 lines) -- Register Allocation and Launch Configuration.** Reads thread-block dimensions (`blockDim.x/y/z` from `v9[6..8]`), computes total threads, handles `maxntid` and `minnctapersm` overrides, and performs complex register budget computation with multiple fallback paths. SM version range checks gate architecture-specific features.

### ISel Pattern Matchers (0x1120000 -- 0x119BF40)

Approximately 160 small functions implement pattern-matching rules for the SM89/90 instruction selector. Each function:

1. Takes `(match_context, ir_node, result_opcode*, result_priority*)` parameters.
2. Calls `sub_A49150(a1, a2, field_id)` to extract IR node properties.
3. Compares extracted values against known SASS opcode requirements through nested if-chains.
4. If all constraints match, writes the selected SASS opcode to `*a3` and sets the priority in `*a4`.

The field IDs map to IR node attributes:

| Field ID | Decimal | Attribute |
|---|---|---|
| `0x1DF` | 479 | Primary opcode class |
| `0x1DE` | 478 | Secondary opcode |
| `0x1C4` | 452 | Operation variant |
| `0x1A3` | 419 | Data type |
| `0x20` | 32 | Addressing mode |
| `0xD0` | 208 | Memory space |
| `0x80` | 128 | Result type |
| `0x31` | 49 | Comparison operator |
| `0xDF` | 223 | Modifier flags |
| `0x7B` | 123 | Texture operation type |

Register class 1023 acts as a wildcard ("any class"). Priority values (e.g., 16) determine preference when multiple patterns match the same IR node. Operand type predicates are checked via `sub_1119410` (register class), `sub_1119420` (is register), `sub_1119430` (is predicate), `sub_1119450` (is general register), `sub_1119490` (is immediate), and `sub_11194A0` (is constant bank reference).

### ISel Mega-Hub (0x119BF40)

`sub_119BF40` (225,792 bytes, estimated 7,500+ lines) is the master instruction selection dispatch function for SM89/90 targets. It is too large for Hex-Rays to decompile. Located immediately after the ISel helper functions, it contains a massive switch/jump-table on the IR opcode that calls the ~160 pattern matchers and selects the highest-priority match for each IR node. The protocol is uniform across all ISel backends:

```
for each pattern_matcher in sm89_90_table:
    matched = pattern_matcher(ctx, ir_node, &pattern_id, &priority)
    if matched && priority > best_priority:
        best_priority = priority
        best_id = pattern_id
emitter_table[best_id](ctx, ir_node)
```

### Instruction Scheduling (0x11D4680 -- 0x11EA000)

The final 16 functions form a cohesive instruction scheduling and emission subsystem. Five functions exceed 7 KB and share an identical data-structure pattern:

| Function | Size | Identity |
|---|---|---|
| `sub_11D6890` | 13,175 bytes | Main basic-block scheduler |
| `sub_11D6080` | 11,782 bytes | Scheduling dependency check |
| `sub_11D5940` | 10,364 bytes | Per-block scheduling initialization |
| `sub_11D52B0` | 9,111 bytes | Scheduling state query (checks for value 711) |
| `sub_11D4AF0` | 10,679 bytes | Scheduling state update |

All five use:
- 184-byte per-basic-block records stored in a growable array at offset +832.
- Capacity tracking at offset +840.
- Overflow entries in a hash-table / linked-list at offset +864.
- 192-byte scheduling contexts allocated from an arena at offset +848.
- Virtual-dispatch cleanup via a vtable at offset +32.

This is a classic list-scheduling implementation that tracks data dependencies (RAW/WAR/WAW hazards), models instruction latencies, manages register pressure, and inserts control-flow barriers.

## Compilation Call Graph

The key call paths for the SM89/90 backend:

```
sub_1116890 (ELF output entry)
  -> sub_1104950 (parse options)
  -> sub_1112F30 (main compilation driver)
     -> sub_110AA30 (per-function codegen init)
        -> sub_1100E50 (feature flag setup)
        -> sub_110BC90 (register/launch config)
     -> sub_110D2A0 (per-function output finalization)
        -> sub_14075D0 / sub_1407FC0 / sub_14091C0 (encoding passes)
     -> sub_1109180 / sub_1107F10 (function list processing)
        -> sub_11078F0 (register analysis)
     -> sub_1111DB0 (symbol table building)
        -> sub_110FA30 (symbol resolution)
           -> sub_110EF30 / sub_110E7E0 (expression walkers)
  -> sub_1109EB0 (trace JSON integration)
  -> sub_1103030 (option table builder)

sub_119BF40 (ISel mega-hub)
  -> sub_1190050 .. sub_119B8F0 (pattern matchers)
  -> sub_100C110 .. sub_10FFFFF (instruction encoder templates)
     -> sub_4C28B0, sub_4C60F0, sub_4C5F90 (field setup)
     -> sub_50xxxx (modifier setup)

sub_11D6890 (block scheduler)
  -> sub_11D6080 (dependency query)
  -> sub_11D5940 (init)
  -> sub_11D4AF0 (update)
  -> sub_11D52B0 (query)
```

## Key Global Data

| Address | Type | Identity |
|---|---|---|
| `xmmword_1E5B2C0`--`xmmword_1E5C1xx` | 128-bit constants | Opcode template constants for encoders |
| `dword_1E3CBD0`, `dword_1E3CBE0` | Lookup tables | Modifier value encoding tables |
| `xmmword_1F460E0`--`0x1F47400` | Constant tables | Instruction encoding parameter tables |
| `dword_1EED2E0` | Lookup table | SM version -> architecture family mapping |
| `off_1EEEFA0` | Descriptor table | ELF metadata field descriptors |

## Confidence Assessment

| Claim | Confidence | Verification |
|---|---|---|
| ISA class string "Hopper" for sm_90 | CONFIRMED | Decompiled `sub_484F50` line 517: `"Hopper"`; string at `0x1d40af0` |
| sm_90/sm_90a share dispatch table callbacks | CONFIRMED | Decompiled `sub_15C0CE0` lines 110-123: sm_90 and sm_90a use identical function pointers (`sub_15C2CE0`, `sub_15C2B30`, `sub_15C3520`) |
| `__CUDA_ARCH__=900` for both sm_90 and sm_90a | CONFIRMED | Decompiled `sub_484F50` lines 518, 561: both use `"-D__CUDA_ARCH__=900"` |
| sm_90a LTO define is `"-D__CUDA_ARCH__=90a0"` | CONFIRMED | Decompiled line 583: `"-D__CUDA_ARCH__=90a0"` |
| sm_90a byte[4] = 1 (suffix_a_flag) | CONFIRMED | Decompiled line 592: `v64->m128i_i8[4] = 1` |
| sm_90a capability vectors copied from sm_90 | CONFIRMED | Decompiled lines 595--599: `_mm_loadu_si128(v56 + 5/6/7)` copies from sm_90 |
| sm_90 Vector 1 = `xmmword_1D40F40` (same as sm_80) | CONFIRMED | Decompiled line 550: `v63 = _mm_load_si128(&v212)` where v212 was set at line 328 to `xmmword_1D40F40` |
| sm_89 Vector 1 = `xmmword_1D40F60` (different) | CONFIRMED | Decompiled line 499: sm_89 block loads `xmmword_1D40F60` |
| FNLZR pre-link guard: sm > 89 | CONFIRMED | From `sub_4275C0`: `dword_2A5F314 > 0x59`; documented in mercury/fnlzr.md line 14 |
| SM90 is first Mercury-capable target | HIGH | sm > 89 threshold makes SM90 (= 90) the first to pass; MercExpand confirmed active from mercury/overview.md line 22 |
| SM90 default binary-kind is `sass` | HIGH | From mercury/overview.md line 22: "Mercury format available but not default. SASS remains the standard output" |
| `byte_2A5F225` (SASS mode) set for sm > 89 | CONFIRMED | Multiple wiki sources cite `sub_427AE0` line 1058 |
| `byte_2A5F222` (Mercury mode) set only for sm > 99 | CONFIRMED | From mercury/compiler-passes.md line 250 |
| Shared SM89/90 backend at `0x100C000`--`0x11EA000` | HIGH | Same address range referenced in SM89 page; consistent with function catalog |
| SM90 instruction codec at `0xA70000`--`0xB80000` (1.1 MB) | HIGH | Address range consistent with decoder function addresses cited (e.g., `sub_ACECF0` for HMMA) |
| `sub_A709F0` field offset query (54 KB, 6,491 lines) | HIGH | Largest function in codec region; address consistent |
| `sub_A7DE70` field presence query (50 KB, 6,240 lines) | HIGH | Companion to `sub_A709F0` |
| 164 per-opcode encoders, 139 decoders | HIGH | Counts from systematic sweep of address ranges |
| WMMA monster decoders at `0xB53000`--`0xB63000` (2,490--2,842 lines each) | HIGH | Three largest functions in codec; opcode class 296 |
| Register sentinels: 255=RZ, 7=PT, 63=URZ | HIGH | Consistent with SASS encoding convention across SM80+ |
| 128-bit instruction words at `*(a1+40)` | HIGH | Decompiled codec functions use two 64-bit writes at consistent offsets |
| Instruction class reference table (37 identified classes) | HIGH | Opcode IDs from decoder analysis; class numbers from `*(a2+12)` |
| Compilation driver `sub_1112F30` (65 KB) shared with SM89 | HIGH | Same function referenced in SM89 page |
| Feature 33 enabled for sm_90 when debug off | HIGH | From `sub_1100E50`; SM90 internal version 30 falls in range 29-30 |
| 14 uniform register decoders at `0xB6B000`--`0xB7C000` | HIGH | Address range and function count from sweep |
| `--blocks-are-clusters` gate in sm_90 backend init only | CONFIRMED | Decompiled `sub_15C3520` contains byte+355 check; `sub_15C3740` does not |
| sm_90 backend init +348 = `0x8000` (32768) | CONFIRMED | Decompiled `sub_15C3520` sets `*(_DWORD *)(v6 + 348) = 0x8000` |
| EIATTR_BLOCKS_ARE_CLUSTERS code 91 (0x5B) | CONFIRMED | From elf/nv-info.md line 199 |

## Cross-References

### nvlink Internal
- [SM89 Ada](sm89-ada.md) -- shared SM89/90 backend (complete Ada vs Hopper difference catalog)
- [Mercury Overview](../mercury/overview.md) -- MercExpand engine, ZREPHEL builtins, Mercury pipeline passes
- [FNLZR (Finalizer)](../mercury/fnlzr.md) -- `sub_4275C0` front-end and `sub_4748F0` engine; pre-link guard `sm > 89`
- [Mercury Compiler Passes](../mercury/compiler-passes.md) -- UseMercSemantics/UseMercResources options, per-pass Mercury configuration
- [Architecture Profiles](arch-profiles.md) -- SM90 profile with capability vectors, `sub_484F50` registration
- [Compatibility](compatibility.md) -- capability vector comparison for finalization
- [Embedded ptxas Overview](../ptxas/overview.md) -- SM89/90 backend at `0x100C000`--`0x11EA000` in address map
- [Instruction Selection Hubs](../ptxas/isel-hubs.md) -- SM89/90 mega-hub `sub_119BF40` (231 KB)
- [Architecture Dispatch](../ptxas/arch-dispatch.md) -- SM90/SM90a vtable registration
- [ELF nv.info](../elf/nv-info.md) -- EIATTR_BLOCKS_ARE_CLUSTERS (code 91) and Mercury ISA version attributes
- [SM100 Blackwell](sm100-blackwell.md) -- successor architecture with Mercury as default format

### Sibling Wikis
- [ptxas: Ada/Hopper](../../ptxas/targets/ada-hopper.html) -- standalone ptxas SM90 Hopper target: WGMMA pipeline optimizer (100 KB), TMA codegen (45 KB), cluster directives, setmaxnreg, mbarrier extensions, warp geometry (16 warps / 240 slots)
- [cicc: SM90 Hopper](../../cicc/targets/sm90-hopper.html) -- cicc compiler SM90 target: cluster builtins, TMA descriptor format (NVVM container tag 401), WGMMA lowering (M-dimension switch), setmaxnreg validation, distributed shared memory qualifiers, atomic cluster scope
