# Error / Status Codes

> *All addresses on this page apply to the libtpu.so shipped in the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Offsets are file offsets; for the `.rodata` string table these equal the virtual address (1:1 mapping — `.rodata` starts at `0x84a0000` both on disk and in memory). Other builds will differ.*

## Abstract

libtpu reports every failure through `absl::Status`, not through `errno` or a custom enum. A `Status` is either `OK` or carries one of the canonical `absl::StatusCode` values — `INVALID_ARGUMENT`, `INTERNAL`, `FAILED_PRECONDITION`, `UNIMPLEMENTED`, `RESOURCE_EXHAUSTED`, `NOT_FOUND`, `UNAVAILABLE`, `OUT_OF_RANGE`, `ABORTED`, `DEADLINE_EXCEEDED`, `ALREADY_EXISTS`, `PERMISSION_DENIED`, `DATA_LOSS`, `CANCELLED`, `UNKNOWN` — plus a message string. At the PJRT boundary that code is translated to a `PJRT_Error_Code` (the four `PJRT_Error_*` C-API entry points — `PJRT_Error_Destroy`, `PJRT_Error_Message`, `PJRT_Error_GetCode`, `PJRT_Error_ForEachPayload` — plus the `pjrt::PjrtErrorCodeToStatusCode` translator are all present) so a JAX/XLA client sees the same taxonomy. This appendix is the consolidated cross-family index: it groups the message *templates* by subsystem, gives a representative verbatim string with its `.rodata` address for each family, names the StatusCode the family typically carries, and points at the deeper pages that catalogue each surface in full.

There are three idioms by which TPU code constructs a non-OK `Status`, and the one used at a callsite is what fixes the StatusCode — the format string alone does not. The dominant idiom by callsite count is the streaming builder `xla::status_macros::MakeErrorStream` (the `RET_CHECK(c) << …` / `return InvalidArgument(…) << …` macro). The byte-confirmed idiom is the templated factory `xla::<Code>StrCat<Types…>(literal0, arg0, literal1, arg1, …, absl::SourceLocation)` — a flattened `absl::StrCat` whose Itanium-mangled template-argument pack names every interleaved literal segment and typed argument; one demangled example reads `xla::InvalidArgumentStrCat<char const(&)[28], long&, char const(&)[22], std::string>`. The third, used sparingly, is the prose free function `absl::<Code>Error("…")`. The error *prose* is overwhelmingly `%s`/`%d`-formatted (string args are shapes, layouts, instruction names, device names; int args are dimensions, counts, sizes) — pointer and float specifiers are confined to low-level driver and cost-model paths.

The page is organized as: an at-a-glance table of the StatusCode keyword surface and the major template families; one TABLE section per family (compile/HLO verifier, runtime/driver/PJRT, ICI/collective, MSA/memory, SparseCore/embedding, scheduler) with representative strings and addresses; the `CHECK`/`RET_CHECK`/fatal templates; the user-facing **hint** strings (the actionable remedies appended to errors); and the Megascale **ErrorAggregator** path (how cross-host failures are collected and classified into a single digest). It is the appendix you grep when you hit a libtpu error.

For reimplementation, the contract is:

- The `absl::Status` model — canonical StatusCode set + message string, translated to `PJRT_Error_Code` at the C-API boundary; no `errno`.
- The three construction idioms and which one fixes the StatusCode (`MakeErrorStream` streaming, `<Code>StrCat` factory, `absl::<Code>Error` prose).
- The template-family taxonomy: which subsystem raises which message shape, and the StatusCode each family carries.
- The hint-string convention: actionable remedies (`--flag=value`, `go/<link>`, `b/<id>`, capacity advice) concatenated onto the error message.
- The Megascale fan-in: per-host `MegaScaleRuntimeError` reports collected, classified into one `Cause`, and emitted as a digest.

| | |
|---|---|
| **Status type** | `absl::Status` / `absl::StatusOr<T>` (pervasive; >2600 `absl::Status` symbols, >11500 `StatusOr` symbols) |
| **Client-facing code** | `PJRT_Error_Code` (translated from `absl::StatusCode` by `pjrt::PjrtErrorCodeToStatusCode`; `PJRT_Error_GetCode` / `_Message` / `_Destroy` / `_ForEachPayload` present) |
| **Dominant construction idiom** | `xla::status_macros::MakeErrorStream` — `RET_CHECK(c) << …` / `return InvalidArgument(…) << …` |
| **Byte-confirmed factory** | `xla::<Code>StrCat<Types…>(…, absl::SourceLocation)` — 38 InvalidArgument, 31 Unimplemented, 29 Internal, 1 ResourceExhausted (distinct C1/C2 ctors) |
| **Prose factory** | `absl::<Code>Error("…")` (sparse) |
| **RET_CHECK prefix** | `"TPU_RET_CHECK failure ("` @ `0xa183292` |
| **CHECK prefix** | `"Check failed: '"` @ `0xa1a64de`, `"Check failed: "` @ `0xa285fb1` |
| **Format idiom split** | printf-style `%s/%d/%lu/%f` (house style) dominates; positional `$0/$1`/`%v` used by Megascale + collective validators |

> **NOTE —** the format string is *not* a reliable StatusCode oracle. Only the 99 `<Code>StrCat` factory instantiations and the `MakeErrorStream` callsites byte-confirm the code; for the large printf-style family fed through `return InvalidArgument(absl::StrFormat(template, …))`, the StatusCode is set at the (disassembly-only) callsite. The per-family StatusCode column below is the *dominant* code for the family, not a byte-confirmed code-per-template map. Treat any single row's StatusCode as MEDIUM confidence unless the family is wholly factory-built.

---

## At-a-Glance: StatusCode Surface and Template Families

### StatusCode keyword occurrence

Noise-filtered occurrence of each `absl::StatusCode` name in the string table. This is a frequency signal, not a per-template count — it indicates which codes the binary leans on.

| StatusCode | Occurrence | Notes | Confidence |
|---|---|---|---|
| `INVALID_ARGUMENT` | ~300 | The verifier / user-input code; the largest factory family (38 StrCat) | HIGH |
| `UNIMPLEMENTED` | ~169 | "not supported" / "not implemented" prose (31 StrCat) | HIGH |
| `NOT_FOUND` | ~75 | lookup misses (driver maps, schedule references) | MEDIUM |
| `FAILED_PRECONDITION` | ~56 | state/ordering violations (firmware queue state, init order) | MEDIUM |
| `RESOURCE_EXHAUSTED` | ~21 | OOM / capacity (1 StrCat; MSA the main raiser) | MEDIUM |
| `UNAVAILABLE` | ~15 | transient transport / device-down | MEDIUM |
| `OUT_OF_RANGE` | ~14 | index/bound errors | MEDIUM |
| `ABORTED` | ~8 | cancellation / teardown | LOW |
| `ALREADY_EXISTS` | ~3 | duplicate registration | LOW |
| `DEADLINE_EXCEEDED` | ~3 | timeout paths (GTC, sflag, deadline timers) | LOW |
| `PERMISSION_DENIED` | ~2 | rare | LOW |
| `DATA_LOSS` | ~1 | rare | LOW |
| `INTERNAL` | — (see note) | 29 InternalStrCat + ~13 MakeErrorStream sites byte-confirmed | MEDIUM |

> **GOTCHA —** the raw `Internal` keyword count is dominated by the `/internal/` source-path component baked into thousands of `.cc` paths in the string table; it is **not** a usable INTERNAL-status count. The reliable INTERNAL figure is the 29 `InternalStrCat` factory instantiations plus the `MakeErrorStream` sites, not the keyword tally.

### Template families

Counts are error-prose-filtered candidates per subsystem (a template may match more than one group; first-match-wins ordering: compile → MSA → ICI → SparseCore → runtime → scheduler → megascale). The full extracted surface is ~2937 distinct error/status templates (2799 printf-style, 138 positional).

| Family | ~Templates | Dominant StatusCode | Dominant idiom | Confidence |
|---|---:|---|---|---|
| Compile / HLO / verifier | ~875 | `INVALID_ARGUMENT` | `StrFormat %s/%d` + `RET_CHECK` | HIGH |
| Runtime / driver / PJRT | ~177 | `INTERNAL` / `FAILED_PRECONDITION` | `StrFormat %s/%d/%p` + errno | MEDIUM |
| ICI / collective | ~90 | `INTERNAL` / `DEADLINE_EXCEEDED` | `StrFormat %d/%s` + `$N` (msc) | MEDIUM |
| MSA / memory / allocation | ~79 | `RESOURCE_EXHAUSTED` / `INTERNAL` | `StrFormat %zu/%lld` | MEDIUM |
| SparseCore / embedding | ~68 | `INVALID_ARGUMENT` | `StrFormat %d/%s/%f` | HIGH |
| Megascale (DCN runtime) | ~21 | `INTERNAL` / `LOG(FATAL)` | positional `$0/$1` | MEDIUM |
| Scheduler / fuel / FIFO | ~15 | `INTERNAL` / `RET_CHECK` | `StrFormat %s/%d/%c` | MEDIUM |
| CHECK / RET_CHECK / fatal | — | abort (no Status) | absl `CHECK` / `LogMessageFatal` | HIGH |

---

## Compile / HLO / Verifier Family

The single largest error block. These are the shape-inference and HLO-verifier diagnostics a JAX/XLA user sees when a program is malformed — rank/dimension/shape mismatches, opcode/operand-count errors, bitcast and layout violations. Nearly all carry `INVALID_ARGUMENT` (the verifier's signature) or fail a `RET_CHECK`. The comparison style `"%d vs. %d"` / `"%s vs. %s"` is the verifier's fingerprint; it appends the two mismatching operand values.

| Address | Representative template | StatusCode | Confidence |
|---|---|---|---|
| `0x857d7ed` | `"A dimension number is out of range in Dot: %s. %s %s"` | `INVALID_ARGUMENT` | HIGH |
| `0x857d595` | `"Argument to Cholesky must have rank >= 2; shape was %s"` | `INVALID_ARGUMENT` | HIGH |
| `0x8580369` | `"All reduced tensors must have the same dimension. Tensor 0 has shape %s, Tensor %d has shape %s"` | `INVALID_ARGUMENT` | HIGH |
| `0x8728000` | `"Binary op expects 2 operands, but got %d"` | `INVALID_ARGUMENT` | HIGH |
| `0x872a259` | `"broadcast_dimensions contains invalid value %d for result with rank %d"` | `INVALID_ARGUMENT` | HIGH |
| `0xa02c26f` | `"Cannot concatenate arrays that differ in dimensions other than the one being concatenated. Dimension %d in both shapes must be equal (or compatible): %s vs %s."` | `INVALID_ARGUMENT` | HIGH |
| `0x858400c` | `"Bad scalar opcode in slot 0, opcode: %d bundle: %v, bits: %s"` | `INVALID_ARGUMENT` | HIGH |
| `0xa01cdc3` | `"Bitcast requires a new on-device shape to have the same size of %d bytes, but got %d bytes."` | `INVALID_ARGUMENT` | MEDIUM |
| `0x857b3fa` | `"sharding's tile count and device count does not match: %d vs. %d; shape=%s, sharding=%s"` | `INVALID_ARGUMENT` | MEDIUM |

> **QUIRK —** the `%v` specifier in `"Bad scalar opcode in slot 0, opcode: %d bundle: %v, …"` (`0x858400c`) is not C-printf. It is the absl `AbslStringify` extension; the `bundle` argument is a per-generation `gxc::gfc::isa::TensorCoreBundle` rendered via its stringifier. The matching factory is byte-confirmed: `xla::InvalidArgumentStrCat<char const(&)[56], absl::StatusOr<…gxc::gfc::isa::TensorCoreBundle>>`, one of six `TensorCoreBundle`-bearing factory instantiations (the per-generation `gxc::gfc` / `gxc::glc` / `vxc` variants). A reimplementation that treats `%v` as a typo for `%s` will mis-format the bundle.

---

## Runtime / Driver / PJRT Family

Device lifecycle, driver state machine, firmware-queue transitions, chip-count and topology checks, and the PJRT C-API surface. Mixed StatusCode: `FAILED_PRECONDITION` for state/ordering violations, `INTERNAL` for driver-invariant failures, `NOT_FOUND` for map misses. This is the family most likely to embed `%p` (pointer) and errno-derived `%d`.

| Address | Representative template | StatusCode | Confidence |
|---|---|---|---|
| `0xa077a78` | `"Cannot transition to %s: the firmware queues are not in %s state; they are in %s state."` | `FAILED_PRECONDITION` | MEDIUM |
| `0x96c33b2` | `"Can't close driver while in state %s; are multiple threads trying to open / close?"` | `FAILED_PRECONDITION` | MEDIUM |
| `0xa0430a3` | `"Cannot remove a driver for %s, was not found in map."` | `NOT_FOUND` | MEDIUM |
| `0xa09fdcd` | `"Chip count (%d) is not supported."` | `INVALID_ARGUMENT` | MEDIUM |
| `0xa00ab4b` | `"Expected %d chips per tray, actually found a tray with %d chips."` | `INTERNAL` | MEDIUM |
| `0x858a3de` | `"failed initializing StreamExecutor for device ordinal %d: %s"` | `INTERNAL` | MEDIUM |
| `0xa1a96ba` | `"executable is built for device %s of type \"%s\"; cannot run it on device %s of type \"%s\""` | `INVALID_ARGUMENT` | HIGH |
| `0x94b68ce` | `"Can't get the optimized program for executable \`%s\`: MPMD execution is not supported by PJRT C API"` | `UNIMPLEMENTED` | MEDIUM |
| `0xa0a9937` | `"%d DMA buffers were still outstanding when the driver was re-opened. These buffers must be unmapped before the driver can be re-opened."` | `FAILED_PRECONDITION` | MEDIUM |

---

## ICI / Collective Family

Inter-Chip-Interconnect topology, link health, routing, and collective buffer validation. Carries `INTERNAL` (topology/routing invariants), `DEADLINE_EXCEEDED` (GTC convergence, sflag waits), or surfaces the fatal-link markers (see the fatal section). The Megascale-tagged collective buffer validators use the positional `$0/$1` idiom rather than printf.

| Address | Representative template | StatusCode | Confidence |
|---|---|---|---|
| `0xa0b4247` | `"Atomic/Special DMA must have length of %d bytes, got %d."` | `INVALID_ARGUMENT` | MEDIUM |
| `0xa0b3abc` | `"Cannot find unicast link next hop routing table for link port %d."` | `INTERNAL` | MEDIUM |
| `0xa0d5412` | `"Detected ICI link failures along %d dimensions, but only 1-dimensional link fault is allowed.."` | `FAILED_PRECONDITION` | MEDIUM |
| `0x871106b` | `"GTC failed to converge (max diff %d > %d) before timeout (%s) expired"` | `DEADLINE_EXCEEDED` | MEDIUM |
| `0x8583d20` | `"ICI Probe failed.  local port: %d name: %s took %d us. status: %s"` | `INTERNAL` | MEDIUM |
| `0xa0ba533` | `"ICI routing failed to retrieve %dth hop dimension from bit encoded cache data."` | `INTERNAL` | MEDIUM |
| `0x9a573e9` | `"ALL_REDUCE Output buffer size is not == Input buffer size. Input size: $0 Output size: $1 Group Size $2 Key: $3 Module: $4 MegascaleInfo: $5"` | `INVALID_ARGUMENT` | MEDIUM |
| `0x9c142f9` | `"ALL_GATHER Input buffer size is not (Output buffer size /  group size). Input size: $0 …"` | `INVALID_ARGUMENT` | MEDIUM |

> **NOTE —** the `$0/$1` strings are `absl::Substitute` positional templates, the second formatting idiom in the binary. They cluster in the Megascale runtime and the collective buffer-size validators. The protobuf extension-declaration validator also uses `$N`, but that is a statically-linked library, not TPU code, and is excluded from the TPU template counts.

---

## MSA / Memory / Allocation Family

Memory-Space-Assignment, HBM/VMEM/SMEM/CMEM allocation, prefetch, defragmentation, and the register allocator. The dominant StatusCode is `RESOURCE_EXHAUSTED` for true OOM (the single `ResourceExhaustedStrCat` factory lives here) and `INTERNAL` for allocator-invariant violations. Note the `%zu`/`%lld` size specifiers.

| Address | Representative template | StatusCode | Confidence |
|---|---|---|---|
| `0xa030cd9` | `"AllocateBufferForMemorySpace: Unsupported memory space: %s."` | `INVALID_ARGUMENT` | MEDIUM |
| `0x858aa58` | `"Allocation (size=%lld) would exceed memory (size=%lld) :: %s :: %s"` | `RESOURCE_EXHAUSTED` | MEDIUM |
| `0xa1300d0` | `"Failed to allocate %zu bytes. Memory limit: %zu bytes. Used: %zu bytes.)"` | `RESOURCE_EXHAUSTED` | HIGH |
| `0xa13e8e5` | `"Failed to allocate node (%zu bytes). Memory limit: %zu [bytes]. Used: %zu [bytes].)"` | `RESOURCE_EXHAUSTED` | HIGH |
| `0xa01cf08` | `"Out of memory allocating %d bytes."` | `RESOURCE_EXHAUSTED` | HIGH |
| `0xa09a63e` | `"Number of bytes %lld allocated must be a multiple of chunk size %lld."` | `INTERNAL` | MEDIUM |
| `0x857eed1` | `"Register allocator verification failure: live range %s; instruction %s"` | `INTERNAL` | MEDIUM |
| `0xa02b12f` | `"Scoped allocation with size %s and limit %s exceeded scoped %s limit by %s."` | `RESOURCE_EXHAUSTED` | MEDIUM |
| `0x8584ecd` | `"Error defragmenting HBM %s: %s"` | `INTERNAL` | MEDIUM |

---

## SparseCore / Embedding Family

The `xla_sc_*` / BarnaCore embedding engine: minibatch construction, table validation, optimizer/feature limits, and the 32-bit SparseCore element limits. Mostly user-facing `INVALID_ARGUMENT` configuration errors (the operator can fix the embedding config).

| Address | Representative template | StatusCode | Confidence |
|---|---|---|---|
| `0xa0a9715` | `"barna_core_infeed_queue_hbm_address must be %d-byte aligned."` | `INVALID_ARGUMENT` | MEDIUM |
| `0xa0d36d1` | `"Invalid num_features: %d found for table: %s in the TPU embedding configuration. Valid values are >0."` | `INVALID_ARGUMENT` | HIGH |
| `0xa030dd4` | `"Could not find valid TPU batch of length at least %d at position %d for row %d. The embedding work in one sample exceeds what the BarnaCore can process: %s. %s."` | `INVALID_ARGUMENT` | MEDIUM |
| `0xa0b7076` | `"Logical replicas must evenly divide the SparseCores in the system. logical_replicas = %d, physical_sparse_cores = %d."` | `INVALID_ARGUMENT` | MEDIUM |
| `0xa069f4e` | `"Number of TPU tables on row: %d exceeds what the BarnaCore hardware supports: %d > %d. This is mostly likely a result of incorrect partitioning."` | `INVALID_ARGUMENT` | MEDIUM |
| `0xa0ff40e` | `"Row pointers would exceed available SCS Smem (%d bytes > %d bytes)"` | `RESOURCE_EXHAUSTED` | HIGH |
| `0xa07d172` | `"Scatter operand has %d elements, which exceeds the 32-bit limit. Unsupported on SparseCore."` | `UNIMPLEMENTED` | MEDIUM |
| `0x86fa1ad` | `"Failed to parse TPU embedding partitioner optimization objective \"%s\". Valid options: performance, hbm_usage, hybrid"` | `INVALID_ARGUMENT` | MEDIUM |

---

## Scheduler / Fuel / FIFO Family

The latency-hiding scheduler, annotation parser, optimization "fuel" budget, and the FIFO push/pop validation. Small family; carries `INTERNAL` / `RET_CHECK` mostly, with `INVALID_ARGUMENT` for the user-facing `--xla_fuel` flag-value error.

| Address | Representative template | StatusCode | Confidence |
|---|---|---|---|
| `0x857fa91` | `"async-done for %s must be scheduled before %s"` | `INTERNAL` | MEDIUM |
| `0x858a9b8` | `"Cannot schedule FIFO pop instruction when the FIFO is empty %s :: %s"` | `INTERNAL` | MEDIUM |
| `0x857ad7e` | `"Cannot schedule FIFO push instruction when the FIFO is full.  FIFO name: %s. (element count %d vs %d). %s :: %s%s"` | `INTERNAL` | MEDIUM |
| `0xa086733` | `"Reference instruction %s was not found in the schedule."` | `NOT_FOUND` | MEDIUM |
| `0xa03ca6a` | `"Illegal value for --xla_fuel. Saw %s, but expected token %s to be an integer."` | `INVALID_ARGUMENT` | MEDIUM |
| `0x862868f` | `"GVN: Not replacing %s because GVN is out of fuel"` | (log / no Status) | LOW |

---

## CHECK / RET_CHECK / Fatal Templates

These are the abort paths — they do not return a `Status`, they terminate. The absl `CHECK` family stringifies the *source expression* into rodata (large blocks of literal C++ source appear in the string table as a side effect of `CHECK(expr) << "msg"`); those source fragments are CHECK-condition evidence, not message templates. The runtime CHECK templates proper are the bare absl prefixes below.

| Address | Template | Origin / macro | Confidence |
|---|---|---|---|
| `0xa1a64de` | `"Check failed: '"` | absl `CHECK` / `QCHECK` runtime prefix | HIGH |
| `0xa285fb1` | `"Check failed: "` | absl `CHECK` runtime prefix (no quote) | HIGH |
| `0xa183292` | `"TPU_RET_CHECK failure ("` | XLA TPU `RET_CHECK` macro | HIGH |
| `0xa2300c5` | `"MakeErrorStream destructed without getting absl::Status: "` | `status_macros` self-check | HIGH |
| `0xa1e7cb3` | `"!!!! FATAL ERROR !!!! for "` | ICI fatal-error check | HIGH |
| `0xa046045` | `"Fatal error occurred. Data links will go down."` | ICI hard-failure marker | HIGH |
| `0xa1b3810` | `"FATAL ERROR RECEIVED FROM HARDWARE!!!"` | hardware fatal interrupt | HIGH |
| `0xbe7d460` | `"FATAL ERROR: This binary was compiled with aes enabled, but this feature is not available on this processor (go/sigill-fail-fast).\n"` | absl CPU-feature startup guard | HIGH |
| `0xa045d37` | `"Aborting the coordinator after collecting errors from all workers as megascale_error_reporter_abort_on_hang is set to true. …"` | Megascale `LOG(FATAL)` | HIGH |

> **QUIRK —** the `go/sigill-fail-fast` fatal is **not** a TPU path. It is the statically-linked absl CPU-feature startup guard, one variant per ISA feature the build requires: `aes`, `avx`, `mmx`, `pclmul`, `popcnt`, `sse`, `sse2`, `sse3`, `sse4.1`, `sse4.2`, `ssse3`, `cmpxchg16b` (12 in total). It fires before any TPU code runs if the host CPU lacks the feature. A reimplementation of the TPU surface need not reproduce it; an operator seeing it has a host-CPU problem, not a TPU one.

> **CORRECTION (ERR-1) —** an earlier note listed the `go/sigill-fail-fast` CPU-feature guard as having 12 variants but named only 11. The binary confirms 12: the missing twelfth is `cmpxchg16b`. The variant strings are a contiguous run beginning at `0xbe7d460`.

The `MakeErrorStream` self-check strings (`"MakeErrorStream destructed without getting absl::Status:"`, `"…shift called after getting absl::Status:"`, `"…got absl::Status more than once:"`) are diagnostics the status-macro machinery emits about its own misuse, not about the program under compilation.

---

## Status-Construction Idioms

The same message text can carry different StatusCodes depending on how the `Status` is built. Three idioms exist; only the first two byte-confirm the code.

```text
1. xla::status_macros::MakeErrorStream  — the dominant idiom by callsite
     RET_CHECK(cond) << "message"        →  INTERNAL (RET_CHECK) or the
     return InvalidArgument(...) << ...      explicitly-named code
   Each StatusOr<T> return type the macro is used in emits a
   MakeErrorStreamWithOutput<T> conversion operator (the operator<<
   and the cv-StatusOr<T> conversion are visible per T in the symtab).

2. xla::<Code>StrCat<Types...>(lit0, arg0, lit1, arg1, ..., SourceLocation)
     A flattened absl::StrCat. The mangled type pack names every piece.
     Distinct C1/C2 ctor instantiations:
       InvalidArgumentStrCat    38   →  INVALID_ARGUMENT
       UnimplementedStrCat      31   →  UNIMPLEMENTED
       InternalStrCat           29   →  INTERNAL
       ResourceExhaustedStrCat   1   →  RESOURCE_EXHAUSTED
     Example (demangled):
       xla::InvalidArgumentStrCat<char const(&)[28], long&,
                                  char const(&)[22], std::string>
       = InvalidArgument("<27-char lit>", long, "<21-char lit>", string)

3. absl::<Code>Error("...")  — prose free function, used sparingly
     e.g. return absl::InternalError("Invalid error type")
```

> **NOTE —** the `<Code>StrCat` arg types are recoverable without disassembly because the Itanium mangling encodes them: `RA<N>_Kc` is a `const char(&)[N]` literal segment (visible literal = N−1 chars), `m`/`l`/`i`/`f` are `unsigned long`/`long`/`int`/`float` by value, and `NSt…basic_string…` is `std::string`. Across the 99 factories the args are overwhelmingly string-ish + `long`; `float` (2) and `TpuVersion` (1) are rare. This is the only place in the binary where format-arg C++ types are byte-recoverable.

### Resolving a template's StatusCode

To go from a grepped error string to its StatusCode, decide which idiom built it. The page tables give the *family-dominant* code; the per-callsite truth is one of these three patterns:

```c
// Pattern A — factory (byte-confirmed). The string is split into literal
// segments interleaved with typed args; the <Code> in the symbol name IS
// the StatusCode. Grep the demangled symtab for the literal-segment text.
//   return InvalidArgumentStrCat("Chip count (", count, ") is not supported.");
//   → symbol xla::InvalidArgumentStrCat<char const(&)[N], int, char const(&)[M]>
//   → INVALID_ARGUMENT, certain.

// Pattern B — RET_CHECK / streaming. The macro wraps a fixed code; the
// streamed text is appended. The "TPU_RET_CHECK failure (" prefix at the
// front of the rendered message is the tell.
//   TPU_RET_CHECK(operands.size() == 2) << "got " << operands.size();
//   → INTERNAL (RET_CHECK is always INTERNAL), certain by prefix.

// Pattern C — printf-style wrapped at the callsite (disassembly-only).
// The format string carries no code; the code is the function called on it.
//   return InvalidArgument(absl::StrFormat(template, shape, n));   // INVALID_ARGUMENT
//   return Internal(absl::StrFormat(template, ...));               // INTERNAL
// The family column is the best available signal without reading .text.
```

A template with the `"%s vs. %s"` / `"%d vs. %d"` comparison shape is almost always a verifier `INVALID_ARGUMENT` (Pattern C with `InvalidArgument`); a template prefixed with `TPU_RET_CHECK failure (` is `INTERNAL` (Pattern B); a template whose exact literal segments appear in a demangled `<Code>StrCat` symbol is that `<Code>` with certainty (Pattern A).

At the PJRT C-API boundary, the `absl::StatusCode` is translated to a `PJRT_Error_Code` (the four `PJRT_Error_*` entry points — `PJRT_Error_Destroy`, `PJRT_Error_Message`, `PJRT_Error_GetCode`, `PJRT_Error_ForEachPayload` — and the `pjrt::PjrtErrorCodeToStatusCode` mapping function are all present) so a JAX/XLA client receives the same canonical taxonomy through the plugin handle. See [PJRT Overview](../pjrt/overview.md).

---

## Hint Strings (Actionable Remedies)

Distinct from the error *template* (the format string), a **hint** is the actionable prose that follows an error with a remedy — concatenated onto the message at the callsite. They partition by a marker token, and the token itself signals user-facing-vs-internal: `--flag=value` / `Reduce … memory` / `Please remove the hosts` are operator-self-service; `go/<link>` / `b/<id>` / `please file a bug` point the user at the XLA/TPU team. Every `--flag` named in a hint is a real registered `absl::Flag`. The hints are catalogued in full on [Hint Strings](../runtime/hint-strings.md); a representative slice:

| Address | Hint string (verbatim, possibly truncated) | Class | Confidence |
|---|---|---|---|
| `0x858bb17` | `"Found a deeply - nested fusion … Please use --xla_tpu_rwb_fusion=false (and --xla_tpu_dot_dot_fusion=false if failure persists) …"` | flag-suggestion | HIGH |
| `0x96c35ed` | `"PartialReduce is designed to be used with fusion. Did you forget to set \`--xla_tpu_nested_dot_fusion=true\`?"` | flag-suggestion | HIGH |
| `0xa074100` | `". Reduce TPU memory usage or set --jellyfish_executor_max_wait_time_for_releasing_memory_on_oom to a larger value."` | capacity / OOM | HIGH |
| `0xa07b361` | `"Not enough HBM spill stack available, please increase."` | capacity | HIGH |
| `0xa0c8b8d` | `"Encountered Dot op during TPU lowering that should have been eliminated during an earlier phase of compilation. This should not happen - please file a bug against XLA."` | bug-report (internal) | HIGH |
| `0x858c562` | `"Kernel body fingerprint collision detected for key: %016x%016x. Please file a bug with the XLA team and provide the colliding kernel bodies."` | bug-report (internal) | HIGH |
| `0xa011573` | `". See go/scoped-vmem for more details."` | doc-link | MEDIUM |
| `0x9feecc8` | `"--xla_tpu_impure_enable_packed_bf16_math_ops is deprecated. Please use --xla_tpu_bf16_emission_mode in TpuCompilationEnvironment."` | deprecation | HIGH |

> **CORRECTION (ERR-2) —** the deeply-nested-fusion remedy was anchored at `0x858bbcf` in scratch notes. The string-table entry begins at `0x858bb17` (`"Found a deeply - nested fusion …"`); `0x858bbcf` is an interior offset pointing at the `"(1) Please use --xla_tpu_rwb_fusion=false …"` segment within the same literal. Use the entry-start address `0x858bb17` for a verbatim grep.

> **GOTCHA —** a `--flag=value` hint tells you the *workaround*, which is often the **non-default**. `"Please use --xla_tpu_rwb_fusion=false"` means the default is `true`; `"Did you forget to set --xla_tpu_nested_dot_fusion=true?"` means the default is `false`. Read the advised value as a direction-of-default signal, not a confirmed default — the byte-confirmed defaults are a separate (smaller) set.

The `"This should not happen - please file a bug against XLA."` family is one contiguous block of TPU-lowering invariant checks at `0xa0c8b1e..0xa0c9758`, one per HLO op the TPU backend expects to have been legalised away before lowering (Dot, Call, the BatchNorm trio, Pad, Reverse, select-and-scatter, custom/output fusion). These are pure internal-bug markers — they should never fire on a well-formed compile.

---

## Megascale ErrorAggregator

The cross-host failure-collection path. When a multi-slice job hangs or errors, every TPU host posts a `MegaScaleRuntimeError` to the coordinator via the `MegaScaleTransport.ReportError` gRPC; the coordinator's `ErrorReporter` funnels them into a single `MegascaleErrorAggregator`, which classifies the aggregate into one `Cause` and emits a `RapidEyeErrorDigestProto` digest. Full treatment on [Megascale Error Aggregator](../megascale/error-aggregator.md); the error-status-relevant shape:

```text
worker host 0..N  ──MegaScaleRuntimeError──▶  /MegaScaleTransport/ReportError
                                                       │
                                          coordinator: ErrorReporter::ReportError
                                                       │  (lazy-alloc one aggregator)
                                                       ▼
                                          MegascaleErrorAggregator::AddError
                                                       │  linked_hash_map upsert,
                                                       │  dedup key "slice:host/task_id"
                                                       ▼
                          when size()==NumWorkers()  or  300ms deadline fires
                                                       ▼
                                          ProcessAndShutdown() → classify Cause
                                                       ▼
                                          LogErrorDigest()  (per-Cause LOG(ERROR))
                                          WriteErrorDigestToStorage() (opt-in)
                                          optional LOG(FATAL) abort
```

The per-host `MegaScaleRuntimeError.ErrorType` is the *input* category — `NO_ERROR=0`, `HANG_DETECTED=1`, `UNRECOVERABLE_ERROR=2`, `CANCELLED=3`. The aggregator runs a post-hoc analysis over the full reported set and emits one `RapidEyeErrorDigestProto.Cause` (the cross-host *verdict*), each with its own `LOG(ERROR)` remedy prose:

| Cause value | Name | Operator remedy (LOG(ERROR) prose) | Confidence |
|---:|---|---|---|
| 0 | `UNKNOWN_CAUSE` | `"Megascale detects a hang but cannot determine the root cause. Please inspect the full digest below."` | HIGH |
| 1 | `BAD_TPU_CHIP` | bad tensor-core chips → "remove the hosts from the fleet and restart the workload" | HIGH |
| 2 | `FINGERPRINT_MISMATCH` | inconsistent HLO compile → "likely a bug in JAX tracing or XLA compiler … inspect the HLO dumps" | HIGH |
| 3 | `DATA_INPUT_STALL` | `"Please check the workers to make sure the data input pipeline is working properly."` (`0x9fd7519`) | HIGH |
| 4 | `UNRECOVERABLE_ERROR` | "Some workers have halted with an unrecoverable error … inspect the error log of these workers" | HIGH |
| 5 | `DIFFERENT_MODULE` | "Please confirm that all workers is running the exact same program." | HIGH |
| 6 | `NETWORKING_ISSUE` | `"Please examine the underlying networking stack for the following hosts."` (`0x9ffc2f2`) | HIGH |
| 7 | `BAD_SC_CHIP` | bad sparse-core chips → "remove the hosts from the fleet and restart" (`0xa058553`) | HIGH |
| 8 | `PROGRAM_NOT_QUEUED` | "Check if your application is blocked/crashing and preventing JAX to queue the next TPU program" | HIGH |

> **NOTE —** the digest is the only place where an error is *scoped to hosts*. A single-process `absl::Status` names an op or a shape; the Megascale digest names the culprit `worker_id` / `host_name` / `chip_id` set. Aggregation is gated by `--megascale_error_aggregation_enabled` (default on); whether the digest aborts the coordinator is gated by `--megascale_error_reporter_abort_on_hang` / `--megascale_error_reporter_abort_on_error` (both default off — a digest is logged but the process survives unless one is set).

> **CORRECTION (ERR-3) —** the per-host `ErrorType` enum (`NO_ERROR`/`HANG_DETECTED`/`UNRECOVERABLE_ERROR`/`CANCELLED`) and the cross-host `Cause` enum are distinct. `UNRECOVERABLE_ERROR` exists in both, but `Cause` is the classifier *output* over the full set, not a relabeling of the input type — e.g. all-`HANG_DETECTED` inputs can classify to `BAD_TPU_CHIP`, `NETWORKING_ISSUE`, `DATA_INPUT_STALL`, etc. Do not conflate the two enums when reimplementing.

---

## Cross-References

- [Error Templates](../runtime/error-templates.md) — the full ~2937-template surface, the printf-vs-positional idioms, the per-subsystem template lists
- [Hint Strings](../runtime/hint-strings.md) — the complete actionable-hint catalogue (flag-suggestion / doc-link / bug-report / capacity / deprecation / operator-action)
- [Megascale Error Aggregator](../megascale/error-aggregator.md) — `MegascaleErrorAggregator` class layout, the `RapidEyeErrorDigestProto` wire format, the Cause classifier, retention and abort policy
- [Megascale Overview](../megascale/overview.md) — where the ErrorReporter sits in the DCN runtime
- [PJRT Overview](../pjrt/overview.md) — the `absl::Status` → `PJRT_Error_Code` translation at the client boundary
- [Megascale Cross-Host Barrier](../megascale/cross-host-barrier.md) — the barrier-participant validators that share the positional `$0/$1` error idiom
