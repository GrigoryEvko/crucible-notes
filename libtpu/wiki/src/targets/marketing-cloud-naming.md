# Marketing / Cloud Naming

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 B). Other versions differ.*

## Abstract

libtpu carries three parallel naming spaces for the same silicon and a fourth that lives only in external Cloud-TPU documentation. The internal **codename** (`jellyfish`…`6acc60406`) is the binary's primary key; the **external display name** (`"TPU v2"`…`"TPU7x"`) is what `TpuVersionToExternalName` emits; the **Cloud-TPU accelerator-type** string (`v2`, `v3`, `v5e`, `v5p`, `v6e`, `tpu7x`) is what the user-facing `accelerator_type` API keys on and what the `AcceleratorTypeToTpuVersionEnum` parser consumes. The fourth space — public marketing names like **Trillium** and **Ironwood** — is **not embedded anywhere in the binary**.

This page is the codename ↔ marketing/Cloud cross-walk and is the **authority** for the marketing / cloud-accelerator-type naming axis: where a sibling Part IV page disagrees, the binary-grounded table below governs. The forward direction (codename / `TpuVersion` → external display name) is a verbatim `switch` in `TpuVersionToExternalName` and is CERTAIN. The Cloud-API string set is **byte-traced in full** from the `AcceleratorTypeToTpuVersionEnum` comparison chain — every accepted literal and its resulting integer is recovered from the decompile, not inferred. The marketing-codename bindings (Trillium = `v6e`/Ghostlite, Ironwood = `6acc60406`) are **external facts layered onto the binary**, not sourced from it; every such row is flagged. The reimplementation payoff is concrete: a clone of libtpu's front door must accept exactly this `accelerator_type` vocabulary, write exactly the public-TpuType integer the parser writes, and emit exactly the display strings the switch returns — get any of the three axes wrong and the plugin either rejects a valid Cloud type or mis-gates a generation.

Three integer/string axes must be kept strictly apart, because the parser does **not** emit the same integer the display switch consumes:

- **Internal `TpuVersion`** — 0-based, the index into the `TpuVersionToExternalName` / `TpuVersionToString` switches: jellyfish=0, dragonfish=1, pufferfish=2, viperfish=3, ghostlite=4, 6acc60406=5.
- **External display name** — the `const char*` that switch returns (`"TPU v2"`…`"TPU7x"`).
- **Public TpuType** — the 1-based integer `AcceleratorTypeToTpuVersionEnum` writes from a Cloud accelerator-type string (`v2`→1, `v3`→2, `v4`→3, `v4lite`→4, `v5e`/`v5lite`→5, `v5p`→6, `v6e`/`v6ea`→7, `tpu7x`/`tpu7`→8). This is the axis `IsAtLeastTPU7x` tests, **not** `TpuVersion`.

For reimplementation, the contract is:

- The `TpuVersion` → external-display-name `switch` and its `lite`-variant logic.
- The Cloud-TPU accelerator-type string vocabulary and where it is parsed.
- Which names are binary-internal (verifiable) versus external-only (inferred).

| | |
|---|---|
| **Forward map** (`TpuVersion`→display) | `tpu::TpuVersionToExternalName` @ `0x20b3a500` |
| **Cloud-API parser** (string→`TpuType`) | `libtpu::(anon)::AcceleratorTypeToTpuVersionEnum` @ `0x204cf620` |
| **Generation gate** | `libtpu::IsAtLeastTPU7x` @ `0x204cfda0` (resolves type, tests `TpuType >= 8`) |
| **Human-readable form** | `tpu::TpuVersionAndVariantToHumanReadableName` @ `0x20b3b040` (same arms as forward map) |
| **Codename source-of-truth** | `TpuVersionToString` @ `0x20b3a480`, rel.ro table @ `0x22011bf0` (indices 0..5) |
| **Marketing names in binary** | none — `Trillium`=0, `Ironwood`=0, `Ghostfish`=0 occurrences |

---

## The Naming Cross-Walk

The complete cross-walk, one row per generation. The four axes are kept separate: the 0-based `TpuVersion` is the display-switch index; the external-display column is the `const char*` that switch returns; the **Public TpuType** column is the integer the Cloud-string parser emits (a *different* axis); the Cloud-API column lists the exact literals the parser accepts. The `TpuVersion`, display, TpuType, and Cloud-API columns are all **byte-traced** from the two switch/compare bodies; only the marketing column is external-only where marked.

| TpuVersion | Codename (internal) | External display | Public TpuType | Cloud-TPU API (parser literals) | Marketing | Confidence |
|---|---|---|---|---|---|---|
| 0 | `jellyfish` | `"TPU v2"` | 1 | `v2` | TPU v2 | CERTAIN |
| 1 | `dragonfish` | `"TPU v3"` | 2 | `v3` | TPU v3 | CERTAIN |
| 2 | `pufferfish` | `"TPU v4"` / `"TPU v4 lite"` | 3 / 4 | `v4` / `v4lite` | TPU v4 | CERTAIN |
| 3 | `viperfish` | `"TPU v5"` / `"TPU v5 lite"` | 6 / 5 | `v5p` (→6) · `v5e`,`v5lite` (→5) | TPU v5p / v5e | CERTAIN |
| 4 | `ghostlite` | `"TPU v6 lite"` | 7 | `v6e`, `v6ea` | **Trillium** | CERTAIN (display+Cloud); marketing LOW (external) |
| 5 | `6acc60406` | `"TPU7x"` | 8 | `tpu7x`, `tpu7` | **Ironwood** (external) | CERTAIN (display+Cloud); marketing LOW (external, 0 binary occ.) |

> **GOTCHA —** the Cloud name `v5p` belongs to **Viperfish (`TpuVersion` 3)**, and so does `v5e`: the parser maps **both** `v5p`→TpuType 6 and `v5e`/`v5lite`→TpuType 5, and both share Viperfish's display name `"TPU v5"` / `"TPU v5 lite"` (string `"TPU v5p"` @ `0x85c9e34`). The next generation, **Ghostlite (`TpuVersion` 4)**, is `v6e`/`v6ea`→TpuType 7. Sliding `v5p` up to Ghostlite — or sliding `v6e` down to Viperfish — is the single most common naming error; see [Superseded-Label Correction List](codename-superseded-labels.md). Canonically: **Viperfish = v5p AND v5e; Ghostlite = v6e (= marketing "Trillium"); 6acc60406 = tpu7x.** No binary string ties `6acc60406` to `v6e` or to "Trillium".

> **CORRECTION (MKT-02) — resolving the Viperfish/Ghostlite Cloud-name conflict.** Two sibling Part-IV families disagree on this axis, and this page (being the authority) governs. `per-codename-hw-constants.md` labels **Viperfish = v5p/v5e** and **Ghostlite = v6e** — *correct*, and consistent with the binding chain below. Some SparseCore pages instead label **Viperfish = v5e** and **Ghostlite = v5p** (a one-generation slide) — *wrong*. The binary settles it without ambiguity: the `0x22011bf0` slot table fixes `viperfish`=`TpuVersion` 3 and `ghostlite`=4; `TpuVersionToExternalName` (`0x20b3a500`) gives 3→`"TPU v5"` and 4→`"TPU v6 lite"`; and the parser (`0x204cf620`) accepts `v5p`/`v5e`/`v5lite` into the v5 (TpuType 5/6) band and `v6e`/`v6ea` into the v6 band (TpuType 7). `v5p` is therefore a *Viperfish* Cloud name and `v6e` is a *Ghostlite* Cloud name; neither belongs to the other. (The marketing name "Trillium" attaches to Ghostlite/`v6e`, not to `6acc60406` — see [Marketing Names Are External-Only](#marketing-names-are-external-only).)

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

external display-name cluster @0x8686541:
  ...|TPU v6 lite|TPU v5 lite|TPU v4 lite|...
v7 cluster @0x84c7968:
  TPU v7x|tpu7x|TPU7x|pwr6x|...
```

The codename ↔ `TpuVersion` binding is not an inference: `TpuVersionToString` (`0x20b3a480`) indexes the `.data.rel.ro` pointer table at `0x22011bf0` (after a `a1 >= 6` fatal-bounds check, proving exactly six entries), and every slot is materialized at link time by an `R_X86_64_RELATIVE` relocation whose addend points at the codename string:

```text
0x22011bf0 + 8*v  ->  codename string (RELA addend)
  [0]  0x863f064  "jellyfish"
  [1]  0x863f392  "dragonfish"
  [2]  0x863f1c4  "pufferfish"
  [3]  0x863f172  "viperfish"     <- TPU v5  family (v5p / v5e / v5lite)
  [4]  0x86864e0  "ghostlite"     <- TPU v6 lite (v6e / v6ea)
  [5]  0x863f0cf  "6acc60406"     <- TPU7x      (tpu7x / tpu7)
```

---

## Cloud-TPU Accelerator-Type Parser

### Purpose

`AcceleratorTypeToTpuVersionEnum` is the reverse direction: it takes a user-supplied accelerator-type string (the Cloud-TPU `accelerator_type`, e.g. `v6e`, `v5p`, `tpu7x`) and resolves it to a **public TpuType** integer (1-based, 1..8 — *not* the internal `TpuVersion`). `IsAtLeastTPU7x` wraps it to gate TPU7x-and-later code paths.

> **QUIRK —** the parser writes the **public TpuType** axis (`v2`→1 … `tpu7x`→8), which is *not* the internal `TpuVersion` (`jellyfish`=0 … `6acc60406`=5) the display switch consumes. The two axes differ in both base (1- vs 0-based) and granularity: a single `TpuVersion` (e.g. Viperfish=3) fans out to *two* TpuTypes (`v5p`→6 and `v5e`/`v5lite`→5). Never feed a TpuType into `TpuVersionToExternalName`, and never assume the parser's integer indexes the codename table at `0x22011bf0`.

### Algorithm

```c
void AcceleratorTypeToTpuVersionEnum(out, string accel_type):   // 0x204cf620
    if accel_type.empty():
        out = Error("Accelerator type is empty.")    // libtpu_init_utils.cc:33
        return
    parts = split(accel_type, '-')                   // split on 0x2d '-'
    if parts.size() != 2:                            // must be '<ver>-<core_count>'
        out = Error("Accelerator type is not in the format ...")  // :37
        return
    tok = AsciiStrToLower(parts[0])                  // leading token, lowercased
    // Fast path: a length-keyed dword/word compare on the inlined buffer.
    if tok.len == 6 && tok == "v4lite":  return ok(4)    // 0x696C3476 + 0x6574
    if tok.len == 2:                                     // 2-byte word compares
        if tok == "v2":  return ok(1)                // *(WORD) == 0x3276
        if tok == "v3":  return ok(2)                //           0x3376
        if tok == "v4":  return ok(3)                //           0x3476
    // Fallthrough (any length, incl. unmatched len-2/len-6): string compares.
    if tok.starts_with("v5lite") || tok == "v5e":  return ok(5)
    if tok == "v5p":                    return ok(6)
    if tok == "v6e" || tok == "v6ea":   return ok(7)
    if tok == "tpu7x" || tok == "tpu7": return ok(8)
    out = Error("Unsupported accelerator type: " + accel_type)  // :67
    // ok(n): out.tpu_type = n; out.ok = 1; return

bool IsAtLeastTPU7x(string accel_type):              // 0x204cfda0
    v = AcceleratorTypeToTpuVersionEnum(accel_type)
    return v.ok() && v.tpu_type >= 8                  // public TpuType axis; only tpu7x/tpu7 qualify
```

The accelerator-type string is split on `-` and the leading token matched against the accepted vocabulary. `IsAtLeastTPU7x` tests the resolved value `>= 8` on the **public TpuType axis**, and on that axis only `tpu7x`/`tpu7` resolve to 8 (Ghostlite's `v6e`/`v6ea` resolve to 7). So the gate is true **only for 6acc60406** — it gates TPU7x-and-later code paths exactly as its name says, and returns *false* for v6e/Ghostlite.

> **GOTCHA —** the name `IsAtLeastTPU7x` is literal, not "v6-class-or-newer". Because the parser maps `v6e`→7 and `tpu7x`→8, the `tpu_type >= 8` test passes for `tpu7x`/`tpu7` (6acc60406) and *fails* for `v6e`/`v6ea` (Ghostlite). A reimplementer who reads the wrapper as "v6e and up" will mis-gate Ghostlite into the TPU7x path; the threshold sits one generation higher than the function's `7x` suffix might suggest at a glance.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TpuVersionToExternalName` | `0x20b3a500` | `TpuVersion` → display string (explicit switch) | CERTAIN |
| `AcceleratorTypeToTpuVersionEnum` | `0x204cf620` | Cloud accelerator-type string → public TpuType (every literal byte-traced) | CERTAIN |
| `IsAtLeastTPU7x` | `0x204cfda0` | Resolve string, test public TpuType `>= 8` (only `tpu7x`/`tpu7`) | CERTAIN |
| `TpuVersionAndVariantToHumanReadableName` | `0x20b3b040` | `TpuVersion` → human-readable name (same switch arms) | CERTAIN |

> **NOTE —** the parser is **byte-traced in full**, not inferred. After lowercasing and the `-` split, it dispatches the leading token by length: a 2-byte token is compared against the dwords/words `v2`/`v3`/`v4` (`*(_WORD*) == 0x3276`/`0x3376`/`0x3476` → TpuType 1/2/3), a 6-byte token against `v4lite` (`0x696C3476`+`0x6574` → 4), and the remaining literals through `starts_with`/`operator==` chains: `starts_with("v5lite") || == "v5e"` → 5, `== "v5p"` → 6, `== "v6e" || == "v6ea"` → 7, `== "tpu7x" || == "tpu7"` → 8. Anything else returns `Error("Unsupported accelerator type: …")` at `libtpu_init_utils.cc:67`. Every literal and its resulting TpuType above is recovered from the `0x204cf620` body — the **Public TpuType** column is CERTAIN, not inference.

---

## Marketing Names Are External-Only

The public marketing codenames are **not in the binary**. A case-insensitive scan of the entire 745 MiB image (781,691,048 B) returns zero matches for each:

| Marketing token | Occurrences in libtpu.so | Status |
|---|---|---|
| `Trillium` | 0 | Marketing-only name; externally documented as Ghostlite / `v6e`. Non-binary knowledge — not a cited fact |
| `Ironwood` | 0 | Marketing-only name; externally documented as the `6acc60406` / `TPU7x` generation. Non-binary knowledge — not a cited fact |
| `Ghostfish` | 0 | Invented gloss for `gfc` by analogy to `ghostlite`→`glc`; NOT a binary fact and NOT a real product name |

> **CORRECTION (MKT-01) —** the only canonical internal name for `TpuVersion` 5 (the `gfc` sub-core / external `"TPU7x"` generation) is the obfuscated tag `6acc60406`. The gloss "Ghostfish" — proposed by analogy (`ghostlite`→`glc`, so `ghostfish`→`gfc`) — appears nowhere in the binary (`ghostfish`/`Ghostfish` = 0 occurrences) and must not be presented as a confirmed codename. The marketing name behind `6acc60406` (externally "Ironwood") is not embedded and cannot be sourced from libtpu.so.

> **QUIRK —** "Trillium" is a documented external name for the `v6e` generation, but a reimplementer reading only the binary will never see it. Bind Ghostlite to `"TPU v6 lite"` / `v6e` (both in the image) and treat `Trillium` as an annotation, not a lookup key. The same applies to any public name for `6acc60406` (externally "Ironwood"): the binary knows it only as `6acc60406` / `"TPU7x"` / `tpu7x`. Both marketing names are layered onto the binary from external Cloud-TPU documentation, never read out of it.

---

## Cross-References

- [Codename Matrix](tpu-version-codename-matrix.md) — the `TpuVersion` enum ↔ codename source-of-truth table
- [PCI Device IDs](pci-device-ids.md) — the chip-DID → codename mapping that anchors each generation
- [Superseded-Label Correction List](codename-superseded-labels.md) — the `v5p`-vs-`v6e` and Ghostfish corrections in detail
- [HAL Families](hal-families.md) — the JXC/PXC/VXC factory routing per codename
