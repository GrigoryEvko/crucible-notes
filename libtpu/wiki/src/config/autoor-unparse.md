# AutoOr Unparse

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

A tri-state TPU compile knob is stored as a packed `AutoOr<T>` value (§[resolution model](autoproto-autoor-resolution.md)) and parsed from a flag string by the ingest grammar (§[parse grammar](autoor-parse-grammar.md)). This page owns the *reverse edge*: `xla::jellyfish::AbslUnparseFlag<AutoOr<T>>` — the family of 25 distinct functions that render a stored `AutoOr<T>` back to the display string a user sees in `--helpfull` or a flag dump, and that a re-parse must accept. The contract is symmetric by design: every printed spelling is a canonical ingest token, so a value written, dumped, and re-fed round-trips to the same `AutoOr<T>` (with two documented exceptions).

The unparse family is uniform. Each instantiation takes `(std::string* sret /*rdi*/, AutoOr<T> const& /*rsi*/)`, opens with a single `cmpb $0x0, OFF(%rsi)` testing the type-class's present/has byte, and branches: if the byte is clear the value is `AUTO` and the function builds the literal `"auto"` from a per-type lazy-static `absl::NoDestructor<std::string>` sourced from one shared `.rodata` literal (`@ 0x85ca81f = "auto"`); if the byte is set it dispatches to the canonical absl/proto2 unparse primitive for `T`. The present-byte offset is the same one the resolver packs into (bit 8 for `bool`, bit 32 for the 4-byte classes, a paired has-byte for `int64`/`double`/`string`/message) — the unparser reads the *same packed layout* the consumer accessors test (§[resolution](autoproto-autoor-resolution.md#2-the-autoort-resolver-template)).

The reference frame is `absl::AbslUnparseFlag` for a plain scalar — but wrapped so that the `AUTO`-present check fires first and short-circuits to the fixed string `"auto"` before the inner type's unparser ever runs. The page is structured as: the family skeleton and the present-byte-per-type-class table; the per-type-class primitives (`bool` xor-trick, signed-decimal integers, `UnparseFloatingPointVal` round-trip, verbatim string, enum value-NAME); the float/double edge tokens (inf/nan, shortest round-trip, the radix asymmetry); and the round-trip degeneracies a reimplementer must accept. The parse direction mirrors this on [autoor-parse-grammar.md](autoor-parse-grammar.md); the message-arm proto-text grammar (`text:`/`serialized:`/`base64:`) is on [autoproto-message-arms.md](autoproto-message-arms.md).

For reimplementation, the contract is:

- **The two-branch skeleton** — `cmpb $0x0, OFF(%rsi)` on the present/has byte; clear ⇒ emit `"auto"` from the shared lazy-static; set ⇒ call the typed primitive on the value bytes.
- **The present-byte offset per type-class** — `+1` bool, `+4` int32/uint32/float/enum, `+8` int64/double, `+0x20` string, `+0x28..+0x50` message. This is the *same* packed layout the resolver writes.
- **The canonical printed spelling per type** — `"true"`/`"false"` (not `1`/`0`), signed decimal (always, no radix marker), shortest-round-trip float/double with inf/nan special tokens, verbatim string, registered enum value NAME or bare-decimal fallback.

| | |
|---|---|
| **Named target** | `xla::jellyfish::AbslUnparseFlag<AutoOr<bool>> @ 0x1d6ba240` — `cmpb $0,+1`; set ⇒ `Unparse(bool)`; clear ⇒ `"auto"` |
| **Family size** | 25 distinct `AbslUnparseFlag<AutoOr<T>>` symbols (30 oneof arms → 25 unparse symbols; §[ICF](#why-30-arms--25-unparse-symbols)) |
| **AUTO literal source** | `"auto" @ 0x85ca81f` (.rodata) — one byte sequence shared by every instance and by the ingest sentinel |
| **bool primitive** | `absl::flags_internal::Unparse(bool) @ 0x21113200` — `"true"@0x867b321` / `"false"@0x868fb8a`, xor-`$5` size trick |
| **int/long primitive** | `Unparse(int) @ 0x211132c0` / `Unparse(long) @ 0x21113380` → `FastIntToBuffer` → signed decimal |
| **float/double primitive** | `UnparseFloatingPointVal<float> @ 0x21113460` / `<double> @ 0x211135a0` — `"%.*g"` + `SimpleAtof`/`SimpleAtod` round-trip check |
| **string primitive** | `AutoOrTypeTraits<string>::Unparse @ 0x1d743c20` → `AbslUnparseFlag(string_view) @ 0x211136e0` (verbatim) |
| **enum primitive** | `proto2::internal::AbslUnparseFlagImpl(int, EnumDescriptor&) @ 0x20ec7220` → value NAME / bare decimal |
| **Present-byte offsets** | `+1` bool · `+4` int32/uint32/float/enum · `+8` int64/double · `+0x20` string · `+0x28..+0x50` message |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The `AbslUnparseFlag<AutoOr<T>>` Family

### Purpose

`xla::jellyfish::AbslUnparseFlag(std::string* sret, AutoOr<T> const& v)` is the function the absl flags machinery calls to stringify a flag whose stored type is `AutoOr<T>`. It is the symmetric inverse of the ingest dispatcher (§[parse grammar](autoor-parse-grammar.md)): ingest turns a string into the packed `(present, value)`; unparse turns that packed value back into the string. There is one instantiation per type-class, all sharing one skeleton.

### Entry Point

```text
AbslUnparseFlag<AutoOr<T>>(std::string* sret, AutoOr<T> const& v)   ── e.g. bool @ 0x1d6ba240
  ├─ cmpb $0x0, OFF(%rsi)                       ── present/has byte for T's class (§1 table)
  ├─ je  → build "auto"                         ── lazy NoDestructor<string> from "auto"@0x85ca81f
  │         (__cxa_guard_acquire + PlacementImpl, then SSO/heap copy into sret)
  └─ jne → typed primitive on the value bytes   ── Unparse(bool)/Unparse(int)/UnparseFloatingPointVal/...
```

### Algorithm

The `bool` instantiation `@ 0x1d6ba240` is the canonical body. The decompile shows the two-branch shape exactly:

```c
function AbslUnparseFlag<AutoOr<bool>>(string* sret, AutoOr<bool>* v):   // 0x1d6ba240
    if v[1]:                                       // present byte at +1 — bit 8 of (present<<8)|val8
        Unparse(sret, *v)                          // 0x21113200, value byte at +0 → "true"/"false"
    else:                                          // AUTO
        once:                                      // __cxa_guard_acquire(auto_flag_value)
            NoDestructor<string>::PlacementImpl(auto_flag_value, "auto")   // "auto" @ 0x85ca81f
        copy auto_flag_value into sret             // SSO if len<=22 else heap; NUL-terminate
    return sret
```

The present byte tested is the resolver's present-bit, *positioned in the byte where that bit lives*: bit 8 of `(present<<8)|val8` is byte `+1`, so the `bool` unparser tests `v[1]` and reads the value at `v[0]`. The value byte is whatever the resolver packed; the unparser never re-reads the `AutoProto` oneof — it works purely on the packed `AutoOr<T>` the consumer holds.

> **NOTE —** the `"OrDie"` fatal path that exists in the *resolver* (`FromProtoOrDie`) has no analogue here. The unparser cannot fail: a clear present byte is `"auto"`, a set present byte is the typed value. There is no error string in any of the 25 unparse bodies.

### Present/has byte per type-class

The present/has byte sits at a class-specific offset because the packed layouts differ — and the unparse `cmpb` offset is the byte-exact witness of that layout. Extracted from the leading `cmpb $0x0, OFF(%rsi)` of all 25 family members:

| Type-class | Unparse symbol | Present/has byte | Packed layout | Confidence |
|---|---|---|---|---|
| `bool` | `AbslUnparseFlag<AutoOr<bool>> @ 0x1d6ba240` | `+0x01` | `(present<<8)\|val8` | CONFIRMED — `if (a2[1])` |
| `int32` | `@ 0x1d744180` | `+0x04` | `(present<<32)\|val32` | CONFIRMED |
| `uint32` | `@ 0x1d743f60` | `+0x04` | `(present<<32)\|val32` | HIGH (int-class twin) |
| `float` | `@ 0x1d743d40` | `+0x04` | `(present<<32)\|f32` | CONFIRMED — `if (*(BYTE*)(rsi+4))`, `vmovss [rsi]` |
| `int64` (`long`) | `@ 0x1d743560` | `+0x08` | `{value@+0, has@+8}` | CONFIRMED |
| `double` | `@ 0x1d744ce0` | `+0x08` | `{value@+0, has@+8}` | CONFIRMED |
| `string` | `@ 0x1d743b00` | `+0x20` | `{body, variant-idx@+0x18, has@+0x20}` | CONFIRMED |
| enum (7 arms) | `@ 0x1d748380` etc. | `+0x04` | `(present<<32)\|val32` | CONFIRMED (`ScavengingMode`) |
| message (11 arms) | `@ 0x1d7453c0` etc. | `+0x28..+0x50` | `{variant idx, sub-msg, has}` | HIGH (offsets recovered, not sizeof-derived) |

> **GOTCHA —** `float` shares the `+0x04` 8-byte-packed slot with `int32`/`enum`, **not** the `+0x08` has-byte slot of `int64`/`double`. A `float` is 4 bytes, so its present bit fits at bit 32 of the 8-byte pack (present byte `+4`); a `double` is 8 bytes and needs the separate has-byte at `+8`. A reimplementer who lumps "floating point ⇒ has-byte at +8" will read the `float` present bit from the wrong byte. This was a correction to an earlier ingest sketch (KF#3 in the source: `float` packs `(present<<32)|f32`, witnessed by `AbslUnparseFlag<AutoOr<float>> @ 0x1d743d40` testing `+4` and `vmovss`-loading the value at `+0`).

### Why 30 arms → 25 unparse symbols

The `AutoProto` oneof has 30 arms but only 25 distinct unparse symbols exist:

- **4 arms have no registered flag** (oneof-inner-only, never a CLI flag) so no unparse is emitted: `uint64`, `BufferContentsSanitizerConfig`, `PrecisionTracerModeProto_Value`, `SkipConfigTypeProto_Value`.
- **2 arms were identical-code-folded** by the linker onto a byte-identical sibling — the symbol is gone, the code is shared: `ExecutionOptions_EffortLevel` (folds onto another `(present<<32)|val32` + enum `AbslUnparseFlagImpl`) and `SparseCoreAssertLevel` (an enum-class arm; its `AbslUnparseFlag<AutoOr<…>>` symbol is likewise absent, folded onto a byte-identical sibling).

> **NOTE —** ICF means a reimplementer cannot infer "every arm has its own unparse function." Two enum/message arms run *another* arm's bytes. The behavior is unchanged (byte-identical code), but a symbol-by-symbol reconstruction will be short two names.

---

## 2. The Scalar Primitives

### Purpose

Once the present byte is set, the family member calls the canonical absl primitive for `T` on the value bytes. These primitives are not TPU-specific — they are stock `absl::flags_internal` unparsers — but the *exact spelling* they produce is the round-trip contract, so each is byte-walked here.

### `bool` → `"true"` / `"false"` (the xor-`$5` trick)

`absl::flags_internal::Unparse(bool) @ 0x21113200` emits the words, never `1`/`0`:

```c
function Unparse(string* sret, unsigned val):       // 0x21113200
    len = val ^ 5                                    // 0 (false) → 5; 1 (true) → 4
    sret.sso_size = val ^ 5                          // byte at sret+23 (SSO length slot)
    src = val ? "true"@0x867b321 : "false"@0x868fb8a
    memcpy(sret, src, len)                           // copy exactly len bytes
    sret[len] = 0
    return sret
```

The `val ^ 5` computes the string length without a branch: `false` is 5 chars, `true` is 4, and `0^5=5`, `1^5=4`. The literal is picked by value (`cmov`-style). The `"true"@0x867b321` / `"false"@0x868fb8a` literals are the *same* `.rodata` bytes the bool ingest (`SimpleAtob`) recognizes, so the round-trip is closed on one byte sequence.

### `int32` / `int64` → signed decimal (always)

`Unparse(int) @ 0x211132c0` and `Unparse(long) @ 0x21113380` both zero the SSO string, set length 22, then call `FastIntToBuffer` (`@ 0x211719e0` / `@ 0x21171f00`) to write base-10 with a leading `-` for negatives:

```c
function Unparse(string* sret, int val):            // 0x211132c0
    sret = ""; sret.sso_size = 22                    // room for INT64_MIN + sign + NUL
    end = FastIntToBuffer(val, sret.buf)             // base-10, '-' prefix if negative
    sret.size = end - sret.buf
    sret[size] = 0
    return sret
```

> **QUIRK —** the unparse is *always decimal*, but the ingest accepts `0x`-radix and a sign (§[parse grammar](autoor-parse-grammar.md)). So a knob set as `0x10` unparses to `"16"`, and `INT64_MAX` set via `0x7fffffffffffffff` unparses to `"9223372036854775807"`. This radix asymmetry is the **only** non-identity in the otherwise-canonical round-trip for integers: the value is preserved, the spelling is normalized to decimal.

### `string` → verbatim (with the `"auto"` degeneracy)

`AutoOrTypeTraits<string>::Unparse @ 0x1d743c20` reads the variant index at `+0x18` (`*((BYTE*)v + 24)`: 0 = owned `std::string`, 1 = `string_view`) and for either delegates to `absl::flags_internal::AbslUnparseFlag(string_view) @ 0x211136e0`, a verbatim copy:

```c
function AutoOrTypeTraits<string>::Unparse(string* sret, variant* v):   // 0x1d743c20
    idx = v[24]                                      // variant index byte
    if idx == 1:                                     // string_view arm
        copy v's view into a temp; AbslUnparseFlag(sret, temp)
    elif idx == 0:                                   // owned std::string arm
        read body (SSO at v+23 or heap ptr at v[0]); AbslUnparseFlag(sret, body)
    else: __throw_bad_variant_access
    return sret                                      // verbatim — no escaping, no quoting
```

The enclosing `AbslUnparseFlag<AutoOr<string>> @ 0x1d743b00` tests the has-byte at `+0x20`; clear ⇒ `"auto"`.

> **GOTCHA —** because `AUTO` unparses to the literal `"auto"` and the string ingest is a verbatim copy, a stored string whose *value* is `"auto"` unparses to `"auto"` and re-parses as `AUTO`. This is an unrecoverable round-trip degeneracy: the string value `"auto"` is indistinguishable from the AUTO sentinel. A reimplementer must accept that `"auto"` is a reserved string-knob value; there is no escape mechanism.

### enum → value NAME, unknown number → bare decimal

`proto2::internal::AbslUnparseFlagImpl(int number, EnumDescriptor const& d) @ 0x20ec7220` looks the number up in the descriptor and prints the registered name, else falls back to the bare decimal:

```c
function AbslUnparseFlagImpl(string* sret, int number, EnumDescriptor* d):   // 0x20ec7220
    node = EnumDescriptor::FindValueByNumber(d, number)   // 0x20e58060
    if node:
        name = *(string*)(node + 8)                       // EnumValueDescriptor name; SSO at +0x17
        copy name into sret                               // e.g. "AGGRESSIVE", "VERIFY"
    else:                                                 // numeric value with no registered name
        sret = ""; FastIntToBuffer(number, sret)          // bare decimal fallback
    return sret
```

This pairs with the enum ingest (`FindValueByName` with lower/upper-case retry + numeric fallback): an enum knob given any-case name round-trips to its *registered-canonical-case* NAME; a number with no registered name round-trips as the bare number. The enum value is read from bit 0..31 of the `(present<<32)|val32` pack; the present bit at byte `+4` gates whether the enum primitive runs at all (clear ⇒ `"auto"`).

---

## 3. Float / Double Edge-Token Formatting

### Purpose

`UnparseFloatingPointVal<T>` is the one primitive with non-trivial numeric edge behavior, and it is exactly where a naive reimplementation diverges. It is absl's shortest-round-trip floating-point printer: it formats with a precision, then *verifies the round-trip* by parsing its own output back and comparing to the original bits. The float and double instantiations are byte-identical apart from the width of the IEEE-754 special-value mask and the `SimpleAtof` vs `SimpleAtod` re-parse.

### Algorithm

`UnparseFloatingPointVal<float> @ 0x21113460` (the `<double>` body `@ 0x211135a0` is structurally identical):

```c
function UnparseFloatingPointVal<float>(string* sret, float x):   // 0x21113460
    bits = bit_cast<uint32>(x)
    tmp = FormatPack("%.*g", PREC, x)                 // 0x... str_format; format to a temp string
    if (bits & 0x7FFFFFFF) >= 0x7F800000:             // exponent all-ones ⇒ inf / nan
        sret = tmp                                    // emit the special token verbatim (inf/-inf/nan)
        return sret
    // finite path — verify the formatted text round-trips to the same value
    if SimpleAtof(tmp, &back):                        // 0x21171440 — re-parse our own output
        vucomiss(back, x)                             // compare back vs original (NaN-aware compare)
    sret = FormatPack("%.*g", PREC, x)                // emit (precision honored, shortest faithful form)
    return sret
```

The double path uses mask `0x7FFFFFFFFFFFFFFF >= 0x7FF0000000000000` (the 11-bit exponent all-ones) and `SimpleAtod @ ...` + `vucomisd`. Both confirmed byte-exact.

### Edge tokens

| Input value | Test | Printed token | Re-parse | Confidence |
|---|---|---|---|---|
| `+inf` | `(bits & exp-mask) >= exp-all-ones`, mantissa 0 | `"inf"` (the `%g` infinity spelling) | accepted by `SimpleAtof`/`SimpleAtod` | HIGH |
| `-inf` | same, sign set | `"-inf"` | accepted | HIGH |
| `nan` | exp all-ones, mantissa ≠ 0 | `"nan"` | accepted | HIGH |
| finite | else | shortest `%g` form that re-parses to the same bits | identity by construction | CONFIRMED (round-trip check present) |

> **QUIRK —** the inf/nan branch *bypasses the round-trip check*: a non-finite value is emitted directly as its `%g` special token and never re-parsed. Only the finite path runs `SimpleAtof`/`SimpleAtod` + `vucomiss`/`vucomisd` to confirm the printed text round-trips. This means a float-typed `AUTO`-or knob can legitimately hold and print `inf`/`nan` — a reimplementer must accept those as valid stored values for the float/double arms, not reject them as parse errors.

> **NOTE —** the decompiled precision argument to `FormatPack` reads as the immediate `4` in both the float and double bodies (`"%.*g", 4`). For `float`, precision 4 (≈ minimal significant digits) is plausible as the shortest-round-trip seed; for `double` a literal `4` is *too low* to round-trip every double, and the trailing `SimpleAtod` + `vucomisd` re-parse-and-compare is the loop that would widen the precision on a mismatch. The exact widening rule (absl's `RoundTripDtoA`/`FtoA`) was not byte-walked beyond the round-trip check shown above; treat the constant `4` as the seed precision, not the final one (**LOW** on the exact widening, **CONFIRMED** on the round-trip-verify structure).

### The integer-sentinel interaction

The 18 int64-sentinel knobs (§[resolution Idiom C](autoproto-autoor-resolution.md#idiom-c--auto--int64-sentinel-18-knobs)) and the 7 double / 12 float knobs all unparse through these primitives when present. A sentinel like `INT64_MAX` is *not* special-cased on unparse — it is just `9223372036854775807` in signed decimal. The sentinel meaning ("AUTO chose this default") is a *consumer* convention, invisible to the unparser; if a user explicitly set the value to `INT64_MAX`, the present byte is set and it unparses as the literal number, not `"auto"`.

---

## 4. The Text Round-Trip Table

This is the user-facing reference: what each type-class prints for `AUTO` vs a present value, and what the ingest re-parses that string back to. Confirmed against the 25 family bodies and the parse grammar.

| Type | `AUTO` prints | Present value prints | Re-parses to | Confidence |
|---|---|---|---|---|
| `bool` | `"auto"` | `"true"` / `"false"` | present/value (`0x101`/`0x100`) | CONFIRMED |
| `int32` | `"auto"` | signed decimal (e.g. `"-1"`) | `(v\|0x100000000)` | CONFIRMED |
| `uint32` | `"auto"` | decimal | `(v\|0x100000000)` | HIGH |
| `float` | `"auto"` | shortest-RT float, or `inf`/`-inf`/`nan` | `(present<<32)\|f32` (byte `+4`) | CONFIRMED |
| `int64` | `"auto"` | signed decimal | `{v, has=1}` (byte `+8`) | CONFIRMED |
| `double` | `"auto"` | shortest-RT double, or `inf`/`-inf`/`nan` | `{v, has=1}` (byte `+8`) | CONFIRMED |
| `string` | `"auto"` | the string VERBATIM | `{body, idx 0, has 1}` (byte `+0x20`) | CONFIRMED |
| enum | `"auto"` | value NAME (registered case) / bare decimal if unnamed | `(number\|0x100000000)` | CONFIRMED |
| message | `"auto"` | `"text:<TextFormat>"` / `"serialized:…"` / `"base64:…"` | sub-message + has | HIGH |

Three asymmetries a reimplementer must reproduce:

- **Radix.** Ingest accepts `0x`-hex and a sign; unparse is always decimal. `true`/`false` (not `1`/`0`) is the canonical bool spelling both directions.
- **Enum case.** Ingest is case-insensitive (lower/upper retry); unparse emits the registered-canonical case of the matched value name.
- **The `"auto"` string degeneracy.** A stored string equal to `"auto"` unparses to `"auto"` and re-parses as `AUTO`. The only value-losing round-trip.

> **NOTE —** the message arms unparse through `proto2::Message::AbslUnparseFlagImpl @ 0x20ef2e40` (`text:` via `TextFormat::Printer::PrintToString`, falling back to `serialized:`/`base64:` when text-format cannot faithfully round-trip unknown fields or maps). That proto-text grammar — and the inner field spelling — is owned by [autoproto-message-arms.md](autoproto-message-arms.md); this page only records that the message present/has byte sits at `+0x28..+0x50` and that the `AUTO` answer is the empty default-instance, printed as `"auto"`.

---

## 5. The AUTO Literal

### Purpose

Every one of the 25 family members builds the string `"auto"` for the unset branch from a *per-type lazy-static* `absl::NoDestructor<std::string>`, but all of them source the characters from one shared `.rodata` literal.

### Algorithm

```c
function emit_auto(string* sret):                    // the je branch of every family member
    if not guard.auto_flag_value:                    // __cxa_guard_acquire(per-type guard)
        NoDestructor<string>::PlacementImpl(per_type_auto_flag_value, "auto")   // src @ 0x85ca81f
        guard.release()
    copy per_type_auto_flag_value into sret           // SSO (len 4 ≤ 22) — fits inline, no heap
    return sret
```

The per-type statics (e.g. `bool @ 0x2257ea50`, `int @ 0x2257ecb0`, `long @ 0x2257ec30`, `string @ 0x2257ec50`, `ScavengingMode @ 0x2257ee30`, `CostModelFlagOptions @ 0x2257ed10`) are distinct objects — one per instantiation — but each is constructed once via `__cxa_guard_acquire`/`release` + `PlacementImpl` from the *same* source literal `"auto" @ 0x85ca81f`. That is the exact byte sequence the ingest grammar uses for its `AUTO` sentinel, so the AUTO round-trip is closed on one `.rodata` string.

> **NOTE —** the per-type lazy-statics are an artifact of templating `AutoFlagValue()` once per `AutoOr<T>` — each instantiation gets its own `static std::string`. They all hold `"auto"`; the duplication is harmless and a reimplementation may collapse them to one global. What must be preserved is that the unset branch emits exactly `"auto"` (lowercase, four bytes), because that is the only spelling the parser maps back to AUTO.

---

## Related Components

| Component | Relationship |
|---|---|
| `AbslUnparseFlag<AutoOr<bool>> @ 0x1d6ba240` | the family skeleton — present-byte check then typed primitive |
| `Unparse(bool) @ 0x21113200` | bool primitive — `"true"`/`"false"` via xor-`$5` |
| `UnparseFloatingPointVal<float>/<double> @ 0x21113460/0x211135a0` | float/double shortest-round-trip + inf/nan tokens |
| `AbslUnparseFlagImpl(int, EnumDescriptor&) @ 0x20ec7220` | enum primitive — value NAME / bare decimal |
| `"auto" @ 0x85ca81f` | the shared AUTO literal — same bytes as the ingest sentinel |
| `Message::AbslUnparseFlagImpl @ 0x20ef2e40` | message-arm proto-text unparse (owned by the message-arms page) |

## Cross-References

- [overview.md](overview.md) — the three-layer config pipeline; this page is the unparse OUT edge of Stage 3
- [autoor-parse-grammar.md](autoor-parse-grammar.md) — the mirror: string → `AutoOr<T>` ingest (`auto`/`true`/`false`/literal/`0x`-hex)
- [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) — the resolution model; the packed `(present, value)` layout this page reads back
- [autoproto-message-arms.md](autoproto-message-arms.md) — the 30-arm oneof and the message-arm `text:`/`serialized:`/`base64:` proto-text grammar
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the TCE that hosts the `AutoProto*` fields backing the 330 AutoOr flags
- [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md) — the byte-exact field#→offset→default map for the knobs that round-trip through this family
