# Vestigial Generations: CoreV1 (Inferentia) & CoreV5

> *All symbols and addresses on this page are read from the **cp310** wheel of `neuronx_cc` 2.24.5133.0+58f8de22. `libwalrus.so` BuildID `92b4d331…`; `libBIR.so` BuildID `a9b1ea38…`. The C++ libraries are rebuilt per wheel (cp310/cp311 share a size but not a SHA; cp312 differs), so re-confirm addresses against cp311/cp312 — see [Version Provenance](../reference/versions.md). For both binaries, `.text`/`.rodata` VMA equals file offset. Treat every address as version-pinned.*

## Abstract

The arch model carries five generations but the backend codegen accepts only three. This page pins that gap precisely, so a reimplementer never mistakes a modelled-but-dead generation for a supported target. Two of the five `ArchLevel` ordinals — **10** (CoreV1 / Inferentia / Tonga / gen1) and **50** (CoreV5 / gen5) — are *vestigial*: both are recognized enum values, both appear in the codename string-mapper, and neither is a reachable target of `Codegen::codegen`. The decisive fact is a single switch in `libwalrus.so`: `Codegen::codegen` (@ `0x11d2cc1`) dispatches on `cmp $0x14` / `cmp $0x1e` / `cmp $0x28` (=20/30/40 = CoreV2/V3/V4) and falls through *everything else* to `boost::throw_exception(std::out_of_range)` (@ `0x73a12c`). There is no `cmp $0xa` (10) and no `cmp $0x32` (50). **The codegen floor is arch 20.**

But the two vestigial generations are not symmetric, and conflating them is the trap this page closes. **CoreV1 is fully *modelled* but has no codegen**: `libwalrus` carries a complete `Inferentia{Act,Dve,Pe,Pool,Psumbuf,Statebuf,Board,Core,Device}` hardware-engine hierarchy plus the static `_inferentia_arch_model` singleton — structurally parallel to Sunda(gen2)/Cayman(gen3)/CoreV4(gen4) — and a *live* analysis-pass branch (`AntiDependencyAnalyzer::getPSUMPartitionRange`, gated `cmpl $0x13`=19) that still enforces Inferentia's partition-0 PSUM rule at arch ≤ 19. What it lacks is a `CoreV1GenImpl` backend class. **CoreV5 is the opposite — a pure forward-declaration stub**: a reserved enum ordinal (50, with *no* `Board`, so `getArchModel` asserts "Unknown architecture"), six `core_v5` strings that are all dormant `</>= ArchLevel::core_v5` feature-gates plus one CLI token, and one `CoreV5` DMA placeholder string. CoreV5 has zero hardware-engine classes, zero ISA tables, zero codegen. So "vestigial" means two different things: **CoreV1 = modelled-but-deprecated; CoreV5 = reserved-but-unbuilt.**

For reimplementation, the contract is: drive codegen off the three live arches (20/30/40); model the gen1 *analysis* floor at arch 10 if you want byte-parity with the Inferentia PSUM rule; and treat every `core_v5` branch as a dormant feature flag that fires uniformly off for all current arches.

| | |
|---|---|
| **Codegen floor (arch)** | `20` (CoreV2) — `Codegen::codegen` switch @ `libwalrus 0x11d2cc1` |
| **Analysis floor (arch)** | `10` (CoreV1/Tonga) — `getPSUMPartitionRange` gate `cmpl $0x13`(=19) @ `0x8c175b` |
| **Live codegen GenImpls** | `{CoreV2GenImpl, CoreV3GenImpl, CoreV4GenImpl}` only (nm + RTTI) |
| **CoreV1 (arch 10)** | HW model PRESENT (`Inferentia*` + `_inferentia_arch_model`); codegen ABSENT |
| **CoreV5 (arch 50)** | pure stub: enum ordinal + ~7 dormant gates + 1 CLI token; nothing executable |
| **String census (libwalrus)** | `core_v5`×6 · `CoreV5`×1 · `CoreV1`×1 · `core_v1`×0 (cp310/311/312 invariant) |
| **getArchModel assert (libBIR)** | `__assert_fail("0 && \"Unknown architecture\"")` @ `0x479093` — gen5 has no `Board` |

---

## The codegen floor is arch 20

### Purpose

The single fact every other claim hangs from: which `ArchLevel` ordinals reach a `Generator`. If an arch ordinal does not match an arm of the `Codegen::codegen` switch, no `CoreV*Gen` is instantiated for it and compilation throws. This is the proof that arch 10 and arch 50 are not codegen targets.

### The arch-select switch

`Codegen::codegen(bir::Module&)` (`_ZN9neuronxcc7backend7Codegen7codegenERN3bir6ModuleE`) loads the module's `ArchLevel` into `%ebp` and runs a three-arm equality ladder. Disassembled verbatim at `libwalrus 0x11d2cc1`:

```asm
; Codegen::codegen arch-select  (libwalrus 0x11d2cc1)
11d2cc1:  83 fd 14        cmp    $0x14,%ebp          ; arch == 20 (CoreV2)?
11d2cc4:  0f 84 ce06..    je     11d3398             ;   → CoreV2Gen path
11d2cca:  83 fd 1e        cmp    $0x1e,%ebp          ; arch == 30 (CoreV3)?
11d2ccd:  0f 84 7d01..    je     11d2e50             ;   → CoreV3Gen path
11d2cd3:  83 fd 28        cmp    $0x28,%ebp          ; arch == 40 (CoreV4)?
11d2cd6:  0f 85 5074..    jne    73a12c              ;   else → boost::throw_exception
                                                     ;          <std::out_of_range>
11d2d10:  e8 ..           call   62b170              ; CoreV4Gen::CoreV4Gen(...)  (the 40 arm)
```

The C shape:

```c
// Codegen::codegen arch-select  (libwalrus 0x11d2c50, switch @0x11d2cc1)
void Codegen::codegen(bir::Module &m) {
    int arch = m.archLevel;                  // %ebp
    if (arch == 0x14) { /* CoreV2Gen */  }   // 20  CONFIRMED je 11d3398
    else if (arch == 0x1e) { /* CoreV3Gen */ } // 30 CONFIRMED je 11d2e50
    else if (arch == 0x28) { /* CoreV4Gen */ } // 40 CONFIRMED call 62b170
    else
        boost::throw_exception(std::out_of_range(...)); // 0x73a12c — arch 10 AND 50 land here
}
```

There is no `cmp $0xa` (10 / CoreV1) and no `cmp $0x32` (50 / CoreV5) anywhere in the ladder. Both vestigial ordinals hit the `out_of_range` throw. The `40` arm is the one that calls a `Gen` ctor inline here (`CoreV4Gen::CoreV4Gen` @ `0x62b170`); the `20`/`30` arms branch to their own constructor sites.

> **NOTE — the analysis floor is lower than the codegen floor.** `ArchLevel2string` (`libBIR 0x479490`) *does* carry a `cmp $0xa` arm (=10) and a `cmp $0x32` arm (=50) — confirmed `cmp $0xa,%esi` @ `0x47949c` and `cmp $0x32,%esi` @ `0x4794d5`. So arch 10 and arch 50 are real, named enum ordinals; they are simply absent from the *codegen* switch. The codegen floor (20) and the enum floor (10) differ by layer — a distinction that is the whole subject of [§CoreV1](#corev1-arch-10--fully-modelled-but-deprecated).

### Function Map

| Symbol | Binary | Address | Role | Confidence |
|---|---|---|---|---|
| `Codegen::codegen(bir::Module&)` | libwalrus | `0x11d2c50` (switch @ `0x11d2cc1`) | Arch → `Generator` dispatch; floor = 20 | CONFIRMED |
| `boost::throw_exception<std::out_of_range>` | libwalrus | `0x73a12c` | Fall-through for arch ∉ {20,30,40} | CONFIRMED |
| `CoreV4Gen::CoreV4Gen(...)` | libwalrus | `0x62b170` | The arch-40 ctor called inline | CONFIRMED |
| `ArchLevel2string(int)` | libBIR | `0x479490` | Names all five ordinals incl. 10 & 50 | CONFIRMED |

The mirror selection path in the verifier is the same three-arch world: the `birverifier` codegen variant is `std::variant<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>` — the *type list itself* excludes `CoreV1Gen` and `CoreV5Gen`. Same three arches, same exclusion, independently. [STRONG — variant type-list]

---

## The codegen class census: {V2, V3, V4} only

### Purpose

The switch proves arch 10/50 are unreachable; the symbol table proves there is no class to reach even if the switch grew an arm. A `CoreV*GenImpl` class would carry a vtable and an RTTI `typeinfo` object. Their presence/absence is a clean binary test.

### Evidence

`nm -DC libwalrus.so` yields exactly three `CoreV*GenImpl` symbols and three matching `typeinfo` objects:

```text
# GenImpl class set (nm -DC libwalrus.so | rg 'CoreV[0-9]GenImpl')
neuronxcc::backend::CoreV2GenImpl
neuronxcc::backend::CoreV3GenImpl
neuronxcc::backend::CoreV4GenImpl

# RTTI typeinfo objects (the D-J22 V4▸V3▸V2 single-inheritance ladder)
0x03d95310 V typeinfo for neuronxcc::backend::CoreV2GenImpl
0x03d95928 V typeinfo for neuronxcc::backend::CoreV3GenImpl
0x03d95c88 V typeinfo for neuronxcc::backend::CoreV4GenImpl
```

There is **no** `CoreV1GenImpl`, `CoreV5GenImpl`, `InferentiaGen`, or `TongaGen` symbol, vtable, or `typeinfo` — `nm` returns zero hits for all of them. Absence of RTTI is absence of a codegen class.

| Generation | `ArchLevel` | `CoreV*GenImpl` class | RTTI `typeinfo` | Codegen target | Confidence |
|---|---|---|---|---|---|
| CoreV1 (Inferentia/Tonga) | 10 | — absent | — absent | **no** | CONFIRMED |
| CoreV2 (Sunda) | 20 | present | `0x03d95310` | yes | CONFIRMED |
| CoreV3 (Cayman) | 30 | present | `0x03d95928` | yes | CONFIRMED |
| CoreV4 | 40 | present | `0x03d95c88` | yes | CONFIRMED |
| CoreV5 | 50 | — absent | — absent | **no** | CONFIRMED |

The ISA opcode-name tables agree exactly. The `enum_variant_string_opcode(int, char*, int)` family — the opcode→mnemonic table per generation — exists for `core_v2` (@ `0x127aea0`), `core_v3` (@ `0x1369a40`), and `core_v4` (@ `0x143fd80`) **only**; there is no `core_v1::` or `core_v5::` ISA namespace in the dynamic symbol table at all. [CONFIRMED — nm -DC]

---

## CoreV1 (arch 10) — fully modelled but deprecated

### Purpose

CoreV1 is the trap: prior reports that called CoreV1's "hardware constants absent" are wrong. The gen1 hardware model is present and complete. What is absent is codegen. This section separates the three layers — HW model (present), analysis rule (live), codegen (absent) — so a reader knows which CoreV1 facilities a reimplementer can ignore and which still run.

> **CORRECTION — CoreV1 hardware constants are NOT absent.** An earlier reading stated that CoreV1 and CoreV5 are symmetric "1 string each" vestigials with their hardware constants gone. That is false for CoreV1. `libwalrus` carries the full `Inferentia*` engine-class hierarchy and the `_inferentia_arch_model` singleton (`0x3e05800`, `.bss`). CoreV1 is a fully-modelled legacy generation; only its *codegen* `GenImpl` is missing. The two vestigial gens are asymmetric.

### The hardware model is present

`nm -DC libwalrus.so` shows the complete gen1 engine hierarchy — every class that Sunda/Cayman/CoreV4 carry has an `Inferentia` peer:

```text
# Inferentia HW engine classes (nm -DC | rg 'Inferentia(Act|Dve|Pe|Pool|Psumbuf|Statebuf|Board|Core|Device)')
InferentiaAct   InferentiaDve   InferentiaPe   InferentiaPool   InferentiaPsumbuf
InferentiaStatebuf   InferentiaBoard   InferentiaCore   InferentiaDevice

# the static singleton (.bss)
0x3e05800 B _inferentia_arch_model       # parallel to _sunda/_cayman/_core_v4_arch_model
```

The `InferentiaCore` ctor (`0x1734720`) is a peer of `SundaCore`/`CaymanCore`/`CoreV4Core` and fills the same `Board → Device → Core → engine` tree documented in [The Arch Object Model](arch-object-model.md). Its divergent immediates encode the half-width nature of gen1 (Statebuf full-width 128 partitions, but Psumbuf 64 partitions, PE cols 64, Pool/Act `numChannels` 64) — geometry, not codegen. So the arch *geometry* floor is 10: `getArchModel("inferentia"|"tonga"|"inf1")` returns a real `Board`.

### A live analysis-pass branch (arch ≤ 19)

CoreV1 still executes in the dependency-analysis layer. `AntiDependencyAnalyzer::getPSUMPartitionRange(PhysicalAccessPattern const&)` branches on the arch level to choose the PSUM partition-alignment rule, and the gen1 arm is reachable for any arch ≤ 19:

```asm
; AntiDependencyAnalyzer::getPSUMPartitionRange  (libwalrus, gate @0x8c175b)
8c175b:  cmpl   $0x13,0xac(%r12)     ; arch <= 19 ?  (0x13 = 19)
8c1764:  jle    8c17a0               ;   → CoreV1 %64 / partition-0 path
...
8c17a0:  add    $0x3f,%eax           ; round up to 64  (CoreV1 64-partition granule)
8c17a3:  and    $0xffffffc0,%eax     ; mask to %64
8c17a6:  test   %ebp,%ebp            ; lowerPartition == 0 ?
8c17a8:  jne    8c183e               ;   else → assert "CoreV1 PSUM accesses must start at partition 0"
```

The assert literal `0 == lowerPartition && "CoreV1 PSUM accesses must start at partition 0"` (`.rodata 0x1ca0610`) is the sole `CoreV1` string in the binary, and it is a *live* assert: arch > 19 takes the gen2 Sunda `%32`-align sibling path; arch ≤ 19 takes this CoreV1 `%64` / partition-0 path. So `cmpl $0x13`(=19) is the documented gen2 lower bound, and the ≤ 19 arm is exactly the Inferentia/Tonga/gen1 (arch = 10) code path. [CONFIRMED — gate + string-pair decoded]

### What CoreV1 does NOT have

| Facility | CoreV1 status | Evidence | Confidence |
|---|---|---|---|
| `CoreV1GenImpl` codegen class | ABSENT | nm: zero hits | CONFIRMED |
| `core_v1::enum_variant_string_opcode` ISA table | ABSENT | only core_v2/v3/v4 tables | CONFIRMED |
| `core_v1::` ISA wire-encoder namespace | ABSENT | no `core_v1::` dynsym | CONFIRMED |
| `Inferentia*` HW engine model | PRESENT | nm: full 9-class hierarchy | CONFIRMED |
| `_inferentia_arch_model` singleton | PRESENT | `0x3e05800` `.bss` | CONFIRMED |
| Live `getPSUMPartitionRange` arch≤19 branch | PRESENT (live) | gate `cmpl $0x13` @ `0x8c175b` | CONFIRMED |

> **VERDICT (CoreV1) — INFERRED-structural.** "Deprecated" is a structural inference: the binary shows a complete HW model and a live analysis rule but no codegen `GenImpl` and no ISA encoder. There is no string that *says* "deprecated"; the status is read from the shape (modelled + analysis-live, codegen-dead). The codegen floor is arch 20; the lowest arch with executable *analysis* behavior is arch 10.

---

## CoreV5 (arch 50) — a pure forward-declaration stub

### Purpose

CoreV5 is the other extreme: a reserved enum ordinal with no hardware model, no ISA, no codegen — only dormant feature-gates and one CLI token. This section enumerates every `core_v5` / `CoreV5` byte in the binary and shows each is a forward reference, never an executable encoder, so a reader knows gen5 is scaffolding, not a target.

### No Board: getArchModel asserts on gen5

`getArchModel` (the codename→`Board` dispatch, `libBIR 0x478f90`) is a linear chain of `std::string::compare` calls over the device-alias roster, terminating in `__assert_fail` for any unrecognized name. The roster decodes straight from `.rodata` (`0x70c6f4`, null-token):

```text
# libBIR .rodata 0x70c6f4 — device-alias roster (null-separated)
tonga · inf1 · sunda · trainium · trn1 · inf2 · cayman · gen3 · core_v4 · 0 && "Unknown architecture" · core_v5
```

Critically, `core_v5` sits **after** the `0 && "Unknown architecture"` assert literal — it has **no** `compare` arm and **no** `Board`. `getArchModel("core_v5")` falls through ten `compare` calls to `__assert_fail` @ `0x479093`. There is no instantiable gen5 device. [CONFIRMED — roster decode + assert xref]

### The seven gen5 anchors are all forward gates

The six `core_v5` strings plus the one `CoreV5` string are exhaustively accounted for, and not one is an encoder:

| # | String (`.rodata`) | Role | Gate shape | Confidence |
|---|---|---|---|---|
| 1 | `…getArchLevel() >= ArchLevel::core_v5` (`0x1d43e18`) + `only core v5+ has special semaphore for HW DGE` (`0x1d43e20`) | `AllocSemaphores::run` capability reserved FOR gen5 | `>=` | CONFIRMED |
| 2 | `ArchLevl < ArchLevel::core_v5 ? …Activation : …DVE` (`0x1d4f8c8`) | `lower_act` engine-type selector, "below gen5" | `<` | CONFIRMED |
| 3 | `ModuleArchLevel < ArchLevel::core_v5` (`0x1d69508`) | CoreV2GenImpl module-level guard | `<` | CONFIRMED |
| 4 | `BatchNormGradAccumulate not supported in core_v5` (`0x1d69530`) | `CoreV2GenImpl::visitInstBNGradients` forward-gate | `<` (`cmpl $0x31`=49) | CONFIRMED |
| 5 | `ActivationReadAccumulator not supported in core_v5` (`0x1d6aaab`) | `lower_act` companion forward-gate | `<` | CONFIRMED |
| 6 | `core_v5` (`0x1dca083`) | `walrus_driver` CLI optlevel/arch-name token | (parser) | CONFIRMED |
| 7 | `CoreV5 cannot support DGE with compute op yet` (`0x1d6d300`) | `CoreV2GenImpl::generateDynamicDMA` gen2-DMA placeholder | (guard) | CONFIRMED |

Anchor 4 is the proof these gates are *dormant*: `CoreV2GenImpl::visitInstBNGradients` compiles `< ArchLevel::core_v5` to a `cmpl $0x31,0x258(%r12)` / `jg` pair (`0x31` = 49), so the BatchNormGradAccumulate path is *gated off* for every current arch (20/30/40, all ≤ 49) and would only flip on at arch ≥ 50:

```asm
; CoreV2GenImpl::visitInstBNGradients forward-gate  (libwalrus 0x1242149)
1242149:  cmpl   $0x31,0x258(%r12)    ; arch > 49 ?  (0x31 = 49 = highest pre-gen5)
1242152:  jg     1242534              ;   → BNGradAccumulate path (never taken on 20/30/40)
```

One further reserved slot lives in the CLI: the `RMW-alignment-target-arch` flag enumerates `tonga, sunda, gen3, core4, gen5` (verbatim help text: *"Any arch including and beyond set value will have RMW alignment triggered. Availe value tonga, sunda,gen3,core4,gen5"* — the `Availe` typo is in the binary). `gen5` is an accepted flag value with no codegen behind it — a reserved option slot. [CONFIRMED — null-token decode]

### What CoreV5 does NOT have

| Facility | CoreV5 status | Evidence | Confidence |
|---|---|---|---|
| `CoreV5GenImpl` codegen class / vtable / RTTI | ABSENT | nm: zero hits | CONFIRMED |
| `core_v5::enum_variant_string_opcode` ISA table | ABSENT | only core_v2/v3/v4 | CONFIRMED |
| `Gen5*` / `CoreV5*` HW engine classes | ABSENT | nm: zero hits | CONFIRMED |
| `_core_v5_arch_model` singleton | ABSENT | only 4 `*_arch_model` in `.bss` | CONFIRMED |
| `Board` in `getArchModel` | ABSENT | falls to `__assert_fail` @ `0x479093` | CONFIRMED |
| `is_core_v5_or_newer` predicate | ABSENT | no symbol; see note below | STRONG |

> **CORRECTION — `is_core_v3_or_newer` is an assert-message substring, not a recovered symbol.** It is tempting to cite `is_core_v3_or_newer` as the only generation predicate. It is **not** an exported function symbol in either binary; it survives only as text inside an assert literal (`is_core_v3_or_newer || (dtype == Dtype::float32)`). There is correspondingly no `is_core_v1`/`is_core_v5` *string* either. The accurate claim is: the binary contains exactly one generation-predicate name, as an assert message, and it is the v3 one. [STRONG — string census; the symbol-level claim is demoted to INFERRED]

> **VERDICT (CoreV5) — INFERRED-structural.** "Forward-declaration stub" is a structural inference from the shape: a reserved ordinal (50, no `Board`), ~7 dormant arch gates, one CLI token, one RMW-flag slot — and nothing executable. The gen5 surface is pre-wired as a set of dormant `arch >=/< 50` branches; the authors left feature-flag scaffolding but no implementation. A future `CoreV5GenImpl` would need to extend the CoreV4▸CoreV3▸CoreV2 single-inheritance ladder, add a `core_v5::` ISA table, build a gen5 `Board` (removing the assert), and flip the reserved gates. [INFERRED from the gate inventory]

---

## Putting it together: the live-vs-vestigial table

The orientation summary. Every row is the binary status of one generation across the three layers that matter — does it have a `Board` (geometry), does it run analysis code, is it a codegen target.

| Gen | `ArchLevel` | Codename(s) | `Board` / geometry | Live analysis branch | Codegen `GenImpl` | Class of vestigial | Confidence |
|---|---|---|---|---|---|---|---|
| 1 | 10 | inferentia / tonga / inf1 | PRESENT (`_inferentia_arch_model`) | PRESENT (`getPSUMPartitionRange` ≤19) | ABSENT | modelled-but-deprecated | CONFIRMED (status row INFERRED-structural) |
| 2 | 20 | sunda / trainium / trn1 / inf2 | PRESENT | live (floor) | `CoreV2GenImpl` | **live target** | CONFIRMED |
| 3 | 30 | cayman / gen3 | PRESENT | live | `CoreV3GenImpl` | **live target** | CONFIRMED |
| 4 | 40 | core_v4 | PRESENT | live | `CoreV4GenImpl` | **live target** | CONFIRMED |
| 5 | 50 | core_v5 / gen5 | ABSENT (asserts) | ABSENT | ABSENT | reserved-but-unbuilt | CONFIRMED (status row INFERRED-structural) |

> **GOTCHA — three different "floors."** The codegen floor is **20** (the lowest arch `Codegen::codegen` will instantiate a `Gen` for). The analysis floor is **10** (the lowest arch with an executable behavioral branch — the CoreV1 PSUM rule). The enum floor is **10** as well (`ArchLevel2string` names ordinal 10). The reserved ceiling is **50** (named in the enum, gated for in dormant branches, but with no `Board` and no codegen). A reimplementer who only implements the codegen floor (20) is correct for codegen; one who wants byte-parity with the dependency analyzer must also model the arch-10 PSUM branch.

---

## Cross-References

- [The Arch Object Model](arch-object-model.md) — `getArchModel → Board/Device/Core`, the `Inferentia*` ctors, and the four `*_arch_model` singletons whose count (4, not 5) is the CoreV5 evidence here.
- [Codename Taxonomy](codename-taxonomy.md) — the codename↔generation↔CoreVN↔device bijection; this page cites the in-binary device-alias roster (`tonga/inf1/…/core_v4`, then `core_v5`) as the table behind that mapping.
- [Per-Generation Hardware-Constant Matrix](hardware-constant-matrix.md) — the per-arch immediates, including the gen1 half-width Inferentia values that prove CoreV1's HW model is real, not a stub.
- [Methodology & the Confidence Model](../methodology.md) — why a recovered assert literal and a byte-pinned `cmp` constant are both binary evidence, and why a "deprecated/stub" status is an INFERRED-structural conclusion rather than a CONFIRMED string.
