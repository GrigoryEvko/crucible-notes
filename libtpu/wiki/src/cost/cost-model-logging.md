# Cost-Model Logging

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. Other versions differ.*

## Abstract

`xla_tpu_impure_cost_model_logging_options` is the one knob that controls whether the TPU compiler's per-op cost model **dumps its work** into the compile log. It is a side-effecting observability flag: turning it on changes nothing about the emitted program, only what a JAX user sees in the cost-analysis output. That "impure" nature is precisely why it sits where it does in the type system. Its value type is `AutoOr<CostModelLoggingOptions>`, a two-field bool proto, but unlike the 330 other `AutoOr` flags in this build it is **not** a `TpuCompilationEnvironment` (TCE) AutoProto field — it is read through the plain `absl::GetFlag` path so that it never enters the hashed, cached, reproducible-compile surface. It is the 31st `AutoOr` type and the only one in the `0x12fc____` band rather than the `0x1d7_____` jellyfish band.

A reader who knows abseil flags should map this to the `ABSL_FLAG(AutoOr<T>, …)` idiom: `AutoOr<T>` is a tri-state (`AUTO` / present-`T`), `FlagImpl+0x58` caches the current value pointer, and `GetFlag` masks a 2-bit init-lock tag off that pointer. The divergence from a normal flag is the `AutoOr` wrapper (a `cmpb $0,+0x28` has-byte test that selects `AUTO` vs the stored sub-message) and the fact that this particular flag's consumer is a cost-model builder, not a TCE resolver. The page covers three things: (1) the flag's storage→resolve→consume→log path centred on `CreateCostModelWindowSettingDelegator`; (2) what the two booleans actually do to the `OpCostManager` analysis log; and (3) the `UnparseFloatingPointVal` / `SimpleAtod` float-text grammar that serialises and parses the *cost values* once logging is enabled — the edge tokens (`inf`, `nan`, scientific, hex-float) a reimplementer must get exactly right.

For reimplementation, the contract is:

- The `CostModelLoggingOptions` 2-bool proto layout and the `AutoOr<CostModelLoggingOptions>` packing (present byte, has byte, sub-message).
- The non-TCE resolve path: the inline `FlagImpl+0x58` / `FlagImpl::Read` `GetFlag` idiom and why it bypasses the TCE AutoProto resolver family.
- The consumer wiring: `CreateCostModelWindowSettingDelegator` and its per-`HloInstruction` closure, including the field#2 dual-window-cost gate.
- What `enable_analysis_logging` (field#1) and `log_codegen_and_non_codegen_window_costs_in_analysis` (field#2) cause `OpCostManager` to emit.
- The float-text round-trip grammar: `UnparseFloatingPointVal<T>` shortest-`%.*g` rendering with reparse-verify, and `SimpleAtod`/`SimpleAtof` ingest with its inf/nan/hex/overflow edge behaviour.

| | |
|---|---|
| **Flag object** | `FLAGS_xla_tpu_impure_cost_model_logging_options` @ `0x22318950` |
| **Flag type** | `AutoOr<CostModelLoggingOptions>` (31st AutoOr type; non-TCE) |
| **FlagOps** | `FlagOps<AutoOr<CostModelLoggingOptions>>` @ `0x12fc0e00` |
| **Default-value gen** | `AbslFlagDefaultGenFor…::Gen` @ `0x12fc1260` → AUTO |
| **AbslUnparseFlag** | `xla::jellyfish::AbslUnparseFlag(AutoOr<…> const&)` @ `0x12fd01a0` |
| **Resolve site** | `RunMemorySpaceAssignment` @ `0x12fc3080` (fast `0x12fc440b` / slow `0x12fc46d3`) |
| **Consumer** | `CreateCostModelWindowSettingDelegator` @ `0x1304e100` |
| **Consumer closure** | per-HLO `DelegationInfo` invoker @ `0x1304ff00` |
| **Window selector** | `ShouldUseCodegenWindows` @ `0x130d3d40` |
| **Log surface** | `OpCostManager::AnalysisLoggingColumns` @ `0x1e474c00` / `AnalysisLoggingLine` @ `0x1e475d20` |
| **Float unparse** | `UnparseFloatingPointVal<float>` @ `0x21113460` / `<double>` @ `0x211135a0` |
| **Float ingest** | `SimpleAtof` @ `0x21171440` / `SimpleAtod` @ `0x21171580` |

---

## The CostModelLoggingOptions Proto

### Purpose

`CostModelLoggingOptions` is a two-field all-bool message. Its only job is to carry, in one `AutoOr` flag, the two switches that govern cost-model logging verbosity. The schema is recovered from the generated `_InternalSerialize` and the `TcParseTable`; both field names (`enable_analysis_logging`, `log_codegen_and_non_codegen_window_costs_in_analysis`) appear verbatim as `.rodata` descriptor strings.

```c
// xla::jellyfish::CostModelLoggingOptions
message CostModelLoggingOptions {
    optional bool enable_analysis_logging = 1;                                   // value byte @ +0x18
    optional bool log_codegen_and_non_codegen_window_costs_in_analysis = 2;       // value byte @ +0x19
}
// InternalMetadata @ +0x08 ; has-bits uint32 @ +0x10 (bit0=field#1, bit1=field#2)
```

### Encoding

The byte layout is confirmed against `CostModelLoggingOptions::_InternalSerialize` @ `0x1db24760`. The generated code reads the has-bits word at `*((DWORD*)this + 4)` (offset `+0x10`), then for field#1 (wire tag `0x08`) emits the byte at `this+24` (`+0x18`) and for field#2 (wire tag `0x10`) emits the byte at `this+25` (`+0x19`):

```c
function _InternalSerialize(this, out):                 // sub_1DB24760
    hasbits = *(u32*)(this + 0x10)
    if (hasbits & 1) && *(u8*)(this + 0x18) == 1:        // field#1 set & true
        emit 0x08, *(u8*)(this + 0x18)                    // tag, value
    if (hasbits & 2) && *(u8*)(this + 0x19) == 1:        // field#2 set & true
        emit 0x10, *(u8*)(this + 0x19)                    // tag, value
```

The `TcParseTable` `_table_` @ `0x21cfa1e8` (size `0x78`) opens with `has_bits_offset 0x10` (first dword) — there are no fields beyond the two booleans. `Clear` is at `0x1db24740`.

> **NOTE —** the two booleans are independent observability levels, not a count. Field#1 (`enable_analysis_logging`) turns the per-op cost dump ON. Field#2 (`log_codegen_and_non_codegen_window_costs_in_analysis`) is a *widener*: it makes the dump emit BOTH the codegen-window and the good-enough-window cost variants so the per-op delta between window strategies is visible. Field#2 has no effect unless field#1 is also set — see [The Consumer Closure](#the-consumer-closure-per-hlo).

---

## AutoOr Storage and the Non-TCE Resolve Path

### Purpose

The flag stores its value as `AutoOr<CostModelLoggingOptions>` — a tri-state wrapper that is either the `AUTO` sentinel or a present `CostModelLoggingOptions`. What makes this flag structurally distinct is *how it is resolved*: through the plain abseil `GetFlag` idiom (`FlagImpl+0x58` cached pointer), not through an `AutoOr<T>::FromProtoOrDie` TCE resolver. There is no `AutoOr<CostModelLoggingOptions>::FromProtoOrDie` and no `AutoOrTypeTraits<CostModelLoggingOptions>::FromAutoProto` symbol in the binary — the entire FromProto resolver family that the 330 TCE AutoProto fields use is absent for this type. That absence is the binary fingerprint of an "impure" flag.

### AutoOr Layout

The `AutoOr<message>` layout is read directly from `AbslUnparseFlag` @ `0x12fd01a0`, which gates on the has-byte and copies the variant:

| Field | Offset | Meaning |
|---|---|---|
| sub-message body | `+0x00` | the embedded `CostModelLoggingOptions` (or a `const Msg*` variant) |
| variant index | `+0x20` | `variant<Msg, const Msg*>` discriminator |
| has byte | `+0x28` | `0` ⇒ `AUTO` / default; nonzero ⇒ a present message |

The default-value generator confirms `AUTO`:

```c
function AbslFlagDefaultGenFor…::Gen(this):              // sub_12FC1260
    *(u8*)(this + 0x00) = 0     // present/body byte
    *(u8*)(this + 0x28) = 0     // has byte = 0  ⇒  AUTO
```

### Resolve Algorithm

`RunMemorySpaceAssignment` @ `0x12fc3080` (the `BuildOpCostManager` caller) reads the flag inline at `0x12fc440b` using the canonical `GetFlag(FLAGS_…)` sequence:

```c
function ResolveCostModelLoggingOptions():               // inline @ 0x12fc440b
    p = *(void**)(FLAGS_…cost_model_logging_options + 0x58)   // FlagImpl+0x58 cached ptr
    if (p & 3) != 0:                                          // 2-bit absl init-lock tag set
        goto slow                                            // 0x12fc46d3: FlagImpl::Read @0x21111940
    autoor = (AutoOr<CostModelLoggingOptions>*)(p & ~3)      // mask tag bits
    if *(u8*)(autoor + 0x28) == 0:                           // has byte == 0
        return AUTO/empty default                            // use the default-instance
    idx = *(u8*)(autoor + 0x20)                              // variant index @ +0x20
    return copy_sub_message(autoor)                          // pass to delegator builder
```

The slow path at `0x12fc46d3` calls `absl::flags_internal::FlagImpl::Read(void*)` @ `0x21111940` (the lazy first-touch initialiser). This is the ordinary abseil pattern; the only `AutoOr`-specific part is the `cmpb $0,+0x28` has-byte test and the `+0x20` variant copy.

> **QUIRK —** this flag is `AutoOr`-typed yet absent from the 1121-field `TpuCompilationEnvironment::_table_` (@ `0x21cfa9e0`). Every other `AutoOr` flag in this build (330 of them) is a TCE AutoProto field resolved via `FromProtoOrDie`. A reimplementer who assumes "all `AutoOr` flags are TCE fields" will look for a resolver that does not exist. The `xla_tpu_impure_*` prefix marks flags deliberately excluded from the hashed/cached deterministic TCE surface because they change compiler *observability*, not the compiled result.

### Unparse — AUTO → `"auto"`

`AbslUnparseFlag(AutoOr<CostModelLoggingOptions> const&)` @ `0x12fd01a0` is the round-trip OUT half. If the has-byte (`+0x28`) is set it dispatches to `proto2::Message::AbslUnparseFlagImpl` (TextFormat, `text:`/`serialized:`/`base64:` fallback); if not, it builds the literal `"auto"` from a lazy-static `absl::NoDestructor<std::string>` constructed once (via `__cxa_guard` + `PlacementImpl`) from the `"auto"` source literal shared by every AutoOr unparse instance:

```c
function AbslUnparseFlag(out, autoor):                   // sub_12FD01A0
    if *(u8*)(autoor + 0x28):                            // has byte set
        return Message::AbslUnparseFlagImpl(out, …)      // text:/serialized:/base64:
    once: AutoFlagValue = NoDestructor<string>("auto")   // __cxa_guard-guarded
    return copy(out, AutoFlagValue)                      // "auto"
```

### Function Map

| Function | Address | Role |
|---|---|---|
| `FlagOps<AutoOr<CostModelLoggingOptions>>` | `0x12fc0e00` | the flag-storage TypeId (FlagImpl `+0x20`) |
| `AbslFlagDefaultGenFor…::Gen` | `0x12fc1260` | sets present `+0x00`=0, has `+0x28`=0 → AUTO |
| `AbslUnparseFlag(AutoOr<…>)` | `0x12fd01a0` | AUTO→`"auto"`, present→TextFormat |
| `RunMemorySpaceAssignment` | `0x12fc3080` | resolve site (fast `0x12fc440b` / slow `0x12fc46d3`) |
| `FlagImpl::Read` | `0x21111940` | lazy first-touch slow-path initialiser |

---

## The Consumer — CreateCostModelWindowSettingDelegator

### Purpose

The resolved `CostModelLoggingOptions` is consumed by `CreateCostModelWindowSettingDelegator` @ `0x1304e100`. It builds an `OpCostManager::CalculationNode` named `"CostModelWindowSettingDelegator"` that, per HLO instruction, decides which window-cost strategy to charge — and, when logging is enabled, charges *both* so the analysis dump can show the delta.

### Signature and Captures

The function signature (confirmed from the demangled symbol) is:

```c
CreateCostModelWindowSettingDelegator(
    string_view name,
    CostModelFlagOptions const& flag_opts,
    CostModelLoggingOptions const& log_opts,          // the resolved AutoOr value
    unique_ptr<CalculationNode> codegen_node,         // "CostModelWithCodegenWindows"
    unique_ptr<CalculationNode> good_enough_node)     // "CostModelWithGoodEnoughWindows"
```

It constructs an `AnyInvocable` closure that captures, **by value**, the `CostModelFlagOptions` (capture `+0x00`) and the `CostModelLoggingOptions` (capture sub-object `+0x48`, so field#2's byte at `msg+0x19` lands at capture `+0x61`), plus the two child calculation nodes. The two child node name strings are `"CostModelWithCodegenWindows"` and `"CostModelWithGoodEnoughWindows"`; the delegator node name is `"CostModelWindowSettingDelegator"`; the leaf cost source it routes is `"TpuHloCostAnalysis"` — see [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md).

### The Consumer Closure (per-HLO)

The per-`HloInstruction` invoker @ `0x1304ff00` is a `RemoteInvoker` returning `CalculationNode::DelegationInfo` and taking `(HloInstruction const&, bool)`. The bool is an "is-analysis-logging pass" flag — it is the gate that, combined with field#2, triggers the dual push. `cap` below is the lambda capture struct (its first qword loaded into `v7 = *a2`): `cap+0x00` is the `CostModelFlagOptions`, `cap+0x48` the `CostModelLoggingOptions`, and `cap+0x68`/`cap+0x70` the two captured child `CalculationNode` pointers. `result` is the returned `DelegationInfo`, two `vector<uint64>` at `result+0x00` (codegen) and `result+0x18` (good-enough). The decompiled body:

```c
function WindowSettingClosure(cap, hlo, is_logging_pass): // sub_1304FF00
    result = {}                                          // zero two slot vectors @ +0x00,+0x18
    if ShouldUseCodegenWindows(hlo, cap.flag_opts):      // sub_130D3D40
        result.codegen.push_back(cap + 0x68)             // charge codegen node ptr
        if !is_logging_pass:                  return result
        if *(u8*)(cap + 0x61) == 0:           return result // field#2 clear (cap+0x48 +0x19)
        result.good_enough.push_back(cap + 0x70)         // ALSO charge good-enough node ptr
    else:
        result.codegen.push_back(cap + 0x70)             // charge good-enough node ptr
        if is_logging_pass && *(u8*)(cap + 0x61):
            result.good_enough.push_back(cap + 0x68)     // ALSO charge codegen node ptr
    return result
```

> **GOTCHA —** the decompiler renders `cap+0x68`, `cap+0x70`, and the field#2 byte `cap+0x61` all against the same base register (`v7`), which makes the two `push_back` arguments look like reads off the `HloInstruction`. They are not: `v7` is the *capture* struct, `+0x68`/`+0x70` are the two captured child-node pointers being pushed into the result vectors (`_RDI` and `_RDI+3`), and `+0x61` is field#2 inside the captured `CostModelLoggingOptions` (capture `+0x48` + msg byte `+0x19` = `+0x61`). The `HloInstruction&` arrives as the forwarded parameter and is passed straight into `ShouldUseCodegenWindows`. The capture layout is fixed by `CreateCostModelWindowSettingDelegator` @ `0x1304e100`, which `operator new(0x78)`s the capture and copies `CostModelFlagOptions` to `+0x00`, `CostModelLoggingOptions` to `+0x48`, and the child pointers to `+0x68`.

The net behaviour: with field#2 clear, exactly one window-cost variant is charged per op (the one `ShouldUseCodegenWindows` selects). With field#2 set **and** the logging pass active, both variants are charged, so the analysis log can print the codegen-vs-good-enough cost delta per op.

### Window Selector — ShouldUseCodegenWindows

`ShouldUseCodegenWindows` @ `0x130d3d40` decides eligibility. It reads a repeated fusion-window enum list off `CostModelFlagOptions` (pointer/count near `+0x30`), and for each enum value jump-tables on the HLO fusion kind:

```c
function ShouldUseCodegenWindows(hlo, flag_opts):        // sub_130D3D40
    if (hlo.flags & 1) == 0:               return false   // not a fusion
    list = flag_opts.window_kinds_ptr; n = flag_opts.window_kinds_count
    for kind in list[0..n]:
        switch kind:
            case 0:  return true                          // sentinel/terminator
            case 1:  if hlo.IsOutputFusion():  return true // @0x1e5a2fc0
            case 2:  if fusion_util::IsConvLowerable(hlo): return true  // @0x14553620
            case 3:  if hlo.IsLoopFusion():    return true // @0x1e5a2fa0
    return false
```

`NeverUseCodegenWindows` @ `0x130d3e80` is the negation/override sibling, also called from `BuildOpCostManager`.

### Function Map

| Function | Address | Role |
|---|---|---|
| `CreateCostModelWindowSettingDelegator` | `0x1304e100` | builds the delegator node; captures `CostModelLoggingOptions` by value |
| window-setting closure (`RemoteInvoker`) | `0x1304ff00` | per-HLO `DelegationInfo`; field#2 (`+0x61`) gates dual charge |
| `ShouldUseCodegenWindows` | `0x130d3d40` | codegen-window eligibility (fusion-kind jump-table) |
| `NeverUseCodegenWindows` | `0x130d3e80` | negation/override sibling |
| `CostModelLoggingOptions` ctor (capture) | `0x1db24640` | copies the message into capture `+0x48` |

---

## What Gets Logged

### Field#1 — enable_analysis_logging

When field#1 is set, the cost-model run emits a tabular per-op dump through two `OpCostManager` methods:

- `OpCostManager::AnalysisLoggingColumns() const` @ `0x1e474c00` — emits the header row (the column labels: cost-metric IDs).
- `OpCostManager::AnalysisLoggingLine(CostMetricId const&, CalculationNode::Result const&) const` @ `0x1e475d20` — emits one row per `(cost metric, HLO op)`, reading the `Result` each calculation node produced.

So the observable side-effect of field#1 is: for every HLO op, the cost values produced by each calculation node (`TpuHloCostAnalysis`, the codegen-window node, the good-enough-window node) are dumped to the compile log. No emitted code changes — this is pure compile-time observability.

### Field#2 — log_codegen_and_non_codegen_window_costs_in_analysis

Field#2 widens those rows. As shown in [The Consumer Closure](#the-consumer-closure-per-hlo), it makes the window-setting delegator charge *both* the codegen-window and the good-enough-window cost into the analysis pass, so each logged row carries both variants and the per-op codegen-vs-good-enough delta is visible.

> **NOTE —** the exact `OpCostManager` member offset where field#1 latches into the per-pass "emit `AnalysisLoggingColumns`/`Line`" decision is not pinned here (the delegator captures the whole message; the field#1 read site that gates the log emission lives in the `OpCostManager` metric-value / compute path, `GetMetricValue` @ `0x1e475160` / `ComputeSeconds` @ `0x1e475a40`). Field#1 enables the dump and field#2 widens it; the precise latch offset is the one open seam.

### Function Map

| Function | Address | Role |
|---|---|---|
| `OpCostManager::AnalysisLoggingColumns` | `0x1e474c00` | header row (column labels) |
| `OpCostManager::AnalysisLoggingLine` | `0x1e475d20` | one row per (metric, HLO) |
| `OpCostManager::GetMetricValue` | `0x1e475160` | metric read path (field#1 latch — not traced) |
| `OpCostManager::ComputeSeconds` | `0x1e475a40` | per-op seconds compute (not traced) |

---

## The Float-Text Grammar

The logged cost values are floats and doubles, and they cross the text boundary twice: serialised out by `UnparseFloatingPointVal<T>` and parsed back by `SimpleAtof`/`SimpleAtod`. This is the same float-flag grammar that any `AutoOr<float>`/`AutoOr<double>` knob uses, and its edge tokens (`inf`, `nan`, scientific, hex-float) are where a naive reimplementation diverges.

### Unparse — Shortest-Round-Trip `%.*g`

`UnparseFloatingPointVal<float>` @ `0x21113460` and `<double>` @ `0x211135a0` render the **shortest decimal that reparses to the exact same bit pattern**. The algorithm is a try-low-precision-then-verify-then-bump:

```c
function UnparseFloatingPointVal<float>(value):          // sub_21113460
    s = FormatPack("%.*g", 6, value)                     // 6 sig-figs (try short)
    if (bits(value) & 0x7FFFFFFF) >= 0x7F800000:         // exponent all-ones = inf/nan
        return s                                         // accept libc %g "inf"/"-inf"/"nan", SKIP verify
    if SimpleAtof(s, &v) && v == value:                  // vucomiss exact reparse check
        return s                                         // 6 sig-figs reparses exactly
    return FormatPack("%.*g", 9, value)                  // FLT_DECIMAL_DIG = 9, guaranteed exact
```

`<double>` is identical with precision 15 then 17 (`DBL_DECIMAL_DIG = 17`), the inf/nan test `bits & 0x7FFFFFFFFFFFFFFF >= 0x7FF0000000000000`, and `SimpleAtod` + `vucomisd` for the reparse verify.

> **QUIRK —** the precision constants are byte-walked from the disassembly: the float path materialises `movq $0x6,-0x68(%rbp)` @ `0x21113474` for the short try and `movq $0x9,-0x68(%rbp)` @ `0x2111352b` for the fallback; the double path materialises `$0xf` (15) @ `0x211135b4` and `$0x11` (17) @ `0x2111367d`. The decompiled C renders the precision as the literal `4` because that `4` is the `mov $0x4,%edx` *argument count* for `FormatPack`, not the `%.*g` precision — the precision is passed in the format-arg pack on the stack at `-0x68`, which the decompiler folds away. Trust the disassembled `0x6`/`0x9` and `0xf`/`0x11` over the rendered `4`.

The consequence: a `float` whose nearest-6-digit decimal already reparses exactly (e.g. the stored `1.10000002f` whose shortest spelling is `1.1`) prints `"1.1"`. A value that needs all 9 digits falls back to 9 sig-figs. No trailing-zero noise; the canonical `%g` spelling is used, and `inf`/`-inf`/`nan` are emitted directly from libc `%g` via the exponent-all-ones bypass without any reparse attempt.

### Ingest — SimpleAtof / SimpleAtod

`SimpleAtof` @ `0x21171440` and `SimpleAtod` @ `0x21171580` parse a string-view to a float/double. The decompile of `SimpleAtod` confirms the four-stage grammar:

```c
function SimpleAtod(begin, end, out):                    // sub_21171580
    *out = 0
    strip leading  ws while kPropertyBits[c] & 8         // table @ 0xbe7fb70, bit 0x8 = whitespace
    strip trailing ws while kPropertyBits[c] & 8
    if empty:                              return false   // nothing left
    if *p == '+':                                         // bare-sign guard
        if length==1 || p[1]=='-':         return false
        p++                                              // skip the '+'
    q = from_chars(p, end, out, /*fmt=*/3)               // @0x2116a340, chars_format = general (3)
    if errc(q) == 22 || q.ptr != end:                    // 22 = invalid_argument; or trailing garbage
        return false
    if errc(q) == 34:                                    // 34 = result_out_of_range (overflow)
        clamp *out to ±inf                               // sign via vucomisd vs ±1.0 constants
    return true                                          // full-consume, value (or clamped inf) stored
```

`from_chars` is called with `chars_format = 3` = `scientific | fixed` (the **general** format). The hex bit (`0x4`) is clear, so the hex-float branch (`test $0x4,%cl` @ `0x2116a373`/`0x2116a380` inside `from_chars`) is never taken: a `0x`/`0X` prefix leads to a parse failure, not a hex parse.

> **GOTCHA —** the two `errc` codes are easy to transpose. `errc(q) == 22` is `std::errc::invalid_argument` — a hard **reject** (the function returns `false`). `errc(q) == 34` is `std::errc::result_out_of_range` — the **overflow** case, which is *accepted*: `*out` is clamped to `±inf` (sign chosen by `vucomisd` against the `±1.0` constants at `qword_A2DF230`/`qword_A2DE728`) and the function returns `true`. Both `SimpleAtod` @ `0x21171580` and `SimpleAtof` @ `0x21171440` share this exact `!=22 && ptr==end`, then `==34` structure.

### Edge-Token Table

The accepted/rejected token set for any `float`/`double` AutoOr knob value (the exact inf/nan keyword spelling set is whatever abseil's `from_chars` keyword path accepts):

| Token | Ingest | Unparse renders |
|---|---|---|
| `auto` | AUTO sentinel | `"auto"` |
| `1.5` / `-0.25` | OK (general) | shortest `%g` (6/15 then 9/17 sig-figs) |
| `1e9` / `1E-3` | OK (scientific) | shortest `%g` |
| `inf` / `-inf` / `Infinity` | OK → ±inf | `"inf"` / `"-inf"` |
| `nan` / `NAN` / `nan(0x1)` | OK → nan(payload) | `"nan"` |
| overflow (e.g. `1e400`) | OK → CLAMP to ±inf | `"inf"` / `"-inf"` |
| `0x1.8p3` / `0x10` (hex-float) | REJECT (`fmt` has no hex bit) | n/a (never stored) |
| `""` / `"+"` / `"-"` / `"  "` | REJECT (empty / bare sign) | n/a |
| leading/trailing whitespace | stripped (`kPropertyBits & 0x8`) | n/a |

> **GOTCHA —** float/double AutoOr knobs accept `inf`/`nan` and clamp overflow to ±inf, but **reject hex-float** — unlike *integer* AutoOr knobs, which accept `0x`-radix via `safe_strto*_base`. A reimplementer who routes all numeric knobs through one parser will wrongly accept `0x10` as `16.0` for a float knob. Also note the radix asymmetry on the integer side: int knobs ingest hex but always *unparse* as decimal. The float unparse is never radix-ambiguous (always `%g` decimal).

### Function Map

| Function | Address | Role |
|---|---|---|
| `UnparseFloatingPointVal<float>` | `0x21113460` | shortest `%.*g` 6→9, inf/nan bypass, reparse-verify |
| `UnparseFloatingPointVal<double>` | `0x211135a0` | shortest `%.*g` 15→17, inf/nan bypass |
| `SimpleAtof` | `0x21171440` | WS-strip + sign-guard + `from_chars(fmt=3)` float |
| `SimpleAtod` | `0x21171580` | WS-strip + sign-guard + `from_chars(fmt=3)` double |
| `from_chars` (float) | `0x2116ada0` | general format; hex bit clear; ±inf clamp constants |
| `from_chars` (double) | `0x2116a340` | general format; `nan@plt` keyword path |
| `kPropertyBits` | `0xbe7fb70` | ASCII property table; bit `0x8` = whitespace |

---

## The Sibling Non-TCE AutoOr Flags

`xla_tpu_impure_cost_model_logging_options` is one of exactly five `AutoOr`-typed flags that are NOT TCE AutoProto fields. The other four share the "read via `GetFlag`, not via a TCE resolver" property. They are listed here because a reimplementer enumerating the `AutoOr` flag surface must account for them — the perfect `330 ↔ 330` `AutoOr ↔ AutoProto` identity has exactly these five residuals.

| Flag | FlagOps | Inner type | Default | Consumer |
|---|---|---|---|---|
| `xla_tpu_impure_cost_model_logging_options` | `0x12fc0e00` | `CostModelLoggingOptions` | AUTO | `CreateCostModelWindowSettingDelegator` @ `0x1304e100` |
| `xla_tpu_impure_use_iteration_mask` | `0x1d6b5840` | bool | AUTO (=ON) | `ShouldUseIterationMask` @ `0x1d6b5dc0` |
| `xla_tpu_comparison_mode_target_module_regex` | `0x1d700400` | string | AUTO | `EnableComparisonMode` @ `0x1d6b8ec0` (RE2 vs module name) |
| `xla_tpu_enable_lem_scheduler` | `0x1d6b5840` | bool | AUTO | registry-mediated (no direct FLAGS_ xref) |
| `xla_tpu_explicit_evict_memory_limit_kib` | `0x1d700120` | int64 | AUTO | registry-mediated (no direct FLAGS_ xref) |

> **NOTE —** `xla_tpu_impure_use_iteration_mask` has polarity AUTO=ON: its consumer reads `FlagImpl+0x58`, `and $0x101 ; cmp $0x100 ; setne` — true unless the user explicitly sets `=false`, and additionally gated on TC version ≥ 3. `enable_lem_scheduler` and `explicit_evict_memory_limit_kib` have NO direct `lea FLAGS_…` reference in `.text`; their effective reads go through the absl flag registry by some inlined `GetFlag<T>` path that a static FLAGS_-address scan cannot pin.

---

## Related Components

| Component | Relationship |
|---|---|
| [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) | the leaf cost source (`"TpuHloCostAnalysis"`) the delegator routes; its float costs are what the log grammar serialises |
| [Cost Model Overview](overview.md) | the per-gen cost-model architecture whose `OpCostManager` runs the logged calculation-node tree |
| [Learned Cost-Model Client](learned-cost-model-client.md) | a sibling `AutoOr<message>` cost-model knob (`EmitterLearnedCostModelOptions`); why the shipped model is data-table driven |
| [Resource Enum (23-slot)](resource-enum.md) | the `ResourceVector` model the cost values ultimately reduce over |

## Cross-References

- [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) — the HLO cost leaf whose flop/byte/transcendental floats are dumped when field#1 is set
- [Cost Model Overview](overview.md) — the three-family cost-model architecture and the `OpCostManager` that hosts the delegator node
- [Learned Cost-Model Client](learned-cost-model-client.md) — the other non-default `AutoOr<message>` cost knob, and the data-table-vs-ML status
- [Resource Enum (23-slot)](resource-enum.md) — the resource-cycle model the logged per-op costs are computed against
- [AutoOr Unparse](../config/autoor-unparse.md) — the full `AbslUnparseFlag<AutoOr<T>>` family this float unparse is one arm of
- [AutoOr Parse Grammar](../config/autoor-parse-grammar.md) — the `ParseAutoOrFromString` ingest dispatcher and the AUTO sentinel
- [Registry-Mediated Flags](../config/registry-mediated-flags.md) — the `enable_lem_scheduler` / `explicit_evict_memory_limit_kib` sibling non-TCE flags
- [Flag Families](../config/flag-families.md) — the `xla_tpu_impure_*` observability-flag class and the TCE-vs-non-TCE storage split
