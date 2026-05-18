# Diagnostics & Optimization Remarks

CICC v13.0 contains three independent diagnostic systems that operate at different phases of compilation and serve different audiences. The EDG frontend diagnostic engine handles C++/CUDA language-level errors and warnings with rich terminal formatting or SARIF JSON output. The LLVM optimization remark infrastructure reports pass-level decisions (what was optimized, what was missed, and why) through the standard `DiagnosticInfo` hierarchy. NVIDIA's custom "profuse" framework provides verbose per-pass diagnostic output that is entirely separate from both EDG diagnostics and LLVM remarks, controlled by dedicated knobs like `profuseinline` and `profusegvn`.

Understanding these three layers is essential for reimplementation because they share no code. EDG diagnostics live in the `0x670000`-`0x6FFFFF` address range and operate on EDG's internal diagnostic record format. LLVM remarks use the stock `OptimizationRemarkEmitter` analysis pass and the `DiagnosticInfoOptimizationBase` class hierarchy. The profuse framework is a pure NVIDIA invention that writes directly to stderr through `cl::opt<bool>` guards with no connection to either of the other two systems.

| | |
|---|---|
| **EDG terminal emitter** | `sub_681D20` (37KB, 1,342 lines) at `0x681D20` |
| **EDG dispatch/SARIF emitter** | `sub_6837D0` (20KB) at `0x6837D0` |
| **Diagnostic format selector** | `unk_4D04198`: 0 = text, 1 = SARIF |
| **Format CLI flag** | `--diagnostics_format=text\|sarif` (case 0x125 in `sub_617BD0`) |
| **EDG output mode CLI** | `--output_mode text\|sarif` (case 293 in lgenfe_main) |
| **LLVM remark registration** | `ctor_152` at `0x4CE3F0` (3 regex `cl::opt`s) |
| **LLVM remark YAML serializer** | `sub_15CAD70` (13KB) at `0x15CAD70` |
| **LLVM remark bitstream serializer** | `sub_F01350` (23KB) at `0xF01350` |
| **Profuse inlining knob** | `profuseinline` at `0x4DBEC0` (ctor\_186\_0), default off |
| **Profuse GVN knob** | `profusegvn` at `0x4FAE7E0` (ctor\_201), default true |
| **Diagnostic output stream** | `qword_4F07510` (FILE\*, typically stderr) |
| **Terminal width** | `dword_4D039D0` (columns, for word-wrapping) |
| **ANSI color enable** | `dword_4F073CC[0]` (nonzero = enabled) |
| **Upstream LLVM equivalent** | `llvm/include/llvm/IR/DiagnosticInfo.h`, `llvm/lib/Analysis/OptimizationRemarkEmitter.cpp` |

## EDG Frontend Diagnostics

### Dispatch Architecture

Every EDG frontend diagnostic passes through `sub_6837D0`, which acts as the single dispatch point. This function performs filtering (severity threshold, duplicate suppression, pragma-based suppression), increments error/warning counters, and then routes to one of two renderers based on the global `unk_4D04198`:

```
sub_6837D0(diag_record)
  |
  +-- severity < byte_4F07481[0]?  --> suppress (return)
  +-- duplicate? (byte_4CFFE80[4*errnum+2] bit flags) --> count only
  +-- pragma disabled? (sub_67D520) --> suppress
  +-- error limit reached? (unk_4F074B0 + unk_4F074B8 >= unk_4F07478) --> error 1508, abort
  |
  +-- unk_4D04198 == 0  -->  sub_681D20(diag)   [terminal text renderer]
  +-- unk_4D04198 == 1  -->  inline SARIF JSON   [JSON renderer within sub_6837D0]
```

The format is selected by the `--diagnostics_format` flag (case 0x125 in `sub_617BD0`), which is surfaced as `--output_mode text|sarif` in the lgenfe CLI.

### Diagnostic Record Layout

EDG diagnostic records are approximately 192-byte structures organized as a tree. Each record can have child diagnostics, notes, context diagnostics (include-stack annotations), and an extra child list, all stored as linked lists.

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| +0 | 4 | `type` | 0 = top-level, 1 = unknown, 2 = child-with-parent, 3 = continuation |
| +8 | 8 | `next_sibling` | Linked list next pointer |
| +16 | 8 | `parent_diag` | Pointer to parent diagnostic node |
| +24 | 8 | `child_list` | Linked list of child diagnostics |
| +40 | 8 | `extra_child_list` | Secondary child list (always emitted) |
| +56 | 8 | `note_list` | Linked list of attached notes |
| +72 | 8 | `context_list` | Context diagnostics (include-stack annotations) |
| +96 | 4 | `has_source_location` | Nonzero if source info is present |
| +100 | 2 | `column_number` | Column in source line (unsigned short) |
| +120 | 8 | `source_file_info` | Passed to `sub_723260` to get filename string |
| +128 | 4 | `line_number` | Source line number (unsigned int) |
| +136 | 4 | `file_id` | File table index (0 = no file) |
| +140 | 2 | `column_end` | End column for underlining range |
| +144 | 4 | `is_command_line` | Nonzero means "command line" prefix |
| +152 | 8 | `source_entity` | If nonzero, use `sub_723640` for decorated location |
| +160 | 8 | `display_name_ptr` | Filename string pointer |
| +168 | 4 | `display_line` | Line number for display |
| +172 | 4 | `tab_stop_width` | Tab stop setting for source display |
| +176 | 4 | `diagnostic_number` | Numeric ID for `-W` flags, becomes SARIF ruleId |
| +180 | 1 | `severity` | Severity code (see severity enum below) |

### Terminal Text Renderer (`sub_681D20`)

The 37KB terminal renderer is the larger and more complex of the two backends. It handles ANSI color output, word-wrapping to terminal width, source context display with caret underlining, and recursive child diagnostic emission.

**Location prefix.** The source location is formatted before the severity label. For file-based diagnostics, `sub_722FC0` or `sub_723640` produces the filename, followed by `(line_number)` in parentheses, wrapped in ANSI color code 5 (file path color). Command-line diagnostics use the localized string at ID 1490 (rendered as `"command line argument"` in the English locale; the bare `"command line"` literal is not present in the string table). Diagnostics with no file have no location prefix.

**Severity label.** The label string is looked up via `sub_67C860(string_id)` from a localized string table. The string table base `v57` is offset by 0 for normal diagnostics, 1 for command-line diagnostics. When diagnostic numbering is enabled (`unk_4D04728` set) and severity is 5 or below with a nonzero diagnostic number at +176, the renderer appends ` #<number>` after the severity label, converted by `sub_67D2D0`.

**ANSI color system.** CICC does not emit standard ANSI escape sequences directly. Instead, it uses an internal 2-byte marker system where byte 0 is `0x1B` (ESC) and byte 1 is a color code from 1 to 5. These internal markers are translated to real terminal escapes by the output layer.

| Internal Code | Semantic | Typical Terminal Mapping |
|---------------|----------|--------------------------|
| 1 | Reset/default | `\033[0m` |
| 2 | Error | Red |
| 3 | Caution/severe-warning | Yellow/magenta |
| 4 | Location highlight | Bold/cyan |
| 5 | File path / remark | Dim/blue |

Color output is gated by `dword_4F073CC[0]` (nonzero = enabled) and `dword_4F073C8` (nonzero = "rich" escape mode; zero = "simple" mode that skips escape bytes entirely).

**Word-wrapping.** Two code paths exist depending on whether ANSI colors are active.

Without colors (Path A), the algorithm is straightforward: compute available width as `dword_4D039D0 - left_margin`, scan for the last space within that width, break there, and emit newline plus indent. The left margin and continuation indent depend on the diagnostic type:

| Type (+0) | Left Margin | Continuation Indent |
|-----------|-------------|---------------------|
| 0 (top-level) | 0 | 10 |
| 1 | 12 | 22 |
| 2 (child) | 10 or 12 | 20 or 22 |
| 3 (continuation) | 1 | 11 |

For type 2, the margin is +2 if the current diagnostic is not the first child of its parent.

With colors (Path B), the algorithm tracks character-by-character with color state (`v40` = current color, `v41` = at-start-of-line flag, `v152` = remaining columns). On encountering an ESC marker, it consumes the 2-byte pair and updates color state via `sub_67BBF0`. When the column limit is hit, the algorithm attempts to break at the last recorded space position (with buffer rewind to `v147`), falling back to a forced break at the current position.

The global `qword_4F07468` controls wrap behavior: the low 32 bits disable wrapping entirely when nonzero, and the high 32 bits suppress source context display when nonzero.

**Source context display.** After the message text, the renderer displays the source line with caret underlining. `sub_729B10(file_id, ...)` retrieves source line data. Each source position entry is a linked list node with a 24+ byte layout: +0 next pointer, +8 source text pointer, +16 entry type (0 = normal char, 1 = same-position, 2 = 2-byte char, 3 = tab), +24 replacement character. The display renders two lines: the source text and a caret/tilde underline line, where `^` marks the error column and `~` extends the range to `column_end`. Multi-byte character handling uses `sub_721AB0` to determine byte counts.

**Recursive emission.** After the main diagnostic and source context, child diagnostics are emitted recursively in this order: child\_list (+24), note\_list (+56, skipped for severity 2 remarks), context\_list (+72, with parent pointer set before recursion), extra\_child\_list (+40). After all children, a blank line separator is emitted (unless compact mode is active), the output buffer is null-terminated, and the result is written via `fputs` to `qword_4F07510` followed by `fflush`.

**Machine-readable log.** When `qword_4D04908` (log FILE\*) is set and the diagnostic type is not 3 (continuation), the renderer writes a single-line record:

```
<severity-char> "<filename>" <line> <col> <message>\n
```

The severity character is indexed from the string `"rwweeccccCli"` by `(severity - 4)`. For child diagnostics, the character is lowercased.

| Index | Character | Meaning |
|-------|-----------|---------|
| 0 (sev 4) | r | remark |
| 1 (sev 5) | w | warning |
| 2 (sev 6) | w | caution (displayed as warning) |
| 3 (sev 7) | e | error |
| 4 (sev 8) | e | error (promoted) |
| 5 (sev 9) | c | catastrophe |
| 6 (sev 10) | c | catastrophe |
| 7 (sev 11) | C | catastrophe (alternate) |
| 8 | l | unknown |
| 9 | i | internal error |

### SARIF JSON Renderer

The SARIF backend is implemented inline within `sub_6837D0`. Rather than emitting a complete SARIF document (no `$schema`, no `runs[]` envelope), it writes one JSON object per diagnostic as a comma-separated stream to `qword_4F07510`. The caller or a post-processing tool is expected to wrap the stream.

Each diagnostic object has this structure:

```json
{
  "ruleId": "EC<number>",
  "level": "error"|"warning"|"remark"|"catastrophe"|"internal_error",
  "message": {"text": "<JSON-escaped message>"},
  "locations": [
    {
      "physicalLocation": {
        "artifactLocation": {"uri": "file://<path>"},
        "region": {"startLine": N, "startColumn": N}
      }
    }
  ],
  "relatedLocations": [
    {
      "message": {"text": "..."},
      "physicalLocation": { ... }
    }
  ]
}
```

The `ruleId` is formed by emitting the literal `"EC"` prefix followed by the decimal diagnostic number (the unsigned word at `diag+176`) formatted into the output buffer; no standalone `"EC%lu"` printf format string survives in the rodata section, so the prefix and the integer are appended through two separate buffer writes. The `level` string is mapped from the severity byte at +180 via a switch statement that yields exactly five SARIF tokens present in the binary: `"remark"`, `"warning"`, `"error"`, `"catastrophe"`, `"internal_error"`. The `message.text` is produced by `sub_683690`, which renders the diagnostic text into `qword_4D039E8` via `sub_681B50` and then copies it character-by-character into `qword_4D039D8` with JSON escaping of `"` and `\` characters. The `locations` array is present only when `*(diag+136) != 0` (valid file ID). The `physicalLocation` is built by `sub_67C120`, which calls `sub_729E00` to decompose the packed source position and `sub_722DF0` to resolve the file ID to a path string. The `relatedLocations` array carries note sub-diagnostics from the linked list at `diag+72`.

Multiple diagnostics are comma-separated: a comma is prepended before `{` when `unk_4F074B0 + unk_4F074B8 > 1` (more than one diagnostic emitted so far).

**Include-stack annotations.** When include depth (`dword_4F04C64`) is greater than zero, `sub_6837D0` walks the include stack (776-byte records at `qword_4F04C68`) calling `sub_67B7E0` to build context annotations. These are linked as children at `diag+40/+48`. The English-locale templates that survive in the string table are `"detected during:"` and the family of `"detected during instantiation of %nt %p"` / `"detected during implicit generation of %nt %p"` / `"detected during processing of template argument list for %na %p"` strings -- EDG uses the C++ instantiation-context vocabulary, not C-preprocessor `"in file included from"` phrasing. Diagnostic numbers 453, 1063, 1064, and 1150 select which of these templates renders for the current include-stack frame.

**Warning-as-error promotion.** When a warning (severity 5) has been emitted and `unk_4D04728` is set, the function creates a synthetic "warnings treated as errors" diagnostic via `sub_67D610(0xE7D, ..., 4)` with severity 4 (remark), then recursively calls `sub_6837D0` on it.

### Diagnostic Filtering and Suppression

Filtering happens in `sub_6837D0` before either renderer is invoked:

1. **Severity threshold**: `byte_4F07481[0]` stores the minimum severity. Diagnostics below this level are silently suppressed.
2. **Duplicate detection**: `byte_4CFFE80[4*errnum + 2]` bit flags track "already seen" diagnostics. Bit 0 marks first occurrence, bit 1 marks already emitted. On second hit, the diagnostic is counted but not emitted.
3. **Pragma suppression**: `sub_67D520` checks whether the diagnostic is disabled via `#pragma nv_diag_suppress` (the spelling that survives in the string table -- the legacy `diag_suppress` form is accepted as an alias by the EDG pragma parser but is not the literal stored alongside the dispatcher). `sub_67D470` records the suppression.
4. **Error limit**: When `unk_4F074B0 + unk_4F074B8 >= unk_4F07478`, error 1508 ("error limit reached") is emitted and `sub_7235F0(9)` aborts compilation.

## Diagnostic Severity Enum

The severity byte at `diag+180` encodes the following levels, used by both the terminal and SARIF renderers:

| Value | Name | Terminal Color | SARIF Level | Log Char | Label |
|-------|------|---------------|-------------|----------|-------|
| 2 | remark | ESC 5 (blue) | `"remark"` | R | R |
| 4 | warning | ESC 5 (blue) | `"warning"` | r | W |
| 5 | caution | ESC 3 (yellow) | `"warning"` | w | W (lowercase) |
| 6 | severe-warning | ESC 3 (yellow) | (falls through to error) | w | E (lowercase) |
| 7 | error | ESC 2 (red) | `"error"` | e | E |
| 8 | error (promoted) | ESC 2 (red) | `"error"` | e | E |
| 9 | catastrophe | ESC 2 (red) | `"catastrophe"` | c | C |
| 10 | catastrophe | ESC 2 (red) | `"catastrophe"` | c | C |
| 11 | internal-error | ESC 2 (red) | `"internal_error"` | i | special |

Severity values 9, 10, and 11 are fatal: after emission, `sub_7AFBD0` (`longjmp` / error propagation `[LOW confidence]` -- the function is called on fatal error paths and does not return to its caller, consistent with `longjmp` or `exit`, but could also be a custom `abort`-style handler; no `setjmp`/`longjmp` string evidence found) and `sub_7235F0(severity)` terminate compilation. Internal errors (11) additionally prepend `"(internal error) "` to the log output and use the prefix for error 3709.

Note: severity 2 (remark) is distinct from LLVM optimization remarks -- it is an EDG frontend remark (e.g., template instantiation notes). Remarks at severity 2 suppress their note\_list children during recursive emission.

> ⚡ **QUIRK — 4,515 EDG diagnostic IDs uncatalogued**
> The string section exposes ~4,515 lowercase identifier-style strings matching the EDG diagnostic-id convention (`abi_tag_ignored`, `abstract_requires_virtual`, `ambig_literal_operator`, `pragma_*`, etc.). The diagnostic record at `dword[+0x14]` selects which of these template strings is rendered, but the wiki currently documents only the dispatch flow, not the catalogue. Confidence: HIGH (raw extraction; the catalogue itself dwarfs every other table in the binary by string count). Useful precursor work for a future "EDG Diagnostic Catalogue" page that would group by phase (`pragma_*`, `ambiguous_*`, `template_*`, `cuda_*`).

## LLVM Optimization Remarks

### Registration and CLI Surface

Three `cl::opt<std::string>` knobs are registered at `ctor_152` (`0x4CE3F0`), each taking a regex pattern:

| Knob | Description | Filters |
|------|-------------|---------|
| `pass-remarks` | Enable optimization remarks from passes whose name matches the pattern | Passed (successful) optimizations |
| `pass-remarks-missed` | Enable missed optimization remarks | Optimizations that were considered but not applied |
| `pass-remarks-analysis` | Enable analysis remarks | Intermediate analysis results and explanations |

These are stock LLVM `cl::opt` registrations. CICC exposes them through the flag catalog (`sub_9624D0`) via the `-inline-info` convenience flag, which routes to the opt phase as:
```
-Xopt -pass-remarks=inline
-Xopt -pass-remarks-missed=inline
-Xopt -pass-remarks-analysis=inline
```

Additional remark-related knobs registered at `ctor_376_0` (`0x512DF0`):

| Knob | Purpose |
|------|---------|
| `pass-remarks-with-hotness` | Include PGO hotness information in remarks |
| `pass-remarks-hotness-threshold` | Minimum hotness for remark emission |
| `pass-remarks-output` | File path for remark output (YAML or bitstream) |
| `pass-remarks-filter` | Additional filter for remark pass names |
| `pass-remarks-format` | Format: `yaml` or `bitstream` |

The `-w` flag (suppress warnings) routes to both opt and llc as `-w`. The `-Werror` flag routes to both as `-Werror`, promoting warnings to errors.

### Remark Emission Protocol

LLVM passes emit remarks through a three-step protocol observed consistently across all analyzed passes:

**Step 1: Construct the remark.** The pass creates a `DiagnosticInfoOptimizationBase` subclass object via one of these constructors:

| Constructor | Address | Creates |
|-------------|---------|---------|
| `sub_B17560` | `0xB17560` | `OptimizationRemark` (pass succeeded) |
| `sub_15CA330` | `0x15CA330` | `OptimizationRemark` (alternative constructor) |
| `sub_15CA540` | `0x15CA540` | `OptimizationRemarkMissed` (pass failed/skipped) |
| `sub_B178C0` | `0xB178C0` | Warning-level `DiagnosticInfo` (non-remark warning) |

The constructor takes a pass name string (e.g., `"coro-split"`, `"wholeprogramdevirt"`, `"loop-distribute"`) and a remark ID string (e.g., `"Devirtualized"`, `"Distribute"`, `"CoroSplit"`).

**Step 2: Build the message.** The message is assembled through a builder pattern:

| Builder Function | Address | Purpose |
|-----------------|---------|---------|
| `sub_B18290` | `0xB18290` | Append raw string to remark message |
| `sub_B16430` | `0xB16430` | Create named string attribute (e.g., `"FunctionName"`) |
| `sub_B16B10` | `0xB16B10` | Create named integer attribute (e.g., `"frame_size"`) |
| `sub_B16530` | `0xB16530` | Append named value (used in analysis remarks) |
| `sub_B180C0` | `0xB180C0` | Finalize and prepare remark for emission |

A typical emission sequence (from CoroSplit at `0x24F05D1`):

```
call sub_B17560("coro-split", "CoroSplit")      // create remark
call sub_B18290("Split '")                       // append prefix
call sub_B16430("function", fn_name)             // named attribute
call sub_B18290("' (frame_size=")                // literal text
call sub_B16B10("frame_size", N)                 // integer attribute
call sub_B18290(", align=")                      // literal text
call sub_B16B10("align", M)                      // integer attribute
call sub_B18290(")")                             // closing paren
```

Resulting remark text: `Split '<function_name>' (frame_size=N, align=M)`

**Step 3: Publish.** `sub_1049740` publishes the remark to the diagnostic handler registered on the `LLVMContext`. The handler consults the `pass-remarks` / `pass-remarks-missed` / `pass-remarks-analysis` regex filters to decide whether to emit or suppress the remark.

After emission, remark objects are cleaned up: vtable-based destructors free the remark structure, and SSO string cleanup checks whether each temporary string pointer differs from its inline buffer address (indicating heap allocation that needs `free`).

### Remark Categories

Standard LLVM categories:

| Category | YAML Tag | Meaning |
|----------|----------|---------|
| Passed | `!Passed` | Optimization was successfully applied |
| Missed | `!Missed` | Optimization was considered but not applied |
| Analysis | `!Analysis` | Intermediate analysis information |
| Failure | `!Failure` | Internal failure during optimization |

NVIDIA-specific categories added to the remark framework:

| Category | YAML Tag | Purpose |
|----------|----------|---------|
| AnalysisFPCommute | `!AnalysisFPCommute` | GPU floating-point commutativity analysis feedback |
| AnalysisAliasing | `!AnalysisAliasing` | GPU memory aliasing analysis feedback |

These NVIDIA-specific categories are registered in the YAML serializer at `sub_15CAD70` and the YAML parser at `sub_C30A00`.

### Serialization Backends

**YAML serializer** (`sub_15CAD70`, 13KB at `0x15CAD70`): Emits structured YAML with fields `Pass`, `Name`, `DebugLoc`, and the remark type tag. Uses a vtable-based streaming API at offsets +96 (`writeKey`), +120 (`beginMapping`), +128 (`endMapping`).

**Bitstream serializer** (`sub_F01350`, 23KB at `0xF01350`): Emits remarks in LLVM's binary bitstream format (used for `-fsave-optimization-record`). Record types include "Remark", "Remark header", "Remark debug location", "Remark hotness", "Argument with debug location", and "Argument". Uses `sub_EFD2C0` for VBR-encoded record emission and `sub_EFCCF0` for abbreviation definitions.

**Remark serializer factory** (`sub_C2E790`, 6KB at `0xC2E790`): `llvm::remarks::createRemarkSerializer` dispatches to YAML or bitstream format based on configuration. Two distinct unknown-format messages are emitted at different layers: `"Unknown remark format: '%s'"` from the format-string parser (when the user-supplied `-pass-remarks-format` value does not match a known token) and `"Unknown remark serializer format."` from the factory itself (when the parsed enum reaches the dispatcher with no matching case). Both literals are present in rodata.

### OptimizationRemarkEmitter Analysis

Two analysis passes provide remark emission capability to function-level and machine-function-level passes:

| Pass | Pipeline Name | Level |
|------|---------------|-------|
| `OptimizationRemarkEmitterAnalysis` | `"opt-remark-emit"` (pipeline ID 181) | Function analysis |
| `MachineOptimizationRemarkEmitterAnalysis` | `"machine-opt-remark-emitter"` (pipeline ID 467) | MachineFunction analysis |

Passes that emit remarks must request the appropriate analysis and store the resulting `OptimizationRemarkEmitter*`. For example, the TwoAddressInstruction pass stores it at `this+272`, obtained via analysis lookup `unk_4FC4534`.

### Passes Known to Emit Remarks

This is a non-exhaustive list of passes observed emitting optimization remarks in the binary:

| Pass | Remark Name | Remark Examples |
|------|-------------|-----------------|
| CoroSplit | `"coro-split"` | `Split '<fn>' (frame_size=N, align=M)` |
| WholeProgramDevirt | `"wholeprogramdevirt"` | `Devirtualized '<fn>'` |
| LoopDistribute | `"loop-distribute"` | `Distribute`, `NoUnsafeDeps`, `TooManySCEVRuntimeChecks` |
| LoopVectorize | `"loop-vectorize"` | Vectorization success/failure details |
| LoopUnroll | `"loop-unroll"` | Unroll factor and failure reasons |
| LoopInterchange | `"loop-interchange"` | `Cannot interchange loops...` |
| LICM | `"licm"` | Hoist success/failure reasons |
| SLPVectorizer | `"slp-vectorizer"` | SLP vectorization decisions |
| MachinePipeliner | `"pipeliner"` | `Pipelined succesfully!` [sic] |
| MachineOutliner | `"machine-outliner"` | Outlining decisions |
| OpenMP SPMD Transform | `"openmp-opt"` | OMP120 (remark), OMP121 (warning) |
| InstCombine | `"instcombine"` | Visit decisions (via `instcombine-visit` filter) |
| FastISel | `"fastisel"` | FastISel failure reports |
| IRCE | `"irce"` | Range check elimination decisions |
| TwoAddressInstruction | `"twoaddressinstruction"` | Two-address conversion decisions |

## NVIDIA Profuse Framework

### Design and Purpose

The "profuse" diagnostic framework is an NVIDIA-specific verbose output system that has no connection to the LLVM `OptimizationRemark` infrastructure. It predates LLVM's remark system and serves a different purpose: providing NVIDIA compiler engineers with extremely detailed, unstructured diagnostic output from specific optimization passes.

The name "profuse" is unfortunately overloaded in the cicc binary. Two completely unrelated systems use the word:
- **PGO profuse**: The `profuse` knob registered at `ctor_375` (`0x512720`) is a boolean that enables profile-guided optimization data consumption. It is set via `-profile-instr-use <file>` which routes to `-Xopt -profuse=true -Xopt -proffile=<file>`. This is a PGO control flag, not a diagnostic system.
- **Diagnostic profuse**: The `profuseinline` and `profusegvn` knobs are NVIDIA diagnostic toggles that control verbose output from specific optimization passes. These are the "profuse framework" discussed here.

### `profuseinline`

Registered at `ctor_186_0` (`0x4DBEC0`) as a `cl::opt<bool>` with default value **off** (false).

When enabled, the NVIDIA custom inliner (`sub_1864060`, the `shouldInline` / inline cost computation) emits verbose diagnostic output for every inlining decision. This includes the computed cost, threshold comparison, argument type-size coercion details, and the final accept/reject decision.

The profuse inlining output goes directly to stderr through fprintf-style calls within the inliner code. It is not routed through `OptimizationRemarkEmitter` and does not appear in remark YAML/bitstream output. This is distinct from the LLVM `inline-remark-attribute` knob which annotates the IR with remark metadata.

The `-inline-info` CLI flag does **not** enable `profuseinline`. Instead, `-inline-info` routes to the three standard `pass-remarks` knobs filtered for "inline". To enable profuse output, one must pass `-Xopt -profuseinline=true` (or `-Xcicc -opt -profuseinline=true` through nvcc).

Comparison of the two diagnostic channels for inlining:

| Feature | profuseinline | -inline-info (pass-remarks) |
|---------|--------------|----------------------------|
| Output format | Unstructured stderr text | Structured LLVM remark |
| Controlled by | `cl::opt<bool>` | Regex filter on pass name |
| Default | Off | Off |
| YAML/bitstream output | No | Yes (if `-pass-remarks-output` set) |
| Cost model details | Yes (full cost breakdown) | No (accept/reject only) |
| NVIDIA-specific metrics | Yes (GPU opcode bonus, struct analysis) | No |

### `profusegvn`

Registered at `ctor_201` (`0x4E0990`) as a `cl::opt<bool>` with default value **true** (enabled). Global address: `0x4FAE7E0`. Description: `"profuse for GVN"`.

When the knob is active (which it is by default), the GVN pass (`sub_1900BB0`, 83KB) emits verbose diagnostic output at the following decision points:
- Value replacement decisions (when a leader is found in the value numbering table)
- Store/load expression hash table matches
- PRE (Partial Redundancy Elimination) insertion decisions

The output is written directly to stderr, bypassing the LLVM remark system entirely. The profuse GVN output is not captured by `-pass-remarks-output` and does not appear in remark YAML or bitstream files.

To disable the verbose output, pass `-Xopt -profusegvn=false`. The fact that this defaults to **true** (unlike `profuseinline` which defaults to false) suggests it may be gated by an additional runtime check (possibly wizard mode or an optimization level gate) to prevent user-visible noise in release builds.

### Profuse vs. LLVM Remarks Summary

| Aspect | Profuse Framework | LLVM Optimization Remarks |
|--------|-------------------|---------------------------|
| Origin | NVIDIA custom | Upstream LLVM |
| Passes | Inliner, GVN only (observed) | Most optimization passes |
| Output | Raw stderr fprintf | Structured DiagnosticInfo |
| Format | Unstructured text | YAML, bitstream, or terminal |
| Filtering | Per-knob boolean | Regex on pass name |
| Serialization | None | YAML and bitstream serializers |
| IDE integration | None | SARIF (with post-processing) |
| Default | Off (inline) / On (GVN) | Off (requires `-pass-remarks`) |

## Filtering and Configuration

### CLI Flags for Diagnostic Control

**EDG frontend diagnostics (Phase I):**

| Flag | Route | Effect |
|------|-------|--------|
| `--diagnostics_format=sarif` | EDG direct | Switch output to SARIF JSON |
| `--output_mode text\|sarif` | EDG direct (case 293) | Same as above, alternative spelling |
| `-w` | opt `-w`, llc `-w` | Suppress all warnings |
| `-Werror` | opt `-Werror`, llc `-Werror` | Promote warnings to errors |
| `--error_limit N` | EDG direct | Maximum errors before abort (`unk_4F07478`) |
| `#pragma diag_suppress N` | EDG source | Suppress specific diagnostic by number |

**LLVM optimization remarks (Phase II / opt):**

| Flag | Route | Effect |
|------|-------|--------|
| `-inline-info` | opt: `-pass-remarks=inline`, `-pass-remarks-missed=inline`, `-pass-remarks-analysis=inline` | Enable inline-specific remarks |
| `-Xopt -pass-remarks=<regex>` | opt direct | Enable passed remarks matching pattern |
| `-Xopt -pass-remarks-missed=<regex>` | opt direct | Enable missed remarks matching pattern |
| `-Xopt -pass-remarks-analysis=<regex>` | opt direct | Enable analysis remarks matching pattern |
| `-Xopt -pass-remarks-output=<file>` | opt direct | Write remarks to file (YAML or bitstream) |
| `-Xopt -pass-remarks-format=yaml\|bitstream` | opt direct | Select output format |
| `-Xopt -pass-remarks-with-hotness` | opt direct | Include PGO hotness in remarks |
| `-Xopt -pass-remarks-hotness-threshold=N` | opt direct | Minimum hotness for emission |
| `-Xopt -pass-remarks-filter=<regex>` | opt direct | Additional pass name filter |

**NVIDIA profuse diagnostics:**

| Flag | Route | Effect |
|------|-------|--------|
| `-Xopt -profuseinline=true` | opt direct | Enable verbose inlining diagnostics |
| `-Xopt -profusegvn=false` | opt direct | Disable verbose GVN diagnostics (on by default) |

**Debug and verbose output:**

| Flag | Route | Effect |
|------|-------|--------|
| `-enable-verbose-asm` | llc `-asm-verbose` | Verbose assembly comments |
| `-show-src` | llc `-nvptx-emit-src` | Embed source in PTX output |
| `-time-passes` | special (must be only flag) | Time each LLVM pass |

### Global Variables Controlling Diagnostic Behavior

| Address | Type | Name | Purpose |
|---------|------|------|---------|
| `unk_4D04198` | int | `diagnostic_format` | 0 = text, 1 = SARIF |
| `byte_4F07481[0]` | byte | `min_severity_threshold` | Minimum severity for emission |
| `unk_4F074B0` | uint | `error_count` | Running error counter |
| `unk_4F074B8` | uint | `warning_count` | Running warning/non-error counter |
| `unk_4F07478` | uint | `error_limit` | Maximum errors before abort |
| `unk_4F07490` | flag | `print_counters` | Whether to print summary counters |
| `unk_4D04728` | byte | `diag_numbering` | Diagnostic numbering enabled |
| `unk_4D042B0` | byte | `command_line_mode` | Command-line diagnostic prefix |
| `unk_4D042B8` | flag | `werror_flag` | Promote severity to 7 for warnings |
| `dword_4D039D0` | int | `terminal_width` | Columns for word-wrapping |
| `dword_4F073CC[0]` | int | `ansi_color_enabled` | ANSI color output flag |
| `dword_4F073C8` | int | `rich_escape_mode` | Rich (2-byte ESC) vs simple mode |
| `qword_4F07468` | int64 | `wrap_control` | Low32: disable wrap. High32: suppress context |
| `qword_4F07510` | FILE\* | `diag_output_stream` | Output stream (stderr) |
| `qword_4D04908` | FILE\* | `diag_log_file` | Machine-readable log file |
| `byte_4CFFE80` | array | `diag_seen_flags` | Per-diagnostic duplicate tracking |

## Growable String Buffer Infrastructure

All three diagnostic systems share the same growable string buffer used for message formatting. The buffer structure appears at `qword_4D039D8` (output buffer), `qword_4D039E0` (prefix buffer), and `qword_4D039E8` (header/message buffer):

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| +0 | 8 | (tag/type) | Unused or type discriminator |
| +8 | 8 | capacity | Maximum bytes before realloc |
| +16 | 8 | length | Current write position |
| +24 | 8 | (unused) | Padding |
| +32 | 8 | data | `char*` pointer to the actual buffer |

| Helper | Address | Operation |
|--------|---------|-----------|
| `sub_823800` | `0x823800` | Reset/clear buffer (set length to 0) |
| `sub_823810` | `0x823810` | Grow buffer capacity (realloc) |
| `sub_8238B0` | `0x8238B0` | Append data: `memcpy(buf->data + buf->length, str, len)` |
| `sub_8237A0` | `0x8237A0` | Allocate new buffer (initial capacity = 1024) |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `sub_67B780` | `0x67B780` | -- | EDG: Increment error/warning counters |
| `sub_67B7E0` | `0x67B7E0` | -- | EDG: Build include-stack annotation |
| `sub_67B9F0` | `0x67B9F0` | -- | EDG: Diagnostic record pool allocator |
| `sub_67BB20` | `0x67BB20` | -- | EDG: Argument node allocator |
| `sub_67BBF0` | `0x67BBF0` | -- | EDG: Set ANSI color state for output |
| `sub_67BD40` | `0x67BD40` | -- | EDG: Emit newline/flush for source context |
| `sub_67BDC0` | `0x67BDC0` | -- | EDG: Load file metadata and tab stop width |
| `sub_67C120` | `0x67C120` | -- | EDG/SARIF: Emit `physicalLocation` JSON |
| `sub_67C860` | `0x67C860` | -- | EDG: Localized string lookup by ID |
| `sub_67D2D0` | `0x67D2D0` | -- | EDG: Convert internal diag ID to user-visible number |
| `sub_67D470` | `0x67D470` | -- | EDG: Record pragma-based suppression |
| `sub_67D520` | `0x67D520` | -- | EDG: Check pragma-based suppression |
| `sub_67D610` | `0x67D610` | -- | EDG: Create synthetic diagnostic (warnings-as-errors) |
| `sub_681B50` | `0x681B50` | -- | EDG: Populate message text into header buffer |
| `sub_681D20` | `0x681D20` | 37KB | EDG: Terminal text diagnostic renderer |
| `sub_683690` | `0x683690` | -- | EDG/SARIF: Emit JSON-escaped `message` object |
| `sub_6837D0` | `0x6837D0` | 20KB | EDG: Diagnostic dispatch and SARIF renderer |
| `sub_721AB0` | `0x721AB0` | -- | EDG: Multi-byte character byte count |
| `sub_722DF0` | `0x722DF0` | -- | EDG/SARIF: Resolve file-id to path string |
| `sub_722FC0` | `0x722FC0` | -- | EDG: Format filename into buffer |
| `sub_723260` | `0x723260` | -- | EDG: Get filename string from file info |
| `sub_723640` | `0x723640` | -- | EDG: Get decorated source location string |
| `sub_729B10` | `0x729B10` | -- | EDG: Retrieve file/line data for source context |
| `sub_729E00` | `0x729E00` | -- | EDG/SARIF: Decompose packed source position |
| `sub_729F80` | `0x729F80` | -- | EDG: Promote severity (hard error) |
| `sub_7235F0` | `0x7235F0` | -- | EDG: Fatal exit with severity code |
| `sub_7AF1D0` | `0x7AF1D0` | -- | EDG: Newline character mapping lookup |
| `sub_823800` | `0x823800` | -- | Shared: Reset/clear growable string buffer |
| `sub_823810` | `0x823810` | -- | Shared: Grow/realloc string buffer |
| `sub_8237A0` | `0x8237A0` | -- | Shared: Allocate new growable buffer |
| `sub_8238B0` | `0x8238B0` | -- | Shared: Append to string buffer |
| `sub_B16430` | `0xB16430` | -- | LLVM Remark: Create named string attribute |
| `sub_B16530` | `0xB16530` | -- | LLVM Remark: Append named value |
| `sub_B16B10` | `0xB16B10` | -- | LLVM Remark: Create named integer attribute |
| `sub_B157E0` | `0xB157E0` | -- | LLVM Remark: Get DebugLoc for remark source location |
| `sub_B17560` | `0xB17560` | -- | LLVM Remark: Construct `OptimizationRemark` (passed) |
| `sub_B178C0` | `0xB178C0` | -- | LLVM Remark: Construct warning-level `DiagnosticInfo` |
| `sub_B180C0` | `0xB180C0` | -- | LLVM Remark: Finalize and prepare remark for emission |
| `sub_B18290` | `0xB18290` | -- | LLVM Remark: Append raw string to remark message |
| `sub_B2BE50` | `0xB2BE50` | -- | LLVM Remark: `getRemarkStreamer` |
| `sub_B6EA50` | `0xB6EA50` | -- | LLVM Remark: `isEnabled` check |
| `sub_B6F970` | `0xB6F970` | -- | LLVM Remark: `getRemarkFilter` |
| `sub_B91220` | `0xB91220` | -- | LLVM Remark: Free remark string |
| `sub_C2E790` | `0xC2E790` | 6KB | LLVM Remark: `createRemarkSerializer` factory |
| `sub_C302C0` | `0xC302C0` | 4KB | LLVM Remark: YAML remark serializer emit |
| `sub_C30A00` | `0xC30A00` | 6KB | LLVM Remark: YAML remark parser (6 type tags) |
| `sub_C31010` | `0xC31010` | 8KB | LLVM Remark: YAML remark field parser |
| `sub_EFCCF0` | `0xEFCCF0` | 9KB | LLVM Remark: Bitstream abbreviation emitter |
| `sub_EFD2C0` | `0xEFD2C0` | 18KB | LLVM Remark: Bitstream record writer |
| `sub_EFE900` | `0xEFE900` | 30KB | LLVM Remark: Bitstream remark parser |
| `sub_F01350` | `0xF01350` | 23KB | LLVM Remark: Bitstream remark serializer |
| `sub_1049740` | `0x1049740` | -- | LLVM Remark: Publish remark to diagnostic handler |
| `sub_15CA330` | `0x15CA330` | -- | LLVM Remark: `OptimizationRemark` constructor |
| `sub_15CA540` | `0x15CA540` | -- | LLVM Remark: `OptimizationRemarkMissed` constructor |
| `sub_15CAB20` | `0x15CAB20` | -- | LLVM Remark: `OptimizationRemark::operator<<(StringRef)` |
| `sub_15CAD70` | `0x15CAD70` | 13KB | LLVM Remark: YAML remark serializer (NVIDIA-extended) |
| `sub_1DCCCA0` | `0x1DCCCA0` | -- | LLVM Remark: `OptimizationRemarkEmitter::emit` |

## Cross-References

- [Entry Point & CLI](../pipeline/entry.md) -- flag routing for `-w`, `-Werror`, `-inline-info`, `-Xopt` pass-through
- [GVN](../llvm/gvn.md) -- `profusegvn` knob and GVN diagnostic output
- [Inliner Cost Model](../lto/inliner-cost.md) -- `profuseinline` knob and inline cost diagnostics
- [LLVM Pass Pipeline](../llvm/pipeline.md) -- `opt-remark-emit` and `machine-opt-remark-emitter` analysis pass registration
- [EDG Frontend](../pipeline/edg.md) -- EDG option registration including `--diagnostics_format`
- [CLI Flags](../config/cli-flags.md) -- complete flag-to-pipeline routing table
- [Knobs](../config/knobs.md) -- `profuseinline`, `profusegvn`, and remark-related knobs
- [AsmPrinter](asmprinter.md) -- remark emission during code generation
