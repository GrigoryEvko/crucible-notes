# TpuVersion-Aware Flag-Prefix Dispatch

> *All addresses, symbols, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, ELF x86-64 DYN, not stripped). Other versions differ.*

## Abstract

libtpu carries seven recognizable per-subsystem flag-prefix namespaces — the four TPU-generation codenames `xla_jf_*` (Jellyfish), `xla_pf_*` (Pufferfish), `xla_vf_*` (Viperfish), `xla_gf_*` (6acc60406/v7x), plus `xla_sc_*` (SparseCore), `barna_core_*` (embedding engine), and the generic `xla_tpu_*` master surface — and the obvious question a reimplementer asks is "does an `xla_jf_*` flag no-op when the active generation is Viperfish?" This page recovers the answer from the binary, and the answer is **no, and the question is malformed**: there is no generation gate on flag registration or on flag *application*. The codename in a flag name is a static authoring namespace, not a runtime routing key.

The reference frame is the [flag-families](flag-families.md) routing map, which establishes the prefix → sink classification (generic `xla_*` → `DebugOptions`; `xla_tpu_*` / codenames / `xla_sc_*` / `barna_core_*` → the `TpuCompilationEnvironment` (TCE); `megascale_*` / `tpu_*` → standalone runtime). This page is the layer below: the actual *dispatch*. Every TCE-routed flag — including all four codename families — is registered unconditionally as one `absl::Flag<T>`, bound 1:1 to one TCE proto field through a single global flag↔field hash map (`FlagFieldMappings`), and applied by a reflection loop that walks **every** field of the TCE descriptor with **no `TpuVersion` parameter anywhere in its signature**. An `xla_jf_*` flag and an `xla_vf_*` flag are equal citizens in that loop; the only difference between them is *which TCE field each writes* and *which consumer subsequently reads that field for the active generation*.

The active generation enters compile-environment setup through a completely separate channel — `AcceleratorTypeToTpuVersionEnum @ 0x204cf620` parses the user's `accelerator_type` string (`v5e`, `v5p`, `v6e`, `tpu7x`, …) into a `TpuVersion` ordinal — and that ordinal then drives *data* selection (the per-gen `_chip_parts.binarypb` defaults via `DefaultsForVersion @ 0x20b1b040`) and *codec/HAL family* selection (`TpuCodec::Create @ 0x1e835fa0`), not flag selection. The one place where a flag *name* is genuinely keyed by `TpuVersion` is a pair of narrow legacy-MSA registries (`Legacy{Evictions,Prefetches}FlagRegistry`, `flat_hash_map<TpuVersion, …>`); everything else is gen-blind.

For reimplementation, the contract is:

- **The non-gating model** — codename-prefixed flags are unconditionally registered and unconditionally applied; the prefix is a namespace, not a dispatch arm. Do not build a per-generation registration switch.
- **The active-gen → codename → data binding** — how a `TpuVersion` ordinal lowers to a codename string (`TpuVersionToString`) and feeds the embedded `<codename>_chip_parts.binarypb` default-proto path and the per-codename codec/HAL family, all keyed on the same ordinal table.
- **The single gen-keyed exception** — the legacy MSA eviction/prefetch per-`TpuVersion` flag-name maps, and why they are the only `flat_hash_map<TpuVersion,…>` flag registry in the binary.

| | |
|---|---|
| **Flag→field bridge (apply on override)** | `OverrideTpuCompEnvByCmdLineFlags @ 0x1d73e640` |
| **Flag→field bridge (build defaults)** | `CreateDefaultTpuCompEnv @ 0x1d73dfa0` |
| **Per-field flag lookup (gen-blind)** | `TpuCompEnvReflection::GetFlagForField @ 0x1d74ad40` |
| **Per-flag field lookup (inverse)** | `TpuCompEnvReflection::GetFieldForFlag @ 0x1d74ab20` |
| **Global flag↔field map** | `FlagFieldMappings::GetInstance` (`absl::NoDestructor`, ctor @ `0x1d753ce0`) |
| **Per-flag writer** | `SetFieldFromFlagString @ 0x1d73fcc0` |
| **Active-gen entry (string→ordinal)** | `AcceleratorTypeToTpuVersionEnum @ 0x204cf620` (`libtpu_init_utils.cc`) |
| **Ordinal → codename string** | `TpuVersionToString @ 0x20b3a480` → table `off_22011BF0` |
| **Ordinal → per-gen defaults proto** | `TpuChipParts::DefaultsForVersion @ 0x20b1b040` |
| **Ordinal → codec/HAL family** | `TpuCodec::Create @ 0x1e835fa0` |
| **Gen-keyed flag-name maps (the exception)** | `LegacyEvictionsFlagRegistry @ 0x1c6f8760`, `LegacyPrefetchesFlagRegistry @ 0x1c6f8940` |

---

## At-a-Glance: Prefix, Generation, and Gating

The per-prefix counts are the registration-symbol-true figures from [flag-families](flag-families.md) (`AbslFlagHelpGenFor<prefix>` symbols). The **Gating** column is what this page recovers: for every TCE-routed family the answer is the same — *unconditional registration, gen-blind application*. The codename column records the authoring association, not a runtime filter.

| Prefix | Authoring gen / codename | Count | Gating mechanism |
|---|---|---:|---|
| `xla_tpu_*` | none (all gens) | 909 | unconditional register; gen-blind reflection apply |
| `xla_jf_*` | Jellyfish (v0) namespace, all-gen compiler core | 148 | unconditional register; gen-blind reflection apply |
| `xla_sc_*` | none (SparseCore backend) | 92 | unconditional register; gen-blind reflection apply |
| `barna_core_*` | none (embedding runtime) | 61 | unconditional register; gen-blind reflection apply |
| `xla_vf_*` | Viperfish (v3) VMEM/MSA | 16 | unconditional register; gen-blind reflection apply |
| `xla_gf_*` | 6acc60406 (`TpuVersion` 5, v7x) VMEM/MSA | 14 | unconditional register; gen-blind reflection apply |
| `xla_pf_*` | Pufferfish (v2) ND all-reduce | 1 | unconditional register; gen-blind reflection apply |
| *(legacy MSA evict/prefetch)* | per-`TpuVersion` flag-name map | n/a | **gen-keyed** `flat_hash_map<TpuVersion,…>` |
| `xla_gl_*` | (Ghostlite codename) | **0** | absent — no flag, no string |

> **NOTE —** there is **no active-gen gating** on flag registration or application. There is no per-generation registration switch and no per-generation application filter: all TCE flags — codename-prefixed or not — register unconditionally and are written into their TCE field whenever present on the command line, regardless of the active `TpuVersion`. An `xla_jf_*` flag does not no-op on Viperfish, nor carry a gen-specific default. Per-generation behavior lives entirely in (a) the *default* proto loaded per codename (`DefaultsForVersion`) and (b) which fields each consuming pass reads for the active gen. `OverrideTpuCompEnvByCmdLineFlags` and `CreateDefaultTpuCompEnv` take no `TpuVersion`, and the reflection loop walks the full descriptor.

> **GOTCHA —** the catalogued `xla_gl_*` / Ghostlite codename family does **not** exist in this build: zero `xla_gl_*` strings, zero registration symbols. The codename `ghostlite` is live as a *silicon* codename (`TpuVersion=4`, `TpuVersionToString` slot 4) but reserves no flag-prefix namespace. A reimplementer must not allocate a `gl` flag arm. The live codename flag-prefix set is `{jf, pf, vf, gf}`.

---

## 1. The Dispatch Mechanism

### Purpose

A flag arriving through `LIBTPU_INIT_ARGS` (e.g. `--xla_vf_max_vmem_used_by_memory_space_assignment=...`) must end up in the right `TpuCompilationEnvironment` field. This section recovers *how* that binding is performed — and demonstrates that the binding is generation-independent. The codename in the flag name is consumed only at authoring time (it disambiguates the field name); at runtime the codename token is opaque, hashed away inside a `FieldDescriptor*`-keyed map.

### Entry Point

```text
PJRT init / TpuPlatform setup
  └─ AcceleratorTypeToTpuVersionEnum (0x204cf620)   ── parse "v5e"/"tpu7x"/… → TpuVersion ordinal
  └─ CreateDefaultTpuCompEnv (0x1d73dfa0)           ── seed every TCE field from its flag's current value
       └─ TpuCompEnvReflection::GetFlagForField (0x1d74ad40)  ── field → flag, via FlagFieldMappings
       └─ TpuCompEnvReflection::ReadFlag                       ── flag value → variant
       └─ TpuCompEnvReflection::SetEnvField                    ── variant → TCE field
  └─ OverrideTpuCompEnvByCmdLineFlags (0x1d73e640)  ── re-walk; override fields whose flag WasPresentOnCommandLine
       └─ (same reflection trio)
       └─ SetFieldFromFlagString (0x1d73fcc0)        ── per-field string parse + write
```

### Algorithm

The override bridge is the canonical view. Note the loop bound and the absence of any `TpuVersion` discriminator.

```c
function OverrideTpuCompEnvByCmdLineFlags(env):        // sub_1d73e640
    meta = TpuCompilationEnvironment::GetMetadata()     // global descriptor metadata
    n    = meta.field_count                              // *(int*)(meta+8)
    if n <= 0: return OK
    overridden = []                                      // for the deprecation report
    for i in 0 .. n-1:                                   // walks EVERY TCE field; v8 += 88 per field
        field = meta.fields[i]                           // 88-byte FieldDescriptor record
        flag  = GetFlagForField(field)                   // sub_1d74ad40 — one map, no gen key
        if not flag.ok(): fail("Flag is not found for field")
        if not WasPresentOnCommandLine(flag):            // absl::flags_internal — user set it?
            continue                                     // untouched flags keep their default
        old = GetFieldValueAsString(field, env)
        // log "Overriding flag <name> to <new>; Old value was: <old>"
        if field.is_deprecated():                        // *(field+56)+125 == 1
            overridden.push(field.name)
        value = ReadFlag(flag, field)                    // current flag value → 20-arm variant
        SetEnvField(value, field, env)                   // write into env's proto field
    if overridden not empty:
        LOG(WARNING) << "[DEPRECATED_XLA_TPU_FLAG_USE] Deprecated "
                     << "TpuCompilationEnvironment flags were overridden: "
                     << join(overridden, ", ")
    return OK
```

`CreateDefaultTpuCompEnv @ 0x1d73dfa0` is the mirror image: the same full-descriptor walk, but it *seeds* every field from `ReadFlag` (the flag's current value, default if unset) rather than gating on `WasPresentOnCommandLine`. It then diffs the result against `GetTpuCompEnvWithDefaultValues()` via `MessageDifferencer::CompareWithFields` and emits the parallel warning `[DEPRECATED_XLA_TPU_FLAG_USE] Deprecated TpuCompilationEnvironment flags were present and not matching their default values:` (string at the call site, source line 5776 of `tpu_compilation_environment.cc`). Its only non-descriptor argument is a `SparseDenseMatmulFdoConfig*` — there is no `TpuVersion`.

> **NOTE —** the deprecation report is the closest thing in the binary to a "this flag does not apply here" signal, and it is *not* generation-scoped. A flag is flagged deprecated by a static bit on its `FieldDescriptor` (offset +56 → +125), the same for every target. A codename flag set on the "wrong" generation produces no warning and no error: it is written into its field exactly as on the "right" generation, and simply goes unread.

### The Flag↔Field Map

`GetFlagForField @ 0x1d74ad40` resolves a `FieldDescriptor*` to its `absl::CommandLineFlag*` through a single process-global structure built lazily on first use:

```c
function GetFlagForField(field):                         // sub_1d74ad40
    once: FlagFieldMappings::GetInstance()               // absl::NoDestructor, ctor 0x1d753ce0
    map = FlagFieldMappings.singleton                    // Swiss-table: FieldDescriptor* → CommandLineFlag*
    h   = crc32_hash(field)                               // MixingHashState over the pointer
    slot = swiss_table_probe(map, field, h)              // SSE group scan (vpcmpeqb / vpmovmskb)
    if slot found: return flag
    return Error("Flag is not found for field")          // tpu_compilation_environment_reflection.cc:80
```

`GetFieldForFlag @ 0x1d74ab20` is the inverse direction (flag → field), used by `SetFieldFromFlagString`. Both consult the *same* `FlagFieldMappings` instance. The map is keyed by raw `FieldDescriptor*` / `CommandLineFlag*` identity and is constructed once at static-init time covering the entire TCE surface; there is no per-generation variant and no rebuild on `TpuVersion` change. This is the structural proof that codename prefixes are not a runtime dispatch axis: by the time a flag reaches the apply loop, its `jf`/`vf`/`gf` token has already been collapsed into a pointer-keyed map entry shared across all generations.

### Function Map

| Function | Address | Role |
|---|---|---|
| `OverrideTpuCompEnvByCmdLineFlags` | `0x1d73e640` | full-descriptor walk; override present flags; emit deprecation report |
| `CreateDefaultTpuCompEnv` | `0x1d73dfa0` | full-descriptor walk; seed defaults from flags; diff vs canonical defaults |
| `SetFieldFromFlagString` | `0x1d73fcc0` | per-flag: field lookup → `ParseFlagFromString` → `SetEnvField` |
| `TpuCompEnvReflection::GetFlagForField` | `0x1d74ad40` | field → flag via `FlagFieldMappings` Swiss-table |
| `TpuCompEnvReflection::GetFieldForFlag` | `0x1d74ab20` | flag → field (inverse) |
| `FlagFieldMappings` ctor | `0x1d753ce0` | builds the global flag↔field map (once) |

### Considerations

The 20-arm `std::variant` woven through every reflection call (`a/h/i/l/j/m/d/f/b/string/RangeSpecProto/RepeatedStrings/SparseDenseMatmulFdoConfig/SlicedPrefetchOptions/MemoryBoundLoopOptimizerOptions/PreferredPrefetchOverrides/MsaSortOrderOverrides/BufferContentsSanitizerConfig/BufferIsolationConfig/AutoProto`) is the field-type universe, not a generation universe. A reimplementer matching the binary must reproduce the variant type list and the per-type `NormalizeFieldType<T>` specializations (one `.text` function per arm, e.g. `NormalizeFieldType<TristateFlag> @ 0x1d761080`), but needs no `TpuVersion`-conditional logic in any of them.

---

## 2. Active-Gen → Codename Binding

### Purpose

The active generation is real and does drive behavior — just not flag selection. This section recovers the chain from the user-facing `accelerator_type` string to the `TpuVersion` ordinal, and from the ordinal to the two things it actually selects: the per-generation default-knob proto and the per-codename codec/HAL family. These are *data* and *factory* selections keyed on the ordinal, parallel to but disjoint from the flag pipeline of §1.

### Entry Point — String to Ordinal

`AcceleratorTypeToTpuVersionEnum @ 0x204cf620` (in `libtpu::(anonymous)`, source `learning/45eac/tfrc/runtime/libtpu_init_utils.cc`) is where a generation first becomes a number. It splits the `accelerator_type` on `-` (`"<version>-<cores>"`), lowercases the first token, and maps it:

```c
function AcceleratorTypeToTpuVersionEnum(accel_type):     // sub_204cf620
    parts = split(accel_type, '-')                         // "v5e-256" → ["v5e","256"]
    if parts.size != 2: return Error("...not in the format of '<tpu_version>-<core_count>'...")
    v = lower(parts[0])
    switch v:                                              // string compares, ordinals are TpuType-side
      "v4lite" -> 4     "v2"  -> 1     "v3"  -> 2     "v4"  -> 3
      "v5lite"/"v5e" -> 5     "v5p" -> 6
      "v6e"/"v6ea"   -> 7
      "tpu7x"/"tpu7" -> 8
      default        -> Error("Unsupported accelerator type: " + accel_type)
    return v
```

> **QUIRK —** the ordinals this parser emits are the public `superpod::routing::TpuType` enum values (`v2`→1, `v5e`→5, `v5p`→6, `v6e`→7, `tpu7x`→8), **not** the internal `TpuVersion` enum (`kJellyfish`=0 … `k6acc60406`=5). The two enums are reconciled elsewhere; a reimplementer must keep them distinct. `IsAtLeastTPU7x @ 0x204cfda0` is a thin capability gate built directly on this parser — it returns `parsed_type >= 8`, i.e. `tpu7x`/`tpu7`. That is the entire body of the "is this at least the newest generation" check.

### Ordinal to Codename String

`TpuVersionToString @ 0x20b3a480` is a bounds-checked table index — the canonical ordinal → codename lowering:

```c
function TpuVersionToString(v):                  // sub_20b3a480
    if v >= 6: LOG(FATAL) << "Invalid TPU version " << v   // tpu_version.cc:152
    return off_22011BF0[v]                         // 6-entry .data.rel.ro pointer table
```

The six relocated table entries are, in order: `jellyfish`, `dragonfish`, `pufferfish`, `viperfish`, `ghostlite`, `6acc60406` (`TpuVersion` 0..5). This table is the single source of truth that ties an ordinal to a codename string; both the defaults-proto path and the diagnostic name paths read through it.

### Ordinal to Per-Gen Defaults Proto

`TpuChipParts::DefaultsForVersion @ 0x20b1b040` is where the codename string becomes a *data* selector. It is the clearest demonstration that the per-generation axis is data, not flag routing:

```c
function DefaultsForVersion(version, variant):    // sub_20b1b040
    name = AsciiStrToLower(TpuVersionToString(version))    // ordinal → "viperfish"
    if variant: name = name + "_" + variant                // optional chip variant suffix
    path = "embed://tpu_chip_parts/" + name + "_chip_parts.binarypb"
    // e.g. "embed://tpu_chip_parts/viperfish_chip_parts.binarypb"
    proto = TpuChipPartsProto()
    ReadBinaryProto(Env::Default(), path, &proto)          // tpu_chip_parts.cc:343 (CHECK OK)
    return TpuChipParts::FromProto(proto)
```

The embedded resource name is assembled from the lowered codename: literals `"embed://tpu_chip_parts/"` (23 bytes) and `"_chip_parts.binarypb"` (20 bytes) bracket the codename. Each generation thus ships a *distinct* default-parameter proto baked into the binary; selecting a generation selects which proto's defaults seed the compile. The TCE flag *defaults* established in §1's `CreateDefaultTpuCompEnv` and the chip-parts defaults here are independent default sources — flags default per their `absl::Flag<T>` static default, chip parts default per the per-gen `.binarypb`.

### Ordinal to Codec / HAL Family

The same ordinal also selects the codec and HAL family — a direct switch, the codec/HAL counterpart to the flag map:

```c
function TpuCodec::Create(version):               // sub_1e835fa0
    switch version:
      0: return CreateTpuCodecJellyfish()          // jxc family
      1: return CreateTpuCodecDragonfish()          // jxc family
      2: return CreateTpuCodecPufferfish()          // pxc family
      3: return CreateTpuCodecViperfish()           // vxc family
      4: return CreateTpuCodecGhostlite()           // gxc/glc family
      5: return sub_1E838380()                       // gxc/gfc family (6acc60406, anonymous class)
```

This is the codec/HAL-family selection the topic asks about: it is keyed on the *ordinal*, exactly like the chip-parts path, and shares no machinery with the flag dispatch. The codename → family-tag relationship (`jellyfish/dragonfish`→`jxc`, `pufferfish`→`pxc`, `viperfish`→`vxc`, `ghostlite/6acc60406`→`gxc`) is the same naming-parallel relationship as the flag prefixes, but family selection happens here, not in the flag layer.

> **NOTE —** the relationship between a flag *prefix* and a codec/HAL *family* is purely lexical: `xla_vf_*` flags and the `vxc`/Viperfish codec both descend from the `viperfish` codename, but neither selects the other. A flag prefix is chosen by the engineer who authored the knob to scope it to a generation's tuning; the codec family is chosen at runtime by `TpuVersion` ordinal. They meet only in the codename string, never in a shared dispatch.

### Function Map

| Function | Address | Role |
|---|---|---|
| `AcceleratorTypeToTpuVersionEnum` | `0x204cf620` | parse `accelerator_type` string → `TpuType` ordinal |
| `IsAtLeastTPU7x` | `0x204cfda0` | capability gate: parsed ordinal `>= 8` |
| `TpuVersionToString` | `0x20b3a480` | ordinal → codename string (table `off_22011BF0`) |
| `TpuChipParts::DefaultsForVersion` | `0x20b1b040` | ordinal → `<codename>_chip_parts.binarypb` defaults |
| `TpuCodec::Create` | `0x1e835fa0` | ordinal → per-codename codec / HAL family |

---

## 3. The One Gen-Keyed Flag Path

### Purpose

Exactly one mechanism in the binary keys a flag *name* on `TpuVersion`: a pair of legacy memory-space-assignment registries. They are the genuine exception to §1's gen-blind rule, and recovering them prevents a reimplementer from either missing the per-gen indirection or over-generalizing it to the whole flag surface.

### Mechanism

`LegacyEvictionsFlagRegistry @ 0x1c6f8760` and `LegacyPrefetchesFlagRegistry @ 0x1c6f8940` are `util_registration::StaticMapBase` specializations whose value map is:

```text
flat_hash_map<tpu::TpuVersion,
              std::pair<const char* /*flag name*/, string_view /*location*/>>
```

i.e. a `TpuVersion` → flag-name map. At static-init each generation that has a legacy eviction (or prefetch) knob registers its *own* flag name against its ordinal via `InsertValue(TpuVersion, name, sourceloc)`. The map enforces single-definition-per-key — a duplicate registration for the same `TpuVersion` is a `LOG(FATAL)` "Attempting to redefine value for key …" (`static_map.h:141`). A consumer that needs "the legacy eviction policy flag for the active generation" looks the active `TpuVersion` up in this map and reads the resolved flag, rather than hard-coding one flag name.

### Why It Is the Exception

These two registries exist because the legacy MSA eviction/prefetch knobs predate the uniform TCE reflection surface and kept their per-generation flag *names* (rather than one TCE field consumed conditionally). They are the only `flat_hash_map<TpuVersion, …>` flag registries in the binary; the bulk surface (909 `xla_tpu_*`, all codename families, `xla_sc_*`, `barna_core_*`) routes through the single gen-blind `FlagFieldMappings`. A reimplementer should model the general case as §1 and treat these two registries as a bounded legacy carve-out.

### Function Map

| Function | Address | Role |
|---|---|---|
| `LegacyEvictionsFlagRegistry::InsertValue` | `0x1c6f8760` | per-`TpuVersion` eviction-flag-name registration |
| `LegacyPrefetchesFlagRegistry::InsertValue` | `0x1c6f8940` | per-`TpuVersion` prefetch-flag-name registration |

> **GOTCHA —** do not generalize these registries. Finding a `flat_hash_map<TpuVersion,…>` keyed flag registry might suggest the whole flag system is gen-keyed; it is not. These two `sync_flag_util` maps are a localized legacy mechanism for MSA eviction/prefetch only. Every other TCE flag — including all `jf`/`pf`/`vf`/`gf` codename flags — is resolved by the pointer-keyed, generation-blind `FlagFieldMappings` of §1.

---

## Related Components

| Component | Relationship |
|---|---|
| `FlagFieldMappings::GetInstance` (ctor `0x1d753ce0`) | the one global flag↔field map; collapses the codename token to a `FieldDescriptor*` key |
| `TpuCompEnvReflection::ReadFlag` / `SetEnvField` | flag-value ↔ TCE-field-value transfer, per-field, gen-blind |
| `NormalizeFieldType<T>` family (e.g. `0x1d761080`) | per-variant-arm type coercion; one per field type, none per generation |
| `GetTpuCompEnvWithDefaultValues` | canonical TCE default snapshot used by `CreateDefaultTpuCompEnv`'s diff |
| `off_22011BF0` (TpuVersionToString table) | ordinal → codename string; shared by the defaults-proto and diagnostic paths |

## Cross-References

- [flag-families.md](flag-families.md) — the prefix → owner / sink routing map; this page is the dispatch layer beneath it (registration and application)
- [xla-flag-atlas.md](xla-flag-atlas.md) — the flat per-flag catalog and subsystem keyword taxonomy the codename families index into
- [flag-catalog-full.md](../appendix/flag-catalog-full.md) — the appendix establishing the per-prefix registration counts cited here
- [tpu-compilation-environment.md](tpu-compilation-environment.md) — the TCE master proto whose every field this dispatch walks; field layout and the reflection surface
- [registry-mediated-flags.md](registry-mediated-flags.md) — the `TpuCompEnvReflection` flag↔field bridge that serves all TCE families through one generic, gen-blind path
- [overview.md](overview.md) — the three-layer config pipeline and the `AutoProto` tri-state knobs the variant arms carry
- [codename-cheatsheet.md](../front/codename-cheatsheet.md) — the `TpuVersion` ordinal ↔ codename ↔ public-name table behind `TpuVersionToString` and `AcceleratorTypeToTpuVersionEnum`
