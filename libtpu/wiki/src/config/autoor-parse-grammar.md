# AutoOr Parse Grammar

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

When a user sets `XLA_FLAGS=--xla_tpu_<knob>=auto` (or `true`, `0.5`, `42`, `MODE_NAME`), libtpu does not hand the raw string to a generic flag parser. The token routes through a two-stage path peculiar to `AutoOr<T>`. First a *FieldDescriptor-keyed arm selector* — `ParseAutoOrFromString<30>` @ `0x1d7504c0`, tail-chaining to `<25>` @ `0x1d7eac40` — looks up the flag's registered C++ type via its `CommandLineFlag` type-tag and dispatches to the one `AutoOr<T>::ParseFlag` instance for that type. That per-type parser is where the grammar lives: every one of the 30 instances opens with an identical `"auto"` sentinel check, and on a non-match delegates to the canonical absl/proto2 primitive for `T` (`SimpleAtob`, `safe_strto32_base`, `SimpleAtod`, an `EnumDescriptor` name lookup, a proto-text reader, …). The parsed value is then packed into the exact present-bit / value code that the resolver on the other side of the round-trip reads back.

The reference frame is `absl::ParseFlag` / `AbslParseFlag`, the customization point absl uses to teach its flag library a new type. `AutoOr<T>` is one such type, and `ParseFlag` is its `AbslParseFlag` overload. The libtpu-specific twist is the dual layer: the `"auto"` sentinel is checked *above* the absl primitive, with a raw case-sensitive `bcmp` against a lazily-constructed `std::string "auto"`, so only the exact lowercase four-byte token is the AUTO sentinel — while the *typed* token set below it (`true`/`yes`/`1`, enum names) is case-insensitive because the absl/proto2 primitives fold case. A reimplementer who treats the whole grammar as case-insensitive, or who routes `"AUTO"` to the sentinel, will diverge from the binary.

This page owns the ingest grammar: the two arm-arity dispatchers and how a flag reaches them; the universal `"auto"`-token detection idiom; the per-type-class parser dispatch (bool / int / float / string / enum / message) with its accepted token set; and the packing of each parse result back into the `(present, value)` code. The *resolution* of that code into a concrete compile value — the present-bit positions, the all-AUTO default instance, the per-consumer `AUTO`→default polarity — is owned by [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md); the reverse text-out direction by [autoor-unparse.md](autoor-unparse.md); the structured message-arm sub-grammar by [autoproto-message-arms.md](autoproto-message-arms.md). This page is the string→code half; that page is the code→value half. Together they close the round-trip.

For reimplementation, the contract is:

- **The arm-selector dispatchers** — `ParseAutoOrFromString<30>`/`<25>` are *not* type parsers; they switch on the flag's `CommandLineFlag` type-tag (`call *0x38`), call the matching `AutoOr<T>::ParseFlag`, fold the result through `NormalizeFieldType<AutoOr<T>>`, and emit the per-arm error on failure.
- **The `"auto"` sentinel** — every `ParseFlag` instance opens with a length-compare + case-sensitive `bcmp` against a lazy-static `std::string "auto"`; a match writes the all-zero (present-bit-clear) code and returns success *before* the typed parser runs.
- **The per-type packing** — bool `(val | 0x100)`, int32/enum `(val | 0x100000000)`, int64/uint64/double/float `{value@+0, has=1@+8}`, string `{body, idx@+0x18=0, has@+0x20=1}` — byte-identical to the code the resolver reads back.

| | |
|---|---|
| **Arm selector (30)** | `ParseAutoOrFromString<30>` @ `0x1d7504c0` — 5-arm prefix, tail-call to `<25>` |
| **Arm selector (25)** | `ParseAutoOrFromString<25>` @ `0x1d7eac40` — 24-arm chain, default `"Not an AutoOr."` |
| **Dispatch key** | `call *0x38(CommandLineFlag)` → type-tag, compared to `FastTypeTag<AutoOr<T>>::kDummyVar` |
| **Per-arm fold** | `AutoOr<T>::ParseFlag(value, &out, &err)` then `NormalizeFieldType<AutoOr<T>>(sret, out, fd)` |
| **`"auto"` literal** | `.rodata` `0x85ca81f` `"auto"` — lazy `std::string` ctor in all 30 `ParseFlag` instances |
| **bool parser** | `AutoOr<bool>::ParseFlag` @ `0x1d6ba160` → `SimpleAtob`; pack `val \| 0x100` |
| **int32 parser** | `AutoOr<int>::ParseFlag` @ `0x1d744080` → `safe_strto32_base`; pack `val \| 0x100000000` |
| **int64 parser** | `AutoOr<long>::ParseFlag` @ `0x1d743480` → `safe_strto64_base`; pack `{value, has@+8}` |
| **enum parser** | e.g. `AutoOr<ScavengingMode_Value>::ParseFlag` @ `0x1d748280` → `AbslParseFlagImpl` name lookup |
| **Error string** | `"Failed to parse '%s' into flag %s: %s"` (`0x8584d92`); enum `0xa0307af`; no-arm `0xa03f8b1` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The Entry Path

### Purpose

`ParseAutoOrFromString<N>` is the explicit-value ingest: a caller supplies the flag, its `proto2::FieldDescriptor`, and a raw `string_view`, and gets back a `StatusOr<variant<…20…>>` — the parsed `AutoOr<T>` folded into the 20-alternative TCE field variant. It is reached when the absl flag library parses an `XLA_FLAGS` token for a flag that was *registered* as `AutoOr<T>`.

### How a flag value arrives

```text
XLA_FLAGS=--xla_tpu_<knob>=<value>
  └─ absl flag registry parses the raw string into the flag's STORED type
       (the flag is registered as AutoOr<bool>; absl invokes
        AutoOr<bool>::ParseFlag at flag-SET time — §3's grammar runs here first)
  └─ TpuCompEnvReflection::SetFieldFromFlagString @ 0x1d73fcc0
     / TpuCompEnvReflection::ReadFlag @ 0x1d74af60     ── bridge flag → TCE env
       ├─ ReadFlag dispatches on CommandLineFlag::*0x38 (the flag's TypeId)
       │    ├─ ReadFlagImpl<T>      ── non-AutoOr wrapper types (plain bool/float/…)
       │    └─ ReadAutoOr<30> @ 0x1d74ca00 / ReadAutoOr<25> @ 0x1d785f00
       │          ── for AutoOr-typed fields; mirrors ParseAutoOrFromString<N>
       │             but re-parses the flag's CURRENT value (CommandLineFlag::*0x18)
       └─ NormalizeFieldType<AutoOr<T>> folds AutoOr<T> into the variant
       └─ SetEnvField @ 0x1d752ae0 writes it into the TCE AutoProto oneof
```

There are two entry points into the same per-type grammar. `ParseAutoOrFromString<N>` takes the value as an argument (the explicit-string path); `ReadAutoOr<N>` reads the flag's current value off the `CommandLineFlag` and re-parses it. Both share the §3 `AutoOr<T>::ParseFlag` instances, so the grammar described here is the single source of truth for either path.

> **NOTE —** the file/line anchor for the per-arm error path is `platforms/xla/service/jellyfish/tpu_compilation_environment_reflection.cc` (string `0x877a8ca`), seen in the `MakeErrorImpl<3>` call inside `ParseAutoOrFromString<30>` at the `"Failed to parse…"` branch. The dispatchers and `ReadAutoOr` mirrors live in this same translation unit.

---

## 2. The Arm-Selector Dispatchers

### Purpose

`ParseAutoOrFromString<30>` and `<25>` are **arm selectors, not parsers**. Each is a straight-line if-chain keyed on the flag's registered C++ type. The `<N>` is the oneof arity: `<30>` covers the 30-arm `AutoProto` oneof, `<25>` the 25-arm subset. `<30>` handles the 5 arms unique to the 30-arm oneof, then tail-calls `<25>` for the common ones. `<25>` handles 24 arms and falls through to the `"Not an AutoOr."` error.

### Algorithm

The dispatch key is the flag's type-tag accessor at vtable offset `0x38`, compared against the compile-time `FastTypeTag<AutoOr<T>>::kDummyVar` address for each candidate arm:

```c
function ParseAutoOrFromString<30>(sret, CommandLineFlag& flag,        // 0x1d7504c0
                                   FieldDescriptor& fd, string_view value):
    tag = flag.vtable[0x38](flag)                 // *(*(flag)+56)(flag) — registered TypeId

    // ── the 5 arms in the 30-arm oneof but NOT in the 25-arm one ──
    if tag == &FastTypeTag<AutoOr<BufferAssignmentAlgorithmProto_Value>>::kDummyVar:
        ok = AutoOr<…>::ParseFlag(value.ptr, value.len, &local, &err)   // 0x1d74a900
        if ok: NormalizeFieldType<AutoOr<…>>(sret, local, fd)           // build variant arm
        else:  sret = MakeErrorImpl("Failed to parse '%s' into flag %s: %s")  // 0x8584d92
        return
    if tag == &FastTypeTag<AutoOr<TpuCustomCallMemorySpaceSpec>>::kDummyVar:  … // 0x1d74a3c0
    if tag == &FastTypeTag<AutoOr<BundleInstrumentationOptions>>::kDummyVar:  … // 0x1d749ce0
    if tag == &FastTypeTag<AutoOr<SparseCoreAssertLevel>>::kDummyVar:         … // 0x1d7495e0
    if tag != &FastTypeTag<AutoOr<AccumulatorTransformations>>::kDummyVar:        // 0x1d748c80
        return ParseAutoOrFromString<25>(sret, flag, fd, value.ptr, value.len)   // TAIL CALL
    … handle AccumulatorTransformations …
```

```c
function ParseAutoOrFromString<25>(sret, flag, fd, value):              // 0x1d7eac40
    tag = flag.vtable[0x38](flag)
    if tag == &FastTypeTag<AutoOr<bool>>::kDummyVar:   … ParseFlag @0x1d6ba160 …
    if tag == &FastTypeTag<AutoOr<int>>::kDummyVar:    … ParseFlag @0x1d744080 …
    … 22 more arms (scalars + remaining enum/message types) …
    // no arm matched: this FieldDescriptor is not an AutoOr-typed TCE field
    sret = MakeErrorImpl("Not an AutoOr.")            // 0xa03f8b1
```

Each matched arm does exactly two things on success: call the type's `ParseFlag` with the verbatim `value`, then call `NormalizeFieldType<AutoOr<T>>(sret, local, fd)` to construct the correctly-tagged variant alternative (the `fd` argument only steers the variant tag, not the parse). On `ParseFlag` failure the arm builds the `"Failed to parse '%s' into flag %s: %s"` status into `sret` via `MakeErrorImpl<3>`, formatting the value, the flag name (`flag.vtable[0]`), and the parser's error string. The decompile shows the `call *(…+56)` tag accessor repeated per arm and the `vmovaps`/`vxorps` clears that zero-initialize the per-arm scratch variant — the if-chain is fully unrolled, not a switch table.

### The 5 vs 24 split

> **QUIRK —** the `<30>` dispatcher exists *only* to peel off the 5 arms that the 30-arm oneof has but the 25-arm one lacks, then it tail-calls `<25>`. A reimplementer can collapse this into a single 30-way switch with identical behavior — the two-function split is a template-instantiation artifact (two oneof arities in the same generic), not a semantic boundary. The 5 prefix arms are: `BufferAssignmentAlgorithmProto_Value` (enum), `TpuCustomCallMemorySpaceSpec`, `BundleInstrumentationOptions`, `AccumulatorTransformations`, and `SparseCoreAssertLevel` (all message arms). Verified in the decompile: the `<30>` body's final non-match is `... != FastTypeTag<AutoOr<AccumulatorTransformations>> → jmp ParseAutoOrFromString<25>(a1,a2,a3,a4,a5)`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ParseAutoOrFromString<30>` | `0x1d7504c0` | 5-arm prefix selector + tail-call to `<25>` | CONFIRMED |
| `ParseAutoOrFromString<25>` | `0x1d7eac40` | 24-arm selector + `"Not an AutoOr."` default | CONFIRMED |
| `ReadAutoOr<30>` | `0x1d74ca00` | from-current-value mirror of `<30>` | HIGH |
| `ReadAutoOr<25>` | `0x1d785f00` | from-current-value mirror of `<25>` | HIGH |
| `TpuCompEnvReflection::ReadFlag` | `0x1d74af60` | dispatch on `CommandLineFlag::*0x38` | HIGH |
| `TpuCompEnvReflection::SetFieldFromFlagString` | `0x1d73fcc0` | string→TCE bridge | HIGH |
| `NormalizeFieldType<AutoOr<T>>` | per-type | folds `AutoOr<T>` into the 20-alt variant | HIGH |

---

## 3. The `"auto"` Token

### Purpose

The AUTO sentinel detection is the one piece of grammar identical across all 30 `ParseFlag` instances. It is checked *before* the typed parser, so an explicit `auto` never reaches `SimpleAtob` / `safe_strto32_base` / the enum lookup — it short-circuits to the all-zero (present-bit-clear) code that the resolver reads as AUTO.

### Algorithm

The bool instance `@ 0x1d6ba160` is the canonical body; every other type's prologue is byte-identical up to the AUTO-write width:

```c
function AutoOr<bool>::ParseFlag(value_ptr, value_len, AutoOr<bool>* out):   // 0x1d6ba160
    // lazy function-local static  std::string kAuto = "auto"
    if !guard.kAuto:                                       // __cxa_guard_acquire
        NoDestructor<std::string>::construct(kAuto, "auto")   // src literal 0x85ca81f
        guard.release()
    // kAuto length: SSO uses byte-17 size; long string uses kAuto[1]
    auto_len = (kAuto[2] < 0) ? kAuto[1] : SHIBYTE(kAuto[2])
    if value_len != auto_len:                             // length gate before bcmp
        goto typed
    data = (kAuto[2] < 0) ? kAuto[0] : &kAuto             // SSO inline vs heap ptr
    if bcmp(value_ptr, data, value_len) == 0:             // CASE-SENSITIVE compare
        *out = 0                                          // AUTO: all-zero, present-bit CLEAR
        return true
typed:
    ok = absl::flags_internal::AbslParseFlag(value_ptr, value_len, &scratch)
    if ok: *out = scratch | 0x100                         // pack present<<8 | val8
    return ok
```

The `kAuto` string is a lazily-constructed function-local static (one per template instantiation; the bool instance's storage is in `.data`, zero in the file, built at first call). Its construction source is the `.rodata` literal `0x85ca81f` = `"auto"` — verified to be the same RIP target for the bool, int, long, and enum instances. The presence test is a length compare followed by a raw `bcmp`, so it is **case-sensitive**: only the exact lowercase `auto` is the sentinel. `"AUTO"` and `"Auto"` fall through to the typed parser, where they fail (no scalar parser accepts them) unless they happen to name a valid enum value.

The AUTO write width varies by type-class — this is the only per-type variation in the prologue:

| Type-class | AUTO write (decompile) | Resulting code |
|---|---|---|
| `bool` | `*out = 0` (`movw $0,(out)`) | `0x000` |
| `int32` / enum | `*out = 0` (`movq $0,(out)`) | `0` |
| `int64`/`uint64`/`double`/`float` | `vmovups %xmm0,(out)` (zeros 16 B) | `{value=0, has=0}` |
| `string` | `out+0x18 = 0xff; out+0x20 = 0` | absent variant index |

> **GOTCHA —** the `"auto"` compare is the *only* case-sensitive token in the entire grammar. Every typed token below it folds case (bool via `EqualsIgnoreCase`, enum via lower/upper retry — §4, §6). A reimplementation that lowercases the input before the sentinel check would wrongly accept `"AUTO"` as the sentinel; one that case-folds nothing would wrongly reject `"TRUE"`. The split is deliberate: AUTO is an exact reserved word, value tokens are user-friendly.

> **QUIRK —** the AUTO branch returns `true` (parse succeeded) with the present-bit clear — *not* an error and *not* a stored value. This is what makes `=auto` indistinguishable from "flag unset" at the resolution layer: both feed the resolver a `0x000` code, and [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md)'s consumer polarity decides the concrete default. The parser does not know, and does not need to know, what AUTO will resolve to.

---

## 4. Scalar Grammar — bool / int / uint / int64 / uint64 / double / float / string

### Purpose

The eight scalar arms delegate to `absl::flags_internal::AbslParseFlag(T*)`, which strips ASCII whitespace then calls absl's core numeric/bool primitive for `T`. The parsed value is packed into the present-bit code; the whitespace strip and the radix-auto numeric behavior are inherited verbatim from absl.

### Per-type parser and packing

| Type | `ParseFlag` | absl callee (strip-WS → core) | OK-pack | Confidence |
|---|---|---|---|---|
| `bool` | `0x1d6ba160` | `AbslParseFlag(bool*)` `0x21112840` → `SimpleAtob` `0x211716c0` | `val \| 0x100` | CONFIRMED |
| `int32` | `0x1d744080` | `AbslParseFlag(int*)` `0x21112a40` → `safe_strto32_base` `0x21173ce0` | `val \| 0x100000000` | CONFIRMED |
| `uint32` | `0x1d743e60` | `AbslParseFlag(uint*)` `0x21112b60` → `safe_strtou32_base` `0x211742a0` | `val \| 0x100000000` | HIGH |
| `int64` | `0x1d743480` | `AbslParseFlag(long*)` `0x21112c80` → `safe_strto64_base` `0x21173e20` | `{value@+0, has=1@+8}` | CONFIRMED |
| `uint64` | `0x1d7ed180` | `AbslParseFlag(ulong*)` `0x21112da0` → `safe_strtou64_base` `0x21174360` | `{value@+0, has=1@+8}` | HIGH |
| `double` | `0x1d744be0` | `AbslParseFlag(double*)` `0x21112ee0` → `SimpleAtod` `0x21171580` | `{dbl@+0, has=1@+8}` | HIGH |
| `float` | (via `NormalizeFieldType`) | `AbslParseFlag(float*)` `0x21112ec0` → `SimpleAtof` `0x21171440` | `{flt@+0, has=1@+8}` | HIGH |
| `string` | `0x1d7437e0` | `AbslParseFlag(string*)` `0x21112f00` (verbatim copy) | `{body, idx@+0x18=0, has@+0x20=1}` | HIGH |

### The bool token set

`SimpleAtob` `@ 0x211716c0` compares the whitespace-stripped input case-insensitively (via `EqualsIgnoreCase` `0x211711c0` → `memcasecmp` `0x21171120`) against two fixed sets, in order:

```text
TRUE  : "true"  "t"  "yes"  "y"  "1"
FALSE : "false" "f"  "no"   "n"  "0"
```

A match writes the bool and `ParseFlag` packs `movzbl b; or $0x100` → `(present<<8)|val8`. So `=true`/`=yes`/`=1`/`=t`/`=y` → `0x101`; `=false`/`=no`/`=0`/`=f`/`=n` → `0x100`. Any other token fails `SimpleAtob`, `AbslParseFlag` returns false, and the arm builds `"Failed to parse '%s' into flag %s: %s"`.

### Numeric radix and signs

The int/uint parsers call `safe_strto{,u}{32,64}_base` with an auto-detected base: a leading `0x`/`0X` selects base 16 (the `cmpb $0x30; cmp $0x78/$0x58` probe), otherwise base 10; a leading `+`/`-` sign is consumed by the signed `strto` (the `add $0xd5; test $0xfd` sign skip). The int32 pack is `movabs $0x100000000; or %val,%rdx` — the present-bit at bit 32, value in the low 32. The int64/uint64/double/float pack stores the value at `+0` and writes a literal `movb $1, +0x8` has-byte.

> **NOTE —** a `-1` token parses *natively* as int32 `0xFFFFFFFF` (low 32 bits) with the present-bit set — `0x1FFFFFFFF` packed. There is no special-casing of `-1` here; the "sentinel" meaning of `-1` (and of `INT64_MAX` for int64 knobs) lives entirely on the resolver/consumer side in [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md). The parser sees `-1` as just another signed integer.

### String — no grammar

`AutoOr<string>::ParseFlag` `@ 0x1d7437e0` copies `value` verbatim into the variant's `std::string` (body at `+0/+8/+16` SSO, variant-index byte `+0x18 = 0`, has-byte `+0x20 = 1`). Any byte sequence is accepted *except* the exact `"auto"`, which the §3 sentinel intercepts first (writing `+0x18 = 0xff`, the absent-variant index). There is no validation, escaping, or quoting at this layer.

---

## 5. Round-Trip Convergence

The packing emitted by every scalar/enum parser is byte-identical to the code the resolver reads back, so the ingest and resolve directions converge on one representation per type-class:

```text
bool                      : (present<<8)|val8           =auto/unset → 0x000
                            =true → 0x101  =false → 0x100
int32 / enum              : (present<<32)|val32          =auto → 0
                            =42   → 0x10000002A
int64/uint64/double/float : {value@+0, has@+8}           =auto → {0,0}
                            =1024 → {1024, has=1}
uint32                    : (present<<32)|val32
string                    : {body, idx@+0x18, has@+0x20} =auto → idx 0xff
```

The consumer's polarity test ([autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) §3 — `AUTO=off` `not;test 0x101;sete` vs `AUTO=on` `and 0x101;cmp 0x100;setne`) then turns this packed code into the effective compile value. A user who writes `--xla_tpu_<knob>=true` lands on `0x101`; `=false` on `0x100`; `=auto` (or leaving the flag unset) on `0x000`. The flag string and the `AutoProto` oneof are two encodings of the same packed code, which is why a `=auto` token and a never-set field are indistinguishable downstream.

---

## 6. Enum Grammar — name lookup + numeric fallback

### Purpose

The 11 enum arms accept a symbolic value name (case-insensitive) or a bare integer that is a valid enum number. The parser resolves the name through the proto2 `EnumDescriptor`, with lower-case and upper-case retries, before falling back to a numeric parse — and emits a descriptor-driven error listing every valid name on failure.

### Algorithm

Shown for `AutoOr<ScavengingMode_Value>::ParseFlag` `@ 0x1d748280`; all enum arms share the body, differing only in the `*_descriptor()` callee:

```c
function AutoOr<EnumT_Value>::ParseFlag(value, value_len, AutoOr<EnumT>* out):  // 0x1d748280
    … "auto" sentinel (§3); on match  *out = 0; return true …            // movq $0
    desc = EnumT_Value_descriptor()                                       // e.g. 0x1db209e0
    ok = proto2::internal::AbslParseFlagImpl(value, &v, desc, &err)       // 0x20ec6cc0
    if !ok: return false                                                  // arm emits enum error
    *out = v | 0x100000000                                                // present<<32 | val32
    return true
```

```c
function proto2::internal::AbslParseFlagImpl(value, int& out, EnumDescriptor& desc, err):  // 0x20ec6cc0
    node = desc.FindValueByName(value)                       // 0x20e58020
    if node: out = node->number; return true                 // mov 0x4(node),%eax
    lower = AsciiStrToLower(value)                            // 0xe6b0560
    if lower != value and (node = desc.FindValueByName(lower)): out = node->number; return true
    upper = AsciiStrToUpper(value)                           // 0xee994c0
    if (node = desc.FindValueByName(upper)): out = node->number; return true
    if safe_strto32_base(value, &n, 10):                     // 0x21173ce0 — bare integer?
        node = desc.FindValueByNumber(n)                     // 0x20e58060
        if node: out = n; return true                        // accept iff it names a real value
    err = "Invalid value '%s' for enum '%s'. Supported values are: %s."   // 0xa0307af
          joining every value name with ", "                 // 0xa299c7c
    return false
```

So an enum knob accepts: the value name in any case (`SCAVENGE_NONE`, `scavenge_none`, `Scavenge_None` all resolve via the lower/upper retry); or a bare integer *only if* it is a valid enum number (`FindValueByNumber` gates it — an out-of-range integer is rejected even though it parses numerically). On failure the error enumerates every legal name, joined by `", "`.

### Enum arm map

The 11 enum `ParseFlag` instances (each → its `*_descriptor()` then the shared `AbslParseFlagImpl`):

| Enum type | `ParseFlag` | Dispatcher |
|---|---|---|
| `ScavengingMode_Value` | `0x1d748280` | `<25>` |
| `Bf16EmissionMode_Value` | `0x1d7488e0` | `<25>` |
| `MlirVerifierOptions_Value` | `0x1d7486c0` | `<25>` |
| `CollectiveMatmulModeProto_Value` | `0x1d748060` | `<25>` |
| `ExpandedScopedAlternateMemoryMode_Value` | `0x1d747760` | `<25>` |
| `ExecutionOptions_EffortLevel` | `0x1d747360` | `<25>` |
| `RematerializationOptions_RematerializationAlgorithm` | `0x1d7484a0` | `<25>` |
| `PrecisionTracerModeProto_Value` | `0x1d7ecbc0` | `<25>` |
| `SkipConfigTypeProto_Value` | `0x1d7eccc0` | `<25>` |
| `BufferAssignmentAlgorithmProto_Value` | `0x1d74a900` | `<30>` (prefix) |

> **NOTE —** one further enum arm routes through the same `AbslParseFlagImpl` engine via the dispatcher but was not individually located in the decompile (the table above lists 10 named instances; the raw census counts 11). The eleventh follows the identical body — descriptor lookup + lower/upper retry + numeric fallback (LOW on its address, HIGH on its grammar being identical).

---

## 7. Message Grammar — proto-text into a default sub-message

### Purpose

The 12 message arms parse a structured proto-text value into a default sub-message instance. The grammar above the `"auto"` sentinel is a key=value reader, not a scalar primitive; the parsed sub-message is held in the variant alongside a has-byte.

### Algorithm

Shown for `AutoOr<CostModelFlagOptions>::ParseFlag` `@ 0x1d744f80`:

```c
function AutoOr<MsgT>::ParseFlag(value, value_len, AutoOr<MsgT>* out):   // 0x1d744f80
    … "auto" sentinel (§3); on match → empty default-instance; return true …
    msg = MsgT(arena)                                       // construct, e.g. 0x1db23d40
    ok = proto2::Message::AbslParseFlagImpl(msg, value, &err)   // 0x20ef2120
    if !ok: return false
    out.variant = move(msg); out.has = 1
    return true
```

`proto2::Message::AbslParseFlagImpl` `@ 0x20ef2120` splits the value on a delimiter (absl `ByChar` Splitter `@ 0xe6d1240`) and feeds each key=value pair to a per-field setter through a `Message` vtable slot (`*0x10`). The AUTO case yields the empty default-instance — the same default surface that [autoproto-message-arms.md](autoproto-message-arms.md) enumerates for the resolve direction.

### Message arm map

The 12 message-typed `ParseFlag` instances:

```text
0x1d744f80  CostModelFlagOptions          0x1d745680  EmitterLearnedCostModelOptions
0x1d745d80  SparseCoreOffloadingOptions   0x1d746460  RepeatedStrings
0x1d744420  RepeatedIntegers              0x1d746e20  ShardyOptions
0x1d747b00  IlpLatencyHidingSchedulerOptions   0x12fcfd00  CostModelLoggingOptions
0x1d7ecdc0  BufferContentsSanitizerConfig
0x1d748c80  AccumulatorTransformations  (<30> prefix)
0x1d7495e0  SparseCoreAssertLevel       (<30> prefix)
0x1d749ce0  BundleInstrumentationOptions(<30> prefix)
0x1d74a3c0  TpuCustomCallMemorySpaceSpec(<30> prefix)
```

> **GOTCHA —** the exact message text grammar is *not* byte-confirmed. The decompile shows `AbslParseFlagImpl` splitting on a delimiter (the `ByChar` Splitter) and dispatching per-field via `*0x10`, but the delimiter character, whether the syntax is canonical proto `TextFormat` or a custom `key=val,key=val` form, and the per-field tokenization are **not** transcribed (LOW). A reimplementer targeting message-arm CLI overrides (e.g. `--xla_tpu_<msg_knob>=field=val,field2=val2`) must verify the delimiter against the binary before relying on it. The scalar and enum grammars above are byte-exact; this one is a skeleton.

---

## 8. Error Paths

Three distinct failures, each with a fixed `.rodata` string:

| Trigger | String | Address | Built by |
|---|---|---|---|
| Scalar/message/enum `ParseFlag` returns false | `"Failed to parse '%s' into flag %s: %s"` | `0x8584d92` | `MakeErrorImpl<3>` per arm |
| Enum name not found + not a valid number | `"Invalid value '%s' for enum '%s'. Supported values are: %s."` | `0xa0307af` | `AbslParseFlagImpl` (names joined by `", "` `0xa299c7c`) |
| No dispatcher arm matched the flag's type-tag | `"Not an AutoOr."` | `0xa03f8b1` | `<25>` default |

The first two are *value* errors (the flag is an `AutoOr<T>` but the token does not parse). The third is a *type* error (the `FieldDescriptor` is not an `AutoOr`-typed TCE field at all) and can only fire from a caller that hands `ParseAutoOrFromString` a non-AutoOr flag — a programming error, not a user-input error. The `"Failed to parse"` status is formatted with `absl::str_format_internal::FormatPack` over three `%s` args: the value, the flag name (via the flag's vtable slot 0), and the inner parser's error string.

---

## Related Components

| Component | Relationship |
|---|---|
| `ParseAutoOrFromString<30>` @ `0x1d7504c0` | the 30-arm explicit-value selector this page documents |
| `ReadAutoOr<30>` @ `0x1d74ca00` | the from-current-flag mirror sharing the same `ParseFlag` grammar |
| `AutoOr<bool>::ParseFlag` @ `0x1d6ba160` | the canonical `"auto"`-sentinel + typed-parse body |
| `proto2::internal::AbslParseFlagImpl` @ `0x20ec6cc0` | the enum name-lookup engine (lower/upper retry + numeric fallback) |
| `absl::SimpleAtob` @ `0x211716c0` | the case-insensitive bool token set |
| `NormalizeFieldType<AutoOr<T>>` | folds the parsed `AutoOr<T>` into the 20-alternative TCE field variant |

## Cross-References

- [overview.md](overview.md) — the three-layer config pipeline; this page owns the ingest (string→code) stage
- [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) — the resolve direction: present-bit packing, the all-AUTO default instance, the per-consumer `AUTO`→default polarity that interprets the code this page produces
- [autoor-unparse.md](autoor-unparse.md) — the reverse `AbslUnparseFlag<AutoOr<T>>` text-out direction (code→display string)
- [autoproto-message-arms.md](autoproto-message-arms.md) — the 30-arm oneof and the message arms' structured sub-defaults (the §7 message grammar)
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the TCE proto whose fields these flags set, and the field#→offset oracle
- [xla-flag-atlas.md](xla-flag-atlas.md) — the `xla_tpu_*` flag surface that feeds these parsers
