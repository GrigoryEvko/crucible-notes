# Configuration System Overview

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, ELF x86-64 DYN, not stripped; demangled C++ symbols quoted verbatim). Other versions differ.*

## Abstract

libtpu has no command line of its own — it is a `.so` loaded by JAX/PJRT — yet it is configured almost entirely by *command-line-style flags*. The configuration system is the machinery that turns a user-supplied string into an effective, per-TPU-generation compile decision. There are three layers, and this page is the map of how a single knob travels through all three. **Layer 1 (flag ingest)** fabricates an `argv` from the `LIBTPU_INIT_ARGS` environment variable and hands it to `absl::ParseCommandLine`, which binds values onto the ~2048 `absl::Flag<T> FLAGS_<name>` globals that the `dlopen` constructor storm pre-registered. **Layer 2 (the two protos)** is where those bound flags become structured config: the generic `xla_*` flags land in `xla::DebugOptions` (290 wire-fields, the proto XLA shares across CPU/GPU/TPU), and the TPU-private `xla_tpu_*` / `xla_jf_*` / `megascale_*` flags land in `TpuCompilationEnvironment` (TCE) — a 1121-field master proto whose every field *is* a registered flag. **Layer 3 (the AUTO resolver)** is the part with no LLVM analogue: ~330 TCE fields are not plain values but `AutoProto` oneofs read through an `AutoOr<T>` tri-state (`AUTO` / `ENABLED` / `DISABLED`), and ~130 hand-written accessors collapse that tri-state into a concrete value using a per-knob polarity that *is* the documented default.

The reference frame for a reimplementer is XLA's own flag system, with two TPU-specific twists. First, the flag *registry* is built at load time, not at parse time: `ParseCommandLine` only binds values to a table the constructor storm already populated, so a reimplementation that defers flag *registration* to init time has nothing to parse against. Second, the "default" of a knob is not a single number — for the ~330 AutoProto fields it is a *resolution rule* baked into each consumer (`AUTO`→off vs `AUTO`→on), and on top of *that* the per-codename MSA overlay rewrites a family of fields per `TpuVersion`. The effective value a JAX user gets is `flag-default ⊕ AUTO-resolution-polarity ⊕ per-version-overlay`, evaluated lazily at the consumer.

This page is a **map**, not a manual. It orients the reader on the three layers, the registry-mediated dispatch, and the `TpuVersion`-aware prefix handling, then hands off to the sibling pages that own each sub-area in detail. Do not look here for the flag atlas, the field dictionaries, or the byte-level resolver tables — those are linked below and own their own internals.

For reimplementation, the contract is:

- **The flag→DebugOptions→TCE→effective-value pipeline** — the four stages a knob passes through, and which symbol owns each handoff.
- **The two protos** — what lands in `DebugOptions` vs `TpuCompilationEnvironment`, and why the `xla_tpu_*` family is *not* DebugOptions fields.
- **The AUTO resolver** — that ~330 TCE fields resolve through an `AutoOr<T>` tri-state whose polarity encodes the default, and where the per-version overlay finishes the job.

| | |
|---|---|
| **Flag ingest** | `tensorflow::tpu::GetLibTpuInitArguments @ 0x20ccca20` → `absl::ParseCommandLine` (inside `RealInitGoogle @ 0x210ae860`) |
| **Env funnel** | `LIBTPU_INIT_ARGS` (str @ file `0x918c880`); the *whole* registered flag set is settable through it |
| **Registered flags** | ~2048 `absl::Flag<T> FLAGS_<name>` globals; 2107 distinct names |
| **DebugOptions proto** | `xla::DebugOptions`, 290 wire-fields, 17 nested enums; `DefaultDebugOptionsIgnoringFlags @ 0x1e66a860` |
| **TCE proto** | `xla::jellyfish::TpuCompilationEnvironment`, 1121 fields, `sizeof 0x15e8`; `_table_ @ 0x21cfa9e0` |
| **Flag→TCE bridge** | `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` · `SetFieldFromFlagString @ 0x1d73fcc0` · `CreateDefaultTpuCompEnv @ 0x1d73dfa0` |
| **AUTO resolver** | `AutoOr<bool>::FromProtoOrDie @ 0xf795300` (`(present<<8)\|val`); ~130 `ObjectView<TCE>` accessors `0x1d6b6420..0x1d6b9f60` |
| **AutoProto** | `xla.jellyfish.AutoProto`, 30-arm oneof, `_table_ @ 0x21cfa788`; default instance `AutoProto_globals_ @ 0x223c8968` (all-AUTO) |
| **String parse** | `ParseAutoOrFromString<30> @ 0x1d7504c0` / `ReadAutoOr<30> @ 0x1d74ca00` |
| **Confidence** | CONFIRMED (byte-anchored vs decompile) unless a row or callout says otherwise |

---

## 1. The Pipeline at a Glance

### Purpose

A configuration knob in libtpu is a four-stage journey. The stages are owned by distinct subsystems, and the value's *meaning* changes at each: a text token becomes a typed flag value, becomes a proto field, becomes a resolved compile decision. This section draws the whole arc so the reader can place every sibling page on it; the stages themselves are detailed in §2–§5.

### The four stages

```text
STAGE 1 — INGEST  (env string → typed flag globals)            [env-vars.md, flag ingest]
  getenv("LIBTPU_INIT_ARGS")          0x918c880
    → GetLibTpuInitArguments          0x20ccca20   ── split on ' ' (absl::ByChar, AllowEmpty)
    → InitializeDriver argv build     0x204cecc0   ── ["./tpu_driver", <CloudTPU defaults>, <tokens>, NULL]
    → RealInitGoogle                  0x210ae860
        → absl::ParseCommandLine                    ── binds --xla_*/--xla_tpu_*/--megascale_* to FLAGS_<name>
                                                       (registry pre-built by the dlopen ctor storm)

STAGE 2a — DEBUGOPTIONS  (the xla_* subset → shared proto)     [debugoptions-proto.md]
  FLAGS_xla_foo  ↔  xla::DebugOptions.xla_foo                  ── 290 fields; only 2 standalone-flag-wired
    baseline = DefaultDebugOptionsIgnoringFlags   0x1e66a860    (rest reached via PJRT debug_options proto path)

STAGE 2b — TCE  (the xla_tpu_*/xla_jf_*/megascale_* set → TPU proto)  [tpu-compilation-environment.md]
  FLAGS_xla_tpu_foo  →  TpuCompilationEnvironment field        ── 1121 fields, 1:1 flag↔field
    CreateDefaultTpuCompEnv           0x1d73dfa0  ── fill from FLAGS_<name> defaults (+0x48 union)
    OverrideTpuCompEnvByCmdLineFlags  0x1d73e640  ── apply the parsed overrides
      SetFieldFromFlagString          0x1d73fcc0
      OverwriteFieldIfNotDefault      0x1d73f360

STAGE 3 — RESOLVE  (proto field → concrete compile value)     [autoproto-autoor-resolution.md]
  ~330 fields are AutoProto* (oneof) → AutoOr<T>::FromProtoOrDie  0xf795300
    → consumer polarity (AUTO=off / AUTO=on)  ── 130 accessors 0x1d6b6420..0x1d6b9f60
  67 fields are inline TristateProto enums   → cmpl $2,+OFF (ENABLED==2)
  + per-codename MSA overlay (v0..v5)         → effective per-TpuVersion value
```

> **NOTE —** the four stages are not a single call body. Stage 1 runs once, at `PJRT_Plugin_Initialize` time (see [../lifecycle/tftpu-initialize-bootstrap.md](../lifecycle/tftpu-initialize-bootstrap.md)). Stage 2 runs when a TCE is materialized for a compilation. Stage 3 runs *lazily, at each consumer* — there is no eager "resolve all knobs" pass. A reimplementer must not assume a flat config struct snapshotted at init; the effective value of an AutoProto knob is computed each time its accessor is called.

### Where the families go

The single most important structural fact is that the flag *families* split across the two protos. This is the GOOD/BAD divide a reimplementer must get right — `--xla_tpu_*` are **not** DebugOptions fields:

| Family | Count | Lands in | Detail page |
|---|---:|---|---|
| `xla_*` (generic) | 121 | `xla::DebugOptions` (290-field proto; most reached via PJRT, not standalone flags) | [debugoptions-proto.md](debugoptions-proto.md) |
| `xla_tpu_*` | 909 | `TpuCompilationEnvironment` (standalone, TPU-private) | [tpu-compilation-environment.md](tpu-compilation-environment.md) |
| `xla_jf_*` | 148 | `TpuCompilationEnvironment` (Jellyfish backend) | [flag-families.md](flag-families.md) |
| `xla_sc_*` / `barna_core_*` | 92 / 61 | `TpuCompilationEnvironment` (SparseCore / embedding) | [flag-families.md](flag-families.md) |
| `megascale_*` | 150 | standalone `absl::Flag` (DCN runtime, not TCE) | [flag-families.md](flag-families.md) |
| `xla_msa_*` / `xla_gf_*` / `xla_ior_*` / `xla_mosaic_*` / `xla_llo_*` | 49 | TCE + DebugOptions mix | [flag-families.md](flag-families.md) |
| `tpu_*` | 69 | runtime/cache/driver (not compile-time) | [flag-families.md](flag-families.md) |

> **QUIRK —** no `xla_gpu_*` flag is registered in this build (zero `AbslFlagHelpGenForxla_gpu_*` symbols), yet the GPU/CPU fields survive in the *shared* `DebugOptions` descriptor as inert metadata — the bulk of the 290 fields are GPU/CPU carryovers with no flag behind them on TPU. The TPU build strips the GPU flag wiring but keeps the GPU fields in the proto. A reimplementer enumerating DebugOptions fields will see GPU fields with no flag behind them — they are inert on TPU. (CONFIRMED; the carryover set is enumerated on [debugoptions-proto.md](debugoptions-proto.md).)

---

## 2. Stage 1 — Flag Ingest

### Purpose

A plugin `.so` has no `argv`. libtpu fabricates one from `LIBTPU_INIT_ARGS`, prepends a synthetic `argv[0]` and the Cloud-TPU default flags, and hands the result to `absl::ParseCommandLine`. Because the parse is a generic absl command-line parse, the entire registered flag set is settable through the one env var — there is no "init-args-only" subset.

### Algorithm

```c
function GetLibTpuInitArguments():                       // 0x20ccca20
    s = getenv("LIBTPU_INIT_ARGS")                       // str @ 0x918c880
    if s == NULL: return { {}, {} }                      // empty argv
    views = absl::StrSplit(s, ByChar(' '), AllowEmpty)   // plain space split — no shell tokenizing
    store = [ string(v) for v in views ]                 // owned std::string copies (24-B SSO)
    argv  = [ &str.data for str in store ]                // parallel char const* view
    return { store, argv }
```

The decompile of `0x20ccca20` confirms the body verbatim: `getenv("LIBTPU_INIT_ARGS")` at line 41, then an `absl::ByChar` / `AllowEmpty` `Splitter` `ConvertToContainer` into a `vector<string_view>`. The argv is then folded into the driver argv inside `InitializeDriver @ 0x204cecc0` and parsed inside `RealInitGoogle @ 0x210ae860`. The full ingest path — the lock gate, the once-guard, the Cloud-TPU default fold — is owned by [../lifecycle/tftpu-initialize-bootstrap.md](../lifecycle/tftpu-initialize-bootstrap.md) §2; this section only places it as Stage 1.

> **GOTCHA —** the split is a single-space `absl::ByChar(' ')` with `AllowEmpty`, not a shell tokenizer. `LIBTPU_INIT_ARGS="--xla_tpu_foo=1  --bar"` (double space) yields an empty-string token between the flags, and any flag *value* containing a space is split into separate (and likely rejected) tokens. A reimplementer must reproduce the plain space split, not a quote-aware one.

### What this section does *not* own

The env-var roster (`LIBTPU_INIT_ARGS`, `TPU_LOAD_LIBRARY`, `LIBTPU_ON_GCE`, `TPU_LIBRARY_PATH`) and their consumers are on [env-vars.md](env-vars.md). `TPU_LIBRARY_PATH` is set by the wheel's `__init__.py` before load (it points `dlopen` at the bundled `libtpu.so`); it is not a flag. The flag *registry* — the ~2048 `FLAGS_<name>` globals and how the `dlopen` constructor storm builds them — is the precondition for this parse and is owned by the lifecycle page and the atlas; the atlas enumerates the names on [xla-flag-atlas.md](xla-flag-atlas.md).

---

## 3. Stage 2 — The Two Protos

### Purpose

Once `ParseCommandLine` has bound values onto the flag globals, two distinct config structures consume them. Knowing which proto a flag lands in is the difference between finding its field and chasing a field that does not exist.

### `xla::DebugOptions` — the shared, generic proto

`DebugOptions` is the proto XLA shares across all backends; the `xla_*` (non-tpu) flags name its fields (`--xla_foo` ↔ field `xla_foo`). 290 live wire-fields are present in libtpu's descriptor pool (max field# 501, 211 numbering gaps); most are GPU/CPU carryovers inert on TPU, and a direct cross-match finds only two registered-flag intersections (`xla_tpu_detect_nan`, `xla_tpu_detect_inf`) — the classic dump/HLO knobs reach DebugOptions through the PJRT `CompileOptions.debug_options` proto path, not as standalone absl flags. The all-default baseline is `DefaultDebugOptionsIgnoringFlags @ 0x1e66a860` (confirmed in the decompile alongside `GetNonDefaultDebugOptions @ 0x1c920540` and `DumpNonDefaultDebugOptions @ 0x1c920d80`, which diff a config against that baseline for logging). The proto carries 17 nested enums (`StepMarkerLocation`, `CommandBufferSchedulingMode`, `WhileLoopUnrolling`, …) and an escape-hatch `xla_backend_extra_options` string→string map. Details, the proto-only field list, and the baseline values are on [debugoptions-proto.md](debugoptions-proto.md) and [default-debugoptions.md](default-debugoptions.md).

### `TpuCompilationEnvironment` (TCE) — the TPU-private master proto

The TCE is the TPU compiler's master config: a 1121-field proto (`sizeof 0x15e8` = 5608 B), whose every field is a registered `absl::Flag`. Its `_table_ @ 0x21cfa9e0` is the byte-exact field#→offset oracle: a `TcParseTableBase` header (`has_bits_offset=16`, `field_entries_offset=0x370`, `num_field_entries=1121`, `num_aux_entries=349`) followed by a 1121-entry `FieldEntry` array sorted by ascending field number, so `entry[i]` is the i-th live field and its offset is deterministic. The proto type histogram (bool 418, message 349, int64 148, enum 74, string 37, float 34, int32 32, double 14, uint32 11, uint64 4) is re-derivable from the `FieldEntry.type_card`.

The flag→TCE bridge is a small family of functions: `CreateDefaultTpuCompEnv @ 0x1d73dfa0` fills the env from the `FLAGS_<name>` default union (`FlagImpl+0x48`), then `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` applies the parsed overrides via `SetFieldFromFlagString @ 0x1d73fcc0` and `OverwriteFieldIfNotDefault @ 0x1d73f360`. The field#→offset mechanism, the byte-exact default census, and the per-field dictionaries are on [tpu-compilation-environment.md](tpu-compilation-environment.md), [tce-field-dictionary-a.md](tce-field-dictionary-a.md), [tce-field-dictionary-b.md](tce-field-dictionary-b.md), and [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md).

> **QUIRK —** the absl flag default is byte-authoritative; the help/error text is not. Two flags (`xla_tpu_rwb_fusion`, `xla_tpu_accumulate_into_mrb`) read `=false` from help-string text but their `FlagImpl+0x48` inline literal is `01 00 00 00` = TRUE. The help text describes the behavior the message is steering you toward, not the registered default. A reimplementer must read defaults from the flag/proto initializers, never from prose. (CONFIRMED — byte-corrected on [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md).)

---

## 4. Stage 3 — The AUTO Resolver

### Purpose

This is the layer with no XLA-CPU/GPU analogue. ~330 of the 1121 TCE fields are not plain values but `AutoProto` oneofs carrying a tri-state — `AUTO` (unset), `ENABLED`, or `DISABLED` — and the concrete compile value is computed lazily at the consumer. The mechanism is the `AutoOr<T>` template plus a hand-written accessor per knob whose polarity *is* the documented default.

### The tri-state, byte-exact

```c
function AutoOr<bool>::FromProtoOrDie(AutoProto& p):     // 0xf795300
    if p.oneof_case_ == 0:        return 0               // +0x1c == 0  ⇒ AUTO ⇒ absent
    v = AutoOrTypeTraits<bool>::FromAutoProto(p)         // 0xf7953e0 — reads body +0x10
    return v | 0x100                                     // SET present-bit (bit8); pack (present<<8)|val
```

The decompile of `0xf795300` confirms the `return v11 | 0x100u` packing exactly. The `AutoProto` memory layout is fixed: oneof body at struct `+0x10`, discriminator `oneof_case_` at `+0x1c` (the field number of the active arm; `0` = unset = AUTO). The packed return is type-class dependent — `(present<<8)|val8` for bool, `(present<<32)|val32` for int32/enum, `{value, has-bit}` for int64, full sub-message + has-flag for message. The all-AUTO default instance is `AutoProto_globals_ @ 0x223c8968` (oneof body and case both zero), and a null TCE field falls back to it — so an unset AutoProto knob always resolves through its consumer's polarity. The 30-arm oneof (8 hardcoded scalar arms, 10 reflection-dispatched enum arms, 12 message arms) is owned by [autoproto-message-arms.md](autoproto-message-arms.md); the resolver template and packing per type-class by [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md).

### Polarity is the default

The same tri-state encoding yields *opposite* defaults depending on which idiom the knob's author chose:

| Idiom | Test | AUTO resolves to | Meaning | Count |
|---|---|---|---|---:|
| `AUTO=off` | `not; test $0x101; sete` | FALSE | only explicit `true` enables | 45 bool |
| `AUTO=on` | `and $0x101; cmp $0x100; setne` | TRUE | only explicit `false` disables | 26 bool |
| int64 sentinel | `test $1,%dl; cmove rcx` | per-knob constant | e.g. `INT64_MAX`, `0`, `1024` | 18 |
| enum/int default | `bt $0x20; cmovb; else 0` | enum-0 / zero | the DEFAULT/first value | 11 |

So `MxuLatencyBalancingUseSequenceDependencies` (env `+0xbe8`, `AUTO=off`) defaults OFF, while `AllowSplitVmem` (env `+0x4a8`, `AUTO=on`) defaults ON — both backed by the same all-AUTO `AutoProto_globals_`. A second storage class exists: 67 fields are *inline* `TristateProto.Value` enums (a 4-byte field read by `cmpl $0x2,+OFF` where `ENABLED==2`), not AutoProto — e.g. `EnableLloLinter` reads `+0x15ac`. The full ~130-accessor census (offset → polarity) and the int64-sentinel table are on [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md).

### The ingest half — string → AutoProto

A user spells an override in `XLA_FLAGS` / `LIBTPU_INIT_ARGS` as a typed token (`auto` / `enabled` / `disabled` / a literal). The text→`AutoOr<T>` parse is `ParseAutoOrFromString<30> @ 0x1d7504c0` feeding `ReadAutoOr<30> @ 0x1d74ca00` (both confirmed present; the `<25>` and `<30>` arities are the two oneof sizes), with `TpuCompEnvReflection::ParseFlagFromString @ 0x1d74e8a0` as the field-keyed entry. The exact token grammar and the reverse unparse are owned by [autoor-parse-grammar.md](autoor-parse-grammar.md) and [autoor-unparse.md](autoor-unparse.md).

> **GOTCHA —** there is no eager "snapshot the config" step for AutoProto knobs. The resolver runs at every accessor call and falls back to `AutoProto_globals_` when the field is unset. A reimplementer who materializes a flat config struct at init and reads it thereafter will miss the AUTO fallback polarity (and the per-version overlay below), silently shipping the wrong default for ~330 fields.

---

## 5. Registry-Mediated Dispatch and the Per-Version Overlay

### Registry-mediated flags

The flag→field correspondence is not hardwired call-by-call; it goes through proto reflection. `TpuCompEnvReflection` (`ReadFlag @ 0x1d74af60`, `ParseFlagFromString @ 0x1d74e8a0`) looks a field up by name in the TCE descriptor, then `SetFieldFromFlagString @ 0x1d73fcc0` writes it. The serialize/normalize direction maps a parsed `AutoOr<T>` into a 20-alternative TCE variant (`i8,u8,i32,i64,u32,u64,double,float,bool,string,RangeSpecProto,…,AutoProto`) via `NormalizeFieldType<T>` and `TpuCompEnvReflection::SetEnvField`. This reflection mediation is what lets one generic bridge serve all 1121 fields; the registry-mediated path is detailed on [registry-mediated-flags.md](registry-mediated-flags.md).

### TpuVersion-aware prefix dispatch

The same logical knob can be addressed with a version-qualified prefix so that one flag name selects different storage per TPU generation, and the per-codename MSA overlay rewrites a family of fields per `TpuVersion` *on top of* the flag default. The flag-default baseline this page describes is therefore not the shipped value: the effective per-codename default is `baseline ⊕ family-overlay` (v0/1→jf, v2→cmem, v3→vf, v4/5→gf), applied by `ComputeMemorySpaceAssignmentOptions` and `OverwriteFieldIfNotDefault @ 0x1d73f360`. The prefix-dispatch mechanism and the per-version overlay are owned by [flag-prefix-dispatch.md](flag-prefix-dispatch.md); the per-codename MSA values are owned by the memory subsystem pages.

> **NOTE —** the specific prefix-strip/version-select string mechanism was not byte-traced on this page (a generic grep for prefix-handling symbols did not isolate it; the overlay-via-`OverwriteFieldIfNotDefault` path *is* confirmed). The flag→reflection→field bridge and the `OverwriteFieldIfNotDefault` overlay are CONFIRMED; the exact `TpuVersion`-aware prefix-token handling is owned by [flag-prefix-dispatch.md](flag-prefix-dispatch.md) and is LOW confidence here. (LOW)

---

## Related Components

| Component | Relationship |
|---|---|
| `GetLibTpuInitArguments @ 0x20ccca20` | Stage 1 — the `LIBTPU_INIT_ARGS` ingest |
| `RealInitGoogle @ 0x210ae860` | hosts the `absl::ParseCommandLine` that binds flags |
| `xla::DebugOptions` | Stage 2a — the shared generic proto for `xla_*` flags |
| `TpuCompilationEnvironment @ _table_ 0x21cfa9e0` | Stage 2b — the 1121-field TPU master proto |
| `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` | the flag→TCE bridge |
| `AutoOr<bool>::FromProtoOrDie @ 0xf795300` | Stage 3 — the tri-state resolver |
| `AutoProto_globals_ @ 0x223c8968` | the all-AUTO default instance for unset knobs |

## Cross-References

- [../lifecycle/tftpu-initialize-bootstrap.md](../lifecycle/tftpu-initialize-bootstrap.md) — Stage 1 in full: the `LIBTPU_INIT_ARGS` ingest, the once-guards, the Cloud-TPU default fold, the `argv` build
- [env-vars.md](env-vars.md) — the env-var roster (`LIBTPU_INIT_ARGS`, `TPU_LOAD_LIBRARY`, `LIBTPU_ON_GCE`, `TPU_LIBRARY_PATH`) and their consumers
- [xla-flag-atlas.md](xla-flag-atlas.md) — the ~2107-name flag surface and the `AbslFlagHelpGenFor` enumeration method
- [flag-families.md](flag-families.md) — the family breakdown (`xla_tpu_*`, `xla_jf_*`, `megascale_*`, `xla_sc_*`, `barna_core_*`, …) and which proto each lands in
- [debugoptions-proto.md](debugoptions-proto.md) — `xla::DebugOptions`: 290 wire-fields, 17 enums, the GPU/CPU-carryover vs flag-wired split
- [default-debugoptions.md](default-debugoptions.md) — the `DefaultDebugOptionsIgnoringFlags @ 0x1e66a860` baseline
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the TCE proto, `_table_` header, field#→offset mechanism
- [tce-field-dictionary-a.md](tce-field-dictionary-a.md) / [tce-field-dictionary-b.md](tce-field-dictionary-b.md) — the per-field name/type/meaning dictionaries
- [tce-field-offsets-defaults.md](tce-field-offsets-defaults.md) — the byte-exact field#→offset→default reference
- [autoproto-autoor-resolution.md](autoproto-autoor-resolution.md) — the `AutoOr<T>` tri-state, the ~130-accessor polarity census, the int64-sentinel table
- [autoor-parse-grammar.md](autoor-parse-grammar.md) — the string→AutoProto parse (`auto`/`enabled`/`disabled`/literal grammar)
- [autoor-unparse.md](autoor-unparse.md) — the reverse `AbslUnparseFlag<AutoOr<T>>` direction
- [autoproto-message-arms.md](autoproto-message-arms.md) — the 30-arm AutoProto oneof and its 12 message arms
- [registry-mediated-flags.md](registry-mediated-flags.md) — the reflection-mediated flag→field bridge (`TpuCompEnvReflection`)
- [flag-prefix-dispatch.md](flag-prefix-dispatch.md) — the `TpuVersion`-aware prefix dispatch and the per-codename overlay
