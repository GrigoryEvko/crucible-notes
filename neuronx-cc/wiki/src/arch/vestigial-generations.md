# Vestigial Generations — CoreV1 (Inferentia) & CoreV5

> *All symbols and addresses on this page are read from the **cp310** wheel of `neuronx_cc` 2.24.5133.0+58f8de22 (`libwalrus.so` and `libBIR.so`); cp310/cp311/cp312 carry the same strings and the same dispatch, so re-confirm only if a page says otherwise — see [Version Provenance](../reference/versions.md). For both binaries `.text`/`.rodata` VMA equals file offset. Treat every address as version-pinned.*

## Abstract

The arch model carries five generations on its ordinal ladder — `10/20/30/40/50` = Inferentia / Sunda / gen3 / CoreV4 / CoreV5 ([`bir::ArchLevel2string` @ `libBIR 0x479490`](arch-object-model.md)) — but only **three** are live codegen targets. The backend `Codegen::codegen` arch-select switch (`libwalrus 0x11d2c50`) dispatches on exactly `20/30/40` and throws on anything else; there is no `CoreV1Gen`/`CoreV5Gen` class, vtable, or RTTI; and there is no `core_v1::`/`core_v5::` ISA encoder namespace. A reader scanning the ladder must not mistake a *modelled* generation for a *supported* one. This page documents which constructors, RTTI tables, dispatch arms, and opcode tables include versus omit each generation, so the codegen floor — **arch 20 (gen2/Sunda)** — is unambiguous.

The two dead generations are **not symmetric**, and that asymmetry is the page's central finding. **CoreV1 (arch 10)** is *fully-modelled but deprecated*: it has a complete `Inferentia` hardware-model engine hierarchy (`InferentiaAct/Dve/Pe/Pool/Psumbuf/Statebuf` + `Inferentia` `Board/Core/Device` + the `_inferentia_arch_model` singleton), a live analysis-pass code path that still fires for it, and a Cython mid-end target package (`penguin/targets/tonga/`) — everything *except* a `walrus` backend `GenImpl`. **CoreV5 (arch 50)** is a *pure forward-declaration stub*: a reserved enum ordinal with no `Board` (`getArchModel` asserts `"Unknown architecture"`), zero hardware-engine classes, zero ISA tables, and nothing executable — only a scatter of dormant `< ArchLevel::core_v5` / `>= ArchLevel::core_v5` feature gates and one CLI token. CoreV1 = modelled-but-deprecated; CoreV5 = reserved-but-unbuilt.

The "deprecated/stub" *status* is a **structural inference**, and this page is explicit about that. No string says "CoreV1 is deprecated" or "CoreV5 is unimplemented"; the verdict is read off the *presence and absence pattern* — which constructors run, which RTTI typeinfos exist, which dispatch arms compare which constants, which opcode tables are emitted. Each finding below is tagged with the include/omit evidence and its confidence; the bare presence/absence facts are CONFIRMED, and the word "vestigial" is the INFERRED label placed over them.

For a reader of this page the contract is:

- **The codegen floor** — `Codegen::codegen` accepts `20/30/40` only; both `10` and `50` fall through to a throw.
- **The two asymmetric absences** — CoreV1 has a full HW model + analysis path + mid-end target but no `GenImpl`; CoreV5 has *nothing* but reserved gates and an ordinal.
- **The evidence pattern** — per generation, the exact set of ctors / RTTI / dispatch arms / opcode tables that include versus omit it, so a reimplementer keys their target list off `{20,30,40}` and treats `{10,50}` as analysis-only / reserved.

| | |
|---|---|
| **Codegen arch-select** | `Codegen::codegen(bir::Module&)` @ `0x11d2c50`; switch @ `0x11d2cc1` — `cmp $0x14/0x1e/0x28`; else throw |
| **Live codegen targets** | arch **20** (CoreV2/Sunda) · **30** (CoreV3/gen3) · **40** (CoreV4) |
| **Analysis-only floor** | arch **10** (CoreV1/Inferentia) — gated `cmp $0x13` (=19) in `getPSUMPartitionRange` |
| **Reserved stub** | arch **50** (CoreV5) — no `Board`, `getArchModel` asserts `"Unknown architecture"` |
| **GenImpl RTTI set** | `_ZTI…CoreV{2,3,4}GenImpl` only; **no** `CoreV1GenImpl`/`CoreV5GenImpl` |
| **Opcode-name tables** | `core_v2` @ `0x127aea0` · `core_v3` @ `0x1369a40` · `core_v4` @ `0x143fd80` — no `core_v1`/`core_v5` |
| **Gen1 HW model** | `Inferentia{Act,Dve,Pe,Pool,Psumbuf,Statebuf,Board,Core,Device}` + `_inferentia_arch_model` — PRESENT |
| **Gen5 HW model** | none — no `CoreV5*`/`Gen5*` engine class, no `_core_v5_arch_model` |
| **Generation predicate** | `is_core_v3_or_newer` (libBIR) only — no `is_core_v1`/`is_core_v5` |
| **String census (libwalrus)** | `core_v5` ×6 · `CoreV5` ×1 · `CoreV1` ×1 · `core_v1` ×0 |

---

## The codegen floor

### Purpose

There is exactly one place that decides "given a module's `ArchLevel`, which `Gen` backend runs": the `Codegen::codegen` arch-select switch. Whatever set of constants that switch compares against *is* the set of supported codegen targets — everything else throws before a single instruction is emitted. This is the test that demotes CoreV1 and CoreV5 from "on the ordinal ladder" to "not a codegen target."

### Entry Point

```text
Codegen::codegen(bir::Module&)        ── libwalrus 0x11d2c50
  └─ arch-select @ 0x11d2cc1          ── cmp %ebp against ArchLevel ordinal
       ├─ cmp $0x14 → CoreV2Gen ctor   (0x11d3398 — CoreV2GenC2)   arch 20
       ├─ cmp $0x1e → CoreV3Gen ctor   (0x11d2e50 — CoreV3GenC2)   arch 30
       ├─ cmp $0x28 → CoreV4Gen ctor                                arch 40
       └─ else      → build message via bir::ArchLevel2string, throw
```

### Algorithm

The switch is a three-arm `cmp/je` chain over the `ArchLevel` ordinal held in `%ebp`. Only `20`, `30`, and `40` have arms; `10` (CoreV1) and `50` (CoreV5) — like any unknown value — fall through to the throw. Confirmed at `0x11d2cc1`:

```c
// Codegen::codegen(bir::Module&)        libwalrus 0x11d2c50
function codegen(Module *m):
    int arch = m->archLevel;                    // %ebp
    if (arch == 0x14) return run_CoreV2Gen(m);  // 0x11d2cc1  cmp $0x14 ; arch 20
    if (arch == 0x1e) return run_CoreV3Gen(m);  // 0x11d2cca  cmp $0x1e ; arch 30
    if (arch == 0x28) return run_CoreV4Gen(m);  // 0x11d2cd3  cmp $0x28 ; arch 40
    // no arm for 0x0a (10/CoreV1), no arm for 0x32 (50/CoreV5)
    string msg = "Codegen: unknown arch " + ArchLevel2string(arch);  // fmt str @0x1c83ec6
    throw std::runtime_error(msg);                  // __cxa_allocate_exception(0x10)
```

> **CORRECTION (D-M15) —** the fall-through throw is a **`std::runtime_error`** carrying the `"Codegen: unknown arch "` format string (`@0x1c83ec6`) concatenated with `ArchLevel2string(arch)` (`__cxa_throw` / `__cxa_allocate_exception(0x10u)` in the decompiled body), **not** `boost::throw_exception(std::out_of_range)` as an earlier reading recorded. The *switch restriction* — `20/30/40` only, everything else throws — is unchanged and CONFIRMED; only the exception type is corrected. The format-string address is the same `0x1c83ec6` cited by [1.02](codename-taxonomy.md); the two pages agree on both the message and the type. A reimplementer's dispatcher should reject arch `10`/`50` with a hard error, not silently codegen them.

### Function Map

| Symbol | Binary | Address | Role | Confidence |
|---|---|---|---|---|
| `Codegen::codegen(bir::Module&)` | libwalrus | `0x11d2c50` | Arch-select dispatch into the `Gen` backends | CONFIRMED |
| arch-select chain | libwalrus | `0x11d2cc1` | `cmp $0x14/$0x1e/$0x28`, else throw | CONFIRMED |
| `bir::ArchLevel2string` | libBIR | `0x479490` | Ordinal→codename; `10/20/30/40/50` → `inferentia/sunda/gen3/core_v4/core_v5` | CONFIRMED |

### Considerations

The Verifier's mirror path corroborates the same three-arch world from a second construction. The instruction-visitor init builds a `std::variant<monostate, CoreV2Gen, CoreV3Gen, CoreV4Gen>` — the variant *type list itself* excludes `CoreV1Gen`/`CoreV5Gen` (read from the mangled gen-vtable symbol). Two independent selectors (the `codegen` switch and the variant type list) name the same set `{20,30,40}`; the exclusion of `10` and `50` is therefore not an accident of one switch but a property of the backend's whole type universe. (STRONG — the variant type list is symbol-confirmed; the per-arm body wiring was not re-disassembled here.)

---

## CoreV1 (Inferentia / Tonga / gen1 / arch 10) — modelled but deprecated

### Purpose

CoreV1 is the case a reader is most likely to misjudge, because its absence is *partial*. The codename "Inferentia" carries a complete hardware model and a live analysis rule — so symbol-grepping for "Inferentia" returns a rich, healthy-looking class family. The thing that is missing is narrow and specific: the `walrus` backend `GenImpl`. This section pins exactly what is present and what is absent, so the "no codegen, but fully modelled" status is read off evidence rather than assumed from the codename's prominence.

### What is present

```text
Inferentia HW model (libwalrus)          ── the Board→Device→Core→engine tree
  ├─ InferentiaBoard / InferentiaDevice / InferentiaCore
  ├─ InferentiaPe / InferentiaPool / InferentiaAct
  ├─ InferentiaDve / InferentiaPsumbuf / InferentiaStatebuf
  └─ _inferentia_arch_model              ── static singleton (the gen1 Board)
Live analysis path (libwalrus)
  └─ AntiDependencyAnalyzer::getPSUMPartitionRange  0x8c1700  ── gates on arch ≤ 19
Mid-end target (Cython)
  └─ penguin/targets/tonga/              ── CodeGenFlow / ISAMapper / Dataflow / APIndex / passes
PWP profile column
  └─ tonga_id                            ── legacy gen1 opcode-id remap
```

- **Full gen1 HW model — PRESENT.** `InferentiaAct/Dve/Pe/Pool/Psumbuf/Statebuf` and `Inferentia` `Board/Core/Device` engine classes exist, with the static `_inferentia_arch_model` singleton — structurally parallel to `Sunda`(gen2)/`Cayman`(gen3)/`CoreV4`(gen4). RTTI typeinfo exists for the `InferentiaPe/Act/Dve/Pool` engine classes. This is the scheduler/latency architectural-profile model (the `Board → Device → Core → engine` tree documented in [the arch object model](arch-object-model.md)), and gen1 is a first-class member of it. [CONFIRMED — `functions`/`strings`/`rtti` census + the `InferentiaCore` ctor @ `0x1734720`]
- **A live analysis rule — PRESENT.** `AntiDependencyAnalyzer::getPSUMPartitionRange` (`0x8c1700`) branches on `cmp dword [r12+0xAC],0x13` (=19) at `0x8c175b`: `arch > 19` takes the Sunda `%32`-aligned PSUM path, `arch ≤ 19` takes the CoreV1 partition-0 path. The `arch ≤ 19` arm is exactly the gen1 (arch 10) code path, and it ends in the assert `"0 == lowerPartition && \"CoreV1 PSUM accesses must start at partition 0\""` — paired with its gen2 sibling `"0 == lowerPartition % 32 && \"Sunda PSUM accesses must start at a %32 partition\""`. So CoreV1 still has *executable* compiler behavior; it is the analysis-layer floor. [CONFIRMED — disasm gate + both assert strings]
- **A mid-end target package — PRESENT.** `penguin/targets/tonga/` ships as a full Cython package (`CodeGenFlow`, `ISAMapper`, `Dataflow`, `DFNLayout`, `APIndex`, and a `tonga__passes__*` family). The legacy gen1 front/mid-end target is richer than CoreV5's nothing, but it is a self-contained codename target, **not** a `walrus`-backend `CoreV1GenImpl`. [CONFIRMED — Cython module tree]
- **A PWP profile column — PRESENT.** Per-function PWP profiles carry a `tonga_id` column (the legacy Tonga opcode-id remap; `tonga_id=0` = unsupported-on-Tonga). And `InstTongaReduceMacroSymbolic` is a BIR *instruction* opcode (`lower_TongaReduceMacroSymbolic @ 0x11b42e0`) — a codename appearing in an op name, not a CoreV1 codegen class. [CONFIRMED]

### What is absent

- **No `CoreV1GenImpl` / `InferentiaGen` / `TongaGen` codegen class.** The `rtti` census carries `_ZTI/_ZTS/_ZTV` for `CoreV2GenImpl`, `CoreV3GenImpl`, `CoreV4GenImpl` only; zero `CoreV1GenImpl` hits. Absence of RTTI = no codegen class. [CONFIRMED — `rtti.json`]
- **No `core_v1::` ISA encoder namespace** anywhere in the dynamic symbol table. [CONFIRMED]
- **No `core_v1::enum_variant_string_opcode` opcode-name table.** The opcode→name family is `core_v2` @ `0x127aea0` / `core_v3` @ `0x1369a40` / `core_v4` @ `0x143fd80` only. [CONFIRMED — disasm function entries]
- **No `cmp $0x0a` (arch 10) arm in `Codegen::codegen`** — gen1 modules hit the throw (§ The codegen floor). [CONFIRMED]
- **`CoreV1` appears as a string exactly once**, and only as the capitalized PSUM assert literal; there is no lowercase `core_v1` namespace/string. [CONFIRMED — census `CoreV1`=1, `core_v1`=0]

### Verdict (CoreV1)

A fully-modelled but deprecated gen1: HW model + analysis rules + mid-end target PRESENT; `walrus` codegen ABSENT and unreachable. The **codegen** floor is arch **20** (gen2); the floor that still has executable **analysis** behavior is arch **10** (Tonga, gated `cmp $0x13`=19). The two floors differ by layer. [INFERRED — the "deprecated" label is placed over the CONFIRMED presence/absence pattern; no string declares deprecation.]

---

## CoreV5 (gen5 / arch 50) — a forward-declaration stub

### Purpose

CoreV5 is the opposite failure mode: a reader might expect parity with CoreV1 ("the other vestigial gen") and look for a gen5 HW model. There is none. CoreV5 exists *only* as a reserved ordinal and a set of dormant feature gates that fire "off" for every current arch. This section pins the stub's full surface so a reimplementer knows the entire gen5 footprint is scaffolding, with nothing behind it.

### What is reserved (and nothing more)

The whole CoreV5 surface is six `core_v5` strings, one `CoreV5` string, and one CLI flag slot — every one of them a gate, an assert, or a token, never an executable encoder.

| Anchor | Form | Consumer / role | Confidence |
|---|---|---|---|
| `< ArchLevel::core_v5` (`lower_act.cpp:313`) | forward gate | lowering-time engine-type selector (`LowerPWP`) | CONFIRMED |
| `ModuleArchLevel < ArchLevel::core_v5` (`CoreV2GenImpl.cpp:1572`) | forward gate | module-level arch guard | CONFIRMED |
| `BatchNormGradAccumulate not supported in core_v5` | forward gate | `CoreV2GenImpl::visitInstBNGradients` @ `0x1241b10`; `cmp …+0x258,$0x31` (=49) @ `0x1242149` | CONFIRMED |
| `ActivationReadAccumulator not supported in core_v5` | forward gate | `CoreV2GenImpl::visitInstActivation` | CONFIRMED |
| `…getArchLevel() >= ArchLevel::core_v5` + `"only core v5+ has special semaphore for HW DGE"` | reserved capability | semaphore-allocation path (`AllocSemaphores::run` @ `0x627730`) | STRONG |
| `CoreV5 cannot support DGE with compute op yet` | placeholder guard | `CoreV2GenImpl::generateDynamicDMA` @ `0x1276b10` | CONFIRMED |
| `core_v5` | CLI token | `walrus_driver` optlevel/arch-name parser roster `{inferentia, sunda, gen3, core_v5}` | CONFIRMED |
| `RMW-alignment-target-arch` enum slot `gen5` | reserved flag value | CLI flag roster `{tonga, sunda, gen3, core4, gen5}` | CONFIRMED |

> **NOTE —** the four `< ArchLevel::core_v5` gates compile to a `cmp …,$0x31` (=49) followed by a `jle`-style branch — i.e. "this feature is disabled until gen5." With current arches `20/30/40` all `≤ 49`, every one of these gates fires the *disabled* arm. The one `>= ArchLevel::core_v5` gate (the HW-DGE special semaphore) is the mirror: a capability reserved *for* gen5, never taken today. The compiler authors left feature-flag scaffolding but no implementation behind it.

### What is absent

- **No `CoreV5GenImpl` class, vtable, or RTTI.** Zero hits in the `rtti` census; the only `CoreV5` symbol-table hits are the assert string itself, not a class. [CONFIRMED]
- **No `core_v5::` ISA namespace, no `enum_variant_string_opcode`, no HW engine class** — no `Gen5*`/`CoreV5*` `Act/Dve/Pe/Pool`, no `_core_v5_arch_model` singleton. [CONFIRMED]
- **No `Board` for arch 50.** `getArchModel` (`libwalrus 0x17344c0` / `libBIR 0x478f90`) compares the codename against `{tonga, inferentia, inf1, sunda, trainium, trn1, inf2, cayman, gen3, core_v4}` and falls through to `__assert_fail("0 && \"Unknown architecture\"")` — `core_v5` is **not** in the alias set, so there is no instantiable gen5 device. [CONFIRMED — disasm string compares, both binaries]
- **No `is_core_v5_or_newer` predicate.** `is_core_v3_or_newer` (libBIR) is the only generation predicate; there is no `is_core_v1`/`is_core_v5`. [CONFIRMED]

### What a future CoreV5GenImpl would need

The gen5 surface is *pre-wired* as a set of dormant branches; a real gen5 would flip them and fill the holes. This is the reserved scaffolding, read off the gate inventory: [INFERRED from the absence pattern]

1. An `ArchLevel 50` arm added to the `Codegen::codegen` switch (today it throws).
2. A `CoreV5Gen`→`CoreV5GenImpl` class extending `CoreV4GenImpl` (continuing the `CoreV4 ▸ CoreV3 ▸ CoreV2GenImpl` single-inheritance ladder confirmed in the `_ZTI` `__si` chain), patching only the slots that change at gen5.
3. A `core_v5::enum_variant_string_opcode` table + `core_v5::` ISA wire structs.
4. `CoreV5`/`Gen5` HW engine classes + a `_core_v5_arch_model` `Board` for `getArchModel`, removing the `"Unknown architecture"` assert.
5. Flipping the reserved gates: enable `BatchNormGradAccumulate` / `ActivationReadAccumulator` (today `< core_v5`), wire the HW-DGE special semaphore (today `>= core_v5`), and resolve the `generateDynamicDMA` `"CoreV5 cannot support DGE with compute op yet"` placeholder.

### Verdict (CoreV5)

A pure forward-declaration stub: a reserved ordinal (50, no `Board`, asserts `"Unknown architecture"`), the gates above, one CLI token, one RMW-flag slot — and nothing executable. [INFERRED — the "stub" label sits over the CONFIRMED total absence of any gen5 HW class / ISA / codegen.]

---

## The include/omit matrix

The structural test in one grid. A generation is a live codegen target iff it appears in the `Codegen::codegen` switch, the `GenImpl` RTTI set, and the opcode-name tables. CoreV1 and CoreV5 each fail that test, but for opposite reasons — read down their columns.

| Evidence axis | gen1 / arch 10 | gen2 / arch 20 | gen3 / arch 30 | gen4 / arch 40 | gen5 / arch 50 | Source |
|---|---|---|---|---|---|---|
| `ArchLevel` ordinal | 10 | 20 | 30 | 40 | 50 | `ArchLevel2string` @ `0x479490` |
| `getArchModel` `Board` | yes (`_inferentia_arch_model`) | yes | yes | yes | **no → assert** | `getArchModel` @ `0x17344c0` |
| HW engine classes | **`Inferentia*` (full)** | `Sunda*` | `Cayman*` | `CoreV4*` | **none** | ctor + rtti census |
| `Codegen::codegen` arm | **no** | `cmp $0x14` | `cmp $0x1e` | `cmp $0x28` | **no** | `0x11d2cc1` |
| `*GenImpl` RTTI | **no** | `CoreV2GenImpl` | `CoreV3GenImpl` | `CoreV4GenImpl` | **no** | `rtti.json` |
| `core_vN::enum_variant_string_opcode` | **no** | `0x127aea0` | `0x1369a40` | `0x143fd80` | **no** | disasm entries |
| Live analysis path | **yes (`≤19` PSUM rule)** | yes | yes | yes | **no** | `getPSUMPartitionRange` @ `0x8c1700` |
| Mid-end target pkg | **`targets/tonga/`** | yes | yes | yes | **no** | Cython module tree |
| Generation predicate | (none) | (none) | `is_core_v3_or_newer` | (covered by ≥v3) | **no** | libBIR `functions.json` |
| **Status** | **modelled, deprecated** | live | live | live | **reserved stub** | INFERRED |

> **QUIRK —** the two dead columns are mirror images. CoreV1 is full *everywhere except* the four codegen rows (switch arm, `GenImpl` RTTI, opcode table, ISA namespace); CoreV5 is empty *everywhere except* the reserved-gate rows. A reimplementer who keys their target list off "has an `ArchLevel` ordinal" will wrongly admit both; off "has a `Codegen::codegen` arm and a `*GenImpl` RTTI" will correctly admit only `{20,30,40}`.

---

## Cross-References

- [The arch Object Model](arch-object-model.md) — `getArchModel` → `Board/Device/Core`, the per-arch ctors (`InferentiaCore` is the gen1 HW model this page calls "present"), and the `"Unknown architecture"` assert that gen5 falls through to. (1.01)
- [Codename Taxonomy](codename-taxonomy.md) — the full codename ↔ `ArchLevel` ↔ marketed-device decode (`tonga`/`inf1`/`inferentia` = arch 10; `core_v5`/`gen5` = arch 50). (1.02)
- [Per-Generation Hardware-Constant Matrix](hardware-constant-matrix.md) — the gen1 (Inferentia) half-width constant set that the present-but-deprecated CoreV1 HW model carries, and why gen5 has no column. (1.04)
- [Methodology & the Confidence Model](../methodology.md) — the four-tier ladder; why a "deprecated/stub" *status* is tagged INFERRED while its underlying presence/absence facts are CONFIRMED.
