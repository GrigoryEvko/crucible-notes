# Frontend Precision-Flag Marshalling

> *All symbols, addresses, and string offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/driver/`). Addresses are file offsets in the named `.so`; treat every one as version-pinned. Other wheels relayout the Cython string pool and shift every offset.*

## Abstract

The precision surface of `neuronx_cc` is a small, deliberately redundant set of CLI knobs that all converge on **one** decision the numeric layer reads: *which FP32 operators get cast, and to what dtype.* This page is the CLI-side companion to the Numerics chapter (the precision-policy page, §9.4 — documented in Part 9, pending; and [9.5 AutoCastFP32](../numerics/autocast-fp32.md)). It traces each flag from `argparse` registration, through the one normalization step in `CompileCommand.buildPipeline`, to the exact token string the downstream stage consumes.

The surface lives in **two** Cython modules and is split by audience. The user-facing pair `--auto-cast` (scope: `none`/`matmult`/`all`, default `none`) and `--auto-cast-type` (dtype: `fp16`/`bf16`/`tf32`/`fp8_e4m3`, default `bf16`) is registered in `CompileCommand.__init__` (`0x36290`). The *internal* compact form `--fp32-cast {10 modes}` is registered in the `Frontend` Job's `RegisterArgs` (`0x17ae0`). The two encode the same `(scope, dtype)` pair: `buildPipeline` first **canonicalizes** the user pair (rewriting `tf32→fp32r`, `fp8_e4m3→fp8e4`, and `all+tf32→matmult`), then **reverse-marshals** it back into a single `"fp32-cast-<scope>-<dtype>"` token via a 4-part `_Pyx_PyUnicode_Join` and appends it to the forwarded option list. That token, plus three independent boolean toggles, is what carries the precision decision toward the tensorizer and the native cast.

Two facts shape everything and are easy to get wrong. First, there is **no integer enum** anywhere on this axis — every comparison is a `_Pyx_PyUnicode_Equals` against an interned unicode token, so the *string* is the value all the way down. Second, the documented "`matmult` is the default" describes the *behaviour selected when auto-cast is enabled*, **not** an `argparse default=` literal: the real `argparse` defaults are `(auto_cast='none', auto_cast_type='bf16')`, i.e. *no casting* until the user asks for it.

| | |
|---|---|
| **User scope flag** | `--auto-cast` — `{none, matmult, all}`, default `none` (`CompileCommand.__init__` @ `0x36290`; default `__pyx_n_u_none` @ `0x39a82`) |
| **User dtype flag** | `--auto-cast-type` — `{fp16, bf16, tf32, fp8_e4m3}`, default `bf16` (default `__pyx_n_u_bf16` @ `0x39f0d`) |
| **Internal compact flag** | `--fp32-cast` — 10 modes, `kind=ArgKind.INTERNAL`, no `argparse` default (`Frontend.RegisterArgs` @ `0x17ae0`, choices @ `0x19340`) |
| **Legacy token bag** | `--fast-math` — `nargs='+'`, 16 tokens, default `[]` (`CompileCommand.__init__`, choices @ `0x3a5c9`) |
| **Normalize + reverse-marshal** | `CompileCommand.buildPipeline` @ `0x619d0` (canonicalize `0x6597–0x6801`; `_Pyx_PyUnicode_Join` reverse-marshal `~0x6bc0`) |
| **Boolean toggles** | `--enable-mixed-precision-accumulation` (def **True**), `--enable-saturate-infinity` (def **False**), `--enable-internal-native-int64` (def **False**) — all `EnableDisableArgumentAction`, `Frontend.RegisterArgs` |
| **Native-int64 forward** | `runXLAFrontend` @ `0x1c3e0` appends `enable-native-int64` to `tensorizer_options` when the internal bool is truthy |
| **Not present** | `--disable-inline-cast` / `inline-cast` / `inline_cast` — **absent** from every `driver/` module (negative grep) |

---

## 1. The Two Layers and the Reimplementation Contract

A reimplementer needs to reproduce exactly two things: the *front* (two parsers that fill a namespace) and the *bridge* (`buildPipeline`'s canonicalize-then-re-encode). Everything else is forwarding.

```text
        ┌─── user CLI ───────────────────────────────────┐     ┌─── internal CLI ──────────────┐
        │  --auto-cast       {none,matmult,all}          │     │  --fp32-cast {10 modes}       │
        │  --auto-cast-type  {fp16,bf16,tf32,fp8_e4m3}   │     │  (kind=ArgKind.INTERNAL)      │
        │  --fast-math       [16 tokens]                 │     │  Frontend.RegisterArgs 0x17ae0│
        │  CompileCommand.__init__ 0x36290               │     └───────────────┬───────────────┘
        └───────────────────────┬────────────────────────┘                     │
                                 │  namespace: auto_cast, auto_cast_type        │  (string passed through;
                                 ▼                                              │   never read as attr in
        ┌─── CompileCommand.buildPipeline 0x619d0 ───────┐                      │   jobs/Frontend.so)
        │  1. canonicalize  (0x6597..0x6801):            │                      │
        │       tf32      -> fp32r                        │                     │
        │       fp8_e4m3  -> fp8e4                        │                     │
        │       all + tf32 -> matmult                     │                     │
        │       auto_cast=='none' -> fast_math.append('none')                  │
        │  2. reverse-marshal (~0x6bc0):                  │                     │
        │       tok = "fp32-cast-" + auto_cast + "-" + auto_cast_type           │
        │       options.append(tok)   <-- 4-arg _Pyx_PyUnicode_Join            │
        └───────────────────────┬─────────────────────────┘                    │
                                 ▼                                              ▼
                       "fp32-cast-matmult-bf16"  ==================  same (scope,dtype) string token
                                 │
                                 ▼
                   numeric lowering (Penguin AutoCastFP32 / native cast) reads (scope, dtype)
```

> **GOTCHA — the user pair and the internal `--fp32-cast` are the *same* value in two spellings.** `--fp32-cast matmult-bf16` is identical to `--auto-cast matmult --auto-cast-type bf16`. `buildPipeline` proves this by *rebuilding* the compact form from the pair (§4). There is no precedence subtlety to reverse — they are one knob with two doors.

**Reimplementation contract.** To reproduce the precision front-end you must implement: (a) two `argparse` registrations with the exact choice lists and defaults of §2/§3; (b) the four canonicalization rewrites of §4.1; (c) the `"fp32-cast-<scope>-<dtype>"` re-encode of §4.2; (d) three `EnableDisableArgumentAction` booleans with defaults `True/False/False` (§5); and (e) the single forward in `runXLAFrontend` that turns the internal int64 bool into the `enable-native-int64` tensorizer token (§5.3). Nothing on this axis uses an integer enum, a config file, or an environment variable.

---

## 2. The User Surface — `--auto-cast`, `--auto-cast-type`, `--fast-math`

Owner: `CompileCommand.__init__` (`__pyx_pw_…_14CompileCommand_1__init` @ `0x36290`). These three flags set namespace attributes; nothing in `__init__` interprets them — that is `buildPipeline`'s job (§4).

### 2.1 `--auto-cast` (operator scope) — default `none` [CONFIRMED]

```c
// CompileCommand.__init__, add_argument('--auto-cast', ...)
parser.add_argument(
    "--auto-cast",                       // __pyx_tuple__26
    kind    = ArgKind.PUBLIC,
    choices = ["none", "matmult", "all"],// PyList_New(3) @ 0x39a0d
    default = "none",                    // __pyx_n_u_none -> n_s_default @ 0x39a82  ** default **
    help    = "Automatically cast FP32 operators to a lower-precision type "
              "for increased performance. (Default: %(default)s)");
// => namespace attr `auto_cast`  (the SCOPE selector: which operators)
```

Binary anchors: the default is the disassembled pair `mov rdx, __pyx_n_u_none` (`0x39a7b`) / `mov rsi, __pyx_n_s_default` (`0x39a82`), i.e. `default="none"`. The help string is present verbatim in `.rodata` (`"Automatically cast FP32 operators to a lower-precision type for increased performance."`).

### 2.2 `--auto-cast-type` (target dtype) — default `bf16` [CONFIRMED]

```c
parser.add_argument(
    "--auto-cast-type",                  // __pyx_tuple__27
    kind    = ArgKind.PUBLIC,
    // each choice is a (value, ArgKind.PUBLIC) 2-tuple:
    choices = [("fp16", PUBLIC), ("bf16", PUBLIC),
               ("tf32", PUBLIC), ("fp8_e4m3", PUBLIC)],
    default = "bf16",                    // __pyx_n_u_bf16 -> n_s_default @ 0x39f0d  ** default **
    help    = "When auto-cast mode is enabled, set the lower-precision data type "
              "to which the selected FP32 operations are cast. (Default: %(default)s)");
// => namespace attr `auto_cast_type`  (the DTYPE selector)
```

Binary anchor for the default: `mov rdx, __pyx_n_u_bf16` (`0x39f0d`) immediately preceding `mov rsi, __pyx_n_s_default` (`0x39f14`) and the `PyDict_SetItem` at `0x39f1e` — that `SetItem` writes `default=bf16` into the kwargs dict. The four `tf32` / `fp8_e4m3` doc lines (`"tf32: TensorFloat-32"`, etc.) are in the same `.rodata` block.

> **NOTE — `tf32` and `fp8_e4m3` are *user* spellings that never reach the numeric layer unchanged.** They are aliases rewritten to the canonical `fp32r` / `fp8e4` in §4.1. The four dtypes the cast actually sees are `bf16 | fp16 | fp32r | fp8e4`.

### 2.3 `--fast-math` (legacy multi-token bag) — default `[]` [CONFIRMED]

`--fast-math` is the O-style legacy surface: `nargs='+'`, an empty-list default, and a 16-token choice set that mixes `fp32-cast-*` spellings (an alternate way to spell the same scope/dtype pair) with `fast-relayout-*` spellings (the transpose-engine dtype, a *separate* axis — see [HLO → Native / NKI Kernel Lowering](../hlo-opt/hlo-to-native-kernel-lowering.md)).

```c
parser.add_argument(
    "--fast-math",                       // __pyx_tuple__32
    kind    = ArgKind.INTERNAL,          // .INTERNAL attr fetched @ 0x3a833
    nargs   = "+",                       // __pyx_k__30 = "+"
    default = [],                        // PyList_New(0) @ 0x3a6ec
    choices = [                          // PyList_New(16) @ 0x3a5c9..0x3a697
      "all", "none",
      "fp32-cast-matmult-bf16", "fp32-cast-matmult-fp16",
      "fp32-cast-matmult-fp32r", "fp32-cast-matmult-fp8e4",
      "fp32-cast-matmult", "fp32-cast-all",
      "fp32-cast-all-fp32r", "fp32-cast-all-fp8e4", "fp32-cast-none",
      "fast-relayout", "fast-relayout-fp32", "fast-relayout-fp32r",
      "fast-relayout-fp8e4", "no-fast-relayout"],
    help    = "Controls how the compiler makes tradeoffs between performance and "
              "accuracy for fp32 operations. (Default: %(default)s)");
// => namespace attr `fast_math` (list of tokens)
```

All 16 tokens were confirmed as exact `.rodata` strings in `CompileCommand.so` (`fp32-cast-matmult`, `fp32-cast-all`, `fp32-cast-none`, the four `fp32-cast-matmult-*`, the two `fp32-cast-all-*`, and the five `fast-relayout`/`no-fast-relayout` tokens). The default-empty-list and `nargs='+'` are the disassembled `PyList_New(0)` and `"+"` literal.

> **QUIRK — `--fast-math` carries the `fp32-cast-*` *prefix* spelling; the internal flag carries the *bare* spelling.** A user writes `--fast-math fp32-cast-matmult-bf16`; the internal `--fp32-cast` choice is `matmult-bf16` (no `fp32-cast-` prefix). They name the same mode. `buildPipeline`'s reverse-marshal (§4.2) is what *adds* the `fp32-cast-` prefix back when re-encoding from the canonical pair, so the forwarded token always carries the prefix form regardless of which door the user used.

---

## 3. The Internal Compact Flag — `--fp32-cast` (10 modes)

Owner: the `Frontend` **Job**, `RegisterArgs` (`__pyx_pw_…_8Frontend_13RegisterArgs` @ `0x17ae0`). `kind=ArgKind.INTERNAL`, so it is hidden from `--help`. Crucially the registration dict carries **no `default=`** (it has only `kind`, `choices`, `help`) — at this layer the `argparse` default is `None`; the *effective* default mode (`matmult`) is what the help text documents as the behaviour the pipeline selects, not a literal on this flag.

### 3.1 The 10-mode choice list [CONFIRMED]

`PyList_New(10)` at `0x19340..0x193cd` (items at `rax+0x00..rax+0x48`); all ten verified as interned strings in `Frontend.so`:

| # | mode token | scope of cast | target dtype | transpose dtype | confidence |
|---|---|---|---|---|---|
| 0 | `all` | all fp32 ops | BF16 | BF16 | CONFIRMED |
| 1 | `matmult` *(effective default)* | matmult-engine fp32 | BF16 | **FP16** | CONFIRMED |
| 2 | `matmult-fp16` | matmult + transpose | FP16 | FP16 | CONFIRMED |
| 3 | `matmult-bf16` | matmult + transpose | BF16 | BF16 | CONFIRMED |
| 4 | `matmult-fp32r` | matmult + transpose | FP32r (tf32) | FP32r | STRONG |
| 5 | `matmult-fp8e4` | matmult + transpose (fp) | FP8E4 | FP8E4 | CONFIRMED |
| 6 | `experimental-all-fp16` | all fp32 ops | FP16 | FP16 | CONFIRMED |
| 7 | `all-fp32r` | all fp32 ops | FP32r (tf32) | FP32r | CONFIRMED |
| 8 | `all-fp8e4` | all floating ops | FP8E4 | FP8E4 | CONFIRMED |
| 9 | `none` | (none) | unchanged | unchanged | CONFIRMED |

The semantics column is reconstructed from the `--fp32-cast` help blob embedded verbatim in `Frontend.so` `.rodata`. The blob (binary-exact, *including* its two typos — `opertors`, `Matmtul`) reads:

```text
all: cast all the fp32 operators to bf16;
matmult: cast all fp32 operators that use inferentia Matmult to bf16 while using
         fp16 for matmult-based transpose;
matmult-fp16: cast all fp32 opertors that use inferentia Matmtul (including transpose)
         to fp16 for better numerical accuracy;
matmult-bf16: cast all fp32 operators that use inferentia Matmult (including transpose)
         to bf16 for better range to avoid overflow;
matmult-fp8e4: cast all floating point operators that use inferentia Matmult
         (including transpose) to fp8e4 for better performance;
experimental-all-fp16: cast all fp32 operators to fp16 for better numerical accuracy;
all-fp32r: cast all the fp32 operators to fp32r;
all-fp8e4: cast all the floating point operators to fp8e4;
```

> **CORRECTION — `matmult-fp32r` and `none` are *accepted choices* but are not separately described in the help blob.** `matmult-fp32r` is present in `choices` (index 4) yet the blob jumps from `matmult-fp8e4` to `experimental-all-fp16`; its dtype (FP32r) is inferred by parallel with the `matmult-*` family [STRONG]. `none` is index 9 and mirrors the user-facing doc line `fp32-cast-none: No casting is performed` (in `CompileCommand.so` `.rodata`) [STRONG]. Do not assume the help blob is the full mode set — the `choices` list is.

> **QUIRK — only `matmult` (index 1) splits the transpose dtype from the op dtype.** Every other mode uses the same dtype for matmult and matmult-based transpose. `matmult` alone is `(matmult, bf16)` *with transpose forced to fp16* — the one special-case in the table, and the reason the doc calls it the accuracy-preserving default.

### 3.2 The internal flag is forwarded, never read as an attribute [CONFIRMED]

In `Frontend.so` the literal `"--fp32-cast"` (`__pyx_kp_u_fp32_cast`) is referenced only by string-pool init, the const-tuple packer `pyx_pymod_exec_Frontend` (building `__pyx_tuple__40 = ('--fp32-cast',)`), and the `RegisterArgs` `add_argument` call. There is **no** `__pyx_n_s_fp32_cast` interned *attribute* name — the Job never executes `args.fp32_cast`. The parsed value rides inside the args namespace / tensorizer-options bundle and is forwarded wholesale to `HLOToTensorizer`. (The flag is registered adjacent to `--internal-hlo2tensorizer-options` / `--tensorizer-options`, the bundle it joins.) [CONFIRMED absence of attr-read; STRONG that forwarding is via the option bundle.]

---

## 4. The Bridge — `CompileCommand.buildPipeline`

`buildPipeline` (`__pyx_pw_…_14CompileCommand_9buildPipeline` @ `0x619d0`, generator-bodies at `0x28ea0`/`0x292c0`) is where the user pair becomes the canonical pair and is then re-encoded into the compact token. Both halves were read from the decompiled body; line references below are decompiled-output lines of that function.

### 4.1 Canonicalization — four rewrites [CONFIRMED]

```c
// buildPipeline, self == v43 == the parsed args/config object.
// All comparisons are _Pyx_PyUnicode_Equals against interned tokens — no enum.

if (self.auto_cast_type == "tf32") {                 // Equals @ dec.6597 (__pyx_n_u_tf32)
    self.auto_cast_type = "fp32r";                   // SetAttr @ dec.6630 (__pyx_n_u_fp32r)
    if (self.auto_cast == "all")                     // Equals @ dec.6656 — INSIDE the tf32 branch
        self.auto_cast = "matmult";                  // SetAttr @ dec.6715 (__pyx_n_u_matmult)
}
if (self.auto_cast_type == "fp8_e4m3") {             // Equals @ dec.6770 (__pyx_n_u_fp8_e4m3)
    self.auto_cast_type = "fp8e4";                   // SetAttr @ dec.6801 (__pyx_n_u_fp8e4)
}
if (self.auto_cast == "none") {                      // Equals @ dec.6827
    self.fast_math.append("none");                   // push 'none' onto fast_math
}
```

Net effect:

| user spelling | canonical token | axis |
|---|---|---|
| `tf32` | `fp32r` | dtype |
| `fp8_e4m3` | `fp8e4` | dtype |
| `all` + `tf32` | scope downgraded to `matmult` | scope |
| `none` (scope) | also pushes `'none'` into `fast_math` | bookkeeping |

> **GOTCHA — `tf32` + scope `all` is *silently downgraded* to `matmult`.** The `auto_cast=='all' -> 'matmult'` rewrite is nested *inside* the `tf32` branch (it only fires when the dtype is `tf32`). So `--auto-cast all --auto-cast-type tf32` does **not** cast all ops in FP32r — it casts only matmult-engine ops. There is no warning; the rewrite is unconditional within the branch. A reimplementer who hoists this check out of the `tf32` branch will produce a different (wrong) cast scope for the `all` + `bf16`/`fp16`/`fp8` combinations.

After §4.1 the canonical pair is guaranteed to be `auto_cast ∈ {none, matmult, all}` and `auto_cast_type ∈ {bf16, fp16, fp32r, fp8e4}` — exactly the knobs the Penguin `AutoCastFP32` pass consumes ([9.5](../numerics/autocast-fp32.md)).

### 4.2 Reverse-marshal — `(scope, dtype) → "fp32-cast-<scope>-<dtype>"` [CONFIRMED]

This is the centerpiece. `buildPipeline` rebuilds the compact token from the two canonical attrs and appends it to the forwarded option list. The decompiled body builds a 4-element join array `v36[0..3]` and calls `_Pyx_PyUnicode_Join` with `count=4`:

```c
// buildPipeline, reverse-marshal block (dec ~7004..7240; asm ~0x6bc0..0x6be0)
join[0] = "fp32-cast-";                  // __pyx_kp_u_fp32_cast_2  (literal "fp32-cast-")
join[1] = self.auto_cast;                // GetAttr auto_cast       (dec.7009)
join[2] = "-";                           // __pyx_kp_u__149         (literal "-")
join[3] = self.auto_cast_type;           // GetAttr auto_cast_type  (dec.7097)

token = _Pyx_PyUnicode_Join(join, 4, /*sep*/ NULL, /*…*/);   // dec.7204 — 4-arg join
options.append(token);                                       // _Pyx_PyObject_Append dec.7241
// e.g. ("matmult","bf16") -> "fp32-cast-matmult-bf16"
```

Binary anchors: the `"fp32-cast-"` prefix literal exists in `CompileCommand.so` `.rodata` as a standalone string (`fp32-cast-`); the `_Pyx_PyUnicode_Join(v36, 4, …)` call and the following `_Pyx_PyObject_Append` are both in the decompiled body. This is the **exact inverse** of §4.1's normalization — it re-emits the prefixed user spelling that §2.3 and the internal `--fp32-cast` both accept.

> **NOTE — the join is unconditional re-encoding, not a lookup table.** There is no `(scope,dtype) -> mode-name` dictionary. `buildPipeline` does not map `(matmult, bf16)` to the *literal* `matmult-bf16` choice; it *string-concatenates* `fp32-cast-` + `matmult` + `-` + `bf16` = `fp32-cast-matmult-bf16`. The compact internal `--fp32-cast matmult-bf16` and the forwarded `fp32-cast-matmult-bf16` token differ only by the `fp32-cast-` prefix, and both decode to the same canonical pair downstream.

### 4.3 Mode ↔ `(scope, dtype)` decomposition table [STRONG]

The compact `--fp32-cast {mode}` is the single-string encoding of the same pair. Reconstructed from the help text (§3.1), the `--auto-cast`/`--auto-cast-type` choice sets, and the §4.1/§4.2 round-trip:

| `--fp32-cast` mode | `(auto_cast, auto_cast_type)` | reverse-marshalled token |
|---|---|---|
| `all` | `(all, bf16)` | `fp32-cast-all-bf16` |
| `matmult` *(default)* | `(matmult, bf16)` + transpose=fp16 *(special-cased)* | `fp32-cast-matmult-bf16` |
| `matmult-fp16` | `(matmult, fp16)` | `fp32-cast-matmult-fp16` |
| `matmult-bf16` | `(matmult, bf16)` | `fp32-cast-matmult-bf16` |
| `matmult-fp32r` | `(matmult, fp32r)` | `fp32-cast-matmult-fp32r` |
| `matmult-fp8e4` | `(matmult, fp8e4)` | `fp32-cast-matmult-fp8e4` |
| `experimental-all-fp16` | `(all, fp16)` | `fp32-cast-all-fp16` |
| `all-fp32r` | `(all, fp32r)` | `fp32-cast-all-fp32r` |
| `all-fp8e4` | `(all, fp8e4)` | `fp32-cast-all-fp8e4` |
| `none` | `(none, —)` | *(no casting; `'none'` pushed to `fast_math`)* |

> **CORRECTION — the user-facing doc block has a copy/paste typo, not a real behaviour.** The `CompileCommand.so` doc line reads `fp32-cast-all-fp8e4: Cast all FP32 operations to FP32r to achieve speed up versus FP32.` — it says "FP32r" while the token is `fp8e4`. The token (`all-fp8e4` ⇒ `(all, fp8e4)`) is authoritative; the prose is a duplicated `fp32r` line. Trust the token, not the sentence.

---

## 5. The Boolean Precision Toggles

Three booleans live in `Frontend.RegisterArgs`. All three use the custom `EnableDisableArgumentAction` — *not* `argparse store_true` — which synthesizes the paired `--enable-X` / `--disable-X` option strings from one registration. The "default" is the value chosen when neither form is given. Defaults were read directly from the disassembled `RegisterArgs` (`__pyx_n_s_default` paired with `_Py_TrueStruct_ptr` / `_Py_FalseStruct_ptr`):

| flag | `kind` | default | dest | def anchor |
|---|---|---|---|---|
| `--enable-mixed-precision-accumulation` | PUBLIC | **True** | `enable_mixed_precision_accumulation` | `_Py_TrueStruct` @ `0x19566` / `n_s_default` @ `0x1956d` |
| `--enable-saturate-infinity` | PUBLIC | **False** | `enable_saturate_infinity` | `_Py_FalseStruct` @ `0x1999b` / `n_s_default` @ `0x199a2` |
| `--enable-internal-native-int64` | INTERNAL | **False** | `enable_internal_native_int64` | `_Py_FalseStruct` @ `0x19836` / `n_s_default` @ `0x1983d` |

### 5.1 `--enable-mixed-precision-accumulation` — default True [CONFIRMED]

```text
help = "Always use full precision of the accelerator ALU when we do accumulation,
        never downcast the input of an accumulation. (Default: true)"
```

Semantics: default **on**. When enabled (the default), accumulation inputs are not downcast — the ALU accumulates in full precision; `--disable-mixed-precision-accumulation` turns it off. No in-module consumer in `Frontend.so`; the value rides the args namespace into the tensorizer/codegen (its codegen effect is traced in the native-kernel lowering chapter, [HLO → Native / NKI Kernel Lowering](../hlo-opt/hlo-to-native-kernel-lowering.md)).

### 5.2 `--enable-saturate-infinity` — default False [CONFIRMED def; bool→cfg bridge INFERRED]

```text
help = "Saturate Inf/-Inf values to MAX_/MIN_FLOAT before operations that could
        produce NaN values for Inf/-Inf inputs on the target architecture"
```

The CLI flag is CONFIRMED (PUBLIC, default False, parsed in the Python Frontend), but the path to the native `FP8ConversionConfig.lo` saturate bit (the bit the native cast reads; the precision-policy page §9.4 is documented in Part 9, pending) is **not** a verbatim string token: `Frontend.so` references `enable_saturate_infinity` only in the string pool and `RegisterArgs` (no `runXLAFrontend` attribute read), and no cp310 Python module reads `args.enable_saturate_infinity`. The bool reaches the native cast through the **serialized args/compiler-config object**, not a recognizable option keyword. [INFERRED bridge.]

> **NOTE — the CLI default (False) and the per-cast primitive default (saturate-ON) are different layers, and are consistent.** The native FP8 conversion primitive defaults to saturate-ON at the byte level; the CLI flag is an *additional* front-end gate that, when set, requests Inf/-Inf clamping at the *operation* level (pre-op clamp to MAX/MIN_FLOAT) — a broader behaviour than the per-cast config bit. `default(False)` at the CLI therefore does not contradict `saturate-ON` at the byte-cast primitive; they gate different things.

### 5.3 `--enable-internal-native-int64` → tensorizer token [CONFIRMED]

This is the one boolean with a *visible* in-module forward. The INTERNAL flag is translated, in `runXLAFrontend` (`0x1c3e0`), into the tensorizer-option token `enable-native-int64`:

```c
// runXLAFrontend, native-int64 forward (decompiled lines ~2492..2576)
v = getattr(obj, "enable_internal_native_int64");      // dec.2492
if (PyObject_IsTrue(v)) {                               // dec ~2521
    opts = getattr(obj, "tensorizer_options");         // dec.2552
    opts.append("enable-native-int64");                // __pyx_kp_u_enable_native_int64  dec.2576
}
// (unconditionally, earlier:) tensorizer_options.append("enable-penguin-mac-count")  dec.2477
```

The PUBLIC-facing `--enable-native-int64` / `enable-native-int64` is **not** a registered `argparse` flag in this module; it is the tensorizer-option token the INTERNAL bool is translated into and forwarded to `HLOToTensorizer`. Same forwarding pattern as the unconditional `enable-penguin-mac-count`. Semantics: when set, keep int64 native (no int64→int32 narrowing). Default off.

> **QUIRK — the tensorizer token `enable-native-int64` shares storage bytes with the CLI flag string `--enable-native-int64`.** Byte-scan of `Frontend.so` finds the sequence `enable-native-int64` exactly **once**, at `0x280d2` — the null-terminated buffer at `0x280d0` is `"--enable-native-int64\0"`, and the interned `__pyx_kp_u_enable_native_int64` constant points two bytes in, at `0x280d2`, reusing the suffix. (Same trick for `--enable-penguin-mac-count` @ `0x27e70` vs the bare token @ `0x27e72`.) A reimplementer reading the string pool will see one buffer doing double duty; this is a Cython interning artifact, not two distinct strings.

> **NOTE — `--enable-saturate-infinity` and `--enable-mixed-precision-accumulation` are *not* forwarded by `runXLAFrontend`.** Only `enable-native-int64` and `enable-penguin-mac-count` are appended to `tensorizer_options` here. The other two booleans reach the backend via the serialized args/config, not via an in-module token append.

---

## 6. Other Precision / Rounding Controls

### 6.1 `--fast-math fast-relayout-*` — transpose-engine dtype [CONFIRMED strings]

The five `fast-relayout` tokens (choices 11–15 of `--fast-math`, §2.3) control the dtype of the *matmult-based tensor transpose* ("relayout"), a separate axis from the op-cast dtype. Help text (`CompileCommand.so` `.rodata`, verbatim):

| token | transpose behaviour |
|---|---|
| `fast-relayout` | Enables fast relayout (matrix multiplier for transpose); dtype = FP16/BF16/FP32 per the accompanying `fp32-cast-xxx`. |
| `fast-relayout-fp32` | transpose dtype = FP32. |
| `fast-relayout-fp32r` | transpose dtype = FP32r. |
| `fast-relayout-fp8e4` | transpose dtype = FP8E4. |
| `no-fast-relayout` | Disables fast relayout; transpose is bit-accurate (lossless) but slower. |

`Frontend.so` carries its own `fast-relayout-fp32r` / `fast-relayout-fp8e4` tokens (adjacent registration, same meaning).

### 6.2 `tf32` / `fp32r` (TensorFloat-32) [CONFIRMED]

`tf32` is purely the *user* spelling on the `--auto-cast-type` / `fp32-cast` axis (doc line `tf32: TensorFloat-32`); it is canonicalized to `fp32r` in `buildPipeline` (§4.1). `fp32r` is the internal dtype token consumed by `AutoCastFP32` and the `matmult-fp32r` / `all-fp32r` modes — reduced-mantissa FP32 (à la NVIDIA TF32) used by the Tensor Engine for speed with better precision than BF16/FP16. There is **no** separate `--tf32` boolean; tf32/fp32r is only ever a dtype value.

### 6.3 `--experimental-unsafe-fp8e4m3fn-as-fp8e4m3` [CONFIRMED def]

Owner: `CompileCommand.so` (not `Frontend.so`). Public form `--experimental-unsafe-fp8e4m3fn-as-fp8e4m3` plus internal alias `--internal-experimental-unsafe-fp8e4m3fn-as-fp8e4m3`; dest `internal_experimental_unsafe_fp8e4m3fn_as_fp8e4m3`; help `"Convert fp8e4m3fn types to fp8e4m3"`. Semantics: an **unsafe** reinterpretation of fp8e4m3fn (finite-only, NaN at `0x7f`) tensors as plain fp8e4m3, changing the NaN/Inf encoding contract. Directly relevant to the FP8 cast path (e4m3fn saturate-to-max `0x7e` vs e4m3 NaN at `0x7f`). [CONFIRMED def — flag + help string present; action/default not separately disassembled.]

### 6.4 `--disable-inline-cast` — NOT PRESENT [CONFIRMED absence]

Negative grep across **every** `driver/` module: the strings `--disable-inline-cast`, `inline-cast`, and `inline_cast` do not exist in `CompileCommand.so`, `Frontend.so`, or any other module under `neuronxcc/driver/`. This flag does **not** exist in `neuronx_cc` 2.24.5133.0. The closest real levers are `no-fast-relayout` (forces lossless transpose) and `fp32-cast-none` / `--auto-cast none` (no casting at all).

> **CORRECTION — do not document an `--disable-inline-cast` knob.** It was a candidate name in the task brief, but the binary has no such string. The cast SCOPE is controlled entirely by the `fp32-cast` mode / `auto_cast` scope axis; there is no per-cast "inline" toggle.

---

## 7. End-to-End Trace — One Flag From CLI to the Cast

Worked example, `--auto-cast all --auto-cast-type tf32`:

```text
1. CompileCommand.__init__ (0x36290): argparse fills
       namespace.auto_cast      = "all"
       namespace.auto_cast_type = "tf32"
2. buildPipeline canonicalize (0x6597..):
       auto_cast_type == "tf32"  ->  auto_cast_type = "fp32r"   (dec.6630)
         (inside tf32 branch) auto_cast == "all" -> auto_cast = "matmult"  (dec.6715)
   => canonical pair = ("matmult", "fp32r")    <-- scope SILENTLY downgraded
3. buildPipeline reverse-marshal (~0x6bc0):
       token = "fp32-cast-" + "matmult" + "-" + "fp32r" = "fp32-cast-matmult-fp32r"
       options.append("fp32-cast-matmult-fp32r")          (dec.7241)
4. forwarded in the tensorizer-options bundle to HLOToTensorizer / the Frontend Job
5. numeric layer reads (scope=matmult, dtype=fp32r): casts only matmult-engine
   fp32 ops (and matmult-based transpose) to FP32r.   See 9.5 AutoCastFP32.
```

The same value would be produced by the single internal token `--fp32-cast matmult-fp32r`. The two doors meet at step 4.

---

## 8. Provenance & Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| `--fp32-cast` 10-mode enum | all 10 strings in `Frontend.so`; `PyList_New(10)` @ `0x19340`; help blob verbatim (with typos) | CONFIRMED |
| `--auto-cast` default `none` | `__pyx_n_u_none` → `n_s_default` @ `0x39a82` | CONFIRMED |
| `--auto-cast-type` default `bf16` | `__pyx_n_u_bf16` → `n_s_default` @ `0x39f0d`, `PyDict_SetItem` @ `0x39f1e` | CONFIRMED |
| canonicalize `tf32→fp32r`, `fp8_e4m3→fp8e4`, `all+tf32→matmult` | `_Pyx_PyUnicode_Equals`/`SetAttr` @ dec.6597/6630/6656/6715/6770/6801 | CONFIRMED |
| reverse-marshal `"fp32-cast-<s>-<t>"` | 4-arg `_Pyx_PyUnicode_Join` + `Append`, prefix literal `fp32-cast-` in `.rodata` | CONFIRMED |
| 3 boolean defaults `True/False/False` | `_Py_True/False_Struct` @ `0x1956d`/`0x199a2`/`0x1983d` | CONFIRMED |
| `enable-native-int64` forward in `runXLAFrontend` | `getattr enable_internal_native_int64` + `IsTrue` + `append` @ dec.2492/2576 | CONFIRMED |
| `enable-native-int64` shares bytes with `--enable-native-int64` | single byte-occurrence @ `0x280d2` inside buffer @ `0x280d0` | CONFIRMED |
| `--disable-inline-cast` absent | negative grep across all `driver/` modules | CONFIRMED |
| `matmult-fp32r` dtype = FP32r | parallel with `matmult-*` family; not in help blob | STRONG |
| `--enable-saturate-infinity` → native `FP8ConversionConfig.lo` | no string token; reaches cast via serialized config | INFERRED |

---

*Cross-references: 9.4 Numerics precision policy (documented in Part 9, pending) · [9.5 AutoCastFP32](../numerics/autocast-fp32.md) · [HLO → Native / NKI Kernel Lowering](../hlo-opt/hlo-to-native-kernel-lowering.md) · [3.8 Flag Catalog](flag-catalog.md) · [The Compile Pipeline at a Glance](../front/pipeline.md).*
