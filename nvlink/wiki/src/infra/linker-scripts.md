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

## The SECTIONS Template

All code paths share a single hardcoded 130-byte string that defines three CUDA-specific host ELF sections:

```
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
