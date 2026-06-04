# AutoProto / AutoOr Resolution

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

A TPU compile knob with `AUTO` / `ENABLED` / `DISABLED` semantics is not stored as a plain `bool` or `int` in the `TpuCompilationEnvironment` (TCE). It is stored as a pointer to an `xla::jellyfish::AutoProto` — a single-`oneof` message whose active arm carries the explicit value, or whose `oneof_case_ == 0` means *unset* (AUTO). The bridge from that proto to a concrete compile decision is the `AutoOr<T>` template: `AutoOr<T>::FromProtoOrDie` reads the oneof, packs the result into a *present-bit / value* code, and a hand-written per-knob accessor collapses that code into the value the compiler actually uses. This page owns that resolution model — the `AutoOr<T>` wrapper, its present-bit packing, the per-knob `AUTO`→default polarity, and the `ObjectView<TCE>` accessor band that dispatches it.

The reference frame is a tri-state option (`std::optional<bool>` with a documented fallback) but with two libtpu-specific twists a reimplementer must reproduce exactly. First, the present-bit is *packed into the same machine word as the value*, at a type-class-dependent position: bit 8 for `bool`, bit 32 for `int32`/enum, a paired `dl` has-byte for `int64`, a separate has-flag for `string`/message. A consumer never tests an `optional::has_value()`; it tests a bit at a fixed position in the packed return. Second — and this is the part with no XLA-CPU/GPU analogue — the `AUTO`→concrete fallback is *not* a property of the field. It is a property of the *consumer*: the same all-AUTO default instance (`AutoProto_globals_`) feeds every unset knob, and two different consumer idioms (`AUTO=off` vs `AUTO=on`) read that same `0x000` packed code to opposite booleans. The bit polarity baked into each accessor *is* the documented default.

The page is structured as: the wire/memory layout of `AutoProto`; the `AutoOr<T>::FromProtoOrDie` resolver template and its per-type-class packing; the per-knob `AUTO`→concrete fallback idioms (the two bool polarities, the int64 sentinel table, the enum-0 rule, the message default-instance rule); the `ObjectView<TCE>` accessor band that hosts all 130 resolvers; and the second, inline-`TristateProto` storage class that shares the tri-state semantics but not the `AutoProto` mechanism. The string-parse ingest (`auto`/`enabled`/`disabled` → `AutoProto`) is on [autoor-parse-grammar.md](autoor-parse-grammar.md); the reverse text on [autoor-unparse.md](autoor-unparse.md); the 12 message-arm sub-defaults on [autoproto-message-arms.md](autoproto-message-arms.md).

For reimplementation, the contract is:

- **The `AutoProto` layout** — oneof body at `+0x10`, discriminator `oneof_case_` at `+0x1c`, `oneof_case_ == 0` ⇒ AUTO; the all-zero default instance `AutoProto_globals_` is the fallback for any null TCE field.
- **The `AutoOr<T>` packed return** — present-bit position per type-class, and that `AUTO` returns the all-zero code (`0x000`) with the present-bit clear.
- **The per-knob polarity** — that the consumer, not the field, decides what `AUTO` resolves to; the 45/26 bool split, the 18-entry int64 sentinel set, and the enum-0 / message-default rules.

| | |
|---|---|
| **Resolver template** | `AutoOr<bool>::FromProtoOrDie @ 0xf795300` — `if (oneof_case_==0) return 0; else return val \| 0x100` |
| **Arm reader** | `AutoOrTypeTraits<bool>::FromAutoProto @ 0xf7953e0` — checks `+0x1c==1`, reads body `+0x10` |
| **`AutoProto` layout** | oneof body `+0x10` (8 B), `oneof_case_` `+0x1c` (u32); `0`=AUTO. Oneof name `"value"` (str `@ 0x867f0e9`) |
| **Default instance** | `AutoProto_globals_ @ 0x223c8968` — body `+0x10 = 0`, case `+0x1c = 0` ⇒ all-AUTO |
| **Parse table / globals** | `AutoProto::_table_ @ 0x21cfa788` · `AutoProto_globals_ @ 0x223c8968` |
| **Resolver accessor band** | `0x1d6b6420 .. 0x1d6b9f60` — 130 `ObjectView<TpuCompilationEnvironment>` single-field accessors |
| **Type-class packing** | bool `(present<<8)\|val8` · int32/enum `(present<<32)\|val32` · int64 `{rax=val, dl=has}` · string/msg `{val, has-byte}` |
| **Polarity census** | 45 `AUTO=off` bool · 26 `AUTO=on` bool · 18 int64 sentinel · 11 enum/int bt-32 · 2 composite · + msg/enum arms |
| **Second storage class** | 67 inline `TristateProto.Value` enums — `cmpl $2,+OFF` (`ENABLED==2`), *not* `AutoProto` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The `AutoProto` Message

### Purpose

`AutoProto` is the storage cell for one tri-state knob. It is a protobuf message with exactly one `oneof` (named `"value"`), whose 30 arms cover every value type a TCE knob can carry — 8 scalar types, 11 enums, and 12 sub-messages. An active arm holds the explicit user value; an inactive oneof (`oneof_case_ == 0`) means the knob was left at `AUTO`. The TCE holds these as `AutoProto*` pointer fields, one per tri-state knob (~330 of the 1121 TCE fields). The resolver reads the layout below; the 30-arm dispatch and the sub-message arms are owned by [autoproto-message-arms.md](autoproto-message-arms.md).

### Memory Layout

Every `AutoOrTypeTraits<T>::FromAutoProto` reader and every consumer accessor reads the same two slots, so the layout is fixed and byte-exact:

```text
AutoProto object
  +0x00   protobuf message header (vtable / arena / unknown-fields)
  +0x10   oneof body   (8 bytes)  ── scalar value, or sub-message pointer
  +0x1c   oneof_case_  (uint32)   ── active arm's AutoProto FIELD NUMBER; 0 = unset = AUTO
```

The discriminator at `+0x1c` is the *field number* of the active arm, not a 0-based index: bool is arm #1, int64 #2, uint64 #3, int32 #4, uint32 #5, double #6, float #7, string #8, then the enum and message arms at higher numbers (see [autoproto-message-arms.md](autoproto-message-arms.md)). The body at `+0x10` overlays all arms — a `bool` reads one byte there, an `int64` reads eight, a `double` reads via `vmovsd`, a sub-message reads a pointer. The reader keys on `oneof_case_` to know how to interpret the eight bytes.

> **NOTE —** the `+0x1c` offset is `4*7` words from the object base — visible in the decompile as `*((_DWORD *)a1 + 7)` (the bool resolver) and `*(_DWORD *)(a2 + 28)` (the bool reader). Do not confuse it with the proto's `_has_bits_`; a oneof has no has-bits, the case field *is* the presence indicator.

### The all-AUTO default instance

```text
AutoProto_globals_   @ 0x223c8968   (.data file off 0x21fc8968)
  +0x10  oneof body  = 0x0
  +0x1c  oneof_case_ = 0            ⇒ AUTO
```

This is the single default instance every unset knob points at. When a TCE `AutoProto*` field is null (the env never set it), the consumer substitutes `&AutoProto_globals_` before calling the resolver — confirmed in every accessor as `if (!v1) v1 = &AutoProto_globals_;`. Because its `oneof_case_` is `0`, the resolver returns the all-zero packed code (`0x000`), and the consumer's polarity decides the concrete default. This is why the default of a tri-state knob is not a stored number — it is a *resolution rule* in the consumer (§3).

---

## 2. The `AutoOr<T>` Resolver Template

### Purpose

`AutoOr<T>::FromProtoOrDie(const AutoProto&)` is the one function that turns an `AutoProto` into a packed `(present, value)` code. It is templated on the C++ value type; the binary carries a distinct instantiation per type-class, each with the same skeleton but a type-specific packing. The `OrDie` suffix is a misnomer for the live path — the `AUTO` guard returns before the fatal path can be reached.

### Entry Point

```text
<accessor>(ObjectView<TpuCompilationEnvironment>)   ── 0x1d6b6420..0x1d6b9f60
  ├─ load env[+OFF]                                  ── the AutoProto* for this knob
  ├─ if null → &AutoProto_globals_                   ── 0x223c8968 (all-AUTO)
  ├─ AutoOr<T>::FromProtoOrDie(autoproto)            ── 0xf795300 (bool) / 0x1092f7e0 (int64) / …
  │     └─ AutoOrTypeTraits<T>::FromAutoProto        ── 0xf7953e0 (bool); checks +0x1c, reads +0x10
  └─ apply consumer polarity                         ── bit test on the packed return (§3)
```

### Algorithm

The bool resolver `@ 0xf795300` is the canonical body; the other type-classes differ only in the final pack:

```c
function AutoOr<bool>::FromProtoOrDie(AutoProto* p):     // 0xf795300
    if p->oneof_case_ == 0:                              // *((_DWORD*)p + 7)  == +0x1c
        return 0                                         // AUTO ⇒ packed 0x000, present-bit clear
    sor = AutoOrTypeTraits<bool>::FromAutoProto(p)       // 0xf7953e0 → StatusOr<bool>
    if not sor.ok():                                     // unreachable: case!=0 guarantees the arm is set
        LOG(FATAL) << "Failed to convert AutoProto into an AutoOr: "  // flag_types.h:845
                   << sor.status() << "\nProto: " << AutoProtoToStr(p)
    val8 = sor.value                                     // the byte read at p+0x10 by the traits reader
    return val8 | 0x100                                  // SET present-bit (bit8); pack (present<<8)|val8
```

The arm reader confirms the layout and the presence semantics:

```c
function AutoOrTypeTraits<bool>::FromAutoProto(out, AutoProto* p):   // 0xf7953e0
    if p->oneof_case_ != 1:                              // *(_DWORD*)(p+28) != 1  — bool is arm #1
        out = MakeError("bool is not set in AutoProto: " + AutoProtoToStr(p))   // flag_types.h:447
        return                                           // StatusOr not-ok
    out.value  = *(byte*)(p + 0x10)                      // body byte
    out.ok     = 1                                       // StatusOr ok-slot
```

> **QUIRK —** `FromProtoOrDie` guards on `oneof_case_ == 0` *before* calling the reader, and the reader fatals only when the case is some *other* arm's number (a type mismatch, e.g. an int64 arm read by the bool reader). The `LOG(FATAL)` path is therefore dead for a well-typed knob: an unset oneof returns `0x000`, a correctly-typed set oneof returns `val | 0x100`. A reimplementer can omit the fatal branch and keep behavior, but the *two-bit code* — present in bit 8, value in bit 0 — must be reproduced exactly, because consumers test it positionally.

### Packed-return representation per type-class

The present-bit lives at a different position per type-class, because the value occupies different widths and the pack must not collide:

| Type-class | Resolver | Packed return | Present-bit | Verified |
|---|---|---|---|---|
| `bool` | `AutoOr<bool>::FromProtoOrDie @ 0xf795300` | `(present<<8) \| val8` | bit 8 (`\| 0x100`) | CONFIRMED — `return v11 \| 0x100u` |
| `int32` / enum (`_Value`) | `AutoOr<int>::FromProtoOrDie @ 0x10979760` · `AutoOr<EffortLevel>::FromProtoOrDie @ 0x109294a0` | `(present<<32) \| val32` | bit 32 (`\| 0x100000000`) | CONFIRMED — `return v11 \| 0x100000000LL` |
| `int64` | `AutoOr<long>::FromProtoOrDie @ 0x1092f7e0` | `{rax = value, dl = has}` | paired `dl` byte (`test $1,%dl`) | CONFIRMED — returns `v10[1]`, has-bit in `dl` |
| `uint32` / `uint64` / `double` / `float` | analogous instantiations | value in low bits, present in width's top bit / paired has | per type | HIGH (idiom matches bool/int) |
| `string` / message | message traits (§ message arms) | `{value-or-submessage, separate has-byte}` | separate has-byte | HIGH (struct-return, has in `+0x58`) |

The enum case is verified against `AutoOr<ExecutionOptions_EffortLevel>::FromProtoOrDie @ 0x109294a0`, whose body is byte-identical to the bool resolver except for the final `return v11 | 0x100000000LL` — the present-bit moved to bit 32 because the enum value occupies the low 32. The int64 case `@ 0x1092f7e0` returns `v10[1]` (the value register) and carries the has-bit in `dl`, the classic two-register `StatusOr`-style return, so its consumers test `dl & 1` rather than a bit in the value word.

> **GOTCHA —** there is no single `AutoOr<T>` ABI. A reimplementation that returns a uniform `{bool present; int64 value}` struct for every type will mismatch the consumer, because the consumers were compiled against the *packed* return and test a fixed bit (`0x100`, `0x100000000`, or `dl & 1`). The present-bit position is part of the contract, not an implementation detail.

---

## 3. The `AUTO` → Concrete Fallback

### Purpose

When the oneof is unset, `FromProtoOrDie` returns the all-zero packed code. The consumer accessor turns that code into a concrete compile value, and *the consumer hardcodes what `AUTO` becomes*. There is no field-level default table; the default is an instruction sequence at the end of each accessor. Five idioms cover the entire census.

### Idiom A — `AUTO=off` bool (45 knobs)

Only an explicit `true` enables; `AUTO` and an explicit `false` both resolve to `false`.

```c
function MxuLatencyBalancingUseSequenceDependencies(env):   // 0x1d6b9c80
    p = env[+0xbe8]                                          // the AutoProto*
    if !p: p = &AutoProto_globals_                           // 0x223c8968
    code = AutoOr<bool>::FromProtoOrDie(p)                   // 0x000 if AUTO, val|0x100 if set
    return (~code & 0x101) == 0                              // true IFF present(bit8) AND value(bit0)
```

`(~code & 0x101) == 0` is true only when *both* bit 8 (present) and bit 0 (value) are set — i.e. the packed code is exactly `0x101`. `AUTO` (`0x000`) and present-false (`0x100`) both yield `false`. So **`MxuLatencyBalancingUseSequenceDependencies` defaults OFF** in v0.0.40 — the all-AUTO `AutoProto_globals_` resolves to `0x000`, fails the `== 0x101` test, returns `false`. The 45-knob `AUTO=off` set (offsets in [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md)) all share this `not; test $0x101; sete` body — among them `EnablePipelinedLoopUnrolling` (`+0x2f0`), `EnableIlpLatencyHidingScheduler` (`+0x648`), `ForceAsyncAllToAll` (`+0xbc8`), `EnableDataDependentScOpAggregation` (`+0xc40`).

### Idiom B — `AUTO=on` bool (26 knobs)

Only an explicit `false` disables; `AUTO` and an explicit `true` both resolve to `true`.

```c
function AllowSplitVmem(env):                               // 0x1d6b70a0
    p = env[+0x4a8]
    if !p: p = &AutoProto_globals_
    code = AutoOr<bool>::FromProtoOrDie(p)
    return (code & 0x101) != 0x100                          // false ONLY when present AND value==0
```

`(code & 0x101) != 0x100` is false only for the exact code `0x100` (present, value 0). `AUTO` (`0x000`) and present-true (`0x101`) both yield `true`. So **`AllowSplitVmem` defaults ON**, backed by the *same* all-AUTO `AutoProto_globals_` as the OFF-defaulting knob above. The 26-knob `AUTO=on` set shares this `and $0x101; cmp $0x100; setne` body — including `EnableMsaSyncCopyReplacement` (`+0x2f8`), `EnableCollectivePipeliner` (`+0x8a8`), `EnableScsOverlays` (`+0xc50`), `IsMosaicCompatibilityModeEnabled` (`+0x470`).

> **QUIRK —** the same packed code `0x000` resolves to opposite booleans in Idiom A and Idiom B. The polarity is not in the field, the proto, or the default instance — it is the literal arithmetic at the end of the accessor. A reimplementer cannot derive a knob's default from its storage; it must read the consumer's test. This is the single most important fact on the page: **the bit polarity is the documented default.**

### Idiom C — `AUTO` → int64 sentinel (18 knobs)

The int64 resolver returns the value in `rax` and the has-bit in `dl`. The consumer substitutes a per-knob constant when the has-bit is clear:

```c
function DcnTransferCountThreshold(env):                    // 0x1d6b64e0
    p = env[+0xbd0]
    if !p: p = &AutoProto_globals_
    value = AutoOr<long>::FromProtoOrDie(p)                 // value in rax, has-bit in dl
    if (has & 1) == 0:                                      // AUTO ⇒ has clear
        return 0x7FFFFFFFFFFFFFFF                           // INT64_MAX sentinel
    return value
```

The `AUTO` sentinel is per-knob — it is the constant the accessor `cmove`s in. 12 are decoded; the remaining 6 follow the same `test $1,%dl; cmove rcx` idiom with their own constant:

| Knob | Env offset | `AUTO` sentinel | Confidence |
|---|---|---|---|
| `DcnTransferCountThreshold` | `+0xbd0` | `INT64_MAX` (`0x7fffffffffffffff`) | CONFIRMED |
| `IciRsPipeliningThresholdBytes` | `+0xa98` | `INT64_MAX` | HIGH |
| `AllGatherMinBytesForSparseCoreOffload` | `+0xaf0` | `0` | HIGH |
| `AllGatherStepCount` | `+0x8a0` | `1` | HIGH |
| `GatherExpanderConcatElementGatherThreshold` | `+0x600` | `4` | HIGH |
| `RaggedAllToAllMaxRdmaSizeKib` | `+0x658` | `8` | HIGH |
| `SparseCoreOffloadQueuingOverlapLimit` | `+0x738` | `64` | HIGH |
| `RotatedPincerVmemShardCopyLoopIterNum` | `+0xbd8` | `64` | HIGH |
| `MaxNumOperandsToEnableWindowCheck` | `+0xb60` | `128` | HIGH |
| `HostCommandHandlerReapInterval` | `+0xba0` | `1024` | HIGH |
| `AutoMaxMetadataStringLength` | `+0x688` | `100000` | HIGH |
| `MaxFetchAndAddValue` | `+0x8c8` | `1000000000` | HIGH |

### Idiom D — `AUTO` → enum-0 / int-default (11 knobs)

The int32/enum resolver packs `(present<<32)|val32`. The consumer tests bit 32 and substitutes `0` (the DEFAULT/first enum value, or zero) on `AUTO`:

```c
function GetBufferAssignmentAlgorithm(env):                // 0x1d6b9cc0 (pattern)
    p = env[+0xc18]
    if !p: p = &AutoProto_globals_
    code = AutoOr<EnumT_Value>::FromProtoOrDie(p)          // (present<<32)|val32
    if bt(code, 32):                                       // present?
        return (int32)code                                 // the explicit enum/int value
    return 0                                                // AUTO ⇒ enum-0 / zero
```

The `bt $0x20; cmovb; else 0` body covers `GetMlirVerifierOptions` (`+0x978`), `ScHbmSpillStack` (`+0xc68`, int32), `TpuScatterExpanderAutounrollFactor` (`+0x800`, int32), `NumSerializedTablesToOptimizeHbm` (`+0x558`, uint32), the float-typed `SparseCoreMismatchDetectorAtol`/`Rtol` (`+0x340`/`+0x348`), and `SparseCoreElementwiseShapeScalingFactor` (`+0xb48`, float). For the enum case, `0` is the proto's first/default enum value; for the int case, literal zero.

### Idiom E — `AUTO` → message default-instance

Message-typed `AutoProto` arms (the 12 sub-message arms) resolve `AUTO` to the *empty default sub-message*, not a scalar. `GetIlpLatencyHidingSchedulerOptions` resolves `AUTO` to `IlpLatencyHidingSchedulerOptions(arena)` (the empty message) via `FromProtoOrDie @ 0x1d6bb6e0`; `GetSparseCoreAssertLevel` resolves `AUTO` to a code-defined level via `FromProtoOrDie @ 0x1d6bcb20`. The sub-message's *inner* field defaults are owned by [autoproto-message-arms.md](autoproto-message-arms.md) — the AUTO answer here is only "an empty instance of the arm's message type."

### Idiom F — primary/override composite (2 decoded)

A few accessors read *two* `AutoProto` fields, the second overriding the first when explicitly present:

```c
function ShouldEnablePostMsaSyncSliceFusion(env):          // composite
    base     = resolve(env[+0x548], AUTO=off)              // primary knob, Idiom A
    override = AutoOr<bool>::FromProtoOrDie(env[+0x448])
    if (override & 0x100) == 0:                            // override not present
        return base
    return override & 1                                    // explicit override wins
```

`PadOperationsInputTiles` (`+0x618`) is the second decoded composite, following the same present-bit gate. The remaining composites (`EnableMixedPrecisionAddTransform` reading `+0x830`&`+0x840`; `AllReduceMinBytesForSparseCoreOffload` reading `+0x1590`&`+0xa48`) follow the pattern but their second-field semantics were not individually decoded (LOW).

---

## 4. The Resolver Accessor Band

### Purpose

All 130 single-field tri-state resolvers live in one `.text` band, `0x1d6b6420 .. 0x1d6b9f60`, each a free function `xla::jellyfish::<Name>(ObjectView<TpuCompilationEnvironment>)`. They are uniform: load the field's `AutoProto*` at a fixed env offset, null-fallback to `AutoProto_globals_`, call the typed `FromProtoOrDie`, apply one of the §3 idioms. The band is the *readable* compile-knob surface — the human-named entry into the otherwise offset-keyed TCE.

### Dispatch dimensions

The band is a 130-row space, but it is fully described by three axes, so there is no need to dump 130 rows here (offsets and flag names are on [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md)):

| Axis | Values | Source |
|---|---|---|
| Type-class | bool, int64, int32/enum, float, message | the `AutoOr<T>` instantiation called |
| `AUTO` polarity | off / on / sentinel / enum-0 / msg-default / composite | the §3 idiom at the accessor tail |
| Env offset | per-knob, `+0x270 .. +0xcd0` (the `AutoProto*` field) | the `_table_` field#→offset map |

The census across the band: 45 `AUTO=off` bool, 26 `AUTO=on` bool, 18 int64 sentinel, 11 enum/int bt-32, 2 primary/override composite, plus the enum-instance and message-default arms. A handful of multi-field composites read 2+ `AutoProto` fields and are counted separately.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `AutoOr<bool>::FromProtoOrDie` | `0xf795300` | bool resolver — `(present<<8)\|val8` packing | CONFIRMED |
| `AutoOrTypeTraits<bool>::FromAutoProto` | `0xf7953e0` | bool arm reader — `+0x1c==1`, body `+0x10` | CONFIRMED |
| `AutoOr<long>::FromProtoOrDie` | `0x1092f7e0` | int64 resolver — `{rax, dl}` has-pair | CONFIRMED |
| `AutoOr<int>::FromProtoOrDie` | `0x10979760` | int32 resolver — `(present<<32)\|val32` | HIGH |
| `AutoOr<ExecutionOptions_EffortLevel>::FromProtoOrDie` | `0x109294a0` | enum resolver — `\| 0x100000000` | CONFIRMED |
| `AutoProto::_table_` | `0x21cfa788` | parse table for the 30-arm oneof | CONFIRMED |
| `AutoProto_globals_` | `0x223c8968` | all-AUTO default instance | CONFIRMED |
| `MxuLatencyBalancingUseSequenceDependencies` | `0x1d6b9c80` | `AUTO=off` exemplar (`+0xbe8`) | CONFIRMED |
| `AllowSplitVmem` | `0x1d6b70a0` | `AUTO=on` exemplar (`+0x4a8`) | CONFIRMED |
| `DcnTransferCountThreshold` | `0x1d6b64e0` | int64 sentinel exemplar (`+0xbd0`, INT64_MAX) | CONFIRMED |
| `GetBufferAssignmentAlgorithm` | `0x1d6b9cc0` | enum bt-32 exemplar (`+0xc18`) | HIGH |
| `EnableLloLinter` | `0x1d6b6740` | inline-Tristate exemplar (`+0x15ac`, §5) | CONFIRMED |

> **NOTE —** do not confuse the two parse tables. `AutoProto::_table_ @ 0x21cfa788` is the AutoProto message's own `TcParseTableBase`, the descriptor that drives the 30-arm `FromAutoProto` reflection. The TCE's master `_table_ @ 0x21cfa9e0` (the 1121-field#→offset oracle) is a *different* table at a nearby address — they sit adjacent in `.data.rel.ro` because both protos are in the same translation unit. Cross-reference by symbol, not by eyeballing the address.

---

## 5. The Second Storage Class — Inline `TristateProto`

### Purpose

Not every tri-state knob is an `AutoProto`. A second class — 67 fields — stores the tri-state as an *inline* `TristateProto.Value` enum, a 4-byte field directly in the TCE struct, with no `AutoProto` pointer and no `AutoOr<T>` call. The semantics are the same (`AUTO`/`ENABLED`/`DISABLED`) but the mechanism is a direct enum compare. A reimplementer must handle both classes; they are not interchangeable.

### Algorithm

```c
function EnableLloLinter(env):                             // 0x1d6b6740
    return env[+0x15ac] == 2                                // ENABLED == 2; AUTO==0, DISABLED==1 → false
```

There is no resolver, no fallback instance, no present-bit. The enum value is read directly with `cmpl $0x2, +OFF(%rdi); sete`. `ENABLED` is the constant `2`; `AUTO` (`0`) and `DISABLED` (`1`) both compare not-equal, so both resolve to `false`. The `AUTO`→concrete here is the *proto enum default* of the field (the `TristateProto` census splits into 21 default-`ENABLED`, 37 default-`AUTO`, 9 default-`DISABLED`).

### How the two classes differ

| Property | Class A — `AutoProto*` | Class B — inline `TristateProto` |
|---|---|---|
| Storage | `AutoProto*` pointer field | 4-byte enum field |
| Field count | ~330 (130 named single-field resolvers) | 67 |
| Resolver | `AutoOr<T>::FromProtoOrDie` + polarity | direct `cmpl $2,+OFF; sete` |
| Present model | packed present-bit (bit 8 / 32 / `dl`) | enum value (`ENABLED==2`) |
| `AUTO` default | consumer polarity (§3) | proto enum default |
| Null fallback | `&AutoProto_globals_` | n/a (value is inline) |

> **GOTCHA —** a knob's name (`Enable…`/`Should…`) does not tell you its class. `EnableLloLinter` is inline `TristateProto` (`cmpl $2`), while `EnablePipelinedLoopUnrolling` is `AutoProto` (`AUTO=off` bool). A reimplementer enumerating tri-state knobs must classify each by its accessor body — pointer-load-and-resolve (Class A) vs inline-compare (Class B) — not by its name or its tri-state-sounding type.

---

## Related Components

| Component | Relationship |
|---|---|
| `AutoOr<bool>::FromProtoOrDie @ 0xf795300` | the resolver template — present-bit packing |
| `AutoProto_globals_ @ 0x223c8968` | the all-AUTO default instance every unset knob falls back to |
| `AutoProto::_table_ @ 0x21cfa788` | the 30-arm oneof parse table that drives `FromAutoProto` |
| `ObjectView<TCE>` band `0x1d6b6420..0x1d6b9f60` | the 130 named single-field resolvers and their polarity tails |
| `TpuCompilationEnvironment` (TCE) | hosts the `AutoProto*` fields and the inline `TristateProto` fields |

## Cross-References

- [overview.md](overview.md) — the three-layer config pipeline; this page owns Stage 3, the AUTO resolver
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the TCE proto that hosts the `AutoProto*` and inline-`Tristate` fields, and the field#→offset oracle
- [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md) — the byte-exact field#→offset→default map; the env offsets cited per resolver here
- [autoproto-message-arms.md](autoproto-message-arms.md) — the 30-arm oneof and the 12 message arms' sub-defaults (Idiom E)
- [autoor-parse-grammar.md](autoor-parse-grammar.md) — the string→`AutoProto` ingest (`auto`/`enabled`/`disabled`/literal)
- [autoor-unparse.md](autoor-unparse.md) — the reverse `AbslUnparseFlag<AutoOr<T>>` text direction
