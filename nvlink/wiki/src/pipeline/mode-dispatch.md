# Mode Dispatch

nvlink does **not** have a traditional mode selector in the style of `ld`'s `-r` versus `-dc`. Instead, the "mode" of a given invocation emerges from a combination of independent boolean and enumeration globals set during option parsing, each of which gates a different branch inside the 1,936-line `main()` function at `0x409800`. There are three layers:

1. **Top-level dispatch** -- `dword_2A77DC0` (the `-ghls` mode) chooses between the device-link pipeline and two host-linker-script generation paths. This is the only dispatch that completely skips the device linker.
2. **Device-link sub-mode** -- when top-level dispatch selects device link, several globals (`byte_2A5F1E8`, `byte_2A5F288`, `byte_2A5F286`, `byte_2A5F222`, `byte_2A5F2C1`, `byte_2A5F225`) together decide which pipeline phases actually execute and what the output format is. The compilation-mode enum `dword_2A5B528` (0/2/4/6) summarises some of this.
3. **Side-effect modes** -- `byte_2A5F29A` (emit-ptx), `byte_2A5F216`/`byte_2A5F215` (dump-callgraph), `qword_2A5F2D0` (dot-file), `qword_2A5F2E0` (register-link-binaries) do not themselves skip phases, but they add extra output files or cause LTO to stop early at PTX.

This page enumerates every nvlink operational mode, the CLI flag(s) that enable it, the pipeline phases it runs versus skips, and the output format produced. All line numbers reference `decompiled/main_0x409800.c` and `decompiled/sub_427AE0_0x427ae0.c` unless noted.

## Mode Selector Globals

Every nvlink mode is a combination of these globals. All are set by `nvlink_parse_options` (`sub_427AE0` at `0x427AE0`) before `main()` reaches its first dispatch at line 385.

| Global | Addr | Type | Set by | Role |
|---|---|---|---|---|
| `dword_2A77DC0` | `0x2A77DC0` | `int` (0/1/2) | `-ghls[=lcs-aug\|lcs-abs]` | Top-level mode: 0 = device link, 1 = absolute script, 2 = augmented script |
| `qword_2A5F1D0` | `0x2A5F1D0` | `char *` | `-ghls=<value>` | Raw string value of `--gen-host-linker-script`; non-NULL indicates mode 1/2 |
| `byte_2A5F1E8` | `0x2A5F1E8` | `bool` | `-r`, `--relocatable-link` | Produce a relocatable output (ET_REL) instead of an executable cubin |
| `byte_2A5F1D8` | `0x2A5F1D8` | `bool` | `--shared` | Only consulted when generating the `-ghls` host command; selects `-shared` over `-r` |
| `byte_2A5F288` | `0x2A5F288` | `bool` | `-lto`, `--link-time-opt`, `-dlto` | LTO master flag. Enables NVVM IR input acceptance and the LTO compile phase |
| `byte_2A5F287` | `0x2A5F287` | `bool` | `-dlto` | Alias that forces `byte_2A5F288 = 1` during post-extraction (line 1076 of `sub_427AE0`) |
| `byte_2A5F286` | `0x2A5F286` | `bool` | derived | Partial-LTO active. Set to 1 when `byte_2A5F285` is true or when `-dlto` picks the partial path (line 1209 of `sub_427AE0`) |
| `byte_2A5F285` | `0x2A5F285` | `bool` | `--force-partial-lto` | Force relocatable (partial) LTO output; forced on by `-r` under LTO (line 1153) |
| `byte_2A5F284` | `0x2A5F284` | `bool` | `--force-whole-lto` | Force whole-program LTO; mutually exclusive with `--force-partial-lto` |
| `byte_2A5F29A` | `0x2A5F29A` | `bool` | `--emit-ptx` | Stop LTO after PTX generation; skip PTX-assembly and subsequent link phases |
| `byte_2A5F222` | `0x2A5F222` | `bool` | derived from `--arch` | Mercury mode; set when SM > 99 (`sub_427AE0` line 1057). Triggers Mercury FNLZR post-link transform |
| `byte_2A5F225` | `0x2A5F225` | `bool` | derived from `--arch` | SASS output mode; forced on for SM > 99 and required (`>= 90`) for sm_90+ targets |
| `byte_2A5F224` | `0x2A5F224` | `bool` | derived from `--arch` | New-style ELF flag (SM > 72); changes the ELF class byte from 7 to 8 |
| `byte_2A5F2C1` | `0x2A5F2C1` | `bool` | derived from `sub_44E490(arch)` | Output-is-archive flag. Forces compilation mode `dword_2A5B528 = 2` |
| `dword_2A5B528` | `0x2A5B528` | `int` (0/2/4/6) | post-extraction | Compilation mode enum: 0 = normal, 2 = archive, 4 = LTO, 6 = SASS/Mercury |
| `dword_2A5B514` | `0x2A5B514` | `int` (>=1) | `--split-compile-extended` | LTO extended split-compile thread count; 1 = single-threaded |
| `dword_2A5B518` | `0x2A5B518` | `int` (>=1) | `--split-compile` | LTO NVVM split-compile thread count; 1 = single-threaded |
| `qword_2A5F278` | `0x2A5F278` | `char *` | `--nvvmpath` | Path to `libnvvm.so`. Required when LTO is active |

## The `-ghls` Mode Variable

`dword_2A77DC0` is a 32-bit integer set during option parsing. It takes one of three values:

| Value | Triggered by | Behavior |
|---|---|---|
| 0 | `-ghls` not specified | Full device linking pipeline |
| 1 | `-ghls=lcs-aug` | Write standalone CUDA `SECTIONS` block to file or stdout, then exit |
| 2 | `-ghls=lcs-abs` (the default when `-ghls` is given without a value) | Extract host `ld` default script, append CUDA `SECTIONS`, validate with `ld -T`, and exit |

The `-ghls` option (long form `--gen-host-linker-script`) is registered with `option_register` at `sub_427AE0+738` as a string option:

- Allowed values: `"lcs-aug,lcs-abs"` (`sub_427AE0` line 745)
- Default value: `"lcs-abs"` (`sub_427AE0` line 748)
- Help text: `"Specify the type of host linker script to be generated."`

## How `-ghls` Sets the Mode

The mode variable is computed at the end of `nvlink_parse_options` with a byte-by-byte string comparison. The parsed value of `--gen-host-linker-script` is stored in `qword_2A5F1D0`. If that pointer is NULL (the option was not given), mode remains 0 and the function continues to architecture validation and the full device link path. If the pointer is non-NULL, the code compares it against the literal `"lcs-aug"`:

```c
// Decompiled from sub_427AE0 lines 1012-1030
v7 = (char *)qword_2A5F1D0;     // parsed -ghls value
v8 = qword_2A5F1D0 == 0;
if ( qword_2A5F1D0 )
{
    v9  = "lcs-aug";
    v10 = 8;                    // 7 chars + NUL
    do
    {
        if ( !v10 ) break;
        v8 = *v7++ == *v9++;
        --v10;
    }
    while ( v8 );
    result = (unsigned int)!v8 + 1;
    dword_2A77DC0 = !v8 + 1;
    if ( qword_2A5F330 )        // input files?
        return sub_467460(&unk_2A5B760, ...);   // fatal: incompatible
    return result;
}
```

Tracing the loop for each possible input:

- `"lcs-aug"` vs `"lcs-aug"`: all 8 bytes compare equal; the `while (v8)` re-enters the loop until `v10 == 0` terminates it with `v8 = true`. Then `!v8 + 1 = 0 + 1 = 1`. **Mode 1.**
- `"lcs-abs"` vs `"lcs-aug"`: mismatch at byte 5 (`b` vs `u`); loop exits with `v8 = false`. Then `!v8 + 1 = 1 + 1 = 2`. **Mode 2.**

The numbering is counterintuitive: the simpler `lcs-aug` path emits mode 1 (just write a SECTIONS block), while the default `lcs-abs` path emits mode 2 (run `ld --verbose` and augment its script). Both modes **skip device linking entirely**.

**Validation**: if `-ghls` is specified alongside input files (`qword_2A5F330 != NULL`, line 1028), the linker emits a fatal error referencing descriptor `unk_2A5B760` and the string `"Input files are not allowed with -ghls option; use --help for more information"`.

## Dispatch in `main()`

After option parsing, `main()` at `0x409800` has three branch points that together implement mode selection:

```c
// Line 384
sub_427AE0(a1, a2);             // nvlink_parse_options

// Line 385: first gate -- skip library resolution for modes 1 and 2
if ( (unsigned int)(dword_2A77DC0 - 1) > 1 )
{
    // Mode 0 (or any value >= 3): resolve -L, LIBRARY_PATH, -l
    // ... ~40 lines of library resolution ...
}

// Line 426: second gate -- skip entire device link body if -ghls given
if ( !qword_2A5F1D0 )
{
LABEL_24:
    // FULL DEVICE LINK PIPELINE (lines 426-1741, ~1300 lines)
    // ELF create, input loop, LTO, merge, layout, relocate, finalize, output
    goto LABEL_282;             // normal exit
}

// Line 1742+: reached when qword_2A5F1D0 != 0
// Build host compiler command prefix (" -v --verbose", "-shared"/"-r", "-m64"/"-m32")
// Build collect2 detection pipeline (line 1815-1828)

// Line 1830: final dispatch on mode value
if ( dword_2A77DC0 == 1 )
{
    // MODE 1 (lcs-aug): write absolute SECTIONS block
    ...
}
else
{
    if ( dword_2A77DC0 != 2 )
        sub_467460(&unk_2A5B750, ...);      // fatal: invalid mode
    // MODE 2 (lcs-abs): run ld --verbose pipeline, then append SECTIONS
    ...
}
```

The `(dword_2A77DC0 - 1) > 1` test at line 385 is an unsigned comparison: mode 0 produces `0xFFFFFFFF > 1` (true, enters library resolution); modes 1 and 2 produce `0` or `1` (false, skip library resolution). The second gate at line 426 handles the actual branch to the device-link pipeline body.

## Mode Catalog

### Mode A -- Full Device Link (default)

The default mode when no `-ghls` is specified and `byte_2A5F1E8 == 0`.

| Property | Value |
|---|---|
| CLI flag | *(none, default)* |
| `dword_2A77DC0` | 0 |
| `byte_2A5F1E8` | 0 |
| Implementing function | `main` at `0x409800` (lines 426-1688 = `LABEL_24` through cleanup) |
| Output ELF type | 2 (ET_EXEC), via `(byte_2A5F1E8 == 0) + 1 = 1 + 1 = 2` at line 486 |
| Output format | Executable device cubin (linked, fully resolved) |
| Exit code path | `exit(0)` or `exit(-1)` at lines 1687-1688 |

**Phases executed**: Init -> CLI parse -> Library resolve -> ELF create -> Input loop -> (Merge -> DCE -> Layout -> Relocate -> Finalize -> Write).

**Phases skipped**: None except the LTO compiler detour (only runs if `-lto`).

**Typical use case**: Static device linking of cubin/PTX/fatbin objects into a single executable device image for CUDA runtime registration.

### Mode B -- Relocatable Device Link (`-r`)

Triggered by `-r` or `--relocatable-link`. The output is a relocatable ELF that can be further linked.

| Property | Value |
|---|---|
| CLI flag | `-r`, `--relocatable-link` |
| `dword_2A77DC0` | 0 |
| `byte_2A5F1E8` | 1 |
| `byte_2A5F212` (ignore-host-info) | forced to 1 at `sub_427AE0` line 1116 |
| Implementing function | `main` at `0x409800`, same path as Mode A |
| Output ELF type | 1 (ET_REL), via `(byte_2A5F1E8 == 0) + 1 = 0 + 1 = 1` at line 486 -- **see note below** |
| Output format | Relocatable device object with unresolved symbols and relocation tables |
| Exit code path | `exit(0)` or `exit(-1)` |

**Note on ELF type computation at line 486**: The expression is `(unsigned int)(byte_2A5F1E8 == 0) + 1`. When `byte_2A5F1E8 == 0` (not relocatable), this yields `1 + 1 = 2`. When `byte_2A5F1E8 == 1` (relocatable), it yields `0 + 1 = 1`. The first parameter of `sub_4438F0` is then stored verbatim into `e_type` at `elfw+16` (`*((_WORD *)v17 + 8) = v101` inside `elfw_create`), so the two values map directly to the standard ELF `ET_EXEC=2` / `ET_REL=1` constants. From that point on, `sub_445000` and the output phase only **read** `elfw+16`; they never rewrite it. See [Entry Point & Main](entry.md#phase-1-elf-writer-creation-lines-426-593).

**Phases executed**: Init -> CLI parse -> Library resolve -> ELF create -> Input loop -> Merge -> (DCE) -> Layout -> Relocate -> Finalize -> Write. DCE still runs because `byte_2A5F214` is forced on by `-r`.

**Interactions**:
- When combined with `-lto`, `-r` forces `byte_2A5F285 = 1` (partial LTO) at `sub_427AE0` line 1153. The LTO compiler is told to emit relocatable output rather than whole-program code.
- `-r` also forces `byte_2A5F212 = 1` (ignore host info), overriding `--use-host-info`.
- `--shared` (`byte_2A5F1D8`) is **orthogonal** to `-r` for device linking: in device-link mode the shared flag is only consulted in the `-ghls` command-string construction (line 1785), never in the ELF writer. For device output, only `byte_2A5F1E8` matters.

**Typical use case**: Partial device linking where the output will be combined with other device objects in a later nvlink invocation, or embedded into an archive.

### Mode C -- LTO Whole-Program Link (`-lto`)

Triggered by `-lto` or `--link-time-opt` (or the alias `-dlto`/`--dlto`). Inputs are NVVM IR / LTO IR modules; nvlink calls into `libnvvm.so` to compile them to PTX, then to cubin, then links normally.

| Property | Value |
|---|---|
| CLI flag | `-lto`, `--link-time-opt`, `-dlto`, `--dlto` |
| `dword_2A77DC0` | 0 |
| `byte_2A5F288` | 1 |
| `byte_2A5F286` (partial LTO) | 0 (whole program) |
| `dword_2A5B528` (compilation mode) | 4 (LTO) -- set at `sub_427AE0` line 1163 (`LABEL_110`) |
| Implementing functions | `main` at `0x409800`, LTO section lines 910-1367; helpers: `sub_426CD0` (collect IR), `sub_4BC6F0` (lto_compile), `sub_4BD4E0` (ptxas whole-program), `sub_4BC470` (libdevice load) |
| Output format | Executable device cubin (whole-program-optimised) |

**Phases executed**: Init -> CLI parse -> Library resolve -> ELF create -> libdevice load -> Input loop (IR accepted) -> **LTO compile** -> Merge -> DCE (skipped) -> Layout -> Relocate -> Finalize -> Write.

**DCE is skipped in whole-program LTO**: the check at line 1427 is `byte_2A5F214 && (!byte_2A5F288 || byte_2A5F285)`. With whole LTO (`byte_2A5F288=1, byte_2A5F285=0`), the condition evaluates false and `sub_426AE0` (DCE) is not called. LTO itself has already performed dead code elimination at the IR level.

**Requirements** (enforced by `sub_427AE0` lines 1141-1150):
- `--nvvmpath <path>` is mandatory; otherwise fatal: `"-nvvmpath should be specified with -lto"`.
- NVVM/LTO IR inputs are **only** accepted when LTO is active; in non-LTO mode they trigger an assert (`"should only see nvvm files when -lto"`).

**Typical use case**: Whole-program device optimisation using LTO IR produced by `cicc -dlto`.

### Mode D -- LTO Partial (Relocatable) Link (`-lto -r` or `--force-partial-lto`)

A variant of mode C that produces a relocatable device object for later linking. Triggered by either combining `-r` with `-lto`, or by `--force-partial-lto`.

| Property | Value |
|---|---|
| CLI flags | `-lto -r` OR `-lto --force-partial-lto` |
| `dword_2A77DC0` | 0 |
| `byte_2A5F288` | 1 |
| `byte_2A5F285` | 1 (forced by `-r` at line 1153, or explicit) |
| `byte_2A5F286` | 1 (set at line 1209) |
| `dword_2A5B528` | 4 |
| Implementing function | `main` at `0x409800`, partial LTO branch at lines 1302-1335 |
| Output format | Relocatable device object containing post-LTO cubin |

**Phases executed**: Init -> CLI parse -> Library resolve -> ELF create -> Input loop -> **LTO compile (partial)** -> Merge -> **DCE runs** (because `byte_2A5F285 = 1` makes `(!byte_2A5F288 || byte_2A5F285)` true) -> Layout -> Relocate -> Finalize -> Write.

**Interactions**:
- `--force-whole-lto` and `--force-partial-lto` are mutually exclusive (fatal error at `sub_427AE0` line 1194, descriptor `unk_2A5B650`, strings `"-force-partial-lto"` + `"-force-whole-lto"`).
- `--force-partial-lto` without `-dlto` is fatal (line 1231).

**Typical use case**: Producing an intermediate relocatable LTO object for later combination with more device code.

### Mode E -- LTO Emit-PTX (`-lto --emit-ptx`)

Stops after LTO generates PTX. No ptxas invocation, no device link. Triggered by `--emit-ptx` in combination with `-lto`.

| Property | Value |
|---|---|
| CLI flags | `--emit-ptx -lto` |
| `dword_2A77DC0` | 0 |
| `byte_2A5F288` | 1 |
| `byte_2A5F29A` | 1 |
| `dword_2A5B514` | forced to 1 at line 1224 (single-threaded split-compile) |
| Implementing function | `main` at `0x409800`, LABEL_347 branch at lines 1120-1134 |
| Output format | Human-readable PTX text file |

**Phases executed**: Init -> CLI parse -> Library resolve -> ELF create -> libdevice load -> Input loop -> **LTO compile (PTX emission only)** -> **LABEL_347 writes PTX and exits via LABEL_282**.

**Phases skipped**: PTX assembly, merge, DCE, layout, relocate, finalize, ELF write. Execution jumps from the LTO path to `LABEL_347` which writes the raw PTX buffer via `sub_4264E0(::filename, v217, n)` and then jumps to `LABEL_282` (normal exit).

**Interactions**:
- `--emit-ptx` without `-dlto` is fatal at `sub_427AE0` line 1235: `"-emit-ptx"` vs `"no -dlto"`.
- When combined with multi-threaded split-compile, threads are forced to 1 with a warning at line 1224 (`unk_2A5B540`, strings `"-emit-ptx"`, `"-split-compile-extended"`, `"-split-compile"`).

**Typical use case**: Dumping LTO-optimised PTX for inspection or for a later ptxas invocation with custom flags.

### Mode F -- Mercury Device Link (derived, SM >= 100)

Not a CLI-enabled mode, but a derived mode entered when `--arch` selects a Blackwell-class target (sm_100+). The entire device link pipeline runs, but the output is transformed by the FNLZR (`sub_4275C0`) into the capsule Mercury binary format.

| Property | Value |
|---|---|
| CLI flag | `--arch=sm_100`, `--arch=sm_103`, `--arch=sm_110`, `--arch=sm_120`, `--arch=sm_121` |
| `dword_2A77DC0` | 0 |
| `byte_2A5F222` (Mercury mode) | 1, set at `sub_427AE0` line 1057 when SM > 99 (`0x63`) |
| `byte_2A5F225` (SASS mode) | 1, set at line 1058 |
| `dword_2A5B528` | 6 (SASS), set at line 1140 |
| Implementing function | `main` at `0x409800`, Mercury branch at lines 1452-1483 |
| Output format | Capsule Mercury binary (FNLZR post-link transform applied) |

**Phases executed**: Init -> CLI parse -> Library resolve -> ELF create (Mercury-aware) -> Input loop (with per-cubin FNLZR pre-link transform at lines 727 and 835) -> (optional LTO with per-cubin FNLZR at lines 1269, 1313) -> Merge -> Layout -> Relocate -> Finalize -> **ELF serialise to buffer** -> **FNLZR post-link transform** (line 1481: `sub_4275C0(&v367, ::filename, dword_2A5F314, ptr, 1)`, final arg `1` = post_link) -> Write.

**FNLZR invocations and the pre_link/post_link flag**: The final parameter to `sub_4275C0` toggles FNLZR operating mode. All per-cubin and per-LTO-output calls pass `0` (pre-link / `"FNLZR: Pre-Link Mode"`); the final-output call at line 1481 passes `1` (post-link / `"FNLZR: Post-Link Mode"`). The string table at `0x2205` and `0x2246` contains both diagnostic strings.

**Typical use case**: Producing the final deployable cubin for Blackwell+ architectures where the SASS layout must be transformed by FNLZR for the driver runtime.

### Mode G -- Archive Output (derived, `sub_44E490(arch)`)

When `--arch` selects a target for which `sub_44E490` returns non-zero (host-side archive targets), `byte_2A5F2C1` is set to 1 and `dword_2A5B528` is set to 2. This places nvlink in a "passthrough archive" mode where the output file is an archive aggregating objects rather than a linked cubin. This mode is mutually exclusive with Mercury mode (`byte_2A5F2C1=1` sets mode 2, then `byte_2A5F225=1` overrides to 6 if Mercury).

| Property | Value |
|---|---|
| CLI flag | *(derived from `--arch` via `sub_44E490`)* |
| `byte_2A5F2C1` | 1 |
| `dword_2A5B528` | 2 (archive) |
| Implementing path | `main` with `byte_2A5F2C0` checks throughout |
| Output format | Archive of device objects |

The exact semantics of this derived mode are outside the scope of the mode-dispatch page; see [Architecture Compatibility](../targets/compatibility.md) for the `sub_44E490` archive-arch predicate.

### Mode H -- `-ghls=lcs-aug` Absolute Linker Script

Mode 1 is the simplest path. It writes a fixed CUDA section definition block either to the output file (`-o`) or to stdout:

| Property | Value |
|---|---|
| CLI flag | `-ghls=lcs-aug` or `--gen-host-linker-script=lcs-aug` |
| `dword_2A77DC0` | 1 |
| Implementing function | `main` at `0x409800`, branch at lines 1830-1850 |
| Output format | Plain-text GNU linker script (130 bytes) |

**Phases executed**: Init -> CLI parse -> Host command-string construction (lines 1742-1828) -> write SECTIONS block -> exit.

**Phases skipped**: Library resolve, ELF create, input loop, LTO, merge, DCE, layout, relocate, finalize, ELF write. **None of the device-link pipeline runs.**

The logic at lines 1830-1850:

```c
if ( dword_2A77DC0 == 1 )
{
    if ( ::filename )                         // -o was specified
    {
        v246 = fopen(::filename, "w");        // truncate
        if ( !v246 )
            sub_467460(&unk_2A5B710, ...);    // fatal: open failed
        fwrite(
            "SECTIONS\n"
            "{\n"
            "\t.nvFatBinSegment : { *(.nvFatBinSegment) }\n"
            "\t__nv_relfatbin : { *(__nv_relfatbin) } \n"
            "\t.nv_fatbin : { *(.nv_fatbin) }\n"
            "}\n",
            1u, 0x82u, v246);                 // 130 bytes
        fclose(v246);
        goto LABEL_282;                       // exit(0)
    }
    // else: fall through to write to stdout at line 1925
}
```

If `-o` is specified, the script is written to that file and the linker exits with code 0. If `-o` is not specified, execution falls through to the common stdout path at line 1925, which writes the same SECTIONS block to stdout and exits.

**Typical use case**: NVCC emits this script when the host compiler needs CUDA-aware section directives but no default script is required.

### Mode I -- `-ghls=lcs-abs` Augmented Linker Script

Mode 2 extracts the host linker's built-in default linker script, appends the CUDA section definitions, and validates the result. This is significantly more complex than mode H.

| Property | Value |
|---|---|
| CLI flag | `-ghls=lcs-abs`, `-ghls` (default value), `--gen-host-linker-script[=lcs-abs]` |
| `dword_2A77DC0` | 2 |
| Implementing function | `main` at `0x409800`, branch at lines 1852-1923 |
| Output format | GNU linker script = host `ld --verbose` output + CUDA SECTIONS block, validated by `ld -T` |

**Phases executed**: Init -> CLI parse -> Host command-string construction -> shell pipeline -> `ld --verbose` -> append SECTIONS -> `ld -T` validation -> exit.

**Phases skipped**: Library resolve, ELF create, input loop, LTO, merge, DCE, layout, relocate, finalize, ELF write.

#### Step 1: Build the Host Compiler Command

Before the mode dispatch, modes 1 and 2 share a common command-string construction path (lines 1780-1828). The base compiler is `--host-ccbin` (or `"gcc"` if not specified). The code builds a verbose invocation string:

```c
char *cmd = host_ccbin ? host_ccbin : "gcc";      // line 1744
// Append " -v --verbose" (stored as xmmword_1D34770, a 16-byte SSE constant)
_mm_load_si128(&xmmword_1D34770) -> &cmd[len-1];  // line 1784
// If byte_2A5F1D8 (--shared): append " -shared "
// Else if byte_2A5F1E8 (--relocatable-link / -r): append " -r "
// Append " -m64 " or " -m32 " per dword_2A5F30C
```

Note that in this host-command construction path, `--shared` takes precedence over `-r`. Only one of `-shared` or `-r` is appended.

#### Step 2: Build the collect2 Detection Pipeline

The code constructs a shell pipeline (line 1820) that extracts linker flags from the compiler's verbose output:

```sh
gcc -v --verbose [-shared|-r] [-m64|-m32] \
  2>&1 | grep collect2 \
       | grep -wo -e -pie \
                   -e "-z [^[:space:]]*" \
                   -e "-m [^[:space:]]*" \
                   -e -r \
                   -e -shared \
       | tr "\n" " "
```

This runs the host compiler in verbose mode, finds the `collect2` invocation line (which reveals the actual linker command), and extracts architecture-specific flags (`-pie`, `-z relro`, `-m elf_x86_64`, etc.). The extracted flags are wrapped in `$(...)` (lines 1821-1828) for shell substitution into the `ld --verbose` command.

#### Step 3: Extract the Host Linker Default Script

The extracted flags are prepended to an `ld --verbose` invocation (line 1859: `strcpy(v18, "ld --verbose ");`), piped through filters to isolate the embedded linker script (line 1864):

```sh
ld --verbose $(extracted_flags) \
  | grep -Fvx -e "$(ld -V)" \
  | sed '1,2d;$d' \
  > output_file
```

The pipeline:
1. `ld --verbose $(flags)` -- prints the default linker script for the given configuration between marker lines.
2. `grep -Fvx -e "$(ld -V)"` -- removes the version identification line that `ld -V` produces.
3. `sed '1,2d;$d'` -- strips the first two lines (the `===` banner) and the last line (closing `===`), leaving just the script body.
4. Output goes to the file specified by `-o`, or to `/dev/stdout` if `-o` is not given (line 1877 constructs the `/dev/stdout` path via direct byte writes).

The shell command is executed via `sub_42FA70` (a `system()` wrapper, line 1882). If verbose mode (`byte_2A5F2D8`) is enabled, the command is printed to stderr prefixed with `#$ ` (line 1880).

If the command fails, the linker emits a fatal error via `sub_467460(&unk_2A5B750, ...)` at `LABEL_23` (line 1888) and falls through to a degraded path.

#### Step 4: Append the CUDA Sections

If the `ld --verbose` extraction succeeded and `-o` was specified, the output file is reopened in append mode (line 1894: `fopen(::filename, "a")`) and the same 130-byte SECTIONS block is appended.

#### Step 5: Validate the Augmented Script

After appending, the linker validates the generated script by running `ld -T` on it (lines 1910-1914):

```sh
ld -T <output_file> 2>&1 | grep 'no input files' > /dev/null
```

This invokes `ld` with the script as a linker script (`-T`). Since no input files are provided, a working script will produce the error `"no input files"` -- which is the expected success signal. If `ld` instead produces a syntax error (meaning the script is malformed), the grep fails, `sub_42FA70` returns nonzero at line 1917, and the linker branches to `LABEL_23` (fatal error).

If validation succeeds, the linker exits with code 0. If it fails, a fatal error is emitted.

### Mode J -- Verbose-Keep Command Reconstruction (`--verbose-keep`)

Not a standalone mode but worth noting: when `byte_2A5F29B` (`--verbose-keep` / `-vkeep`) is set, the LTO compile path and the Mercury output path both print reconstructed nvlink invocations to stdout. This does not skip any phases but does dump intermediate files. Controlled by lines 1102-1119 (LTO) and 1463-1479 (Mercury).

## Mode Decision Tree

```
                        nvlink mode selection
                        =====================

 argv/argc  --->  sub_427AE0 (nvlink_parse_options)
                        |
                        v
               qword_2A5F1D0 (the -ghls value)
                    |        |
                NULL |        | non-NULL
                    |        |
                    v        v
    ==========================================
    |                                        |
  [no -ghls]                     [dispatch on dword_2A77DC0 at line 1830]
    |                                      |
    |                                      +-- == 1 ---> Mode H: lcs-aug
    |                                      |             write 130-byte SECTIONS
    |                                      |             to -o file or stdout, exit(0)
    |                                      |
    |                                      +-- == 2 ---> Mode I: lcs-abs
    |                                      |             ld --verbose | sed -> file
    |                                      |             append SECTIONS
    |                                      |             ld -T validation
    |                                      |             exit(0) or fatal
    |                                      |
    |                                      +-- other --> fatal (unreachable)
    |
    v
  [device link path -- line 426 LABEL_24]
    |
    +-- byte_2A5F288 (-lto / --link-time-opt / -dlto)?
    |     |
    |     +-- TRUE:  LTO path taken at lines 910-1367
    |     |           |
    |     |           +-- byte_2A5F29A (--emit-ptx)? YES --> Mode E: emit-ptx
    |     |           |                                       write PTX via LABEL_347
    |     |           |                                       exit(0)
    |     |           |
    |     |           +-- byte_2A5F285 (partial LTO forced by -r or --force-partial-lto)?
    |     |           |     YES --> Mode D: LTO partial (relocatable)
    |     |           |              LTO -> merge -> DCE -> ... -> ET_REL output
    |     |           |
    |     |           +-- byte_2A5F284 (--force-whole-lto) or default:
    |     |                      --> Mode C: LTO whole-program
    |     |                          LTO -> merge -> (skip DCE) -> ... -> ET_EXEC output
    |     |
    |     +-- FALSE: standard path
    |           |
    |           +-- byte_2A5F1E8 (-r / --relocatable-link)?
    |           |     |
    |           |     +-- TRUE: Mode B: relocatable device link
    |           |     |          merge -> DCE -> ... -> ET_REL output
    |           |     |
    |           |     +-- FALSE: Mode A: full device link
    |           |                merge -> DCE -> ... -> ET_EXEC output
    |
    +-- byte_2A5F222 (SM > 99, Mercury) derived mode
          YES --> Mode F: Mercury overlay
                  Runs on top of A/B/C/D -- transforms each merged cubin
                  with FNLZR pre-link, and final serialised ELF with
                  FNLZR post-link (sub_4275C0 last arg = 1).
                  Output file contains capsule mercury binary.
```

## Mode -> Pipeline Phase Matrix

| Phase | Mode A (default) | Mode B (`-r`) | Mode C (LTO whole) | Mode D (LTO partial) | Mode E (emit-ptx) | Mode F (Mercury) | Mode H (lcs-aug) | Mode I (lcs-abs) |
|---|---|---|---|---|---|---|---|---|
| CLI parse | Y | Y | Y | Y | Y | Y | Y | Y |
| Library resolve | Y | Y | Y | Y | Y | Y | N | N |
| ELF writer create | Y | Y | Y | Y | Y | Y | N | N |
| libdevice load | N | N | Y | Y | Y | if LTO | N | N |
| Input loop | Y | Y | Y | Y | Y | Y | N | N |
| LTO compile | N | N | Y | Y | Y | if LTO | N | N |
| PTX assemble | N | N | Y | Y | **N (stops)** | Y | N | N |
| Merge | Y | Y | Y | Y | N | Y | N | N |
| DCE | Y | Y | N | Y | N | Y | N | N |
| Layout | Y | Y | Y | Y | N | Y | N | N |
| Relocate | Y | Y | Y | Y | N | Y | N | N |
| Finalize | Y | Y | Y | Y | N | Y | N | N |
| ELF serialize to buffer | N | N | N | N | N | Y | N | N |
| **FNLZR post-link transform** | N | N | N | N | N | **Y** | N | N |
| Write output | Y | Y | Y | Y | Y (PTX text) | Y | Y (script) | Y (script) |
| `ld -T` validation | N | N | N | N | N | N | N | Y |

Modes G (archive) and J (verbose-keep) are orthogonal overlays and are not rows in this matrix.

## Output Format Per Mode

| Mode | Output file content | ELF class byte | ELF type |
|---|---|---|---|
| A | Executable device cubin | 7 (legacy) or 8 (`byte_2A5F224=1`, SM > 72) | writer tag `2` |
| B | Relocatable device object | 7 or 8 | writer tag `1` |
| C | Executable device cubin (post-LTO) | 7 or 8 | writer tag `2` |
| D | Relocatable device object (post-LTO) | 7 or 8 | writer tag `1` |
| E | PTX text (human-readable) | N/A | N/A |
| F | Capsule mercury binary (after FNLZR post-link) | 8 | Mercury-specific |
| G | Archive file containing device objects | N/A | N/A |
| H | GNU linker script (130 bytes, SECTIONS block only) | N/A | N/A |
| I | GNU linker script (host default + appended SECTIONS) | N/A | N/A |

## The SECTIONS Block (Modes H and I)

All three code paths that write the linker script share the same hardcoded 130-byte (`0x82`) constant string:

```
SECTIONS
{
	.nvFatBinSegment : { *(.nvFatBinSegment) }
	__nv_relfatbin : { *(__nv_relfatbin) } 
	.nv_fatbin : { *(.nv_fatbin) }
}
```

This string literal appears three times in the decompiled output at:
- Line 1837-1843 (Mode H, file output, `fwrite` with `"w"`)
- Line 1897-1903 (Mode I, append after `ld --verbose` extraction, `fwrite` with `"a"`)
- Line 1925-1931 (fallback stdout output, `fwrite` to `stdout`)

The three sections in the linker script are CUDA-specific host ELF sections:

| Section | Description |
|---|---|
| `.nvFatBinSegment` | Contains the embedded fatbin blob (device code for all target architectures) |
| `__nv_relfatbin` | Contains a relocatable reference to the fatbin, used by the CUDA runtime for registration |
| `.nv_fatbin` | Alternative fatbin container section used in some linking configurations |

These sections must appear in the host linker script so that `ld` preserves them during host linking rather than discarding them as unknown sections. The `__nv_relfatbin` entry uses a non-dotted section name, which is unusual for ELF but valid in GNU `ld` linker scripts. The trailing space after `*(__nv_relfatbin) }` on that line is present in the binary.

## Key Addresses

| Address | Symbol | Role |
|---|---|---|
| `0x409800` | `main` | Entry point; contains all mode dispatch logic |
| `0x427AE0` | `sub_427AE0` (`nvlink_parse_options`) | Sets all mode-selecting globals |
| `0x42FA70` | `sub_42FA70` | Shell command executor (`system()` wrapper) |
| `0x4275C0` | `sub_4275C0` (FNLZR entry, `post_link_transform`) | FNLZR pre-link (arg=0) and post-link (arg=1) |
| `0x4BC6F0` | `sub_4BC6F0` (`lto_compile`) | NVVM IR -> PTX via libnvvm |
| `0x4BD4E0` | `sub_4BD4E0` | ptxas whole-program compile (LTO Mode C) |
| `0x4BD760` | `sub_4BD760` | ptxas relocatable compile (LTO Mode D single, direct PTX input) |
| `0x4264B0` | `sub_4264B0` | ptxas split-compile work item dispatcher |
| `0x4BC470` | `sub_4BC470` | libdevice loader (used in LTO modes) |
| `0x2A77DC0` | `dword_2A77DC0` | `-ghls` mode variable (0/1/2) |
| `0x2A5F1D0` | `qword_2A5F1D0` | Parsed `-ghls` string value |
| `0x2A5F1E8` | `byte_2A5F1E8` | `--relocatable-link` flag |
| `0x2A5F1D8` | `byte_2A5F1D8` | `--shared` flag (host-command only) |
| `0x2A5F288` | `byte_2A5F288` | LTO master flag (`-lto`) |
| `0x2A5F286` | `byte_2A5F286` | Partial LTO active |
| `0x2A5F285` | `byte_2A5F285` | `--force-partial-lto` flag |
| `0x2A5F284` | `byte_2A5F284` | `--force-whole-lto` flag |
| `0x2A5F29A` | `byte_2A5F29A` | `--emit-ptx` flag |
| `0x2A5F222` | `byte_2A5F222` | Mercury mode (sm > 99) |
| `0x2A5F225` | `byte_2A5F225` | SASS output mode |
| `0x2A5F224` | `byte_2A5F224` | SM > 72 flag |
| `0x2A5F2C1` | `byte_2A5F2C1` | Archive output flag |
| `0x2A5B528` | `dword_2A5B528` | Compilation mode enum (0/2/4/6) |
| `0x2A5B514` | `dword_2A5B514` | Extended split-compile threads |
| `0x2A5B518` | `dword_2A5B518` | Split-compile threads |
| `0x2A5F278` | `qword_2A5F278` | `--nvvmpath` value |
| `0x1D34770` | `xmmword_1D34770` | 16-byte SSE constant: `" -v --verbose"` |

## See Also

- [Pipeline Overview](overview.md) -- full pipeline diagram showing how mode dispatch gates individual phases
- [Entry Point & Main](entry.md) -- the `main()` function containing all mode dispatch logic, with phase-by-phase walkthrough
- [CLI Option Parsing](cli-options.md) -- `--gen-host-linker-script`, `--relocatable-link`, `--link-time-opt`, `--emit-ptx` option registration and validation
- [Input File Loop](input-loop.md) -- Phase 7, only runs in modes A-F (never in modes H or I)
- [Library Resolution](library-resolution.md) -- skipped for modes H and I
- [Merge](merge.md) -- skipped for mode E (emit-ptx) and modes H/I
- [Output Writing](output.md) -- output phase behavior varies by mode (ELF vs PTX vs linker script vs Mercury capsule)
- [LTO Overview](../lto/overview.md) -- detailed LTO pipeline for modes C, D, E
- [Mercury FNLZR](../mercury/fnlzr.md) -- FNLZR invocation pattern for Mode F
- [Architecture Compatibility](../targets/compatibility.md) -- SM-number thresholds that derive Mercury/SASS/archive modes
- [Linker Scripts](../infra/linker-scripts.md) -- the `ld --verbose` pipeline used by Mode I
- [Environment Variables](../config/env-vars.md) -- `LIBRARY_PATH` consumed during Phase 4 (skipped for modes H/I)

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `dword_2A77DC0` at `0x2A77DC0` as mode variable | **HIGH** | Referenced at lines 385, 1830, 1853 of `main_0x409800.c`; written at line 1027 of `sub_427AE0_0x427ae0.c` |
| Mode values 0/1/2 dispatch at lines 385 and 1830-1935 | **HIGH** | All three branches directly visible in `main_0x409800.c` |
| `-ghls` string comparison (`"lcs-aug"` 8-byte compare) | **HIGH** | Byte-by-byte loop at `sub_427AE0` lines 1016-1025 |
| `!v8 + 1` expression computing `lcs-aug` -> 1, `lcs-abs` -> 2 | **HIGH** | `sub_427AE0` line 1027; loop termination analysis |
| Mode H writes fixed 130-byte (`0x82`) SECTIONS block | **HIGH** | `fwrite(..., 0x82u, v246)` at line 1845 of `main_0x409800.c` |
| Mode I runs `ld --verbose` pipeline with `collect2` detection | **HIGH** | Shell command construction at lines 1818-1820 and 1858-1864 |
| Mode I validates via `ld -T <script> 2>&1 \| grep 'no input files'` | **HIGH** | Lines 1910-1914 of `main_0x409800.c` |
| Mode A/B branch via `byte_2A5F1E8` controlling ELF type tag | **HIGH** | Line 486: `(byte_2A5F1E8 == 0) + 1` as first arg of `sub_4438F0` |
| `-r` forces `byte_2A5F212 = 1` (ignore-host-info) | **HIGH** | `sub_427AE0` line 1115-1116 |
| `-lto` requires `--nvvmpath` else fatal | **HIGH** | `sub_427AE0` lines 1141-1150 |
| LTO whole-program skips DCE | **HIGH** | Line 1427: `byte_2A5F214 && (!byte_2A5F288 || byte_2A5F285)` false when `byte_2A5F288=1, byte_2A5F285=0` |
| `--force-partial-lto`/`--force-whole-lto` mutually exclusive | **HIGH** | `sub_427AE0` lines 1194-1202 |
| Mode D: `-r` with `-lto` forces `byte_2A5F285 = 1` | **HIGH** | `sub_427AE0` line 1151-1153 |
| Mode D: `byte_2A5F286 = 1` at LABEL_71 | **HIGH** | `sub_427AE0` line 1209 |
| Mode E: `--emit-ptx` jumps to LABEL_347 and exits after writing PTX | **HIGH** | `main_0x409800.c` lines 1120-1134, 1122-1127 |
| Mode E: `--emit-ptx` forces `dword_2A5B514 = 1` | **HIGH** | `sub_427AE0` line 1224 |
| Mode E: `--emit-ptx` without `-dlto` is fatal | **HIGH** | `sub_427AE0` line 1235 |
| Mode F (Mercury): `byte_2A5F222 = 1` when SM > 99 (`0x63`) | **HIGH** | `sub_427AE0` lines 1055-1060 |
| Mode F: FNLZR post-link transform at main line 1481 with last arg = 1 | **HIGH** | `sub_4275C0(&v367, ::filename, dword_2A5F314, ptr, 1)` directly visible |
| FNLZR pre-link calls at lines 727, 835, 1269, 1313 pass 0 | **HIGH** | All five call sites enumerated via grep on `main_0x409800.c` |
| Compilation mode enum `dword_2A5B528` values (0/2/4/6) | **HIGH** | Assignments at `sub_427AE0` lines 1137-1140, 1163; main line 1154 |
| Mode G: `byte_2A5F2C1` from `sub_44E490` derived arch predicate | **MEDIUM** | Call at `sub_427AE0` line 1039; exact semantics of `sub_44E490` partial |
| `(dword_2A77DC0 - 1) > 1` unsigned comparison at line 385 | **HIGH** | Exact instruction pattern in `main_0x409800.c` |
| SECTIONS block appears 3 times (lines 1837-1842, 1897-1902, 1925-1930) | **HIGH** | Direct grep verification in `main_0x409800.c` |
| Three CUDA section names (`.nvFatBinSegment`, `__nv_relfatbin`, `.nv_fatbin`) | **HIGH** | All three present in each `fwrite` call |
| `sub_42FA70` as `system()` wrapper | **HIGH** | `decompiled/sub_42FA70_0x42fa70.c` exists; called at lines 1882, 1917 |
| `xmmword_1D34770` as `" -v --verbose"` SSE constant | **MEDIUM** | `_mm_load_si128` at line 1784; string content inferred from surrounding command-assembly pattern |
| `sub_4BD4E0` (whole-program LTO ptxas) vs `sub_4BD760` (relocatable) | **HIGH** | Called at lines 1165 and 1190 respectively in `main_0x409800.c` |
| `-ghls` fatal with input files (`qword_2A5F330 != NULL`) | **HIGH** | `sub_427AE0` line 1028, descriptor `unk_2A5B760` |
