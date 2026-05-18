# Option Forwarding to cicc and ptxas

When nvlink performs link-time optimization it does not compile NVVM IR itself -- it delegates to the cicc back-end through libNVVM. Before invoking `nvvmCompileProgram`, nvlink must assemble a complete option vector that tells cicc which target architecture to generate for, what optimization levels to use, and which per-module math-mode settings the original translation units agreed on. Three functions handle this pipeline: `sub_426CD0` builds the cicc/NVVM option list (an array of string pointers), `sub_429BA0` builds the ptxas option string (a single space-separated string for the embedded assembler), and `sub_4BC6F0` appends host-reference and variables-tracking options at compilation time.

| | |
|---|---|
| **cicc option builder** | `sub_426CD0` at `0x426CD0` (7,040 bytes / 275 lines) |
| **ptxas option builder** | `sub_429BA0` at `0x429BA0` (6,699 bytes / 306 lines) |
| **Compile-time augmenter** | `sub_4BC6F0` at `0x4BC6F0` (13,602 bytes / 489 lines) |
| **Caller** | `main()` at `0x409800`, LTO pipeline branch |
| **Downstream** | `sub_4BC6F0` (`nvvm_compile_and_extract`) passes the option vector to `nvvmCompileProgram` |

## cicc Option List Construction (`sub_426CD0`)

`sub_426CD0` takes three parameters: a pointer to the linker state, a pointer to the module list, and a pointer to an output count. It creates an empty linked list (`sub_464AE0(16)`), appends option strings one at a time via `sub_464C30`, and finally converts the list into a flat `char**` array via `sub_464BC0`. The output count is written through the third parameter.

Every option string is allocated through `sub_426AA0` (arena-backed `strdup`) and appended to the list via `sub_464C30`. The options are appended in a fixed order. The following subsections describe each option and the conditions under which it is emitted.

### Always-Emitted Options

These two options appear in every LTO invocation, unconditionally:

| Option | Format | Source |
|---|---|---|
| `-arch=compute_N` | `snprintf("-arch=compute_%d", dword_2A5F314)` | Target SM number from `--arch` |
| `-link-lto` | Literal string | Tells cicc this is a link-time compilation |

The architecture number is the raw integer from the `--arch` option (e.g. 90, 100), not the `compute_` string -- the format prefix is hardcoded in the `snprintf` call. The buffer is 80 bytes (`0x50`), which limits the formatted string length.

### Split Compilation Options

Split compilation is controlled by two independent option variables:

| Global | Meaning | Default |
|---|---|---|
| `dword_2A5B514` | `-split-compile-extended` thread count | 1 (disabled) |
| `dword_2A5B518` | `-split-compile` thread count | 1 (disabled) |

The forwarding logic has three cases:

```
if split_compile_extended == 1:
    // Not specified -- check split_compile
    if split_compile != 1:
        emit "-split-compile=<split_compile>"
else:
    // Extended was specified
    emit "-split-compile-extended=<split_compile_extended>"
    if split_compile != 1:
        if split_compile_extended != 1:
            warning: both -split-compile and -split-compile-extended specified
        else:
            emit "-split-compile=<split_compile>"
```

When both `-split-compile` and `-split-compile-extended` are specified with non-default values, nvlink emits only `-split-compile-extended` and produces a diagnostic via `sub_467460` warning that both were given. When only one is non-default, the corresponding option is forwarded. When both are 1 (the default), neither option is emitted and cicc uses its own default.

### Ofast-Compile Level

The Ofast-compile level (`qword_2A5F258`) is a string pointer. If non-NULL, the function checks the first three characters to determine the level:

| Check | Characters | Option emitted |
|---|---|---|
| `'m','a','x','\0'` | `max` | `-Ofast-compile=max` |
| `'m','i','d','\0'` | `mid` | `-Ofast-compile=mid` |
| `'m','i','n','\0'` | `min` | `-Ofast-compile=min` |

The character comparisons are byte-level: `v10[0] == 'm'` (109), `v10[1] == 'a'` (97) or `'i'` (105), etc. The value `"0"` (the disabled case) does not match any of these patterns and produces no option. When the pointer is NULL, the option is skipped entirely.

Note: the CLI parser also accepts `"0"` as a valid Ofast-compile value (meaning "disabled"). The forwarding function only recognizes `min`, `mid`, and `max`.

### Register Limit

If `dword_2A5F22C` (the `--maxrregcount` value) is greater than zero, the function emits:

```
-maxreg=<N>
```

Note the option name difference: the nvlink CLI option is `--maxrregcount` but the forwarded cicc option is `-maxreg`. This name mapping is hardcoded.

### Conditional Boolean Options

These options are emitted only when their corresponding global flag is set:

| Flag | Address | Option Emitted | Meaning |
|---|---|---|---|
| `byte_2A5F24C` | generate-line-info | `-generate-line-info` | Emit line number debug information |
| `byte_2A5F244` | inline-info | `-inline-info` | Emit inlining decision diagnostics |
| `byte_2A5F286` | relocatable compile | `--device-c` | Separate compilation mode |
| `byte_2A5F285` | force-partial-lto | `--force-device-c` | Force partial LTO (relocatable output) |
| `byte_2A5F310` | debug | `-g` | Debug compilation |

The `-generate-line-info` option is stored as an SSE constant load (`xmmword_1D34730`) -- the decompiler shows `_mm_load_si128` because the 20-byte string is loaded as a 16-byte SSE register plus a 4-byte `dword`. Similarly, `--force-device-c` uses `xmmword_1D34740`. The underlying strings are `-generate-line-info` and `--force-device-c` respectively.

### Host Info and Dead Code Elimination

When `byte_2A5F214` (mark-used, meaning `--use-host-info` or `--kernels-used` was specified) is set **and** `byte_2A5F285` (force-partial-lto) is **not** set, two things happen:

1. `sub_426AE0` is called -- this processes the module list using host-provided symbol usage information. `sub_426AE0` inspects each module's host-reference metadata (offsets +24 through +26 in the module descriptor), skipping `cudadevrt` entries. If any module has valid host-reference data (offset +25 set), the function sets `byte_2A5F211 = 1` and optionally invokes up to six host-info insertion functions (`sub_43F020` through `sub_43F340`) that populate the linker's external-symbol tracking sets.

2. If `byte_2A5F211` is set after the host-info pass, the option `-has-global-host-info` is appended. This flag tells cicc that the linker has provided host symbol usage information, enabling cicc to perform more aggressive dead code elimination during compilation.

The guard on `byte_2A5F285` means that in partial-LTO mode (relocatable compilation), host info is never forwarded -- the linker cannot know which symbols will be needed by future link steps.

### -Xnvvm Passthrough Options

The `-Xnvvm` mechanism allows users to pass arbitrary options directly to cicc. These options are accumulated during CLI parsing into `qword_2A5F230` (a linked list of strings). The forwarding logic processes them as follows:

```
if qword_2A5F230 != NULL:
    // Phase 1: Tokenize all -Xnvvm strings
    // Each -Xnvvm value may contain spaces; split on spaces
    for each xnvvm_entry in qword_2A5F230:
        tokenize(xnvvm_entry, " ") -> append tokens to flat_list

    // Phase 2: Reverse the flat list (sub_4649E0)
    // The list was built in LIFO order; reverse to get CLI order
    flat_list = reverse(flat_list)

    // Phase 3: Scan for math-mode options and filter duplicates
    seen_ftz = false
    seen_prec = false
    seen_fma = false

    for each token in flat_list:
        if starts_with(token, "-ftz="):       seen_ftz = true
        if starts_with(token, "-prec-div="):  seen_prec = true
        if starts_with(token, "-prec-sqrt="): seen_prec = true
        if starts_with(token, "-fma="):       seen_fma = true

        // Skip options already emitted by earlier phases
        if token == "-link-lto":                         skip
        if generate_line_info && token == "-generate-line-info": skip
        if inline_info && token == "-inline-info":       skip
        if relocatable && token == "--device-c":         skip
        if force_partial && token == "--force-device-c": skip
        if debug && token == "-g":                       skip
        if ofast_compile && starts_with(token, "-Ofast-compile="): skip
        if token == "-compile-time":                     skip
        if use_host_info && !force_partial && has_global_host_info
           && token == "-has-global-host-info":          skip

        // Anything that survived filtering: forward verbatim
        append token to option_list
```

The filtering ensures that options which nvlink has already emitted (with possibly different values derived from consensus) are not duplicated. The `-compile-time` option is always stripped -- it is an internal profiling flag that should not be forwarded.

The tokenization function `sub_44EC40` splits each `-Xnvvm` value on space characters and appends each resulting token to a flat linked list via the callback `sub_4644C0`. The tokenizer passes parameters `(string, delimiter, 0, 1, callback, &list, 0, 0)`.

### Math-Mode Defaults from Consensus

After processing the `-Xnvvm` tokens, `sub_426CD0` fills in default values for any math-mode options that were not explicitly provided through `-Xnvvm`. This is where the per-module option consensus values (tracked during fatbin extraction) are consumed:

| Option | Consensus Value Variable | Emitted When |
|---|---|---|
| `-ftz=N` | `dword_2A5F274` | Not seen in `-Xnvvm` tokens |
| `-prec-div=N` | `dword_2A5B524` | Not seen in `-Xnvvm` tokens |
| `-prec-sqrt=N` | `dword_2A5B520` | **Always** (unconditionally) |
| `-fma=N` | `dword_2A5B51C` | Not seen in `-Xnvvm` tokens |

There is an asymmetry: `-prec-sqrt` is always emitted regardless of whether an explicit value appeared in `-Xnvvm`, while the other three are only emitted if the `-Xnvvm` scan did not find them. In the decompiled code, `-prec-sqrt` is emitted at line 258 outside any conditional block, while `-prec-div` is gated by `if (!v19)` at line 251 and `-ftz`/`-fma` by their own flags at lines 244 and 262 respectively.

The `seen_prec` flag (v19) is shared between `-prec-div=` and `-prec-sqrt=` in the scan phase, but only `-prec-div` emission respects it. This means if a user provides `-Xnvvm -prec-sqrt=0`, the scan sets `seen_prec = true`, which suppresses the consensus `-prec-div` default -- but the consensus `-prec-sqrt` is still emitted unconditionally, potentially duplicating the user's value. This is either a deliberate safety measure (ensuring cicc always receives an explicit `-prec-sqrt`) or a latent bug where `-prec-sqrt` should have its own `seen_sqrt` flag.

The consensus values come from the per-module option tracking performed during fatbin extraction (see below).

## Compile-Time Option Augmentation (`sub_4BC6F0`)

The option array produced by `sub_426CD0` is not the final set passed to `nvvmCompileProgram`. The compilation function `sub_4BC6F0` allocates a new array with capacity `(option_count + 8)` entries, copies the original options, and conditionally appends additional options:

### `--force-device-c` Scan

Before appending host-reference options, `sub_4BC6F0` scans the existing option array for the `--force-device-c` string (lines 213-235). This is a byte-by-byte comparison loop across all `a8` entries. If `--force-device-c` is found, `v25` is set to 1; otherwise `v30` is set to 1 (indicating host-ref options should be added).

### Host-Reference Options

When the linker context field at offset 97 (`elfw[97]`) is set **and** `--force-device-c` is **not** present in the option array, up to six host-reference options are appended:

| Option prefix | Source (elfw offset) | Semantics |
|---|---|---|
| `-host-ref-ek=` | `elfw[520]` | Externally-visible kernel references |
| `-host-ref-ik=` | `elfw[528]` | Internally-visible kernel references |
| `-host-ref-ec=` | `elfw[536]` | Externally-visible constant references |
| `-host-ref-ic=` | `elfw[544]` | Internally-visible constant references |
| `-host-ref-eg=` | `elfw[552]` | Externally-visible global references |
| `-host-ref-ig=` | `elfw[560]` | Internally-visible global references |

Each option is constructed via `strcpy(buf, "-host-ref-XX=")` followed by `strcat(buf, value)` where the value is extracted by `sub_43FBC0` from the corresponding elfw field. Options are only appended if `sub_43FBC0` returns non-NULL for that field. The host-ref values originate from the host ELF analysis during input processing -- the host linker embeds lists of which device symbols the host code references.

### Variables Flag

When the linker context field at offset 98 (`elfw[98]`) is set (meaning `--variables-used` tracking is active), the literal string `"-variables"` is appended to the option array. This instructs libnvvm to preserve all global variables regardless of whether they appear referenced.

The `-variables` string is loaded via an SSE constant (`xmmword_1D48A60`) into a stack buffer at offset `si128`, and the pointer to this buffer is placed in the option array slot.

### Option Array Layout at Compile Time

The final option array passed to `nvvmCompileProgram` can contain up to `option_count + 8` entries:

```
slots [0 .. option_count-1]:   options from sub_426CD0
slots [option_count .. +5]:    up to 6 host-ref-{ek,ik,ec,ic,eg,ig} options
slot  [option_count+6]:        "-variables" (if active)
remaining slots:               unused padding
```

## Per-Module Option Consensus

When nvlink extracts NVVM IR modules from fatbin containers (`sub_42AF40`), each module can carry embedded compiler options (stored as strings in the fatbin member metadata). These options may differ across modules compiled with different flags. The linker must decide on a single value for each math-mode option before forwarding to cicc.

### 5-State Consensus Machine

The consensus mechanism uses a **5-state** machine per tracked option. State transitions depend on whether each module's embedded option string contains or lacks the tracked option:

| State | Value | Meaning |
|---|---|---|
| 0 | `UNINITIALIZED` | No module has been processed yet (globals start at zero) |
| 1 | `ALL_ABSENT` | Every module processed so far lacked this option in its embedded string |
| 2 | `ALL_PRESENT` | At least one module provided this option; all providing modules agree on value |
| 3 | `MIXED_PRESENCE` | Some modules provided the option, others did not; no value conflict |
| 4 | `VALUE_CONFLICT` | Two or more modules provided different values for this option |

The transition table for integer options (`-ftz`, `-prec_div`, `-prec_sqrt`, `-fmad`, `-maxreg`, `-split-compile`):

```
Current State  |  Module HAS option            |  Module LACKS option
-------------------------------------------------------------------
0 (UNINIT)     |  -> 2, record value            |  -> 1
1 (ALL_ABSENT) |  -> 3, record value            |  stay 1
2 (ALL_PRESENT)|  if val == old: stay 2         |  -> 3
               |  if val != old: -> 4           |
3 (MIXED)      |  if val == old: stay 3         |  stay 3
               |  if val != old: -> 4           |
4 (CONFLICT)   |  stay 4                        |  stay 4
```

For boolean options (`-generate-line-info`, `-inline-info`), the transitions are structurally identical except the "value" is always 1 when present (the corresponding `byte` flag is set to 1 unconditionally on presence).

States 3 (MIXED_PRESENCE) and 4 (VALUE_CONFLICT) behave differently: state 3 uses the value from the first module that provided the option without emitting a warning, while state 4 triggers a diagnostic via `sub_467460`. In both cases the first-seen value is forwarded to cicc.

### Tracked Options

Eight options are tracked with paired global variables (state + value):

| Fatbin Option String | State Variable | Value Variable | Forwarded cicc Name |
|---|---|---|---|
| `-ftz=` | `dword_2A5F270` | `dword_2A5F274` | `-ftz=N` |
| `-prec_div=` | `dword_2A5F26C` | `dword_2A5B524` | `-prec-div=N` |
| `-prec_sqrt=` | `dword_2A5F268` | `dword_2A5B520` | `-prec-sqrt=N` |
| `-fmad=` | `dword_2A5F264` | `dword_2A5B51C` | `-fma=N` |
| `-maxreg ` | `dword_2A5F250` | `dword_2A5F254` | `-maxreg=N` |
| `-split-compile ` | `dword_2A5F260` | `dword_2A5B518` | `-split-compile=N` |
| `-generate-line-info` | `dword_2A5F248` | `byte_2A5F24C` | `-generate-line-info` |
| `-inline-info` | `dword_2A5F240` | `byte_2A5F244` | `-inline-info` |

### Name Translation in Consensus

The embedded option strings in fatbin metadata use older naming conventions that differ from what nvlink forwards to cicc:

| Fatbin Embedded Name | Search Method | Forwarded cicc Name |
|---|---|---|
| `-ftz=N` | `strstr(haystack, "-ftz=")` | `-ftz=N` (same) |
| `-prec_div=N` | `strstr(haystack, "-prec_div=")` | `-prec-div=N` (underscore to hyphen) |
| `-prec_sqrt=N` | `strstr(haystack, "-prec_sqrt=")` | `-prec-sqrt=N` (underscore to hyphen) |
| `-fmad=N` | `strstr(haystack, "-fmad=")` | `-fma=N` (name change: fmad to fma) |
| `-maxreg N` | `strstr(haystack, "-maxreg ")` | `-maxreg=N` (space to equals) |
| `-split-compile N` | `strstr(haystack, "-split-compile ")` | `-split-compile=N` (space to equals) |

The space-delimited format for `-maxreg` and `-split-compile` in the fatbin string reflects the old cicc command-line convention (positional arguments); the forwarded format uses `key=value` pairs. The underscore-to-hyphen and `-fmad`-to-`-fma` translations are implicit in the forwarding logic -- `sub_42AF40` parses using the old names and stores values in global variables, while `sub_426CD0` emits using the new names from those same globals.

### Conflict Diagnostics

When the state reaches `VALUE_CONFLICT` (4), the linker emits a warning diagnostic (via `sub_467460`) indicating that modules disagree on the option value. The first value seen is used as the forwarded value. This is the origin of the nvlink warning messages:

```
nvlink warning: module compiled with different -ftz setting
nvlink warning: module compiled with different -prec-div setting
```

For boolean options (`-generate-line-info`, `-inline-info`), the "value" is always 1 when present. For integer options (`-ftz`, `-prec_div`, `-prec_sqrt`, `-fmad`), the value is typically 0 or 1 but the mechanism supports arbitrary integers (parsed via `strtol`). For `-maxreg`, the value is the register count limit. For `-split-compile`, the value is the thread count.

## ptxas Option String Construction (`sub_429BA0`)

`sub_429BA0` builds a single space-separated string for the embedded ptxas assembler. It operates independently of `sub_426CD0` but reads from many of the same global variables.

### -Xptxas String Builder

The `-Xptxas` values stored in `qword_2A5F238` (a linked list) are concatenated using a StringBuilder pattern:

```c
buf = string_builder_create(128);            // sub_44FB20(128) -- initial capacity 128 bytes
for each entry in qword_2A5F238:
    if string_builder_nonempty(buf):         // sub_4504A0(buf)
        string_builder_append_char(buf, ' '); // sub_44FF90(buf, 32) -- space separator
    string_builder_append_str(buf, entry);    // sub_44FE60(buf, entry)
xptxas_joined = string_builder_extract(buf);  // sub_44FDC0(buf) -- returns C string
```

This produces a single space-separated string of all `-Xptxas` values. If the linked list is empty (`sub_464740` returns 0), the join is skipped and `xptxas_joined` remains NULL.

### Options Forwarded to ptxas

Each individual option is `snprintf`-formatted into a separately arena-allocated buffer with an exact size limit. If a `snprintf` exceeds the buffer, `sub_467460` emits a diagnostic warning about the overflow.

| Option | Condition | Format | Buffer Size |
|---|---|---|---|
| `-Xptxas` passthrough | `qword_2A5F238` non-NULL and non-empty | All `-Xptxas` values joined with spaces | Dynamic (strlen of joined string) |
| `-maxrregcount=N` | `dword_2A5F22C > 0` | `"-maxrregcount=%d"` | 18 bytes (max 17 chars + null) |
| `-cuda-api-version=VER` | `qword_2A5F218` non-NULL | `"-cuda-api-version=%s"` | 23 bytes (max 22 chars + null) |
| `--Ofast-compile=LEVEL` | `qword_2A5F258` matches `min`/`mid`/`max` | `"--Ofast-compile=%s"` | 20 bytes (max 19 chars + null) |
| `--device-stack-protector-frame-size-threshold=N` | `byte_2A5F1FC` set | `"--device-stack-protector-frame-size-threshold=%d"` | 50 bytes (max 49 chars + null) |
| `--device-stack-protector=true/false` | `byte_2A5F1FF` set | Literal `"--device-stack-protector=true"` or `"=false"` | 30 or 31 bytes (literal string) |
| `-split-compile=N` | `dword_2A5B518 != 1` | `"-split-compile=%d"` | 19 bytes (max 18 chars + null) |

The `--device-stack-protector` value depends on `byte_2A5F1FE`: when set, the string is `"--device-stack-protector=true"` (30 bytes including null); when clear, `"--device-stack-protector=false"` (31 bytes). This value is determined by the CLI parser -- `byte_2A5F1FE` holds the boolean value of the option, while `byte_2A5F1FF` records whether the option was explicitly specified.

### Final String Assembly

The six option components are assembled into a single output string via:

```c
snprintf(dest, total_length, "%s %s %s %s %s %s",
    xptxas_joined,           // or "" if NULL
    maxrregcount_buf,        // or "" if NULL
    cuda_api_version_buf,    // or "" if NULL
    ofast_compile_buf,       // or "" if NULL
    device_stack_protector,  // or "" if NULL
    split_compile_buf);      // or "" if NULL
```

The total output buffer length is the sum of all individual component lengths plus 7 (for the six space separators and null terminator). Each NULL pointer is replaced with an empty string `""` before the final `snprintf`.

The `--device-stack-protector-frame-size-threshold` buffer is **not** included in this six-component `snprintf`. Its buffer is allocated and formatted separately, and freed via `sub_431000` at the end of the function (line 301-303). This option reaches the embedded ptxas through a separate mechanism -- it is concatenated into the `-Xptxas` stream or passed as a distinct parameter to the embedded ptxas invocation.

After the final string is assembled, if the `-Xptxas` joined string was non-NULL, it is freed via `sub_431000`.

### Early-Exit Conditions

`sub_429BA0` has a multi-branched early-exit path. When **all** of the following hold simultaneously:

- No `-Xptxas` options: `qword_2A5F238 == NULL`
- No maxrregcount: `dword_2A5F22C <= 0`
- No cuda-api-version: `qword_2A5F218 == NULL`
- No device-stack-protector: `byte_2A5F1FF == 0`
- No frame-size-threshold: `byte_2A5F1FC == 0`
- Split-compile at default: `dword_2A5B518 == 1`
- No Ofast-compile (or value is not `min`/`mid`/`max`): `qword_2A5F258 == NULL` or first char is not `'m'` or pattern does not match

The function returns NULL (specifically the value of `qword_2A5F218`, which is NULL in this case). This signals to the caller that no ptxas-specific options need to be forwarded.

The Ofast-compile early-exit check does a full byte-by-byte verification of `"max"` / `"mid"` / `"min"`. If the pointer is non-NULL but the value is `"0"` (disabled), it does not match any pattern and the function returns early (treating it as "no option to forward").

## Forwarded Options Matrix

Complete mapping of every option that flows from nvlink CLI to cicc and/or ptxas:

| nvlink CLI Flag | Forwarded to cicc | Forwarded to ptxas | Source |
|---|---|---|---|
| `--arch sm_N` | `-arch=compute_N` | *(arch set separately)* | `sub_426CD0` |
| `--link-time-opt` / `-lto` | `-link-lto` | *(not forwarded)* | `sub_426CD0` |
| `--maxrregcount N` | `-maxreg=N` | `-maxrregcount=N` | `sub_426CD0` / `sub_429BA0` |
| `--Ofast-compile LEVEL` / `-Ofc` | `-Ofast-compile=LEVEL` | `--Ofast-compile=LEVEL` | `sub_426CD0` / `sub_429BA0` |
| `--split-compile N` | `-split-compile=N` | `-split-compile=N` | `sub_426CD0` / `sub_429BA0` |
| `--split-compile-extended N` | `-split-compile-extended=N` | *(not forwarded)* | `sub_426CD0` |
| `-g` / `--debug` | `-g` | *(not forwarded)* | `sub_426CD0` |
| `--device-c` | `--device-c` | *(not forwarded)* | `sub_426CD0` |
| `--force-partial-lto` | `--force-device-c` | *(not forwarded)* | `sub_426CD0` |
| `-Xnvvm OPTS` | *(forwarded verbatim, filtered)* | *(not applicable)* | `sub_426CD0` |
| `-Xptxas OPTS` | *(not forwarded)* | *(joined space-separated)* | `sub_429BA0` |
| `--device-stack-protector` | *(not forwarded)* | `--device-stack-protector=true/false` | `sub_429BA0` |
| `--device-stack-protector-frame-size-threshold N` | *(not forwarded)* | `--device-stack-protector-frame-size-threshold=N` | `sub_429BA0` |
| `--cuda-api-version VER` | *(not forwarded)* | `-cuda-api-version=VER` | `sub_429BA0` |
| `--use-host-info` | `-has-global-host-info` | *(not forwarded)* | `sub_426CD0` |
| *(consensus)* `-generate-line-info` | `-generate-line-info` | *(not forwarded)* | `sub_426CD0` |
| *(consensus)* `-inline-info` | `-inline-info` | *(not forwarded)* | `sub_426CD0` |
| *(consensus)* `-ftz` | `-ftz=N` | *(not forwarded)* | `sub_426CD0` |
| *(consensus)* `-prec-div` | `-prec-div=N` | *(not forwarded)* | `sub_426CD0` |
| *(consensus)* `-prec-sqrt` | `-prec-sqrt=N` | *(not forwarded)* | `sub_426CD0` |
| *(consensus)* `-fma` | `-fma=N` | *(not forwarded)* | `sub_426CD0` |
| *(host ELF analysis)* | `-host-ref-{ek,ik,ec,ic,eg,ig}=VAL` | *(not forwarded)* | `sub_4BC6F0` |
| *(variables tracking)* | `-variables` | *(not forwarded)* | `sub_4BC6F0` |

Note the `--force-partial-lto` to `--force-device-c` mapping: nvlink's user-facing name describes the linking semantics (partial LTO), while the forwarded cicc name describes the compilation semantics (separate/device compilation).

## Forwarding Data Flow

The complete data flow from CLI to cicc compilation, including the compile-time augmentation:

```
nvlink CLI
   |
   v
nvlink_parse_options (0x427AE0)
   |
   +-- Stores --arch                  -> dword_2A5F314
   +-- Stores --maxrregcount          -> dword_2A5F22C
   +-- Stores --Ofast-compile         -> qword_2A5F258
   +-- Stores -Xnvvm                  -> qword_2A5F230 (linked list)
   +-- Stores -Xptxas                 -> qword_2A5F238 (linked list)
   +-- Stores -g                      -> byte_2A5F310
   +-- Stores --split-compile         -> dword_2A5B518
   +-- Stores --split-compile-extended -> dword_2A5B514
   +-- Stores --device-stack-protector -> byte_2A5F1FF (specified), byte_2A5F1FE (value)
   +-- Stores --device-stack-protector-frame-size-threshold -> byte_2A5F1FC, dword_2A5F1F8
   +-- Stores --device-c / --force-device-c -> byte_2A5F286 / byte_2A5F285
   +-- Stores --cuda-api-version      -> qword_2A5F218
   +-- Stores --use-host-info         -> byte_2A5F213, byte_2A5F214
   |
   v
extract_and_process_fatbin (0x42AF40)  [per input file]
   |
   +-- Updates consensus states:
   |     dword_2A5F270/dword_2A5F274 (ftz state/value)
   |     dword_2A5F26C/dword_2A5B524 (prec-div state/value)
   |     dword_2A5F268/dword_2A5B520 (prec-sqrt state/value)
   |     dword_2A5F264/dword_2A5B51C (fmad state/value)
   |     dword_2A5F250/dword_2A5F254 (maxreg state/value)
   |     dword_2A5F260/dword_2A5B518 (split-compile state/value)
   |     dword_2A5F248/byte_2A5F24C  (generate-line-info state/value)
   |     dword_2A5F240/byte_2A5F244  (inline-info state/value)
   |
   v
sub_426CD0 -- build cicc option list
   |
   +-- Reads all globals above
   +-- Produces char** option_array, int option_count
   |
   v
sub_429BA0 -- build ptxas option string
   |
   +-- Reads -Xptxas, maxrregcount, cuda-api-version,
   |   Ofast-compile, device-stack-protector, split-compile
   +-- Produces char* space-separated option string
   |
   v
nvvm_compile_and_extract (0x4BC6F0)
   |
   +-- Copies option_array into (option_count + 8) array
   +-- Scans for --force-device-c in existing options
   +-- If elfw[97] set and no --force-device-c:
   |     appends -host-ref-{ek,ik,ec,ic,eg,ig}= options
   +-- If elfw[98] set: appends -variables
   +-- Calls nvvmCompileProgram(program, final_count, final_array)
   +-- Passes ptxas options through separate channel
```

## Option Name Mapping Summary

Several options have different names at different stages of the forwarding pipeline:

| nvlink CLI | Fatbin Embedded | Forwarded to cicc | Forwarded to ptxas |
|---|---|---|---|
| `--maxrregcount` | `-maxreg N` (space) | `-maxreg=N` (equals) | `-maxrregcount=N` |
| `--Ofast-compile` / `-Ofc` | *(not embedded)* | `-Ofast-compile=LEVEL` | `--Ofast-compile=LEVEL` |
| `--link-time-opt` / `-lto` | *(not embedded)* | `-link-lto` | *(not forwarded)* |
| `--split-compile` | `-split-compile N` (space) | `-split-compile=N` (equals) | `-split-compile=N` |
| `--split-compile-extended` | *(not embedded)* | `-split-compile-extended=N` | *(not forwarded)* |
| `-g` / `--debug` | *(not embedded)* | `-g` | *(not forwarded)* |
| `--device-c` | *(not embedded)* | `--device-c` | *(not forwarded)* |
| `--force-partial-lto` | *(not embedded)* | `--force-device-c` | *(not forwarded)* |
| *(consensus)* | `-fmad=N` | `-fma=N` | *(not forwarded)* |
| *(consensus)* | `-prec_div=N` (underscore) | `-prec-div=N` (hyphen) | *(not forwarded)* |
| *(consensus)* | `-prec_sqrt=N` (underscore) | `-prec-sqrt=N` (hyphen) | *(not forwarded)* |
| `--device-stack-protector` | *(not embedded)* | *(not forwarded)* | `--device-stack-protector=true/false` |
| `--device-stack-protector-frame-size-threshold` | *(not embedded)* | *(not forwarded)* | `--device-stack-protector-frame-size-threshold=N` |
| `--cuda-api-version` | *(not embedded)* | *(not forwarded)* | `-cuda-api-version=VER` |
| `--use-host-info` | *(not embedded)* | `-has-global-host-info` | *(not forwarded)* |
| *(host ELF)* | *(not embedded)* | `-host-ref-{ek,ik,ec,ic,eg,ig}=` | *(not forwarded)* |
| *(variables)* | *(not embedded)* | `-variables` | *(not forwarded)* |

## Decompilation Notes

1. The SSE constant loads (`_mm_load_si128` of `xmmword_1D34730` and `xmmword_1D34740`) are the decompiler's representation of 16-byte string copies. The strings at those addresses are `-generate-line-info` (20 bytes including terminator, 16 via SSE + 4 via dword) and `--force-device-c` (17 bytes, 16 via SSE + 1 null byte).

2. `sub_426AA0` is the arena-backed string allocator -- it calls `sub_4307C0` (arena_alloc) with the requested size. Every option string is allocated this way and never freed; the arena is destroyed after the entire LTO pipeline completes.

3. `sub_464C30` appends a value to the end of a linked list created by `sub_464AE0`. `sub_464BB0` returns the list length. `sub_464BC0` converts the list into a flat array (allocated from the arena) and returns the array pointer.

4. `sub_4649E0` reverses a linked list. This is used to correct the LIFO insertion order of tokenized `-Xnvvm` strings back to the original CLI order.

5. The `-prec-sqrt` unconditional emission (outside the `seen_prec` guard) may be intentional to ensure cicc always receives an explicit precision setting for square root, even if the user provided one through `-Xnvvm`. Alternatively, this may be a subtle bug where `-prec-sqrt` should be gated by its own `seen_sqrt` flag rather than sharing the `seen_prec` flag with `-prec-div`.

6. In `sub_4BC6F0`, the `-variables` string is loaded via `_mm_load_si128(&xmmword_1D48A60)` into a stack-local `si128` variable, then a pointer to `si128` is stored into the option array. This means the string lives on the stack frame of `sub_4BC6F0` and remains valid through the `nvvmCompileProgram` call (which is synchronous).

7. The `sub_426AE0` function (host-info processing) inspects each module node's structure at offsets +24 (has LTO info), +25 (has host-ref data), and +26 (skip flag). It specifically excludes modules whose name contains `"cudadevrt"` from the host-ref processing. If all non-cudadevrt modules have valid host-ref data, `byte_2A5F211` is set to 1; otherwise `byte_2A5F212` is set to 1 (forcing ignore-host-info).

8. The string builder functions used in `sub_429BA0` for `-Xptxas` concatenation: `sub_44FB20` creates a buffer with the given initial capacity (128 bytes), `sub_4504A0` checks if the buffer length is non-zero, `sub_44FF90` appends a single byte (the space separator, ASCII 32), `sub_44FE60` appends a C string, and `sub_44FDC0` extracts the final null-terminated C string.

## Cross-References

- [LTO Overview](overview.md) -- high-level pipeline context; the option vector feeds into Step 2 (libnvvm compilation)
- [libnvvm Integration](libnvvm-integration.md) -- `sub_4BC6F0` passes the assembled option vector to `nvvmCompileProgram`; documents host-ref option construction and `-variables` flag
- [Split Compilation](split-compilation.md) -- `-split-compile` and `-split-compile-extended` forwarding logic
- [Whole vs Partial LTO](whole-vs-partial.md) -- `--force-partial-lto` maps to `--force-device-c` when forwarded
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- `-has-global-host-info` option appended when DCE is active; `sub_426AE0` host-info processing
- [Fatbin Extraction](../input/fatbin-extraction.md) -- `sub_42AF40` consensus tracking for per-module math-mode options
- [CLI Flags](../config/cli-flags.md) -- nvlink's own CLI option definitions and global variable mapping
- [Embedded ptxas Options](../config/ptxas-options.md) -- the consumer of the ptxas option string; documents all ~160 ptxas options

### Sibling Wiki

- **cicc wiki**: [CLI Flag Inventory](../../../../cicc/wiki/src/config/cli-flags.md) -- the consumer of these forwarded options. Documents how cicc processes `-arch=compute_N`, `-link-lto`, `-maxreg`, `-split-compile`, `-ftz`, `-prec-div`, `-prec-sqrt`, `-fma`, `-g`, `-generate-line-info`, `-inline-info`, `--device-c`, `--force-device-c`, and `-host-ref-*` flags, routing each to the lnk/opt/llc/lto output vectors.
- **cicc wiki**: [LTO & Module Optimization](../../../../cicc/wiki/src/lto/index.md) -- how cicc processes `-link-lto` and the LTO pass pipeline activated by the forwarded option set
- **ptxas wiki**: [CLI Options](../../../../ptxas/wiki/src/config/cli-options.md) -- the consumer of the ptxas option string. Documents `-maxrregcount`, `--Ofast-compile`, `-split-compile`, `--device-stack-protector`, `--device-stack-protector-frame-size-threshold`, and `-cuda-api-version`.

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| cicc option list emission order and conditions in `sub_426CD0` | HIGH | Fully decompiled function, all 275 lines traced branch-by-branch |
| ptxas option string assembly in `sub_429BA0` | HIGH | Fully decompiled function, all 306 lines traced |
| 5-state consensus machine (states 0-4) | HIGH | All 8 option tracking blocks in `sub_42AF40` lines 283-512 verified with identical state transition patterns |
| Name translations (fmad->fma, prec_div->prec-div, space->equals) | HIGH | Directly visible comparing `strstr` search strings in `sub_42AF40` against `snprintf` format strings in `sub_426CD0` |
| Host-ref option construction in `sub_4BC6F0` | HIGH | Decompiled function, lines 213-381 show six host-ref prefix constructions |
| `-variables` flag append in `sub_4BC6F0` | HIGH | Lines 383-389, SSE constant load of string at `xmmword_1D48A60` |
| `-prec-sqrt` unconditional emission | HIGH | Structural position in decompiled code: line 258 is outside any conditional block, while lines 244, 251, 262 are inside guards |
| StringBuilder mechanism for `-Xptxas` concatenation | MEDIUM | Function semantics inferred from call pattern (create, check-empty, append-char, append-string, extract); individual function bodies not decompiled |
| `--device-stack-protector-frame-size-threshold` separate delivery channel | MEDIUM | Buffer `v12` is freed separately and not included in the 6-component snprintf; exact delivery mechanism to ptxas not traced |
| `sub_426AE0` host-info processing logic | MEDIUM | Decompiled at 113 lines; module descriptor field offsets inferred from dereference patterns |
