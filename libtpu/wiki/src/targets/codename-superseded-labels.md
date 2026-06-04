# Naming Pitfalls and Authoritative Labels

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 B). Other versions differ.*

## Abstract

The TPU codename model has several easily-confused labels, and a handful of plausible-but-wrong names that the binary contradicts. This page is the standing pitfall list: each tempting-but-wrong label is named, the byte evidence that overrules it is cited, and the authoritative value is given. If a downstream page says "Ghostlite = v5p", "Trillium = 6acc60406", "2 HAL families", or treats "Ghostfish" as a confirmed codename, it is wrong by the evidence here.

The pitfalls fall into three buckets: **count traps** (the `TpuVersion` and HAL-family counts are easy to undercount), **off-by-one naming traps** (Cloud names slide one generation too high, mislabeling Ghostlite as `v5p` and 6acc60406 as `v6e`/Trillium), and **invented codenames** (the "Ghostfish" gloss that has zero occurrences in the binary). Every authoritative value below is byte-anchored.

The reimplementation payoff is direct: a clone that adopts any wrong label here routes silicon to the wrong factory, codec, or external name. Trusting "Ghostlite = v5p" hands Ghostlite's `TpuVersion` 4 the display string and Cloud type that belong to Viperfish (`TpuVersion` 3); trusting "2 HAL families" forks the VXC class a generation early; trusting "Ghostfish" or "`TpuCodec6acc60406`" invents a symbol the binary never declares. Each row below names the binary fact that keeps the integer dispatch, the factory key, and the emitted name consistent across generations.

| | |
|---|---|
| **Authoritative `TpuVersion` count** | 6 (enum `0..5`), bounded in `TpuVersionToString` @ `0x20b3a480` (`a1 >= 6` → fatal) |
| **Authoritative HAL family count** | 3 — JXC, PXC, VXC factory classes |
| **Codename source-of-truth** | `TpuVersionToString` rel.ro table @ `0x22011bf0` (6 relocations) |
| **Marketing names in binary** | none (`Trillium`/`Ironwood`/`Ghostfish` = 0 occurrences) |

---

## Count Facts

> **Trap — "5 TpuVersions" undercounts; there are 6.** The binary defines **six** generations. `TpuVersionToString` (`0x20b3a480`) bounds-checks `a1 >= 6` before indexing a six-entry pointer table, and the `.data.rel.ro` table at `0x22011bf0` has exactly six `R_X86_64_RELATIVE` relocations naming `jellyfish` (`0x863f064`), `dragonfish` (`0x863f392`), `pufferfish` (`0x863f1c4`), `viperfish` (`0x863f172`), `ghostlite` (`0x86864e0`), `6acc60406` (`0x863f0cf`). The most-often-missed value is `viperfish` (v3): it is easily folded into a neighbor. `TpuVersionFromProto` (`0x20b3a8c0`) likewise has cases `1..6` mapping to internal `0..5`.

> **Trap — "2 HAL families" undercounts; there are 3.** There are **three** distinct factory classes, confirmed by three separate typeinfo records: `tpu::(anon)::TpuHalJxcHardwareFactory`, `tpu::(anon)::TpuHalPxcHardwareFactory`, and `tpu::TpuHalVxcHardwareFactory`. The six generations route across them as JXC (Jellyfish, Dragonfish), PXC (Pufferfish), and VXC (Viperfish, Ghostlite, 6acc60406). The two newest generations share the **VXC** factory: the `glc` init module's CHECK string at `0x94a4a6f` reads `make_unique<TpuHalVxcHardwareFactory>(kGhostlite)`, and the `gfc` init module's at `0x94a3ef5` reads `make_unique<TpuHalVxcHardwareFactory>(k6acc60406)`. So `glc`/`gfc` are VXC-family sub-cores, not a fourth factory. See [HAL Families](hal-families.md).

---

## Naming Facts

The `glc` and `gfc` encoder **addresses** and **namespaces** are unambiguous; the trap is sliding the Cloud/marketing names one generation too high onto them.

> **Trap — "Ghostlite = v5p" is one generation too high; Ghostlite = v6e / "TPU v6 lite".** The `glc` encoder at `0x1f250160` is `gxc::glc::isa::TensorCoreVectorAlu0Encoder::Encode`. The namespace `Ghostlite`/`glc` is correct; `v5p` is not — `v5p` is the Cloud standard name for **Viperfish (v3)**, whose string `"TPU v5p"` lives at `0x85c9e34`, one generation below. The correct binding is `glc` = `TpuVersion 4` = `ghostlite` = `"TPU v6 lite"` (case 4 of `TpuVersionToExternalName` @ `0x20b3a500`) = Cloud `v6e` = Trillium (external). See [Marketing / Cloud Naming](marketing-cloud-naming.md).

> **Trap — "6acc60406 = Trillium / v6e" is one generation too low; 6acc60406 = TPU7x.** The `gfc` encoder at `0x1f8b53c0` carries the namespace `gfc` and tag `6acc60406` — both correct — but `Trillium`/`v6e` belongs to Ghostlite, one generation below. The correct binding is `gfc` = `TpuVersion 5` = `6acc60406` = `"TPU7x"` (case 5 of `TpuVersionToExternalName`) = Cloud `tpu7x`. The bit-level VALU encoding for these two encoders is unaffected; only the version/marketing labels move.

> **Trap — "Ghostfish" is not a binary codename.** The gloss "Ghostfish" is a tempting analogy to `ghostlite`→`glc`, but a case-insensitive scan of the full image returns **zero** matches for `ghostfish`/`Ghostfish`, and there is no `TpuCodec6acc60406` C++ class symbol (only a source-path string `tpu_codec_6acc60406.cc`; the v5 codec built by `sub_1E838380` is anonymous). The only canonical internal name for this generation is the obfuscated tag `6acc60406`.

---

## Codec / Class-Topology Facts

> **Note:** The v5 codec is anonymous, not a class named `tpu::TpuCodec6acc60406`. `TpuCodec::Create` (`0x1e835fa0`) case 5 calls `sub_1E838380`, which constructs an object whose vtable has **no demangled symbol** — the class is anonymous. By contrast case 4 calls the **named** `tpu::CreateTpuCodecGhostlite`, and `TpuCodecGhostlite` is a real class with `DecodeBundle`/`EncodeBundle` methods. The v5 codec is functional but un-reified; there is no `TpuCodec6acc60406` class.

The codec dispatch, verbatim from the decompile:

```c
TpuCodec* TpuCodec::Create(TpuVersion v):     // 0x1e835fa0
    case 0: return CreateTpuCodecJellyfish()
    case 1: return CreateTpuCodecDragonfish()
    case 2: return CreateTpuCodecPufferfish()
    case 3: return CreateTpuCodecViperfish()
    case 4: return CreateTpuCodecGhostlite()    // NAMED class
    case 5: return sub_1E838380()               // anonymous v5 codec
```

> **Note:** `xla::jellyfish::GhostliteBundleRestrictions` is a named class (typeinfo + vtable + `AddMxuRequirements`/`AddXluRequirements` methods present), whereas the v5 generation registers `6acc60406BundleRestrictions` by string only with no canonical C++ class. The naming asymmetry between the two newest generations — Ghostlite named, 6acc60406 obfuscated — is a deliberate pattern.

---

## Device-Type and v5 Chip-DID Facts

> **Note:** The Ghostlite→device-type `13` (`0xd`) and 6acc60406→device-type `12` (`0xc`) binding is a direct store, not an inference. `DeviceTypeFromDeviceIdentifiers` (`0xf6993a0`) writes the constants directly: `*(_DWORD*)(result+8) = 13` on the `IsGlc` branch and `= 12` on the `IsGfc` branch. The full device-type map (Jellyfish 3, Dragonfish 5, Pufferfish 7, Puffylite 8, Viperfish 10, Viperlite 11, 6acc60406 12, Ghostlite 13) is a single-function fact. See [PCI Device IDs](pci-device-ids.md).

> **Note:** The 6acc60406 PCI device ID is a real `.rodata` record, just an anonymous one. It sits at `.rodata` `0xbdf3cc4`–`0xbdf3ce8` as three **anonymous** 12-byte records (PF hdr `0x0075`, VF hdr `0x0076`, Mgt PF hdr `0x0077`; all chip DID `0x00f2`, rev-mask `0xff`), terminated by the `s_44716` tag. The chip DID `0x00f2` is independently confirmed in the `IsGfc` immediates (`0xF21AE000751AE0`, `0xF21AE000761AE0`).

---

## The Dual-Enum chip_parts Trap

> **Note:** The embedded `6acc60406_chip_parts.binarypb` blobs (`0xbdf29a0`, `0xbdf2ba0`) begin `08 06 12 97 01` — protobuf field 1 (varint) = **6**. That `6` is the **TpuVersionProto** value (`TPU_VERSION_6acc60406`), a 1-based enum, not the internal `TpuVersion`. `TpuVersionFromProto` (`0x20b3a8c0`) maps it by `internal = proto − 1`, so proto `6` → internal `TpuVersion 5`. "chip_parts version 6" and "internal TpuVersion 5" name the **same** 6acc60406 / TPU7x silicon on two parallel enums; there is no conflict. The codec platform-tag guards confirm it independently (the v4/Ghostlite codec checks proto tag 5, the v5 codec checks proto tag 6). See [Dual Enum](dual-enum-proto-vs-internal.md).

---

## Naming Pitfall Quick Table

| Tempting-but-wrong label | Why it tempts | Authoritative value | Evidence |
|---|---|---|---|
| "5 TpuVersions" | viperfish folds into a neighbor | 6 (`0..5`) | `TpuVersionToString` bound `>= 6`; 6 relocs @ `0x22011bf0` |
| "2 HAL families" | VXC absorbs three codenames | 3 (JXC / PXC / VXC) | three factory typeinfos |
| "Ghostlite = v5p" | off-by-one Cloud name | `v6e` / "TPU v6 lite" | `TpuVersionToExternalName` case 4 |
| "6acc60406 = Trillium / v6e" | off-by-one marketing name | "TPU7x" / `tpu7x` | `TpuVersionToExternalName` case 5 |
| "v5 codec = TpuCodec6acc60406" | analogy to named codecs | anonymous codec (`sub_1E838380`) | no class symbol; only `tpu_codec_6acc60406.cc` path |
| "Ghostfish" (gfc codename) | analogy to `ghostlite`→`glc` | `6acc60406` (obfuscated tag) | `ghostfish` = 0 occurrences |
| "chip_parts internal version 6" | proto/internal conflation | proto 6 = internal 5 | `08 06` blob byte; `TpuVersionFromProto` `−1` |
| "v5 PCI DID has no record" | anonymous (no symbol) | chip DID `0x00f2` (anon records) | bytes `0xbdf3cc4`+; `IsGfc` immediates |

---

## Cross-References

- [Codename Matrix](tpu-version-codename-matrix.md) — the authoritative `TpuVersion` ↔ codename table these corrections defend
- [Marketing / Cloud Naming](marketing-cloud-naming.md) — the `v5p`-vs-`v6e` cross-walk and the external-only marketing names
- [PCI Device IDs](pci-device-ids.md) — the byte-pinned device-type and v5 chip-DID facts (PIN-01 / PIN-02)
- [HAL Families](hal-families.md) — the three-factory routing (CNT-02)
