# Mercury Overview

Mercury is NVIDIA's internal codename for a new GPU ISA binary format that replaces the legacy SASS (Shader ASSembler) encoding for modern GPU architectures. The name is ROT13-obfuscated throughout the binary as "Zrephel" -- applying ROT13 to "MERCURY" yields "ZREPHEL", the form seen in all instruction-level string tables. In nvlink v13.0.88, Mercury surfaces across four distinct subsystems: the MercExpand instruction expansion engine, the capsule mercury (capmerc) ELF format, the R_MERCURY relocation family, and the FNLZR (finalizer) that converts between SASS and Mercury representations.

## String Evidence Summary

| Category | Count | Address Range | Examples |
|---|---|---|---|
| `mercury` / `Mercury` | 82 | `0x1D35A17`--`0x245EF38` | `R_MERCURY_ABS64`, `EIATTR_MERCURY_ISA_VERSION`, `mercury,capmerc,sass` |
| `Zrephel` / `ZREPHEL` | 667 | `0x1D42C80`--`0x1D4DF80` | ROT13-encoded SASS builtins: `ZREPHEL_zoneevre_neevir` = `MERCURY_mbarrier_arrive` |
| `R_MERCURY_*` | 67 | `0x1D35A17`--`0x1D35F4C` | 65 unique relocation types plus `R_MERCURY_NONE` and `R_MERCURY_NONE_LAST` sentinels |
| `.nv.merc.*` | 20 | `0x24582E8`--`0x2458D00` | `.nv.merc.debug_info`, `.nv.merc.rela`, `.nv.merc.symtab_shndx` |
| `capmerc` | 7 | `0x1D33FA9`--`0x1D41EF8` | `capmerc.cubin`, `--binary-kind capmerc`, self-check strings |
| `FNLZR` | 17 | `0x1D32381`--`0x2458F10` | `FNLZR: Input ELF: %s`, `FNLZR: Pre-Link Mode`, `FNLZR: JIT Path` |

## Architecture Generation Mapping

Mercury is not a single monolithic format. It has two deployment tiers tied to GPU architecture:

| Architecture | SM Range | Mercury Role | Default `--binary-kind` |
|---|---|---|---|
| Hopper | SM90, SM90a | Mercury format available but not default. SASS remains the standard output. MercExpand runs in the backend pipeline | `sass` |
| Blackwell | SM100, SM100a, SM100f | Mercury is default. Capsule Mercury (capmerc) is the standard ELF output format | `capmerc` |
| Blackwell Ultra / Future | SM103, SM120, SM121 | Mercury-only. No legacy SASS path | `capmerc` |

The `--binary-kind` CLI flag at `0x1D41D94` (xref `0x4ACC47`) selects the output format:

```
--binary-kind <mercury|capmerc|sass>

Specify the type of target ELF binary kind.
Default on sm100+ is capmerc.
```

The three valid values are parsed from the string `"mercury,capmerc,sass"` at `0x1D41D03` (xref `0x4AC55C`). The option description at `0x1D41E78` confirms the sm100+ default: `"Specify the type of target ELF binary kind. Default on sm100+ is capmerc"`.

## ROT13 Obfuscation Scheme

NVIDIA applies ROT13 encoding to Mercury-related instruction mnemonics throughout the binary. This is a trivial Caesar cipher (A-M swap with N-Z) applied character-by-character, preserving underscores and digits. The encoding is consistent across all 667 `ZREPHEL_*` strings.

Key decodings:

| ROT13 (in binary) | Decoded (real name) | Instruction Category |
|---|---|---|
| `ZREPHEL` | `MERCURY` | ISA prefix |
| `ZREPHEL_zoneevre_neevir` | `MERCURY_mbarrier_arrive` | Barrier ops (124 strings) |
| `ZREPHEL_oneevre_neevir_flap` | `MERCURY_barrier_arrive_sync` | Barrier ops (86 strings) |
| `ZREPHEL_jnectebhc_zzn_flap_0` | `MERCURY_warpgroup_mma_sync_0` | Warpgroup MMA (40 strings) |
| `ZREPHEL_ngbz_nqq_f32` | `MERCURY_atom_add_s32` | Atomics (36 strings) |
| `ZREPHEL_erqhk_f32_flap` | `MERCURY_redux_s32_sync` | Reductions (32 strings) |
| `ZREPHEL_srapr_zoneevref` | `MERCURY_fence_mbarriers` | Fences (32 strings) |
| `ZREPHEL_gptra05_yq` | `MERCURY_tcgen05_ld` | SM100 tensor ops (4 strings) |

The 667 ROT13-encoded entries represent the Mercury-ISA-specific builtin instruction templates. These are instruction descriptors used by the ISel pattern matchers and the MercExpand engine. Each string encodes the full instruction signature: opcode, operand types (e.g., `fepf` = `srcs` for source operands, `he4` = `ur4` for uniform register 4-wide), and variant index.

Instruction category distribution across the 667 ZREPHEL builtins:

| Category (decoded) | Count | Description |
|---|---|---|
| `mbarrier` | 124 | Async barrier operations (arrive, wait, test, try_wait, pend) |
| `barrier` | 86 | Classic warp-level barrier synchronization |
| `warpgroup` | 40 | Warpgroup MMA operations (fp16, fp8, int, sparse variants) |
| `atom` | 36 | Atomic memory operations |
| `redux` | 32 | Warp-level reductions (s32, u32) |
| `fence` | 32 | Memory fence operations |
| `max`, `min`, `addmin`, `addmax` | 78 | Integer min/max combinators |
| `elect` | 20 | Warp-level leader election |
| `match` | 16 | Warp match operations |
| `vabsdiff4` | 14 | SIMD 4-byte absolute difference |
| `mov`, `selmov` | 16 | Data movement |
| `vote` | 12 | Warp-level voting |
| `tcgen05` | 4 | SM100 Blackwell tensor core gen05 |
| Others | 57 | `cvt`, `cvta`, `mapa`, `createpolicy`, `shfl`, `ld`, `st`, `cp`, `red`, `fma`, `sad`, `predict`, `multimem`, `griddepcontrol` |

## Mercury Pipeline in the Backend Compiler

Mercury processing occurs in the backend scheduling/encoding pipeline within the embedded ptxas. Four named Mercury passes are identified from the pass table at `0x2443C00`:

| Pass Name | String Address | Xref Address | Stage |
|---|---|---|---|
| `MercEncodeAndDecode` | `0x2443CA2` | `0x24443F0` | Encode IR to Mercury binary, then decode for verification |
| `MercExpandInstructions` | `0x2443CB6` | `0x24443F8` | Expand pseudo-instructions into Mercury machine operations |
| `MercGenerateWARs1` | `0x2443CCD` | `0x2444400` | Generate write-after-read hazard barriers (first pass) |
| `MercGenerateOpex` | `0x2443CDF` | `0x2444408` | Generate operand extensions for wide encodings |
| `MercGenerateWARs2` | `0x2443CF0` | `0x2444410` | Generate WAR barriers (second pass, post-opex) |
| `MercGenerateSassUCode` | `0x2443D02` | `0x2444418` | Generate final SASS microcode from Mercury representation |
| `PostFixForMercTargets` | `0x2443C44` | `0x24443C0` | Target-specific fixups for Mercury architectures |

Additional Mercury-prefixed pass markers confirmed from logging strings:

| Marker String | Address | Xref | Engine |
|---|---|---|---|
| `"After MercExpand"` | `0x1DFE320` | `0x5FF15E` | MercExpand dispatch at `sub_5FDDB0` |
| `"After MercConverter"` | `0x241F913` | `0x19798F8` | MercConverter in scheduling pipeline |
| `"After MercWARs"` | `0x1D41C60` | `0x4A480A` | WAR hazard barrier insertion |
| `"After MercOpex"` | `0x1D41C6F` | `0x4ABC3E` | Operand extension generation |

## The MercExpand Engine

MercExpand is the instruction expansion pass that lowers IR-level pseudo-instructions into Mercury machine operations. It occupies the address range `0x5E4470`--`0x600260` (~112 KB, ~40 functions) in the binary.

### Core Architecture

The engine operates on a per-basic-block basis, iterating the IR instruction linked list. For each node, it dispatches to specialized handlers based on the IR opcode type (field at node offset +28):

**Main dispatch function**: `sub_5FDDB0` (MercExpand_Dispatch, ~25.5 KB)

```
Dispatch logic (switch on opcode type at node+28):
  case 0   -> vtable+48  (generic expansion)
  case 5,8,9 -> register width clamping (max width = 15)
  case 11  -> complex handler with 3 sub-paths:
               sub_5F80E0 (vtable+584 path)
               sub_5FAC90 (shared memory, vtable+1160)
               sub_5FC1B0 (surface ops, data type 559-560)
               vtable+88  (fallback)
  case 12  -> vtable+136
  case 17  -> conditional on debug flag at offset +1536
  case -1  -> terminator (checks predication flags)
  case 120 -> special node (skip processing)

Special case: attribute 200 == 1107 triggers MOV expansion
  (sub_5FC6B0, creates target opcode 346, sets attribute 227=1233)
```

### Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `0x5FDDB0` | `MercExpand_Dispatch` | 25.5 KB | Main entry point, instruction dispatch loop |
| `0x5F38E0` | `MercExpand_HandleInstruction` | 35.0 KB | Per-instruction expansion, 2nd largest function in engine |
| `0x5F0180` | `MercExpand_PropagateRegConstraints` | 14.2 KB | Register constraint propagation (bitvector scanning) |
| `0x5F8B60` | `MercExpand_ApplyResourceConstraints` | 16.0 KB | Register resource accounting (52 register types) |
| `0x5EA930` | `MercExpand_LookupTargetOpInfo` | 12.1 KB | Target instruction descriptor lookup (184 bytes per descriptor) |
| `0x5EB130` | `MercExpand_ApplyRegConstraintsFromTarget` | 11.0 KB | Target-specific register constraints (capabilities 40-47) |
| `0x5EA4F0` | `MercExpand_InvalidateRegisterState` | 4.3 KB | Register cache invalidation (13 register slots, 15+ generation counters) |
| `0x5F60E0` | `IRTree_Walk` | 18.6 KB | Recursive tree walker (manually unrolled to 5 nesting levels) |

### Instruction Handlers

The dispatch loop delegates to specialized handlers per instruction category:

| Function | Handler | Notes |
|---|---|---|
| `0x5EC540` | HandlePredication | Predicate register setup |
| `0x5EC940` | HandleBarrier | Barrier synchronization |
| `0x5ECC60` | HandleSync | Warp synchronization |
| `0x5ED060` | HandleDepInfo | Dependency information |
| `0x5ED3A0` | HandleTexSampler | Texture/sampler operations |
| `0x5ED850` | HandleAtomicOp | Atomic memory operations |
| `0x5EDA80` | HandleMemOp | Memory load/store |
| `0x5EE030` | HandleConversion | Type conversion |
| `0x5EE750` | HandleCmp | Comparison operations |
| `0x5EE930` | HandleSelect | Select/conditional move |
| `0x5EEB20` | HandleBranch | Branch expansion (7.7 KB) |
| `0x5EF0E0` | HandleCall | Function call expansion |
| `0x5EF4D0` | HandleReturn | Return/exit expansion |
| `0x5EF760` | HandlePhi | PHI node expansion |
| `0x5FAC90` | HandleSharedMem | Shared memory access (9.6 KB) |
| `0x5FB5B0` | HandleGlobalMem | Global memory access |
| `0x5FBC30` | HandleConstMem | Constant memory access |
| `0x5FC1B0` | HandleSurfaceOp | Surface read/write |
| `0x5FC6B0` | ExpandMOV | MOV instruction (attribute 200==1107 special case) |
| `0x5FCE20` | ExpandRETURN | Return/exit (creates opcode 270, sets attribute 118=519) |

### Internal Data Structures

**Target instruction descriptor** (184 bytes per entry, base at state offset +832):
- Offset 0: descriptor index
- Offsets 152-167: 6 register class constraint words + 4 constraint flags
- Offsets 2880-2904: register constraint linked lists
- Offsets 3384-3456: scheduling hints
- Offset 3672: capability flag 51

**Register state cache** (at MercExpand state offset +400):
- 13 register slots mapping to physical register file partitions
- Generation counters for cache invalidation
- Slots cover: general purpose (R0-R255), predicates (P0-P6), special registers (CC, etc.)

**FNV-1a hash maps**: Used for IR node tracking and lookup tables throughout MercExpand. Node identification uses hash maps keyed on node metadata at offset +112 -> +20.

## Capsule Mercury (capmerc) Format

Capsule Mercury is the new ELF binary format for SM100+ targets. It wraps Mercury-encoded instructions in a specialized ELF layout with `.nv.merc.*` sections. The `capmerc.cubin` filename extension is used (string at `0x1D33FA9`, xrefs from `0x40A84F` and `0x42A26F`).

### ELF Sections

The 20 `.nv.merc.*` section names identified in the binary:

| Section Name | Purpose |
|---|---|
| `.nv.merc` | Main Mercury instruction section |
| `.nv.merc.rela` | Mercury relocation entries |
| `.nv.merc.symtab_shndx` | Extended section header index (for >65535 sections) |
| `.nv.merc.nv.shared.reserved.` | Reserved shared memory region |
| `.nv.merc.debug_abbrev` | DWARF abbreviation tables |
| `.nv.merc.debug_aranges` | DWARF address ranges |
| `.nv.merc.debug_frame` | DWARF call frame information |
| `.nv.merc.debug_info` | DWARF debug information entries |
| `.nv.merc.debug_line` | DWARF line number program |
| `.nv.merc.debug_loc` | DWARF location lists |
| `.nv.merc.debug_macinfo` | DWARF macro information |
| `.nv.merc.debug_pubnames` | DWARF public names |
| `.nv.merc.debug_pubtypes` | DWARF public types |
| `.nv.merc.debug_ranges` | DWARF address ranges |
| `.nv.merc.debug_str` | DWARF string table |
| `.nv.merc.nv_debug_ptx_txt` | Embedded PTX source text |
| `.nv.merc.nv_debug_line_sass` | NVIDIA SASS-level line tables |
| `.nv.merc.nv_debug_info_reg_sass` | NVIDIA SASS register debug info |
| `.nv.merc.nv_debug_info_reg_type` | NVIDIA register type debug info |

### Self-Check Mechanism

nvlink includes a self-check facility for capsule mercury output, enabled via the `--self-check` CLI flag (string at `0x1D41D3A`). The self-check description at `0x1D41EC8`: `"Self check for capsule mercury (capmerc)"`.

Self-check validates three sections independently:

| Check | Error String | Address |
|---|---|---|
| Text section | `"Self check for capsule mercury text section failed"` | `0x2458F38` |
| Debug section | `"Self check for capsule mercury debug section failed"` | `0x2458F70` |
| Relocation section | `"Self check for capsule mercury relocation section failed"` | `0x2458FA8` |

On failure, the error at `0x1F44288` references internal documentation: `"Failure of '%s' section in self-check for capsule mercury. See the Jira confluence page 'MERCSW-125' for more information that includes some debugging steps."` The `MERCSW` Jira project is NVIDIA's internal Mercury software tracker.

An additional option produces reconstituted SASS for debugging: `"Generate output of capmerc based reconstituted sass only through -self-check"` (string at `0x1D41EF8`).

## Mercury Uplift

The "mercury uplift" path converts legacy SASS ELF binaries into Mercury format. The error string at `0x2458FE8` (`"Invalid elf provided for mercury uplift."`, xref `0x24590B8`) confirms this conversion direction. A related skip path at `0x1D3BCB7` (`"skip mercury section %i"`, xref `0x45F624`) handles sections that should not be uplifted.

The uplift path coexists with the `"don't uplift %s"` diagnostic at `0x1D3410E` (xref `0x42BBDC`), indicating per-symbol or per-section uplift control.

## ELF Attributes for Mercury

Two EIATTR (ELF Info Attribute) types are Mercury-specific:

| Attribute | String Address |
|---|---|
| `EIATTR_MERCURY_ISA_VERSION` | `0x1D36F31` |
| `EIATTR_MERCURY_FINALIZER_OPTIONS` | `0x1D37170` |

Four EICOMPAT (ELF Info Compatibility) attributes relate to Mercury and finalization:

| Attribute | String Address | Purpose |
|---|---|---|
| `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` | `0x245EF08` | Mercury ISA version (major.minor) |
| `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION` | `0x245EF38` | Mercury ISA patch version |
| `EICOMPAT_ATTR_ENABLE_OPPORTUNISTIC_FINALIZATION` | `0x245EED8` | Controls cross-family finalization |
| `EICOMPAT_ATTR_CAN_FASTPATH_FINALIZE` | `0x245EF88` | Fast-path finalization eligibility |

## Relationship to FNLZR (Finalizer)

The FNLZR (Finalizer) subsystem is the runtime component that converts between Mercury and SASS representations. It operates in two modes, logged via diagnostic strings:

| Mode | String | Address | Xref |
|---|---|---|---|
| Pre-Link | `"FNLZR: Pre-Link Mode"` | `0x1D323BD` | `0x427999` |
| Post-Link | `"FNLZR: Post-Link Mode"` | `0x1D32397` | `0x427951` |
| JIT | `"FNLZR: JIT Path"` | `0x1DF8C40` | `0x52DDE1` |

FNLZR logs its input: `"FNLZR: Input ELF: %s"` (`0x1D32381`), and tracks lifecycle: `"FNLZR: Starting %s"` / `"FNLZR: Ending %s"` / `"FNLZR: Flags [ %u | %u ]"`.

Finalization also appears in the capsule mercury code region with thread-level parallelism: `"Failed to create finalizer thread"` (`0x2458EC0`), suggesting that the finalizer runs as a separate thread during link.

The `--opportunistic-finalization-lvl` flag (string at `0x1D41F70`) controls cross-architecture finalization behavior:

```
--opportunistic-finalization-lvl <0|1|2|3>

0 = default behavior
1 = no opportunistic finalization
2 = intra family finalization only
3 = intra and inter family finalization
```

Fast-path finalization is confirmed by the diagnostic at `0x1D40610`: `"[Finalizer] fastpath optimization applied for off-target %u -> %u finalization"`, indicating cross-SM finalization (e.g., compiling SM90 code for an SM100 target).

See [FNLZR (Finalizer)](fnlzr.md) for detailed analysis of the finalization subsystem.

## MercGenerateSassUCode

The final Mercury pipeline stage is `MercGenerateSassUCode` (`0x2443D02`, xref `0x2444418`), which converts the Mercury internal representation into SASS microcode -- the actual GPU-executable instruction encoding. Related dump utilities exist:

| Function | String Address | Purpose |
|---|---|---|
| `DumpNVuCodeText` | `0x2443DA2` | Dump microcode in text format |
| `DumpNVuCodeHex` | `0x2443DB2` | Dump microcode in hex format |

The `.ucode` section name at `0x1EEC922` and `EIATTR_UCODE_SECTION_DATA` at `0x1D36D20` confirm that microcode is a distinct section in the output ELF.

## Cross-References

- [Capsule Mercury Format](capmerc-format.md) -- detailed capmerc ELF layout and encoding
- [R_MERCURY Relocations](r-mercury-relocations.md) -- the 67 Mercury relocation types
- [Mercury ELF Sections](elf-sections.md) -- the 20 `.nv.merc.*` sections
- [Mercury Compiler Passes](compiler-passes.md) -- MercExpand, MercConverter, MercWARs, MercOpex
- [FNLZR (Finalizer)](fnlzr.md) -- SASS-to-Mercury and Mercury-to-SASS conversion
