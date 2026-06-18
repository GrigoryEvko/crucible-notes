# with_ln vs trainium PWP Target Selection

> *All symbols, addresses, and JSON content on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (`neuronxcc/starfish/lib/libwalrus.so`, `neuronxcc/pwp/`, cp310; cp311/cp312 ship byte-identical PWP JSON and blobs). Treat every address as version-pinned.*

## Abstract

The wheel ships **two** PWP (Piece-Wise Polynomial) activation-table catalogs under `neuronxcc/pwp/`: `pwp_bin_trainium` (21 activation-function sets, 65 files) and `pwp_bin_with_ln` (14 sets, 44 files). Each is a self-contained directory holding one `act_info.json` *menu*, one blank `version.json`, and three blobs per set (`<set>_bkt.bin`, `<set>_ctrl.bin`, `<set>.json`). These two directories are the entire PWP-target design space — there is no third, and no per-architecture split. This page documents how the two catalogs differ and **how exactly one is chosen** at compile time.

The surprising answer to "what selects the target" is: *nothing inside the binary*. There is no `getPwpDir(target)`, no `"pwp_bin_" + arch` concatenation, no `cl::opt` default path, and no LayerNorm-fused codegen branch anywhere in `libwalrus`, `walrus_driver`, or the shipped Python. The choice is made entirely by the **value** of one LLVM command-line string option — `--act-root-json <absolute path>` — which the upstream Python compile-job orchestration computes and hands to `walrus_driver`. The directory the path points at *is* the selection. Both directory names (`with_ln`, `pwp_bin`) appear in exactly one shipped file, the `dist-info/RECORD` manifest that merely lists them; they occur in zero `.so`, zero executable, and zero `.py`.

The split is a **training-vs-inference profile** axis, orthogonal to the ISA/arch axis. `pwp_bin_trainium` is the full production roster (forward + backward heroes); `pwp_bin_with_ln` is an inference-trimmed subset whose 22-function universe is a **strict subset** of trainium's 36. "with_ln" is not a device, not an arch codename, and not an `ArchLevel` — it is a profile descriptor ("with LayerNorm support, inference-trimmed"). The DVE engine ships the same idea packaged differently (named `tables[]` profiles inside one `dve_info.json` per gen), confirming the read.

For reimplementation, the contract is:

- The **selection mechanism**: a single `cl::opt<std::string>` (`actRootJson`), defaulted empty, with an absolute-path guard; the path's directory is the target.
- The **catalog delta**: which functions `with_ln` drops relative to `trainium`, proven at the function-universe level (not the set-name level — those diverge).
- The **gen-invariant encoding**: why one PWP target serves every arch, so the with_ln/trainium choice is a profile axis and not a device axis.

| | |
|---|---|
| **Selector option** | `cl::opt<std::string> actRootJson` @ `libwalrus` .bss `0x3df8a60` (`nm -DC`: `actRootJson[abi:cxx11]`) |
| **Flag / desc / default** | `"act-root-json"` @ `0x1dc2e21` · `"Activation root json file"` @ `0x1dc2e07` · `cl::init("")` @ `0x1dc026e` |
| **Sibling (DVE)** | `dveRootJson[abi:cxx11]` @ `0x3e03b20` · flag `"dve-root-json"` |
| **lower_act consumer** | `LowerPWPImpl::LowerPWPImpl` @ `0x115d130` → `fillAllActInfos` @ `0x115af90` |
| **NEFF consumer** | `NeffFileWriter::updateFromActJsonFile(boost::filesystem::path)` @ `0x1542db0` |
| **trainium catalog** | 21 sets · 65 files · function universe = 36 |
| **with_ln catalog** | 14 sets · 44 files · function universe = 22 (⊂ trainium's 36) |
| **PWP axis** | profile (train vs inference) — **orthogonal** to arch `{tonga, sunda, gen3, core4, gen5}` |

---

## The Selection Mechanism

### Purpose

A `walrus_driver` invocation must be told which activation-table catalog to embed in the NEFF. The mechanism is deliberately *dumb*: the catalog is identified by a filesystem path to its `act_info.json`, passed as a command-line string. All target identity flows from that one argument — the binary never reasons about "trainium" or "with_ln" as named targets.

### Entry Point

```text
walrus_driver <bir.json> --neff-output-filename <out> [--act-root-json <abs path>] [--dve-root-json <abs path>] ...
  └─ libwalrus: cl::opt<std::string> actRootJson         ── parses the string, no default
       ├─ LowerPWPImpl::LowerPWPImpl  (0x115d130)         ── lower_act pass: absolute-path guard + parse
       │    └─ fillAllActInfos        (0x115af90)         ── reads act_func_sets / pwp_file_keys
       └─ NeffFileWriter::updateFromActJsonFile (0x1542db0) ── loads json, adds referenced blobs to NEFF BOM
            (called from NeffFileWriter::writeFile @ 0x1541040)
```

The flag is registered inside `libwalrus`, so `walrus_driver` exposes it without a literal `"--act-root-json"` string of its own; only `walrus_bugpoint_driver` re-exposes the bare token `"act-root-json"` (and `"dve-root-json"`) directly — both confirmed present in that ELF, both absent from `walrus_driver`.

### Algorithm

The option registration is a stock LLVM `cl::opt<std::string>` construction in the static-init that builds the lower_act `PassOptions` (objdump @ `0x7c5e36`–`0x7c5e74`):

```c
// libwalrus static-init for lower_act PassOptions  (0x7c5e36..0x7c5e74)
actRootJson = cl::opt<std::string>(             // global @ .bss 0x3df8a60
    /* name */ "act-root-json",                 // 0x1dc2e21
    cl::desc("Activation root json file"),      // 0x1dc2e07
    cl::init("")                                // 0x1dc026e  — DEFAULT IS EMPTY
);
// exact parallel: dveRootJson @ 0x3e03b20  <-  "dve-root-json"
```

> **QUIRK —** the default is the empty string, not a fallback path. Omitting `--act-root-json` does **not** silently select trainium; it makes the lower_act pass abort. The default catalog is chosen *upstream*, by the Python driver that builds the `walrus_driver` command line — not by any in-binary default. A reimplementer who wires a default of `pwp_bin_trainium` into the option has put the policy in the wrong layer.

The consumer enforces that the supplied value is an absolute path to an existing JSON, then drives everything from the JSON's contents:

```c
// LowerPWPImpl::LowerPWPImpl(Module&, PassOptions&, Logger&)  (0x115d130)
path = options.get(actRootJson);
if (path.empty())                                            // guard 1
    reportError("pwp act_info.json file must be specified");     // 0x1d50688, via reportError @ 0x6205c0
if (!is_absolute(path))                                     // guard 2
    reportError("pwp act_info.json file path must be absolute"); // 0x1d506b8

arch = Module::getArch().getArchModel();   // 0x115d27e/0x115d286 — HWM geometry ONLY (see GOTCHA)
                                           // default case -> "LowerAct: unknown arch type" @ 0x1c8364d

fillAllActInfos(loadJsonFile(path));       // 0x115af90 — purely data-driven catalog parse
```

```c
// fillAllActInfos(const json& act_info)  (0x115af90)
//   reads the two top-level keys of act_info.json:
for (set : act_info["act_func_sets"])      // key string @ 0x1c83615
    register_set(set["name"],              // per-set "name"
                 set["act"]);              // {func_name: budget} map  — the menu
file_keys = act_info["pwp_file_keys"];     // key string @ 0x1c83607  == ["bkt_bin","ctrl_bin","profile_json"]
// the cmp $0x1e near 0x115b767 is an ActivationFunctionType enum compare inside the
// set-cover Rb_tree (see 10.7 set-cover), NOT an arch-level gate.
```

> **GOTCHA —** the arch switch inside the ctor (`getArchModel`, default case `"LowerAct: unknown arch type"` @ `0x1c8364d`) is for the **HWM cost/geometry model**, not target selection. It picks per-core SBUF/PSUM geometry, never the trainium-vs-with_ln catalog. The set roster is 100% the JSON content; arch and PWP-target are two independent inputs (see [§ The Two Orthogonal Axes](#the-two-orthogonal-axes)).

### How the path reaches the NEFF

The packager side loads the same path and walks the catalog to copy every referenced blob into the NEFF Bill-Of-Materials:

```c
// NeffFileWriter::updateFromActJsonFile(boost::filesystem::path p)  (0x1542db0)
json j = bir::loadJsonFile(p);                        // 0x1542e14
j["activation_function_sets"];                        // 0x1542e19  — NEFF def.json WRAPPER key (0x1c8353d)
j["act_info"];                                        // 0x1542e43  — NEFF wrapper key       (0x1c83534)
j["pwp_file_keys"];                                   // 0x1543135                            (0x1c83607)
for (set : j["act_func_sets"]) {                      // 0x154315c                            (0x1c83615)
    for (key : pwp_file_keys)                          // bkt_bin / ctrl_bin / profile_json
        addToBom(set[key], ...);                       // 0x154308d — embed each blob
}
// writeFile @ 0x1541040 logs debug marker "foundActJson" (0x1c86cc8 @ 0x15427be);
// sibling updateFromDveJsonFile @ 0x1540130 does the identical job for --dve-root-json.
```

> **NOTE —** there are two distinct key vocabularies. The **catalog** `act_info.json` has exactly two top-level keys, `{act_func_sets, pwp_file_keys}` (verified by `jq 'keys'`). The strings `"activation_function_sets"` (`0x1c8353d`) and `"act_info"` (`0x1c83534`) that `updateFromActJsonFile` also indexes are keys of the **NEFF def.json wrapper**, a different JSON layer — do not look for them inside the shipped `act_info.json`.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `cl::opt<std::string> actRootJson` | `0x3df8a60` (.bss) | The selector global; its string value is the PWP target | CERTAIN |
| `cl::opt<std::string> dveRootJson` | `0x3e03b20` (.bss) | DVE-side parallel (`--dve-root-json` → `dve_info.json`) | CERTAIN |
| `LowerPWPImpl::LowerPWPImpl` | `0x115d130` | lower_act ctor: empty/absolute guards, then parse | CERTAIN |
| `LowerPWPImpl::fillAllActInfos` | `0x115af90` | Reads `act_func_sets` + `pwp_file_keys`; builds the set menu | CERTAIN |
| `NeffFileWriter::updateFromActJsonFile` | `0x1542db0` | Loads path; adds referenced blobs to NEFF BOM | CERTAIN |
| `NeffFileWriter::updateFromDveJsonFile` | `0x1540130` | DVE parallel | CERTAIN |
| `NeffFileWriter::writeFile` | `0x1541040` | Calls the two `updateFrom*` consumers | CERTAIN |

---

## The Catalog Delta — with_ln ⊂ trainium

### Purpose

The two catalogs are not arbitrary; `with_ln`'s function universe is a **strict subset** of `trainium`'s. This is the substance of the "split" — `with_ln` is the inference-only profile obtained by deleting the backward-pass heroes and the always-resident utility pad, then repacking the survivors into fewer, leaner sets.

### The subset proof

Taking the union of every `act` key across all sets in each catalog gives the *function universe* a target can emit. The two universes:

```text
trainium : 36 functions     with_ln : 22 functions
```

The subset relation is proven by set difference (`jq` extract → `comm`):

```text
functions in with_ln NOT in trainium  (comm -23):
  (empty)                       ⇒  with_ln ⊆ trainium   [CONFIRMED]
```

> **QUIRK —** the subset holds at the **function** level, *not* the set-name level. `with_ln` introduces three set *names* that trainium does not have — `erf_and_others`, `mish_and_small`, `softplus_tanh_and_others` — and trainium has ten set names that with_ln drops. So `comm` on the set-name lists is *not* a subset. The sets are repacked/merged (softplus is merged into the tanh set; `erf` is promoted to its own set; `mish` rides with `small`), but the *functions* they expose are a strict subset. A reimplementer who proves the subset by comparing `name` fields will get a false negative.

### The exact trim set

The 14 functions present in `trainium` but absent from `with_ln` (`comm -13`):

| Dropped function | Category |
|---|---|
| `derivative_erf` | backward-pass derivative |
| `derivative_gelu` | backward-pass derivative |
| `derivative_gelu_apprx_sigmoid` | backward-pass derivative |
| `derivative_identity` | backward-pass derivative (utility pad) |
| `derivative_leaky_relu` | backward-pass derivative (utility pad) |
| `derivative_relu` | backward-pass derivative (utility pad) |
| `derivative_sigmoid` | backward-pass derivative |
| `derivative_silu` | backward-pass derivative |
| `derivative_tanh` | backward-pass derivative |
| `silu` | forward hero dropped with its derivative |
| `abs_reciprocal_sqrt` | fused utility |
| `copy` | always-resident utility pad |
| `is_finite` | always-resident utility pad |
| `memset_zero` | always-resident utility pad |

The pattern: **all nine `derivative_*` heroes are gone**, `silu` (and its derivative) is gone, and the always-resident utility pad (`copy`, `is_finite`, `memset_zero`, the `derivative_identity/relu/leaky_relu` trio) is trimmed off every shared set. What survives is the forward/inference math plus the LayerNorm-path heroes.

> **NOTE —** "LN support" is realised by what the roster *keeps lean*, not by any new op. `with_ln`'s `natural_log` set is the leanest set in either catalog — `{ln: 40, abs: 1}`, two functions — versus trainium's 14-function `natural_log` set. The normalisation divisor heroes (`sqrt`, `reciprocal_sqrt`) stay at full LUT budget but in 8- and 5-function sets instead of 14 (verified: `sqrt_and_others` = 8 funcs in with_ln vs 14 in trainium). There is no `layernorm` activation function in either catalog; the split adds **zero** functions over trainium.

### Why two catalogs and not one

Shipping two independent directories lets the compiler embed the *minimal* image for inference (smaller NEFF, fewer LUT refills) and the *full* image for training, with the set-cover packing ([10.7](set-cover.md)) optimized independently for each. Because with_ln's shared sets carry fewer co-resident functions, their blob section count and packing differ, so the blobs are independently generated:

> **CORRECTION (D-M16) —** an earlier assumption held that with_ln re-uses trainium's blobs (a symlink/dedup). It does not. All 11 commonly-named `*_bkt.bin` blobs **differ byte-for-byte** between the directories (`md5`): e.g. `sqrt_and_others_bkt.bin` is `f439cf3e…` in trainium and `0bc145ce…` in with_ln. The *only* byte-identical file across the two directories is the (blank) `version.json` (`md5 bd412555…` both). The fingerprint-dedup hits reported elsewhere were cross-cpython copies (cp310/11/12 of the *same* directory), not cross-target. with_ln is a first-class, separately-built image.

### Is with_ln legacy?

Both catalogs' `version.json` is `{"KaenaPWP":{"brazil_version":"","git_sha":""}}` — blank. So are all three `dve_bin_gen{2,3,4}/version.json` (`{"KaenaDVE":…}`, blank). The blank `git_sha` is a **uniform release build-scrub**, not a "this artifact is stale" marker.

> **GOTCHA —** do not read the empty `git_sha` as evidence that `with_ln` is a legacy leftover. The whole release strips `brazil_version` and `git_sha` from every PWP and DVE `version.json` alike. with_ln ships in the 2.24.5133.0 wheel alongside trainium, with freshly, independently generated blobs — it is a current-gen artifact.

---

## The Two Orthogonal Axes

### Purpose

The compiler has two independent target selectors. Conflating them is the central trap of this page: a reimplementer who treats "trainium" as an arch will look for it in the ISA enum and not find a clean fit; one who treats with_ln as an arch will not find it at all.

### Axis A — ISA / arch (device codename)

The arch axis maps a device codename → `ArchLevel` → Core geometry + ISA opcode schema. The enum (libBIR + `libwalrus getArchModel`) and its help string:

```text
help string @ libwalrus:  "tonga, sunda,gen3,core4,gen5"   (verbatim)

tonga / inf1 / inferentia          -> Inferentia (gen1, ArchLevel 10)
sunda / TRAINIUM / trn1 / inf2 ...  -> Sunda      (gen2, 20)   <- "trainium" is a Sunda alias
cayman / gen3                       -> Cayman     (30)
core_v4 / core4                     -> CoreV4     (40)
gen5                                -> (50, stub)
```

The help string carries **no** `with_ln` token and **no** PWP-target token. The DVE table directories *are* per-gen (`dve_bin_gen2/3/4`) — DVE binds to this arch axis. PWP does not.

### Axis B — PWP target (the act-root-json path)

The PWP target is the `--act-root-json` path: `pwp_bin_trainium` (21) or `pwp_bin_with_ln` (14). There are only **two** directories and **no** per-gen split — no `pwp_bin_gen2/3/4`. A single PWP target serves all arches because the table-load codegen is **gen-invariant**:

> **NOTE —** the `act_func_set_id` encoding and the `LoadActFuncSet` codegen are gen-invariant — one `CoreV2` visitor is reused for every gen, and opcode `0x23` (`ActivationTableLoad`) is identical across core_v2 and core_v4 (see [10.6 (LoadActFuncSet)](loadactfuncset.md)). Because the load mechanism does not vary by arch, the PWP catalog does not need a per-arch variant. The single dimension that *does* vary is train-vs-inference profile — and that is exactly the trainium/with_ln split.

Therefore "trainium" in the directory name is the **device-family label for the full set** (named after the gen2 device that the full training roster targets), not a per-arch binding. The two axes are independent inputs to the compile; no ISA target-config field disambiguates the PWP target.

```text
            ISA / arch axis (codename)              PWP-target axis (act-root-json)
            -------------------------               -------------------------------
            tonga  sunda  cayman  core4  gen5        pwp_bin_trainium   pwp_bin_with_ln
              |      |       |      |     |                 (21)              (14)
              +---- Core geometry, ISA  -+            +-- train+inference  +-- inference-only
                    (DVE binds here:                       (full roster)       (subset)
                     dve_bin_gen2/3/4)                  CHOSEN BY PATH VALUE, not by arch
```

### The DVE parallel

The DVE engine ships the same train-vs-inference idea, packaged as named sub-tables instead of separate directories — independent corroboration that with_ln is a *profile*, not a device. `dve_info.json` has a `tables[]` array of named profiles (verified by `jq`):

```text
dve_bin_gen2 / gen3 :  { default, transformer, transformer_fwd, transformer_bwd, transformer_bwd2 }
dve_bin_gen4        :  { default, saturate, transformer }
```

`transformer_fwd` (forward/inference) vs `transformer_bwd`/`bwd2` (backward/training) is exactly the trainium (full, incl. backward) vs with_ln (forward/inference-trimmed) split — but DVE keeps all profiles in one `dve_info.json` per gen, whereas PWP splits at the directory level. Same concept, different packaging. And like PWP, `libwalrus` contains no `"transformer"`/`"default"`/`"saturate"` selector string — the sub-table is chosen by data-driven op/func coverage, never by a baked name.

> **QUIRK —** with_ln is the PWP analog of DVE's `transformer_fwd`. The naming asymmetry (`with_ln` describes a *graph property*, `transformer_fwd` a *workload + direction*) is cosmetic; both are "the inference/forward-trimmed profile of their engine's table catalog."

---

## What Is — and Is Not — in the Binary

The mechanism is fully resolved from the binary; the *policy* is not. This boundary matters for a reimplementer deciding where to put the decision.

| Question | Where it lives | Confidence |
|---|---|---|
| What option selects the target | `cl::opt actRootJson` in libwalrus | CONFIRMED |
| What the value means | the absolute path's directory = the catalog | CONFIRMED |
| with_ln ⊆ trainium (functions) | the two `act_info.json` files | CONFIRMED |
| Exact 14-function trim set | `comm -13` on the function universes | CONFIRMED |
| PWP axis ⊥ ISA axis | gen-invariant load codegen + no per-gen dirs | CONFIRMED |
| with_ln = inference/forward profile | subset proof + DVE `transformer_fwd` analog | STRONG |
| **Which compile-mode/graph picks with_ln** | the Python WalrusDriver/Kelper job orchestration — **NOT in the wheel binaries** | INFERRED (gap) |

> **NOTE —** the upstream rule that *decides* to pass `pwp_bin_with_ln` (which front-end flag, compile-mode, or graph property triggers it) is not resolvable from the shipped wheel. The orchestration that builds the `walrus_driver` shell command is not in the 199 shipped `.py`; it is a driver/native path upstream of this binary. The `dynamic_pwp` gate (`neff_feature_dynamic_pwp`) is *orthogonal* — it toggles dynamic-vs-static table loading, not which roster, and is confirmed **not** the selector here.

---

## Related Components

| Name | Relationship |
|---|---|
| `LowerPWPImpl` (lower_act pass) | Consumes `--act-root-json`; builds the set menu via `fillAllActInfos` |
| `NeffFileWriter` | Embeds the selected catalog's blobs into the NEFF BOM |
| `getArchModel` (HWM model) | The *other* axis — device geometry; does **not** pick the PWP target |
| DVE (`--dve-root-json`) | Exact parallel mechanism; expresses the same train/inference split as named `tables[]` |

## Cross-References

- [PWP Activation Model](pwp-model.md) — 10.1, the piecewise-polynomial evaluation model both catalogs' coefficients feed
- [LoadActFuncSet](loadactfuncset.md) — 10.6, the `act_func_set_id` encoding and opcode `0x23` table-load that is gen-invariant across both catalogs
- [Set-Cover](set-cover.md) — 10.7, the greedy set-cover packing each catalog drives independently
- [Activation Function Catalog](act-function-catalog.md) — 10.2, the per-set function rosters and budgets that this page compares between targets
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where `WalrusDriver` sits and how the `walrus_driver` command line (carrying `--act-root-json`) is emitted
- [SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the per-core HWM geometry chosen by the orthogonal arch axis (`getArchModel`)
