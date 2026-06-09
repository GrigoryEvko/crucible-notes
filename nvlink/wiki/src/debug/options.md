# Debug Options & Levels

nvlink v13.0.88 exposes three user-facing debug options (`-g`, `--generate-line-info`, `--suppress-debug-info`) that control whether DWARF information, line number tables, and SASS-level source maps are included in the output cubin. Internally these options set a small cluster of global flag bytes which then propagate into the embedded ptxas back-end, the cicc LTO compiler (through `-g` and `-generate-line-info` forwarding), and the Mercury/FNLZR finalization pipeline. A single global word (`dword_2A5B528`) tracks the overall compilation mode and interacts with the debug level to determine which debug sections are generated.

This page documents every flag byte, the debug level encoding, the PTX `@@DWARF` directive handler, how flags propagate through LTO, and the interactions between the three user options. It also covers the four embedded ptxas debug options, the two ptxas debug-assist options (`--dont-merge-basicblocks`, `--dont-opt-last-ret`), the tensor memory access check pair, and the internal `--edbg` / `--verbose-tkinfo` diagnostic controls.

## Key Facts

| Property | Value |
|---|---|
| nvlink debug flag | `byte_2A5F310` at `0x2A5F310` (set by `-g` / `--device-debug`) |
| nvlink lineinfo flag | `dword_2A5F248` at `0x2A5F248` (consensus state for `--generate-line-info`) |
| nvlink lineinfo value | `byte_2A5F24C` at `0x2A5F24C` (consensus value for `--generate-line-info`) |
| suppress-debug-info flag | `byte_2A5F226` at `0x2A5F226` (set by `--suppress-debug-info`) |
| extended debug (sm > 72) | `byte_2A5F224` at `0x2A5F224` |
| full debug / SASS mode | `byte_2A5F225` at `0x2A5F225` (set when sm > 89) |
| Mercury mode flag | `byte_2A5F222` at `0x2A5F222` (set when sm > 99) |
| compilation mode word | `dword_2A5B528` at `0x2A5B528` (0/2/4/6) |
| verbose-tkinfo flag | `byte_2A5F223` at `0x2A5F223` (auto-set when `-g` present) |
| edbg level | `dword_2A5F308` at `0x2A5F308` (integer, internal ELF debug output level) |
| ptxas device-debug option | `"device-debug"` registered in `sub_1104950` and `sub_1103030` |
| ptxas lineinfo option | `"lineinfo"` registered in `sub_1103030` (constant `30616012`) |
| ptxas suppress-debug-info | `"suppress-debug-info"` registered in `sub_1103030` |
| ptxas sp-bounds-check | `"sp-bounds-check"` registered in `sub_1104950` / `sub_1103030` |
| ptxas tensor-check | `"g-tensor-memory-access-check"` / `"gno-tensor-memory-access-check"` registered in `sub_1103030` |
| ptxas dont-merge-basicblocks | `"dont-merge-basicblocks"` registered in `sub_1103030` |
| @@DWARF directive handler | `sub_1442040` at `0x1442040` (3,300 bytes / 82 lines) |
| DWARF emitter (codegen) | `sub_1672F50` at `0x1672F50` (22,076 bytes / 600 lines) |
| section name classifier | `sub_1CED7C0` at `0x1CED7C0` (6,757 bytes / 315 lines) |
| Mercury debug dispatcher | `sub_1CED0E0` at `0x1CED0E0` (9,262 bytes / 373 lines) |

## Debug CLI Flag Effect Matrix

This compact reference summarizes every debug-related flag accepted on the nvlink or embedded ptxas command line, the global variable each one lands in, and the direct effect on output generation. Flags are grouped by scope: nvlink-level (processed by `nvlink_parse_options`, `sub_427AE0`), ptxas-level (processed by `sub_1104950`), and internal/developer-only.

### nvlink-level Debug Flags

| Flag (long / short) | Type | Global | Default | Effect on output |
|---|---|---|---|---|
| `--debug` / `-g` | bool | `byte_2A5F310` | 0 | Forward `-g` to cicc (`sub_426CD0` line 179), forward `--device-debug` to ptxas (`sub_429BA0`), set cubin config word `+24` to `5`, auto-enable `verbose-tkinfo`, emit full DWARF + NVIDIA extensions in FNLZR output. Cleared to 0 if `--suppress-debug-info` is also present. |
| `--generate-line-info` (also `-lineinfo`) | consensus | `byte_2A5F24C` / `dword_2A5F248` | 0 | Forward `-generate-line-info` to cicc (`sub_426CD0` line 150-153 via SSE load of `xmmword_1D34730`), forward `--lineinfo` to ptxas. Emits `.debug_line` only (plus `.nv_debug_line_sass` on sm > 89). Participates in per-module consensus during fatbin extraction (`sub_42AF40`); does not take a value directly on the nvlink command line. |
| `--suppress-debug-info` | bool | `byte_2A5F226` | 0 | When combined with `-g`: clears `byte_2A5F310` pre-generation, producing a non-debug build. When alone: emits **fatal** diagnostic `"-suppress-debug-info" / "no -g"` via `sub_467460(dword_2A5B650, ...)` (same severity slot as `--no-opt`/`--optimize-data-layout` and other mutual-exclusion fatals); the binary's own help text claims the option is "ignored" without `--debug`, which is misleading. Does not touch `byte_2A5F24C` at nvlink level. |
| `--verbose-tkinfo` | bool | `byte_2A5F223` | false | Emit full command line and object names in tkinfo section. Auto-enabled to 1 when `-g` is set and the user did not pass `--verbose-tkinfo` explicitly (`sub_427AE0` lines 947-948). Read by FNLZR during tkinfo emission. |

### Architecture-derived Debug Flags (not user-settable)

| Flag | Global | Condition | Effect on output |
|---|---|---|---|
| Extended debug | `byte_2A5F224` | `sm > 72 && machine_width != 32` (`sub_427AE0` line 1048-1054) | Enables richer debug section formats (SASS-level annotations). Cleared back to 0 with diagnostic if machine width is 32. |
| SASS mode | `byte_2A5F225` | `sm > 89` (`sub_427AE0` line 1056-1058) | Switches output from PTX to native SASS. With `-g` active, enables emission of `.nv_debug_line_sass` and `.nv_debug_info_reg_sass`. Forces `dword_2A5B528 = 6`. |
| Mercury mode | `byte_2A5F222` | `sm > 99` (`sub_427AE0` line 1057) | All debug sections get `.nv.merc.` prefix in FNLZR output. Unlocks five Mercury-only sections (`.nv.merc.debug_aranges`, `.nv.merc.debug_ranges`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, `.nv.merc.debug_pubtypes`). |

### ptxas-level Debug Flags (embedded compiler)

| Flag | Registration | Help text summary | Effect |
|---|---|---|---|
| `--device-debug` / `-g` | `sub_1103030` line 414, `sub_1104950` line 215 | "Generate debug information for device code" | Full DWARF generation during PTX->SASS compilation. Auto-enables `--sp-bounds-check` and `--g-tensor-memory-access-check`. Overrides `--maxrregcount` with "'setmaxnreg' ignored to allow debugging" diagnostic. |
| `--generate-line-info` (alias `lineinfo`) | `sub_1103030` line 444 | "Generate debug line table information" | Emits `.debug_line` and (sm > 89) `.nv_debug_line_sass` only; no variable locations or type information. Internal alias `filelineinfo` tracks the line-info-with-filenames subvariant. |
| `--suppress-debug-info` | `sub_1103030` line 428-440 | "Do not generate debug information sections in final output object file." | Strips both `-g` and `-lineinfo` debug output at the ptxas level (broader than the nvlink-level flag). Ignored without `--device-debug` or `--generate-line-info`. |
| `--sp-bounds-check` | `sub_1103030` line 457-458, `sub_1104950` | "Generate stack-pointer bounds-checking code sequence. Turned on automatically when `-g` or `-O0`." | Instruments stack frame accesses with runtime bounds checks. |
| `--g-tensor-memory-access-check` | `sub_1103030` line 1124 | "Enable tensor memory access checks for tcgen05 operations." | Validates tcgen05 tensor memory accesses on Blackwell sm_100+. Enabled by default with `-g`. |
| `--gno-tensor-memory-access-check` | `sub_1103030` line 1138 | (inverse of above) | Disables the check. Always overrides `--g-tensor-memory-access-check` regardless of command-line order. |
| `--dont-merge-basicblocks` | `sub_1103030` line 249 | "Prevents basic block merging for debuggable code." | Preserves basic block boundaries for easier debugging without requiring full DWARF. |
| `--dont-opt-last-ret` | `sub_1103030` line ~260 | "Prevents optimizing the last return instruction." | Allows the debugger to set a breakpoint on the final return of a kernel. |
| `--dwarf-output-file <path>` | `sub_1103030` line 1103950 | "Specify name of file into which the DWARF information ... must be written." | Internal diagnostic: dumps DWARF from PTX `@@DWARF` directives to a file. |
| `--gen-device-shadows` | `sub_1103030` line 1103B1F | "Generate device shadow variables in host address space as externals." | Host-visible shadow copies of device variables for debugger inspection. |

### Internal / developer-only Debug Flags

| Flag | Type | Global | Purpose |
|---|---|---|---|
| `--edbg <N>` | int | `dword_2A5F308` | Internal ELF debugging verbosity. Not related to DWARF output. |
| `--trap-into-debugger` / `-_trap_` | bool | (handler install) | Installs a signal handler (`sub_42FA60`) that traps into a host debugger on assertion failures. Debugging nvlink itself, not device code. |

### Mutual Exclusions and Interactions

- **`--device-debug` + `--generate-line-info`**: The embedded ptxas parser reports the conflict pair as `"device-debug or lineinfo"` (`sub_1104950` line 692). Full debug subsumes line info.
- **`-g` + `--maxrregcount`**: Full debug overrides the register count limit, emitting `"Potential Performance Loss: 'setmaxnreg' ignored to allow debugging."` (register allocator).
- **`-g` + `--suppress-debug-info`** (nvlink): Clears `byte_2A5F310` to 0. Equivalent to not passing `-g`.
- **`--suppress-debug-info` alone**: Fatal diagnostic `"-suppress-debug-info" / "no -g"` — aborts the link. The binary's help text describes the option as "ignored" without `--debug`, but the validation block in `sub_427AE0` calls `sub_467460(dword_2A5B650, ...)` (the same severity slot that produces a fatal error for `--no-opt` / `--optimize-data-layout` and `--force-partial-lto` / `--force-whole-lto`).
- **`-g` without `--verbose-tkinfo`**: `byte_2A5F223` is auto-set to 1.
- **`byte_2A5F224` + 32-bit**: Cleared back to 0 with a diagnostic. Extended debug requires 64-bit addressing.

## nvlink CLI Debug Options

Three debug-related options are registered in `nvlink_parse_options` (`sub_427AE0` at `0x427AE0`):

### `-g` / `--device-debug`

Enables full device debug information. Registered with the long name `"debug"` and short name `"g"` (type 1, boolean). Stores a 1-byte boolean into `byte_2A5F310`. This is the master debug switch — when set, nvlink:

1. Forwards `-g` to cicc during LTO compilation (via `sub_426CD0`).
2. Passes `--device-debug` to the embedded ptxas back-end.
3. Sets the ELF config word at offset +24 to value 5 (versus 4 for non-debug builds) during output generation, recorded in the cubin header metadata.
4. Causes DWARF `.debug_info`, `.debug_line`, `.debug_frame`, `.debug_str`, `.debug_abbrev`, `.debug_loc`, and all NVIDIA proprietary debug sections to be included in the output.
5. Auto-sets `byte_2A5F223` (`verbose-tkinfo`) to 1 if the user did not explicitly provide `--verbose-tkinfo`. This causes the tkinfo section to include the full compilation command line.

The help text in the binary reads: `"Specify that this was a debug compile"`.

**Interaction with `--suppress-debug-info`**: The decompiled code at `sub_427AE0` line 1084 shows that when both `-g` and `--suppress-debug-info` are present, nvlink **clears** `byte_2A5F310` to 0. This is a pre-generation suppression: the debug flag is removed before any compilation occurs, so no debug sections are ever generated. This is more efficient than post-generation stripping because it avoids the compilation cost of debug information entirely.

```c
// sub_427AE0, lines 1084-1089
if ( byte_2A5F226 )          // --suppress-debug-info set?
{
  if ( byte_2A5F310 )        //   -g also set?
    byte_2A5F310 = 0;        //     clear the debug flag (pre-generation suppression)
  else
    sub_467460(..., "-suppress-debug-info", "no -g", ...);  // warn: useless without -g
}
```

### `--generate-line-info` / `-lineinfo`

Enables line number information only, without full debug data. Unlike `-g`, this option does not carry source variable locations or type information — it produces only `.debug_line` sections mapping SASS instructions to source lines.

The value is stored in `byte_2A5F24C` (the consensus value byte) with `dword_2A5F248` tracking the consensus state. This option participates in the per-module consensus mechanism during fatbin extraction (documented in [Option Forwarding](../lto/option-forwarding.md)): when multiple translation units disagree on whether `--generate-line-info` was active, the consensus state machine resolves the conflict.

Note that `--generate-line-info` is NOT registered as a regular CLI option in `sub_427AE0`. Instead, the consensus value in `byte_2A5F24C` is set during fatbin extraction (`sub_42AF40`) by scanning each input module's embedded option string for `"-generate-line-info"` via `strstr()`. The consensus state machine in `dword_2A5F248` resolves conflicts when modules disagree.

During LTO, the option is forwarded to cicc as `-generate-line-info` (via `sub_426CD0`). The string constant at address `0x1D34730` is loaded as a 16-byte SSE register followed by a 4-byte tail, yielding the 20-character string `-generate-line-info`.

The help text reads: `"Generate debug line table information"`.

The option name `"lineinfo"` is also registered separately in the ptxas option table (`sub_1103030`) with the internal numeric constant `30616012` (`0x1D3230C`). This constant is a string table offset referencing the `"lineinfo"` name used during ptxas command-line construction.

**Mutual exclusion with `-g`**: In the embedded ptxas option parser (`sub_1104950`), when both `--device-debug` and `--generate-line-info` are specified, `--generate-line-info` loses. The ptxas option parser reports `"device-debug or lineinfo"` as the conflicting pair. Full debug subsumes line info.

### `--suppress-debug-info`

Suppresses debug information that would otherwise be emitted. Registered with long name `"suppress-debug-info"` (type 1, boolean). Stores a 1-byte boolean into `byte_2A5F226`.

The help text reads: `"Do not preserve debug symbols in output.\nNote: This option is ignored if used without --debug option."` This is confirmed by decompiled code: when `byte_2A5F226` is set but `byte_2A5F310` is not, nvlink emits a warning via `sub_467460` with the message pair `"-suppress-debug-info"` / `"no -g"`.

When `-g` IS also present, the suppress flag clears `byte_2A5F310` to zero, effectively converting the build to a non-debug build. Debug sections are never generated (pre-generation suppression, not post-generation stripping as might be assumed).

In the FNLZR pipeline, `byte_2A5F223` (at `0x2A5F223`, one byte before `byte_2A5F224`) serves double duty: it is set by `--verbose-tkinfo` (or auto-set by `-g`) and is also read by FNLZR as the tkinfo verbosity flag. There is no separate FNLZR-level suppress flag — the suppression is accomplished by clearing `byte_2A5F310` before compilation begins.

## nvlink Internal Debug Options

Two additional options in `nvlink_parse_options` are debug-related but not intended for end users:

### `--edbg <N>`

Registered with long name `"edbg"` (type 4, integer, default `"0"`). Stores an integer into `dword_2A5F308` at `0x2A5F308`. The help text reads: `"Internal elf debugging output level"`.

This controls the verbosity of nvlink's ELF processing diagnostics. Higher values produce more detailed output about section processing, relocation resolution, and symbol table construction. This is a developer-only diagnostic option, not related to DWARF debug information in the output.

### `--verbose-tkinfo <true|false>`

Registered with long name `"verbose-tkinfo"` (type 1, boolean, default `"false"`, multiplicity 1 with flag 4). Stores a 1-byte boolean into `byte_2A5F223` at `0x2A5F223`. The help text reads: `"While generating tkinfo section, emit object name and command line arguments which contains all arguments having file format"`.

When set, the tkinfo section in the output cubin includes the full compilation command line and source file names. This option has a special interaction with `-g`: if `-g` is set and the user did NOT explicitly provide `--verbose-tkinfo`, nvlink automatically enables it (line 947-948 of `sub_427AE0`):

```c
sub_42E390(v2, "verbose-tkinfo", &byte_2A5F223, 1);
if ( byte_2A5F310 && !(unsigned __int8)sub_42E580(v2, "verbose-tkinfo") )
  byte_2A5F223 = 1;
```

The `sub_42E580` call checks whether the user explicitly provided the option. If `-g` is set but `--verbose-tkinfo` was not explicitly given, the flag is forced to 1. This ensures that debug builds always have full tkinfo for debugger consumption.

### `--trap-into-debugger` / `-_trap_`

Registered with long name `"trap-into-debugger"` and short name `"_trap_"` (type 1, boolean, flag 8). The help text reads: `"Trap into debugger upon assertion failures and application crashes"`.

When set, nvlink calls `sub_42FA60()` to install a signal handler that traps into an attached debugger (e.g., gdb) upon assertion failures or crashes. This is a development/QA option for debugging nvlink itself, not for debugging device code.

## ptxas Debug Options

The embedded ptxas back-end registers its own set of debug options in two functions:

- `sub_1103030` (option definition table builder, `0x1103030`, 29,803 bytes) — registers option names, types, and help text via `sub_42F130`.
- `sub_1104950` (command option parser, `0x1104950`, 37,578 bytes) — extracts option values into the per-module compilation state via `sub_42E390`.

### `--device-debug` / `-g`

Registered as `"device-debug"` in both `sub_1103030` and `sub_1104950`. Extracted into the per-module compilation state structure. When set, ptxas:

- Disables certain optimizations that would interfere with debugging (instruction scheduling reordering, aggressive register coalescing).
- Emits full DWARF debug sections via the codegen DWARF emitter (`sub_1672F50`).
- Forces register allocation to preserve debugger-visible values across instruction boundaries.
- Auto-enables `--sp-bounds-check` for stack pointer bounds checking.
- Auto-enables `--g-tensor-memory-access-check` for tcgen05 tensor memory validation.

The diagnostic `"Potential Performance Loss: 'setmaxnreg' ignored to allow debugging."` (seen in the register allocator at `sub_1107190`) confirms that `-g` overrides `--maxrregcount` when both are specified.

### `--generate-line-info`

Registered as `"lineinfo"` in `sub_1103030`. The help text reads: `"Generate line-number information for device code"`. A lighter alternative to full debug: only line number programs are generated, without variable location or type information. The codegen DWARF emitter checks this flag to decide whether to emit `.debug_line` and `.nv_debug_line_sass` sections.

The option also has an internal alias `"filelineinfo"` at `0x1EE91B5` used in the compilation state to track line-info-with-file-names mode.

### `--suppress-debug-info`

Registered as `"suppress-debug-info"` in `sub_1103030`. The help text reads: `"Do not generate debug information sections in final output object file.\nNote: This option is ignored if used without --device-debug or --generate-line-info option."` Mirrors the nvlink-level `--suppress-debug-info` for the ptxas compilation stage.

### `--sp-bounds-check`

Registered as `"sp-bounds-check"` in both `sub_1103030` and `sub_1104950`. The help text reads: `"Generate stack-pointer bounds-checking code sequence. This option is turned on automatically when device-debug (-g) or opt-level(-O) 0 is specified."` Enables stack pointer bounds checking instrumentation. When active, ptxas inserts runtime checks around stack frame accesses to detect buffer overflows on the device stack.

### `--g-tensor-memory-access-check` / `--gno-tensor-memory-access-check`

Registered as `"g-tensor-memory-access-check"` and `"gno-tensor-memory-access-check"` in `sub_1103030`. These control tensor memory access validation for tcgen05 operations (Blackwell sm_100+ architecture).

The help text for `--g-tensor-memory-access-check` reads: `"Enable tensor memory access checks for tcgen05 operations. This is enabled by default with -g."` The `--gno-*` variant always overrides the `--g-*` variant regardless of command-line order. Usage is displayed as `"--g-tensor-memory-access-check / --gno-tensor-memory-access-check"` in diagnostics.

### `--dont-merge-basicblocks`

Registered as `"dont-merge-basicblocks"` in `sub_1103030` at `0x11035BE`. The help text reads: `"Normally, ptxas attempts to merge consecutive basic blocks as part of its optization process. However, for debuggable code this is very confusing. This option prevents basic block merging, at a slight perfomance cost."` This option is complementary to `-g`: it is used when the developer wants to preserve basic block boundaries for easier debugging without necessarily enabling full DWARF output.

### `--dont-opt-last-ret`

Registered in `sub_1103030` at `0x11035D5`. The help text reads: `"Normally, ptxas optimizes return instructions at the end of the program. However, for debuggable code this causes problems setting breakpoint at the end. This option prevents ptxas from optimizing this last return instruction."` Without this option, the debugger cannot set a breakpoint on the last return of a kernel because ptxas may fold it into the preceding instruction.

### DWARF output file option

Registered in `sub_1103030` at `0x1103950`. The help text reads: `"Specify name of file into which the DWARF information held by the parsed PTX files must be written"`. This is an internal diagnostic option that dumps the DWARF data extracted from PTX `@@DWARF` directives to a file for inspection.

### Device shadow variables option

Registered in `sub_1103030` at `0x1103B1F`. The help text reads: `"Used in debug compilation flow: generate device shadow variables in host address space as externals, as opposed to statics"`. This option is relevant to the CUDA debugging infrastructure where device variables need host-visible shadow copies for the debugger to inspect device memory.

## Debug Level Encoding

The global word `dword_2A5B528` at `0x2A5B528` encodes the overall compilation mode as a 4-value enumeration. It is set in `nvlink_parse_options` based on the combination of archive mode (`byte_2A5F2C1`), SASS mode (`byte_2A5F225`), and LTO mode (`byte_2A5F288`):

```c
// sub_427AE0, lines 1137-1163
if ( byte_2A5F2C1 )           // output-is-archive?
  dword_2A5B528 = 2;          //   passthrough mode
if ( byte_2A5F225 )           // sm > 89 (SASS mode)?
  dword_2A5B528 = 6;          //   SASS output mode (overrides passthru)
...
dword_2A5B528 = 4;            // LTO mode (reached through LTO path)
```

| Value | Mode | Debug Implication |
|---|---|---|
| 0 | Normal linking | Debug sections included if `-g` or `-lineinfo` was passed |
| 2 | Passthrough (output-is-archive) | Debug sections passed through unchanged from inputs |
| 4 | LTO mode | Debug sections generated by cicc/ptxas during recompilation |
| 6 | SASS output mode (sm > 89) | Full SASS-level debug including `.nv_debug_line_sass` |

The effective debug level is determined by the combination of `dword_2A5B528` and the three user flags:

```text
Level 0: No debug
  dword_2A5B528 = any, byte_2A5F310 = 0, byte_2A5F24C = 0

Level 2: Partial debug (line info only)
  dword_2A5B528 = any, byte_2A5F310 = 0, byte_2A5F24C = 1
  Output: .debug_line only (no .debug_info, .debug_abbrev, etc.)

Level 6: Full debug
  dword_2A5B528 = any, byte_2A5F310 = 1
  Output: All DWARF sections + NVIDIA extensions
  If sm > 89 (byte_2A5F225 = 1): also .nv_debug_line_sass
```

### Architecture-Dependent Debug Flags

Three flag bytes are derived from the target architecture during option parsing in `nvlink_parse_options`:

| Flag | Address | Condition | Meaning |
|---|---|---|---|
| `byte_2A5F224` | `0x2A5F224` | sm > 72 (`0x48`) | Extended debug info available — architectures above Volta support richer debug section formats including SASS-level annotations |
| `byte_2A5F225` | `0x2A5F225` | sm > 89 (`0x59`) | SASS output mode — the linker produces native SASS (not PTX), enabling SASS-level debug line tables |
| `byte_2A5F222` | `0x2A5F222` | sm > 99 (`0x63`) | Mercury mode — debug sections get `.nv.merc.` prefix during ELF emission |

The `byte_2A5F224` flag has a special interaction with 32-bit mode: if sm > 72 but `dword_2A5F30C` (machine width) is 32, the flag is cleared and a diagnostic is emitted. This means extended debug info requires 64-bit address mode.

When `byte_2A5F225` is set and `-g` is active, the FNLZR pipeline generates additional `.nv_debug_line_sass` and `.nv_debug_info_reg_sass` sections that map individual SASS instructions back to source lines and track register contents for the debugger. Architectures at sm_72 and below use only PTX-level debug information.

For Mercury mode targets (sm > 99), `byte_2A5F222` is set to 1, and the debug sections are further prefixed with `.nv.merc.` during ELF emission (see [DWARF Processing](dwarf-processing.md)).

## Debug Section Output Matrix

The debug sections included in the output depend on the combination of flags and target architecture. The top-level matrix below summarizes emission of the canonical DWARF sections and the NVIDIA extension envelope for each of the three user-visible debug levels; the per-section matrices that follow (Standard DWARF, NVIDIA Extensions, Mercury) break this out on a section-by-section basis.

### Debug Level Summary Matrix

| Debug Level | `.debug_info` | `.debug_abbrev` | `.debug_line` | `.debug_str` | `.debug_frame` | `.debug_loc` | NVIDIA ext |
|---|---|---|---|---|---|---|---|
| `-g0` (no flag, default) | — | — | — | — | — | — | — |
| `-lineinfo` / `--generate-line-info` | — | — | Yes | — | — | — | `.nv_debug_line_sass` only (sm > 89) |
| `-g` / `--device-debug` (full) | Yes | Yes | Yes | Yes | Yes | Yes | Yes (all `.nv_debug_*`, arch-gated) |
| `-g` + `--suppress-debug-info` | — | — | — | — | — | — | — |
| `-lineinfo` + `--suppress-debug-info` (nvlink level) | — | — | Yes | — | — | — | `.nv_debug_line_sass` only (sm > 89) |
| `-lineinfo` + `--suppress-debug-info` (ptxas level) | — | — | — | — | — | — | — |

Notes on the summary matrix:

1. **`-g0` is not a registered flag.** "No debug" is the default state when neither `-g` nor `--generate-line-info` is specified. The row is labelled `-g0` for familiarity with the nvcc spelling, but nvlink has no `-g0` token — the absence of `byte_2A5F310` and `byte_2A5F24C` encodes this state.
2. **`-g` + `--suppress-debug-info` clears `byte_2A5F310` pre-generation** (see `sub_427AE0` line 1086-1087). No debug sections are ever generated, so the result is indistinguishable from `-g0`. This is cheaper than post-generation stripping because cicc and ptxas are invoked without `-g`.
3. **`-lineinfo` + `--suppress-debug-info` is asymmetric between the two parsers.** The nvlink-level `--suppress-debug-info` only clears `byte_2A5F310` — it does not touch `byte_2A5F24C`, so line tables still survive. The ptxas-level `--suppress-debug-info` (documented as "ignored if used without --device-debug or --generate-line-info option") does suppress line info when passed to the embedded compiler. This is the one observable semantic difference between the two registrations.
4. **NVIDIA extension column is architecture-gated.** For `-lineinfo` the only NVIDIA extension section ever emitted is `.nv_debug_line_sass`, and only on sm > 89. For `-g` the full set (`.nv_debug_line_sass`, `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type`, `.nv_debug_ptx_txt`, `.nv_debug_info_ptx`) is emitted, subject to the individual arch gates in the per-section matrices below.

See [DWARF Processing](dwarf-processing.md) for the layout of each `.debug_*` section, [Line Tables](line-tables.md) for the construction of `.debug_line` and `.nv_debug_line_sass`, and [NVIDIA Debug Extensions](nvidia-extensions.md) for the five `.nv_debug_*` formats.

### Standard DWARF Sections

| Section | `-g` | `-lineinfo` | `-g` + `--suppress-debug-info` | Neither |
|---|---|---|---|---|
| `.debug_line` | Yes | Yes | No | No |
| `.debug_info` | Yes | No | No | No |
| `.debug_abbrev` | Yes | No | No | No |
| `.debug_str` | Yes | No | No | No |
| `.debug_frame` | Yes | No | No | No |
| `.debug_loc` | Yes | No | No | No |

### NVIDIA Extension Sections

| Section | `-g` | `-lineinfo` | `-g` + `--suppress-debug-info` | Neither | Arch Gate |
|---|---|---|---|---|---|
| `.nv_debug_line_sass` | Yes | Yes | No | No | sm > 89 |
| `.nv_debug_info_reg_sass` | Yes | No | No | No | sm > 89 |
| `.nv_debug_ptx_txt` | Yes | No | No | No | — |
| `.nv_debug_info_reg_type` | Yes | No | No | No | — |
| `.nv_debug_info_ptx` | Yes | No | No | No | — |
| `.nv_debug.shared` | — | — | — | — | metadata-only (excluded from linking) |

### Mercury Debug Sections (sm > 99)

For Mercury targets, all standard and NVIDIA debug sections are emitted under the `.nv.merc.` prefix. Additionally, five DWARF sections that are not emitted in standard mode appear exclusively in Mercury output:

| Section | `-g` | `-lineinfo` |
|---|---|---|
| `.nv.merc.debug_line` | Yes | Yes |
| `.nv.merc.debug_info` | Yes | No |
| `.nv.merc.debug_abbrev` | Yes | No |
| `.nv.merc.debug_str` | Yes | No |
| `.nv.merc.debug_frame` | Yes | No |
| `.nv.merc.debug_loc` | Yes | No |
| `.nv.merc.nv_debug_line_sass` | Yes | Yes |
| `.nv.merc.nv_debug_info_reg_sass` | Yes | No |
| `.nv.merc.nv_debug_ptx_txt` | Yes | No |
| `.nv.merc.nv_debug_info_reg_type` | Yes | No |
| `.nv.merc.debug_aranges` | Yes | No |
| `.nv.merc.debug_ranges` | Yes | No |
| `.nv.merc.debug_macinfo` | Yes | No |
| `.nv.merc.debug_pubnames` | Yes | No |
| `.nv.merc.debug_pubtypes` | Yes | No |

The `.nv.merc.debug_aranges`, `.nv.merc.debug_ranges`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, and `.nv.merc.debug_pubtypes` sections have no non-Mercury equivalents in the string table. They exist only in the Mercury intermediate format and are consumed by FNLZR during finalization. The FNLZR prefix-stripping logic (`sub_4748F0`) advances the section name pointer by 8 bytes, converting `.nv.merc.debug_info` to `.debug_info` while retaining the leading dot.

### Complete Section Inventory by Debug Level

| Debug Level | Standard DWARF | NVIDIA Extensions | Mercury-only | Total |
|---|---|---|---|---|
| No debug | 0 | 0 | 0 | 0 |
| `-lineinfo` only (sm <= 89) | 1 (`.debug_line`) | 0 | 0 | 1 |
| `-lineinfo` only (sm > 89) | 1 (`.debug_line`) | 1 (`.nv_debug_line_sass`) | 0 | 2 |
| `-g` (sm <= 72) | 6 | 2 (`ptx_txt`, `reg_type`) | 0 | 8 |
| `-g` (sm 73--89) | 6 | 2 (`ptx_txt`, `reg_type`) | 0 | 8 |
| `-g` (sm 90--99) | 6 | 4 (`line_sass`, `reg_sass`, `ptx_txt`, `reg_type`) | 0 | 10 |
| `-g` (sm > 99, Mercury) | 6 | 4 | 15 | 25 |
| `-g` + `--suppress-debug-info` | 0 | 0 | 0 | 0 |

## PTX @@DWARF Directive

The `@@DWARF` directive is a PTX-internal mechanism for embedding raw DWARF data within PTX source text. When ptxas encounters `@@DWARF` during parsing, it invokes the section directive validator at `sub_1442040` (`0x1442040`, 3,300 bytes, 82 lines).

### Processing

The handler (`sub_1442040`) recognizes two directive types:

1. **`.section` directive**: Declares a named DWARF section. The handler validates that the section name starts with `.debug_` or `.nv_debug_` and creates the corresponding output section in the ELF writer.

2. **`@@DWARF` directive**: Indicates that the subsequent data bytes should be appended to the current DWARF section. The PTX source encodes DWARF data as hex byte sequences following the `@@DWARF` marker.

During codegen, the DWARF emitter `sub_1672F50` at `0x1672F50` (22,076 bytes) generates the actual `@@DWARF` content. This function takes 11 parameters including the source file name, line numbers, and a debug level indicator. It emits:

- `.file` directives for source file registration.
- `.local` directives for local symbol naming.
- Section data for `.debug_*` and `.nv_debug_*` sections.
- Format strings like `"%s.%lu"` for generating unique local symbol names that incorporate the compilation unit identifier.

The DWARF emitter uses two prefix strings for section name detection: `".nv_debug_"` at `0x226B814` and `".debug_"` at `0x226B81F`.

The `@@DWARF` mechanism is used only during JIT compilation of PTX inputs (the `sub_4BD760` path in `main()`). For pre-compiled cubins and LTO compilation, debug information is carried in standard ELF sections.

## Debug Flag Propagation Through LTO

During link-time optimization, debug flags must propagate from nvlink's CLI through to the cicc compiler and embedded ptxas back-end. The complete flow is:

```text
nvlink CLI
  |
  +--> nvlink_parse_options (sub_427AE0)
  |      byte_2A5F310 = -g flag
  |      byte_2A5F24C = --generate-line-info value (consensus)
  |      byte_2A5F226 = --suppress-debug-info flag
  |      byte_2A5F223 = verbose-tkinfo (auto-set if -g)
  |
  +--> suppress-debug-info interaction (sub_427AE0 line 1084)
  |      if byte_2A5F226 && byte_2A5F310: clear byte_2A5F310
  |      if byte_2A5F226 && !byte_2A5F310: warn "no -g"
  |
  +--> cicc option list builder (sub_426CD0)
  |      if byte_2A5F310: append "-g"
  |      if byte_2A5F24C: append "-generate-line-info"
  |      if byte_2A5F244: append "-inline-info"
  |      Dedup: -Xnvvm options already containing these are skipped
  |
  +--> nvvmCompileProgram (via sub_4BC6F0)
  |      cicc receives -g / -generate-line-info
  |      cicc generates PTX with @@DWARF sections if debug active
  |
  +--> ptxas option string builder (sub_429BA0)
  |      --device-debug forwarded if -g was set
  |      --lineinfo forwarded if --generate-line-info was set
  |
  +--> ptxas compilation (sub_1112F30)
  |      Reads device-debug, lineinfo, suppress-debug-info
  |      Emits DWARF sections based on flag combination
  |
  +--> FNLZR finalization (sub_4748F0)
         byte_2A5F310 -> config word at offset +24 (4 or 5)
         byte_2A5F224 -> extended debug flag
         byte_2A5F223 -> verbose-tkinfo flag
         Carries or strips debug sections based on flags
```

### cicc Option Deduplication

The cicc option builder (`sub_426CD0`) performs deduplication when `-Xnvvm` options are present. If the user has passed `-Xnvvm "-generate-line-info"`, the builder checks whether the option is already in the `-Xnvvm` list before appending its own copy. The deduplication logic at lines 227-233 of the decompiled source tests:

```c
if ( strcmp("-link-lto", v22)
  && (!byte_2A5F24C || strcmp("-generate-line-info", v22))
  && (!byte_2A5F310 || *v23 != '-' || v23[1] != 'g' || v23[2]) )
```

This prevents duplicate `-generate-line-info` and `-g` flags from reaching cicc when the user has manually passed them through `-Xnvvm`.

### Consensus Mechanism for --generate-line-info

The `--generate-line-info` option is special because it participates in the per-module option consensus system. When nvlink processes multiple input fatbins, each translation unit may or may not have been compiled with `-lineinfo`. The consensus state machine tracks this:

| State | Value | Meaning |
|---|---|---|
| `NOT_SEEN` | 0 | No module has specified this option yet |
| `ABSENT` | 1 | At least one module lacked the option |
| `PRESENT` | 2 | At least one module specified the option |
| `CONFLICT` | 3 | Modules disagree — both present and absent seen |

The decompiled consensus logic in `sub_42AF40` (fatbin extraction, lines 469-489):

```c
if ( strstr(v54, "-generate-line-info") )
{
  byte_2A5F24C = 1;                      // set the value flag
  if ( !dword_2A5F248 )                  // NOT_SEEN?
    dword_2A5F248 = 2;                   //   -> PRESENT
  else if ( dword_2A5F248 == 1 )         // ABSENT (previously saw module without)?
    dword_2A5F248 = 3;                   //   -> CONFLICT
}
else                                     // this module does NOT have -generate-line-info
{
  if ( !dword_2A5F248 )                  // NOT_SEEN?
    dword_2A5F248 = 1;                   //   -> ABSENT
  else if ( dword_2A5F248 == 2 )         // PRESENT (previously saw module with)?
    dword_2A5F248 = 3;                   //   -> CONFLICT
}
```

The consensus state is stored in `dword_2A5F248` and the resolved value in `byte_2A5F24C`. During fatbin extraction (`sub_42AF40`), each input module's `-lineinfo` state is compared against the running consensus. Once all inputs are processed, the final consensus value determines whether `-generate-line-info` is forwarded to cicc.

Note that once `byte_2A5F24C` is set to 1, it is never cleared back to 0. This means that if ANY input module was compiled with `-generate-line-info`, the value flag remains set regardless of conflicts. The conflict state (3) is tracked but does not override the value. In practice, the consensus resolves to "present" if any module had line info.

For `-g` (`byte_2A5F310`), there is no consensus mechanism. If nvlink's CLI includes `-g`, all LTO compilations and all ptxas invocations receive the debug flag, regardless of what the original translation units used. This is because full debug is a linker-level decision that overrides per-TU settings.

### suppress-debug-info Interaction (Corrected)

The `--suppress-debug-info` flag (`byte_2A5F226`) is checked early in `nvlink_parse_options`, immediately after all option values are extracted. Its effect is to clear the `-g` flag before any compilation occurs:

1. If `-g` is set and `--suppress-debug-info` is also set: `byte_2A5F310` is cleared to 0. No debug information is generated at all. This is a pre-generation suppression.
2. If `-g` is NOT set and `--suppress-debug-info` is set: a warning is emitted (`"-suppress-debug-info"` / `"no -g"`) and the flag has no effect.
3. If only `--generate-line-info` is set (without `-g`): `--suppress-debug-info` does NOT suppress line tables. The suppress flag only interacts with `-g`.

This behavior is confirmed by the decompiled code and is consistent with the help text which states: `"Note: This option is ignored if used without --debug option."` The ptxas-level `--suppress-debug-info` has broader scope: its help text states it is ignored `"without --device-debug or --generate-line-info option"`, meaning the ptxas version CAN suppress line tables.

## ptxas Debug Performance Statistics

The embedded ptxas emits two debug-specific timing/memory statistics when profiling is active:

| Metric | Format String | Address |
|---|---|---|
| Debug info generation time | `"DebugInfo-time        : %.3f ms (%.2f%%)\n"` | `0x1EED040` |
| Peak debug info memory | `"PeakDebugInfoMemoryUsage        : %.3lf KB\n"` | `0x1EED160` |

These are reported as part of the `--verbose` compilation statistics output. The time metric shows both absolute milliseconds and percentage of total compilation time. The memory metric reports peak heap allocation in kilobytes attributed to debug info data structures.

## ptxas Internal State Tracking

The embedded ptxas uses two internal state identifiers for debug tracking:

| Identifier | String | Address | Purpose |
|---|---|---|---|
| `CAN_FINALIZE_DEBUG` | `"CAN_FINALIZE_DEBUG"` | `0x1D40080` | Gate flag: set when debug sections are ready for finalization |
| `deviceDebug` | `"deviceDebug"` | `0x1D40148` | State label for the device-debug compilation mode |
| `lineInfo` | `"lineInfo"` | `0x1D40158` | State label for the line-info-only compilation mode |

These are used internally by the ptxas compilation driver (`sub_1112F30`) to track which debug mode is active during codegen.

## Section Name Classification

Two functions classify section names as debug sections, used during ELF processing to decide whether sections should be carried, stripped, or prefixed:

### Standard Debug Classifier (`sub_1CED7C0`)

Recognizes 15 unprefixed debug section names in the following check order:

```text
.debug_abbrev, .debug_aranges, .debug_frame, .debug_info,
.debug_loc, .debug_macinfo, .debug_pubnames, .debug_pubtypes,
.debug_ranges, .debug_str, .nv_debug_info_reg_sass,
.nv_debug_info_reg_type, .nv_debug_ptx_txt (prefix match),
.debug_line, .nv_debug_line_sass
```

The first 10 use length-bounded `memcmp`. The `.nv_debug_ptx_txt` entry uses `sub_44E3A0` (prefix match) instead of exact comparison, suggesting possible versioned or per-CU suffix variants. A section type bitmask `0x5D05` pre-filters by `sh_type` before name comparison.

### Mercury Debug Classifier (`sub_1CED0E0`)

Recognizes the same 15 sections with `.nv.merc.` prefix. Additionally checks for the Mercury flag (bit 28 of `sh_flags`, value `0x10000000`). Sections that pass the type guard but lack the Mercury flag are skipped.

## Global Variable Reference

| Address | Name | Size | Source | Description |
|---|---|---|---|---|
| `0x2A5F310` | `byte_2A5F310` | 1 | `-g` / `--device-debug` | Master debug flag (cleared by `--suppress-debug-info`) |
| `0x2A5F314` | `dword_2A5F314` | 4 | `--arch` | SM version number (used for architecture thresholds) |
| `0x2A5F30C` | `dword_2A5F30C` | 4 | `--machine` | Machine width (32 or 64, gates `byte_2A5F224`) |
| `0x2A5F308` | `dword_2A5F308` | 4 | `--edbg` | Internal ELF debug output level |
| `0x2A5F24C` | `byte_2A5F24C` | 1 | `--generate-line-info` consensus | Line info value (0 or 1) |
| `0x2A5F248` | `dword_2A5F248` | 4 | `--generate-line-info` consensus | Consensus state (0--3) |
| `0x2A5F244` | `byte_2A5F244` | 1 | `-inline-info` consensus | Inline info value (0 or 1) |
| `0x2A5F226` | `byte_2A5F226` | 1 | `--suppress-debug-info` | Suppress debug output |
| `0x2A5F225` | `byte_2A5F225` | 1 | Derived (sm > 89) | SASS mode / full debug flag |
| `0x2A5F224` | `byte_2A5F224` | 1 | Derived (sm > 72, 64-bit only) | Extended debug info flag |
| `0x2A5F223` | `byte_2A5F223` | 1 | `--verbose-tkinfo` / auto (`-g`) | Verbose tkinfo flag |
| `0x2A5F222` | `byte_2A5F222` | 1 | Derived (sm > 99) | Mercury mode flag |
| `0x2A5B528` | `dword_2A5B528` | 4 | Derived | Compilation mode (0/2/4/6) |

## Cross-References

### Debug section internals

- [DWARF Processing](dwarf-processing.md) — structure and re-emission of `.debug_info`, `.debug_abbrev`, `.debug_str`, `.debug_frame`, `.debug_loc`; DW_AT_NV_general_flags handling; the DWARF parser state machine. Read this after the Debug Section Output Matrix above to see how each enabled section is actually populated.
- [Line Table Merging](line-tables.md) — `.debug_line` and `.nv_debug_line_sass` construction, NVIDIA extended DWARF line opcodes, sequence deduplication, and the line table diagnostics (`"Duplicate debug line sequence for a section found"`, `"Debug line table not present for current sequence"`, `"Dwarf debug line error %s"`). This is the landing page for the `-lineinfo` row of the Debug Level Summary Matrix.
- [NVIDIA Debug Extensions](nvidia-extensions.md) — byte-level format of the five `.nv_debug_*` sections: `.nv_debug_line_sass`, `.nv_debug_info_reg_sass`, `.nv_debug_info_reg_type`, `.nv_debug_ptx_txt`, `.nv_debug_info_ptx`. Includes the architecture gates (sm > 89 for the `_sass` variants) referenced in the NVIDIA Extension Sections sub-matrix.
- [Mercury Debug](mercury-debug.md) — the `.nv.merc.debug_*` prefix scheme, the Mercury flag (`0x10000000`) in `sh_flags`, and the five Mercury-only section names (`.nv.merc.debug_aranges`, `.nv.merc.debug_ranges`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, `.nv.merc.debug_pubtypes`).

### Option processing infrastructure

- [CLI Option Parsing](../pipeline/cli-options.md) — the generic `sub_42E390` / `sub_42E580` option registration and extraction framework that backs all three nvlink-level debug flags.
- [Option Forwarding to cicc](../lto/option-forwarding.md) — the `sub_426CD0` cicc option builder, including the SSE load of `xmmword_1D34730` for the 20-character `-generate-line-info` string and the dedup loop against `-Xnvvm` options at lines 227-233.
- [Embedded ptxas Options](../config/ptxas-options.md) — full catalog of 89 ptxas options; see the debug category section for the `sub_1103030` / `sub_1104950` registration pair.

### Pipeline integration

- [FNLZR](../mercury/fnlzr.md) — finalization pipeline: reads `byte_2A5F310` (debug flag), `byte_2A5F224` (extended debug), `byte_2A5F223` (verbose-tkinfo), `byte_2A5F225` (SASS mode) from the global flag block and carries or strips debug sections accordingly. Implements the `.nv.merc.` prefix stripping via the 8-byte pointer advance in `sub_4748F0`.
- [ELF Output](../pipeline/output.md) — cubin header config word at offset `+24`: value `4` for non-debug builds, `5` when `byte_2A5F310 == 1`. This is the authoritative external marker for a debug cubin.
- [Compilation Pipeline Entry](../pipeline/entry.md) — where `sub_427AE0` sits in the overall flow and how `dword_2A5B528` (compilation mode word) is consumed by the downstream stages.

### Sibling wikis

The debug flag semantics documented here originate in the upstream CUDA toolchain. nvlink forwards these flags through LTO and the embedded ptxas back-end:

- [ptxas: Debug Info](../../ptxas/output/debug-info.html) — the ptxas side of `--device-debug`, `--generate-line-info`, and `--suppress-debug-info`. Documents the three-way debug mode enum (none / lineinfo / full), the auto-enable relationship between `-g` and `--sp-bounds-check` / `--g-tensor-memory-access-check`, and the SASS-level DWARF section emission path. The ptxas wiki's Mercury and SASS debug classifiers are the upstream counterparts to nvlink's `sub_1CED0E0` and `sub_1CED7C0`.
- [cicc: Debug Info Pipeline](../../cicc/pipeline/debug-info-pipeline.html) — cicc's four-stage debug metadata lifecycle and the `-g` / `-generate-line-info` handling at the LLVM IR level. nvlink's `sub_426CD0` forwards these flags to cicc's `nvvmCompileProgram` entry, where cicc either preserves full `!DILocation`/`!DISubprogram` metadata (`-g`), strips variable/type info but keeps line info (`-generate-line-info`), or strips everything. nvlink's consensus mechanism in `byte_2A5F24C`/`dword_2A5F248` resolves conflicts across per-module line-info state before this upstream call.

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| `byte_2A5F310` is the `-g`/`--device-debug` flag | HIGH | Decompiled `sub_427AE0` line 944: `sub_42E390(v2, "debug", &byte_2A5F310, 1)` exact match |
| `byte_2A5F223` (`verbose-tkinfo`) auto-enabled by `-g` | HIGH | Decompiled `sub_427AE0` lines 946-948: `sub_42E390(v2, "verbose-tkinfo", &byte_2A5F223, 1); if ( byte_2A5F310 && !sub_42E580(v2, "verbose-tkinfo") ) byte_2A5F223 = 1;` exact match |
| `dword_2A5F308` stores `--edbg` integer value | HIGH | Decompiled `sub_427AE0` line 949: `sub_42E390(v2, "edbg", &dword_2A5F308, 4)` exact match |
| `byte_2A5F226` is the `--suppress-debug-info` flag | HIGH | Decompiled `sub_427AE0` line 989: `sub_42E390(v2, "suppress-debug-info", &byte_2A5F226, 1)` exact match |
| `--suppress-debug-info` clears `-g` pre-generation | HIGH | Decompiled `sub_427AE0` lines 1084-1087: `if ( byte_2A5F226 ) { if ( byte_2A5F310 ) byte_2A5F310 = 0; ... }` exact match |
| `byte_2A5F224` set when sm > 72 (0x48) | HIGH | Decompiled `sub_427AE0` line 1048: `byte_2A5F224 = (unsigned int)dword_2A5F314 > 0x48` exact match |
| `byte_2A5F222` set when Mercury mode (sm > 99) | HIGH | Decompiled `sub_427AE0` line 1057: `byte_2A5F222 = 1` within the Mercury-mode branch |
| `byte_2A5F225` set when sm > 89 (SASS mode) | HIGH | Decompiled `sub_427AE0` line 1058: `byte_2A5F225 = 1` within Mercury branch; line 1064 also sets it when Mercury is active |
| `dword_2A5B528` compilation mode values (2/4/6) | HIGH | Decompiled `sub_427AE0` line 1138: `dword_2A5B528 = 2` (passthrough), line 1140: `dword_2A5B528 = 6` (SASS mode), line 1163: `dword_2A5B528 = 4` (LTO mode) all confirmed |
| cicc option builder forwards `-g`, `-generate-line-info`, `-inline-info` | HIGH | Decompiled `sub_426CD0` line 147: `if ( byte_2A5F24C )`, line 155: `if ( byte_2A5F244 )`, line 157: `strcpy(s, "-inline-info")`, line 177: `if ( byte_2A5F310 )` exact matches |
| cicc option dedup against `-Xnvvm` at lines 226-233 | HIGH | Decompiled `sub_426CD0` lines 226-233: `strcmp("-link-lto", v22) && (!byte_2A5F24C || strcmp("-generate-line-info", v22)) && (!byte_2A5F244 || strcmp("-inline-info", v22)) && ... (!byte_2A5F310 || *v23 != 45 || v23[1] != 103 || v23[2])` — ASCII 45='-' and 103='g' confirm `-g` literal match |
| Fatbin `-generate-line-info` consensus state machine | HIGH | Decompiled `sub_42AF40` lines 469-489: `if ( strstr(v54, "-generate-line-info") ) { byte_2A5F24C = 1; if ( !dword_2A5F248 ) dword_2A5F248 = 2; else if ( dword_2A5F248 != 1 ) ... dword_2A5F248 = 3; }` state transitions match the NOT_SEEN/ABSENT/PRESENT/CONFLICT table |
| Consensus state values 0/1/2/3 | HIGH | Decompiled `sub_42AF40` lines 474, 480, 489 show state assignments `= 2` (PRESENT), `= 3` (CONFLICT), `= 1` (ABSENT) matching wiki documentation |
| @@DWARF directive handler `sub_1442040` at `0x1442040` | HIGH | Decompiled file present at exact address; line 55 references `"@@DWARF directive"` and line 62 references `".section directive"` with version `"2.0"` passed to diagnostic helper `sub_467A70` |
| DWARF emitter `sub_1672F50` at `0x1672F50` | HIGH | Decompiled file present at exact address |
| Prefix strings `.nv_debug_` at `0x226B814` and `.debug_` at `0x226B81F` | HIGH | Addresses cross-verified against nvidia-extensions.md evidence |
| ptxas `"device-debug"` registered in `sub_1103030` | HIGH | Decompiled `sub_1103030` line 414: `"device-debug"` exact match |
| ptxas `"lineinfo"` registered in `sub_1103030` | HIGH | Decompiled `sub_1103030` line 444: `"lineinfo"` exact match |
| ptxas `"suppress-debug-info"` registered in `sub_1103030` | HIGH | Decompiled `sub_1103030` lines 428-429: `"suppress-debug-info"` exact match |
| ptxas `"sp-bounds-check"` registered in `sub_1103030` | HIGH | Decompiled `sub_1103030` lines 457-458: `"sp-bounds-check"` exact match |
| ptxas `"dont-merge-basicblocks"` registered in `sub_1103030` | HIGH | Decompiled `sub_1103030` line 249: `"dont-merge-basicblocks"` exact match |
| ptxas `"g-tensor-memory-access-check"` registered in `sub_1103030` | HIGH | Decompiled `sub_1103030` line 1124: `"g-tensor-memory-access-check"` exact match |
| ptxas constant `30616012` for `lineinfo` at `0x1D3230C` | MEDIUM | Consistent with ptxas option table offset pattern, but not individually re-verified this pass |
| Mutually-excluded pair `"device-debug or lineinfo"` | HIGH | String confirmed in `nvlink_strings.json` at offset 37769 |
| Help text `"Specify that this was a debug compile"` | HIGH | String confirmed in `nvlink_strings.json` at offset 4359 |
| Help text `"Generate debug line table information"` | HIGH | String confirmed in `nvlink_strings.json` at offset 22591 |
| Help text `"Internal elf debugging output level"` | HIGH | String confirmed in `nvlink_strings.json` at offset 4383 |
| Help text `"Do not preserve debug symbols in output..."` | HIGH | String confirmed in `nvlink_strings.json` at offset 4785 |
| Help text `"Trap into debugger upon assertion failures and application crashes"` | HIGH | String confirmed in `nvlink_strings.json` at offset 5061 |
| ptxas help text `"Do not generate debug information sections in final output object file..."` | HIGH | String confirmed in `nvlink_strings.json` at offset 35237 |
| ptxas help text `"Generate stack-pointer bounds-checking code sequence..."` | HIGH | String confirmed in `nvlink_strings.json` at offset 35261 |
| ptxas diagnostic `"Potential Performance Loss: 'setmaxnreg' ignored to allow debugging."` | HIGH | String confirmed in `nvlink_strings.json` at offset 281442 |
| `"CAN_FINALIZE_DEBUG"` state identifier | HIGH | String confirmed in `nvlink_strings.json` at offset 16841 |
| `"deviceDebug"` state identifier | HIGH | String confirmed in `nvlink_strings.json` at offset 16858 |
| `"lineInfo"` state identifier | HIGH | String confirmed in `nvlink_strings.json` at offset 16870 |
| `"DebugInfo-time        : %.3f ms (%.2f%%)\n"` format string | HIGH | String confirmed in `nvlink_strings.json` at offset 38203 |
| `"PeakDebugInfoMemoryUsage        : %.3lf KB\n"` format string | HIGH | String confirmed in `nvlink_strings.json` at offset 38275 |
| `"filelineinfo"` internal alias | HIGH | String confirmed in `nvlink_strings.json` at offset 33373 |
| Debug Section Output Matrix (flag combinations produce specific sections) | MEDIUM | Individual flag-to-section mappings are consistent with decompiled classifier logic in `sub_1CED7C0`/`sub_1CED0E0`, but the full matrix was assembled from multiple function analyses rather than a single table read |
| Mercury-only sections present in the `0x245832A`--`0x2458470` string cluster | HIGH | All 15 Mercury section names (including `.nv.merc.debug_aranges`, `.nv.merc.debug_ranges`, `.nv.merc.debug_macinfo`, `.nv.merc.debug_pubnames`, `.nv.merc.debug_pubtypes`) confirmed in the contiguous string cluster (cross-verified against mercury-debug.md evidence) |
| `--g-tensor-memory-access-check` enabled by default with `-g` | MEDIUM | Help text string explicitly states this behavior; the auto-enable trigger is in ptxas option post-processing (`sub_1104950`) not individually traced this pass |
| `--sp-bounds-check` auto-enabled by `-g` or `-O0` | MEDIUM | Help text string explicitly states this behavior; the trigger logic is in ptxas option post-processing not individually verified here |
| `--verbose-tkinfo` included in FNLZR tkinfo section | MEDIUM | Auto-enable code path from `sub_427AE0` confirmed; the downstream FNLZR consumption is consistent with tkinfo section writer but not re-verified this pass |
