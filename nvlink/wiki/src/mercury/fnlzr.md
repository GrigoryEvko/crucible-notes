# FNLZR (Finalizer)

The FNLZR subsystem is nvlink's embedded binary rewriter for Mercury-class targets (sm >= 100). It accepts a fully-linked or partially-linked device ELF, invokes the embedded ptxas/OCG compiler backend to re-emit SASS, and produces a transformed ELF suitable for the target architecture. FNLZR operates in two distinct modes -- pre-link mode, which processes individual cubins before they enter the merge phase, and post-link mode, which applies a capmerc (Capsule Mercury) transformation after the complete link. The name "FNLZR" appears verbatim in diagnostic messages (`FNLZR: Input ELF: %s`, `FNLZR: Pre-Link Mode`, etc.) and is gated behind bit 0 of `dword_2A5F308`, the `--edbg` verbose flags word.

The subsystem comprises two main functions: `sub_4275C0` (3,989 bytes), the front-end dispatcher that selects mode, builds a 160-byte configuration struct, and delegates to the engine; and `sub_4748F0` (48,730 bytes), the full-featured FNLZR engine that orchestrates architecture validation, memory allocation, compilation unit setup, ELF emission, and optional self-check verification. A third entry point, `sub_52DD50`, provides a JIT-specific wrapper that emits `FNLZR: JIT Path` diagnostics and routes through the same `sub_4748F0` engine.

## Key Facts

| Property | Value |
|---|---|
| Front-end dispatcher | `sub_4275C0` (3,989 bytes / 162 lines) |
| Core engine | `sub_4748F0` (48,730 bytes / 1,830 lines) |
| JIT wrapper | `sub_52DD50` (0x52DD50, ~600 bytes) |
| Architecture guard | sm > 89 (`dword_2A5F314 > 0x59`) for pre-link; sm >= 100 (`byte_2A5F222`) for post-link |
| Debug trace flag | Bit 0 of `dword_2A5F308` (set by `--edbg`) |
| Config struct size | 160 bytes (20 qwords, `v28[0..19]` in the decompilation) |
| Error channel | `sub_467460` with `"Internal error"` or filename-qualified `"Internal FNLZR error"` |
| Called by | `main()` (6 call sites), `sub_42AF40` (fatbin extraction, 2 sites), `sub_52DD50` (JIT) |

## Position in the Pipeline

```
Relocation Phase (sub_469D60)
  |
  v
Finalization Phase (sub_445000 -- ELF reindexing)
  |
  v
Output Serialization (sub_45BF00 / sub_45C920 -- write bytes)
  |
  v
*** FNLZR Post-Link (sub_4275C0, a5=1, Mercury sm>=100) ***    <-- this page
  |
  v
Final output file written to disk

-----  OR (pre-link path)  -----

Input cubin loaded from fatbin/file
  |
  v
*** FNLZR Pre-Link (sub_4275C0, a5=0, sm>89) ***    <-- this page
  |
  v
Architecture validation (sub_426570)
  |
  v
Merge Phase (sub_45E7D0)
```

## Front-End Dispatcher: sub_4275C0

### Signature

```c
int64_t sub_4275C0(
    uint64_t *elf_ptr,      // a1 -- pointer to in-memory ELF image pointer (modified in-place)
    const char *filename,   // a2 -- source filename, or NULL -> "in-memory-ELF-image"
    uint32_t target_arch,   // a3 -- dword_2A5F314 (target SM number, e.g. 100)
    uint64_t *output_ptr,   // a4 -- receives output ELF pointer (pre-link only; NULL for post-link)
    char post_link           // a5 -- 0=pre-link, 1=post-link
);
```

### Mode Selection

The dispatcher reads the ELF header flags at offset `+48` of the section header returned by `sub_448360(*a1)` and checks the ELF type byte at offset `+7`.

**Pre-link mode** (`a5 == 0`): Applied to individual cubins before they enter the merge phase. The guard condition checks whether the ELF already contains finalized SASS. For ELF type `0x41` ('A', the Mercury ELF class marker), the check is `(flags >> 2) & 1` -- if that bit is set, finalization has already been applied and the function returns an internal error. For non-Mercury ELF types, the check is `(flags & 0x80004000) == 0` -- if both the capmerc and SASS-present bits are clear, the ELF does not need finalization.

**Post-link mode** (`a5 == 1`): Applied after the full link+finalization pipeline has serialized the merged ELF. The guard checks whether the SASS-present or capmerc bit is set (the inverse mask from pre-link), confirming the ELF is indeed a Mercury binary that requires post-link transformation.

### Configuration Struct Construction

The dispatcher builds a 160-byte configuration struct (`v28[0..19]`) on the stack, zeroed with `memset`, then populates it based on the global linker state:

| Offset (qword index) | Field | Source |
|---|---|---|
| `v28[3]` bits 32..39 | Debug flag | `byte_2A5F310 != 0` (i.e. `-g` was passed) |
| `v28[3]` bits 40..47 | Line info suppression | `byte_2A5F210 != 0` |
| `v28[3]` low dword | Optimization level | 4 (normal) or 5 (debug mode with `byte_2A5F2A9`) |
| `v28[8]` low dword | Fallback opt level | 3 (when neither debug nor `byte_2A5F310`) |
| `v28[13]` byte 0 | capmerc transform flag | 1 if Mercury mode (`byte_2A5F222`) |
| `v28[13]` byte 1 | SASS-only flag | `byte_2A5F225 != 0` |
| `v28[13]` byte 2 | Always 1 | Constant |
| `v28[13]` byte 3 | Extended debug | `byte_2A5F224 != 0` |
| `v28[13]` byte 4 | Suppress debug info | `byte_2A5F223 != 0` |

### Diagnostic Output

When `(dword_2A5F308 & 1) != 0` (bit 0 of `--edbg`), the dispatcher emits a sequence of messages to `stderr`:

```
FNLZR: Input ELF: <filename>
FNLZR: Pre-Link Mode            (or "Post-Link Mode")
FNLZR: Flags [ <capmerc> | <sass> ]
FNLZR: Starting <filename>
  ... engine runs ...
FNLZR: Ending <filename>
```

The two flag values in `Flags [ %u | %u ]` are the capmerc-transform flag and the SASS-only flag respectively.

### Invocation and Error Handling

After construction, the config struct is passed to `sub_4748F0` as a series of unpacked qword arguments (the decompiler shows `v28[1]` through `v28[19]` passed individually due to the 25-parameter calling convention). If `sub_4748F0` returns non-zero, the dispatcher calls `sub_467460` to emit an `"Internal FNLZR error"` diagnostic with the filename. Finally, `sub_43D990` is called on the ELF to finalize ownership transfer.

## Core Engine: sub_4748F0

### Signature

```c
uint32_t sub_4748F0(
    uint32_t  arch,               // a1  -- target SM number
    void     *elf_data,           // a2  -- input ELF bytes
    void    **output_buf,         // a3  -- receives output buffer
    size_t   *output_size,        // a4  -- receives output size
    void     *self_check_data,    // a5  -- for self-check mode (NULL normally)
    char     *option_string,      // a6  -- extra compiler flags string
    /* a7..a25: 160-byte config struct unpacked as 19 qwords */
    ...
);
```

The function is enormous (48,730 bytes) with over 330 local variables. It operates as a complete embedded compiler pipeline -- from ELF-in to ELF-out -- orchestrating all phases of Mercury finalization.

### Execution Phases

#### Phase 1: Environment Setup

1. Saves and replaces the setjmp/longjmp error context at `v341` (the arena metadata pointer). If any sub-function calls `longjmp`, the engine catches the error at the top-level `_setjmp` guard and returns error code 6.

2. If `a6` (option string) is non-empty, parses it into an options structure via `sub_4ACD60` (the embedded option parser). This allows the caller to inject compiler flags like `--opt-level`, `--binary-kind`, etc.

3. Creates a 540-byte module context array (`v419[0..67]`) zeroed with `memset`, then populates it with the target architecture, input ELF pointer, and configuration flags.

#### Phase 2: Architecture Validation

Reads the ELF header to extract the embedded architecture number from the flags field (offset `+48` for non-Mercury, `+49` high byte for Mercury type `0x41`). Three validation checks gate progress:

1. **Version check**: If the architecture profile version at `v388` exceeds `0x101`, return error 25 (version too high).
2. **Compatibility check**: `sub_43D9A0` tests whether the input ELF is a valid device object. If it fails, the function returns error 6.
3. **Type check**: The ELF type at `v43 + 16` must be `0xFF00` (Mercury) with the expected subtype (values 1 or 2). If neither, the function bails with error 7.

#### Phase 3: Fastpath Optimization

Before launching the full compilation pipeline, the engine checks whether the input ELF's embedded architecture can be directly converted to the target architecture via `sub_470DA0` (`can_finalize_with_capability_mask`). If the capability bitmask matches and `--opportunistic-finalization-lvl` is active (`HIDWORD(v419[58])`), the engine takes the fastpath:

```
[Finalizer] fastpath optimization applied for off-target N -> M finalization
```

The fastpath simply copies the input ELF bytes verbatim into the output buffer and patches the architecture field in the ELF header to match the target. This avoids the full recompilation cost when the source and target architectures are binary-compatible within the same family (e.g., sm_100 to sm_103).

#### Phase 4: Compilation Unit Initialization

Allocates a 656-byte compilation unit descriptor via `sub_4B6F40`, sets its vtable to `off_1D49C58`, and copies the 256-byte architecture profile. The unit descriptor stores:

| Offset | Field | Description |
|---|---|---|
| +0 | vtable | `off_1D49C58` |
| +8 | memory space | From `*v350` (`sub_488470` allocation) |
| +12 | target arch | `HIDWORD(v342)` (target SM number) |
| +14 | source arch | Extracted from input ELF header flags |
| +16 | debug flag | `BYTE4(a8)` |
| +17 | PIC flag | From ELF type word |
| +20 | opt level | From `a10` |
| +24 | line info | From `a9` |
| +88 | compilation context | Allocated by `sub_4B6F40` |
| +108 | Ofast mode | From `a25` |
| +184..191 | capmerc/self-check | Packed mode flags from `a20` |
| +248 | mercury profile | Set to 1 if arch > 99 and profile data present |

#### Phase 5: Input Section Processing

The engine iterates sections from the input ELF (`v75`, `v419[0]`, `v419[1]` -- three section lists obtained via `sub_464AE0`) and processes them in two passes:

**Pass 1** (symbol/relocation pass): Iterates via `sub_464BB0`/`sub_464DB0` and calls `sub_1CF07A0` (ELF_EmitSymbolTable) for each section. If any emission returns non-zero, the engine propagates the error immediately.

**Pass 2** (relocation table pass): Similarly iterates and calls `sub_1CF1690` (ELF_EmitRelocationTable).

Both passes check the `.note.nv.tkinfo` section for existing linker stamps. If the tkinfo contains an entry produced by `"nvlink"` or `"nvJIT API"`, the `v67` flag is set to indicate the ELF has already been through a link phase.

#### Phase 6: Compilation

Calls `sub_1CEF440` to initialize the compilation pipeline, then dispatches to one of two ELF writers depending on whether the output is relocatable:

- **Relocatable**: `sub_1CF72E0` (ELF_EmitProgramHeaders) then `sub_1CF7F30` (ELF_WriteRelocatableObject)
- **Complete**: `sub_1CF2100` (ELF_EmitSectionHeaders) then `sub_1CF3720` (ELF_WriteCompleteObject, 99,074 bytes)

After compilation, the output buffer and size are stored at `*v346` and `*v347`.

#### Phase 7: Debug Info Processing

If debug line info (`v357`) or debug frame info (`v358`) was provided as input:

1. For line info: calls `sub_477480` (debug line table build), `sub_4783C0` (debug line program serialize), and `sub_477510` to extract the serialized `.debug_line` section.
2. For frame info: same sequence for `.debug_frame`.
3. If `.debug_line` relocation entries exist in the input (detected by matching the `".debug_line"` section name), the engine applies address remapping via a BST lookup (`sub_4826F0`/`sub_4747E0`).

#### Phase 8: Tkinfo Emission

When `BYTE3(v419[54])` (verbose-tkinfo flag) and `LOBYTE(v419[58])` are both set, the engine constructs a tkinfo note section containing:

- The tool name (from `v380`)
- Compiler identification string: `"Cuda compilation tools, release 13.0, V13.0.88"`
- Build string: `"Build cuda_13.0.r13.0/compiler.36424714_0"`
- The caller-provided annotation string (`a22`)

This metadata is appended as a NOTE section with type 2000 in the output ELF.

#### Phase 9: Self-Check Verification

When `HIBYTE(a20)` is set and `a5` (self-check data) is NULL, the engine recursively invokes itself:

```c
v30 = sub_4748F0(target_arch, *output_buf, &v381, &v382, &v396, s, ...);
```

This second pass recompiles the output from Phase 6 and compares the result. If `BYTE6(a20)` is set, the output from the recursive call replaces the original output. This implements the `--self-check` option for capmerc validation.

When `a5` is non-NULL (the recursive self-check call), the engine performs a three-part comparison:

1. **Section content comparison**: For each section in the original output, `memcmp` against the corresponding section in the recompiled output. Mismatch returns error 17.
2. **Symbol table comparison**: Iterates `.nv.merc.`-prefixed sections via `sub_464BB0`/`sub_464DB0`, strips the prefix, and matches by name. Compares section data, size, and flags. Mismatch returns error 19.
3. **Relocation table comparison**: Same stripping and matching for relocation sections. Mismatch returns error 18.

#### Phase 10: Cleanup

1. Destroys the instruction encoding/decoding tables (`sub_45B680`)
2. Frees temporary string buffers (`sub_4746C0`)
3. Frees allocated memory via `sub_431000`
4. If Phase 4 allocated a compilation context (`v385`), destroys it via `sub_488530`
5. If Phase 4 allocated a memory space descriptor (`v353`), destroys that too
6. If a "Final memory space" arena was created (`v352`), releases it via `sub_45CAE0`/`sub_431C70`

### Return Codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 4 | Input ELF not eligible for finalization (arch mismatch, no capmerc bit) |
| 5 | Post-link requested on non-capmerc ELF (LOBYTE(a20) set but wrong type) |
| 6 | Internal error (longjmp or sub-function failure) |
| 7 | Unknown ELF type (not Mercury, not standard cubin) |
| 11 | Memory space allocation failed (`sub_488470` returned NULL) |
| 17 | Self-check section content mismatch |
| 18 | Self-check relocation table mismatch |
| 19 | Self-check symbol table mismatch |
| 25 | Architecture version too high (> 0x101) |

## Invocation Points in main()

`sub_4275C0` is called from six distinct sites in `main()` and two sites in `sub_42AF40` (fatbin member extraction):

### Pre-link Invocations (a5=0)

1. **Cubin input loop** (main line ~727): After loading a cubin file and validating its architecture via `sub_426570`, if `dword_2A5F314 > 0x59` (sm > 89) and either SASS mode is off or the ELF passes `sub_43DA40` (Mercury detection), and the validation flag `v361` is clear:
   ```
   sub_4275C0(&v362, filename, dword_2A5F314, &s1, 0);
   ```

2. **Mercury object from fatbin** (main line ~835): After `sub_43E100` processes a Mercury ELF extracted from a fatbin, the same guard applies.

3. **LTO compilation output** (main line ~1269): After ptxas/cicc produces a cubin from LTO IR, if the output targets sm > 89.

4. **Split-compile LTO output** (main line ~1313): Same as above but in the split-compile code path.

5. **Fatbin extraction pre-link** (sub_42AF40 lines ~179, ~227): When extracting cubins from fatbin members, pre-link finalization is applied if the architecture exceeds 89 and the extracted cubin matches the target.

### Post-link Invocations (a5=1)

6. **Capmerc transformation** (main line ~1481): After the complete link has been serialized, when writing Mercury capmerc output, `sub_4275C0` is called with `a5=1` to apply the post-link capmerc transformation to the serialized ELF bytes.

### Merge-phase Pre-link (a5=0, no output)

7. **Pre-merge finalization** (main line ~1503): When `byte_2A5F221` and `byte_2A5F220` are both set and the input ELF's flags indicate it is not yet finalized, `sub_4275C0` is called with `a4=NULL` (no separate output -- modifies in-place) before the object enters the merge loop.

## JIT Entry Point: sub_52DD50

The JIT wrapper at `0x52DD50` provides the FNLZR interface for the nvJIT API path (used by the CUDA driver for runtime compilation). It reads configuration from a context object at `a1` rather than from global variables:

| Context offset | Field |
|---|---|
| `a1 + 64` | Debug trace flag (equivalent to bit 0 of `dword_2A5F308`) |
| `a1 + 72` | Target architecture number |
| `a1 + 76` | Mode flags bitfield |
| `a1 + 80` | Debug compilation flag |
| `a1 + 90` | Optimization level control |
| `a1 + 99` | Line info suppression |
| `a1 + 101` | Extended debug |

The wrapper emits its own diagnostic set: `"FNLZR: JIT Path"`, `"FNLZR: preLink Mode"`, `"FNLZR: postLink Mode"`, and `"FNLZR: Ending JIT"`. The mode selection follows the same logic as `sub_4275C0` -- checking the ELF flags to determine pre-link vs. post-link -- but the config struct is populated from the JIT context object rather than from global linker state.

If the engine returns non-zero, the JIT wrapper calls `sub_1CEF420` to translate the numeric error code into a diagnostic string, then routes through `sub_467460` for error reporting.

## Architecture Compatibility Checks

Two helper functions implement the "can this ELF be finalized for this target?" query:

### sub_4709E0 -- can_finalize_architecture_check

Tests whether the input ELF's architecture is compatible with the finalization target. Uses a lookup table at `dword_1D40660[]` indexed by the "finalization class" byte (values 0-4). The function applies an internal architecture remapping:

| Input | Remapped To | Reason |
|---|---|---|
| 104 | 120 | sm_104 maps to sm_120 family for finalization |
| 130 | 107 | sm_103 family (code 130) maps to sm_100 family base (107) |
| 101 | 110 | sm_101 maps to sm_110 for finalization |

Family matching uses decade comparison: `source/10 == target/10` means same family (e.g., 100 and 103 are both in the 10x decade). Special handling exists for sm_110, sm_121, and sm_100.

The `CAN_FINALIZE_DEBUG` environment variable, when set, enables verbose tracing of this check via `strtol` parsing.

### sub_470DA0 -- can_finalize_with_capability_mask

Extends the architecture check with a capability bitmask. Maps target architecture codes to bitmask values:

| Architecture code | Decimal | Bitmask |
|---|---|---|
| 'd' | 100 (sm_100) | 1 |
| 'g' | 103 (sm_103) | 8 |
| 'n' | 110 (sm_110) | 2 |
| 'y' | 121 (sm_121) | 64 |

The function reads a capability mask pointer from `a1+16` and returns true only if the target's bitmask is a subset of the source's declared capabilities. This enables the fastpath optimization where binary-compatible architectures within the same family can skip recompilation.

## Configuration Options

The embedded option parser at `sub_4AC380` defines the FNLZR-specific command-line options (separate from nvlink's own CLI):

| Option | Description | Default |
|---|---|---|
| `--binary-kind` | Target ELF kind: `mercury`, `capmerc`, or `sass` | `capmerc` on sm100+ |
| `--cap-merc` | Generate Capsule Mercury | (flag) |
| `--self-check` | Re-compile and verify output matches | (flag) |
| `--out-sass` | Emit raw SASS output | (flag) |
| `--opportunistic-finalization-lvl` | Fastpath optimization level (0-2) | 0 |
| `--fastpath-off` | Disable fastpath finalization | (flag) |
| `--opt-level` | Optimization level for embedded compilation | 3 |
| `--generate-line-info` | Emit debug line info in output | (flag) |
| `--disable-smem-reservation` | Disable shared memory reservation | (flag) |
| `--verbose-tkinfo` | Emit object name and command line in tkinfo | (flag) |
| `--compile-as-at-entry-patch` | Compile as "at entry" fragment patch | (flag) |
| `--trap-into-debugger` | Trap on assertion failures | (flag) |

These options can be injected via the `a6` option string parameter to `sub_4748F0`, which parses them through `sub_4ACD60`.

## Global Variables

| Address | Type | Name | Description |
|---|---|---|---|
| `dword_2A5F308` | uint32 | edbg_flags | Verbose flags; bit 0 enables FNLZR tracing |
| `dword_2A5F314` | uint32 | target_arch | Target SM number (e.g., 100 for sm_100) |
| `byte_2A5F222` | byte | is_mercury | 1 if sm > 99 |
| `byte_2A5F225` | byte | is_sass_mode | 1 if sm > 89 |
| `byte_2A5F310` | byte | debug_flag | 1 if `-g` was passed |
| `byte_2A5F210` | byte | suppress_line_info | Line info suppression |
| `byte_2A5F224` | byte | extended_debug | Extended debug info |
| `byte_2A5F223` | byte | suppress_debug | Suppress debug info |
| `byte_2A5F2A9` | byte | ofast_flag | Ofast compilation flag |
| `byte_2A5F221` | byte | fnlzr_pre_merge | Enable pre-merge finalization |
| `byte_2A5F220` | byte | fnlzr_pre_merge_2 | Secondary pre-merge guard |
| `byte_2A5B510` | byte | dont_uplift | Skip uplift for matching arch |

## Relationship to the Embedded ptxas

FNLZR does not contain its own instruction selection or register allocation logic. Instead, it delegates the heavy lifting to the embedded ptxas compiler backend via the functions in the `0x1CF0000-0x1D32172` range:

- `sub_1CEF5B0` -- ELF_ProcessRelocations (relocation processing)
- `sub_1CF07A0` -- ELF_EmitSymbolTable (symbol table emission, 25,255 bytes)
- `sub_1CF1690` -- ELF_EmitRelocationTable (relocation emission, 16,049 bytes)
- `sub_1CF2100` -- ELF_EmitSectionHeaders (section header construction, 31,261 bytes)
- `sub_1CF3720` -- ELF_WriteCompleteObject (complete ELF output, 99,074 bytes)
- `sub_1CF72E0` -- ELF_EmitProgramHeaders (program header emission, 17,710 bytes)
- `sub_1CF7F30` -- ELF_WriteRelocatableObject (relocatable output, 44,740 bytes)

The compilation unit descriptor at `off_1D49C58` provides the vtable for the OCG (Optimizing Code Generator) backend, which performs the actual Mercury-to-SASS translation. The memory space is managed through the "Final memory space" arena created specifically for each FNLZR invocation.

## Debugging FNLZR

### Enabling Trace Output

Set `--edbg 1` on the nvlink command line to enable bit 0 of `dword_2A5F308`. This produces the full FNLZR trace:

```
FNLZR: Input ELF: mykernel.cubin
FNLZR: Pre-Link Mode
FNLZR: Flags [ 0 | 1 ]
FNLZR: Starting mykernel.cubin
FNLZR: Ending mykernel.cubin
```

For JIT paths, the corresponding output is:

```
FNLZR: JIT Path
FNLZR: preLink Mode
FNLZR: Flags [ 0 | 1 ]
FNLZR: Starting JIT
FNLZR: Ending JIT
```

### Environment Variables

- `CAN_FINALIZE_DEBUG`: When set, enables verbose output from the architecture compatibility checks (`sub_4709E0`, `sub_470DA0`). The value is parsed with `strtol` but any non-zero value activates tracing.

### Self-Check Mode

Pass `--self-check` to enable re-compilation verification. The engine compiles the input, then recompiles its own output, and compares the two at the section, symbol, and relocation level. Mismatches produce error codes 17, 18, or 19 with no additional diagnostic text -- the caller must inspect the return code.
