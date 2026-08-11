# The Per-Set Activation `profile_json` and the `updateFromActJsonFile` Consumer

> *All paths, JSON values, addresses and symbols on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; the `neuronxcc/pwp/` tree is byte-identical across cp310/cp311/cp312). `act_info.json` md5 `aa8ea33a5b7414695fa87ca890f50d6c`. Other versions will differ.*

## Abstract

Every loadable activation set in `neuronx_cc` ships as **three** files: two binary blobs (`<set>_bkt.bin`, `<set>_ctrl.bin`) and one JSON manifest (`<set>.json`). This page documents that JSON manifest — the **per-set combined `profile_json`** — and the native code that reads it at NEFF-pack time. The manifest is not the activation numerics (those live one tier up, in `pwp_jsons/<func>_Np.json`, [§10.1](pwp-model.md)); nor is it the packed hardware LUT (that is the two blobs, [§10.4](bkt-ctrl-blob.md)). It is the **index and denormalized control block** that fuses every function co-resident in a set into one image: it names the two blobs, counts their entries, records where each function's data begins inside them (coarse and per-exponent-octave), and carries a 30-field scalar hardware-control record per function.

The schema is small and regular: a fixed **9 top-level keys**, and a `profile_meta_data[]` array of **exactly 30 fields** per function. Both counts are invariant across all 35 shipped sets (21 `pwp_bin_trainium` + 14 `pwp_bin_with_ln`). The two index maps that every prior reverse-engineering pass deferred — `func_exp_to_bkt_start_idx` and `func_exp_to_ctl_start_idx` — are decoded here: they are **per-octave** region-base tables whose list length encodes the function's symmetry class (2 = asymmetric, 1 = symmetric/pos-only, 0 = the runtime-alpha `parametric_relu`).

The consumer story is narrower than the manifest's richness suggests. There is no C++ function symbol named `FindActInfo`, and the per-set `profile_json`'s keys (`profile_meta_data`, `func_exp_to_*`, …) appear in **zero** decompiled native bodies. What *does* exist is (a) a Cython module `FindActInfo.cpython-3XX.so` that merely **locates the roster file** `act_info.json` on disk, and (b) `NeffFileWriter::updateFromActJsonFile` in `libwalrus`, which matches the module's used set **by name** against the roster and embeds that set's **binary** blobs into the NEFF. Neither reads the per-set `profile_json`. The manifest is a build-time / simulator artifact — its denormalized control fields are baked into the blobs offline, and the runtime addresses functions by `func_id` against the resident tables, not by re-parsing JSON. See [§5](#5-the-consumer-findactinfo-the-roster-locator-and-updatefromactjsonfile) and the [GOTCHA](#findactinfo-gotcha) there.

### What a reimplementer must reproduce

- The **9-key** top-level schema of `<set>.json` and the meaning of each key ([§1](#1-the-9-key-top-level-schema)).
- The **30-field** `profile_meta_data` per-function record, field-by-field with types, and its mapping back to the per-function `pwp_jsons` schema ([§3](#3-the-30-field-profile_meta_data-record)).
- The two index granularities: `func_to_*_start_idx` (coarse per-function base) and `func_exp_to_*_start_idx` (fine per-octave base), including the list-length-encodes-symmetry rule and the constant per-function pos/neg delta ([§4](#4-the-index-maps-func_to_-and-func_exp_to_)).
- The join keys that wire the roster, the per-set profile, the per-function profile, and the two blobs together ([§2](#2-combined-how-n-functions-share-one-image)).
- What the native consumer actually reads (the roster name + the binary blobs) versus what it ignores (the entire per-set profile) ([§5](#5-the-consumer-findactinfo-the-roster-locator-and-updatefromactjsonfile)).

### At a glance

| | |
|---|---|
| **Artifact** | `neuronxcc/pwp/pwp_bin_{trainium,with_ln}/<set>.json` |
| **Set count** | 21 (trainium) + 14 (with_ln) = 35 per-set profiles |
| **Top-level keys** | exactly **9** (identical across all 35 sets) |
| **`profile_meta_data` fields** | exactly **30** per function (identical across all 35 sets) |
| **Funcs per set** | = `len(profile_meta_data)` = `|func_to_bkt_start_idx|` = roster `act:{}` size (exp set: 15) |
| **Roster (tier above)** | `act_info.json` — `{pwp_file_keys, act_func_sets[]}`; [§10.x catalog](act-function-catalog.md) |
| **Blobs (tier below)** | `<set>_bkt.bin` (32 B/entry), `<set>_ctrl.bin` (packed u32/octave); [§10.4](bkt-ctrl-blob.md) |
| **Numerics (tier above)** | `pwp_jsons/<func>_Np.json` — 24-key polynomial profile; [§10.1](pwp-model.md) |
| **Native consumer** | `neuronxcc::backend::NeffFileWriter::updateFromActJsonFile` @ `0x1542db0` (`libwalrus`) |
| **Compile-time roster reader** | `neuronxcc::backend::LowerPWPImpl::fillAllActInfos` @ `0x115af90` (`libwalrus`) |
| **Roster file locator** | Cython `FindActInfo.cpython-3XX.so` → `findActInfoFile` / `findDVEInfoFile` |
| **NEFF-pack failure code** | error code `1228` (`matchedActInfoJson.has_value()` assert) |
| **Evidence** | the shipped JSON (all schema and value claims); the `libwalrus` IDA sidecar; `nm`/`strings` on `FindActInfo.so` |

---

## 1. The 9-key top-level schema

`jq keys` on any `pwp_bin_{trainium,with_ln}/<set>.json` gives **exactly** these 9 keys. The key set is verified identical across all 21 trainium + 14 with_ln per-set profiles (`for f in *.json; do jq -c 'keys|sort' "$f"; done | sort -u` yields a single line).

| Key | Type | Role |
|---|---|---|
| `bkt_bin` | string | Bucket/coefficient blob reference, e.g. `"exp_and_others_bkt.bin"`. The 32-byte-entry poly-coeff blob ([§10.4](bkt-ctrl-blob.md)). |
| `bkt_entry_cnt` | int | Number of 32-byte entries in `bkt.bin`. exp set = **941**. `bkt_entry_cnt × 32` = `bkt.bin` byte size. |
| `ctl_bin` | string | Control/octave blob reference, e.g. `"exp_and_others_ctrl.bin"`. |
| `ctl_entry_cnt` | int | Number of packed-u32 octave words in `ctrl.bin` (pos + neg, summed over funcs). exp set = **89**. |
| `func_to_bkt_start_idx` | object | `{ func(bare) → int }` — the `bkt` entry index where the function's whole block begins ([§4a](#4a-func_to__start_idx--the-coarse-per-function-base)). |
| `func_to_ctl_start_idx` | object | `{ func(bare) → int }` — the `ctrl` entry index where the function's octave block begins. |
| `func_exp_to_bkt_start_idx` | object | `{ func → { exponent(str) → [int,…] } }` — per-octave region bases into `bkt` ([§4b](#4b-func_exp_to__start_idx--the-fine-per-octave-map)). |
| `func_exp_to_ctl_start_idx` | object | `{ func → { exponent(str) → [int,…] } }` — same, into `ctrl`. |
| `profile_meta_data` | array | `[ per-func 30-field scalar HW-control object ]` ([§3](#3-the-30-field-profile_meta_data-record)). |

> **QUIRK — the three-way `ctrl`/`ctl` spelling skew.** The control blob is referred to by **three different spellings** in three places: the roster `act_info.json` uses key `ctrl_bin` (with the `r`); the per-set profile uses key `ctl_bin` (no `r`); and the actual file is named `<set>_ctrl.bin`. In `exp_and_others.json` the `ctl_bin` value is the string `"exp_and_others_ctrl.bin"`, while `act_info.json`'s set entry has key `ctrl_bin`. A reader keying off the wrong spelling will silently miss the field. **The file-reference string value is authoritative** — follow it, not the key name.

> **NOTE — no version field on the per-set profile.** `pwp_bin_*/version.json` ships separately as `{"KaenaPWP":{"brazil_version":"","git_sha":""}}` (both fields blank, 80 bytes, both targets). The per-set profiles carry no version; provenance is owned by the roster/version file, not the manifest.

---

## 2. "Combined": how N functions share one image

The word *combined* in "per-set combined profile" means a single loadable set fuses **multiple** activation functions into one blob pair. The number of fused functions is the same everywhere:

```text
len(profile_meta_data)  ==  |func_to_bkt_start_idx|  ==  |func_to_ctl_start_idx|
                        ==  act_info.json act:{} size for that set
```

For the `exp_and_others` set this is **15**: the hero `exp`, plus `tanh`, plus the 13 always-resident "universal" functions (`identity`, `relu`, `leaky_relu`, `parametric_relu`, `abs`, `sign`, `memset_zero`, `derivative_relu`, `derivative_leaky_relu`, `derivative_identity`, `is_finite`, `square`, `copy`). Every co-resident function gets exactly one `profile_meta_data` entry, one `func_to_*` index, and one `func_exp_to_*` octave map.

### Blob layout (the index points into these)

The two blobs are **concatenations of per-function blocks**, in act-array order; the per-set JSON is the index that locates each block:

```text
bkt.bin  = [ func0_LUT_sections | func0_4_sat | func1_LUT | func1_4_sat | … ]
           func_to_bkt_start_idx[f] = entry index where func f's whole block starts.
           f's 4 saturation entries sit at the END of f's block, pointed at ABSOLUTELY
           by profile_meta_data[f].{pos,neg}_{small,large}_signal_pwl_control (§3F).

ctrl.bin = [ func0_neg_octaves | func0_pos_octaves | func1_octaves | … ]
           func_to_ctl_start_idx[f] = entry index where f's octave block starts
                                    = profile_meta_data[f].pwl_control_base_neg (§3E).
           f's pos octaves start at  profile_meta_data[f].pwl_control_base_pos.
```

Verified `func_to_bkt_start_idx` for the exp set: `exp→0, identity→781, copy→785, leaky_relu→789, parametric_relu→793, abs→797, sign→801, memset_zero→805, relu→809, tanh→813, derivative_relu→921, derivative_leaky_relu→925, derivative_identity→929, is_finite→933, square→937`. So `exp`'s block spans `0..780` (781 entries = 777 LUT sections + 4 saturation clamps); each `lut_size=0` resident occupies just 4 entries (its saturation-only block); `square`'s block is `937..940`, and `937 + 4 = 941 = bkt_entry_cnt`. ✓

> **NOTE — self-contained set invariant.** Every index inside a per-set profile is **absolute within that set's own blobs** — `func_to_*_start_idx`, `func_exp_to_*` values, `pwl_control_base_*`, and the four saturation `pwl_control` pointers all index *this* set's `bkt`/`ctrl`. A loaded set needs nothing else: `{bkt blob} + {ctrl blob} + {per-func scalar meta}` is complete. This is why the *same* function's `bkt` block is byte-identical across sets and targets — only the co-residency order and the `ctrl` stride differ; the per-set profile just re-indexes shared per-function data.

### The pos/neg region split

A function that stores **both** a positive and a negative table (asymmetric, `symmetry_opt_en=0` — `exp`, `softplus`, the derivatives) splits its block into a *neg* sub-block then a *pos* sub-block: `pwl_control_base_pos > pwl_control_base_neg`. A function that stores **one** table — symmetric (`symmetry_opt_en=1`: `tanh`, `sign`, `sigmoid`, …) or pos-only domain (`sqrt`, `reciprocal`, `ln`) — has `base_pos == base_neg`. The per-octave maps ([§4b](#4b-func_exp_to__start_idx--the-fine-per-octave-map)) encode this split octave by octave.

### Join keys (how the maps cross-reference)

| Map | Key form | Example |
|---|---|---|
| `act_info.json` `act:{}` | **bare** func name | `"exp"`, `"tanh"`, `"sigmoid"` |
| `func_to_*` / `func_exp_to_*` | **bare** func name | `"exp"`, `"tanh"` |
| `profile_meta_data[].func_name` | **budget-suffixed** name | `"exp_400p"`, `"tanh_4p"`, `"sigmoid_40p"`, `"identity_1p"` |

The budget-suffixed `func_name` is the `pwp_jsons/<func>_Np.json` filename stem — the link back to the per-function numerics ([§10.1](pwp-model.md)). The bare↔suffixed join is a prefix match (strip the trailing `_<N>p`).

---

## 3. The 30-field `profile_meta_data` record

`profile_meta_data` is an array with one entry per co-resident function, in act-array order. `jq '[.profile_meta_data[]|keys]|unique'` returns **exactly 30 keys**, identical across all 35 sets. The record is a denormalized, silicon-register re-expression of each function's `pwp_jsons` scalar config — the data the Activation engine programs into per-function control registers when the set is loaded.

The fields below are grouped by role; values are taken verbatim from the shipped `exp_400p` (asymmetric LUT) and `sigmoid_40p` (point-symmetric, store-neg) entries.

> **GOTCHA — every `f*_result` / `*_bound` / `symmetry_point` / `fma_const_*` is a raw IEEE-754 `uint32` bit pattern, NOT a float.** The JSON value is the integer reinterpretation of the 32-bit float. `fpinf_result: 2139095040` is `0x7f800000` = `+inf`; `fzero_result: 1065353216` is `0x3f800000` = `1.0`. A reimplementer must `bit_cast<float>` these, never read them as decimals. The two `*_exp_threshold` / `*_mantissa_threshold` saturation fields are the exception — those are **true integers**, compared directly against the input's exponent/mantissa.

### A. Identity (2 fields)

| Field | Type | Meaning |
|---|---|---|
| `func_name` | string | `"<func>_<N>p"` (budget-suffixed). Join key to the per-function profile ([§2](#join-keys-how-the-maps-cross-reference)). |
| `func_id` | int | **The silicon LUT code** = `neuron_id` = `sunda_id` ([§10.1](pwp-model.md) crosswalk). This is how an `Activation` op (opcode `0x21`) names its function; the engine resolves it against the resident set's tables. Verified: `identity=1, relu=2, leaky_relu=3, parametric_relu=4, sigmoid=5, tanh=6, exp=7, square=30, sign=31, abs=33, is_finite=35, copy=128, memset_zero=130`. |

### B. Special-value results (4 fields) — raw `uint32` bit patterns

| Field | Maps to `pwp_jsons` | exp value | sigmoid value |
|---|---|---|---|
| `fnan_result` | `nan_result` | `2143289344` (`0x7fc00000`, qNaN) | same |
| `fpinf_result` | `pinf_result` | `2139095040` (`+inf`, `e^+inf`) | `1065353216` (`1.0`, `sigmoid(+inf)`) |
| `fninf_result` | `ninf_result` | `0` (`e^-inf`) | `0` (`sigmoid(-inf)`) |
| `fzero_result` | `zero_result` | `1065353216` (`1.0` = `e^0`) | `1056964608` (`0.5` = `sigmoid(0)`) |

The per-set profile renames the per-function keys with an `f` prefix and flattens the per-function 6-field float-decomposition box down to its bare `.int` member ([§10.1](pwp-model.md)).

### C. Symmetry fold (4 fields)

| Field | Type | Maps to `pwp_jsons` | Meaning |
|---|---|---|---|
| `symmetry_opt_en` | int 0/1 | `symmetry_en` | Master fold enable. |
| `sym_invert_sign_point` | int 0/1 | `symmetry_invert_sign_opt` | Odd reconstruction `r := −r`. |
| `symmetry_opt_use_neg_region` | int 0/1 | `symmetry_opt_use_neg_region` | Evaluate the fold on the stored **neg** table (`sel = −|v|`). |
| `symmetry_point` | uint32 | `symmetry_point` | Post-fold additive offset (raw f32 bits). |

Shipped values: `exp = {0,0,0,0}` → asymmetric, store both regions, evaluate signed `v`. `sigmoid = {1,1,1, 1065353216}` → point-symmetric: store **neg**, evaluate `sel = −|v|`, negate, add `1.0` ⇒ `sigmoid(x) = 1 − sigmoid(−x)`. `tanh = {1,1,0,0}` → odd about origin: store pos, evaluate `|v|`, negate.

### D. Input / octave bias (5 fields)

| Field | Type | Meaning |
|---|---|---|
| `imm_bias` | int 0/1 | Function takes an immediate additive bias (true on `copy`/`memset_zero`/`is_finite`/`reciprocal`). = `pwp_jsons.imm_bias`. |
| `exp_offset` | int | The **lowest** octave (smallest unbiased exponent) the function covers = `pwp_jsons.exponent_offset` = the first key of `func_exp_to_*[func]`. Verified: `exp=-19`, `sigmoid=-4`, `sqrt=-116`, `reciprocal=-42`, `tanh=-14`. `lut_size=0` residents use the sentinel `-127` (single degenerate octave); `parametric_relu` uses `128`. |
| `fma_const_0` | uint32 | Post-poly FMA constant 0 (raw f32). Nonzero **only** for `parametric_relu` (`=1.0`); `0` elsewhere. = `pwp_jsons.fma_const0`. |
| `fma_const_1` | uint32 | FMA constant 1 (raw f32). `0` on all observed functions. = `pwp_jsons.fma_const1`. |
| `fma_indirection_src_sel` | int | FMA source selector ∈ `{0,2}`. `==2` **uniquely** for `parametric_relu` (its slope α is a register operand, not a LUT coefficient). `0` for every other function. **Not present in the per-function `pwp_jsons` schema** — added at set-build time. |

> **QUIRK — `fma_indirection_src_sel=2` is the runtime-α tell.** `parametric_relu` is the sole no-LUT path: it has an empty `func_exp_to_*` map, `exp_offset=128`, and `fma_const_0=1.0`. The `fma_indirection_src_sel=2` selects the α/bias indirection register so the slope is supplied at runtime. *(Meaning of the `2` is INFERRED from the prelu-only pattern; the exact silicon semantics are not in the wheel.)*

### E. PWL region pointers (2 fields) — absolute `ctrl.bin` octave-block bases

| Field | Type | Meaning |
|---|---|---|
| `pwl_control_base_pos` | int | `ctrl` entry index where the function's **pos**-region octaves start. |
| `pwl_control_base_neg` | int | `ctrl` entry index where the function's **neg**-region octaves start. `== func_to_ctl_start_idx[func]`. |

Verified: `exp` base_neg=0 / base_pos=26 (asymmetric: neg block first); `tanh` 64/64 (symmetric, single region); `sigmoid` 28/39 (store-neg). The delta `base_pos − base_neg` equals the per-octave `ctl` map element pair delta ([§4b](#4b-func_exp_to__start_idx--the-fine-per-octave-map)).

### F. Saturation clamp thresholds + pointers (12 fields) — the 4-way clamp

Four signal regions `{small,large} × {pos,neg}`. The `*_signal_exp_threshold` and `*_mantissa_threshold` are **true integers** (exponent/mantissa comparison thresholds); the `*_signal_pwl_control` fields are **absolute `bkt` indices** of the clamp segment.

| Field | Type | Meaning |
|---|---|---|
| `small_pos_signal_exp_threshold` | int | Biased-exp threshold below which `+input` is "small" (near-origin clamp). |
| `pos_small_signal_pwl_control` | int | `bkt` index of the `+small` clamp segment. |
| `small_neg_signal_exp_threshold` | int | Neg-side small threshold. |
| `neg_small_signal_pwl_control` | int | `bkt` index of the `−small` clamp segment. |
| `large_pos_signal_exp_threshold` | int | Biased-exp threshold above which `+input` is "large" (saturate). |
| `large_pos_signal_mantissa_threshold` | int | Mantissa tie-break at `exp == threshold`. |
| `pos_large_signal_pwl_control` | int | `bkt` index of the `+large` clamp segment. |
| `large_neg_signal_exp_threshold` | int | Neg-side large threshold. |
| `large_neg_signal_mantissa_threshold` | int | Neg-side mantissa tie. |
| `neg_large_signal_pwl_control` | int | `bkt` index of the `−large` clamp segment. |

The four `pwl_control` pointers are **consecutive** (`n, n+1, n+2, n+3`) in `bkt` order `[pos_small, neg_small, pos_large, neg_large]` — they index the 4 saturation entries appended at the end of the function's `bkt` block ([§2](#blob-layout-the-index-points-into-these)). Verified for `exp`: `pos_small@777, neg_small@778, pos_large@779, neg_large@780` (the 4 clamps after exp's 777-section LUT), with `large_pos_signal_exp_threshold=133, large_pos_signal_mantissa_threshold=3240472, large_neg_signal_mantissa_threshold=4362891`. The map back to `pwp_jsons.saturation_points`: `small ↔ *_low`, `large ↔ *_high`; `exp_threshold ↔ sat_point`; `mantissa_threshold ↔ mantissa_point`.

> **NOTE — `small_*` field counts as 4, not 6.** Group F is 12 fields total because the *small* regions have no `mantissa_threshold` (only `exp_threshold` + `pwl_control`), while the *large* regions add a `mantissa_threshold`. So: small×2 = 4 fields, large×2 = 6 fields, plus the 2 small pointers = 12. (Field arithmetic: A2 + B4 + C4 + D5 + E2 + F12 + G3 = **30**. ✓)

### G. Bounds + multipass (3 fields)

| Field | Type | Meaning |
|---|---|---|
| `lower_bound` | uint32 | Smallest valid input (raw f32 bits). `exp = 4286578687` = `0xff7fffff` = `−FLT_MAX`. |
| `upper_bound` | uint32 | Largest valid input. `exp = 2139095039` = `0x7f7fffff` = `+FLT_MAX`. |
| `use_multipass` | bool | `true` only for the 2-pass `ln` variants; `false` elsewhere. |

---

## 4. The index maps: `func_to_*` and `func_exp_to_*`

Two granularities address the blobs. The coarse one locates a function; the fine one locates one octave of one function. The fine maps are decoded here directly from the shipped JSON — they are not described anywhere in the binary.

### 4a. `func_to_*_start_idx` — the coarse per-function base

```text
func_to_bkt_start_idx[f] = bkt entry index where func f's WHOLE block begins.
func_to_ctl_start_idx[f] = ctrl entry index where f's octave block begins
                         = profile_meta_data[f].pwl_control_base_neg.
```

In act-array order the blocks are contiguous, so `func_to_bkt[f_{i+1}] − func_to_bkt[f_i]` is `f_i`'s total block size (LUT sections + 4 saturation). The exp-set `func_to_ctl_start_idx` is `exp→0, identity→52, copy→54, leaky_relu→56, parametric_relu→58, abs→58, sign→60, memset_zero→61, relu→62, tanh→64, …, square→88` (and `ctl_entry_cnt=89` = `88 + 1`).

### 4b. `func_exp_to_*_start_idx` — the fine per-octave map

```text
func_exp_to_{bkt,ctl}_start_idx[func][exponent] = [int, …]
```

Keyed by the input's **unbiased exponent octave** (a string, e.g. `"-19"` … `"6"` for `exp`). The value is a **list** of region start indices for that octave. The list length encodes how many physical regions the function stores — i.e. its symmetry class:

| `listlen` | Meaning | Which funcs (exp set) |
|---|---|---|
| **2** | `[neg_region_idx, pos_region_idx]` — asymmetric (`symmetry_opt_en=0` **and** `base_pos ≠ base_neg`); both tables stored. | `exp`, `identity`, `copy`, `leaky_relu`, `abs`, `relu`, the `derivative_*` residents. |
| **1** | `[single_region_idx]` — one region: either symmetric (`en=1`: `tanh`, `sign`, `memset_zero`, `is_finite`, `square`, `sigmoid`) or pos-only domain (`en=0` but `base_pos == base_neg`: `sqrt`, `reciprocal_sqrt`, `ln`, `reciprocal`). | `tanh`, `sign`, `square`, … |
| **0** (`{}`) | No octave map — `parametric_relu` (runtime-α, no LUT; `fma_indirection_src_sel=2`). | `parametric_relu` |

> **GOTCHA — the `_exp` in `func_exp_to_*` is the float *exponent*, not an expansion over precision budgets.** The map is keyed by the input exponent octave, and its list elements are the neg/pos region split. No shipped function carries multiple budgets within one set, so there is nothing for a per-budget reading to index.

**Decoded `bkt` map** — the value is the `bkt` section base for that octave (the same datum `ctrl.bin` bits `[0:11]` pack, pre-resolved). For `exp` (asymmetric, 2-element):

```text
func_exp_to_bkt_start_idx.exp["-19"] = [0,   406]
                              ["-1"]  = [18,  424]
                              ["0"]   = [20,  426]
                              ["6"]   = [272, 678]
```

- `element[0]` = cumulative `bkt` offset of octave E **within the neg block** (octaves `-19..-1` each have `nsec=1` ⇒ +1 each: `-19→0 … -1→18`; then density doubles toward the origin as `num_sections = 2^extract_size` grows — [§10.1](pwp-model.md)).
- `element[1] = element[0] + 406`. The neg block spans 406 `bkt` entries; the pos block starts at `+406`. **This delta is constant across all octaves of the function** (it equals the neg block size). Verified: every exp octave has `pos − neg == 406`.

**Decoded `ctl` map** — the value is the octave's own `ctrl`-word index (one `ctrl` word per octave):

```text
func_exp_to_ctl_start_idx.exp["-19"] = [0,  26]
                              ["-1"]  = [18, 44]
                              ["0"]   = [19, 45]
                              ["6"]   = [25, 51]
```

- `element[0] = E − exp_offset` (one consecutive `ctrl` word per neg octave: `-19→0 … 6→25`).
- `element[1] = element[0] + 26`, where `26 = pwl_control_base_pos − pwl_control_base_neg`. Constant per function.

So the two constant deltas have clean meanings: **the `ctl` delta = `base_pos − base_neg`; the `bkt` delta = the neg LUT block size.** Both hold constant across every octave of a function.

**Single-region (`listlen=1`) examples (verified):** `func_exp_to_bkt.tanh["-14"]=[813]`, `func_exp_to_ctl.tanh["-14"]=[64]` (one index, the pos region; evaluated on `|v|`, negated). `func_exp_to_bkt.tanh["-13"]=[814]`, `[65]` (octaves advance by one). `parametric_relu`'s map is `{}` in both `func_exp_to_bkt` and `func_exp_to_ctl`.

> **NOTE — why two granularities.** `func_to_*_start_idx` gives the function's coarse block base (used to locate the function plus its 4 saturation entries). `func_exp_to_*_start_idx` gives the per-octave base — the same information `ctrl.bin` holds in packed form, but pre-decoded into JSON so a build tool or simulator can read it without unpacking the `u32`. The `bkt` variant pre-resolves the section base; the `ctl` variant gives the octave's `ctrl`-word slot. The first key of `func_exp_to_*[f]` always equals `exp_offset` ([§3D](#d-input--octave-bias-5-fields)) = `pwp_jsons.exponent_offset`.

---

## 5. The consumer: "FindActInfo," the roster locator, and `updateFromActJsonFile`

The natural expectation is a runtime lookup — call it "find the act info" — that walks an op's activation function → its profile entry → the blobs. **No such C++ function exists, and nothing native reads the per-set `profile_json`.** Three distinct mechanisms realize the idea between them, and none parses the manifest documented above.

<a id="findactinfo-gotcha"></a>
> **GOTCHA — `FindActInfo` is a Python file-locator, not a profile reader; and there is no C++ symbol of that name.** A symbol sweep finds **zero** native functions named `FindActInfo` (no `_Z…FindActInfo…E`, nothing in any IDA `functions.json`/`names.json`). What *does* ship is a Cython extension `neuronxcc/driver/jobs/support/FindActInfo.cpython-3XX.so`. `nm -C` on it exposes only `PyInit_FindActInfo`, `findActInfoFile`, and `findDVEInfoFile`. Its **entire** data-string vocabulary is `act_info.json`, `dve_info.json`, `pwp_bin`, `dve_bin_gen{2,3,4,5}`, and the error `"Unable to locate act_info.json in …"`. It references **none** of the per-set profile keys — `strings … | rg 'profile_meta_data|func_exp_to|bkt_entry_cnt|ctl_entry_cnt'` returns nothing. **`FindActInfo` locates the roster file on disk; it never opens a per-set profile.**

### 5a. Compile-time lookup — `fillAllActInfos` (reads the roster, not the profile)

`neuronxcc::backend::LowerPWPImpl::fillAllActInfos` @ `0x115af90` (`libwalrus`) parses `act_info.json` — its decompiled body reads the literals `act_func_sets`, `name`, `act`, `tables`, `binary` — and builds an `AllActSetName2ActInfo` map (`llvm::MapVector<string, ActFuncSetInfo, …>`). The op-set lookup is `generateInstLoadActFuncSet` @ `0x1156510` doing `AllActSetName2ActInfo[setName]` → an unsigned set index → instruction field `+0xF0`. This resolves *which set* an op's activation function belongs to. It reads **only the roster** `act_info.json`. The per-set profile's keys (`profile_meta_data`, `bkt_entry_cnt`, `func_exp_to_*`) appear in **zero** decompiled bodies across the corpus.

> **GOTCHA — the per-set `profile_json` is never parsed by the compiler's lowering path.** It is a build-time / simulator artifact. The numeric simulator reads the per-function `pwp_jsons` ([§10.1](pwp-model.md)); the per-set profile is the blob **manifest**, consumed at NEFF-pack time ([§5b](#5b-neff-pack--updatefromactjsonfile)) and by the offline table generator that produced the blobs. Its 30 denormalized control fields are baked into `bkt`/`ctrl` offline, not read at op time.

### 5b. NEFF-pack — `updateFromActJsonFile`

`neuronxcc::backend::NeffFileWriter::updateFromActJsonFile` @ `0x1542db0` (`libwalrus`; 707-line decompiled body) is the actual "find the act info and embed it" routine. Annotated pseudocode of what it reads — the **roster name** and the **binary blobs**, never the profile:

```c
// NeffFileWriter::updateFromActJsonFile @ 0x1542db0  (libwalrus)
// Embeds the module's USED activation set's binary tables into the NEFF BOM.
void NeffFileWriter::updateFromActJsonFile(const std::string &actJsonPath,
                                           const std::string &usedActSet) {
    // (1) Load the act JSON (the ROSTER, act_info.json). Loaded via bir::loadJsonFile.
    json root = bir::loadJsonFile(actJsonPath);            // lines 128 / 152

    // (2) Walk act_func_sets[], matching each set's "name" node against usedActSet.
    std::optional<json> matchedActInfoJson;               // the "FindActInfo" result
    for (json &set : root["act_func_sets"]) {
        json probe;  strcpy(probeKey, "name");            // line 327 builds {"name": …}
        if (operator==(set["name"], usedActSet)) {        // nlohmann ==, line 391
            matchedActInfoJson = set;                     // capture on match
            break;
        }
    }

    // (3) GATE: no matching set name → hard error. errorcode 1228.
    //     assert matchedActInfoJson.has_value(); else NeuronAssertion<ErrorCode>(…1228…)
    //     cause/resolution strings keyed on "usedActSet"  (lines 276 / 320 / 321)
    if (!matchedActInfoJson.has_value())
        throw_with_nested(NeuronAssertion(1228, /* "Unable to find used act set" */));

    // (4) For each entry whose value is the nlohmann BINARY tag (type 8) — the bkt/ctrl
    //     blobs — read the blob and add it to the NEFF Bill-of-Materials.
    for (auto &[key, val] : matchedActInfoJson->items())
        if (val.type() == json::value_t::binary)          // type 8
            this->addToBom(key, val.get_binary());        // lines 180 / 647

    this->actTablesEmbedded = 1;                          // *(v60+212) = 1, line 699
}
```

What the body establishes, key by key:

- **Match key** = the set **name** (string), compared against `usedActSet` (the set the compiled module actually used). On mismatch the routine throws with error code **1228** — a loud, deterministic failure rather than silent fall-through.
- **Payload embedded** = entries tagged `binary` (nlohmann type 8) — the `bkt`/`ctrl` blobs. They are added to the NEFF BOM via `addToBom` so the runtime can DMA-load them on `LoadActFuncSet`. The NEFF feature flag that gates dynamic embedding of these tables is documented in [§12.5](../formats/neff-feature-flags.md).
- **Not read:** `bkt_entry_cnt`, `ctl_entry_cnt`, `func_to_*`, `func_exp_to_*`, or any `profile_meta_data` field. The routine consumes the roster's name + binary tables only.

### 5c. Runtime addressing — no profile read at op time

Once a set is resident, an `Activation` op (opcode `0x21`) names its function by `func_id` (the silicon LUT code = `profile_meta_data[f].func_id`, [§3A](#a-identity-2-fields)), packed into the encoded **bundle wire byte `+0x23`** — the same selector slot the `LoadActFuncSet` uses for its set index ([10.6 §1/§2](loadactfuncset.md)). The engine resolves `func_id` against the **currently-resident** set's tables, located at the `func_to_*_start_idx` offsets baked into the blobs.

> **NOTE — `+0x23` is the bundle wire byte, not an `InstActivation` IR-struct offset.** The func code reaches the engine in encoded-bundle byte `+0x23` (the 64-byte compute word's activation-table/function selector slot); on the in-memory `InstActivation` IR object the `ActivationFunctionType` lives at `+0x90`/`+0x120` ([10.2 §1](act-function-catalog.md), [10.6 §1](loadactfuncset.md)), and the silicon LUT `func_id` is supplied through the 31-entry func-remap at encode time. Do not read `+0x23` as a field offset of the BIR instruction struct.

No per-set `profile_json` is opened at op time: the manifest's job — binding `func_id` to blob offsets plus the 30 control fields — was performed once, offline, when the blobs were generated.

---

## 6. The tie to the blobs: profile = manifest, blobs = packed tables

The per-set `profile_json` and the `bkt`/`ctrl` blobs ([§10.4](bkt-ctrl-blob.md)) are two views of the same data. The profile is the human-readable index; the blobs are the packed tables.

| Profile datum | Blob datum it indexes |
|---|---|
| `profile_meta_data[f].{small,large}×{pos,neg}_signal_pwl_control` | absolute `bkt` indices of `f`'s 4 saturation entries (exp `pos_large@779` = `bkt[779].d0` = `0x7f800000` = `+inf`). |
| `func_exp_to_bkt_start_idx[f][E][0/1]` | the `bkt` section base for octave `E` = the value packed in `ctrl.bin` bits `[0:11]`. The JSON map is the unpacked form. |
| `func_to_ctl_start_idx[f]` / `pwl_control_base_neg` | the `ctrl` entry index where `f`'s octave words begin; each word's `{bkt base, extract_lsb, extract_size}` drives the two-level LUT. |
| `bkt_entry_cnt` / `ctl_entry_cnt` | blob sizes: `bkt_entry_cnt × 32` = `bkt.bin` size; `ctl_entry_cnt × {32 trn / 16 wln}` = `ctrl.bin` size. |

A reimplementer needs both: the per-set profile (this page) gives the function→offset + control metadata and the symmetry/special/saturation semantics; the blobs give the raw 32-byte poly coefficients and the packed octave words. The Activation engine loads the blobs (`LoadActFuncSet`, opcode `0x23`) and programs the per-function control registers from the profile's scalar fields, denormalized at build time into the engine's control microcode.

---

## 7. Evidence summary

| Claim | Confidence | Basis |
|---|---|---|
| Per-set profile = exactly 9 keys, identical across all 35 sets | CERTAIN | `jq keys` over all 21+14 sets → single sorted key-set. |
| `profile_meta_data` = exactly 30 fields/entry, identical across all 35 sets | CERTAIN | `jq '[…|keys]|unique'` = 30 names; verbatim exp/sigmoid values. |
| `func_id == neuron_id ==` silicon LUT code | CERTAIN | shipped values match the [§10.1](pwp-model.md) crosswalk (exp=7, sigmoid=5, tanh=6, …). |
| `ctrl`/`ctl`/`_ctrl.bin` three-way spelling skew | CERTAIN | roster key `ctrl_bin`, profile key `ctl_bin`, value `"…_ctrl.bin"`. |
| `func_exp` listlen = stored-region count; deltas constant per func | CERTAIN | exp `[0,406]`/`[272,678]` (Δ406), ctl `[0,26]`/`[25,51]` (Δ26); tanh `[813]`/`[64]`; prelu `{}`. |
| No native `FindActInfo` symbol; the per-set profile is never parsed natively | CERTAIN | symbol sweep → 0 hits; profile keys in 0 decompiled bodies; `FindActInfo.so` is a Cython roster locator. |
| `updateFromActJsonFile` matches by name → asserts (1228) → embeds `binary` blobs | CERTAIN | `libwalrus` IDA body @ `0x1542db0` (707 lines); assert string + line. |
| `fma_indirection_src_sel=2` = prelu α/bias indirection select | MEDIUM | the unique-to-prelu pattern is read directly; the silicon meaning of the value `2` is inferred from it. |
| profile↔blob byte ties (sat pointers, ctrl bkt-base, entry counts) | HIGH | cross-validated against the [§10.4](bkt-ctrl-blob.md) blob decode; not re-byte-validated here. |
| Roster also ships as obfuscated alias `l`, byte-identical | HIGH | the literal `l` file was not present in this extract to re-check. |

> **GOTCHA — the offline table generator is not in the wheel.** The build-time tool that consumes the per-function `pwp_jsons` and emits the per-set profile + `bkt`/`ctrl` blobs ships nowhere in the wheel. The denormalization rules (field renames, the 4-way saturation-pointer packing, the per-octave `func_exp` computation) are **inferred from the output**, not read from the generator. A reimplementer can reproduce the *runtime* and *NEFF-pack* behavior exactly from the shipped artifacts; reproducing the *generator* requires deriving these rules from the patterns documented in [§3](#3-the-30-field-profile_meta_data-record)–[§4](#4-the-index-maps-func_to_-and-func_exp_to_).

---

*Cross-references: the polynomial activation model ([§10.1](pwp-model.md)); the `bkt`/`ctrl` blob layout ([§10.4](bkt-ctrl-blob.md)); NEFF feature flags ([§12.5](../formats/neff-feature-flags.md)).*
