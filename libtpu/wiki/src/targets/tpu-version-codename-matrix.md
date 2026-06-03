# TPU Version Codename Matrix

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`libtpu.so` is the PJRT plugin that XLA loads to compile and run programs on Google TPU silicon. Every code path that depends on which generation of TPU it is targeting — HAL factory routing, ISA codec selection, bundle encoding, chip-constant lookup — keys off a single 6-value C++ enum, `tpu::TpuVersion`. The enumerators, recovered from CHECK strings and mangled symbol names, are `kJellyfish=0`, `kDragonfish=1`, `kPufferfish=2`, `kViperfish=3`, `kGhostlite=4`, and `k6acc60406=5`. This page is the authoritative reconciliation of that enum against the four other identity spaces it touches: the on-wire protobuf enum, the internal codename string, the external "TPU vN" display name, and the HAL family that services it.

The structure mirrors the LLVM target-triple problem. Just as a triple like `nvptx64-nvidia-cuda` resolves to a `Triple::ArchType` integer that gates every backend decision, `TpuVersion` is the integer that gates every TPU-specific decision in libtpu — but unlike a triple, it is never spelled in user text. Users supply an `accelerator_type` string (`v5p`, `v6e`, `tpu7x`); the library translates that to `TpuVersion` through a parser, and from there everything is integer dispatch. The complication this page exists to settle is that the integer the runtime uses internally (`TpuVersion`, 0-based, chronological) is **not** the integer that travels on the wire (`TpuVersionProto`, 1-based, with `TPU_VERSION_INVALID=0` reserved). The two are off by one, and conflating them is the single most common error in reading this binary.

The page opens with the one compiled artifact that pins the enum-to-codename mapping beyond dispute — the `.data.rel.ro` pointer table behind `TpuVersionToString` — then the three source-of-truth functions that read it, then the full five-axis cross-walk, then a per-codename feature matrix, and closes with the confidence accounting that says which rows are pinned by guard code and which rest on declaration order.

For reimplementation, the contract is:

- The 6-value `TpuVersion` enum, its integer assignments, and the bounds check (`version < 6`) that every consumer performs before indexing a per-version table.
- The off-by-one relationship `internal = proto - 1` between `TpuVersion` and `TpuVersionProto`, and the three functions that materialize it.
- The enum-int ↔ codename ↔ external-name ↔ HAL-family cross-walk, so a reimplementation routes the same silicon to the same factory, codec, and bundle encoder.

| | |
|---|---|
| **Enum** | `tpu::TpuVersion` — 6 values, `kJellyfish=0` … `k6acc60406=5` |
| **Canonical map** | `tpu::TpuVersionToString` @ `0x20b3a480` indexing `off_22011BF0` (6-entry rel.ro pointer table) |
| **Wire-form map** | `tpu::TpuVersionFromProto` @ `0x20b3a8c0` — `switch(proto)`, internal = proto − 1 |
| **Flag unparse** | `tpu::AbslUnparseFlag` @ `0x20b3ab40` — re-indexes the same `off_22011BF0` + length table |
| **External name** | `tpu::TpuVersionToExternalName` @ `0x20b3a500` — switch to `"TPU vN [lite]"` |
| **Length table** | `unk_BDF3BD8` @ `0xBDF3BD8` — parallel 6-entry per-codename byte lengths |
| **Bounds** | every reader checks `version >= 6` → `LogMessageFatal` at `tpu_version.cc:152` |
| **Source file** | `learning/45eac/tpu/runtime/tpu_version.cc` (recovered from fatal-log strings) |

---

## The Canonical Pointer Table

The indisputable enum-to-codename mapping is not a switch statement and not a string built at runtime. It is a six-entry pointer array compiled into `.data.rel.ro` at virtual address `0x22011BF0`. Each slot holds a relocated pointer (`R_X86_64_RELATIVE`) into `.rodata`, and the relocation target at slot `N` is the null-terminated codename for `TpuVersion N`. Because the array is materialized at link time and indexed directly by the enum integer, it is the root that every other identity axis hangs off.

`tpu::TpuVersionToString` (`0x20b3a480`, 115 bytes) is the canonical reader. Its body is a bounds check followed by a two-table load:

```c
const char *TpuVersionToString(unsigned version) {     // sub_20B3A480
    if (version >= 6) {                                // bounds guard
        LogMessageFatal("learning/45eac/tpu/runtime/tpu_version.cc", 152);
        log << "Invalid TPU version " << (TpuVersion)version;   // fatal
    }
    return off_22011BF0[version];                      // rel.ro pointer table
    // a parallel load of unk_BDF3BD8[version] returns the length in rdx
}
```

The disassembly pins the table address and the first entry directly — `lea rax, off_22011BF0` is annotated `"jellyfish"` by the symbolizer, and the second `lea rdx, unk_BDF3BD8` loads the parallel length table indexed by the same register. Reading the relocation addend at each of the six slots yields the codenames in enum order:

| `TpuVersion` | Table slot (rel.ro) | `.rodata` target | Codename literal | Length | Confidence |
|---:|---|---|---|---:|---|
| 0 | `0x22011BF0` | `0x863F064` | `jellyfish` | 9 | CERTAIN |
| 1 | `0x22011BF8` | `0x863F392` | `dragonfish` | 10 | CERTAIN |
| 2 | `0x22011C00` | `0x863F1C4` | `pufferfish` | 10 | CERTAIN |
| 3 | `0x22011C08` | `0x863F172` | `viperfish` | 9 | CERTAIN |
| 4 | `0x22011C10` | `0x86864E0` | `ghostlite` | 9 | CERTAIN |
| 5 | `0x22011C18` | `0x863F0CF` | `6acc60406` | 9 | CERTAIN |

The parallel length table at `0xBDF3BD8` stores `{9, 10, 10, 9, 9, 9}` so the codename can be returned as a length-counted `string_view` without a `strlen`. The lengths are confirmed by the external-name builders, which embed the same byte counts as immediates (see below).

> **NOTE —** the table is the *only* place codename strings are reachable by enum index. `dragonfish` has no `xla_df_` flag prefix, no dedicated bundle-restrictions class, and shares Jellyfish's encoder — yet it has its own pointer-table slot and its own codename literal. A reimplementation that derives the codename list from the *consumers* (flag prefixes, encoder families) will under-count; the pointer table is the authoritative roster.

---

## The Three Source-of-Truth Functions

Three functions in `tpu_version.cc` define the enum's external contract. They are independent — different call sites, different output forms — but they agree on the same 6-value space, which is why a reimplementation can treat any one of them as ground truth and check the others against it.

### `TpuVersionToString` — enum → internal codename

Covered above (`0x20b3a480`). Returns the `.rodata` codename via `off_22011BF0[version]`. This is the form used in log lines, debug dumps, and the `gxc::glc` / `gxc::gfc` namespace derivation.

### `AbslUnparseFlag` — enum → flag string

`tpu::AbslUnparseFlag` (`0x20b3ab40`) is the Abseil command-line-flag unparser for a `--tpu_version=` flag. It performs the **same** bounds check (`version >= 6`, fatal at `tpu_version.cc:152`) and indexes the **same** `off_22011BF0` pointer table and the **same** `qword_BDF3BD8` length table, then `memcpy`s the codename into the caller's string buffer and null-terminates it:

```c
void AbslUnparseFlag(string *out, unsigned version) {  // sub_20B3AB40
    if (version >= 6)
        LogMessageFatal(".../tpu_version.cc", 152), log << "Invalid TPU version " << ...;
    const char *name = off_22011BF0[version];           // same table as ToString
    size_t      len  = qword_BDF3BD8[version];           // same length table
    out->size = len; memcpy(out->data, name, len); out->data[len] = 0;
}
```

That two independent functions read the identical compiled table is the strongest internal corroboration that the mapping is `{0:jellyfish … 5:6acc60406}` and not some permutation. The flag round-trips through the same codename strings the runtime logs.

### `TpuVersionFromProto` — wire enum → internal enum

`tpu::TpuVersionFromProto` (`0x20b3a8c0`) converts the on-wire `TpuVersionProto` (1-based) to the internal `TpuVersion` (0-based), returning a `StatusOr<TpuVersion>`. The body is an explicit per-case switch, every arm of which writes `internal = proto − 1` and sets the status-OK flag:

```c
StatusOr<TpuVersion> TpuVersionFromProto(TpuVersionProto proto) {  // sub_20B3A8C0
    switch (proto) {
        case 1: result.value = 0; result.ok = 1; break;   // TPU_VERSION_JELLYFISH  -> kJellyfish
        case 2: result.value = 1; result.ok = 1; break;   // TPU_VERSION_DRAGONFISH -> kDragonfish
        case 3: result.value = 2; result.ok = 1; break;   // TPU_VERSION_PUFFERFISH -> kPufferfish
        case 4: result.value = 3; result.ok = 1; break;   // TPU_VERSION_VIPERFISH  -> kViperfish
        case 5: result.value = 4; result.ok = 1; break;   // TPU_VERSION_GHOSTLITE  -> kGhostlite
        case 6: result.value = 5; result.ok = 1; break;   // TPU_VERSION_6acc60406  -> k6acc60406
        default:                                            // 0 / >6
            result.status = MakeError("Invalid TPU version: " + proto,
                                      ".../tpu_version.cc", 421);
    }
    return result;
}
```

The relationship is uniformly `internal = proto − 1` across all six valid cases. `proto = 0` (`TPU_VERSION_INVALID`) and `proto > 6` fall to the default arm and produce a non-OK `Status` with the message `"Invalid TPU version: <N>"` from line 421. `tpu::TpuVersionFromProtoOrDie` (`0x20b3aa20`) wraps it: it calls `FromProto`, checks the status, and on failure raises `LogMessageFatal` at line 428 with `"Could not read TPU version from protobuf."`. The "OrDie" variant is what most internal call sites use when the proto is known well-formed — for example `ProgramProtoUtil::BundleCount` reads a proto-side version field and feeds it straight through `FromProtoOrDie` before switching on the internal value.

> **CORRECTION (CODE-FORM) —** an earlier reading described `FromProto` as the arithmetic idiom `lea -0x1(%rsi),%eax ; cmp $0x5 ; ja fail ; jmp *table[eax]` — i.e. compute `proto-1` once, range-check, then jump-table on the result. The decompiled body is instead an explicit six-arm `switch(proto)` where each arm independently stores its `proto-1` value. The two are semantically identical (`internal = proto - 1`, default = error) and a compiler may lower either to the other; the byte-accurate source-level form is the per-case switch shown above. Either way the contract — proto N maps to internal N−1, proto 0 and >6 are errors — holds.

---

## The Five-Axis Cross-Walk

`TpuVersion` is the hub of five identity spaces. The table below is the consolidated cross-walk; each column is verified against a distinct binary artifact, so a reimplementation can reconstruct any one axis from the integer and check it against the others.

| `TpuVersion` (int) | Enum tag | Codename (`ToString`) | `TpuVersionProto` (wire) | External name (`ToExternalName`) | HAL family / factory | Confidence |
|---:|---|---|---:|---|---|---|
| 0 | `kJellyfish` | `jellyfish` | 1 (`TPU_VERSION_JELLYFISH`) | `TPU v2` | JXC / `TpuHalJxcHardwareFactory` | HIGH |
| 1 | `kDragonfish` | `dragonfish` | 2 (`TPU_VERSION_DRAGONFISH`) | `TPU v3` | JXC / `TpuHalJxcHardwareFactory` | HIGH |
| 2 | `kPufferfish` | `pufferfish` | 3 (`TPU_VERSION_PUFFERFISH`) | `TPU v4` (`… lite`) | PXC / `TpuHalPxcHardwareFactory` | HIGH |
| 3 | `kViperfish` | `viperfish` | 4 (`TPU_VERSION_VIPERFISH`) | `TPU v5` (`… lite`) | VXC / `TpuHalVxcHardwareFactory` | HIGH |
| 4 | `kGhostlite` | `ghostlite` | 5 (`TPU_VERSION_GHOSTLITE`) | `TPU v6 lite` | VXC / `TpuHalVxcHardwareFactory` | HIGH |
| 5 | `k6acc60406` | `6acc60406` | 6 (`TPU_VERSION_6acc60406`) | `TPU7x` | VXC / `TpuHalVxcHardwareFactory` | HIGH |

> **NOTE — proto enumerator names are codename-based, not marketing-based.** The `TpuVersionProto` enumerators are spelled `TPU_VERSION_<CODENAME>` (`TPU_VERSION_INVALID`=0, then `TPU_VERSION_JELLYFISH`=1 … `TPU_VERSION_6acc60406`=6), confirmed as a contiguous descriptor string block at `0xC1928DB`–`0xC19297D`. There is **no** `TPU_V2`/`TPU_V3`/`TPU_V7X` enumerator anywhere in the binary — the `TPU v2…TPU7x` strings are the separate *external/marketing* axis emitted by `TpuVersionToExternalName` (`0x20b3a500`). Do not conflate the wire enumerator name with the external name: `TPU_VERSION_JELLYFISH` (wire) and `TPU v2` (external) name the same silicon on two different axes.

The axes line up cleanly because every consumer indexes the same integer. Some axis details are worth pinning:

- **Codenames are fish names through `viperfish`, then degrade.** Generations 0-3 carry real fish codenames (`jellyfish`, `dragonfish`, `pufferfish`, `viperfish`). Generation 4 is `ghostlite` — a contraction, not a fish. Generation 5 is `6acc60406` — an obfuscated 9-character tag, the only non-mnemonic codename. The string `ghostfish` appears zero times in the binary; the only canonical internal name for generation 5 is `6acc60406`.

- **The external name carries a `lite` discriminator for generations 2 and 3.** `TpuVersionToExternalName` (`0x20b3a500`) and `TpuVersionAndVariantToHumanReadableName` (`0x20b3b040`) both branch on a variant `string_view`: when the variant is exactly 4 bytes and equals the little-endian dword `1702127980` (= `0x6574696c` = ASCII `"lite"`), `pufferfish` reads `"TPU v4 lite"` and `viperfish` reads `"TPU v5 lite"`. Generations 4 and 5 have fixed external names (`"TPU v6 lite"`, `"TPU7x"`) with no variant branch.

```text
TpuVersionToExternalName(version, variant_sv):       // sub_20B3A500
  0 -> "TPU v2"
  1 -> "TPU v3"
  2 -> (variant=="lite") ? "TPU v4 lite" : "TPU v4"   // lite = dword 0x6574696c
  3 -> (variant=="lite") ? "TPU v5 lite" : "TPU v5"
  4 -> "TPU v6 lite"
  5 -> "TPU7x"
  _ -> "Unknown TPU version"
```

- **The HAL family collapses three codenames onto one factory.** Generations 0-1 share `TpuHalJxcHardwareFactory`; generation 2 has its own `TpuHalPxcHardwareFactory` (constructed with no version argument, since PXC services only Pufferfish); generations 3, 4, and 5 all share `TpuHalVxcHardwareFactory`, differentiated only by the `TpuVersion` value the factory is constructed with. There are exactly three HAL factory classes — `TpuHalJxcHardwareFactory`, `TpuHalPxcHardwareFactory`, `TpuHalVxcHardwareFactory` — confirmed in the symbol table. The mapping of codename to the registering init module (the `google_init_module_tpu_hal_*` translation unit) and the per-version dispatch live in [HAL Families](hal-families.md).

> **GOTCHA —** the HAL *factory class* and the HAL *init module* do not name the same thing. Generations 4 and 5 register through init modules named for `glc` and `gfc` respectively, but both construct the shared `TpuHalVxcHardwareFactory`. A reimplementation that infers "Ghostlite has a GlcFactory" from the `glc` init-module name will invent a class that does not exist. The factory class is VXC; `glc`/`gfc` name the *sub-core ISA family*, not a factory.

---

## Codec Selection Confirms the Ordering

The instruction-selection codec factory is an independent confirmation of the enum-int ordering. `tpu::TpuCodec::Create` (`0x1e835fa0`) is a clean `switch(TpuVersion)` over a `CreateTpuCodec*` per-codename constructor:

```c
StatusOr<TpuCodec*> TpuCodec::Create(TpuVersion version) {   // sub_1E835FA0
    switch (version) {
        case 0: codec = CreateTpuCodecJellyfish();  break;
        case 1: codec = CreateTpuCodecDragonfish(); break;
        case 2: codec = CreateTpuCodecPufferfish(); break;
        case 3: codec = CreateTpuCodecViperfish();  break;
        case 4: codec = CreateTpuCodecGhostlite();  break;   // named
        case 5: codec = sub_1E838380();             break;   // anonymous v5 codec
    }
    result.value = codec; result.ok = 1; return result;
}
```

Cases 0-4 each call a demangled `CreateTpuCodec<Codename>` factory; the names match the pointer-table codenames one-for-one. Case 5 is the tell: there is **no** `CreateTpuCodec6acc60406` symbol. The v5 codec is constructed by an anonymous factory (`sub_1E838380`) that installs a vtable with no named `_ZTV` / `_ZTI` symbol. The named codec for generation 4 is `TpuCodecGhostlite` (130 cross-references; vtable at `0x21d35c00`); the generation 5 codec is reified only through string registrations (`6acc60406BundleRestrictions`, `6acc60406HardwareScanner`, `6acc60406RouteCacheSet`), never as a `TpuCodec6acc60406` C++ class. This asymmetry — Ghostlite fully named, 6acc60406 obfuscated and anonymous — recurs across every axis and is the binary's own signal that generation 5 is the newest, least-exposed silicon in this build.

The bundle-encoder dispatch (`tpu::ProgramProtoUtil::BundleCount`, `0x1e830e80`) confirms the same grouping from a third angle. It reads a proto-side version field, runs it through `TpuVersionFromProtoOrDie`, then switches on the internal value:

```text
BundleCount internal-version switch:                 // sub_1E830E80
  case 0, 1 -> CreateEncoderJfDf   (Jellyfish + Dragonfish share)
  case 2    -> CreateEncoderPf     (Pufferfish)
  case 3    -> CreateEncoderVf     (Viperfish)
  case 4, 5 -> CreateEncoderGlGf   (Ghostlite + 6acc60406 share)
```

The pairing 0+1 (JfDf) and 4+5 (GlGf) is direct binary evidence that Dragonfish reuses Jellyfish's encoder and 6acc60406 reuses Ghostlite's GlGf encoder — exactly the sharing the codename namespaces (`gxc::glc` for v4, `gxc::gfc` for v5) imply.

---

## Per-Codename Feature Matrix

The feature presence below is derived from the per-codename C++ classes, bundle-restriction registrations, and the SparseCore / BarnaCore namespace populations. The architecture arc is the familiar one for an accelerator line: a fused first-generation dataflow engine (BarnaCore on HBM embeddings), a mid-life pivot to a fetch/load core split, and a late addition of a dedicated SparseCore.

| Codename | TensorCore | BarnaCore | SparseCore | Bundle restrictions | Confidence |
|---|:---:|:---:|:---:|---|---|
| jellyfish | yes | yes | no | `JellyfishBundleRestrictions` | HIGH |
| dragonfish | yes | yes | no | shares `JellyfishBundleRestrictions` | HIGH |
| pufferfish | yes | yes | no | `PufferfishBundleRestrictions` | HIGH |
| viperfish | yes | no | yes | `ViperfishBundleRestrictions` | HIGH |
| ghostlite | yes | no | yes | `GhostliteBundleRestrictions` | HIGH |
| 6acc60406 | yes | no | yes | `6acc60406BundleRestrictions` (string-registered) | MEDIUM |

Reading the matrix:

- **TensorCore is universal.** Every generation has a TensorCore; it is the constant.
- **BarnaCore is the early-generation embedding engine.** Present on Jellyfish through Pufferfish (the `platforms_deepsea::jellyfish::barna_core` namespace is populated and a `ComputeThreadCountPerBarnaClump` function takes a `TpuVersion` argument), retired from Viperfish onward.
- **SparseCore arrives with Viperfish.** Viperfish, Ghostlite, and 6acc60406 carry SparseCore support; the rodata string `"FusionDebugger supports Viperfish and later platforms"` corroborates the Viperfish-and-later boundary, and the `xla_sc_` flag prefix family (SparseCore flags) is gated to these three generations.
- **Bundle restrictions are per-codename except for the shared pairs.** Four named codename `*BundleRestrictions` classes exist — `JellyfishBundleRestrictions`, `PufferfishBundleRestrictions`, `ViperfishBundleRestrictions`, `GhostliteBundleRestrictions` (each with its own `_ZTV`/`_ZTI`/`_ZTS` triple) over the `TpuBundleRestrictions` base; Dragonfish shares Jellyfish's. The `6acc60406BundleRestrictions` row is MEDIUM because it is registered by string only — there is no demangled C++ class of that name, consistent with the anonymous-codec pattern for generation 5.

---

## Confidence Accounting

The credibility of this matrix rests on knowing which assignments are pinned by guard code and which rest on declaration order. The distinction matters for a reimplementation: a pinned row can be trusted verbatim; an order-inferred row should be re-derived if the binary version changes.

- **Integers 0 and 5 are HIGH, pinned by guards.** Generation 0 (`kJellyfish`) is the first table slot, the first `CreateTpuCodec` case, and the `xor %esi,%esi` (value-0) Register call — its zero-ness is structurally fixed. Generation 5 (`k6acc60406`) is pinned at the top: the `version >= 6` bounds check in `ToString`/`AbslUnparseFlag` proves the enum has exactly six values and 5 is the maximum; `TpuVersionFromProto` proves `proto 6 → internal 5`; the codec switch and the encoder dispatch both terminate at case 5. There is no seventh value and no gap.

- **Integers 1-4 are HIGH on the codename map, but their *relative* order rests on the pointer table's slot order, which equals proto declaration order.** The pointer table at `0x22011BF0` is the authority, and it is materialized at link time in enum order, so the codename-to-integer binding for 1-4 is as solid as for 0 and 5. What is *inferred* (rather than guard-pinned) is that this order is also the chronological silicon order — `dragonfish` is newer than `jellyfish`, `viperfish` newer than `pufferfish`, and so on. That chronology is consistent with every axis (external names `v2 < v3 < v4 < v5 < v6 < v7x`, proto values, the GetTpuType superpod ordering) but is an inference from declaration order, not a single guard.

- **The external "TPU vN" names are HIGH, directly switch-pinned.** `ToExternalName` and `ToHumanReadableName` are explicit switches with literal string arms; there is no ambiguity in the `v4=TPU v4`, `v6 lite`, `TPU7x` bindings.

- **The 6acc60406 → public-marketing-name binding is OUT OF SCOPE for the binary.** The strings `Trillium`, `Ironwood`, and `Ghostfish` occur zero times in `libtpu.so`. The library names generation 5 only as `6acc60406` (internal), `TPU7x` (external display), and `tpu7x`/`tpu7` (the Cloud `accelerator_type` strings the parser accepts). Any mapping of `6acc60406` to a public product codename is an external fact layered onto the binary, not sourced from it; the wiki marks it LOW and documents it in [Marketing / Cloud Naming](marketing-cloud-naming.md).

> **QUIRK —** the obfuscated tag `6acc60406` is the literal codename, not a placeholder a tool failed to resolve. It is the relocation target of pointer-table slot 5, the `ToString` return value for generation 5, and the prefix of the embedded `6acc60406_chip_parts.binarypb` resource. Whoever built this generation deliberately stripped the mnemonic codename and shipped a 9-character hex-looking tag in its place — every other codename is a word, this one alone is a token. A reimplementation should treat `6acc60406` as the canonical name and not "fix" it to a fish.

---

## Cross-References

- [Dual Enum (Proto vs Internal)](dual-enum-proto-vs-internal.md) — the `internal = proto − 1` off-by-one in full, with the complete wire-value table and the FromProto/ToProto bodies
- [Part IV Overview](overview.md) — how `TpuVersion` threads through HAL routing, chip constants, and ISA selection
- [HAL Families](hal-families.md) — the JXC / PXC / VXC factory routing and the per-codename init modules
- [Sub-Core Taxonomy](sub-core-taxonomy.md) — the fetch/load core split (`pfc`/`plc`, `vfc`/`vlc`) and the `gxc::glc` / `gxc::gfc` ISA sub-families
- [Per-Codename HW Constants](per-codename-hw-constants.md) — chip constants gated by `TpuVersion`
- [PCI Device IDs](pci-device-ids.md) — the DeviceIdentifiers records that map silicon to `TpuVersion` at discovery time
- [Marketing / Cloud Naming](marketing-cloud-naming.md) — external `accelerator_type` strings and why `Trillium`/`Ironwood` are not in the binary
- [ISA Overview](../isa/overview.md) — the codec and bundle-encoder families that `TpuVersion` selects
