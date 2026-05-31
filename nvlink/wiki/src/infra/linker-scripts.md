# Linker Script Generation

nvlink can generate GNU `ld` linker scripts that instruct the host linker to preserve CUDA-specific ELF sections during host linking. When `nvcc` compiles a CUDA program, device code is embedded in the host object files inside special sections. Without a linker script that names these sections, the host linker silently discards them. The `-ghls` (`--gen-host-linker-script`) option activates this code path, which bypasses the entire device linking pipeline and instead produces a linker script fragment (or a complete augmented script) and exits.

This feature exists because `nvcc`'s driver needs a linker script at host link time. Rather than shipping a static script, `nvcc` invokes `nvlink -ghls` to generate one dynamically, accounting for the host toolchain's default script and architecture-specific flags.

| | |
|---|---|
| **CLI option** | `-ghls` / `--gen-host-linker-script` with values `lcs-aug` or `lcs-abs` |
| **Mode variable** | `dword_2A77DC0` at `0x2A77DC0` (values 1 or 2) |
| **Parsed value storage** | `qword_2A5F1D0` (pointer to the option string) |
| **Default value** | `lcs-abs` (when `-ghls` is given without an argument) |
| **Implementation** | `main()` at `0x409800`, lines 1743-1936 of the decompiled output |
| **Shell execution** | `sub_42FA70` at `0x42FA70` (a `system()` wrapper) |
| **Template size** | 130 bytes (0x82) |
| **Template rodata address** | `0x1d34450` (shared by all three `fwrite` call sites) |
| **Host compiler dword** | `::src` global (populated from `--host-ccbin`, default `"gcc"`) |
| **Shared flag** | `byte_2A5F1D8` (`--shared` was seen) |
| **Relocatable flag** | `byte_2A5F1E8` (`-r` was seen) |
| **Machine width** | `dword_2A5F30C` (32 or 64, or 0 if unset) |
| **Verbose flag** | `byte_2A5F2D8` (`--verbose` enables `#$ ` trace) |
| **Xlinker list head** | `qword_2A5F2E8` (linked list of pre-composed `ld` flags) |

## The SECTIONS Template

All code paths share a single hardcoded 130-byte string that defines three CUDA-specific host ELF sections:

```text
SECTIONS
{
	.nvFatBinSegment : { *(.nvFatBinSegment) }
	__nv_relfatbin : { *(__nv_relfatbin) } 
	.nv_fatbin : { *(.nv_fatbin) }
}
```

The `fwrite` call uses size 1 and count 0x82 (130), which is the exact byte length of this string excluding the null terminator. The string is referenced from three separate `fwrite` calls in the binary, all pointing to the same data address.

Note the trailing space after `*(__nv_relfatbin) }` on the second section line -- this is present in the binary and is written verbatim.

### The Three Sections

| Section name | ELF convention | Description |
|---|---|---|
| `.nvFatBinSegment` | Standard dotted name | Contains the embedded fatbin blob -- the concatenation of device code compiled for all target GPU architectures. This is the primary container that the CUDA runtime locates at program startup. |
| `__nv_relfatbin` | Non-dotted (double underscore prefix) | Contains a relocatable reference to the fatbin data. The CUDA runtime's registration mechanism (`__cudaRegisterFatBinary`) uses this section to locate the fatbin at load time. The section data begins with the fatbin magic `0xBA55ED50` followed by a size field. |
| `.nv_fatbin` | Standard dotted name | Alternative fatbin container used in certain linking configurations (e.g., relocatable linking with `-r`). Provides a secondary location for fatbin data when the primary `.nvFatBinSegment` is not suitable. |

Without these linker script entries, GNU `ld` treats these as unknown sections and either discards them or merges them incorrectly during host linking. The script ensures they appear as distinct, named output sections in the host executable.

The consumer-side function `sub_476D90` at `0x476D90` validates that a host ELF contains these sections by calling `sub_476EC0` (a section-name predicate) for each of `.nvFatBinSegment`, `__nv_relfatbin`, and `.nv_fatbin`. It then extracts the `__nv_relfatbin` data and verifies the fatbin magic at offset 0.

## Mode 1: Standalone Fragment (`lcs-aug`)

Mode 1 is triggered by `-ghls=lcs-aug` and produces the SECTIONS template as a standalone fragment. The mode variable `dword_2A77DC0` is set to 1.

### Behavior

When `-o` is specified, the script is written to the output file in truncate mode (`"w"`):

```c
// main() line 1830-1848
if (dword_2A77DC0 == 1) {
    if (filename) {
        FILE *f = fopen(filename, "w");
        if (!f)
            fatal_error(&unk_2A5B710, filename, ...);
        fwrite(SECTIONS_TEMPLATE, 1, 0x82, f);
        fclose(f);
        exit(0);
    }
    // fall through to stdout path
}
```

When `-o` is not specified, execution falls through to the common stdout path at line 1925, which writes the same template to `stdout` and exits.

The `lcs-aug` name stands for "linker-script augmentation." The output is a fragment meant to be appended to an existing linker script by the caller (`nvcc`), not used as a complete script on its own.

## Mode 2: Full Augmented Script (`lcs-abs`)

Mode 2 is triggered by `-ghls=lcs-abs` (or `-ghls` with no argument, since `lcs-abs` is the default). The mode variable `dword_2A77DC0` is set to 2. This mode extracts the host linker's built-in default script, appends the CUDA SECTIONS block, and validates the result. It is a five-step pipeline that shells out to `gcc`, `ld`, `grep`, and `sed`.

### Step 1: Build the Host Compiler Verbose Command

Before the mode dispatch, all linker script modes share a command-construction path. The base compiler comes from `--host-ccbin` (stored in `::src`), defaulting to `"gcc"`:

```c
char *cmd = host_ccbin;
if (!host_ccbin)
    cmd = "gcc";
```

The string `" -v --verbose"` is appended via a 16-byte SSE store (`xmmword_1D34770`). Then, depending on the link flags:

- If `--shared` is active (`byte_2A5F1D8`): appends `" -shared "`
- If `-r` is active (`byte_2A5F1E8`): appends `" -r "`
- If `--machine=64` (`dword_2A5F30C == 64`): appends `" -m64 "`
- If `--machine=32` (`dword_2A5F30C == 32`): appends `" -m32 "`

The `--shared` and `-r` flags are mutually exclusive. The `-shared` check takes priority (tested first).

### Step 2: Build the collect2 Detection Pipeline

The code appends a shell pipeline that extracts linker flags from the compiler's verbose output:

```sh
<compiler> -v --verbose [-shared|-r] [-m64|-m32] \
  2>&1 | grep collect2 \
       | grep -wo -e -pie \
                   -e "-z [^[:space:]]*" \
                   -e "-m [^[:space:]]*" \
                   -e -r \
                   -e -shared \
       | tr "\n" " "
```

The decompiled string constant (line 1818-1820):

```c
" 2>&1 | grep collect2  | grep -wo -e -pie -e \"-z [^[:space:]]*\" "
"-e \"-m [^[:space:]]*\" -e -r -e -shared  | tr \"\\n\" \" \" "
```

This pipeline works because:

1. `gcc -v --verbose` prints the complete compiler invocation sequence to stderr, including the internal call to `collect2` (GCC's wrapper around `ld`).
2. `grep collect2` isolates the line containing the actual linker invocation.
3. The second `grep -wo` extracts only the architecture-significant flags: `-pie`, `-z <arg>` (e.g., `-z relro`, `-z now`), `-m <arg>` (e.g., `-m elf_x86_64`), `-r`, and `-shared`.
4. `tr "\n" " "` joins the extracted flags into a single space-separated string.

The entire pipeline is wrapped in `$(...)` for shell command substitution, so the extracted flags become arguments to the subsequent `ld --verbose` call:

```c
// Line 1821-1828: wrap in $(...) for substitution
strcpy(wrapper, "$(");
strcat(wrapper, pipeline);
// Append closing ')' -- written as *(_WORD*) = 41 (ASCII ')')
```

### Step 3: Extract the Host Linker Default Script

The extracted flags are prepended to an `ld --verbose` invocation:

```sh
ld --verbose $(extracted_flags) \
  | grep -Fvx -e "$(ld -V)" \
  | sed '1,2d;$d' \
  > <output_file>
```

The decompiled construction (lines 1858-1878):

```c
// Build: "ld --verbose " + collect2_flags
strcpy(buf, "ld --verbose ");
strcat(buf, collect2_flags);

// Append filter pipeline
strcat(buf, " | grep -Fvx -e \"$(ld -V)\" | sed '1,2d;$d' > ");

// Append output destination
if (filename)
    strcat(buf, filename);
else
    strcat(buf, "/dev/stdout");  // hex: 0x6474732F7665642F + "out"
```

The pipeline steps:

1. **`ld --verbose $(flags)`** -- When invoked with `--verbose`, `ld` prints its built-in default linker script between two `===` banner lines. The `$(flags)` substitution passes the architecture flags extracted in step 2, ensuring `ld` selects the correct default script for the target configuration (e.g., 64-bit, PIE, shared).

2. **`grep -Fvx -e "$(ld -V)"`** -- Removes the version string that `ld -V` outputs. The `-F` flag treats the pattern as a fixed string, `-v` inverts the match (remove matching lines), and `-x` requires the entire line to match. This strips `ld`'s version identification from the output.

3. **`sed '1,2d;$d'`** -- Deletes the first two lines (the opening `===` banner and blank line) and the last line (the closing `===` banner), leaving just the script body.

4. **Output** -- Written to the file specified by `-o`, or to `/dev/stdout` if `-o` is not given. The `/dev/stdout` path is constructed via two hex-encoded memory stores: `0x6474732F7665642F` decodes to `/dev/std` (little-endian) and `byte_74756F` contributes `out`.

The command is executed via `sub_42FA70` (the `system()` wrapper at `0x42FA70`). If `--verbose` is enabled, the command string is printed to stderr as `#$ <command>` before execution.

After execution, the intermediate buffers are freed via `sub_431000` (arena free).

### Step 4: Append the CUDA Sections

If step 3 succeeded (return code 0) and `-o` was specified, the output file is reopened in append mode and the SECTIONS template is appended:

```c
// main() line 1892-1907
if (filename) {
    FILE *f = fopen(filename, "a");  // append mode
    if (!f)
        fatal_error(&unk_2A5B710, filename, ...);
    fwrite(SECTIONS_TEMPLATE, 1, 0x82, f);
    fclose(f);
```

The result is a complete linker script: the host linker's default script (all the standard section definitions, entry point, memory layout) followed by the three CUDA-specific section definitions. This augmented script can be passed to `ld -T` to replace its built-in script entirely.

### Step 5: Validate with `ld -T`

After appending, the generated script is validated by invoking `ld` with the `-T` flag:

```sh
ld -T <output_file> 2>&1 | grep 'no input files' > /dev/null
```

The decompiled construction (lines 1909-1919):

```c
strcpy(buf, "ld -T ");
strcat(buf, filename);
strcat(buf, " 2>&1 | grep 'no input files' > /dev/null");
```

The validation logic is inverted: since no object files are provided, a syntactically valid script will cause `ld` to emit the error `"no input files"`. The `grep` succeeds (exit 0), and `sub_42FA70` returns 0 -- indicating the script is well-formed. If the script has syntax errors, `ld` emits a different error message, `grep` fails (exit 1), and the validation fails.

On validation success, the linker proceeds to exit with code 0. On failure, execution jumps to `LABEL_23`, which calls `sub_467460(&unk_2A5B750, ...)` to emit a fatal error.

If `--verbose` is enabled, the validation command is also printed to stderr.

## The `ld --verbose` Pipeline

Mode 2 (`lcs-abs`) is implemented as two sequential shell commands whose combined effect is to produce a syntactically valid, architecture-correct, CUDA-augmented linker script. The rest of this section reconstructs the full pipeline from the decompiled main() at `0x409800` (lines 1743-1923) and the string constants at `0x1d3415a` (`"ld --verbose "`), `0x1d3416f` (`"ld -T "`), `0x1d343d8` (collect2 filter pipeline), `0x1d34450` (SECTIONS template), `0x1d34508` (grep+sed tail), and `0x1d34770` (the `" -v --verbose"` xmmword loaded via `_mm_load_si128`).

### Data Flow Diagram

```text
                 +---------------------------------------------------+
                 |  main() mode dispatch (dword_2A77DC0 == 2)         |
                 +---------------------------------------------------+
                                          |
                                          v
  +---------+                             |
  | ::src   |---- "gcc" (default) ------->|
  | (ccbin) |                             |
  +---------+                             |
                                          v
                 +---------------------------------------------------+
                 |  Step 1: Compose compiler verbose command          |
                 |  (sub_426AA0 arena allocations)                    |
                 |                                                    |
                 |     <ccbin> + " -v --verbose" (xmmword @1D34770)   |
                 |     if byte_2A5F1D8:  append " -shared "            |
                 |     elif byte_2A5F1E8: append " -r "                |
                 |     if dword_2A5F30C==64: append " -m64 "           |
                 |     elif dword_2A5F30C==32: append " -m32 "         |
                 +---------------------------------------------------+
                                          |
                                          v
                 +---------------------------------------------------+
                 |  Step 2: Append collect2 filter pipeline           |
                 |  (string @1D343D8, 119 bytes)                      |
                 |                                                    |
                 |     " 2>&1 | grep collect2 | grep -wo              |
                 |       -e -pie -e \"-z [^[:space:]]*\"              |
                 |       -e \"-m [^[:space:]]*\" -e -r -e -shared     |
                 |       | tr \"\\n\" \" \" "                         |
                 |                                                    |
                 |     wrap with "$(" ... ")"                         |
                 +---------------------------------------------------+
                                          |
                                          v  (call 1: extraction)
                 +---------------------------------------------------+
                 |  Step 3: Build ld --verbose command                |
                 |  (string @1D3415A = "ld --verbose ")               |
                 |                                                    |
                 |     "ld --verbose " + $(<compiler filter pipeline>)|
                 |     + " | grep -Fvx -e \"$(ld -V)\"                |
                 |       | sed '1,2d;$d' > "                          |
                 |     + (::filename OR "/dev/stdout")                |
                 +---------------------------------------------------+
                                          |
                                          v
                       sub_42FA70 -> system()       [returns 0 on ok]
                                          |
                                          v
                 +---------------------------------------------------+
                 |  Step 4: Append CUDA SECTIONS (130 bytes @1D34450) |
                 |                                                    |
                 |     fopen(::filename, "a")                         |
                 |     fwrite(SECTIONS_TEMPLATE, 1, 0x82, f)          |
                 |     fclose(f)                                      |
                 +---------------------------------------------------+
                                          |
                                          v  (call 2: validation)
                 +---------------------------------------------------+
                 |  Step 5: Validate with ld -T                       |
                 |  (string @1D3416F = "ld -T ")                      |
                 |                                                    |
                 |     "ld -T " + ::filename                          |
                 |     + " 2>&1 | grep 'no input files' > /dev/null"  |
                 +---------------------------------------------------+
                                          |
                                          v
                       sub_42FA70 -> system()    [returns 0 on ok]
                                          |
                                          v
                                     exit(0)
```

### Exact Command Strings

The binary stores four literal shell-command fragments that, when assembled, form the two commands Mode 2 executes. The following table gives each fragment's rodata address, byte length, and the main() xref that reads it.

| Rodata addr | Length | Value (as stored) | xref site |
|---|---|---|---|
| `0x1D34770` | 16 | `" -v --verbose"` (xmmword, `_mm_load_si128`) | `main+...` (line 1784) |
| `0x1D343D8` | 119 | `" 2>&1 \| grep collect2  \| grep -wo -e -pie -e \"-z [^[:space:]]*\" -e \"-m [^[:space:]]*\" -e -r -e -shared  \| tr \"\\n\" \" \" "` | `main+...` (line 1818-1820) |
| `0x1D3415A` | 13 | `"ld --verbose "` | `main+0x199` (`main+0x409999`) |
| `0x1D34508` | 46 | `" \| grep -Fvx -e \"$(ld -V)\" \| sed '1,2d;$d' > "` | `main+...` (line 1864) |
| `0x1D3416F` | 6 | `"ld -T "` | `main+0x2E8` (`main+0x409AE8`) |
| `0x1D34450` | 130 | The `SECTIONS { ... }` template (shared by three `fwrite` calls) | three sites: lines 1838, 1898, 1926 |
| `0x1D34168` | 6 | `"#$ %s\n"` (verbose trace prefix) | `main+...` and `sub_42FA70+0xBE` |

The two composed commands that actually reach `system()` are reproduced below. Placeholders in angle brackets are the runtime-substituted values from the option parser.

**Extraction command** (arena buffer chain `v18 -> v19 -> v20 -> v21 -> v24|v319`):

```sh
ld --verbose $( <ccbin> -v --verbose [-shared|-r] [-m64|-m32] \
                2>&1 | grep collect2 \
                     | grep -wo -e -pie \
                                -e "-z [^[:space:]]*" \
                                -e "-m [^[:space:]]*" \
                                -e -r \
                                -e -shared \
                     | tr "\n" " " ) \
  | grep -Fvx -e "$(ld -V)" \
  | sed '1,2d;$d' \
  > <output_file_or_/dev/stdout>
```

**Validation command** (arena buffer chain `v38 -> v39 -> v40 -> v41`):

```sh
ld -T <output_file> 2>&1 | grep 'no input files' > /dev/null
```

### Parser "State Machine"

nvlink does not parse the `ld --verbose` output itself -- all parsing is delegated to `grep`, `sed`, and `tr`. The effective state machine that the shell pipeline implements is a three-stage line filter. Each stage reads lines from stdin and writes accepted lines to stdout; a line that survives all three stages lands in the output file.

```text
stdin (ld --verbose output)
         |
         |  S0 (line 1):  "GNU ld (GNU Binutils for <distro>) <version>"
         |  S1 (line 2):  "  Supported emulations: ..."
         |  S2 (line 3):  "using internal linker script:"
         |  S3 (line 4):  "=================================================="
         |  S4 (lines 5..N-1):   <script body, possibly preceded by blank>
         |  S5 (line N):  "=================================================="
         |  S6 (line N+1): NUL (EOF)
         |
         v
+-----------------------------------------------------------+
| Stage A: grep -Fvx -e "$(ld -V)"                          |
|                                                           |
| Drops every line whose entire content equals any line of  |
| the multi-line "ld -V" version banner. `-F` = fixed       |
| string, `-v` = invert, `-x` = whole line. Typical "ld -V" |
| output spans 4-5 lines (version, copyright, supported     |
| emulations), which are therefore erased from the main     |
| "--verbose" output. Effect: suppresses the redundant      |
| version preamble that otherwise leaks into the script.    |
+-----------------------------------------------------------+
         |
         v
+-----------------------------------------------------------+
| Stage B: sed '1,2d;$d'                                    |
|                                                           |
| - '1,2d' -- delete lines 1 and 2 (the "using internal     |
|             linker script:" header and the opening        |
|             "===" banner).                                |
| - '$d'   -- delete the final line (the closing "===").    |
|                                                           |
| After stage A has already removed the version banner,     |
| stage B strips the three remaining decorative lines,      |
| leaving only the verbatim body of ld's built-in script.   |
+-----------------------------------------------------------+
         |
         v
+-----------------------------------------------------------+
| Stage C: shell redirect ">"                               |
|                                                           |
| Truncating write to <output_file> or /dev/stdout. No      |
| filtering; just byte copy.                                |
+-----------------------------------------------------------+
         |
         v
stdout (script body, terminated by trailing newline from ld)
```

The decorative lines that stages A and B together eliminate are exactly:

1. `GNU ld (GNU Binutils for <distro>) <version>` (banner line 1)
2. The blank or `Supported emulations: ...` line (banner line 2)
3. `using internal linker script:` (the label that introduces the script)
4. `==================================================` (opening separator)
5. `==================================================` (closing separator)

Lines 1, 2, and 4 are the targets of `sed '1,2d;$d'` after stage A, while the multi-line version banner `ld -V` that stage A removes is a near-superset of lines 1-2 of the `--verbose` output -- the authors used belt-and-suspenders filtering because `ld -V` on some distros emits a single line and on others emits several, and the exact line counts differ by binutils version. Running both filters ensures that whichever lines escape `grep -Fvx` are still caught by `sed '1,2d;$d'`.

### Template Transformation Rules

The pipeline applies a purely additive transformation. The extracted script body is preserved verbatim; only a fixed 130-byte SECTIONS block is appended. No token rewriting, no macro substitution, no path editing occurs. The rules are:

| Rule | Applied to | Effect |
|---|---|---|
| R1: Strip version preamble | `ld --verbose` output | Stage A + B of the filter pipeline delete banner lines |
| R2: Preserve body verbatim | `ld --verbose` output | No other edits to lines that survive filtering |
| R3: Append SECTIONS block | Filtered output file | 130-byte template concatenated via `fopen(file, "a")` |
| R4: No OUTPUT_FORMAT rewrite | Filtered output file | The `OUTPUT_FORMAT(...)` and `OUTPUT_ARCH(...)` directives from the default script are kept unchanged; architecture consistency is already guaranteed by the `collect2` flag extraction in step 2 |
| R5: No INSERT directive | Appended template | The CUDA SECTIONS block is concatenated at the end of the file; it relies on the fact that a standalone `SECTIONS { ... }` block in GNU ld augments (does not replace) the implicit output sections |

Because rule R5 is subtle, it is worth restating: when GNU ld sees a linker script containing a standalone `SECTIONS { ... }` block in addition to a full default script body, it processes the two as consecutive SECTIONS commands. Output sections from the first block are placed first, then the second block's sections are appended. This is the mechanism that allows `.nvFatBinSegment`, `__nv_relfatbin`, and `.nv_fatbin` to land in the output image without colliding with the default `.text`, `.data`, and `.bss` placement.

Because rule R1 strips the version banner, the output file is syntactically a pure linker-script fragment -- it is legal as a `-T` argument. The validation step in Step 5 relies on this: if the filter failed to strip the banner (e.g., because `ld -V` returned unexpected output), the extra non-script text would trigger a syntax error and `ld -T` would not print the expected `no input files` message, causing the `grep` to return non-zero and failing validation.

### Where the Result Goes

The pipeline never pipes the generated script into a child process via stdin. It always materializes the script to a file (or `/dev/stdout`), and it is the calling driver (`nvcc`) that later passes the file path as an argument to the real host linker invocation.

The two possible destinations are:

| `::filename` value | Redirect target | Subsequent consumer |
|---|---|---|
| non-NULL (`-o <file>` was given) | the literal filename | `nvcc` passes `-Wl,-T,<file>` (or equivalently `-T <file>` to `collect2`) when it performs the host link |
| NULL | `/dev/stdout` (`0x6474732F7665642F` + `byte_74756F`) | Whoever invoked `nvlink -ghls` captures stdout -- typically a pipe or here-doc inside the `nvcc` driver |

The `/dev/stdout` path is constructed via two hex-encoded memory stores: the 8-byte immediate `0x6474732F7665642F` decodes to `"/dev/std"` (little-endian), and the 4-byte tail `byte_74756F` contributes `"out\0"`. This avoids shipping a separate `/dev/stdout` string in rodata, saving a few bytes and ensuring the path cannot be relocated by a rodata patcher.

Note the asymmetry between Mode 1 and Mode 2:

- In **Mode 1**, the SECTIONS template is written directly to `stdout` (via `fwrite(..., stdout)` at line 1925) when no output file is given. No shell command is invoked.
- In **Mode 2**, the `ld --verbose` pipeline's redirect points at `/dev/stdout` (not the C stdio stream), and the subsequent SECTIONS append occurs through `fopen(::filename, "a")`. When `::filename` is NULL in Mode 2, the append-step's `fopen` path is skipped -- the only output the user sees is the (already-redirected) `ld --verbose` body from Step 3, without the CUDA SECTIONS block. This is a latent inconsistency in the decompiled code: Mode 2 with no `-o` produces an incomplete script that lacks the CUDA sections. In practice `nvcc` always supplies `-o` when invoking `nvlink -ghls=lcs-abs`, so the buggy branch is never exercised.

### Who Consumes the Script

The generated script is consumed by the host linker driver, **not** directly by `ld`. The chain is:

1. `nvcc` invokes `nvlink -ghls=lcs-abs -o /tmp/<stem>.ld --host-ccbin <cc> [link-flags]`
2. `nvlink` executes the pipeline described above, producing `/tmp/<stem>.ld`
3. `nvcc` then invokes the host compiler as a linker driver: `<cc> -Wl,-T,/tmp/<stem>.ld host1.o host2.o ... -lstdc++ -lcudart ...`
4. The host compiler's internal `collect2` forwards the `-T` to `ld`
5. `ld` reads `/tmp/<stem>.ld` **in place of** its built-in default script (the `-T` flag, unlike `-dT`, fully replaces the default script)

Step 5 is why nvlink goes to the trouble of extracting `ld`'s built-in default script: a `-T` script must be complete on its own, not a fragment, and the default script is architecture-dependent (32 vs 64 bit, PIE vs non-PIE, shared vs executable). Mode 1 (`lcs-aug`) skips extraction because the caller intends to use `-dT` (the "augment" form) or to splice the fragment into a larger script manually.

## The `--Xlinker` / `--host-linker-options` Path

When `--host-linker-options` (short form `--Xlinker`) is specified, the command construction in step 1-2 takes an alternative path. Instead of building the `gcc -v --verbose` + collect2 pipeline, the code iterates through the linked list of `--Xlinker` values (`qword_2A5F2E8`) and concatenates them into the command string directly:

```c
// main() lines 1746-1776
if (qword_2A5F2E8) {
    // Iterate linked list: each node has [next_ptr, value_string]
    node = *(qword **)qword_2A5F2E8;
    result = *(char **)(qword_2A5F2E8 + 8);
    while (node) {
        option = (char *)node[1];
        // Allocate and concatenate
        buf = arena_alloc(strlen(result) + strlen(option) + 1);
        strcpy(buf, result);
        result = strcat(buf, option);
        node = (qword *)*node;
    }
    // result now contains all Xlinker options concatenated
}
```

This path bypasses the collect2 detection entirely. The `-Xlinker` values are treated as pre-composed `ld` flags, and the mode 2 pipeline uses them directly in the `ld --verbose` invocation. The option help text describes this as "Specify options directly to the host linker (ignored by nvlink)" -- the options are not used during device linking, only during linker script generation.

## Error Handling

Three error conditions are handled:

| Condition | Error source | Behavior |
|---|---|---|
| Cannot open output file | `fopen` returns NULL | `sub_467460(&unk_2A5B710, filename, ...)` -- fatal error with filename |
| Shell command fails | `sub_42FA70` returns nonzero | `sub_467460(&unk_2A5B750, ...)` -- fatal error for invalid script generation |
| Validation fails | `ld -T` grep returns nonzero | Same error via `LABEL_23` -- the generated script is malformed |

All errors route through the standard error reporting system (`sub_467460`). The error at `unk_2A5B750` is specific to linker script generation failure. The error at `unk_2A5B710` is the generic "cannot open file" error shared with other output paths.

An unexpected mode value (anything other than 1 or 2 when the linker script path is entered) triggers `sub_467460(&unk_2A5B750, ...)` as a defensive check. This is unreachable in practice since the mode is only set to 1 or 2 by the option parser.

## Verbose Trace

When `--verbose` (`byte_2A5F2D8`) is enabled, each shell command executed during mode 2 is printed to stderr with the prefix `#$ `:

```c
if (byte_2A5F2D8)
    fprintf(stderr, "#$ %s\n", command);
```

This affects two commands: the `ld --verbose` extraction pipeline (line 1881) and the `ld -T` validation command (line 1916). Mode 1 does not execute any shell commands, so verbose has no effect there.

## Mutual Exclusion with Input Files

If `-ghls` is specified alongside input files (`qword_2A5F330 != NULL`), the option parser emits a fatal error via `sub_467460(&unk_2A5B760, ...)`. Linker script generation is a standalone operation and cannot be combined with device linking. This check is performed in `nvlink_parse_options` at `0x427AE0` immediately after setting the mode variable.

## When nvcc Uses This

The linker script generation feature is invoked by `nvcc`'s driver during host linking of CUDA programs. The typical sequence is:

1. `nvcc` compiles device code to fatbins and embeds them in host `.o` files
2. Before host linking, `nvcc` invokes `nvlink -ghls=lcs-abs -o /tmp/script.ld --host-ccbin <compiler> [--shared] [-m64|-m32]`
3. `nvlink` generates the augmented script and exits
4. `nvcc` passes `-T /tmp/script.ld` to the host linker (`ld` or `collect2`)
5. The host linker preserves `.nvFatBinSegment`, `__nv_relfatbin`, and `.nv_fatbin` sections in the output executable
6. At runtime, `__cudaRegisterFatBinary` locates the fatbin data via these sections

The `lcs-aug` mode is available for cases where `nvcc` wants only the CUDA fragment (to manually splice into an existing script), but the default `lcs-abs` mode is what `nvcc` typically uses for standard compilation flows.

## Function Cross-Reference

| Function | Address | Role in linker script generation |
|---|---|---|
| `main` | `0x409800` | Contains the entire linker script generation logic (lines 1743-1936) |
| `nvlink_parse_options` | `0x427AE0` | Parses `-ghls`, sets `dword_2A77DC0`, validates mutual exclusion |
| `sub_42FA70` | `0x42FA70` | `system()` wrapper -- executes the shell pipelines |
| `sub_426AA0` | `0x426AA0` | Arena allocator for command string buffers |
| `sub_431000` | `0x431000` | Arena free -- releases intermediate buffers |
| `sub_467460` | `0x467460` | Fatal error emission |
| `sub_476D90` | `0x476D90` | Consumer side -- validates host ELF contains the three CUDA sections |
| `sub_476D80` | `0x476D80` | Predicate -- checks for `.nvFatBinSegment` section existence |
| `sub_476EC0` | `0x476EC0` | Section name lookup predicate used by the above |

## Cross-References

**Internal (nvlink wiki):**

- [CLI Flags](../config/cli-flags.md) -- `-ghls` / `--gen-host-linker-script` option and its `lcs-aug` / `lcs-abs` argument values
- [Environment Variables](../config/env-vars.md) -- `--host-ccbin` setting that determines the host compiler used for script generation
- [Pipeline Entry](../pipeline/entry.md) -- `main()` lines 1743--1936 where the linker script generation logic resides
- [Output Phase](../pipeline/output.md) -- "Host Linker Script Output" sub-section documents this path from the pipeline's perspective (Mode 2 is one of the non-ELF output routes)
- [NVIDIA Section Types](../elf/nvidia-sections.md) -- Fatbin sections (`.nvFatBinSegment`, `__nv_relfatbin`, `.nv_fatbin`) referenced in the SECTIONS template
- [Host ELF Input](../input/host-elf.md) -- Host ELF processing that validates the presence of the three CUDA sections
- [Fatbin Extraction](../input/fatbin-extraction.md) -- How embedded fatbins in host objects are located and extracted
- [Error Reporting](error-reporting.md) -- `sub_467460` fatal error emission on script generation or validation failure
- [Memory Arenas](memory-arenas.md) -- Arena allocator (`sub_426AA0`, `sub_431000`) for command string buffer management
- [Library Search](library-search.md) -- Another subsystem that composes shell-like arguments from CLI options and environment variables (`-l<name>`, `-L<dir>`, `LIBRARY_PATH`), sharing the same arena-allocated buffer-chain idiom used by the linker script command builder. Both subsystems funnel through infrastructure wrappers: library search invokes `sub_42A2D0` (archive probing with direct `open`/`read` syscalls), while linker script generation invokes `sub_42FA70` (the `system()` wrapper that shells out to `gcc`, `ld`, `grep`, and `sed`)

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| SECTIONS template string with 3 CUDA sections | HIGH | Exact string at `0x1d34450` in strings JSON: `"SECTIONS\n{\n\t.nvFatBinSegment : { *(.nvFatBinSegment) }\n\t__nv_relfatbin : { *(__nv_relfatbin) } \n\t.nv_fatbin : { *(.nv_fatbin) }\n}\n"` |
| Template size is 130 bytes (0x82) | MEDIUM | String length matches approximately; the `fwrite` size was inferred from decompiled main() |
| `-ghls` / `--gen-host-linker-script` option | HIGH | Strings `"gen-host-linker-script"` at `0x1d327fc` and `"Input files are not allowed with -ghls option"` at `0x1d34e80` |
| `lcs-aug` and `lcs-abs` mode values | HIGH | String `"lcs-aug,lcs-abs"` at `0x1d3282d`; `"lcs-aug"` standalone at `0x1d329bd` |
| `system()` wrapper at `sub_42FA70` | HIGH | Decompiled: calls `system(v9)` after optional `fprintf(stream, "#$ %s\n", a6)` for verbose trace |
| Verbose trace prefix `"#$ "` | HIGH | `sub_42FA70` decompiled: `fprintf(stream, "#$ %s\n", a6)` -- exact format |
| collect2 detection pipeline string | HIGH | Exact string at `0x1d343d8`: `' 2>&1 \| grep collect2  \| grep -wo -e -pie -e "-z [^[:space:]]*" -e "-m [^[:space:]]*" -e -r -e -shared  \| tr "\\n" " "'` |
| `ld --verbose` extraction command | HIGH | String `"ld --verbose "` at `0x1d3415a` in strings JSON |
| `ld -T` validation command | HIGH | String `"ld -T "` at `0x1d3416f` |
| Validation uses `grep 'no input files'` | HIGH | Exact string `" 2>&1 \| grep 'no input files' > /dev/null"` at `0x1d34508` |
| `--host-ccbin` option for host compiler | HIGH | String `"host-ccbin"` at `0x1d3283d` |
| `--shared` and `-r` linker flags | HIGH | String `"Percolate the nvcc -shared option"` at `0x1d33ad0`; `-shared` and `-r` in collect2 grep pipeline |
| Section names `.nvFatBinSegment`, `__nv_relfatbin`, `.nv_fatbin` | HIGH | Individual strings at `0x1d40770`, `0x1d40781`, `0x1d40790` in strings JSON |
| Consumer function `sub_476D90` validates host ELF sections | HIGH | Decompiled file exists; calls `sub_476EC0` for section-name lookup |
| Mode variable `dword_2A77DC0` values 1 and 2 | MEDIUM | Inferred from main() decompiled conditional branching; not directly visible as named constant |
| Signal handler in `sub_42FA70` checks `v10 & 0x7F` for tool termination | HIGH | Decompiled: `if (__OFSUB__((v10 & 0x7F) + 1, 1))` followed by `sub_467460(&unk_2A5BB00, ...)` for signal and `sub_467460(&unk_2A5BB40, ...)` for core dump |
| `" -v --verbose"` is appended via a single 16-byte SSE store from `xmmword_1D34770` | HIGH | Decompiled main() line 1784: `*v173 = _mm_load_si128((const __m128i *)&xmmword_1D34770);` -- 16 bytes exactly matches `" -v --verbose\0\0"` |
| Mode 2 with no `-o` produces a script without the CUDA SECTIONS block | MEDIUM | Decompiled main() line 1892 gates the append-step behind `if (::filename)`; the else-branch at line 1925 writes only the bare template to stdout, which is dead code for Mode 2 because Step 3 already redirected to `/dev/stdout` |
| `ld -T` replaces (does not augment) the default script | HIGH | GNU ld documented behavior: `-T scriptfile` replaces default; `-dT` or script loaded via `INSERT` augments. Mode 2 generates a script that is self-contained precisely so `-T` (not `-dT`) suffices |
| Pipeline consumer chain (`nvcc` -> host cc -> `collect2` -> `ld`) | MEDIUM | `nvcc` behavior documented in CUDA toolkit reference; the Mode 2 pipeline's `collect2`-aware flag extraction step (Step 2) is direct evidence that the eventual consumer of the script is a `collect2`-invoked `ld`, not bare `ld` |
| `/dev/stdout` literal built from two hex stores (`0x6474732F7665642F` + `byte_74756F`) | HIGH | Decompiled main() lines 1876-1878: `*(_QWORD *)v320 = 0x6474732F7665642FLL; *((_DWORD *)v320 + 2) = (_DWORD)&byte_74756F;` -- bytes `2F 76 65 64 2F 73 74 64` decode to `/dev/std` in little-endian, followed by `"out\0"` |
