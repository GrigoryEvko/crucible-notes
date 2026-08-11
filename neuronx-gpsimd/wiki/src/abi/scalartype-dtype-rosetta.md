# ScalarType ↔ DTYPE Rosetta

This page is the canonical three-way translation table between the three
namespaces a GPSIMD custom op straddles, and the recovered machine code that
implements the conversion at runtime:

1. **`c10::ScalarType`** — the PyTorch / ATen scalar-type ordinal that the host
   framework hands the kernel (`caffe2::TypeMeta`, dispatch keys, `at::Tensor`).
2. **`NEURON_ISA_TPB_DTYPE`** — the firmware/ISA data-type code carried in the
   marshalled tensor descriptor (`ARG_TENSOR.dtype`, a single byte).
3. The **firmware DTYPE code** as it appears in the binary ISA instruction
   stream — numerically identical to (2); the ISA header is the authoritative
   numbering.

The translation is realized entirely inside the customop wrapper library
(`libneuroncustomop.a`, member `wrapper_api.o`, ELF32-Xtensa, DWARF v4). There
is exactly **one** runtime conversion direction implemented as code —
ISA→ScalarType — and it is **inlined**, not a callable. The reverse direction
(`torch_to_isa_dtype`) exists only conceptually; no such symbol is present
(`nm` sweep over every archive member returns none).
`[HIGH][OBSERVED]`

Cross-references:
[firmware DTYPE model](../firmware/kernels/dtype-model.md) ·
[customop marshalling](customop-marshalling.md) ·
[tensor object chain](tensor-object-chain.md) ·
[Q7PtrType](q7ptrtype.md).

> **CORRECTION (vs ABI-06).** That report *inferred* the ISA→ScalarType
> map from the surrounding call graph. This page **decodes the actual switch**
> from the Xtensa machine code (`xtensa-elf-objdump`, core `ncore2gp`), so every
> row below is `OBSERVED` from the compare/branch chain, not reconstructed.

> **CORRECTION (vs ABI-06).** The unsupported-dtype DEFAULT arm is **not**
> `ScalarType::Undefined` (ordinal 18). It is a hard `_Assert` →
> `noreturn`/abort. The pre-decode sentinel that *would* have been stored is the
> magic `42` (`0x2a`), never `18`, and it is always overwritten on a hit or
> aborted on a miss. Details under *The unsupported set* below.

---

## 1. Master three-way table

`NEURON_ISA_TPB_DTYPE` is defined identically across the v2–v5 ISA headers
shipped in `custom_op/c10/include/neuron_<arch>_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
(`sunda`, `cayman`, `mariana`, `maverick`). The v1 header
(`arch-isa/tpb/aws_tonga_isa_tpb_common.h`, prefix `TONGA_`) defines a strict
subset. The ScalarType ordinals are the standard ATen numbering recovered from
the shipped `c10/core/ScalarType.h`
(`AT_FORALL_SCALAR_TYPES_WITH_COMPLEX_AND_QINTS`, with explicit `/* N */`
markers). `[HIGH][OBSERVED]`

| ISA code | `NEURON_ISA_TPB_DTYPE` name | elem size | ScalarType ordinal | ScalarType name | runtime mapping |
|---:|---|---:|---:|---|---|
| `0x0` | `INVALID`   | — | — | — | **abort** |
| `0x1` | `UINT64`    | 8 | — | (no ScalarType) | **abort** |
| `0x2` | `INT8`      | 1 | 1  | `Char`     | ✓ mapped |
| `0x3` | `UINT8`     | 1 | 0  | `Byte`     | ✓ mapped |
| `0x4` | `INT16`     | 2 | 2  | `Short`    | ✓ mapped |
| `0x5` | `UINT16`    | 2 | — | (no ScalarType) | **abort** |
| `0x6` | `BFLOAT16`  | 2 | 15 | `BFloat16` | ✓ mapped |
| `0x7` | `FP16`      | 2 | 5  | `Half`     | ✓ mapped |
| `0x8` | `INT32`     | 4 | 3  | `Int`      | ✓ mapped |
| `0x9` | `UINT32`    | 4 | — | (no ScalarType) | **abort** |
| `0xA` | `FP32`      | 4 | 6  | `Float`    | ✓ mapped |
| `0xB` | `FP32R`     | 4 | — | (no ScalarType) | **abort** |
| `0xC` | `INT64`     | 8 | 4  | `Long`     | ✓ mapped |
| `0xD` | `FP8_EXP3`  | 1 | — | (no ScalarType) | **abort** |
| `0xE` | `FP8_EXP4`  | 1 | — | (no ScalarType) | **abort** |
| `0xF` | `FP8_EXP5`  | 1 | — | (no ScalarType) | **abort** |

> **NOTE — non-contiguous codes.** The ISA assigns codes by family, not by
> width: signed-int {2,4,8,C}, unsigned-int {3,5,9,1}, float {6,7,A,B,D,E,F}.
> `INT8=0x2` but `UINT8=0x3`; `FP32=0xA` but `FP32R=0xB`; `INT64=0xC`. The
> compiler exploits this in the dispatch by sorting the compare chain on the
> raw byte value and pruning with range guards (`bgei`/`bltui`), not on a
> semantic family. Header `Note:` comments confirm `0xB`/`FP22` partial-fp32
> aliasing in RTL. `[HIGH][OBSERVED]`

### Supported vs absent set

The eight ISA codes with a `✓` are the **entire** set the wrapper will accept;
every other code falls through to the abort arm. The supported set is exactly
`{0x2, 0x3, 0x4, 0x6, 0x7, 0x8, 0xA, 0xC}` — re-grounded directly from the
compare constants present in the decoded switch (the `beqi/bnei a2,K`
immediates are precisely `{2,3,4,6,7,8,10,12}`). `[HIGH][OBSERVED]`

The **absent** set is
`{0x0 INVALID, 0x1 UINT64, 0x5 UINT16, 0x9 UINT32, 0xB FP32R, 0xD FP8_EXP3, 0xE FP8_EXP4, 0xF FP8_EXP5}`.
Two reasons they are absent:

* **No ATen ordinal at all** for unsigned 16/32/64-bit integers in this c10
  build — `ScalarType.h` has `Byte`(=uint8) and the signed widths, but no
  `UInt16/UInt32/UInt64`. There is simply nothing to map them to.
* **FP8 / FP32R are device-internal** accumulator/quantization formats; they
  never reach a host `at::Tensor` boundary, so the marshalling layer treats
  their appearance as a programming error rather than silently widening.

> **GOTCHA.** The v1 (`TONGA_`) header does **not even define** codes
> `0x1,0x2,0x4,0x9,0xB,0xD,0xE,0xF` — its enum is only
> `{INVALID=0,UINT8=3,UINT16=5,BFLOAT16=6,FP16=7,INT32=8,FP32=0xA,INT64=0xC}`.
> So on a v1 part, `INT8`/`INT16` are not even nameable, yet the *binary*
> dispatch (compiled against a v2+ header) accepts them. The library binary is
> built once against the modern ISA and is forward/backward-tolerant of the ISA
> code space; the v1 absence is a header-only fact, not a runtime gate.
> `[HIGH][OBSERVED]` (binary) `[HIGH][OBSERVED]` (v1 header)

---

## 2. The runtime dispatch: `isa_to_torch_dtype`, inlined

There is no out-of-line `isa_to_torch_dtype` symbol. The function is **inlined**
into every consumer; the canonical instance lives inside
`UTensor::operator at::Tensor() const`
(mangled `_ZNK7UTensorcvN2at6TensorEEv`, weak, own section
`.text._ZNK7UTensorcvN2at6TensorEEv`, size `0x304`). A second, byte-identical
copy is inlined into `UTensor::copy(at::Tensor const&)`
(`_ZN7UTensor4copyERKN2at6TensorE`), where it backs the dtype-match gate
(§3). `[HIGH][OBSERVED]`

Two distinct decode trees run over the same dtype byte:

* an **element-size** tree (computes `sizeof(elem)` to size the storage), and
* an **ordinal** tree (selects the `c10::ScalarType` to stamp into the
  `caffe2::TypeMeta`).

Both read the dtype from offset 10 of the `UTensor` object (`a7`), via
`l8ui a2, a7, 10`. Neither is a jump table: both are if/else compare chains
(`beqi`/`bnei`/`bgei`/`bltui` against the immediates above), balanced by the
compiler into a shallow binary tree with a `bgei a2,7` split at the root.
`[HIGH][OBSERVED]`

### 2a. The ordinal tree (the actual ScalarType selection)

Annotated decode of the ordinal-producing tree at `+0x1a4 .. +0x216`. Register
`a2` holds the dtype byte; `a3` accumulates the chosen ScalarType ordinal; the
result is committed with `s16i a3, a1, 144` (stack slot `0x90`) and consumed by
`NeuronTensorImpl::intrusive_ptr::make<…, c10::DispatchKey, caffe2::TypeMeta>`
(`make<…>` reloc confirmed at `+0x216`/`+0x226`). `[HIGH][OBSERVED]`

```c
/* UTensor::operator at::Tensor() const, decoded from
   .text._ZNK7UTensorcvN2at6TensorEEv  (xtensa-elf-objdump, core ncore2gp).
   `dtype` is the byte at UTensor + 10 (== ARG_TENSOR.dtype, see NOTE in #4).
   Returns the c10::ScalarType ordinal; aborts on any unsupported code.       */
static inline int isa_to_torch_dtype(uint8_t dtype /* a2 */)
{
    int ord = 42;                 /* +0x1a7  movi a3,42  : sentinel (0x2a),    */
                                  /* +0x1a9  s16i a3,a1,144 : pre-stored, then */
                                  /*          overwritten on every hit         */

    if (dtype >= 7) {             /* +0x1ac  bgei a2,7 -> high half            */
        if (dtype >= 10) {        /* +0x1c7  bgei a2,10                        */
            if (dtype != 10)      /* +0x1ed  bnei a2,10 -> +0x20e              */
                goto maybe_int64;
            ord = 6;              /* +0x1f0  FP32  (0xA) -> Float  (6)         */
        } else {
            if (dtype == 7) { ord = 5;   /* +0x1cf  FP16 (0x7) -> Half  (5) */ }
            else if (dtype == 8) { ord = 3; /* +0x1d7 INT32(0x8)-> Int  (3) */ }
            else goto unsupported;        /* +0x1d7  bnei a2,8 -> default     */
        }
    } else {                      /* low half: dtype < 7                       */
        if (dtype >= 4) {         /* +0x1af  bgei a2,4 -> +0x1e2               */
            if (dtype == 4) { ord = 2; /* +0x1e5  INT16(0x4)-> Short (2)   */ }
            else if (dtype == 6) { ord = 15; /* +0x20b BF16(0x6)->BFloat16(15)*/ }
            else goto unsupported;        /* +0x203  bnei a2,6 -> default      */
        } else {
            if (dtype == 2) { ord = 1; /* +0x1bf  INT8 (0x2)-> Char  (1)   */ }
            else if (dtype == 3) { ord = 0; /* +0x200 UINT8(0x3)-> Byte (0) */ }
            else goto unsupported;        /* +0x1f8  bnei a2,3 -> default      */
        }
    }
    goto commit;

maybe_int64:
    if (dtype != 12) goto unsupported;    /* +0x20e  bnei a2,12 -> default     */
    ord = 4;                              /* INT64 (0xC) -> Long (4)           */

commit:
    /* +0x236  s16i a3,a1,144  ; ord -> caffe2::TypeMeta arg slot */
    return ord;

unsupported:
    /* +0x2f0 : const16 a10,.data+0x3ab ; const16 a2,_Assert ; callx8 a2       */
    _Assert("utypes.hpp:37 0");           /* noreturn / abort                  */
}
```

> **NOTE — the `42` sentinel.** `+0x1a7 movi a3,42` pre-loads `0x2a` into the
> ordinal slot before the tree runs, and `+0x1a9 s16i a3,a1,144` writes it to
> the stack. On any matched code the slot is rewritten with the real ordinal at
> `+0x236`; on any unmatched code control reaches `+0x2f0` and aborts before the
> stale `42` is read. `42` is **not** a ScalarType and is never visible to the
> caller — it is a placeholder the optimizer left in, not a fall-through value.
> `[HIGH][INFERRED]` (the value is `OBSERVED`; its "never observable" status is
> the inference)

### 2b. The element-size tree (inline `dtype_get_sizeof`)

The size tree at `+0x148 .. +0x187` produces `sizeof(elem)` in `a3`, used to
multiply against the element count when sizing the `NeuronStorageImpl`. Decoded:

```c
/* element size, inlined; identical numbering to TONGA_isa_tpb_dtype_get_sizeof */
static inline size_t isa_dtype_sizeof(uint8_t dtype /* a2 */)
{
    if (dtype >= 8) {                  /* +0x14d  bgei a2,8                    */
        if (dtype == 8)  return 4;     /* INT32                               */
        if (dtype == 10) return 4;     /* +0x16c  bnei a2,10 ; FP32           */
        if (dtype != 12) _Assert("utypes.hpp:37 0"); /* +0x17f                */
        return 8;                      /* INT64                               */
    }
    if (((dtype - 2) & 0xff) < 2) return 1; /* +0x158 bltui ; {INT8,UINT8}    */
    if (((dtype - 6) & 0xff) < 2) return 2; /* +0x160 bltui ; {BFLOAT16,FP16} */
    if (dtype == 4)               return 2; /* +0x163 ; INT16                 */
    _Assert("utypes.hpp:37 0");        /* +0x166  j +0x2f0                    */
}
```

The width map `{0x2:1, 0x3:1, 0x4:2, 0x6:2, 0x7:2, 0x8:4, 0xA:4, 0xC:8}` is
**bit-for-bit** the Tonga `tonga_isa_tpb_dtype_get_sizeof` switch
(`aws_tonga_isa_tpb_common.h`: `UINT8`→1; `{UINT16,FP16,BFLOAT16}`→2;
`{INT32,FP32}`→4; `INT64`→8; `default`→`ILLEGAL_INST_DTYPE`), extended only by
the two signed widths the v2+ header adds (`INT8`→1, `INT16`→2). `FP8_*` are
1-byte in the ISA but the wrapper never reaches their size case — it aborts in
the ordinal tree first. `[HIGH][OBSERVED]`

---

## 3. The dtype-match gate (`utypes.hpp:65`)

When a host `at::Tensor` is *bound back into* a `UTensor`
(`UTensor::copy(at::Tensor const&)`), the library asserts that the framework's
declared ScalarType agrees with what the ISA dtype byte translates to. The exact
recovered assertion string (`.data+0x1ed`, referenced from `copy` at `+0x23c`
and `+0x247`, calling `_Assert`):

```
utypes.hpp:65 aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)
```

`[HIGH][OBSERVED]`

In the binary this is *two* inlined dtype trees feeding a compare:
`copy` runs the §2a ordinal tree on `t_.dtype` (the ISA byte at offset 10,
`l8ui a5,a2,10` at `+0x12`), producing the expected ScalarType in `a6`, then
loads `aten_t`'s ScalarType (`l8ui a5,a10,198`, `+0x95`) and compares. A
mismatch hits `_Assert(utypes.hpp:65 …)` and aborts. The gate is preceded by the
storage-pointer gate `utypes.hpp:64 xt_ptr_` (`.data+0x1a1`, `+0x228`) which
checks the backing `Q7PtrType` is present. `[HIGH][OBSERVED]`

> **GOTCHA.** The gate is the only place the *direction* host→device is checked,
> and it does so by re-running the device→host map and comparing — there is no
> independent `torch_to_isa_dtype`. A custom op that hands back an `at::Tensor`
> whose ScalarType disagrees with the original ISA dtype does not get a graceful
> error path; it aborts via `_Assert`. There is no recovery, no error return.
> `[HIGH][OBSERVED]`

---

## 4. ARG_TENSOR dtype field: offset `0x02` vs the offset-10 load

The marshalled descriptor `NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR` (48 bytes) is
defined in the v2+ ISA header with explicit per-field offset comments:

```c
typedef struct NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR {
    NEURON_ISA_TPB_CUSTOM_OP_ARG_LOCATION      location;             // 1  ( 0    )
    NEURON_ISA_TPB_CUSTOM_OP_TENSOR_SHAPE_TYPE framework_shape_type; // 1  ( 1    )
    NEURON_ISA_TPB_DTYPE                       dtype;                // 1  ( 2    )
    uint8_t                                    reserved0[5];         // 5  ( 3- 7)
    NEURON_ISA_TPB_CUSTOM_OP_TENSOR_SHAPE_UNION   framework_shape;   // 16 ( 8-23)
    NEURON_ISA_TPB_CUSTOM_OP_TENSOR_STORAGE_UNION storage;           // 16 (24-39)
    uint8_t                                    reserved1[8];         // 8  (39-47)
} NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR;
```

So in the **ARG_TENSOR** namespace, `dtype` is at **offset `0x02`** (matching
the shared Part-7 layout). But the recovered code reads it at **offset 10**
(`l8ui a2, a7, 10`). `[HIGH][OBSERVED]`

> **NOTE — reconciling `0x02` vs `10`.** `a7` is not a pointer to a bare
> `ARG_TENSOR`; it points at the **`UTensor` wrapper object** (`t_`), which
> embeds the `ARG_TENSOR` at wrapper offset `+8` (the first 8 bytes are the
> wrapper header — the `xt_ptr_` storage handle gated by `utypes.hpp:64`). Thus
> the dtype byte lands at `8 (embedded ARG_TENSOR base) + 2 (dtype field) = 10`.
> The same `+8` embed offset is independently confirmed by the shape-type read
> at the top of the operator: `l8ui a3, a3, 9` (`+0xc`) reads
> `framework_shape_type` (ARG_TENSOR `+1`) at wrapper `+9`, and immediately
> branches on it (`beqi a3,1 -> abort utypes.hpp:94 …OUT_OF_LINE_SHAPE`;
> `bnei a3,2` for `INLINE_SHAPE4D`). Both reads (`+9`, `+10`) are exactly
> `ARG_TENSOR base 8` plus the header offsets `1` and `2`. There is no
> contradiction: `0x02` is the field offset *within* `ARG_TENSOR`; `10` is the
> same field *within* the `UTensor` that embeds it. `[HIGH][OBSERVED]`

The `framework_shape_type` enum it gates on:
`INLINE_SHAPE8D=0x00`, `OUT_OF_LINE_SHAPE=0x01`, `INLINE_SHAPE4D=0x02`. The
operator aborts on `OUT_OF_LINE_SHAPE` (`utypes.hpp:94`,
`t_.framework_shape_type != …OUT_OF_LINE_SHAPE`) — out-of-line N-D shapes are
not materializable into an `at::Tensor` by this path. `[HIGH][OBSERVED]`

---

## 5. The unsupported set: assert, not Undefined

The DEFAULT arm of the ordinal tree is the abort sequence at `+0x2f0`:

```
+0x2f0  const16 a10, .data+0x3ab     ; -> "utypes.hpp:37 0"
+0x2f8  const16 a2,  _Assert
+0x2fb..+0x2fe  (ALT/OP reloc pair)
+0x301  callx8  a2                    ; _Assert(...) ; noreturn
```

The recovered message at `.data+0x3ab` is `utypes.hpp:37 0` (the customop
library's `_Assert(cond, msg, …)` formats the `__FILE__:__LINE__ <expr>` prefix;
the `expr` here is the literal `0`, i.e. an unconditional fail). Control never
returns; there is no `ScalarType::Undefined` (ordinal 18) produced.
`[HIGH][OBSERVED]`

> **CORRECTION — assert line number.** ABI-06 attributed the default abort
> to `utypes.hpp:89 ret == 0`. Byte-accurate `.data` extraction (`objcopy
> --only-section=.data` + offset index) shows the relocation `.data+0x3ab`
> resolves to **`utypes.hpp:37 0`**, not `:89`. The string `utypes.hpp:89 ret ==
> 0` *does* exist in `.data` (at `+0x394`) but belongs to a different assert in
> the same translation unit; it is **not** the dtype default arm. `[HIGH][OBSERVED]`

For completeness, the relevant recovered `utypes.hpp` assertion strings in this
TU and their roles:

| `.data` off | string | role |
|---:|---|---|
| `0x1a1` | `utypes.hpp:64 xt_ptr_` | storage-handle present gate (in `copy`) |
| `0x1ed` | `utypes.hpp:65 aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)` | **dtype-match gate** |
| `0x394` | `utypes.hpp:89 ret == 0` | unrelated status check (not the dtype default) |
| `0x3ab` | `utypes.hpp:37 0` | **dtype default arm** (ordinal & size trees) |
| `0x43a` | `utypes.hpp:94 …!= …OUT_OF_LINE_SHAPE` | shape-type gate |
| `0x4d6` | `utypes.hpp:139 num_dim != 0 …` | rank ≥ 1 gate |

`[HIGH][OBSERVED]`

---

## 6. The companion path: `operator c10::Scalar() const`

`UTensor::operator c10::Scalar() const` (`_ZNK7UTensorcvN3c106ScalarEEv`, own
section, size `0x300`) does **not** reproduce the full ordinal table. It reads
the same dtype byte (`l8ui a4, a3, 10`, `+0x42`) and performs only a coarse
**float-vs-integral classification** with the same root `bgei a4,7` split, then
populates a `c10::Scalar` as a `double` (FP family) or an integral. Its own
default arm aborts via `_Assert("utypes.hpp:37 0")` (reached at `+0x2ec`).
`[HIGH][OBSERVED]`

The notable detail is the **bf16 widening**: for `BFLOAT16=0x6` the Scalar path
expands the 16-bit value into an IEEE-754 fp32 bit pattern in software before
loading it into a float register — the recovered sequence builds the fp32 sign,
8-bit exponent and mantissa with shifts (`slli a7,a7,24 …`) and commits with
`wfr v0, a7` / `mul.s` normalization, since the Xtensa FP unit has no native
bf16. (`Half=0x7` takes the same software expansion branch with the FP16
field widths.) `[HIGH][OBSERVED]`

> **GOTCHA.** A `UTensor` that holds more than one element cannot be coerced to
> `c10::Scalar`; the Scalar path carries its own rank/count gates
> (`utypes.hpp:172`/`:178`, `nelem_* == 0 && "Cannot create an at::Scalar.
> Argument has more than 1 …"`). `[HIGH][OBSERVED]`

---

## 7. Per-arch dtype set: v1 → v5

| arch (codename) | ISA gen | DTYPE enum | INT8/INT16 | UINT32/64 | FP8_* / FP32R | confidence |
|---|---|---|:--:|:--:|:--:|---|
| tonga    | v1 | `{0,3,5,6,7,8,A,C}` (8 codes) | absent | absent | absent | `[HIGH][OBSERVED]` |
| sunda    | v2 | full 16-code map | present | present | present | `[HIGH][OBSERVED]` |
| cayman   | v3 | full 16-code map | present | present | present | `[HIGH][OBSERVED]` |
| mariana  | v4 | full + `_BASIC_` alias enum | present | present | present | `[HIGH][OBSERVED]` |
| maverick | v5 | full + `_BASIC_` alias enum | present | present | present | `[MED][OBSERVED]` |

The v4/v5 headers (`mariana`, `maverick`) define the dtype enum **twice**: a
`NEURON_ISA_TPB_DTYPE_BASIC_*` enum and the plain `NEURON_ISA_TPB_DTYPE_*` enum,
with identical numeric values. The split signals an extended-dtype tier layered
above the basic set; the numeric codes for the basic 16 are unchanged from v2.

> **NOTE.** The dtype *codes* and *element sizes* are byte-grounded from the
> shipped v1–v4 headers and the decoded v2-built binary. The claim that the
> v5/maverick set is *exactly* the same 16-code basic map with no additional
> codes reaching the customop boundary is `[MED][INFERRED]` — it rests on the
> header enum matching v2–v4 verbatim, not on a v5-specific binary decode of the
> wrapper. Any v5/Maverick-only extended dtype would surface as a new compare
> constant in a v5 build of `wrapper_api.o`, which is not present in this
> archive. `[MED][INFERRED]`

---

## Summary of anchors

* `wrapper_api.o` (member of `libneuroncustomop.a`), ELF32-Xtensa core
  `ncore2gp`.
* `_ZNK7UTensorcvN2at6TensorEEv` (`operator at::Tensor`, size `0x304`): ordinal
  tree `+0x1a4..+0x216`, size tree `+0x148..+0x187`, default abort `+0x2f0`,
  dtype load `l8ui a2,a7,10`, ordinal store `s16i a3,a1,144`,
  `make<…,caffe2::TypeMeta>` reloc `+0x216`/`+0x226`.
* `_ZN7UTensor4copyERKN2at6TensorE` (`copy`): dtype-match gate
  `_Assert(.data+0x1ed = utypes.hpp:65)` at `+0x23c`/`+0x247`.
* `_ZNK7UTensorcvN3c106ScalarEEv` (`operator c10::Scalar`, size `0x300`):
  float/int classification + software bf16 widen, default abort `+0x2ec`.
* Headers: `c10/core/ScalarType.h` (ordinals);
  `neuron_<arch>_arch_isa/tpb/aws_neuron_isa_tpb_common.h` (DTYPE enum,
  `ARG_TENSOR` offsets); `arch-isa/tpb/aws_tonga_isa_tpb_common.h`
  (`tonga_isa_tpb_dtype_get_sizeof`, v1 subset).
* No `torch_to_isa_dtype` / `isa_to_torch_dtype` out-of-line symbol exists (`nm`
  sweep = none); both are inlined.
