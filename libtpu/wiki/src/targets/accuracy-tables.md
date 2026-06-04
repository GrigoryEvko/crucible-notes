# Per-Gen Accuracy Tables

> *All values, blob offsets, and constants on this page were decoded byte-exactly from the five embedded `accuracy_table_*.binarypb` blobs and the `select_accuracy.cc` consumer functions in `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

Each TPU generation ships an embedded `accuracy_table_<codename>.binarypb` proto blob that records the **numerical accuracy of the hardware transcendental approximations** — `sin`, `cos`, `tan`, `exp`, `log`, `rsqrt`, `sqrt`, `divide`, `erf`, `logistic`, `tanh`, and their fast/accurate variants. These are not raw minimax polynomial coefficients; they are *measured* worst-case relative-error and ULP bounds, plus a piecewise input→error curve, that the compiler consults to decide whether the fast hardware approximation (the XLU/EUP path) meets the user-requested `ResultAccuracy`, or whether it must emit the slower accurate software polynomial.

Five blobs exist — jellyfish (`TPU v2`), dragonfish (`TPU v3`), pufferfish (`TPU v4`), viperfish (`TPU v5`), ghostlite (`TPU v6 lite`); external names are per `TpuVersionToExternalName` (see the [codename matrix](tpu-version-codename-matrix.md)). The newest generation, `6acc60406` (external `TPU7x`), ships **no** accuracy table: its `embed://` lookup fails and the impl-type selector falls through to a warning-driven fallback. (`Trillium`/`Ironwood` are marketing names with **zero** occurrences in the binary — `6acc60406` is the only internal name for this generation.) The first three generations carry byte-identical content (same 181,284-byte blob modulo the version stamp); viperfish and ghostlite each tighten the fast-path bounds by one to four orders of magnitude, in lockstep with the per-gen transcendental cycle-cost improvements.

The decode is reimplementation-grade: all five blobs were carved byte-exact from `.lrodata` and their md5 verified against the `FileWrapper` fingerprints, the proto schema was recovered from message typeinfo plus serializer wire tags, and the four-function consumer chain (`GetTargetAccuracy → GetAccuracyEntry → CompareAccuracy → GetImplType`) was disassembled.

| | |
|---|---|
| **Blobs** | 5 × `accuracy_table_<codename>.binarypb` in `.lrodata`, md5-verified vs `FileWrapper` fp |
| **Format** | raw uncompressed serialized protobuf (`xla.service.jellyfish.accuracy.AccuracyTable`) |
| **Entries / table** | 19 (15 distinct op names; 6 ops carry a fast+accurate variant pair) |
| **Gens with table** | jellyfish, dragonfish, pufferfish, viperfish, ghostlite — **not** 6acc60406 |
| **Version stamp** | proto field 3 = `tpu::TpuVersion + 1` (1..5) |
| **Consumer** | `xla::jellyfish::{GetTargetAccuracy, GetAccuracyEntry, CompareAccuracy, GetImplType}` |
| **Source file** | `platforms/xla/service/jellyfish/select_accuracy.cc` |
| **Confidence** | CONFIRMED unless a cell is annotated otherwise |

---

## Per-Gen Blob Mapping

Each blob is keyed by codename in its `embed://` URI and stamped with `proto field 3 = TpuVersion + 1`. Two version numberings coexist and must not be conflated: the runtime `tpu::TpuVersion` (0-based, read from `Target+0x398`) and the proto-internal field-3 stamp (1-based).

| codename | `tpu::TpuVersion` | proto f3 | blob file | data_va / off | size (B) | md5 (== `FileWrapper` fp) · Conf |
|---|---:|---:|---|---|---:|---|
| jellyfish | 0 | 1 | `accuracy_table_jellyfish.binarypb` | `0x040c8990` | 181,284 | `e3a2a768…d4c42cf4` · CONFIRMED |
| dragonfish | 1 | 2 | `accuracy_table_dragonfish.binarypb` | `0x040703d0` | 181,284 | `4d2efd10…748bc019` · CONFIRMED |
| pufferfish | 2 | 3 | `accuracy_table_pufferfish.binarypb` | `0x040f4dc0` | 181,284 | `e8dbf7b1…a185d4c4` · CONFIRMED |
| viperfish | 3 | 4 | `accuracy_table_viperfish.binarypb` | `0x041211f0` | 181,139 | `354963c9…3746b900` · CONFIRMED |
| ghostlite | 4 | 5 | `accuracy_table_ghostlite.binarypb` | `0x0409c800` | 180,617 | `8acb76b8…fb433612` · CONFIRMED |
| 6acc60406 | 5 | — | — (none) | — | — | no table — fallback · CONFIRMED |

The `embed://` URI is assembled as `embed://accuracy_table_<codename>_memfile/accuracy_table_<codename>.binarypb` from the path pieces `"embed://accuracy_table_"` + codename + `"_memfile/"` + `"accuracy_table_"` + codename + `".binarypb"`; the codename comes from `tpu::TpuVersionToString(version)` (string table @ `0x22011bf0`, `0→jellyfish … 5→6acc60406`, FATAL above 5).

> **GOTCHA —** jellyfish, dragonfish, and pufferfish are byte-identical modulo the field-3 stamp (same 181,284 B, same per-op values, same 1,187-row exp curve) — their XLU/EUP units are the same design. A reimplementer can ship one table for v2/v3/v4 and only needs distinct data for viperfish and ghostlite.

---

## The Proto Schema

```text
message AccuracyTable {                  // the blob
  repeated AccuracyEntry entries = 1;     // tag 0x0a; 19 per table
}
message AccuracyEntry {
  string name           = 1;  // 0x0a  "cosine","exponential",…
  PrimitiveType type    = 2;  // 0x10  always 11 == F32
  int32  version        = 3;  // 0x18  TpuVersion + 1
  int32  variant        = 4;  // 0x20  0 = fast/NoEup, 1 = EUP/accurate
  double max_input      = 5;  // 0x29  domain upper bound
  double worst_rel_err  = 6;  // 0x31  worst-case relative error over domain
  double worst_ulp      = 7;  // 0x39  worst-case ULP over domain
  repeated ErrorInfo curve = 8; // 0x42 piecewise input→error breakpoints
}
message AccuracyEntry.ErrorInfo {
  double input   = 1;  // 0x09  input-magnitude breakpoint
  double rel_err = 2;  // 0x11  relative error guaranteed at/below
  double ulp     = 3;  // 0x19  ULP bound at/below
}
```

`type` (field 2) is uniformly **11 = `F32`** on every entry: the XLU computes in F32 internally regardless of bf16 I/O, so one entry governs the bf16-cast variant too. The `curve` (field 8) is monotone — as `|input|` grows away from 0/subnormal, the approximation's relative error shrinks; `worst_rel_err`/`worst_ulp` are the values at the smallest-input breakpoint.

The 19 entries (15 op names) are: trig `cosine`, `sine`, `tan`; exp family `exponential` (×2), `exponential-minus-one` (×2); log family `log` (×2), `log-plus-one` (×2); roots/division `rsqrt`, `sqrt`, `divide`; special/activation `erf`, `logistic` (×2), `tanh` (×2). The six dual-variant ops carry both a variant-0 (fast / "NoEup" XLU) and a variant-1 (high-precision "EUP") entry. These match the StableHLO ops that carry a `result_accuracy` attribute (`vhlo.cosine_v2`, `vhlo.exponential_v2`, …). There is no `cbrt` entry (binary string: `"Accuracy analysis for cbrt is not implemented yet."`).

---

## Cross-Gen Accuracy Constants

Top-level worst-case **relative error** (field 6) per `(op, variant)` — lower = more accurate. `j/d/p` = jellyfish/dragonfish/pufferfish (identical). **Confidence:** every value below is CONFIRMED — read byte-exact from the field-6/field-7 doubles of the carved blobs (the `data_va`s in [Per-Gen Blob Mapping](#per-gen-blob-mapping)); the per-cell source is the proto, not an inference.

| op | var | jf/df/pf | viperfish | ghostlite |
|---|:---:|---:|---:|---:|
| cosine | 0 | 2.38e-07 | 2.38e-07 | 2.38e-07 |
| sine | 0 | 2.38e-07 | 2.38e-07 | 2.38e-07 |
| tan | 0 | 4.77e-07 | 4.77e-07 | 4.77e-07 |
| divide | 0 | 1.0 | 1.0 | 1.0 |
| rsqrt | 0 | 2.38e-07 | 2.38e-07 | 1.19e-07 |
| sqrt | 0 | 2.38e-07 | 2.38e-07 | 2.38e-07 |
| erf | 0 | 4.77e-07 | 9.54e-07 | 2.38e-07 |
| exponential | 0 / 1 | 1.0 / 1.0 | 1.0 / 1.0 | 1.0 / 1.0 |
| exp-minus-one | 0 / 1 | 2.44e-04 / 1.0 | 2.44e-04 / 1.0 | 1.0 / 1.0 |
| log | 0 / 1 | 4.88e-04 / 4.77e-07 | 3.82e-06 / 4.77e-07 | 2.38e-07 / 4.77e-07 |
| log-plus-one | 0 / 1 | 4.88e-04 / 1.19e-07 | 2.44e-04 / 1.19e-07 | 2.44e-04 / 1.19e-07 |
| logistic | 0 / 1 | 4.0 / 1.0 | 2.0 / 1.0 | 1.0 / 1.0 |
| tanh | 0 / 1 | 1.22e-04 / 4.77e-07 | 7.63e-06 / 4.77e-07 | 1.19e-07 / 4.77e-07 |

Top-level worst-case **ULP** (field 7), same layout:

| op | var | jf/df/pf | viperfish | ghostlite |
|---|:---:|---:|---:|---:|
| cosine | 0 | 4 | 4 | 4 |
| sine | 0 | 4 | 4 | 4 |
| tan | 0 | 8 | 8 | 8 |
| divide | 0 | 256 | 64 | 1 |
| rsqrt | 0 | 2 | 2 | 1 |
| sqrt | 0 | 4 | 4 | 2 |
| erf | 0 | 8 | 8 | 1 |
| exponential | 0 / 1 | inf / 1 | inf / 1 | inf / 1 |
| exp-minus-one | 0 / 1 | 2048 / 1.68e7 | 2048 / 1.68e7 | 1.68e7 / 1.68e7 |
| log | 0 / 1 | 4096 / 4 | 64 / 4 | 2 / 4 |
| log-plus-one | 0 / 1 | 4096 / 1 | 4096 / 1 | 4096 / 1 |
| logistic | 0 / 1 | 6.71e7 / 256 | 3.36e7 / 128 | 1.68e7 / inf |
| tanh | 0 / 1 | 2048 / 8 | 128 / 8 | 1 / 8 |

> **NOTE —** `exponential` variant-0 worst-ULP stays `inf` on every generation because the smallest breakpoint sits at a subnormal input where the relative error is 1.0 (100%). The fast `exp` is only usable above the subnormal range; the `worst_*` figures are deliberately the worst-case (smallest-input) values, not the typical accuracy.

---

## Piecewise Error Curves (representative)

The field-8 curve gives per-input-magnitude accuracy. Selected jellyfish (= df = pf) fast-path (variant 0) transitions, by input threshold:

```text
divide:      input>=0         rel=1.0       ulp=256    (subnormal: 100% error)
             input>=2.35e-38  rel=1.53e-05  ulp=128
             input>=4.70e-38  rel=1.19e-07  ulp=1      (normal range: near-exact)
exponential: input>=0         rel=1.0       ulp=inf
             input>=8.97e-44  rel=1.0       ulp=128
             input>=2.35e-38  rel=1.53e-05  ulp=128    (caps at ~bf16 precision)
logistic:    input>=0         rel=4.0       ulp=6.71e+07
             input>=9.54e-07  rel=2.0       ulp=3.36e+07
             input>=3.82e-06  rel=1.0       ulp=1.68e+07
             input>=3.05e-05  rel=4.88e-04  ulp=8192
tan:         input>=0..32 over 7 segments: rel 4.77e-07→5.96e-08, ulp 8→1
```

Ghostlite (best gen) tightens these dramatically, e.g. `divide input>=2.35e-38 → rel=1.19e-07 ulp=1` (vs `1.53e-05/128` on jf); `log input>=1.53e-05 → rel=2.38e-07 ulp=1` (vs `4.88e-04/4096`); `logistic input>=1.19e-07 → rel=1.19e-07 ulp=1` (vs `4.0/6.7e7`).

---

## Consumer Chain — the precision decision

Four functions in `select_accuracy.cc` turn the table into an implementation choice:

1. **`GetTargetAccuracy(HloOpcode, Target&, bool variant)`** @ `0x1d60dea0` — the loader. Reads `Target+0x398` = `tpu_version()`, builds the `embed://` URI via `TpuVersionToString`, calls `tsl::ReadBinaryProto` to parse the blob, then `GetAccuracyEntry`. Returns `StatusOr<AccuracyEntry>`; a failed blob read produces a `Status` via `CreateStatusAndConditionallyLog` at `select_accuracy.cc:143` (the table-missing path taken for `6acc60406`).
2. **`GetAccuracyEntry(string_view name, AccuracyTable&, bool variant)`** @ `0x1d60e040` — linear scan over `table.entries`, matching `(entry.name == name)` (via `bcmp`) **and** the variant byte at `entry+80` (`== variant`). On no match it builds an error `Status` (`MakeErrorImpl<5>`) with `"No accuracy entry found for opcode: "` (`select_accuracy.cc:129`).
3. **`CompareAccuracy(ResultAccuracy&, StatusOr<Entry> fast, StatusOr<Entry> accurate, HloOpcode)`** @ `0x1d60d7a0` — the tolerance check. Reads `ResultAccuracy.mode` (`a2+28`); when mode is TOLERANCE it loads the tolerance struct (`a2+16`, fields at `+0x18`/`+0x20`) and `vucomisd`-compares it against the entry's top-level bound (`entry+0x40`); it short-circuits to the fast (variant-0) entry if that passes, else the accurate (variant-1) entry. If neither status is OK it emits `"Only accurate implementation is available."` (`:33`) or `"Only EUP implementation is available."` (`:35`); if no variant meets the request it returns an error `Status` (`MakeErrorImpl<5>`, `:44`) reading `"No implementation found for requested accuracy: … for opcode: <op>"`.
4. **`GetImplType(Target&, ResultAccuracy&, HloOpcode)`** @ `0x1d60db80` — the master decision. Loads both variants (`GetTargetAccuracy(...,0)` and `(...,1)`) and branches on `ResultAccuracy.mode` (`a3+28`): **DEFAULT** returns the available impl (warning if the preferred one is absent); **HIGHEST** forces the EUP path; **TOLERANCE** (`mode == 2`) calls `CompareAccuracy`. An unhandled mode does **not** abort — it returns an error `Status` (`MakeErrorImpl<3>`, `:79`) reading `"Unsupported result accuracy for opcode <op>: <message>"`.

```text
missing-variant fallback inside GetImplType (also the whole-table-missing case
for 6acc60406, whose embed:// lookup returns NOT_FOUND for both variants):
  EUP entry missing      → WARN "No EUP implementation is available, returning accurate implementation."   (select_accuracy.cc:69)
  accurate entry missing → WARN "No accurate implementation is available, returning default implementation." (select_accuracy.cc:61)
```

> **NOTE —** `CompareAccuracy` (`0x1d60d7a0`) performs **direct `vucomisd` comparisons** of the `ResultAccuracy` tolerance fields against the entry's top-level bound (`entry+0x40`); there is no `ulps × 2^-23` relative-error conversion and no `mulsd`-by-constant in any of the four consumer bodies. `GetImplType` returns an **error `Status`** for an unhandled mode, not a fatal abort. The decompile-confirmed decision logic (CONFIRMED): both `GetTargetAccuracy` calls; the DEFAULT / HIGHEST / TOLERANCE (`mode == 2`) dispatch; the two missing-variant warnings at `:69`/`:61`; and the `"Unsupported result accuracy"` error return at `:79`.

The `optional<ResultAccuracy>` originates from the HLO instruction's `result_accuracy` attribute and threads down into the `LloRegionBuilder` transcendental emitters (e.g. `Sln` @ `0x1d534520` computes `log(x) = Vlog2(F32, x, accuracy) * ln2`), where `GetImplType` chooses EUP vs the `*NoEupF32` software expansion; `DecomposeEupInstruction` @ `0x126a0340` lowers the chosen EUP op.

---

## Accuracy vs Auto-Cast (different layers)

The accuracy table chooses the *transcendental approximation algorithm* (fast XLU/EUP vs accurate software polynomial); it never changes the storage dtype (`type` is always F32). Storage-dtype and matmul-accumulation precision are a separate layer — `AutoMixedPrecision`, `PrecisionConfig`, `MayIncreaseBF16AllReduceAccumulationAccuracy` @ `0x127a22c0`. Both feed the cost model: a fast approx that passes `CompareAccuracy` keeps the op in the cheap XLU path, which is why per-gen accuracy and per-gen transcendental cycle cost improve together (sin/cos 198→154→142, tan 219→170→151 across jf→viperfish→ghostlite). The `compiled_with_accuracy_setting` flag @ `0x22581ff0` (`FEATURE_KIND_RESULTS_ACCURACY_SETTING`) records whether the executable was compiled with explicit accuracy settings, for cache-key/replay.

---

## Related Components

| Name | Relationship |
|---|---|
| `tpu::TpuVersionToString` @ `0x20b3a480` | resolves codename for the `embed://` URI |
| `tsl::ReadBinaryProto` @ `0x20d02060` | parses the embedded blob (re-parsed per call) |
| `LloRegionBuilder::Sln` / `Vlog2` / `*NoEupF32` | the emitters `GetImplType` routes between |
| `DecomposeEupInstruction` @ `0x126a0340` | lowers the chosen EUP transcendental |

## Cross-References

- [Codename Matrix](tpu-version-codename-matrix.md) — the `tpu::TpuVersion` ↔ codename mapping the version stamp keys off
- [Per-Codename Constants](per-codename-hw-constants.md) — the per-generation hardware-constant table (sibling per-gen data source)
- [EUP Correction Coefficients](../cost/eup-correction-coeffs.md) — the EUP-path correction terms the accurate variants invoke
