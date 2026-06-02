# Marketing / Cloud Naming

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 B). Other versions differ.*

## Abstract

libtpu carries three parallel naming spaces for the same silicon and a fourth that lives only in external Cloud-TPU documentation. The internal **codename** (`jellyfish`…`6acc60406`) is the binary's primary key; the **external display name** (`"TPU v2"`…`"TPU7x"`) is what `TpuVersionToExternalName` emits; the **Cloud-TPU accelerator-type** string (`v2`, `v3`, `v5e`, `v5p`, `v6e`, `tpu7x`) is what the user-facing `accelerator_type` API keys on and what the `AcceleratorTypeToTpuVersionEnum` parser consumes. The fourth space — public marketing names like **Trillium** and **Ironwood** — is **not embedded anywhere in the binary**.

This page is the codename ↔ marketing/Cloud cross-walk. The forward direction (codename / `TpuVersion` → external display name) is a verbatim `switch` in `TpuVersionToExternalName` and is CERTAIN. The Cloud-API string set is recovered from the embedded accelerator-type cluster and the parser. The marketing-codename bindings (Trillium = v6e, and the unpublished name for 6acc60406) are **external facts layered onto the binary**, not sourced from it; every such row is flagged.

For reimplementation, the contract is:

- The `TpuVersion` → external-display-name `switch` and its `lite`-variant logic.
- The Cloud-TPU accelerator-type string vocabulary and where it is parsed.
- Which names are binary-internal (verifiable) versus external-only (inferred).

| | |
|---|---|
| **Forward map** | `tpu::TpuVersionToExternalName` @ `0x20b3a500` |
| **Cloud-API parser** | `libtpu::(anon)::AcceleratorTypeToTpuVersionEnum` @ `0x204cf620` |
| **Generation gate** | `libtpu::IsAtLeastTPU7x` @ `0x204cfda0` (resolves type, tests `>= 8`) |
| **Codename source-of-truth** | `TpuVersionToString` rel.ro table @ `0x22011BF0` |
| **Marketing names in binary** | none — `Trillium`=0, `Ironwood`=0, `Ghostfish`=0 occurrences |

---

## The Naming Cross-Walk

The complete cross-walk, one row per generation. The codename and external-display columns are byte-verified; the Cloud-API column is from the embedded accelerator-type strings; the marketing column is external-only where marked.

| TpuVersion | Codename (internal) | External display | Cloud-TPU API | Marketing | Confidence |
|---|---|---|---|---|---|
| 0 | `jellyfish` | `"TPU v2"` | `v2` | TPU v2 | CERTAIN (display); marketing trivial |
| 1 | `dragonfish` | `"TPU v3"` | `v3` | TPU v3 | CERTAIN (display) |
| 2 | `pufferfish` | `"TPU v4"` / `"TPU v4 lite"` | `v4` / `v4lite` | TPU v4 | CERTAIN (display) |
| 3 | `viperfish` | `"TPU v5"` / `"TPU v5 lite"` | `v5p` / `v5e` | TPU v5p / v5e | CERTAIN (display); Cloud HIGH |
| 4 | `ghostlite` | `"TPU v6 lite"` | `v6e` | **Trillium** | CERTAIN (display); marketing LOW (external) |
| 5 | `6acc60406` | `"TPU7x"` | `tpu7x` / `tpu7` | (unpublished) | CERTAIN (display); marketing UNKNOWN |

> **GOTCHA —** the Cloud name `v5p` belongs to **Viperfish (v3)**, not to Ghostlite. The standard Viperfish Cloud name is `v5p` (string `"TPU v5p"` @ `0x85c9e34`) and its lite/efficiency variant is `v5e`; Ghostlite is `v6e`. Sliding the `v5p` label up one generation to Ghostlite is the single most common naming error and is corrected in [Superseded-Label Correction List](codename-superseded-labels.md). Ghostlite = `v6e` = Trillium; the prior generation's `v5p` is unrelated.

---

## Forward Map — `TpuVersionToExternalName`

### Purpose

`TpuVersionToExternalName` is the authoritative `TpuVersion` → user-facing display-string function. It takes the enum value plus an optional variant string view, and returns a static C string. It is the function whose output a user sees in tooling and error messages.

### Algorithm

```c
const char* TpuVersionToExternalName(int v, view variant):   // 0x20b3a500
    switch (v):
      case 0: return "TPU v2"
      case 1: return "TPU v3"
      case 2: return (variant.len==4 && variant=="lite") ? "TPU v4 lite" : "TPU v4"
      case 3: return (variant.len==4 && variant=="lite") ? "TPU v5 lite" : "TPU v5"
      case 4: return "TPU v6 lite"               // Ghostlite — no plain "TPU v6"
      case 5: return "TPU7x"                      // 6acc60406
      default: return "Unknown TPU version"
```

The `lite` test compares the first four bytes of the variant view against `0x6574696C` (the little-endian dword for the ASCII string `"lite"`) with length 4. Only Pufferfish (v4) and Viperfish (v5) carry the optional `lite` suffix this way; Ghostlite already names itself `"TPU v6 lite"` unconditionally and 6acc60406 is always `"TPU7x"`.

> **QUIRK —** there is no plain `"TPU v6"` string for any generation. Ghostlite is the v6-class part and it is named `"TPU v6 lite"` directly in case 4 — the `lite` qualifier is baked into the name, not appended by the variant branch. A reimplementation that expects a `"TPU v6"` base name and a separate `lite` suffix (the v4/v5 pattern) will produce a name the binary never emits.

### Evidence

The display strings, read directly from the binary:

```text
0x868655d  "TPU v6 lite"   (case 4 — Ghostlite)
0x84c7976  "TPU7x"         (case 5 — 6acc60406)
0x85c9e34  "TPU v5p"       (Viperfish Cloud standard name)
0x998f2b3  "TPU v5"        (case 3 base)
0x9c163f0  "TPU v2"        (case 0)

external display-name cluster @0x8686540:
  ...|TPU v6 lite|TPU v5 lite|TPU v4 lite|...
v7 cluster @0x84c7968:
  TPU v7x|tpu7x|TPU7x|pwr6x|...
```

---

## Cloud-TPU Accelerator-Type Parser

### Purpose

`AcceleratorTypeToTpuVersionEnum` is the reverse direction: it takes a user-supplied accelerator-type string (the Cloud-TPU `accelerator_type`, e.g. `v6e`, `v5p`, `tpu7x`) and resolves it to a `(TpuVersion, variant)` pair. `IsAtLeastTPU7x` wraps it to gate TPU7x-and-later code paths.

### Algorithm

```c
void AcceleratorTypeToTpuVersionEnum(out, string accel_type):   // 0x204cf620
    if accel_type.empty():
        out = Error("Accelerator type is empty.")    // libtpu_init_utils.cc:33
        return
    parts = split(accel_type, '-')                   // split on 0x2d '-'
    // length-keyed comparison of the leading token against the
    // accepted vocabulary (v2/v3/v4/v4lite/v5/v5e/v5p/v6e/tpu7x/...)
    // resolving each to its (TpuVersion, Variant) pair
    ...

bool IsAtLeastTPU7x(string accel_type):              // 0x204cfda0
    v = AcceleratorTypeToTpuVersionEnum(accel_type)
    return v.ok() && v.tpu_type >= 8                  // public TpuType axis
```

The accelerator-type string is split on `-` and the leading token matched against the accepted vocabulary. `IsAtLeastTPU7x` tests the resolved value `>= 8` on the **public TpuType axis** (where Ghostlite=8 and tpu7x=10), so it returns true for v6e and tpu7x — i.e. "this is a v6-class-or-newer part".

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuVersionToExternalName` | `0x20b3a500` | `TpuVersion` → display string | CERTAIN |
| `AcceleratorTypeToTpuVersionEnum` | `0x204cf620` | Cloud accelerator-type string → `(TpuVersion, variant)` | HIGH |
| `IsAtLeastTPU7x` | `0x204cfda0` | Resolve string, test public TpuType `>= 8` | CERTAIN |
| `TpuVersionAndVariantToHumanReadableName` | `0x20b3b040` | `(TpuVersion, variant)` → human-readable name | HIGH |

> **NOTE —** the parser uses length-keyed hashing over the split tokens; the exact accepted-string set and each string's `(TpuVersion, variant)` resolution were not individually enumerated from the hash map. The forward `TpuVersionToExternalName` switch (CERTAIN above) and the embedded `v5e`/`v5p`/`v6e`/`tpu7x` string cluster pin the vocabulary; the reverse map's full contents are HIGH-confidence inference, not byte-traced per entry.

---

## Marketing Names Are External-Only

The public marketing codenames are **not in the binary**. A case-insensitive scan of the entire 745 MB image returns zero matches for each:

| Marketing token | Occurrences in libtpu.so | Status |
|---|---|---|
| `Trillium` | 0 | External Cloud-TPU name for v6e (Ghostlite) — LOW confidence binding, inferred |
| `Ironwood` | 0 | Not present; no binary evidence it maps to any codename here |
| `Ghostfish` | 0 | Plausible expansion of `gfc` by analogy to `ghostlite`→`glc`; NOT a binary fact |

> **CORRECTION (MKT-01) —** the only canonical internal name for the v5 / gfc generation is the obfuscated tag `6acc60406`. The gloss "Ghostfish" — proposed by analogy (`ghostlite`→`glc`, so `ghostfish`→`gfc`) — appears nowhere in the binary (`ghostfish`/`Ghostfish` = 0 occurrences) and must not be presented as a confirmed codename. The marketing name behind 6acc60406 is not embedded and cannot be sourced from libtpu.so.

> **QUIRK —** "Trillium" is a documented external name for the v6e generation, but a reimplementer reading only the binary will never see it. Bind Ghostlite to `"TPU v6 lite"` / `v6e` (both in the image) and treat `Trillium` as an annotation, not a lookup key. The same applies to any public name for 6acc60406: the binary knows it only as `6acc60406` / `"TPU7x"` / `tpu7x`.

---

## Cross-References

- [Codename Matrix](tpu-version-codename-matrix.md) — the `TpuVersion` enum ↔ codename source-of-truth table
- [PCI Device IDs](pci-device-ids.md) — the chip-DID → codename mapping that anchors each generation
- [Superseded-Label Correction List](codename-superseded-labels.md) — the `v5p`-vs-`v6e` and Ghostfish corrections in detail
- [HAL Families](hal-families.md) — the JXC/PXC/VXC factory routing per codename
