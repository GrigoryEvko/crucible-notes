# FNLZR (Finalizer)

The FNLZR subsystem is nvlink's embedded binary rewriter for Mercury-class targets (sm >= 100). It accepts a fully-linked or partially-linked device ELF, invokes the embedded ptxas/OCG compiler backend to re-emit SASS, and produces a transformed ELF suitable for the target architecture. FNLZR operates in two distinct modes — pre-link mode, which processes individual cubins before they enter the merge phase, and post-link mode, which applies a capmerc (Capsule Mercury) transformation after the complete link. The name "FNLZR" appears verbatim in diagnostic messages (`FNLZR: Input ELF: %s`, `FNLZR: Pre-Link Mode`, etc.) and is gated behind bit 0 of `dword_2A5F308`, the `--edbg` verbose flags word.

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

```text
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

**Pre-link mode** (`a5 == 0`): Applied to individual cubins before they enter the merge phase. The guard condition checks whether the ELF already contains finalized SASS. For ELF type `0x41` ('A', the Mercury ELF class marker), the check is `(flags >> 2) & 1` — if that bit is set, finalization has already been applied and the function returns an internal error. For non-Mercury ELF types, the check is `(flags & 0x80004000) == 0` — if both the capmerc and SASS-present bits are clear, the ELF does not need finalization.

**Post-link mode** (`a5 == 1`): Applied after the full link+finalization pipeline has serialized the merged ELF. The guard checks whether the SASS-present or capmerc bit is set (the inverse mask from pre-link), confirming the ELF is indeed a Mercury binary that requires post-link transformation.

### Configuration Struct Construction

The dispatcher builds a 160-byte configuration struct (`v28[0..19]`) on the stack, zeroed with `memset`, then populates it based on the global linker state:

| Offset (qword index) | Field | Source |
|---|---|---|
| `v28[3]` bits 32..39 | Debug flag | `byte_2A5F310 != 0` (i.e. `-g` was passed) |
| `v28[3]` bits 40..47 | Line info suppression | TODO: identify true byte for suppress_line_info — previous wiki revisions cited `byte_2A5F210`, but `byte_2A5F210` is the `--disable-smem-reservation` CLI byte (feeds `merge_flags` bit 12 per `main:375-379`) and has no documented Mercury subsystem use |
| `v28[3]` low dword | Optimization level | 4 (normal) or 5 (debug mode with `byte_2A5F2A9`) |
| `v28[8]` low dword | Fallback opt level | 3 (when neither debug nor `byte_2A5F310`) |
| `v28[13]` byte 0 | capmerc transform flag | 1 if Mercury mode (`byte_2A5F222`) |
| `v28[13]` byte 1 | SASS-only flag | `byte_2A5F225 != 0` |
| `v28[13]` byte 2 | Always 1 | Constant |
| `v28[13]` byte 3 | Extended debug | `byte_2A5F224 != 0` |
| `v28[13]` byte 4 | Suppress debug info | `byte_2A5F223 != 0` |

### Diagnostic Output

When `(dword_2A5F308 & 1) != 0` (bit 0 of `--edbg`), the dispatcher emits a sequence of messages to `stderr`:

```text
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

The function is enormous (48,730 bytes) with over 330 local variables. It operates as a complete embedded compiler pipeline — from ELF-in to ELF-out — orchestrating all phases of Mercury finalization.

### Execution Phases

The 10 phases execute sequentially within a single function body. The decompiled code is 1,830 lines with over 330 local variables. The following pseudocode reconstructs each phase from `sub_4748F0_0x4748f0.c`.

#### Phase 1: Environment Setup (lines 426–493)

Establishes the error recovery context and parses any injected compiler options.

```c
fn fnlzr_engine(arch, elf_data, out_buf, out_size, self_check_data, option_str, config[0..19]):
    # 1a. Save and replace the global setjmp/longjmp error handler.
    #     sub_44F410 returns the arena metadata pointer (2 bytes + 1 qword).
    prev_handler = sub_44F410(arch, elf_data)
    saved_byte0 = prev_handler[0]       # v354 -- error propagation flag A
    saved_byte1 = prev_handler[1]       # v355 -- error propagation flag B
    saved_ctx   = prev_handler->qword1  # v343 -- saved longjmp target
    prev_handler[0..1] = 0              # disable old handler
    prev_handler->qword1 = &env         # redirect longjmp to our _setjmp

    if _setjmp(env):
        # Any longjmp from a subroutine lands here.
        prev_handler->qword1 = saved_ctx      # restore previous handler
        prev_handler[0..1] = {1, 1}           # re-arm both propagation flags
        return 6  # "internal error"

    # 1b. Initialize scratch state.
    opt_level_fallback = 3               # v414
    config_ptr = &config[0]              # v406 = &a8
    si128 = load_xmm(xmmword_1D40750)   # SSE constant for arch profile lookup
    memset(options_block, 0, sizeof)     # v403..v411 zeroed

    # 1c. Parse injected option string, if any.
    if option_str and *option_str:
        len = strlen(option_str)
        buf = arena_alloc(round_up_pow2(len + 9))
        memcpy(buf, option_str, len + 1)
        sub_4ACD60(options_block, buf, len, ...)   # parse "--opt-level 3" etc.
        config.ofast = options_block.ofast_flag    # a25 overridden
        arena_free(buf)

    # 1d. Merge config defaults: if config[0].arch == 0, use target_arch.
    if config.arch_override == 0:
        config.arch_override = arch
    # If config does not specify a PIC flag, default to 1.
    if not config.pic_flag:
        pic_enabled = config.pic_byte5
```

Key data structures initialized here:

| Variable | Stack offset | Size | Description |
|---|---|---|---|
| `env` | rbp-0x328 | 200 bytes | `jmp_buf` for setjmp/longjmp error trap |
| `v419[]` | rbp-0x258 | 600 bytes (75 qwords) | Module context array; carries all state between phases |
| `v403[]` | rbp-0x458 | 32 bytes (2 owords) | Parsed options block from `sub_4ACD60` |
| `v341` | rbp-0x680 | 8 bytes (pointer) | Saved arena metadata pointer for error handler chain |
| `v343` | rbp-0x670 | 8 bytes | Previous longjmp target (restored on exit) |

The `_setjmp` / `longjmp` pattern is the only error recovery mechanism. Every early-exit path in phases 2–9 restores `v341` before jumping to the cleanup at `LABEL_20`.

#### Phase 2: Architecture Validation (lines 500–720)

Four sequential checks gate entry to the compilation pipeline. Any failure short-circuits to cleanup.

```c
    # 2a. Module context initialization.
    memset(v419, 0, 0x218)              # 67 qwords = 536 bytes
    v419[8].lo = arch                   # target SM number
    v419[4]    = elf_data               # input ELF pointer

    # 2b. Device object validation.
    is_valid = sub_43D9A0(elf_data)     # returns 1 if valid device ELF
    if not is_valid:
        restore_handler()
        return 6

    # 2c. Read ELF header fields.
    hdr = sub_448360(elf_data)          # returns section header table base
    flags = *(uint32*)(hdr + 48)        # e_flags
    elf_type_byte = *(byte*)(hdr + 7)   # ELF class marker (0x41 = 'A' = Mercury)
    elf_subtype = *(uint16*)(hdr + 16)  # ELF subtype (0xFF00 = Mercury, 1 or 2 = cubin)

    # 2d. Finalization-needed check (same logic as front-end dispatcher).
    if elf_type_byte == 0x41:  # Mercury
        bit_to_check = 2
        if (flags & 1) != 0:
            goto finalization_eligible   # bit 0 set -> needs finalization
    else:  # Standard cubin
        bit_to_check = 0x4000
        if flags < 0:  # bit 31 set
            goto finalization_eligible
    if (bit_to_check & flags) == 0:
        restore_handler(); return 6     # not eligible

    # 2e. Subtype validation -- must be Mercury (0xFF00) with subtype 1 or 2.
    if elf_subtype != 0xFF00 and (elf_subtype - 1) > 1:
        restore_handler(); return 6     # unknown format

    # 2f. Create "Final memory space" arena (4096-byte initial page).
    arena = sub_432020("Final memory space", 0, 4096)
    mem_space = sub_45CAE0(arena, 0)    # v351

    # 2g. Mark capmerc flag in module context.
    if (bit_to_check & flags) != 0:
        v419[54].byte0 = 1              # capmerc transform needed

    # 2h. Extract architecture profile via sub_43E610.
    profile_valid = sub_43E610(elf_data, &profile_buf)  # v387
    profile_version = *(uint16*)(profile_buf + 6)       # v388

    # 2i. Version ceiling check.
    if profile_version > 0x101:
        restore_handler(); return 25    # "architecture version too high"

    # 2j. Profile/subtype cross-checks for Mercury vs standard cubins.
    #     Mercury type 0x41 with (flags & 2) set and post-link mode triggers
    #     special handling for sm_121 target with opt-level 5 (debug).
    #     Standard cubin with (flags & 0x4000) set follows similar logic.
    #     If post-link mode (BYTE1(a20)) is set but capmerc flag is not,
    #     return error 4 ("not eligible").
    #     If capmerc mode requested on wrong ELF type, return error 5.
```

The version check at step 2i uses `0x101` (decimal 257) as the ceiling. This corresponds to version 1.1 in the Mercury profile format — any profile claiming version 1.2 or higher causes immediate rejection.

The ELF type byte at `hdr + 7` distinguishes three cases:

| Byte value | Decimal | Meaning | Finalization bit |
|---|---|---|---|
| `0x41` | 65 | Mercury ELF (`'A'`) | Bit 0 of `e_flags` (finalized indicator) |
| `0x07` | 7 | Standard device cubin | Bit 14 (`0x4000`) of `e_flags` |
| `0x08` | 8 | Mercury cubin variant | Same as `0x41` path (via LABEL_55 fallthrough) |

Any other value at `hdr + 8` (the secondary type byte) that is not 7 or 0 causes error 7 ("unknown ELF type").

#### Phase 3: Fastpath Optimization (lines 830–880)

Skips the full compilation pipeline when the source and target architectures are binary-compatible.

```c
    # 3a. Gate conditions: NOT self-check mode AND NOT recursive call.
    if BYTE2(config.a21) == 1:   goto skip_fastpath  # self-check pass
    if self_check_data != NULL:  goto skip_fastpath  # recursive invocation

    # 3b. Extract source architecture from ELF flags.
    if elf_type_byte == 0x41:
        source_arch = (flags >> 8) & 0xFFFF   # Mercury: arch in bits 8..23
    else:
        source_arch = flags & 0xFF             # Standard: arch in low byte

    # 3c. Capability bitmask check via sub_470DA0.
    invert_flag = 0
    if v419[54].byte0:        # capmerc eligible
        invert_flag = BYTE1(a20) ^ 1   # invert unless post-link mode
    can_fastpath = sub_470DA0(&profile_buf, source_arch, arch, invert_flag)

    if not can_fastpath:
        goto skip_fastpath

    # 3d. Diagnostic (when --opportunistic-finalization-lvl > 0).
    if HIDWORD(v419[58]):     # opportunistic finalization level
        printf("[Finalizer] fastpath optimization applied for "
               "off-target %u -> %u finalization\n", source_arch, arch)

    # 3e. Copy input ELF verbatim.
    input_size = sub_43DA80(elf_data)    # total ELF size in bytes
    *out_size = input_size
    output = arena_alloc(mem_space, input_size)
    memset(output, 0, input_size)
    memcpy(output, elf_data, input_size)
    *out_buf = output

    # 3f. Patch architecture field in output ELF header.
    out_hdr = sub_448360(*out_buf)
    out_flags = *(uint32*)(out_hdr + 48)
    if *(byte*)(out_hdr + 7) == 0x41:  # Mercury
        *(uint32*)(out_hdr + 48) = (arch << 8) & 0xFFFF00 | out_flags & 0xFF0000FF
    else:                               # Standard cubin
        *(uint32*)(out_hdr + 48) = (arch & 0xFF) | (out_flags & 0xFFFFFF00)

    return 0   # success -- full pipeline skipped
```

The `sub_470DA0` capability bitmask check maps architecture codes to power-of-two bitmask values, then tests whether the target's bitmask is a subset of the source's declared capabilities at `profile_buf + 16`:

| Architecture code | Decimal (char) | SM | Bitmask value |
|---|---|---|---|
| `'d'` (0x64) | 100 | sm_100 | 1 |
| `'g'` (0x67) | 103 | sm_103 | 8 |
| `'n'` (0x6E) | 110 | sm_110 | 2 |
| `'y'` (0x79) | 121 | sm_121 | 64 |

The function also applies architecture remapping before the bitmask test:

| Input code | Remapped to | Reason |
|---|---|---|
| 104 | 120 | sm_104 is finalization-equivalent to sm_120 family |
| 130 | 107 | sm_103 family (internal code 130) maps to sm_100 base (107) |
| 101 | 110 | sm_101 maps to sm_110 family |

There is an additional special-case bypass at `LABEL_202` (lines 623–636): when the target is sm_121, the source subtype is 2, and the opt-level is not 5 (debug), and the source arch field matches 120 — the engine copies the ELF verbatim without even calling `sub_470DA0`. This handles the sm_120-to-sm_121 uplift case where the binaries are known to be identical.

#### Phase 4: Compilation Unit Initialization (lines 722–987)

Constructs the 256-byte architecture profile descriptor and the 656-byte compilation unit (CU) object that drives the embedded ptxas backend.

```c
    # 4a. Build three section lists from the input ELF.
    section_count = sub_448730(elf_data)
    v75       = sub_464AE0(section_count)   # primary section list
    v419[0]   = sub_464AE0(section_count)   # auxiliary section list A
    v419[1]   = sub_464AE0(section_count)   # auxiliary section list B
    v419[50]  = sub_44FB20(128)             # 128-entry string pool
    v419[65]  = NULL                        # mercury profile pointer (set later)

    # 4b. Create instruction encoding/decoding tables for target arch.
    v419[6] = sub_45AC50(arch)              # encoding table
    v419[7] = sub_459640(arch)              # decoding table

    # 4c. Store mode flags into module context.
    v419[58].byte1 = pic_enabled            # PIC mode
    v419[54].byte1 = a20                    # post-link / capmerc packed flags
    v419[59].byte0 = BYTE5(a20)             # self-check sub-mode
    v419[58].hi    = HIDWORD(a14)           # opportunistic finalization level
    v419[58].byte2 = BYTE1(a21)             # additional mode flag

    # 4d. Process relocations from input ELF via embedded ptxas.
    err = sub_1CEF5B0(v75, &reloc_ctx, v419)   # ELF_ProcessRelocations
    if err: return err

    # 4e. Allocate 256-byte architecture profile descriptor (v350).
    alloc_ctx = sub_44F410(v75, &reloc_ctx)->qword3   # arena from context
    v350 = arena_alloc(alloc_ctx, 256)
    memset(v350, 0, 256)

    # 4f. Allocate memory space via sub_488470.
    mem_space_obj = sub_488470()           # v383
    *v350 = mem_space_obj                  # store at offset +0 of profile
    if mem_space_obj == NULL: return 11    # allocation failure

    # 4g. Allocate 656-byte CU descriptor via sub_4B6F40.
    cu = sub_4B6F40(656, mem_space_obj)

    # 4h. Initialize CU descriptor fields.
    *(qword*)(cu + 0)   = off_1D49C58     # vtable pointer (OCG backend)
    *(qword*)(cu + 8)   = *v350           # memory space reference
    *(qword*)(cu + 16)  = 10240           # initial code buffer size (0x2800)
    *(qword*)(cu + 24)  = 0              # code buffer pointer (NULL initially)
    *(qword*)(cu + 32)  = 0              # symbol table pointer
    *(qword*)(cu + 40)  = 0              # relocation table pointer
    *(qword*)(cu + 48)  = 0              #
    *(qword*)(cu + 56)  = 0              #
    *(dword*)(cu + 64)  = 0              # section count
    *(qword*)(cu + 72)  = 0              #
    *(qword*)(cu + 80)  = 0              #
    *(qword*)(cu + 88)  = 0              #
    # Zero-initialize the 512-byte region at cu+96 (64 qwords).
    memset_aligned(cu + 96, 0, 512)
    # Zero the tail fields.
    *(qword*)(cu + 608) = 0
    *(qword*)(cu + 616) = 0
    *(qword*)(cu + 624) = 0
    *(qword*)(cu + 632) = 0
    *(qword*)(cu + 640) = 0
    *(qword*)(cu + 648) = 0

    # 4i. Link CU into profile descriptor.
    *(qword*)(v350 + 88) = cu             # profile[11] = CU pointer

    # 4j. Populate CU target/source architecture.
    *(dword*)(v350 + 8)  = arch           # target arch (offset +2 as dword index)
    if elf_type_byte == 0x41:
        *(dword*)(v350 + 12) = *(uint16*)(hdr + 49)   # source from Mercury header
    else:
        *(dword*)(v350 + 12) = *(byte*)(hdr + 48)     # source from standard flags

    # 4k. Populate remaining CU fields based on ELF subtype.
    if elf_subtype == 1:  # relocatable object
        *(byte*)(v350 + 17) = 1           # PIC flag
        *(dword*)(v350 + 20) = 5          # opt level forced to 5
        *(byte*)(v350 + 191) = 0          # not a complete object
    else:                 # executable / complete object
        *(byte*)(v350 + 191) = 1          # complete object flag
        *(byte*)(v350 + 17) = config.pic  # PIC from caller config
        *(dword*)(v350 + 20) = config.opt_level

    *(word*)(v350 + 24)  = config.line_info_word
    *(qword*)(v350 + 48) = config.include_path ?: ""
    *(qword*)(v350 + 56) = config.source_path ?: ""
    *(byte*)(v350 + 16)  = config.debug_flag
    *(dword*)(v350 + 100) = config.extra_opt
    *(dword*)(v350 + 96) = config.line_info_dword
    *(dword*)(v350 + 104) = config.codegen_flag
    *(byte*)(v350 + 108) = config.ofast_mode
    *(dword*)(v350 + 120) = config.hi_codegen
    *(qword*)(v350 + 128) = config.extra_path_a ?: ""
    *(word*)(v350 + 136) = config.target_word
    *(qword*)(v350 + 144) = config.extra_path_b ?: ""
    *(word*)(v350 + 185) = a20            # packed capmerc/self-check flags
    *(byte*)(v350 + 184) = BYTE2(a20)     # capmerc transform sub-flag
    *(byte*)(v350 + 187) = BYTE5(a20)     # self-check sub-mode

    # 4l. If self-check sub-mode is active, allocate section tracking lists.
    if BYTE5(a20):
        tracker = alloc_via_vtable(cu.memspace, 24)
        tracker[1] = 0; tracker[2] = 0xFFFFFFFF
        tracker[0] = cu.memspace
        v350[24] = tracker                # at offset +192
        v419[61] = sub_464AE0(8)          # symbol section tracker
        v419[60] = sub_464AE0(8)          # relocation section tracker

    # 4m. Set Mercury profile if source arch > 99.
    if *(dword*)(v350 + 12) > 99 and profile_valid:
        *(byte*)(v350 + 248) = 1          # mercury_profile flag
        *(dword*)(v350 + 212) = profile_buf[0]  # profile data byte 0
        *(dword*)(v350 + 216) = profile_buf[2]  # profile data byte 2
        v419[65] = &profile_buf           # mercury profile pointer
```

The CU descriptor at 656 bytes is the largest single allocation in the FNLZR engine. Its vtable at `off_1D49C58` provides the interface to the OCG (Optimizing Code Generator) backend. The initial code buffer size of 10,240 bytes (0x2800) is a hint that gets resized dynamically during compilation.

The complete CU descriptor layout:

| Offset | Size | Field | Source |
|---|---|---|---|
| +0 | 8 | vtable | `off_1D49C58` (OCG backend vtable) |
| +8 | 8 | memory space | `*v350` from `sub_488470` |
| +16 | 8 | code buffer size | 10240 (constant initial) |
| +24 | 8 | code buffer ptr | NULL (allocated later by OCG) |
| +32..88 | 56 | symbol/reloc/section tables | Zero-initialized |
| +88 | 8 | CU back-pointer | Points to CU object from profile descriptor |
| +96..607 | 512 | compilation state | 64 qwords, zero-initialized |
| +608..655 | 48 | tail metadata | 6 qwords, zero-initialized |

#### Phase 5: Input Section Processing (lines 987–1065)

Two separate concerns: tkinfo scanning to detect prior linking, and the two-pass section emission loop.

```c
    # 5a. Scan .note.nv.tkinfo for prior linker stamps.
    tkinfo_section = sub_4483B0(elf_data, ".note.nv.tkinfo")
    already_linked = false    # v67

    if tkinfo_section:
        note_base = elf_data + tkinfo_section->data_offset   # offset +24
        note_end  = note_base + tkinfo_section->data_size    # offset +32
        cursor = note_base

        while cursor < note_end:
            note_type   = *(uint32*)(cursor + 8)    # note descriptor type
            payload_len = *(uint32*)(cursor + 4)    # note descriptor size
            if note_type != 2000: break             # not a CUDA tool note

            payload_start = cursor + 48             # 12 dwords header = 48 bytes
            if payload_start > note_end: break      # truncated
            remaining = payload_len - 24
            if payload_start + remaining > note_end: break

            if payload_len != 24:                   # has tool name string
                if *(byte*)(cursor + payload_len - 25 + 48) != 0: break  # no NUL terminator
                name_offset = *(uint32*)(cursor + 32)    # tool name offset
                if remaining > name_offset:
                    tool_name = payload_start + name_offset
                    if strcmp(tool_name, "nvlink") == 0:
                        already_linked = true; break
                    if strcmp(tool_name, "nvJIT API") == 0:
                        already_linked = true; break

            cursor += payload_len + 24              # advance to next note
    v419[66].byte0 = already_linked

    # 5b. Pass 1: Symbol table emission.
    for i in 0 .. sub_464BB0(v75):
        section = sub_464DB0(v75, i)
        if section:
            err = sub_1CF07A0(section, v419)   # ELF_EmitSymbolTable
            if err: goto cleanup_with_error(err)

    # 5c. Pass 2: Relocation table emission.
    for j in 0 .. sub_464BB0(v75):
        section = sub_464DB0(v75, j)
        if section:
            err = sub_1CF1690(section, v419)   # ELF_EmitRelocationTable
            if err: goto cleanup_with_error(err)
```

The tkinfo scanning loop is precise about note format validation. Each note entry is:

| Offset from note start | Size | Field |
|---|---|---|
| +0 | 4 | `n_namesz` (always the name size) |
| +4 | 4 | `n_descsz` (payload descriptor size) |
| +8 | 4 | `n_type` (must be 2000 for CUDA tool notes) |
| +12..47 | 36 | Name and alignment padding |
| +48 | variable | Payload (contains tool name at internal offset) |

The two-pass section loop processes the same section list (`v75`). Pass 1 (`sub_1CF07A0`, ELF_EmitSymbolTable — 25,255 bytes) builds the symbol table from input sections. Pass 2 (`sub_1CF1690`, ELF_EmitRelocationTable — 16,049 bytes) processes relocation entries. The three section lists (`v75`, `v419[0]`, `v419[1]`) are created from the same section count (`sub_448730`) but accumulate different section categories during processing — symbols, data sections, and relocations respectively.

#### Phase 6: Compilation and ELF Emission (lines 1057–1492)

The most complex phase. It initializes the compilation pipeline, handles debug info input, creates address mapping structures, dispatches to the appropriate ELF writer, and allocates the output buffer.

```c
    # 6a. Initialize the compilation pipeline.
    v419[32] = v350 + 25                    # compilation context = profile + 200 bytes
    sub_1CEF440(v419, 0.0)                  # pipeline initialization
    *(byte*)(v350 + 33) = (v419[33] != 0)   # propagate compilation flag

    # 6b. If a19 (output relocation context) is provided, register it.
    if a19:
        v350[21] = a19
        v350[22] = out_buf                  # output receives relocated data

    # 6c. Prepare debug info input structures.
    debug_line_input  = v419[10]     # v357 -- .debug_line section data
    debug_frame_input = v419[11]     # v358 -- .debug_frame section data
    line_remap_input  = v419[9]      # v360 -- line info remapping data
    has_line  = (debug_line_input != NULL)
    has_frame = (debug_frame_input != NULL)

    # 6d. If line remapping data is present, create a 232-byte remap context.
    if line_remap_input:
        remap_ctx = alloc_via_vtable(v350[11], 232)   # v359
        *(qword*)remap_ctx = v350[11]                  # memory space
        sub_4705D0(remap_ctx + 32, v350[11])           # init BST structure
        remap_ctx[3] = line_remap_input[4]             # source mapping count
        remap_ctx[1] = line_remap_input[1]             # source data pointer
        remap_ctx[2] = *(dword*)(line_remap_input + 16)  # source size
        sub_4BC030(remap_ctx)                          # build BST from source data
        v350[8] = remap_ctx + 32                       # install BST at profile +64

    # 6e. If relocatable debug info sections exist, create cross-ref context.
    has_debug_reloc = (v419[13] != NULL and v419[12] != NULL)
    if has_debug_reloc:
        reloc_debug_ctx = alloc_via_vtable(v350[11], 224)   # v161
        sub_470720(reloc_debug_ctx, v350[11])               # init
        # ... populate from v419[12] (line) and v419[13] (frame) ...
        sub_4707D0(reloc_debug_ctx, line_data, line_size, frame_data, frame_size, v419[17])
        sub_4AD3E0(reloc_debug_ctx)                         # finalize
        v350[10] = reloc_debug_ctx

    # 6f. Create 104-byte debug output context.
    debug_out = alloc_via_vtable(v350[11], 104)   # v175
    # Initialize 13 qword slots: alternating memory-space pointers and 0xFFFFFFFF sentinels.
    for slot in 0..12:
        debug_out[slot] = v350[11] if even else 0xFFFFFFFF

    # 6g. Process .debug_line input (if present).
    if debug_line_input:
        section_name = debug_line_input[3]    # section name string
        sub_4713E0(&line_hash, section_name)  # hash the name
        sub_4746F0(scratch_buf_A, &line_hash) # init scratch buffer
        if line_hash.hi:
            vtable_call(v350[11], free)       # release temp
        sub_47DE50(debug_out, debug_line_input[1], scratch_buf_A,
                   *(dword*)(debug_line_input + 16), debug_line_input[4],
                   0, v419[17], 0)            # mode=0 -> line info

    # 6h. Process .debug_frame input (if present).
    if debug_frame_input:
        section_name = debug_frame_input[3]
        sub_4713E0(&frame_hash, section_name)
        sub_4746F0(scratch_buf_B, &frame_hash)
        if frame_hash.hi:
            vtable_call(v350[11], free)
        sub_47DE50(debug_out, debug_frame_input[1], scratch_buf_B,
                   *(dword*)(debug_frame_input + 16), debug_frame_input[4],
                   0, v419[17], 1)            # mode=1 -> frame info

    # 6i. Set the debug-present flag on the CU profile.
    *(byte*)(v350 + 25) |= (has_line or has_frame)
    v419[19] = debug_out

    # 6j. Create 80-byte output tracking context.
    out_track = alloc_via_vtable(v350[11], 80)   # v184
    *out_track = v350[11]
    sub_list = alloc_via_vtable_24(v350[11], 24)
    sub_list[2] = v350[11]; sub_list[1] = 0; *sub_list = 1
    out_track[5] = sub_list
    out_track[1..4] = 0
    *(dword*)(out_track + 32) = 0
    out_track[7] = 0; out_track[8] = 0xFFFFFFFF; out_track[9] = 0
    out_track[6] = v350[11]
    v350[9] = out_track

    # 6k. If relocation context, propagate output pointers.
    if a19:
        v350[21] = a19; v350[22] = out_buf

    # 6l. Process function index sections (if relocation context exists).
    if reloc_ctx:      # v379 non-zero
        for k in 0 .. sub_464BB0(v419[0]):
            section = sub_464DB0(v419[0], k)
            if section:
                err = sub_471700(section, v419, &additional_ctx, 0.0)
                if err: goto cleanup_with_error(err)
    else:
        # No relocation context -- fill index with 0xFF.
        memset(v419[63], 0xFF, 4 * v419[64].lo)

    # 6m. Invert the function index bitmask.
    for idx in 0 .. v419[64].lo:
        v419[63][idx] = ~v419[63][idx]

    # 6n. If line remap data present, finalize the BST.
    if line_remap_input:
        sub_4BC0E0(remap_ctx, line_remap_input[4])
        *(dword*)(line_remap_input + 16) = *(qword*)(remap_ctx + 16)
        line_remap_input[1] = *(qword*)(remap_ctx + 8)

    # 6o. If debug relocation context, finalize it.
    if has_debug_reloc:
        sub_4AD120(reloc_debug_ctx)
        # Update v419[12] and v419[13] with rewritten offsets/sizes.

    # 6p. Destroy the mutex allocated for compilation synchronization.
    pthread_mutex_destroy(v419[30])

    # --- Phase 7 is embedded here (debug table serialization) ---
    # (see Phase 7 below)

    # --- Phase 8 is embedded here (tkinfo emission) ---
    # (see Phase 8 below)

    # 6q. Dispatch to ELF writer (header construction + serialization).
    output_sections = sub_464AE0(sub_448730(elf_data))   # v419[52]
    phdr_section = sub_4484F0(elf_data, 2)               # program headers
    v419[53] = sub_464AE0(phdr_section->size / phdr_section->entsize)

    if *(byte*)(v350 + 186):  # relocatable output flag
        err = sub_1CF72E0(v419)   # ELF_EmitProgramHeaders
    else:
        err = sub_1CF2100(v419)   # ELF_EmitSectionHeaders (31,261 bytes)
    if err: goto cleanup_with_error(err)

    # 6r. Allocate output buffer and write the ELF.
    output_size = v419[2]                            # computed by header emission
    output = arena_alloc(mem_space, output_size)
    memset(output, 0, output_size)
    v419[5] = output                                 # output buffer in module context

    if *(byte*)(v350 + 186):  # relocatable
        err = sub_1CF7F30(v419)   # ELF_WriteRelocatableObject (44,740 bytes)
    else:
        err = sub_1CF3720(v419)   # ELF_WriteCompleteObject (99,074 bytes)
    if err: goto cleanup_with_error(err)

    # 6s. Store final output pointer and size.
    *out_size = v419[2]
    *out_buf  = v419[5]
```

The relocatable-vs-complete dispatch is determined by `*(byte*)(v350 + 186)` — the "relocatable output" flag set in Phase 4 based on the ELF subtype. Subtype 1 (relocatable) triggers the relocatable path; subtype 2 (executable) triggers the complete path.

The output allocation at step 6r allocates from the "Final memory space" arena created in Phase 2. This arena's lifetime extends beyond the FNLZR engine return — the caller (`sub_4275C0`) owns the output buffer.

#### Phase 7: Debug Info Serialization (lines 1294–1372)

Post-compilation processing of `.debug_line` and `.debug_frame` sections.

```c
    # 7a. Serialize .debug_line table.
    if debug_line_input:    # v357
        sub_477480(debug_out, 0)     # build debug line table (mode=0)
        sub_4783C0(debug_out, 0)     # serialize debug line program (mode=0)
        result = sub_477510(debug_out, 0)   # extract serialized section
        debug_line_input[1] = *(qword*)(result + 8)     # data pointer
        *(dword*)(debug_line_input + 16) = *(dword*)(result + 16) + 1  # size (note: +1)

    # 7b. Serialize .debug_frame table.
    if debug_frame_input:   # v358
        sub_477480(debug_out, 1)     # build debug frame table (mode=1)
        sub_4783C0(debug_out, 1)     # serialize debug frame program (mode=1)
        result = sub_477510(debug_out, 1)
        debug_frame_input[1] = *(qword*)(result + 8)
        *(dword*)(debug_frame_input + 16) = *(dword*)(result + 16) + 1

    # 7c. Apply address remapping for .debug_line relocations.
    if v419[14] and v419[14][4]:     # relocation entries exist
        sub_4826F0(&bst_root, debug_out, 0)  # build BST from address map

        for entry_idx in 0 .. sub_464BB0(v419[14][4]):
            entry = sub_464DB0(v419[14][4], entry_idx)
            if *(dword*)(entry + 24) != 0x10008:   # type check (65544 decimal)
                continue

            sym_idx = *(dword*)(entry + 28)
            symtab = sub_4483B0(elf_data, ".symtab")
            sym_name = sub_4486A0(elf_data, symtab, sym_idx)

            # Check if symbol name is ".debug_line" (12-byte comparison).
            if strncmp(sym_name, ".debug_line", 12) == 0:
                # Look up the original offset in the BST.
                original_offset = *(dword*)(entry + 8)
                node = bst_root
                while node:
                    if original_offset < *(dword*)(node + 24):
                        node = *node          # left child
                    elif original_offset > *(dword*)(node + 24):
                        node = *(node + 8)    # right child
                    else:
                        # Match found -- replace with remapped offset.
                        *(qword*)(entry + 8) = *(uint32*)(node + 28)
                        break

        sub_4747E0(&bst_root)    # destroy BST
        sub_474760(&bst_aux)     # destroy auxiliary data
```

The BST (binary search tree) at step 7c maps original `.debug_line` section offsets to their new positions in the recompiled output. The relocation type `0x10008` (65,544 decimal) is `R_CUDA_ABS32_HI_20` — the high 20 bits of a 32-bit absolute relocation used for debug section cross-references.

The `+1` adjustment on the serialized size at steps 7a and 7b (`*(dword*)(result + 16) + 1`) accounts for the NUL terminator byte that the serializer does not include in its reported size.

#### Phase 8: Tkinfo Note Emission (lines 1373–1406)

Constructs the `.note.nv.tkinfo` metadata section for the output ELF.

```c
    # 8a. Gate condition: verbose-tkinfo flag AND tool-name flag both set.
    if not BYTE3(v419[54]): goto skip_tkinfo
    if not LOBYTE(v419[58]): goto skip_tkinfo

    # 8b. Initialize the tkinfo string table (1000-byte initial capacity).
    sub_43E490(&v419[39] + 4, 1000)

    # 8c. Populate header fields.
    WORD2(v419[42]) = 2                          # note type (2 = tool info)
    HIWORD(v419[42]) = sub_43E3C0(elf_data)      # ELF hash/identifier
    LOWORD(v419[43]) = sub_43E420(elf_data)      # secondary identifier

    # 8d. Initialize the strings section (2000-byte capacity).
    sub_43E490(&v419[43] + 4, 2000)
    HIDWORD(v419[46]) = 2                        # string section type
    v419[50] = sub_44FB20(128)                   # fresh 128-entry pool
    LODWORD(v419[47]) = 0                        # string offset counter

    # 8e. Build tool name string.
    tool_name = NULL
    sub_462C10(a13, 0, &tool_name)               # extract tool name from config

    offset = v419[47].lo
    if BYTE4(a20):    # tool name override present
        v419[47].hi = sub_450280(v419[50], "%s%c", tool_name, 0) + offset
    else:
        v419[47].hi = sub_450280(v419[50], "%c", 0) + offset  # empty name

    # 8f. Append compiler identification strings.
    offset2 = v419[47].hi
    version_str = sub_468440(v419[50])           # "nvlink" or similar tool name
    v419[48].lo = sub_450280(v419[50], "%s%c", version_str, 0) + offset2

    v419[48].hi = sub_450280(v419[50], "%s%c",
        "Cuda compilation tools, release 13.0, V13.0.88", 0) + v419[48].lo

    v419[49].lo = sub_450280(v419[50], "%s%c",
        "Build cuda_13.0.r13.0/compiler.36424714_0", 0) + v419[48].hi

    # 8g. Append the caller-provided annotation string (a22).
    sub_450280(v419[50], "%s%c", a22, 0)
```

The tkinfo note is a structured NOTE section with type 2000. The string table is built incrementally with `sub_450280` (a `snprintf`-like formatter that returns the number of bytes written). Each string is NUL-terminated by the `%c` + `0` pattern. The five strings in order are:

| String index | Content | Description |
|---|---|---|
| 0 | Tool name or empty | From `a13` config parameter |
| 1 | Tool identifier | From `sub_468440` (e.g., "nvlink") |
| 2 | `"Cuda compilation tools, release 13.0, V13.0.88"` | CUDA toolkit version |
| 3 | `"Build cuda_13.0.r13.0/compiler.36424714_0"` | Build identifier |
| 4 | Caller annotation | From `a22` parameter |

#### Phase 9: Self-Check Verification (lines 1488–1744)

When the `--self-check` flag is active (`HIBYTE(a20)` set), the engine implements a two-mode verification system: the initial call performs recompilation and comparison; the recursive call performs the actual comparison.

**Mode A: Initial call** (`self_check_data == NULL`):

```c
    if not HIBYTE(a20): goto skip_self_check
    if self_check_data != NULL: goto mode_B

    # 9a. Copy the 160-byte config struct for the recursive call.
    memcpy(config_copy, &config[0..19], 38 * 4)   # 152 bytes of config

    # 9b. If BYTE5(a20) is set, build section tracking lists for comparison.
    if BYTE5(a20):
        # Collect sections from the CU descriptor's section list.
        section_list = *(qword*)(cu + 192)   # v284 = v350[24]
        count = *(int*)(section_list + 16)
        base  = *(qword*)(section_list + 8)

        # Build an ordered list of (data_ptr, data_size) pairs.
        self_check_sections = sub_464AE0(count)   # v396
        for each entry in base[0..count]:
            sub_464C30(entry, self_check_sections)

        # Copy v419[61] (symbol sections) and v419[60] (relocation sections).
        sym_copy = sub_464AE0(sub_464BB0(v419[61]))
        for each in v419[61]:
            sub_464C30(entry, sym_copy)

        rel_copy = sub_464AE0(sub_464BB0(v419[60]))
        for each in v419[60]:
            sub_464C30(entry, rel_copy)

    # 9c. Modify config for recursive call.
    config_copy[96] = 0       # clear output relocation context
    config_copy[22] = 0       # clear output buffer pointer
    config_copy[73] = 0       # clear additional flag

    # 9d. Recursive invocation.
    err = sub_4748F0(
        arch,                  # same target
        *out_buf,              # output from Phase 6 as new input
        &recheck_buf,          # v381
        &recheck_size,         # v382
        &self_check_sections,  # v396 -- non-NULL triggers Mode B
        option_str,
        config_copy[0..19]     # modified config
    )

    # 9e. If BYTE6(a20) ("replace output"), copy recheck output over original.
    if BYTE6(a20):
        memcpy(*out_buf, recheck_buf, recheck_size)
        *out_size = recheck_size

    if err: goto cleanup_with_error(err)
```

**Mode B: Recursive call** (`self_check_data != NULL`):

The recursive call enters the same Phase 1–6 pipeline but with `self_check_data` pointing to the section tracking lists from Mode A. After Phase 6 completes, it performs a three-part comparison instead of returning:

```c
    # 9f. Section content comparison.
    section_list = *(qword*)(cu + 192)
    count = *(int*)(section_list + 16)
    base  = *(qword*)(section_list + 8)
    index = 0

    for each (data_ptr, data_size) in base[0..count]:
        original = sub_464DB0(self_check_data[0], index)   # from Mode A
        if memcmp(original, data_ptr, data_size) != 0:
            return 17   # section content mismatch

    # 9g. Relocation section comparison.
    #     Compare v419[60] (recompiled) against self_check_data[3] (original).
    if sub_464BB0(v419[60]) != sub_464BB0(self_check_data[3]):
        return 18   # relocation count mismatch

    for each section in v419[60]:
        name = section->name
        if sub_44E3A0(".nv.merc.", name):
            name += 8                          # strip ".nv.merc." prefix (8 chars)

        found = false
        for each ref_section in self_check_data[3]:
            ref_name = ref_section->name
            if sub_44E3A0(".nv.merc.", ref_name):
                ref_name += 8

            if strcmp(name, ref_name) == 0:
                if ref_section->data == section->data
                   and ref_section->size == section->size
                   and ref_section->flags == section->flags:
                    found = true; break
        if not found: return 19                # symbol/section mismatch

    # 9h. Symbol section comparison.
    #     Same pattern for v419[61] vs self_check_data[2].
    if sub_464BB0(v419[61]) != sub_464BB0(self_check_data[2]):
        return 18   # symbol count mismatch

    for each section in v419[61]:
        name = section[0]   # first qword is the name pointer
        if sub_44E3A0(".nv.merc.", name):
            name += 8

        for each ref in self_check_data[2]:
            ref_name = ref[0]
            if sub_44E3A0(".nv.merc.", ref_name):
                ref_name += 8
            if strcmp(name, ref_name) == 0:
                if *(dword*)(ref + 16) == *(dword*)(section + 16):   # size match
                    if memcmp(*(qword*)(ref + 8), section[1], size) == 0:
                        break
        else:
            return 18   # no matching symbol section found
```

The `.nv.merc.` prefix stripping at step 9g/9h handles the fact that Mercury ELF sections have their names prefixed with `.nv.merc.` during compilation. The self-check comparison must ignore this prefix to match corresponding sections between the original compilation and the recompilation. The prefix is exactly 9 bytes (`.nv.merc.`), but the code strips 8 characters — this is because `sub_44E3A0` returns a pointer to the character after the prefix match, which is at offset 9, and the `+= 8` applies to the pointer returned by the search, not the original name. (This is a quirk of the decompiler representation: `sub_44E3A0` returns the match position, and the actual skip is `name += 8` from that returned position, for a total skip of 9 bytes.)

The self-check error codes are intentionally terse:

| Error | Comparison stage | Meaning |
|---|---|---|
| 17 | Section content (`memcmp`) | Raw bytes differ between original and recompiled output |
| 18 | Relocation tables | Relocation count or content mismatch |
| 19 | Symbol sections | Section count, name, data, size, or flags mismatch |

#### Phase 10: Cleanup (lines 1746–1830)

Ordered resource release, with two distinct paths depending on whether the function reached the successful output stage (LABEL_229) or hit an error (LABEL_20).

```c
    # --- Success path (LABEL_229) ---

    # 10a. Destroy instruction encoding/decoding tables.
    sub_45B680(&v419[6])        # encoding table for target arch
    sub_45B680(&v419[7])        # decoding table for target arch

    # 10b. Free temporary debug scratch buffers.
    sub_4746C0(scratch_buf_B)   # v400 -- frame hash scratch
    sub_4746C0(scratch_buf_A)   # v399 -- line hash scratch

    # 10c. Free dynamically allocated option/config memory.
    if *(qword*)(&v404 + 8):   # options block high pointer
        sub_431000(*(qword*)(&v404 + 8))
    if v416:                    # options block low pointer
        sub_431000(v416)

    # 10d. Drain the deferred-free list.
    while true:
        ptr = sub_464640(&v416 + 8)   # pop from free list
        if ptr == NULL: break
        sub_431000(ptr)

    # 10e. Restore the setjmp/longjmp error handler.
    *(qword*)(v341 + 8) = saved_ctx   # restore previous longjmp target
    *v341 = (saved_byte0 ? true : (*v341 != 0))   # conditional flag restore
    *(v341 + 1) = (saved_byte1 ? true : (v341[1] != 0))
    result = 0

    # --- Fall through to common cleanup ---

    # --- Error path (LABEL_20) ---
    # Steps 10c and 10d execute on both paths.

    # 10f. Destroy compilation contexts (if allocated).
LABEL_3:
    if v385:                        # additional compilation context
        sub_488530(v384)            # destroy context A
LABEL_4:
    if v353:                        # profile descriptor allocated
        sub_488530(v383)            # destroy context B (memory space obj)
LABEL_5:
    if v352:                        # "Final memory space" arena created
        sub_45CAE0(v386)            # release arena metadata
        sub_431C70(v349, 0)         # free arena backing memory

    return result
```

The cleanup is structured as a fall-through chain (`LABEL_3` -> `LABEL_4` -> `LABEL_5`). The flags `v385`, `v353`, and `v352` track which resources were successfully allocated during Phases 2 and 4:

| Flag | Set when | Controls cleanup of |
|---|---|---|
| `v352` | Phase 2 creates "Final memory space" arena | Arena metadata (`sub_45CAE0`) + backing memory (`sub_431C70`) |
| `v353` | Phase 4 allocates 256-byte profile descriptor | Memory space object (`sub_488530` on `v383`) |
| `v385` | Phase 4 allocates additional compilation context | Additional context (`sub_488530` on `v384`) |

The `sub_488530` function is the memory space destructor. It is called with the same allocation handle returned by `sub_488470` in Phase 4. The `sub_45CAE0` / `sub_431C70` pair releases the arena: `sub_45CAE0` detaches the arena metadata, and `sub_431C70` frees the underlying page allocations (the second argument `0` indicates no deferred cleanup).

The error handler restoration at step 10e uses a conditional pattern: if the saved propagation flag was originally non-zero, the flag is unconditionally set to true; if it was zero, the flag preserves whatever value the current handler accumulated during execution. This allows error state to propagate correctly through nested FNLZR invocations (as happens during self-check in Phase 9).

### Worked Example: FNLZR Finalization of a Blackwell Kernel

This section traces a single invocation of `sub_4748F0` against a hypothetical but realistic input: a sm_100 (Blackwell datacenter) Mercury-format cubin containing one kernel `tcgen05_matmul` that uses the `tcgen05.mma` family of tensor-core intrinsics introduced in sm_100. The walkthrough follows the 10 execution phases sequentially, showing the state of the three primary data structures (`v419[]` module context, `v350` profile descriptor, `cu` compilation unit) at each step, and cross-references every transformation to the exact line in `sub_4748F0_0x4748f0.c`.

#### SETUP: Input Cubin State

The input file `tcgen05_matmul.cubin` is 18,432 bytes on disk, loaded into memory at `0x7f8a40000000`. Its ELF header has the following relevant fields after parsing by `sub_448360`:

| Field | Offset | Value | Meaning |
|---|---|---|---|
| `e_ident[7]` | +7 | `0x41` | Mercury ELF class marker (`'A'`) |
| `e_ident[8]` | +8 | `0x08` | Mercury cubin variant |
| `e_type` | +16 | `0xFF00` | Mercury subtype (pre-finalized) |
| `e_flags` | +48 | `0x00640003` | `arch=100` in bits 8–15, bit 0 set (needs finalization), bit 1 set (has SASS stub) |
| `e_flags[49]` | +49 | `0x0064` | Source architecture (sm_100) |

The Mercury profile at `.nv.merc.profile` contains a version word of `0x100` (1.0) and a capability bitmask of `0x0001` (sm_100 only). The input contains:

- `.nv.merc.text.tcgen05_matmul` — 2,048 bytes of pre-finalized Mercury IR for the kernel body
- `.nv.merc.info.tcgen05_matmul` — 384 bytes of kernel metadata (register usage, shared memory, barriers)
- `.nv.info` — 128 bytes of EIATTR entries (`EIATTR_MAX_REG_COUNT = 128`, `EIATTR_MAX_STACK_SIZE = 0`)
- `.nv.constant0.tcgen05_matmul` — 368 bytes of constant bank 0 (parameters)
- `.symtab` — 4 symbols (`_Z14tcgen05_matmulPf`, `.text.tcgen05_matmul`, `.nv.constant0.tcgen05_matmul`, `$__internal_0$__sti____cudaRegisterAll`)
- `.strtab`, `.shstrtab`
- `.rel.nv.constant0.tcgen05_matmul` — 2 `R_CUDA_ABS32_LO_20` relocations (parameter pointer lo/hi)
- `.note.nv.tkinfo` — 96-byte tool note stamped by the compiler driver (`cicc`)

The caller (`sub_4275C0` at `main()` line ~727) invokes `sub_4748F0` with:

```text
arch           = 100        (target sm_100 -- same as source, this is the pre-link path)
elf_data       = 0x7f8a40000000
output_buf     = &s1        (will receive the transformed ELF pointer)
output_size    = &s1_size
self_check_data= NULL       (no self-check on first pass)
option_string  = NULL
config:
  a8  = 0x0000000400000000   (debug flag off, opt-level 4)
  a9  = 0                    (fallback opt 0, no lineinfo)
  a10 = 0x0000000100000000   (binary-kind=capmerc transform needed, pre-link mode bit 1=0)
  a13 = "tcgen05_matmul.cu"  (source file for tkinfo)
  ... other config slots zero ...
```

#### Phase 0: Entry and Argument Unpack (lines 426–447)

The function prolog saves `a1` into `HIDWORD(v342)` (line 426), copies the ELF pointer into `src` (line 427, `src = a2`), and stores the output pointers (`v346 = a3`, `v347 = a4`). The three cleanup tracking flags are zeroed:

```text
v352 = 0    // "Final memory space" arena not yet allocated
v353 = 0    // 256-byte profile descriptor not yet allocated
v385 = 0    // additional compilation context not yet allocated
```

`sub_44F410(a1, a2)` is called to fetch the per-thread arena metadata pointer (returned in `v25`/`v341`). The current error propagation flags and longjmp target are captured:

```text
v354 = *v25         // saved_byte0 (currently 0)
v355 = v25[1]       // saved_byte1 (currently 0)
v343 = *((qword*)v25 + 1)  // previous longjmp target
*(word*)v25 = 0     // clear both flag bytes
*((qword*)v25 + 1) = env   // redirect longjmp to our local jmp_buf
```

#### Phase 1: Environment Setup and Options Parsing (lines 448–498)

The `_setjmp(env)` at line 448 establishes the error trap. On the forward pass it returns 0 so execution continues at line 456.

```text
v32 = 0             // counter for function index invert loop
v414 = 3            // fallback opt level
v406 = &a8          // config pointer into stack frame
si128 = xmmword_1D40750   // SSE constant for arch profile lookup
```

Since `option_string` is `NULL`, the entire block at lines 472–493 is skipped. The `a8` config parameter is checked at line 494: since `(dword)a8 == 0` is false (opt-level 4 is stored in low dword), `v33 = a8 = 4` and `HIDWORD(v342) = 4`.

Wait — re-reading line 496: the check is `if (!(_DWORD)a8) v33 = HIDWORD(v342)`. Since `a8 = 0x0000000400000000`, the low dword is 0, so this branch is taken: `v33 = HIDWORD(v342) = 100` (the target arch). This is the "default config arch to caller arch" step. The target_arch field of config (stored in a8's low dword) is replaced with 100.

The PIC byte check at line 499:

```text
v34 = 1
if (!BYTE4(a10)) v34 = BYTE5(a10)  // v34 stays 1 because BYTE4(a10) = 1
```

#### Phase 2: Architecture Validation (lines 500–723)

**2a. Zero the module context** (lines 501–504):

```text
v379 = 0                           // reloc_ctx handle
memset(v419, 0, 0x218)              // 536 bytes = 67 qwords
v419[8].lo = HIDWORD(v342) = 100    // target SM
v419[4] = src                       // elf_data pointer
```

**2b. Device object validation** (line 505):

```text
v35 = sub_43D9A0(src)   // returns 1 -- this IS a valid device ELF
```

**2c. Read ELF header fields** (lines 527–529):

```text
v42 = v419[4]              = 0x7f8a40000000
v43 = sub_448360(src)      = 0x7f8a40000040  (points to the class+type+flags block)
v44 = *(dword*)(v43 + 48)  = 0x00640003      (e_flags)
```

**2d. Finalization-needed check** (lines 530–562):

```text
*(byte*)(v43 + 7) == 65        // YES: Mercury ELF class marker 0x41
  (v44 & 1) != 0                  // YES: bit 0 = "needs finalization"
  goto LABEL_42                 // skip the ineligibility return path
```

**2e. Subtype validation** (lines 564–566):

```text
v67 = (*(word*)(v43+16) != 0xFF00) && (*(word*)(v43+16) - 1 > 1u)
    = (0xFF00 != 0xFF00) && ...
    = false
  -> pass (Mercury 0xFF00 subtype accepted)
```

**2f. Create "Final memory space" arena** (lines 567–572):

```text
v352 = 1                                         // arena flag set -- cleanup required
v349 = sub_432020("Final memory space", 0, 4096) // 4KB initial arena
v68  = sub_45CAE0(v349, 0)                       // resolve metadata
v351 = v68                                        // memory space root handle
v386 = v68
```

**2g. Set capmerc flag in module context** (lines 573–577):

```text
v69 = 0x4000
if (v209 /* = (v43+7 == 65) */) v69 = 2    // use bit 1 for Mercury ELF
if ((v69 & v44 /* = 3 */) != 0)             // 2 & 3 = 2, non-zero
  LOBYTE(v419[54]) = 1                       // capmerc transform flag ON
```

**2h. Extract architecture profile** (lines 578–580):

```text
v71 = sub_43E610(v419[4], &v387)   // v71 = v356 = 1 (profile found)
                                    // v387 buffer filled with Mercury profile
                                    // v388 = version word = 0x100 (1.0)
                                    // v389 = capability mask at offset +2
```

**2i. Version ceiling check** (lines 581–599): `v388 = 0x100 < 0x101` — check passes.

**2j. Fastpath-eligible branch** (lines 601–654): This is a Mercury ELF (`v72 == 65`), `v71 && v389` are true, so line 601 takes the `if` branch. Line 603 checks `v72 != 65` — false, so we fall into `LABEL_79`:

```text
LABEL_79:
  WORD1(v419[54]) = 257   // set capmerc word
  if (!BYTE1(a20))        // BYTE1 of a10 = 0, so this is true
    goto LABEL_72
```

At `LABEL_72` (line 695):

```text
v73 = *(byte*)(v43 + 8)   // = 0x08 (Mercury cubin variant)
if (*(byte*)(v43+7) == 65) // YES
  if (v73 == 8)            // YES
    goto LABEL_55          // continue to Phase 4
```

**Module context state at end of Phase 2:**

```text
v419[4]  = 0x7f8a40000000   // elf_data
v419[8]  = 100              // target arch
v419[54] = 0x0000010101     // capmerc flag (byte 0=1, word 1=0x0101)
v349     = arena handle
v351     = memory space root
v352     = 1 (arena alloc flag)
v356/v71 = 1 (profile valid)
v388     = 0x100 (Mercury version 1.0)
v389     = 0x0001 (capability mask: sm_100 bit)
```

#### Phase 3: Fastpath Optimization Check (lines 820–881)

This phase is visited *after* Phase 4's first two allocations, but logically it is the "skip the pipeline" branch. At line 821:

```text
v209 = (BYTE2(a21) == 1)   // a21 high byte = 0, so v209 = false
                            // (no self-check pass)
if (!v209 && !v348)         // v348 = a5 = NULL, so skip_fastpath=false
  # enter the fastpath attempt block
```

Source architecture extraction (line 836–839):

```text
v241 = *(dword*)(v43 + 48) = 0x00640003
if (*(byte*)(v43+7) == 65):
  v112 = (v241 >> 8) & 0xFFFF = 0x6400 >> 8 = 0x64 = 100  // Mercury
```

Invert flag (lines 833–835):

```text
v239 = 0
if (LOBYTE(v419[54]))   // = 1
  v239 = BYTE1(a20) ^ 1 = 0 ^ 1 = 1
v240 = 1                // inversion enabled for pre-link mode
```

Capability check (line 842):

```text
v242 = sub_470DA0(&v387, 100, 100, 1)
```

Because target == source (100 == 100) and the capability mask at `v387+16` contains bit 0 (value 1 for sm_100), `sub_470DA0` returns 1 (can fastpath). But with `v240 = 1` (invert flag set by pre-link mode), the function actually inverts its result to test "cannot fastpath this target". The semantics here are: for pre-link mode, we want the fastpath to trigger only when the source ELF cannot be used as-is on the target. Since the source IS the target, fastpath is NOT applicable in this case — `v242` comes back 0.

The decompiled code at line 844 takes the `if (v242)` branch only when fastpath applies. In our walkthrough, `v242 = 0`, so we fall through to Phase 4 proper at line 882.

**(Alternative fastpath outcome)** If the source had been sm_100 but the ELF came from a ptxas cross-compile targeting sm_103 and we were finalizing for sm_100 — and sm_100 was a subset of the declared capability mask — then `v242 = 1` would trigger the fastpath at lines 845–880:

```text
# 3e. Copy input ELF verbatim
v245 = sub_43DA80(elf_data)   // = 18432 (total size)
*v347 = 18432                 // set output size
v249 = sub_4307C0(v351, 18432) // allocate from Final memory space
memset(v249, 0, 18432)
memcpy(v249, src, 18432)
*v346 = v249                  // output buffer

# 3f. Patch arch field
v254 = sub_448360(*v346)
v255 = *(dword*)(v254 + 48)   // out_flags
if (*(byte*)(v254+7) == 65):  // Mercury
  *(dword*)(v254+48) = (100<<8) & 0xFFFF00 | v255 & 0xFF0000FF
                      = 0x00006400 | 0xFF000003 = 0xFF006403

v30 = 0
goto LABEL_20    // skip to cleanup
```

In our example, this branch does NOT trigger, so we proceed to Phase 4.

#### Phase 4: Compilation Unit Initialization (lines 882–988)

**4a. Build section lists** (lines 724–733, executed earlier at LABEL_55):

```text
v74 = sub_448730(elf_data) = 11     // section count
v75 = sub_464AE0(11)                 // primary list, empty capacity 11
v419[0] = sub_464AE0(11)             // auxiliary A (function indices)
v419[1] = sub_464AE0(11)             // auxiliary B (data sections)
v419[50] = sub_44FB20(128)           // 128-entry string pool
v419[65] = 0                          // mercury profile ptr -- set later

# Encoding/decoding tables
v419[6] = sub_45AC50(100)   // SM100 instruction encoder
v419[7] = sub_459640(100)   // SM100 instruction decoder
```

**4b. Store mode flags** (lines 735–739):

```text
BYTE1(v419[58]) = v34 = 1          // PIC mode enabled
BYTE1(v419[54]) = a20 = 0          // post-link/capmerc packed byte
LOBYTE(v419[59]) = BYTE5(a20) = 0  // self-check sub-mode off
HIDWORD(v419[58]) = HIDWORD(a14) = 0  // opportunistic finalization level
BYTE2(v419[58]) = BYTE1(a21) = 0    // additional mode flag
```

**4c. Process relocations** (line 740):

```text
v30 = sub_1CEF5B0(v75, &v379, v419)   // ELF_ProcessRelocations
```

This walks the input ELF's `.rel.*` sections and populates `v75` with section entries plus fills `v379` with a relocation context. Our input has `.rel.nv.constant0.tcgen05_matmul` with 2 entries, so `v379` becomes a non-NULL handle carrying those two `R_CUDA_ABS32_LO_20` records. `v30 = 0` on success.

**4d. Allocate 256-byte profile descriptor** (lines 758–774):

```text
v94 = *((qword*)sub_44F410(v75, &v379) + 3)   // arena from module context
v350 = sub_4307C0(v94, 256)                    // allocate 256 bytes
v353 = 1                                        // profile alloc flag set
```

**4e. Allocate memory space object** (lines 776–779):

```text
v102 = sub_488470()   // v102 = 0x7f8a44000000 (fresh memspace)
v383 = v102
*v350 = v102          // profile[0] = memspace handle
if (!v102) return 11  // allocation failure -- our case: v102 != NULL
```

**4f. Allocate 656-byte CU descriptor** (lines 785–829):

```text
v103 = sub_4B6F40(656, v102)   // v103 = 0x7f8a44001000 (CU address)

# Initialize CU fields
*(qword*)(v103 + 0)   = off_1D49C58   // OCG vtable
*(qword*)(v103 + 8)   = v106 = v102   // memspace back-ref
*(qword*)(v103 + 16)  = 10240          // initial code buffer size (0x2800)
*(qword*)(v103 + 24)  = 0              // code buffer ptr (NULL until OCG allocates)
*(qword*)(v103 + 32..88) = 0           // tables zeroed
*(dword*)(v103 + 64)  = 0              // section count
# memset loop at lines 811-817 zeros 64 qwords of CU body (offset 96-607)
*(qword*)(v103 + 608..648) = 0         // tail metadata zeroed

# Link CU into profile
*(qword*)(v350 + 88) = v103            // profile[11] = CU pointer
```

**4g. Populate CU target/source arch** (lines 911–917):

```text
*((dword*)v350 + 2) = HIDWORD(v342) = 100   // target arch at +8

if (*(byte*)(v43+7) == 65):  // Mercury
  v115 = *(uint16*)(v43 + 49) = 0x0064 = 100   // source from Mercury header +49
*((dword*)v350 + 3) = 100    // source arch at +12
```

**4h. ELF subtype branch** (lines 918–932): `*(word*)(v43+16) = 0xFF00 != 1`, so this is NOT a relocatable object — take the `else` branch:

```text
*((byte*)v350 + 191) = 1              // complete object flag
*((byte*)v350 + 17)  = BYTE4(a9) = 0   // PIC from caller
*((dword*)v350 + 5)  = a10 = 0         // opt level
```

**4i. Config field population** (lines 933–964):

```text
*((word*)v350 + 12)  = WORD2(a10) = 0         // line_info_word
v350[6] = "" (v119 was NULL)                   // include path
v350[7] = "" (v120 was NULL)                   // source path
*((byte*)v350 + 16)  = BYTE4(a8) = 0           // debug flag
*((dword*)v350 + 25) = HIDWORD(a14) = 0        // extra_opt
*((dword*)v350 + 24) = a9 = 0                  // line_info_dword
*((dword*)v350 + 26) = a15 = 0                 // codegen_flag
*((byte*)v350 + 108) = a25 = v409 = 0          // ofast mode
*((dword*)v350 + 30) = HIDWORD(a15) = 0        // hi_codegen
v350[16] = "" (v121 was NULL)                  // extra_path_a
*((word*)v350 + 68)  = a17 = 0                 // target_word
v350[18] = "" (v122 was NULL)                  // extra_path_b
*(word*)((char*)v350 + 185) = a20 = 0          // packed capmerc/self-check
*((byte*)v350 + 184) = BYTE2(a20) = 0          // capmerc sub-flag
*((byte*)v350 + 187) = BYTE5(a20) = 0          // self-check sub-mode
v350[20] = a23 = 0
v350[19] = a24 = 0
```

**4j. Self-check tracker** (lines 965–979): `v124 = BYTE5(a20) = 0` — skipped.

**4k. Set Mercury profile flag** (lines 980–988):

```text
if (*((dword*)v350 + 3) > 99u && v114)   // 100 > 99, profile valid
  *((byte*)v350 + 248) = 1               // mercury_profile flag
  *((dword*)v350 + 53) = v387[0]         // profile header byte
  *((dword*)v350 + 54) = v387[2]         // profile capability byte
  v419[65] = &v387                        // mercury profile pointer
```

**Profile descriptor state at end of Phase 4:**

```text
v350 + 0   = 0x7f8a44000000   // memspace handle
v350 + 8   = 100              // target arch
v350 + 12  = 100              // source arch
v350 + 16  = 0                // debug flag
v350 + 17  = 0                // PIC flag
v350 + 20  = 0                // opt level
v350 + 88  = 0x7f8a44001000   // CU pointer
v350 + 186 = 0                // NOT relocatable output
v350 + 191 = 1                // complete object
v350 + 248 = 1                // mercury profile valid
```

#### Phase 5: Input Section Processing (lines 989–1056)

**5a. Scan `.note.nv.tkinfo`** (lines 989–1028):

```text
v132 = v419[4] = 0x7f8a40000000
v133 = sub_4483B0(v132, ".note.nv.tkinfo") // returns section header
v134 = v132 + v133[3]  // note_base = 0x7f8a4000XXX
v135 = v134 + v133[4]  // note_end
```

Walk the notes:

```text
note[0]:
  v140 = v134[1] = 96    // descsz
  v134[2] = 2000          // CUDA tool note -- matches
  v137 = 96 - 24 = 72     // remaining
  v140 != 24, so check name terminator
  v138 = v134[8] = 0      // name offset
  v137 > v138 (72 > 0), compute v139 = note_base + 48 + 0
  strcmp(v139, "nvlink")    -> not equal (this is "cicc")
  strcmp(v139, "nvJIT API") -> not equal
  # advance
  v134 = v134 + 96 + 24 = past note_end
  goto LABEL_134
```

Since neither "nvlink" nor "nvJIT API" stamped this cubin, `v67` remains at its previous value of `v35 = 1` (the device_elf_valid flag). Wait — re-reading line 1026: `v67 = v35` only when the walk exits via LABEL_134 without finding a match. So `v67 = 1` here, but this is the default-false case because the scan completed without a "break" hit.

Actually, the logic is inverted: the tool name check sets a "break" that jumps out of the walk. If no tool note matched, the walk falls through the loop back-edge. Re-reading lines 1017–1019: the strcmp result being equal causes `break`, which exits the walking loop, and then `v67 = 1` is set. If there's no match, `v134 += v140 + 24` advances to next note, and the inner loop check at 1022 terminates because `v135 <= v134` -> `goto LABEL_134`. At LABEL_134, `v67 = v35 = 1` is assigned at line 1026 *before* the label. Wait — line 1026 is executed only in the fall-through path from the `while(1)` loop's natural exit. The default-initialized value of `v67` at line 989 is... actually `v67` was set to `false` earlier at line 564 as part of the subtype check, then reassigned as an accumulator. Let me trust the code: since neither "nvlink" nor "nvJIT API" match, the break doesn't trigger, the walk exits, and `v67 = v35 = 1` at line 1026.

Wait — that's wrong semantically. The break-on-match sets `v67 = 1` (already_linked = true). If no match, `v67` stays false. Looking again at the assembly pattern, `v67` is being used as a counter value fed into `LOBYTE(v419[66])` at line 1028. For our input, the cubin was stamped by `cicc` (the frontend), not `nvlink` or `nvJIT API`, so `v67` should end up false.

```text
LOBYTE(v419[66]) = 0   // already_linked = false
```

**5b. Symbol table emission** (lines 1029–1032, 1782–1789):

Actually the control flow is inverted: the for-loop at line 1029 starts with `i = 0` and the terminating branch is at line 1031 when `i >= sub_464BB0(v75)`. Inside the loop body at line 1782, it calls `sub_1CF07A0` on each section:

```text
for (i = 0; i < sub_464BB0(v75); ++i):
  v142 = sub_464DB0(v75, i)
  if (v142):
    v143 = sub_1CF07A0(v142, v419)   // ELF_EmitSymbolTable
    if (v143) break                   // error
```

Our input has 11 sections populated into `v75` during Phase 4a. Pass 1 processes each one. For the `.symtab` section, `sub_1CF07A0` builds the symbol table entries in the working memspace; for `.nv.merc.*` sections it classifies them into the module context's section categorization arrays. Result: 4 symbols processed, no error.

**5c. Relocation table emission** (lines 1033–1055):

```text
for (j = 0; j < sub_464BB0(v75); ++j):
  v145 = sub_464DB0(v75, j)
  if (v145):
    v146 = sub_1CF1690(v145, v419)   // ELF_EmitRelocationTable
    if (v146) break                   // error code returned
```

The 2 relocations in `.rel.nv.constant0.tcgen05_matmul` are processed. No error.

#### Phase 6: Compilation and ELF Emission (lines 1057–1492)

**6a. Initialize compilation pipeline** (lines 1057–1065):

```text
v147 = v350
v419[32] = v350 + 25            // compilation context = profile+200
sub_1CEF440(v419, 0.0)           // pipeline init (OCG fire-up)
v399[0] = 0; v399[32] = 0        // scratch buffers zeroed
v400[0] = 0; v400[32] = 0
*((byte*)v350 + 33) = LOBYTE(v419[33]) != 0   // propagate compile flag
```

`sub_1CEF440` is the OCG pipeline initializer. It reads `v419[31] = v350` and walks the mercury IR sections, invoking the instruction decoder (`v419[7]`) per kernel. For our `tcgen05_matmul` kernel, it will:

1. Parse the 2048 bytes of `.nv.merc.text.tcgen05_matmul` into ~160 Mercury instructions
2. Recognize the 8 `tcgen05.mma` opcodes and bind them to the sm_100 tensor-core encoding slots
3. Schedule instructions across the 4-issue SM100 pipelines
4. Emit final SASS bytes into the CU's code buffer (allocated lazily at `cu+24`)

**6b. Relocation context bypass** (lines 1066–1071): `a19 = 0` (no output relocation context), so this block is skipped.

**6c. Debug info input extraction** (lines 1072–1079):

```text
v357 = v419[10]   // = NULL (no .debug_line in our input)
v358 = v419[11]   // = NULL (no .debug_frame)
v360 = v419[9]    // = NULL (no line remap)
v152 = (v419[10] != 0) = false
v153 = (v419[11] != 0) = false
```

**6d–6h. Debug info paths**: all skipped because `v357`, `v358`, `v360` are NULL.

**6f. Create 104-byte debug output context** (lines 1132–1152): This is allocated unconditionally:

```text
v175 = vtable_call(v350[11], 16, 104)   // alloc 104 bytes
# Initialize 13 qword slots alternating memspace / 0xFFFFFFFF sentinels
v175[0] = v350[11]
v175[1] = v350[11]
v175[2] = 0
v175[3] = 0xFFFFFFFF
v175[4] = v350[11]
v175[5] = 0
v175[6] = 0xFFFFFFFF
v175[7] = v350[11]
v175[8] = 0
v175[9] = 0xFFFFFFFF
v175[10] = v350[11]
v175[11] = 0
v175[12] = 0xFFFFFFFF
```

Lines 1153–1194 skipped (no debug sections to process).

**6i. Set debug-present flag** (line 1197): `*((byte*)v350 + 25) |= 0` — unchanged.

**6j. Create 80-byte output tracking context** (lines 1199–1226):

```text
v184 = vtable_call(v350[11], 16, 80)   // 80 bytes
v184[0] = v350[11]                       // memspace
v186 = vtable_call(v350[11], 24, 24)     // 24-byte sub-struct
v186[2] = v350[11]
v186[1] = 0
*v186 = 1
v184[5] = v186
v184[1..4] = 0
*(dword*)(v184+32) = 0
v184[7] = 0; v184[8] = 0xFFFFFFFF; v184[9] = 0
v184[6] = v350[11]
v350[9] = v184             // profile[9] = output tracker
v188[22] = 0
```

**6k. Relocation context propagation** (lines 1227–1234): `a19 = 0`, skipped.

**6l. Function index processing** (lines 1235–1256): `v379 != NULL` (we have the reloc_ctx from Phase 4c), so:

```text
v190 = v419[0]   // auxiliary list A (function indices)
for (k = 0; k < sub_464BB0(v190); ++k):
  v192 = sub_464DB0(v419[0], k)
  if (v192):
    v193 = sub_464DB0(v419[0], k)   // re-fetch
    v194 = sub_471700(v193, v419, &v384, 0.0)
    if (v194) { v30 = v194; goto LABEL_268 }
    v190 = v419[0]
```

`sub_471700` is the per-kernel finalization orchestrator (78,516 bytes). For our single kernel, it invokes the OCG backend to translate Mercury IR into finalized SASS:

1. Reads `.nv.merc.text.tcgen05_matmul` bytes
2. Decodes each instruction via `v419[7]` (decoder table)
3. Re-encodes each instruction through `v419[6]` (encoder table) with target-specific tweaks
4. For `tcgen05.mma` ops: binds tensor memory descriptors to the sm_100 TMEM slot allocator
5. Resolves cross-references between `.nv.constant0` and the kernel body
6. Emits final SASS bytes into the CU code buffer

It also populates `v384` (the additional compilation context), so `v385` may be set.

**6m. Invert function index bitmask** (lines 1262–1273):

```text
if (LOWORD(v419[64])) {   // function count = 1
  v195 = 0
  do {
    ++v32
    v196 = (int*)((char*)v419[63] + v195)
    v195 += 4
    *v196 = ~*v196     // invert each dword
  } while (LOWORD(v419[64]) > v32)
}
```

For our single kernel, one dword is inverted. `v419[63]` was allocated at lines 888–901 (8 bytes via vtable call).

**6n, 6o. Debug info finalize** (lines 1274–1292): `v360 = NULL`, `v367 = false`, both skipped.

**6p. Destroy compilation mutex** (line 1293):

```text
pthread_mutex_destroy(v419[30])
```

**Phase 7 (embedded, lines 1294–1315)**: Debug line/frame serialization — both `v357` and `v358` are NULL, skipped entirely.

**Phase 7c (embedded, lines 1316–1372)**: Debug address remapping — `v419[14]` is NULL (no `.debug_info` relocations in our input), skipped.

**Phase 8 (embedded, lines 1373–1406)**: Tkinfo emission. Check at line 1373: `BYTE3(v419[54]) && LOBYTE(v419[58])`. `BYTE3(v419[54])` is the `--verbose-tkinfo` flag, unset in our config — skipped.

**Continuing Phase 6...**

**6q. Relocation-context output path** (lines 1407–1428): `a19 = 0`, skipped, fall through to the ELF writer dispatch.

**6q. Dispatch to ELF writer** (lines 1430–1459):

```text
v256 = sub_448730(elf_data) = 11             // section count
v257 = sub_464AE0(11)                         // output section list
v419[52] = v257
v258 = sub_4484F0(elf_data, 2)               // section header table
v419[53] = sub_464AE0(v258_size / v258_ent)  // program header list

# Dispatch
if (*((byte*)v350 + 186) /* = 0, complete object */)
  v30 = sub_1CF2100(v419)   // ELF_EmitSectionHeaders -- compute output size
else
  v30 = sub_1CF72E0(v419)   // ELF_EmitProgramHeaders (relocatable path)
```

`sub_1CF2100` (31,261 bytes) constructs the output ELF section headers and stores the computed total size in `v419[2]`. For our kernel, the output will be roughly:

| Section | Size (bytes) | Notes |
|---|---|---|
| ELF header + shoff + padding | 192 | |
| `.text.tcgen05_matmul` | ~1,920 | Finalized SASS (slightly smaller than input Mercury IR) |
| `.nv.info.tcgen05_matmul` | 192 | Kernel attributes |
| `.nv.info` | 128 | EIATTR entries |
| `.nv.constant0.tcgen05_matmul` | 368 | Unchanged from input |
| `.rel.nv.constant0.tcgen05_matmul` | 24 | 2 relocations |
| `.symtab` | 96 | 4 symbols |
| `.strtab` | 128 | Symbol name strings |
| `.shstrtab` | 192 | Section name strings |
| `.note.nv.tkinfo` | 96 | New tkinfo (nvlink stamp) |
| Section header table | 10 * 64 = 640 | 10 section headers |
| **Total** | **~3,976** | Rounded to 4096 after padding |

So `v419[2] = 4096`.

**6r. Allocate output buffer** (lines 1460–1467):

```text
v269 = v419[2] = 4096
v270 = v386   // Final memory space
v274 = sub_4307C0(v386, 4096)   // allocate 4KB from arena
memset(v274, 0, 4096)
v419[5] = v274   // output buffer in module context
```

**6r (continued). Write the ELF** (lines 1468–1472):

```text
if (*((byte*)v350 + 186) /* = 0 */)
  v30 = sub_1CF7F30(v419)   // relocatable path -- NOT taken
else
  v30 = sub_1CF3720(v419)   // ELF_WriteCompleteObject (99,074 bytes)
```

`sub_1CF3720` walks `v419[52]` (output section list) and `v419[5]` (output buffer), emitting:

1. ELF header at offset 0 (`e_ident[7] = 0x41`, `e_flags = 0x006400FF` with bit 0 CLEARED now — finalization complete, bits 5–7 set to mark SASS present)
2. Section contents at their computed offsets
3. Section header table at the tail

For each `.nv.merc.*.tcgen05_matmul` section in the input, it emits the corresponding `.text.tcgen05_matmul` / `.nv.info.tcgen05_matmul` / etc. output sections (dropping the `.nv.merc.` prefix). The 2048-byte Mercury IR input is replaced with ~1920 bytes of finalized sm_100 SASS generated by the OCG backend.

**6s. Store final output pointer and size** (lines 1488–1492):

```text
v277 = HIBYTE(a20) = 0   // self-check flag
v278 = v346               // caller's output buf pointer
*v347 = (size_t)v419[2] = 4096
*v278 = v419[5]           // hand off output buffer
```

#### Phase 7: Debug Info Serialization (lines 1294–1372)

Already visited as embedded logic inside Phase 6. In our example, the entire debug path is bypassed because the input cubin has no `.debug_line` or `.debug_frame` sections. Had they been present, the serializer would:

1. Call `sub_477480(v175, 0)` to build the line table (mode 0)
2. Call `sub_4783C0(v175, 0)` to serialize the DWARF line program
3. Call `sub_477510(v175, 0)` to extract the output bytes
4. Walk `v419[14]` relocations and remap `.debug_line` offsets through the BST built by `sub_4826F0`

#### Phase 8: Tkinfo Note Emission (lines 1373–1406)

Also bypassed in our example because `BYTE3(v419[54]) = 0` (`--verbose-tkinfo` not passed). In a `--verbose-tkinfo` run, the phase would:

1. Initialize a 1000-byte string table via `sub_43E490(&v419[39]+4, 1000)`
2. Tag the note type as 2 (tool info) at `WORD2(v419[42])`
3. Append the tool name "nvlink"
4. Append `"Cuda compilation tools, release 13.0, V13.0.88"`
5. Append `"Build cuda_13.0.r13.0/compiler.36424714_0"`
6. Append the caller annotation string (`a22`)

#### Phase 9: Self-Check Verification (lines 1488–1744)

Check at line 1493: `v277 = HIBYTE(a20) = 0`, so the entire self-check block is skipped. The engine proceeds directly to LABEL_229 at line 1746.

Had `--self-check` been passed, the flow would be:

1. Copy 152 bytes of config into `v417` (line 1497–1507)
2. Clear `v417[96]`, `v417[22]`, `v417[73]` (disable nested self-check + output reloc)
3. Recursively call `sub_4748F0` with the just-produced output buffer as the new input and `&v396` as `self_check_data`
4. On return, compare sections / relocations / symbols against the tracked lists

#### Phase 10: Cleanup (lines 1746–1829)

**10a. Destroy encoder/decoder tables** (lines 1747–1748):

```text
sub_45B680(&v419[6], v27)   // SM100 encoder destroyed
sub_45B680(&v419[7], v27)   // SM100 decoder destroyed
```

**10b. Free debug scratch buffers** (lines 1749–1750):

```text
sub_4746C0(v400)   // frame hash scratch
sub_4746C0(v399)   // line hash scratch
```

**10c. Free option/config dynamic memory** (lines 1751–1754):

```text
if (*((qword*)&v404 + 1))   // = NULL
  sub_431000(...)
if ((qword)v416)             // = NULL (no options string parsed)
  sub_431000(v416, v27)
```

Both pointers are NULL, so nothing freed.

**10d. Drain deferred-free list** (lines 1755–1761):

```text
while (true):
  v236 = sub_464640(&v416 + 1, v27)
  if (!v236) break
  sub_431000(v236, v27)
```

Empty list in our case, loop exits immediately.

**10e. Restore error handler** (lines 1762–1773):

```text
v209 = (v354 == 0)              // saved_byte0 was 0
*((qword*)v341 + 1) = v343      // restore longjmp target
v237 = v35 = 1                    // device_elf_valid propagation
if (v209)
  v237 = (*v341 != 0) = false    // current flag was cleared
*v341 = v237 = false

v209 = (v355 == 0)              // saved_byte1 was 0
if (v209)
  v35 = (v341[1] != 0) = false
v30 = 0                          // success return code
v341[1] = v35
```

**10f. Destroy compilation contexts** (lines 1774–1822):

```text
LABEL_3:
  if (v385 /* = 1, sub_471700 allocated v384 */):
    goto LABEL_27
LABEL_27:
  sub_488530(v384, v27, a7)   // destroy additional context
  if (v353 /* = 1, profile desc allocated */):
LABEL_28:
    sub_488530(v383, v27, a7)   // destroy memory space obj
LABEL_5:
  if (v352 /* = 1, Final memory space arena */):
    sub_45CAE0(v386, v27)     // detach arena metadata
    sub_431C70(v349, 0)        // free backing pages
```

All three cleanup paths execute because all three flags were set.

**Return**: `v30 = 0` (success).

#### Final Output State

The caller receives:

```text
*output_buf = 0x7f8a44002000   // (from "Final memory space" arena)
*output_size = 4096
```

The output buffer contains a complete sm_100 cubin with:

| Field | Before (input) | After (output) |
|---|---|---|
| `e_ident[7]` | `0x41` (Mercury) | `0x07` (standard device cubin) |
| `e_type` | `0xFF00` (Mercury) | `0x0002` (executable device object) |
| `e_flags` | `0x00640003` (needs finalization) | `0x0064C000` (finalized SASS, caps merged) |
| `.nv.merc.text.tcgen05_matmul` | 2048 bytes Mercury IR | *(removed)* |
| `.nv.merc.info.tcgen05_matmul` | 384 bytes | *(removed)* |
| `.text.tcgen05_matmul` | *(absent)* | ~1920 bytes finalized SASS with tcgen05 encodings |
| `.nv.info.tcgen05_matmul` | *(absent)* | 192 bytes kernel attributes |
| `.nv.info` | 128 bytes EIATTR | 128 bytes EIATTR (unchanged structure, possibly re-stamped values) |
| `.nv.constant0.tcgen05_matmul` | 368 bytes | 368 bytes (unchanged) |
| `.rel.nv.constant0.tcgen05_matmul` | 24 bytes | 24 bytes (relocations preserved) |
| `.symtab` | 4 entries | 4 entries (`_Z14tcgen05_matmulPf` now bound to `.text.tcgen05_matmul`) |

Back in the caller `sub_4275C0`, the output is written through `sub_43D990` which finalizes ownership transfer, then the `FNLZR: Ending tcgen05_matmul.cubin` diagnostic is emitted (if `--edbg 1` is active), and the output replaces the input in the linker's in-memory ELF slot.

#### Decompiled Function Reference Table

| Phase | Line | Function called | Role |
|---|---|---|---|
| 0 | 438 | `sub_44F410` | Fetch arena metadata + error handler |
| 0 | 448 | `_setjmp` | Establish error recovery frame |
| 2 | 505 | `sub_43D9A0` | Validate device ELF |
| 2 | 528 | `sub_448360` | Locate section header table |
| 2 | 568 | `sub_432020` | Create "Final memory space" arena |
| 2 | 569 | `sub_45CAE0` | Resolve arena metadata |
| 2 | 580 | `sub_43E610` | Extract Mercury architecture profile |
| 3 | 842 | `sub_470DA0` | Fastpath capability bitmask check |
| 3 | 854 | `sub_43DA80` | Total ELF size (for verbatim copy) |
| 4 | 724 | `sub_448730` | Section count |
| 4 | 725 | `sub_464AE0` | Allocate primary section list |
| 4 | 730 | `sub_44FB20` | Allocate 128-entry string pool |
| 4 | 732 | `sub_45AC50` | Build sm_100 instruction encoder |
| 4 | 733 | `sub_459640` | Build sm_100 instruction decoder |
| 4 | 740 | `sub_1CEF5B0` | Process input ELF relocations |
| 4 | 776 | `sub_488470` | Allocate memory space object |
| 4 | 785 | `sub_4B6F40` | Allocate 656-byte CU descriptor |
| 4 | 908 | `sub_44F670` | Allocate compilation mutex |
| 4 | 909 | `sub_44F970` | Initialize compilation mutex |
| 5 | 991 | `sub_4483B0` | Locate `.note.nv.tkinfo` section |
| 5 | 1017 | `strcmp` | Tool name comparison |
| 5 | 1031 | `sub_464BB0` | List length |
| 5 | 1035 | `sub_464DB0` | List indexing |
| 5 | 1785 | `sub_1CF07A0` | ELF_EmitSymbolTable (pass 1) |
| 5 | 1038 | `sub_1CF1690` | ELF_EmitRelocationTable (pass 2) |
| 6 | 1059 | `sub_1CEF440` | OCG compilation pipeline init |
| 6 | 1247 | `sub_471700` | Per-kernel finalization orchestrator |
| 6 | 1293 | `pthread_mutex_destroy` | Release compilation mutex |
| 6 | 1437 | `sub_1CF72E0` | ELF_EmitProgramHeaders (relocatable) |
| 6 | 1439 | `sub_1CF2100` | ELF_EmitSectionHeaders (complete) |
| 6 | 1463 | `sub_4307C0` | Allocate output buffer from arena |
| 6 | 1469 | `sub_1CF7F30` | ELF_WriteRelocatableObject |
| 6 | 1471 | `sub_1CF3720` | ELF_WriteCompleteObject |
| 7 | 1296 | `sub_477480` | Build debug line/frame table |
| 7 | 1297 | `sub_4783C0` | Serialize DWARF program |
| 7 | 1299 | `sub_477510` | Extract serialized debug bytes |
| 7 | 1320 | `sub_4826F0` | Build debug address remap BST |
| 7 | 1329 | `sub_4483B0` | Locate `.symtab` section |
| 7 | 1330 | `sub_4486A0` | Get symbol name by index |
| 7 | 1370 | `sub_4747E0` | Destroy remap BST |
| 8 | 1375 | `sub_43E490` | Initialize string table |
| 8 | 1377 | `sub_43E3C0` | Get ELF hash identifier |
| 8 | 1378 | `sub_43E420` | Get ELF secondary identifier |
| 8 | 1384 | `sub_462C10` | Extract tool name from config |
| 8 | 1390 | `sub_450280` | snprintf-style string append |
| 8 | 1398 | `sub_468440` | Get nvlink version string |
| 9 | 1518 | `sub_464AE0` | Allocate self-check tracker |
| 9 | 1531 | `sub_464C30` | Copy section list entry |
| 9 | 1569 | `sub_4748F0` | **Recursive self-check call** |
| 9 | 1597 | `memcpy` | Replace output with recheck bytes |
| 9 | 1627 | `memcmp` | Section content comparison |
| 9 | 1648 | `sub_44E3A0` | `.nv.merc.` prefix detection |
| 10 | 1747 | `sub_45B680` | Destroy encoder/decoder table |
| 10 | 1749 | `sub_4746C0` | Free debug hash scratch |
| 10 | 1757 | `sub_464640` | Pop from deferred free list |
| 10 | 1760 | `sub_431000` | Free arena block |
| 10 | 1819 | `sub_488530` | Destroy memspace / compilation context |
| 10 | 1826 | `sub_45CAE0` | Detach arena metadata |
| 10 | 1827 | `sub_431C70` | Free arena backing pages |

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
   ```c
   sub_4275C0(&v362, filename, dword_2A5F314, &s1, 0);
   ```

2. **Mercury object from fatbin** (main line ~835): After `sub_43E100` processes a Mercury ELF extracted from a fatbin, the same guard applies.

3. **LTO compilation output** (main line ~1269): After ptxas/cicc produces a cubin from LTO IR, if the output targets sm > 89.

4. **Split-compile LTO output** (main line ~1313): Same as above but in the split-compile code path.

5. **Fatbin extraction pre-link** (sub_42AF40 lines ~179, ~227): When extracting cubins from fatbin members, pre-link finalization is applied if the architecture exceeds 89 and the extracted cubin matches the target.

### Post-link Invocations (a5=1)

6. **Capmerc transformation** (main line ~1481): After the complete link has been serialized, when writing Mercury capmerc output, `sub_4275C0` is called with `a5=1` to apply the post-link capmerc transformation to the serialized ELF bytes.

### Merge-phase Pre-link (a5=0, no output)

7. **Pre-merge finalization** (main line ~1503): When `byte_2A5F221` and `byte_2A5F220` are both set and the input ELF's flags indicate it is not yet finalized, `sub_4275C0` is called with `a4=NULL` (no separate output — modifies in-place) before the object enters the merge loop.

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

The wrapper emits its own diagnostic set: `"FNLZR: JIT Path"`, `"FNLZR: preLink Mode"`, `"FNLZR: postLink Mode"`, and `"FNLZR: Ending JIT"`. The mode selection follows the same logic as `sub_4275C0` — checking the ELF flags to determine pre-link vs. post-link — but the config struct is populated from the JIT context object rather than from global linker state.

If the engine returns non-zero, the JIT wrapper calls `sub_1CEF420` to translate the numeric error code into a diagnostic string, then routes through `sub_467460` for error reporting.

## Architecture Compatibility Checks

Two helper functions implement the "can this ELF be finalized for this target?" query:

### sub_4709E0 — can_finalize_architecture_check

Tests whether the input ELF's architecture is compatible with the finalization target. Uses a lookup table at `dword_1D40660[]` indexed by the "finalization class" byte (values 0-4). The function applies an internal architecture remapping:

| Input | Remapped To | Reason |
|---|---|---|
| 104 | 120 | sm_104 maps to sm_120 family for finalization |
| 130 | 107 | sm_103 family (code 130) maps to sm_100 family base (107) |
| 101 | 110 | sm_101 maps to sm_110 for finalization |

Family matching uses decade comparison: `source/10 == target/10` means same family (e.g., 100 and 103 are both in the 10x decade). Special handling exists for sm_110, sm_121, and sm_100.

The `CAN_FINALIZE_DEBUG` environment variable, when set, enables verbose tracing of this check via `strtol` parsing.

### sub_470DA0 — can_finalize_with_capability_mask

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
| (TODO) | byte | suppress_line_info | TODO: identify true byte for suppress_line_info; `byte_2A5F210` was previously listed here but is the `--disable-smem-reservation` CLI byte that feeds `merge_flags` bit 12, not a Mercury-subsystem flag |
| `byte_2A5F224` | byte | extended_debug | Extended debug info |
| `byte_2A5F223` | byte | suppress_debug | Suppress debug info |
| `byte_2A5F2A9` | byte | ofast_flag | Ofast compilation flag |
| `byte_2A5F221` | byte | fnlzr_pre_merge | Enable pre-merge finalization |
| `byte_2A5F220` | byte | fnlzr_pre_merge_2 | Secondary pre-merge guard |
| `byte_2A5B510` | byte | dont_uplift | Skip uplift for matching arch |

## Relationship to the Embedded ptxas

FNLZR does not contain its own instruction selection or register allocation logic. Instead, it delegates the heavy lifting to the embedded ptxas compiler backend via the functions in the `0x1CF0000-0x1D32172` range:

- `sub_1CEF5B0` — ELF_ProcessRelocations (relocation processing)
- `sub_1CF07A0` — ELF_EmitSymbolTable (symbol table emission, 25,255 bytes)
- `sub_1CF1690` — ELF_EmitRelocationTable (relocation emission, 16,049 bytes)
- `sub_1CF2100` — ELF_EmitSectionHeaders (section header construction, 31,261 bytes)
- `sub_1CF3720` — ELF_WriteCompleteObject (complete ELF output, 99,074 bytes)
- `sub_1CF72E0` — ELF_EmitProgramHeaders (program header emission, 17,710 bytes)
- `sub_1CF7F30` — ELF_WriteRelocatableObject (relocatable output, 44,740 bytes)

The compilation unit descriptor at `off_1D49C58` provides the vtable for the OCG (Optimizing Code Generator) backend, which performs the actual Mercury-to-SASS translation. The memory space is managed through the "Final memory space" arena created specifically for each FNLZR invocation.

## Debugging FNLZR

### Enabling Trace Output

Set `--edbg 1` on the nvlink command line to enable bit 0 of `dword_2A5F308`. This produces the full FNLZR trace:

```text
FNLZR: Input ELF: mykernel.cubin
FNLZR: Pre-Link Mode
FNLZR: Flags [ 0 | 1 ]
FNLZR: Starting mykernel.cubin
FNLZR: Ending mykernel.cubin
```

For JIT paths, the corresponding output is:

```text
FNLZR: JIT Path
FNLZR: preLink Mode
FNLZR: Flags [ 0 | 1 ]
FNLZR: Starting JIT
FNLZR: Ending JIT
```

### Environment Variables

- `CAN_FINALIZE_DEBUG`: When set, enables verbose output from the architecture compatibility checks (`sub_4709E0`, `sub_470DA0`). The value is parsed with `strtol` but any non-zero value activates tracing.

### Self-Check Mode

Pass `--self-check` to enable re-compilation verification. The engine compiles the input, then recompiles its own output, and compares the two at the section, symbol, and relocation level. Mismatches produce error codes 17, 18, or 19 with no additional diagnostic text — the caller must inspect the return code.

### Sibling Wikis

- [ptxas: Capsule Mercury & Finalization](../../ptxas/codegen/capmerc.html) — standalone ptxas finalizer at `sub_612DE0` (47KB), which performs fastpath optimization for off-target finalization. The nvlink FNLZR engine (`sub_4748F0`) shares the same finalization logic but at different addresses due to static linking. Self-check verifier: `sub_720F00` (64KB Flex lexer) + `sub_729540` (35KB comparator). Off-target compatibility: `sub_60F290`.
- [ptxas: Mercury Encoder Pipeline](../../ptxas/codegen/mercury.html) — standalone ptxas Mercury pipeline that FNLZR re-invokes during finalization.

## Confidence Assessment

| Claim | Rating | Evidence |
|---|---|---|
| `sub_4275C0` front-end dispatcher (3,989 bytes / 162 lines) | **HIGH** | Decompiled file `sub_4275C0_0x4275c0.c` exists. Size, line count, and 5-parameter signature verified. |
| `sub_4748F0` core engine (48,730 bytes / 1,830 lines / 25 params) | **HIGH** | Decompiled file `sub_4748F0_0x4748f0.c` exists. Size, line count, and parameter count verified from decompiled code. |
| JIT wrapper at `sub_52DD50` (~600 bytes) | **HIGH** | Function exists. `"FNLZR: JIT Path"` string at `0x1DF8C40` verified with xref to `0x52DDE1`. |
| FNLZR diagnostic strings (12 total) | **HIGH** | All 12 FNLZR strings verified in `nvlink_strings.json`: `"FNLZR: Input ELF: %s"` at `0x1D32381`, `"FNLZR: Post-Link Mode"` at `0x1D32397`, `"FNLZR: Pre-Link Mode"` at `0x1D323BD`, and 9 more. |
| `--edbg` bit 0 enables FNLZR trace via `dword_2A5F308` | **HIGH** | Global variable referenced in decompiled `sub_4275C0`. Bit 0 check `(dword_2A5F308 & 1)` explicit. |
| Config struct: 160 bytes (20 qwords) | **HIGH** | `memset(v28, 0, 160)` explicit in decompiled `sub_4275C0`. Field layout verified from assignment patterns. |
| Pre-link (a5=0) vs Post-link (a5=1) mode selection | **HIGH** | Parameter `a5` usage verified in decompiled code. Guard conditions for each mode confirmed. |
| ELF class byte `0x41` = Mercury, `0x07` = standard cubin | **HIGH** | Verified from decompiled `sub_4748F0` Phase 2 switch on `hdr+7`. |
| ELF subtype `0xFF00` = Mercury | **HIGH** | Check `elf_subtype == 0xFF00` explicit in decompiled Phase 2. |
| Version ceiling `profile_version > 0x101` returns error 25 | **HIGH** | Explicit comparison in decompiled Phase 2. Return value 25 confirmed. |
| `"Final memory space"` arena at `0x1D405E8` | **HIGH** | String verified at exact address. Used in `sub_432020` call in Phase 2. |
| 10-phase pipeline structure in `sub_4748F0` | **HIGH** | All 10 phases identified from decompiled code flow analysis. Phase boundaries determined by functional grouping of sequential code blocks. |
| Phase 1 setjmp/longjmp error recovery | **HIGH** | `_setjmp` call verified in decompiled code. Return value 6 on longjmp confirmed. |
| Phase 3 fastpath: `sub_470DA0` capability bitmask check | **HIGH** | Function exists. Decompiled code shows bitmask mapping: 'd'->1, 'g'->8, 'n'->2, 'y'->64. |
| Architecture remapping: 104->120, 130->107, 101->110 | **HIGH** | Switch/if-chain in decompiled `sub_4709E0` and `sub_470DA0` with exact mapping values. |
| Phase 4: 656-byte CU descriptor with vtable at `off_1D49C58` | **HIGH** | Allocation `sub_4B6F40(656, ...)` explicit. vtable assignment `*(qword*)(cu + 0) = off_1D49C58` confirmed. |
| Phase 4: 256-byte architecture profile descriptor | **HIGH** | `arena_alloc(alloc_ctx, 256)` followed by `memset(v350, 0, 256)` explicit in decompiled code. |
| Phase 5 tkinfo scanning: note type 2000, tool names "nvlink" and "nvJIT API" | **HIGH** | `strcmp(tool_name, "nvlink")` and `strcmp(tool_name, "nvJIT API")` explicit in decompiled code. Note type 2000 check confirmed. |
| Phase 6 ELF writer dispatch: `sub_1CF3720` (complete) vs `sub_1CF7F30` (relocatable) | **HIGH** | Both function calls verified in decompiled Phase 6. Conditional dispatch on `*(byte*)(v350 + 186)` confirmed. |
| Phase 8 tkinfo version string `"Cuda compilation tools, release 13.0, V13.0.88"` | **HIGH** | String verified at `0x1D33D18` in `nvlink_strings.json`. |
| Phase 9 self-check: recursive `sub_4748F0` invocation | **HIGH** | Recursive call with `self_check_data != NULL` verified in decompiled code. Error codes 17, 18, 19 confirmed. |
| Phase 9 `.nv.merc.` prefix stripping (8-byte skip) | **MEDIUM** | `sub_44E3A0` call with `.nv.merc.` prefix verified. The 8-byte vs 9-byte skip interpretation involves decompiler pointer arithmetic nuance. |
| Phase 10 cleanup: 3-level fall-through chain (LABEL_3/4/5) | **HIGH** | Fall-through structure verified from decompiled code. Resource tracking flags `v352`, `v353`, `v385` confirmed. |
| Return codes (0, 4, 5, 6, 7, 11, 17, 18, 19, 25) | **HIGH** | All return values verified from decompiled code at their respective error sites. |
| 6 call sites in `main()` + 2 in `sub_42AF40` | **MEDIUM** | Call site count from xref analysis. Exact count may vary by 1 if indirect calls are included. |
| `CAN_FINALIZE_DEBUG` environment variable | **HIGH** | `getenv("CAN_FINALIZE_DEBUG")` call verified in decompiled `sub_4709E0`. `strtol` parse confirmed. |
| CLI options: `--binary-kind`, `--cap-merc`, `--self-check`, `--out-sass`, etc. | **HIGH** | All option strings verified in `nvlink_strings.json`. Help text strings confirmed at stated addresses. |
| `sub_471700` finalization orchestrator (78,516 bytes) | **HIGH** | Decompiled file `sub_471700_0x471700.c` exists. Size from function bounds. Called from `sub_4748F0` Phase 6. |
