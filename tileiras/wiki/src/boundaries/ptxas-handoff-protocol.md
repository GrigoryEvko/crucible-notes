# Handoff Protocol: tileiras → ptxas

## Abstract

Tileiras finishes its MLIR-to-PTX lowering inside its own address space and then shells out to a separate `ptxas` binary to obtain a cubin. The boundary is text-only: PTX leaves tileiras as an ASCII string passed inline on the child's command line, ptxas writes the assembled cubin bytes to stdout, and tileiras reads them back through the parent end of the pipe set up by its subprocess harness. No shared memory, no temporary file for the PTX, no IPC beyond argv plus stdout. A separate knob file (path supplied through the environment) carries scheduling and codegen hints that tileiras itself never inspects. This page reconstructs that boundary from the binary.

## Subprocess argv

The argv vector is assembled by the PTX serialization path and handed to a subprocess wrapper that uses the platform process-launch primitives. The launcher itself is architecture-agnostic; the GPU target appears only in the argv strings assembled at the call site.

The final argv shape, in order, is:

```
ptxas
[ <module-attribute "ptxas-options" tokens> ]
-arch sm_<NN>
--opt-level <N>
--input-as-string <PTX text>
[ ...basePTXOptions tokens... ]
   = " --knobs-file=<PTX_KNOBS_PATH>"
   = " --nv-host=\"<host-code-temp-path>\""
   = " <basePTXOptions string-attr value>"
```

| Flag | Origin | Role |
|------|--------|------|
| `ptxas` | fixed argv program name | Tool name; resolved through `$PATH` by the spawn helper. |
| `-arch sm_<NN>` | module GPU compute-capability attribute | Target architecture string. NN is decimal, for example `sm_100`, `sm_103`, `sm_120`, or `sm_121`. |
| `--opt-level <N>` | module optimization-level attribute | ptxas optimization level, accepted as a small decimal value. |
| `--input-as-string <PTX>` | PTX serializer output | Inlines the PTX program as a single argv token rather than reading a file. |
| ` --knobs-file=<path>` | `$PTX_KNOBS_PATH` when `$MLIR_ENABLE_EVO` is set | Hands ptxas a path to the scheduling-knob file. Tileiras performs no path validation. |
| ` --nv-host="<path>"` | host-code serialization path | Points ptxas at a temporary host-code blob. Quotes and backslashes in the path are escaped before the token is wrapped in double quotes. |

The `--input-as-string` choice ties the PTX size to the kernel's `MAX_ARG_STRLEN` budget (131 072 bytes per token on Linux). For larger kernels a fallback to `--input-file=<temp.ptx>` would be required; the current binary does not implement one.

## PTX text protocol

Tileiras emits **PTX as ASCII text**, not LLVM bitcode and not NVVM IR. LLVM bitcode, NVVM IR text, and PTX-only output modes all stop before ptxas; only the cubin-producing mode reaches the subprocess launcher. By the time argv is built, the PTX has already passed through the full NVPTX backend pipeline inside tileiras's process. `ptxas` sees a finished PTX program, not an intermediate.

## Cubin returned via stdout

There is no `-o <out.cubin>` flag in the argv. Instead, the subprocess harness plumbs ptxas's stdout into a parent-side buffer and stores the captured bytes as the cubin payload. No temporary cubin file is named on the parent side for ptxas's output. Stderr is captured separately and forwarded into the driver's diagnostic callback.

The harness enforces a wall-clock timeout. On expiry the child is killed, and the diagnostic `"Child timed out"` or `"Child timed out but wouldn't die"` is surfaced through the same stderr pipe. Abnormal exits decode into either `"Program could not be executed"` or a signal-name string with an optional `" (core dumped)"` suffix.

## Knob-file format

Tileiras only writes the path; ptxas does the parsing. The receiver-side file format is:

```
<arbitrary preamble bytes>
[knobs]
<command-stream>
```

The literal `[knobs]` is mandatory and case-sensitive; everything before it is preamble and silently discarded. After the header, commands separate on whitespace, the `~` byte, or the `;;` sequence. A command is either an `INJECTSTRING <body> ;;`, a `WHEN=<clause>` directive, or a regular `key=value` or bare-`key` assignment. Identifiers are case-insensitive.

| Knob (representative) | Value type | Effect |
|-----------------------|-----------|--------|
| `DUMPIR=AllocateRegisters` | string/identifier | Dumps the IR after the named pass (debug aid). |
| `EmitLDCU` | bool/int | SM90+ only; controls whether ptxas may emit `ldcu` instructions. Requires `-forcetext` plus `-sso out.sass`. |
| `IgnorePotentialMixedSizeProblems` | bool | Suppresses one class of mixed-width verifier errors. |
| `WHEN=SH=<clause>` | when-list (type 9) | Conditional predicate gate that scopes the next assignment. |
| `INJECTSTRING <text> ;;` | raw bytes | Splices a SASS template into the output stream. |
| any int knob (`...=N`) | INT32 / UINT32 | Decimal only; `0x` prefixes silently parse as 0. |
| any range knob (`...=N..M`) | INT32_RANGE | Either side may be omitted (sentinels `INT_MIN`/`INT_MAX`). |
| any list knob (`...=N1,N2,N3`) | INT32_LIST | Comma-separated decimals; trailing commas reject with `"End of integer range value is not ',' or null character"`. |
| any float knob (`...=1.5e-3`) | FLOAT32 / FLOAT64 | Whatever libc `sscanf("%f"|"%lf")` accepts. |

Malformed knob files terminate the compile with a fatal diagnostic — `"Knobs header not found in %s"`, `"Invalid knob identifier"`, `"Invalid knob specified (%s)"`, `"Invalid knob type"` — emitted to stderr and surfaced via the harness.

## Scheduling boundary invariant

Tileiras schedules MLIR ops on its own internal Blackwell pipeline model with fifteen reservation slots; ptxas independently schedules SASS using its own latency tables and dual-issue rules. **The two scheduling layers do not share any explicit constraint vocabulary across the boundary.** The PTX text carries instruction order plus a small set of declarative directives (`.maxntid`, `.reqntid`, `.minnctapersm`, `.pragma "nounroll"`, ...); none of it expresses tileiras's slot map. ptxas is free to reorder within the bounds PTX semantics permit, but only ever **adds stalls** relative to the order tileiras committed to — it never reorders past PTX-level dependences, and tileiras has already committed to whatever in-instruction parallelism it chose. The practical consequence is that any scheduling intent tileiras wants enforced has to survive PTX-text serialization either as instruction order or as a knob-file / directive hint; anything else is lost at the boundary.

## Producer-side bug flagged

Tileiras can in principle emit both `.maxntid` and `.reqntid` directives on a single entry function because its directive emission paths are independent. ptxas rejects that combination during final entry-function validation. The relevant rule is *"`.maxntid` and `.reqntid` are mutually exclusive"*, alongside the related constraints that `.maxnctapersm`/`.minnctapersm` require launch-bounds metadata, `.reqntid` plus `.reqnctapercluster` requires `.blocksareclusters`, and `.reqnctapercluster` conflicts with `.maxclusterrank`.

For reimplementation, the safest rule is to normalize launch-bound metadata before PTX printing. Pick either `.maxntid` or `.reqntid`, emit the dependent cluster directives only when their prerequisite is present, and surface ptxas stderr verbatim when the receiver rejects the final PTX.
