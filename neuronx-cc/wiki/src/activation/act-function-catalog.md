# Activation Function Catalog & Function-Sets

> *All enum names, ordinals, and binary addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The `ActivationFunctionType` enum and its serializers live in `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`, VA==fileoff for `.text`/`.rodata`); the activation-function-set rosters are the shipped `act_info.json` files (cp310/311/312 byte-identical — both files md5-stable across all three wheels). Treat every address as version-pinned.*

## Abstract

The Activation (Scalar) engine evaluates 31 distinct activation functions, but it does not carry 31 independent fixed-function units. Nine of them are **hardwired elementwise** silicon — relu, abs, identity, square, and their kin — that cost nothing to keep resident. The other twenty-two are **piecewise-polynomial** functions, each realized by a coefficient table (LUT) that must be *loaded* into the engine before it can compute that function. The engine LUT holds **one bundle at a time**, so the compiler cannot freely mix arbitrary polynomial functions in a single pass.

This page documents two things that together form the activation "instruction set" the rest of Part 10 builds on. First, the **catalog**: which functions exist (the 31-member `bir::ActivationFunctionType` enum), and the hardwired-vs-LUT split that decides which ones need a table load. Second, the **function-set structure**: the `act_info.json` roster of pre-baked LUT bundles — **21 sets** for the `trainium` profile, **14 sets** for the `with_ln` profile — each naming a small group of functions that are co-resident when that bundle is loaded. The compiler's set-cover allocator (LowerPWPImpl `calculateBestSets`, [10.7 (set-cover)](set-cover.md)) picks a greedy first-fit sequence of bundles to cover a graph's requested functions, emitting one `InstLoadActFuncSet` per chosen set ([10.6 (LoadActFuncSet)](loadactfuncset.md) for the codegen).

Two facts shape everything downstream and are easy to get wrong. First, the per-function integer in an `act` map is **not** the LUT bucket count — it is a *profile-variant selector* (`_Np`) that indexes a separate per-function profile in `pwp_jsons/`; the actual polynomial resolution `lut_size` is a different, independent number (`exp` selector 400, but `lut_size` 777). Second, `with_ln` is a strict **functional subset** of `trainium` — it adds no layernorm-specific activation; it merely strips the backward-pass (derivative) and utility functions out of co-residency to leave more LUT headroom on the forward/inference path.

| | |
|---|---|
| **Enum** | `bir::ActivationFunctionType` — 31 members (0..30), `Unknown`=30 sentinel |
| **Forward serializer** | `ActivationFunctionType2string` @ `0x4002a0` (libBIR.so, 31-arm switch) |
| **Inverse serializer** | `string2ActivationFunctionType` @ `0x40d710` (31-arm if/else) |
| **Carrying opcode** | `InstActivation` (InstructionType=4); `func` field @ object `+0x120`, wire key `"func"` |
| **Hardwired count** | 9 BIR members (`lut_size`==0) + 3 always-on derivatives + `memset_zero` |
| **LUT-driven count** | 22 BIR members (`lut_size`>0), bundled into act-func-sets |
| **Roster (trainium)** | `pwp/pwp_bin_trainium/act_info.json` — **21** sets (15859 B, md5 `aa8ea33a…`) |
| **Roster (with_ln)** | `pwp/pwp_bin_with_ln/act_info.json` — **14** sets (6735 B, md5 `35097b68…`) |
| **Set loader** | `InstLoadActFuncSet` (InstructionType=6) — loads exactly one set by index |
| **Residency rule** | engine LUT holds one set; to use function X you load the set whose `act` map contains X (and get every other function in that set for free) |

The catalog and enum facts come from static analysis of `libBIR.so`; the function-set rosters are extracted from the shipped `act_info.json` files.

---

## 1. The function catalog — 31 members, hardwired vs LUT

All 31 activation variants route through **one** BIR opcode — `InstActivation` (InstructionType=4) — selected by a 4-byte `ActivationFunctionType` enum stored at object offset `+0x120` and serialized under the JSON key `"func"` (`InstActivation::toJson` @ `0x435450`; the `lea [rbx+0x120]` precedes the `bir::to_json(...ActivationFunctionType...)` call @ `0x4354ec`). The enum is InstaBrew-generated: its forward serializer (`ActivationFunctionType2string` @ `0x4002a0`, a 31-arm switch) and inverse (`string2ActivationFunctionType` @ `0x40d710`) agree byte-exactly on the ordinal↔name mapping in both directions, so the table below is fixed in both directions.

Two functions also have **dedicated hardwired opcodes** that bypass the generic path: `Exp(7)` can be emitted as `InstExponential` (IT=103, no `func` field — value implicit in the opcode), and `Reciprocal(25)` as `InstReciprocal` (IT=21, a stub `readFieldsFromJson` @ `0x405e10`). The compiler may emit either encoding for those two; the enum members 7/25 reach the same silicon via the generic `InstActivation` path.

The **hardwired vs PWP** split is decided by one criterion read directly from the per-function profiles in `pwp_jsons/`:

> **`lut_size` == 0 ⇒ hardwired** elementwise op — no LUT bucket spend, always resident in *every* set (its `act_info` budget is the sentinel value `1`).
> **`lut_size` > 0 ⇒ piecewise-polynomial** — must be loaded as the "hero" of an act-func-set before it can be computed.

### 1.1 The full enum (PWP? column = the split)

`PWP-id` is `neuron_id` (the canonical Neuron opcode-id the Activation engine dispatches on); `lut` is `pwp_jsons` `lut_size`. `PWP?` is **H**ardwired (`lut`==0) or **T**able-driven (`lut`>0).

| Int | BIR Name (verbatim) | pwp func name | PWP-id | lut | PWP? |
|----:|---------------------|---------------|-------:|-----:|:----:|
| 0  | `Identity`                      | identity                      | 1   | 0    | **H** |
| 1  | `Square`                        | square                        | 30  | 0    | **H** |
| 2  | `Relu`                          | relu                          | 2   | 0    | **H** |
| 3  | `Lrelu`                         | leaky_relu                    | 3   | 0    | **H** |
| 4  | `Prelu`                         | parametric_relu               | 4   | 0    | **H** |
| 5  | `Sigmoid`                       | sigmoid                       | 5   | 796  | T |
| 6  | `Tanh`                          | tanh                          | 6   | 104  | T |
| 7  | `Exp`                           | exp                           | 7   | 777  | T |
| 8  | `Softplus`                      | softplus                      | 9   | 828  | T |
| 9  | `Sqrt`                          | sqrt                          | 8   | 1113 | T |
| 10 | `Rsqrt`                         | reciprocal_sqrt               | 29  | 1202 | T |
| 11 | `Ln`                            | ln                            | 10  | 513–1219 | T(mp) |
| 12 | `Erf`                           | erf                           | 21  | 66   | T |
| 13 | `Sin`                           | sin                           | 19  | 55   | T |
| 14 | `Arctan`                        | arctan                        | 28  | 168  | T |
| 15 | `Sign`                          | sign                          | 31  | 0    | **H** |
| 16 | `Gelu`                          | gelu                          | 23  | 504  | T |
| 17 | `Mish`                          | mish                          | 24  | 1099 | T |
| 18 | `Gelu_apprx_tanh`               | gelu_apprx_tanh               | 25  | 945  | T |
| 19 | `Gelu_apprx_sigmoid`            | gelu_apprx_sigmoid            | 26  | 1042 | T |
| 20 | `Derivative_Gelu_apprx_sigmoid` | derivative_gelu_apprx_sigmoid | 38  | 1283 | T |
| 21 | `Derivative_Gelu`               | derivative_gelu               | 32  | 115  | T |
| 22 | `Derivative_Erf`                | derivative_erf                | 22  | 466–1182 | T |
| 23 | `Copy`                          | copy                          | 128 | 0    | **H** |
| 24 | `Abs`                           | abs                           | 33  | 0    | **H** |
| 25 | `Reciprocal`                    | reciprocal                    | 129 | 1016 | T |
| 26 | `Abs_reciprocal_sqrt`           | abs_reciprocal_sqrt           | 34  | 1202 | T |
| 27 | `Silu`                          | silu                          | 36  | 908  | T |
| 28 | `Derivative_silu`               | derivative_silu               | 37  | 1008 | T |
| 29 | `Is_finite`                     | is_finite                     | 35  | 0    | **H** |
| 30 | `Unknown`                       | (sentinel — no pwp)           | —   | —    | — |

> **NOTE —** `Ln(11)` is the only `use_multipass=true` profile. Its `pwp_jsons` has multipass variants (`ln_4p_0mp` `lut_size` 775, `ln_4p_1mp` 832) *and* single-pass variants (`ln_40p` 513, `ln_400p` 1219). The two act-func-set ln budgets (40, 400) select the **single-pass** profiles; the multipass profiles are reached by a separate codegen path (see 10.1 `pwp-model.md`), not by any `act_info` budget.

### 1.2 The hardwired set (the always-resident pad)

Nine BIR members are hardwired (`lut_size`==0): **Identity(0), Square(1), Relu(2), Lrelu(3), Prelu(4), Sign(15), Copy(23), Abs(24), Is_finite(29)**. Two `act_info` resident classes are *not* exposed as standalone BIR `ActivationFunctionType` members but appear in the `act` maps:

- the **always-on derivative trio** — `derivative_relu`, `derivative_leaky_relu`, `derivative_identity` (each `lut_size`==0, cheap elementwise gradients);
- **`memset_zero`** (pwp `neuron_id` 130; a PWP-only entry, no BIR enum slot).

On the `trainium` profile these thirteen names form a single block that is co-resident in *every* set at budget `1` — the "no-LUT-spend" floor (see §3). The `with_ln` profile keeps only **`abs`** as universal and drops the rest from the always-on pad (§4).

> **GOTCHA —** `Relu(2)` has a dedicated fast-path opcode `InstGenericRelu` (IT=2) in addition to its `InstActivation` route, mirroring the Exp/Reciprocal dual encoding. Reading "relu" in a graph does not guarantee an `InstActivation`.

---

## 2. The `act_info.json` schema, field by field

Each profile (`pwp_bin_trainium`, `pwp_bin_with_ln`) ships one `act_info.json` with exactly **two** top-level keys (`jq keys`):

```json
{
  "act_func_sets": [ /* ordered array of set descriptors */ ],
  "pwp_file_keys": ["bkt_bin", "ctrl_bin", "profile_json"]
}
```

- **`act_func_sets`** — the ordered array of LUT bundles. `trainium` length **21**, `with_ln` length **14** (`jq '.act_func_sets|length'`). `InstLoadActFuncSet` (IT=6) loads exactly one of these by array index.
- **`pwp_file_keys`** — the three string keys naming the per-set file-reference fields. Identical in both profiles. It is metadata (a key list), not per-set data.

Each set descriptor has exactly **5** keys (`jq '.act_func_sets[0]|keys'` → `["act","bkt_bin","ctrl_bin","name","profile_json"]`):

| Key | Type | Meaning |
|-----|------|---------|
| `name`         | string | set identity (the func-bundle name, e.g. `exp_and_others`) |
| `bkt_bin`      | string | `"<name>_bkt.bin"` — bucket/LUT-coefficient blob (decoded by 10.x, M06 scope) |
| `ctrl_bin`     | string | `"<name>_ctrl.bin"` — control/region-table blob |
| `profile_json` | string | `"<name>.json"` — per-SET combined profile (schema is M05 scope) |
| `act`          | object | `{ func_name(str) → budget(int) }` — **the payload** |

The `bkt_bin`/`ctrl_bin`/`profile_json` references are deterministic from `name` and **all present** — 21×3 (trainium) + 14×3 (with_ln) sibling files, zero missing (verified by file-existence sweep over every reference). `bkt` blobs run ≈8–46 KB, `ctrl` ≈0.5–8 KB, profiles ≈10–47 KB.

Both profiles also ship a `version.json` (per-target, not per-set, 80 B, identical): `{"KaenaPWP":{"brazil_version":"","git_sha":""}}` — both fields blank (build-stamp scrubbed). There is no top-level `pwp/version.json`.

### 2.1 The `act` map — co-residency and budget

The `act` object is the heart of the descriptor: it lists **every function that becomes resident when this set is loaded**, each mapped to its per-function budget.

```c
// Conceptual reading of one act_func_set descriptor.
struct ActFuncSet {
  const char *name;            // "<set>"
  const char *bkt_bin;         // "<set>_bkt.bin"  — coefficient buckets
  const char *ctrl_bin;        // "<set>_ctrl.bin" — region/exponent control
  const char *profile_json;    // "<set>.json"     — combined per-set profile
  map<string,int> act;         // func -> budget
};

// budget semantics:
//   budget == 1  : hardwired/cheap elementwise resident. No LUT spend.
//                  These are the "free riders" carried in every (or most) sets.
//   budget  > 1  : a polynomial "hero". The integer is the _Np PROFILE SELECTOR
//                  (pwp_jsons/<func>_<budget>p.json), NOT the LUT bucket count.
```

> **GOTCHA —** the budget integer is **not** the polynomial bucket count, despite reading like one. It is the `_Np` profile-variant selector — a foreign key into `pwp_jsons/<func>_<budget>p.json`. The actual resolution `lut_size` lives *inside* that profile and is an independent number. Seven paired reads make the independence plain: `exp` 400→`lut_size` 777 · `softplus` 40→828 · `sigmoid` 40→796 · `sqrt` 65536→1113 · `reciprocal_sqrt` 40000→1202 · `mish` 4→1099 · `gelu` 4→504.

**Co-residency rule.** To compute activation X the compiler must load the (unique-or-cheapest) set Y whose `act` map contains X; that load *also* brings in every other function in Y. You cannot mix two heroes from different sets without a second `InstLoadActFuncSet`. The engine LUT holds one set at a time — `InstLoadActFuncSet` is the residency gate, and the allocator ([10.7](set-cover.md)) is a greedy set-cover over the requested function list.

---

## 3. The `trainium` catalog — 21 sets

Legend: **HERO** = function(s) at budget>1 (the high-resolution polynomial payload). The **13 universal residents** `{abs, copy, derivative_identity, derivative_leaky_relu, derivative_relu, identity, is_finite, leaky_relu, memset_zero, parametric_relu, relu, sign, square}` appear at budget=1 in **all 21** sets (this is the hardwired always-on block from §1.2, obtained by intersecting the budget-1 keys across all sets). Below, that block is abbreviated `+13univ`; only the heroes are spelled out. Sizes are `bkt`/`ctrl`/`profile` in bytes.

| #  | set name | HERO(s) [budget] | #funcs | bkt/ctrl/prof (B) |
|---:|----------|------------------|-------:|-------------------|
| 0  | `exp_and_others`                            | exp[400], tanh[4]                          | 15 | 30112/2848/28093 |
| 1  | `softplus_and_others`                       | softplus[40]                               | 14 | 28288/1952/24207 |
| 2  | `sigmoid_and_others`                        | sigmoid[40], tanh[4], erf[4], arctan[4]    | 17 | 38464/2624/31764 |
| 3  | `sqrt_and_others`                           | sqrt[65536] ⟵ max budget                   | 14 | 37408/8128/46870 |
| 4  | `small`                                     | — (no hero; pure residents)                | 13 | 1664/640/19534 |
| 5  | `natural_log`                               | ln[40]                                      | 14 | 40800/4736/35257 |
| 6  | `natural_log_exp_and_others`                | ln[400], exp[400] ⟵ dual hi-res            | 15 | 43200/6400/40548 |
| 7  | `sigmoid_derivative`                        | derivative_sigmoid[40]                      | 14 | 27296/992/22252 |
| 8  | `tanh_and_derivative`                       | derivative_tanh[400], tanh[4]               | 15 | 17952/1568/25531 |
| 9  | `trig_and_small`                            | sin[4], arctan[4]                           | 15 | 9056/1920/26659 |
| 10 | `gelu_and_others`                           | derivative_gelu[40], gelu[4], tanh[4]       | 16 | 25184/2144/28177 |
| 11 | `gelu_apprx_tanh_and_others`                | gelu_apprx_tanh[40], tanh[4]                | 15 | 35488/1824/25864 |
| 12 | `gelu_apprx_sigmoid_and_others`             | gelu_apprx_sigmoid[40], tanh[4]             | 15 | 38592/1920/26237 |
| 13 | `reciprocal_and_small`                      | reciprocal[400]                             | 14 | 34304/3360/30460 |
| 14 | `reciprocal_sqrt_and_small`                 | reciprocal_sqrt[40000]                      | 14 | 40256/6560/41653 |
| 15 | `abs_reciprocal_sqrt_and_small`             | abs_reciprocal_sqrt[40000]                  | 14 | 40256/6560/41675 |
| 16 | `mish_and_others`                           | mish[4]                                     | 14 | 36960/1408/23030 |
| 17 | `erf_derivative`                            | derivative_erf[400]                         | 14 | 16704/960/22120 |
| 18 | `silu_and_others`                           | silu[32], tanh[4], sin[4]                   | 16 | 36192/2400/29022 |
| 19 | `derivative_silu_and_others`                | derivative_silu[32], tanh[4], sin[4]        | 16 | 39392/1984/28457 |
| 20 | `derivative_gelu_apprx_sigmoid_and_others`  | derivative_gelu_apprx_sigmoid[4096], tanh[4]| 15 | 46304/1600/25903 |

> **QUIRK —** the `gelu_and_others` set's real high-resolution hero is **`derivative_gelu`[40]**, not `gelu` (which sits there at budget 4). The set is tuned for the gelu *backward* pass; the forward gelu rides along cheaply. Likewise `tanh_and_derivative`'s hero is `derivative_tanh`[400], with forward tanh at 4.

**Trainium co-residency facts** (which polynomial functions ride together):

- **`tanh`[4] is the most-shared hero — present in 9 sets** (`exp_and_others`, `sigmoid_and_others`, `tanh_and_derivative`, `gelu_and_others`, `gelu_apprx_tanh_and_others`, `gelu_apprx_sigmoid_and_others`, `silu_and_others`, `derivative_silu_and_others`, `derivative_gelu_apprx_sigmoid_and_others`). It has **no set of its own** — to use tanh you load whichever other set carries it (cheapest = `exp_and_others`). There is no `tanh_and_small`.
- **`erf` is reachable only via `sigmoid_and_others`(#2)** on trainium — co-resident with sigmoid+tanh+arctan. No standalone erf set (contrast `with_ln`, §4).
- **`sin` lives in 3 sets**: `trig_and_small`(#9) and both silu sets (#18, #19) — silu's `x·sigmoid(x)` kernel co-loads tanh and sin.
- **`exp`** is reachable via `exp_and_others`(#0) or `natural_log_exp_and_others`(#6, paired with `ln`[400]); **`ln`** via `natural_log`(#5, budget 40) or `natural_log_exp_and_others`(#6, budget 400). The two ln sets differ in *both* resolution and co-residency.
- The `small` set (#4) is the **no-hero set**: its `act` map is exactly the 13 universal residents — what is resident when no polynomial function is needed (LUT empty).
- Each of `{sqrt, reciprocal_sqrt, abs_reciprocal_sqrt, reciprocal, softplus, derivative_sigmoid, derivative_tanh, derivative_erf, mish, gelu_apprx_tanh, gelu_apprx_sigmoid, derivative_silu, derivative_gelu_apprx_sigmoid}` has **exactly one owning set** — a single point of residency (load that set, get only `+13univ` besides).

---

## 4. The `with_ln` catalog — 14 sets

The `with_ln` profile is **much leaner**. Only **`abs`** is universal across all 14 sets; the trainium 13-block is gone as a unit — `with_ln` drops `copy`, `memset_zero`, `is_finite`, and **all** derivative residents from the always-on pad. Per-set function counts shrink to 2–10 (vs trainium's 13–17). The full `act` map is given for each set.

| #  | set name | HERO(s) [budget] | #funcs | bkt/ctrl/prof (B) |
|---:|----------|------------------|-------:|-------------------|
| 0  | `exp_and_others`                | exp[400], tanh[4]            | 8  | 29216/1248/17539 |
| 1  | `sigmoid_and_others`            | sigmoid[40], tanh[4]         | 8  | 29824/592/14797 |
| 2  | `softplus_tanh_and_others`      | softplus[40], tanh[4]        | 8  | 30848/1072/16815 |
| 3  | `sqrt_and_others`               | sqrt[65536]                  | 8  | 36640/3904/37786 |
| 4  | `small`                         | — (no hero)                  | 7  | 896/160/10474 |
| 5  | `natural_log`                   | ln[40]                       | 2  | 39264/2080/17249 |
| 6  | `gelu_and_others`               | gelu[4], tanh[4]             | 8  | 20480/752/15249 |
| 7  | `gelu_apprx_tanh_and_others`    | gelu_apprx_tanh[40], tanh[4] | 8  | 34592/736/15264 |
| 8  | `gelu_apprx_sigmoid_and_others` | gelu_apprx_sigmoid[40], tanh[4] | 8 | 37696/784/15637 |
| 9  | `mish_and_small`                | mish[4]                      | 7  | 36064/528/12428 |
| 10 | `reciprocal_and_small`          | reciprocal[400]              | 5  | 33152/1424/16767 |
| 11 | `reciprocal_sqrt_and_small`     | reciprocal_sqrt[40000]       | 5  | 39104/3056/28082 |
| 12 | `trig_and_small`                | sin[4], arctan[4]            | 8  | 8160/784/16105 |
| 13 | `erf_and_others`                | erf[4], tanh[4], sin[4]      | 10 | 8480/752/18573 |

The lean `act` maps (budget-1 residents elided to `+resid`):

```text
0  exp_and_others       : exp=400 tanh=4 | identity leaky_relu parametric_relu relu abs sign
1  sigmoid_and_others   : sigmoid=40 tanh=4 | identity leaky_relu parametric_relu relu abs sign
2  softplus_tanh_others : softplus=40 tanh=4 | identity leaky_relu parametric_relu relu abs sign
3  sqrt_and_others      : sqrt=65536 | identity leaky_relu parametric_relu relu sign abs square
4  small                : relu leaky_relu parametric_relu identity sign abs square   (7, no hero)
5  natural_log          : ln=40 abs                                        (2 funcs — leanest)
6  gelu_and_others      : gelu=4 tanh=4 | relu leaky_relu parametric_relu abs sign identity
7  gelu_apprx_tanh      : gelu_apprx_tanh=40 tanh=4 | relu leaky_relu parametric_relu abs sign identity
8  gelu_apprx_sigmoid   : gelu_apprx_sigmoid=40 tanh=4 | relu leaky_relu parametric_relu abs sign identity
9  mish_and_small       : mish=4 | relu leaky_relu parametric_relu sign abs identity
10 reciprocal_and_small : reciprocal=400 | parametric_relu sign abs square
11 reciprocal_sqrt_small: reciprocal_sqrt=40000 | identity square abs sign
12 trig_and_small       : sin=4 arctan=4 | relu abs leaky_relu parametric_relu sign identity
13 erf_and_others       : erf=4 tanh=4 sin=4 | relu abs leaky_relu parametric_relu identity sign square
```

**With_ln co-residency facts:**

- **`tanh`[4] present in 7 of 14 sets** (#0,1,2,6,7,8,13) — same "never standalone" pattern.
- **`erf` gets a dedicated set** (`erf_and_others`#13, co-resident sin+tanh). On trainium erf only existed inside `sigmoid_and_others`; `with_ln` promotes it to its own bundle, which frees `sigmoid_and_others` to be a clean sigmoid+tanh set.
- **`softplus` is merged with `tanh`** into `softplus_tanh_and_others`(#2). Trainium had a tanh-free `softplus_and_others`; `with_ln` co-loads tanh with softplus (the LayerNorm + softplus-family path wants tanh resident simultaneously).
- **`natural_log`#5 = `{ln, abs}` only** — 2 functions, the leanest set in either profile. The LN-heavy path keeps the LUT almost entirely for `ln`.

---

## 5. Trainium vs with_ln — the difference

> **NOTE —** "with_ln" means *with-layernorm-support, inference-trimmed* — **not** "extra LN activation op". The function-universe diff is strict: `with_ln ⊂ trainium`. No activation exists on `with_ln` that is absent from `trainium`. LN support is realized by keeping the `ln`/`sqrt`/`reciprocal_sqrt` heroes in lean, easy-to-load sets, not by adding a function.

### 5.1 Set-name diff (21 → 14)

**Shared (11 sets, same name — but `act` maps differ, §5.2):** `exp_and_others`, `gelu_and_others`, `gelu_apprx_sigmoid_and_others`, `gelu_apprx_tanh_and_others`, `natural_log`, `reciprocal_and_small`, `reciprocal_sqrt_and_small`, `sigmoid_and_others`, `small`, `sqrt_and_others`, `trig_and_small`.

**Trainium-only (10 sets, dropped):** `abs_reciprocal_sqrt_and_small`, `derivative_gelu_apprx_sigmoid_and_others`, `derivative_silu_and_others`, `erf_derivative`, `mish_and_others` (→ renamed `mish_and_small`), `natural_log_exp_and_others`, `sigmoid_derivative`, `silu_and_others`, `softplus_and_others` (→ renamed/merged `softplus_tanh_and_others`), `tanh_and_derivative`.

**With_ln-only (3 sets, added/renamed):** `erf_and_others` (NEW — erf promoted to its own set), `mish_and_small` (rename of `mish_and_others`), `softplus_tanh_and_others` (rename+merge of `softplus_and_others`, adds tanh).

Net: **21 − 10 + 3 = 14.** ✓

### 5.2 Shared-set `act`-map delta

Every shared name has a leaner `with_ln` map: it loses the trainium resident pad `{copy, memset_zero, is_finite, derivative_relu, derivative_leaky_relu, derivative_identity}` and often `square`. The biggest co-residency change is `sigmoid_and_others`: trainium carries sigmoid+tanh+**erf**+**arctan** (17 funcs); `with_ln` keeps sigmoid+tanh only (8 funcs) — erf and arctan move out to `erf_and_others` and `trig_and_small`. Heroes and budgets are *preserved* on shared sets; only the resident pad is trimmed.

### 5.3 Function-universe diff

Functions present on `trainium` but **absent from the entire `with_ln` file** (14): `abs_reciprocal_sqrt`, `copy`, `derivative_erf`, `derivative_gelu`, `derivative_gelu_apprx_sigmoid`, `derivative_identity`, `derivative_leaky_relu`, `derivative_relu`, `derivative_sigmoid`, `derivative_silu`, `derivative_tanh`, `is_finite`, `memset_zero`, `silu`. Functions on `with_ln` but absent from `trainium`: **none**.

So `with_ln` drops all 7 backward-pass derivative functions + `copy` + `memset_zero` + `is_finite` + `silu` + `abs_reciprocal_sqrt`. It is the forward/inference-oriented profile.

### 5.4 Distinct budget tiers

| profile | budget tiers |
|---------|--------------|
| `trainium` | `{1, 4, 32, 40, 400, 4096, 40000, 65536}` (8 tiers) |
| `with_ln`  | `{1, 4, 40, 400, 40000, 65536}` (6 tiers) |

`with_ln` loses tiers 32 and 4096 — exactly the budgets of `silu`/`derivative_silu`[32] and `derivative_gelu_apprx_sigmoid`[4096], all in dropped sets.

---

## 6. How the catalog is used (forward references)

- **10.1 (`pwp-model.md`)** — the piecewise-polynomial evaluation model that consumes the `pwp_jsons/<func>_<budget>p.json` profiles a budget selects, and the multipass `ln` path.
- **[10.6 (`LoadActFuncSet` codegen)](loadactfuncset.md)** — the IT6 wire encoding (opcode `0x23`, the `act_func_set_id`/`act_tbl_sel` index byte), the `dynamic_pwp` gate, and the no-cost-model finding. This page is the *menu*; 10.6 is how a chosen set is *installed*.
- **[10.7 (set-cover)](set-cover.md)** — the allocator `LowerPWPImpl::calculateBestSets` that picks a **greedy first-fit** sequence of these sets to cover a graph's requested `ActivationFunctionType` list, emitting one `InstLoadActFuncSet` (IT=6) per chosen set. 10.7 is the *chooser*.
- **1.10 (Activation engine — `arch/`)** — the Scalar/Activation engine that holds one resident set at a time and dispatches polynomial functions by `neuron_id`.

---

## 7. Evidence summary

| Claim | Anchor | Confidence |
|-------|--------|-----------|
| `ActivationFunctionType` = 31 members 0..30 | `ActivationFunctionType2string` @ `0x4002a0`; both serializers agree byte-exactly | CERTAIN |
| Hardwired = 9 BIR members (`lut_size`==0) + 3 derivatives + memset | `lut_size` read per function from `pwp_jsons` | CERTAIN |
| trainium = 21 sets, with_ln = 14 sets | `jq '.act_func_sets\|length'` over both shipped files | CERTAIN |
| Per-set hero/budget maps (all of §3, §4) | `jq` over the shipped `act_info.json` | CERTAIN |
| 13 universal residents (trainium) / `abs` only (with_ln) | budget-1 key intersection across all sets | CERTAIN |
| budget ≠ `lut_size` | seven paired reads (exp 400→777, sqrt 65536→1113, …) | CERTAIN |
| with_ln ⊂ trainium function universe | `comm` over the `act`-key unions | CERTAIN |
| cp310/311/312 byte-identical rosters | md5 match across all three wheels | CERTAIN |
| `InstLoadActFuncSet` (IT=6) loads exactly one set | the IT6 residency gate; the codegen wire encoding belongs to [10.6](loadactfuncset.md) | HIGH |

## 8. Out of scope here

Owned by sibling pages: the `bkt`/`ctrl` blob byte-decode; the per-set `profile_json` schema and polynomial coefficient layout; the `InstLoadActFuncSet` codegen and `act_func_set_id` wire encoding ([10.6](loadactfuncset.md)); the greedy set-cover allocator ([10.7](set-cover.md)).

One item is genuinely open rather than delegated: the per-engine legal-function gate — the libBIR `DenseMap<EngineInfo, vector<set<ActivationFunctionType>>>` static image. Its structure is known but its contents were not dumped, so which of the 31 functions each engine accepts on each arch is [UNRESOLVED].
