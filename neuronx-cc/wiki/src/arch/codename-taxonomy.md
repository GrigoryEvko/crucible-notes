# Codename ↔ Device ↔ Generation Taxonomy

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The name-mappers and the alias table live in `neuronxcc/starfish/lib/libBIR.so` (GNU build-id `a9b1ea38c47e579178b179fd445aa8edd593f206`); the codegen and hardware-model side lives in `libwalrus.so` (build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`). Both `lib*.so` are rebuilt per wheel, so every address is cp310-specific — see [Build & Version Provenance](../reference/versions.md). For `.text`/`.rodata`, virtual address equals file offset.*

## Abstract

The Neuron compiler names one piece of silicon as many as six different ways, and which name a reader sees depends only on which layer of the binary they are standing in. The same hardware generation is `inferentia` to the enum, `Inf1` to the customer, `CoreV1` to the cost model, `tonga` to the legacy front-end, ArchLevel `10` to every dispatch, and "gen1" to a human. The mapping is not arbitrary — it is a fixed five-rung ladder, and getting it wrong is the single most common way to misread these binaries. This page establishes the canonical vocabulary the rest of the wiki uses and gives a reader the means to translate any codename/arch-level/marketed-name triple seen in the binaries.

There is **no single silicon-mapping struct** in this build — no `kDeviceTypeInfo[]` array keyed by an enum the way some accelerator toolchains carry one. Instead the taxonomy is distributed across **five in-binary mechanisms that agree**: one alias-cluster table (`getArchModel`, which folds nine spellings onto four device objects) and three pure name-mappers in `libBIR` (`ArchLevel2string`, `ArchLevel2RuntimeTarget`, `ArchLevel2ExternalString`) that each project one ArchLevel ordinal into a *different* name space, plus the `libwalrus` codegen/hardware-model families that bind each ordinal to a `CoreVNGen` generator and a `<Codename>Core` hardware model. Together they pin every codename↔arch↔CoreV↔device binding. The recovery is byte-grounded: the inline string constants in the three mappers decode directly from their `mov`-immediate operands, and the `getArchModel` dispatch arms each return a known `Board` singleton.

The reimplementation contract is small but exact:

- **The five-rung ArchLevel ladder** — ordinals `{10, 20, 30, 40, 50}` = generations `{1, 2, 3, 4, 5}`, the spine every other mapping hangs off.
- **The three name spaces per ordinal** — internal (enum-canonical), runtime (cost-model codename), external (marketed device) — and the rule that the same ordinal yields three different strings.
- **The alias-cluster table** — the nine input spellings `getArchModel` accepts and the four `Board` singletons they collapse onto, including the two off-by-one traps (`Trn1` = gen2, not gen1; `inf2` clusters under Sunda).
- **The two asymmetric edge generations** — `CoreV1` (modelled but un-codegen'd) and `CoreV5` (a pure forward stub) — covered fully in [1.03](vestigial-generations.md).

| | |
|---|---|
| **Alias-cluster table** | `getArchModel(string const&)` @ libBIR `0x478f90` (libwalrus copy @ `0x17344c0`) |
| **Internal name** | `bir::ArchLevel2string` @ libBIR `0x479490` |
| **Runtime codename** | `bir::ArchLevel2RuntimeTarget` @ libBIR `0x479570` ⟵ the `mariana` proof |
| **Marketed device** | `bir::ArchLevel2ExternalString` @ libBIR `0x479650` |
| **Inverse parse** | `bir::string2ArchLevel` @ libBIR `0x479720`; `bir::isArchSupported` @ `0x4797d0` |
| **CoreVN codegen select** | `birverifier::InstVisitor::initCodegen` @ libwalrus `0xfc5d00`; `Codegen::codegen` @ `0x11d2c50` |
| **ArchLevel ordinals** | `{10, 20, 30, 40, 50}` (= 0x0a / 0x14 / 0x1e / 0x28 / 0x32) |
| **Active codegen floor / ceiling** | gen2 (arch 20) … gen4 (arch 40); gen1 + gen5 are non-codegen |

---

## The authoritative table

This is the deliverable. Every column is a different name for the same five hardware generations; every row is one generation. Read across to translate.

| arch | gen | internal (`2string`) | runtime (`2RuntimeTarget`) | marketed (`2ExternalString`) | CoreVN | HW-model family | codegen (`initCodegen`) | `getArchModel` aliases |
|---|---|---|---|---|---|---|---|---|
| **10** | 1 | `inferentia` | `inferentia` | `Inf1` | CoreV1 | `Inferentia*` | — (legacy path) | `tonga`, `inferentia`, `inf1` |
| **20** | 2 | `sunda` | `sunda` | `Trn1` | CoreV2 | `Sunda*` | `CoreV2Gen` | `sunda`, `trainium`, `trn1`, `inf2` |
| **30** | 3 | `gen3` | `cayman` | `Trn2` | CoreV3 | `Cayman*` | `CoreV3Gen` | `cayman`, `gen3` |
| **40** | 4 | `core_v4` | `mariana` | `Trn3` | CoreV4 | `CoreV4*` | `CoreV4Gen` | `core_v4` |
| **50** | 5 | `core_v5` | `core_v5` | `Trn4` | CoreV5 | — (none) | — (throws) | — (no Board → assert) |

*Confidence: arch ordinals, all three name-space strings, CoreVN binding, and the codegen projection are **CONFIRMED** (byte-read off the mapper bodies and the `getArchModel` dispatch arms). The HW-model family ↔ generation binding is **CONFIRMED** (dynsym ctor census). Cores-per-chip is deliberately omitted — it is not a binary literal in this build (see [§ Cores-per-chip](#cores-per-chip-is-not-a-binary-constant)).*

Three facts in this table are the ones a reader trips over, so they get their own callouts below: the **internal name and runtime name diverge at gen3+** (`gen3`/`cayman`, `core_v4`/`mariana`); the **marketed Trainium index trails the generation index by one** (`Trn1` = gen2); and **`inf2` is a Sunda SKU**, not its own generation.

> **QUIRK —** the same ArchLevel ordinal produces three different strings depending on which mapper you call. `ArchLevel2string(40)` = `"core_v4"`, `ArchLevel2RuntimeTarget(40)` = `"mariana"`, `ArchLevel2ExternalString(40)` = `"Trn3"`. None is "the" name; each is the canonical name *for its layer*. A reimplementer must keep three parallel name tables, not one.

---

## The alias-cluster table — `getArchModel`

### Purpose

`getArchModel` is the single forgiving input parser: it accepts nine codename spellings and folds them onto four `Board` singletons (one per built generation). It is what turns a user-supplied or module-carried arch string into a hardware-model object. It is *not* a name *emitter* — it never produces the canonical output spelling; that is the job of the three `ArchLevel2*` mappers. Its asymmetry (nine inputs, four outputs, with no `core_v5` arm) is the structural reason gen5 is a forward stub: there is no `Board` for it to return.

### Entry Point

```text
bir::Module::getArch()            (libBIR 0x354ef0) ── module's arch codename string
        │
        ▼
getArchModel(codename)            libBIR 0x478f90  (libwalrus copy 0x17344c0)
        │  linear std::string::compare(const char*) chain — one arm per alias
        ▼
returns const Board&  →  one of four .bss singletons, else __assert_fail
```

### Algorithm

The body is a flat chain of `std::string::compare(PKc)` calls — ten compares in the libBIR copy — each arm returning the address of a static `Board` on match, with the no-match path aborting. The arms are grouped: several aliases share one return target.

```c
// _Z12getArchModel... @ libBIR 0x478f90  — CONFIRMED (data_refs + strings_referenced)
const Board& getArchModel(const std::string& name) {
    // gen1 cluster → Board @0x91de40
    if (name == "tonga"      // rodata 0x70c6f4
     || name == "inferentia" // rodata 0x70a339
     || name == "inf1")      // rodata 0x70c6fa
        return *(Board*)0x91de40;           // lea @0x478fa4

    // gen2 cluster → Board @0x91dfc0
    if (name == "sunda"      // 0x70c6ff
     || name == "trainium"   // 0x70c705
     || name == "trn1"       // 0x70c70e
     || name == "inf2")      // 0x70c713
        return *(Board*)0x91dfc0;           // lea @0x478ffc

    // gen3 cluster → Board @0x91e140
    if (name == "cayman"     // 0x70c718
     || name == "gen3")      // 0x70c71f
        return *(Board*)0x91e140;           // lea @0x479070

    // gen4 → Board @0x91e2c0
    if (name == "core_v4")   // 0x70c724
        return *(Board*)0x91e2c0;           // lea @0x479067

    // no "core_v5" arm — gen5 has no Board
    __assert_fail("0 && \"Unknown architecture\"",   // 0x70c72c
                  "ctm.cpp", /*line*/ 0x5F, ...);     // call @0x479093
}
```

The four `Board` return targets (`0x91de40` / `0x91dfc0` / `0x91e140` / `0x91e2c0`) and the nine alias-string addresses are read directly from the function's `data_refs` and `strings_referenced`. The aliases sit in a contiguous `.rodata` run from `0x70c6f4` upward; `core_v5` (`0x70c748`) follows the `"0 && Unknown architecture"` assert literal (`0x70c72c`) and is *not* one of the compare arms — the spatial layout itself shows gen5 is past the table.

> **GOTCHA —** `getArchModel` accepts **`inf2` under the gen2/Sunda cluster** and **`trainium`/`trn1` also under Sunda**. `inf2` is the inference-optimised SKU of the *same* gen2 silicon, so it shares Sunda's `Board`. A reimplementer must not give `inf2` its own generation; it is a board variant, not a CoreV. The marketed *output* name for gen2 is `Trn1` (not `Inf2`) — `getArchModel` is a many-spellings-in parser, `ArchLevel2ExternalString` is the one-canonical-name-out emitter.

### Considerations

The `Board` singletons themselves carry **no inline ArchLevel integer** in libBIR; they are storage references populated at library load by the hardware-model constructors (the geometry tree of [1.01](arch-object-model.md)). So `getArchModel` binds *name → object*, not *name → ordinal*; the ordinal binding is the inverse parser `string2ArchLevel` (below). The libwalrus copy of `getArchModel` (`0x17344c0`) is the same dispatch with the same nine aliases and four singletons, in `.bss` at different addresses — a cross-binary consistency check, two compilations of one `ctm` source.

---

## The three name-mappers — one ordinal, three name spaces

All three are dense `switch(ArchLevel)` bodies over the ordinal axis `{0x0a, 0x14, 0x1e, 0x28, 0x32}`, with the default arm aborting via `__assert_fail("Unknown arch level", Architectures.cpp, …)` (literal `0x70c750`). They differ only in which string each ordinal maps to. The decisive evidence is each function's `constants_used` array: short codenames are **constructed inline** from `mov`-immediate operands rather than loaded from `.rodata`, and those immediates decode straight to ASCII.

> **NOTE — how an inline string decodes.** A 4-byte little-endian immediate `0x6972616d` written into a `std::string`'s buffer is the bytes `6d 61 72 69` = `m a r i`. The mapper follows it with a 2-byte `0x6e61` = `a n` and a single `0x61` = `a`, assembling `"mariana"` with no `.rodata` pointer. This is why grepping `.rodata` for `"mariana"` finds nothing while the string is provably emitted: it lives in the code, not the string pool.

### `ArchLevel2string` — the internal (enum-canonical) name

```c
// _ZN3bir16ArchLevel2string... @ libBIR 0x479490  — CONFIRMED
const char* ArchLevel2string(ArchLevel a) {     // constants_used: 10,20,30,40,50, ...
    switch (a) {
    case 10: return "inferentia";               // rodata 0x70a339
    case 20: return "sunda";                     // inline 0x646e7573 'sund' + 0x61 'a'
    case 30: return "gen3";                      // rodata 0x70c71f  ← NOT "cayman"
    case 40: return "core_v4";                   // inline 0x65726f63 'core' + 0x765f '_v' + 0x34 '4'
    case 50: return "core_v5";                   // rodata 0x70c748
    default: __assert_fail("Unknown arch level", "Architectures.cpp", ...);  // 0x70c750
    }
}
```

This is the **enum-canonical** roster — the spelling the `bir::ArchLevel` enum is named with, and the spelling `string2ArchLevel` parses back. Note `case 30` yields `"gen3"`, *not* `"cayman"`: the internal name space never uses the silicon codename for gen3+. (Confirmed two ways: the `"cayman"` string at `0x70c718` is referenced only by `getArchModel`, never by `ArchLevel2string`.)

### `ArchLevel2RuntimeTarget` — the runtime codename (the `mariana` proof)

```c
// _ZN3bir23ArchLevel2RuntimeTarget... @ libBIR 0x479570  — CONFIRMED
const char* ArchLevel2RuntimeTarget(ArchLevel a) {  // constants_used: 10,20,30,40,50, mari/an, caym/an
    switch (a) {
    case 10: return "inferentia";                    // rodata 0x70a339
    case 20: return "sunda";                          // rodata 0x70c6ff
    case 30: return "cayman";                         // inline 0x6d796163 'caym' + 0x6e61 'an'
    case 40: return "mariana";                        // inline 0x6972616d 'mari' + 0x6e61 'an' + 0x61 'a'
    case 50: return "core_v5";                         // rodata 0x70c748
    default: __assert_fail("Unknown arch level", ...);
    }
}
```

> **This is the single in-binary site that ties ArchLevel 40 to the name `mariana`.** The decode is unambiguous: the function's `constants_used` array contains `1769103725` (= `0x6972616d` = `mari` LE) and `28257` (= `0x6e61` = `an` LE), and these occur in the `case 40` arm. Likewise `case 30` carries `1836671331` (= `0x6d796163` = `caym`). So the runtime-codename projection is: gen3 = `cayman`, gen4 = `mariana`. Earlier reports could only *infer* "Mariana = gen4"; this mapper **confirms** it. [CONFIRMED — inline immediate decode]

The "runtime target" name is the one the cost model and the perf simulator use as a label. Crucially, **`mariana` has no other home in the binary.** It is *not* a libwalrus hardware-model class name (gen4's HW classes are the generic `CoreV4*`, not `Mariana*`), and the only literal `"mariana"`/`"Mariana"` strings in libwalrus are timezone data (`"North_Mariana"` @ `0x3054cdc`, `"meta:North_Mariana"` @ `0x3885b06`/`0x3931eea`) — unrelated to the codename. The authoritative gen4-codename evidence is this `RuntimeTarget` arm alone, plus the perf-sim `CoreV4Hwm` label.

> **CORRECTION (supersedes prior "Mariana\* HW class" assumptions).** There is **no** `Mariana*` Core/Board/Pe/Engine class in libwalrus. The gen4 hardware-model family reverted to the generic stem `CoreV4*` (`CoreV4Core`, `CoreV4Board`, `CoreV4Pe`, …). The codename `mariana` survives only as (1) this `ArchLevel2RuntimeTarget(40)` string and (2) the perf-sim `CoreV4Hwm` label. Do not look for a `Mariana*` symbol; it does not exist.

### `ArchLevel2ExternalString` — the marketed device name

```c
// _ZN3bir24ArchLevel2ExternalString... @ libBIR 0x479650  — CONFIRMED
const char* ArchLevel2ExternalString(ArchLevel a) { // constants_used: 10,20,30,40,50, Inf1, Trn4
    switch (a) {
    case 10: return "Inf1";   // inline 0x31666e49 = 'I' 'n' 'f' '1'
    case 20: return "Trn1";   // rodata 0x70c763   (data_ref @0x47966a)
    case 30: return "Trn2";   // rodata 0x70c768   (data_ref @0x4796b0)
    case 40: return "Trn3";   // rodata 0x70c76d   (data_ref @0x4796f0)
    case 50: return "Trn4";   // inline 0x346e7254 = 'T' 'r' 'n' '4'
    default: __assert_fail("Unknown arch level", ...);
    }
}
```

This is the customer-facing device-name table — what appears in AWS instance documentation. The endpoints (`Inf1`, `Trn4`) are inline immediates; the three middle entries are `.rodata` pointers, confirmed by the function's `data_refs` to `0x70c763` / `0x70c768` / `0x70c76d` (the IDA string scanner absorbed these 5-byte tokens into adjacent strings, but the data references resolve them exactly).

> **GOTCHA — the Trainium index is off by one from the generation index.** The marketed map is `gen1 → Inf1`, then `gen2 → Trn1`, `gen3 → Trn2`, `gen4 → Trn3`, `gen5 → Trn4`. So **`Trn1` is gen2 (Sunda), not gen1.** "TrnN" denotes generation `N+1`; the first generation is the inference-only `Inf1`, and Trainium numbering starts one behind the generation count. Any reading that equates `Trn1` with the first/legacy generation is wrong — that is `Inf1`. This is the most error-prone cell in the whole table.

### The inverse: `string2ArchLevel` and `isArchSupported`

```c
// _ZN3bir16string2ArchLevel... @ libBIR 0x479720  — CONFIRMED
ArchLevel string2ArchLevel(const std::string& s) {  // constants_used: 10,20,30,40,50
    if (s == "inferentia") return 10;   // 0x70a339
    if (s == "sunda")      return 20;   // 0x70c6ff
    if (s == "gen3")       return 30;   // 0x70c71f  (data_ref @0x47975c) — note: "gen3", not "cayman"
    if (s == "core_v4")    return 40;   // 0x70c724
    if (s == "core_v5")    return 50;   // 0x70c748
    __assert_fail("Unknown arch level", ...);
}
// _ZN3bir15isArchSupported... @ 0x4797d0 — same 5-token membership → bool
```

`string2ArchLevel` parses **only the enum-canonical spellings** (`ArchLevel2string`'s output set), *not* the silicon codenames. It does not accept `cayman` or `mariana` — those are output-only runtime labels. The canonical *input* spelling of an arch flag is therefore `{inferentia, sunda, gen3, core_v4, core_v5}`; the broader alias set (`tonga`/`trn1`/`inf2`/`cayman`/…) is accepted only by `getArchModel`, which resolves to a `Board` rather than an ordinal. `isArchSupported` is the same five-token membership test returning a bool.

### `ArchRevision2string` — a separate stepping axis (not a generation)

A fourth small mapper, `bir::ArchRevision2string` @ `0x479860`, projects an `ArchRevision` ordinal (carried alongside `ArchLevel` by the `bir::Arch(ArchLevel, ArchRevision)` constructor) into a short string. Its `constants_used` decode to `"v1"` (`0x3176`) and `"v2"` (`0x3276`), with a default arm asserting `"Unknown arch revision"` (`0x70c772`). This is a **silicon stepping / minor-revision** field, orthogonal to the codename taxonomy — a part of gen N can exist at revision v1, v2, … It is a flag, not a generation; the exact revision spelling beyond v1/v2 is **INFERRED** (the full roster was not enumerated) and it does not participate in any of the name spaces above.

---

## The codegen and hardware-model bindings (libwalrus)

The three libBIR mappers fix *names*; libwalrus fixes *behavior* — which generation gets a code generator and which gets a hardware model. These two facets are **asymmetric across the five generations**, and the asymmetry is the whole content of [1.03](vestigial-generations.md).

### The CoreVN code-generator selection

Two libwalrus sites dispatch ArchLevel to a `CoreVNGen` generator, and both enumerate **exactly the same three** generations:

```c
// birverifier::InstVisitor::initCodegen @ libwalrus 0xfc5d00  — CONFIRMED
// selects std::variant<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>
switch (module.archLevel) {                 // constants_used: 20, 30, 40
    case 0x28: construct CoreV4Gen;         // arch 40 → ctor @0x62b170
    case 0x1e: construct CoreV3Gen;         // arch 30 → ctor @0x61dcc0
    case 0x14: construct CoreV2Gen;         // arch 20 → ctor @0x611690
    // no CoreV1Gen / CoreV5Gen arm
}
```

```c
// neuronxcc::backend::Codegen::codegen @ libwalrus 0x11d2c50  — CONFIRMED
switch (archLevel) {                        // constants_used include 20, 30, 40
    case 0x14: → CoreV2Gen   (arch 20)
    case 0x1e: → CoreV3Gen   (arch 30)
    case 0x28: → CoreV4Gen   (arch 40)
    default:   → throw   ("Codegen: unknown arch " @0x1c83ec6)
}
```

So the **active codegen world is `{CoreV2, CoreV3, CoreV4}` = arch `{20, 30, 40}` = gens 2–4.** Arch 10 (gen1/Inferentia) is *not* a `CoreVNGen` target — it is handled by the legacy `Inferentia*` path and has no unified generator. Arch 50 (gen5/CoreV5) has no codegen at all and falls through to the throw. The `std::variant` *type list* itself — `<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>` — structurally excludes CoreV1Gen and CoreV5Gen.

The RTTI confirms this independently: the only `GenImpl` typeinfo symbols in libwalrus are `_ZTIN9neuronxcc7backend13CoreV{2,3,4}GenImplE` (with matching `_ZTS`/`_ZTV`); there is no `CoreV1GenImpl` or `CoreV5GenImpl` symbol, vtable, or typeinfo. The variants ctor addresses (`CoreV2Gen @0x611690`, `CoreV3Gen @0x61dcc0`, `CoreV4Gen @0x62b170`) are read off `initCodegen`'s callee list.

### The hardware-model families

Separately from codegen, libwalrus carries a full per-codename hardware-model class set (the latency/architectural-profile model that the geometry tree of [1.01](arch-object-model.md) hangs off). Each generation that has a model ships the same nine-class family — `Core`, `Board`, `Device`, `Pe`, `Pool`, `Act`, `Dve`, `Psumbuf`, `Statebuf` — under its codename stem:

| Generation | HW-model stem | dynsym symbol count | Codegen generator |
|---|---|---|---|
| gen1 | `Inferentia*` | 35 | — (legacy, no `CoreVNGen`) |
| gen2 | `Sunda*` | 34 | `CoreV2Gen` |
| gen3 | `Cayman*` | 43 | `CoreV3Gen` |
| gen4 | `CoreV4*` (generic) | 234 | `CoreV4Gen` |
| gen5 | — (none) | 0 | — (throws) |

*Confidence: symbol-count census CONFIRMED via `nm`/native-exports. The gen4 stem is the generic `CoreV4`, not `Mariana` (see the runtime-codename correction above).*

> **NOTE — the two facets do not line up at the edges.** gen1 has a *full hardware model* (`Inferentia*` + `_inferentia_arch_model`) but **no codegen** — it is a modelled-but-deprecated legacy generation. gen5 has **neither** model nor codegen — a pure forward stub (no `Gen5*`/`CoreV5*` classes, no `_core_v5_arch_model`). So "the floor" differs by layer: the **codegen floor is gen2** (arch 20), but **analysis-layer behavior still exists at gen1** — e.g. `AntiDependencyAnalyzer::getPSUMPartitionRange` keeps a live arch≤19 branch enforcing CoreV1's partition-0 PSUM rule. The full treatment of both edge generations is [1.03](vestigial-generations.md). One implementation detail worth flagging here: the `<Codename>Core` hardware-model classes are **plain structs with no vtables** — they are populated by the per-arch constructors, not dispatched polymorphically.

### The arch ordinals are generation × 10

Because the ordinals are exactly `gen × 10`, the comparison gates scattered through the backend read as clean generation cuts:

- `arch ≤ 19` ⇔ ArchLevel == 10 ⇔ gen1 (Inferentia/Tonga) only.
- `arch ≥ 20` (often `> 19`) ⇔ the modern-CoreV floor (gen2+).
- `arch == 0x28` (40) ⇔ gen4 / CoreV4 / mariana exactly.
- `arch > 40` / `< ArchLevel::core_v5` ⇔ the gen5 forward gate (currently never taken by any built target).

The libwalrus RMW-alignment knob enumerates the ladder verbatim in one string (binary's own typo preserved): `"Any arch including and beyond set value will have RMW alignment triggered. Availe value tonga, sunda,gen3,core4,gen5"` (@ `0x1dc1808`). This `tonga < sunda < gen3 < core4 < gen5` ordering is the canonical generation order, using the shorthands `core4`/`gen5`.

---

## The marketed-instance layer

One level above the codename taxonomy sits the EC2-instance-name layer, in the Cython module `driver/InstanceFamily`. The user passes a `--target-instance` value; the driver folds it onto a `getArchModel` alias. The instance-token roster (`inf1`, `inf2`, `trn1`, `trn1n`, `trn2`, `trn2n`, `trn3`, `trn3pre`, `gen3`, `core_v4`, `core_v5`) collapses onto the four built generations following the alias clusters and the marketed `ExternalString` names:

| Instance tokens | Folds to | arch |
|---|---|---|
| `inf1` | `inferentia` | 10 |
| `trn1`, `trn1n`, `inf2` | `sunda` | 20 |
| `trn2`, `trn2n`, `gen3` | `cayman` / gen3 | 30 |
| `trn3`, `trn3pre`, `core_v4` | `core_v4` / mariana | 40 |
| `core_v5` | `core_v5` | 50 (forward) |

*Confidence: **STRONG** by correspondence. Each token's arch binding follows the `getArchModel` alias clusters and the `ExternalString` marketed names unambiguously, but the Cython `instanceToFamily` dict body was not byte-disassembled, and the exact fold of the `*n` (network-optimised) / `*pre` (pre-production) suffix variants onto the same ArchLevel is **INFERRED**, not byte-proven.* The libwalrus feature-gate strings corroborate the binding: messages like *"on TRN1 … On TRN3+, use COPY"*, *"PSUM write must be FP32 … for trn2"*, and *"Shared memory is only supported on trn2"* place TRN1 at gen2-era, TRN2 at gen3, TRN3 at gen4 — exactly the `ExternalString` map.

---

## Cores-per-chip is not a binary constant

A reader expecting a "NeuronCores per chip" integer (4 for Inferentia, 2 for Trainium, …) will not find one in this build. There is **no** cores-per-chip literal in libBIR or libwalrus. The per-generation geometry is carried by four runtime-built `arch_model` objects (`_inferentia_arch_model`, `_sunda_arch_model`, `_cayman_arch_model`, `_core_v4_arch_model`), populated at load time, and the marketed per-chip core count is runtime state, not a compile-time constant. The only physical-core constant the backend pins is the **logical-core ceiling**: the assert *"`(LNC_SIZE <= 2)` && Only logical cores containing 1 or 2 physical cores are currently supported"* (CONFIRMED) — i.e. a logical NeuronCore spans 1 or 2 physical cores (the LNC2 topology for gen3/gen4). Any exact per-chip count is therefore **SPECULATIVE** from the binary alone and must be read from the `arch_model` object at runtime, never hard-coded. The hardware-constant detail lives in [1.04](hardware-constant-matrix.md).

---

## Related Components

| Component | Relationship |
|---|---|
| `getArchModel` (libBIR `0x478f90` / libwalrus `0x17344c0`) | the alias-cluster table — name → `Board` singleton; spine of the geometry tree |
| `bir::ArchLevel2{string,RuntimeTarget,ExternalString}` | the three name-mappers — one ordinal → three name spaces |
| `bir::string2ArchLevel` / `isArchSupported` | the inverse parse + membership test (enum-canonical spellings only) |
| `initCodegen` / `Codegen::codegen` (libwalrus) | bind ArchLevel → `CoreVNGen` generator; define the active-codegen set `{20,30,40}` |
| `<Codename>{Core,Board,Pe,…}` HW classes | per-generation hardware-model families; the geometry of [1.01](arch-object-model.md) |
| `driver/InstanceFamily` (Cython) | the marketed EC2-instance → alias layer above the codename taxonomy |

## Cross-References

- [1.01 The Arch Object Model](arch-object-model.md) — the `getArchModel → Board → Device → Core` geometry tree this page's alias-cluster table feeds; the four `Board` singletons by codename.
- [1.03 Vestigial Generations (CoreV1 & CoreV5)](vestigial-generations.md) — the two asymmetric edge generations introduced here: gen1 (modelled, un-codegen'd) and gen5 (pure forward stub), with the full string/gate inventory.
- [1.04 Per-Generation Hardware-Constant Matrix](hardware-constant-matrix.md) — the actual geometry numbers per generation, keyed by the codenames this page decodes; where cores-per-chip would live if it were a literal.
- [Glossary & Naming Conventions](../glossary.md) — the one-line definitions of every codename and the CoreVN / Trn / Inf naming families.
