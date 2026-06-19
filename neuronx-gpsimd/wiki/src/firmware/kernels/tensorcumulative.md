# TensorCumulative — the tensor-only prefix scan (opcodes `0x4E`/`0x5E`)

**TensorCumulative** is the GPSIMD **single-tensor inclusive prefix scan**: given one input tensor it emits
`out[i] = out[i-1] op in[i]` — a running cumulative along a chosen axis, one output element per input
element. It is the **SCAN-emit twin of [Tensor-Reduce](tensor-reduce.md)**: the two ops ride the **same**
64-byte `NEURON_ISA_TPB_S4D4_TR_STRUCT`, carry the **same** single combiner `op@36` and the **same**
axis selector `op_dim@37`, and differ in exactly one validity predicate — Reduce **collapses** the axis to
one output, TensorCumulative **emits a running partial at every index** along it. There is no separate
"reduce op" field, no scalar, no second tensor, and no cross-tile accumulator.

The opcode is a **pair** — `0x4E` (`TENSOR_CUMULATIVE_ARITH_OP`, 78) and `0x5E`
(`TENSOR_CUMULATIVE_BITVEC_OP`, 94) — but this is an **op-CLASS split, not a two-distinct-op split**: both
opcodes share one struct and one `op@36` field; the opcode only decides whether `op@36` is gated by
`is_arith_op` (the `0x4E` arith datapath, including the integer-engine AluOp band and `Rsqrt`) or by
`is_bitvec_op` (the `0x5E` bit/shift/`Crc32` datapath). This is the identical convention used by Tensor-Reduce
(`0x42`/`0x52`) and by the deprecated `PtrMulti` byte-neighbours `0x4F`/`0x5F`.

This page closes the **GPSIMD scan family**: TensorCumulative (`0x4E`/`0x5E`, tensor-only) /
[TensorTensorScan](tensor-tensor-scan.md) (`0xe5`, two-tensor combine then scan) /
[TensorScalarCacheCumulative](ts-cache-cumulative.md) (`0xe6`, scalar-fold scan with a persistent
cross-tile accumulator).

Everything below derives **solely** from static analysis of the shipped GPSIMD device-firmware blob carved
from `libnrtucode_internal.so` (sha256 `b7c67e89…`, **10,276,288 B**, re-verified this pass), the shipped
public ISA C headers (`aws_neuron_isa_tpb_s4d4_tr.h`, `aws_neuron_isa_tpb_common.h`,
`instruction_mapping.json` — each `gcc`/`jq`-verified this pass), and the device DEBUG self-name strings
carved with `strings -a`. The scan datapath vocabulary is carried from the FLIX-decode/TIE-semantics passes
as cited. Every claim carries a confidence tag `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` per the
[confidence model](../../reference/confidence-model.md). Combiner math is grounded in the
[ALU-op matrix](alu-op-matrix.md).

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | `TensorCumulative` = opcode **pair** `0x4E` (`TENSOR_CUMULATIVE_ARITH_OP`, 78) / `0x5E` (`TENSOR_CUMULATIVE_BITVEC_OP`, 94), both `// Y` maintained, **byte-identical value on all four gens**. | `common.h` mariana `:188`/`:189`, sunda `:180`/`:181`, cayman `:183`/`:184`, maverick `:191`/`:192` (4-gen diff) | HIGH/OBSERVED |
| 2 | It binds the **64-byte** `NEURON_ISA_TPB_S4D4_TR_STRUCT` — the **Tensor-Reduce family** struct — shared with 11 opcodes (Reduce `0x42`/`0x52`, TransposeReduce `0x83`/`0x84`, Copy `0x46`, Cast `0x47`, Reciprocal `0x48`, StreamShuffle `0x6a`, StreamTranspose `0x6b`). | `instruction_mapping.json` `struct2opcode[NEURON_ISA_TPB_S4D4_TR_STRUCT]` (`jq`, verbatim) | HIGH/OBSERVED |
| 3 | **Tensor-only scan**: the struct has **one** input tensor (`src_mem_pattern@12`, `MEM_PATTERN4D`, 20 B) and **one** output (`dst_mem_pattern@44`, 20 B), a **single** combiner `op@36` (`ALU_OP`), an **explicit** axis `op_dim@37` (`TENSOR_SUBDIM`). **No** imm, **no** `accumulator_cmd`, **no** second tensor, **no** `REDUCE_OP` field. | `s4d4_tr.h:28-43` + `gcc` compile-verify (sizeof 64, offsets identical sunda/mariana/maverick, this pass) | HIGH/OBSERVED |
| 4 | The **SCAN-emit contract**: `s4d4_tr_same_src_dst_count` ⇒ `same_element_count_m4d(src,dst)` — one out-element per in-element. Tensor-Reduce instead uses a collapse contract on the **same** struct. | `s4d4_tr.h:235-237` verbatim | HIGH/OBSERVED |
| 5 | The `0x4E`/`0x5E` split is an **op-class split**: both forbid `Divide`/`Pow`/`Mod`; arith ⇒ `is_arith_op(op)`, bitvec ⇒ `is_bitvec_op(op)`. The arith branch uses `is_arith_op` (**not** `is_general_arith_op`) — so it **alone** among the three scans admits the integer-engine band (`AddInt`/`MultInt`/…/`ModInt`) and `Rsqrt`. | `tensor_cumulative_valid_alu_op` (`s4d4_tr.h:213-222`) + `is_arith_op`/`is_bitvec_op`/`is_general_arith_op` (`common.h:1859`/`:1892`/`:1910`) verbatim | HIGH/OBSERVED |
| 6 | **`negated@35` forced 0** (`has_zero_negated_field`) — a concrete reduce-vs-scan difference: Tensor-Reduce arith permits `negated ∈ {0,1}`. | `s4d4_tr.h:107-108` + `:190-211` (vs `:96-105`) verbatim | HIGH/OBSERVED |
| 7 | **Dispatch = DVE only**, via the **grouped** self-name worker `"S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle"` (4 copies = the DVE gens). **No** `"P:"` cumulative name; `0x4E`/`0x5E` **absent** from both Q7_POOL `kernel_info` tables. | `strings -a -t x` @ `0x18d1c3`/`0x4274a4`/`0x6ef1c9`/`0x8af573` (this pass) + POOL-table absence (carried) | HIGH/OBSERVED |
| 8 | **DTYPE**: `is_valid_dtype` gate ⇒ FP8/FP16/BF16/FP32 + INT8/16/32 + UINT8/16/32; FP32R **out-only**; **INT64/UINT64 excluded** (unlike Tensor-Reduce); int32/uint32 **accepted** (unlike CacheCumulative). Bitvec adds `in==out ∈ {UINT8,UINT16,UINT32,INT32}` with `LogicalShiftRight`→INT32. | `s4d4_tr.h:196-197`/`:224-233` + `common.h:1455` verbatim | HIGH/OBSERVED |
| 9 | **Per-gen**: scan-defining contract byte-identical on all four gens; deltas are evolutionary plumbing only (sunda/cayman `start_addr`/`t4d`, no indirect-quadrant + narrow int band; mariana/maverick `check_m4d` + indirect-quadrant + full int band). | 4-gen header diff + compile-verify | HIGH/OBSERVED |

One-line semantics:

```
out[i] = out[i-1]  op@36  in[i]          along op_dim@37 (X/XY/XYZ/XYZW),  out_count == in_count
```

---

## 2. The opcodes — `{Arith 0x4E, Bitvec 0x5E}`, `// Y`, all four gens

`NEURON_ISA_TPB_OPCODE_*` (read verbatim, byte-identical value on every gen):

| value | name | maint | struct | gen-present |
|---|---|---|---|---|
| `0x4E` | `TENSOR_CUMULATIVE_ARITH_OP` (78) | `// Y` | `S4D4_TR` | all 4 |
| `0x5E` | `TENSOR_CUMULATIVE_BITVEC_OP` (94) | `// Y` | `S4D4_TR` | all 4 |

Per-gen opcode lines (`rg`, this pass): sunda `common.h:180`/`:181`; cayman `:183`/`:184`; mariana
`:188`/`:189`; maverick `:191`/`:192`. `[HIGH/OBSERVED]`

The **opcode neighbourhood** (mariana, verbatim) shows the `Arith=0x4x` / `Bitvec=0x5x` layout the family uses:

```
0x4D  RNG                            // Y
0x4E  TENSOR_CUMULATIVE_ARITH_OP     // Y   <== this op (Arith class)
0x4F  TENSOR_SCALAR_PTR_MULTI_ARITH  // n, ucode/kaenadve exists, not maintained/used
0x5E  TENSOR_CUMULATIVE_BITVEC_OP    // Y   <== this op (Bitvec class)
0x5F  TENSOR_SCALAR_PTR_MULTI_BITVEC // n, ucode/kaenadve exists, not maintained/used
```

> **GOTCHA — `0x4E`/`0x4F` and `0x5E`/`0x5F` are byte-neighbours but NOT the same family.**
> `0x4F`/`0x5F` (`PtrMulti`) ride the *different* `S4D4_TSM` struct, are `// n` deprecated/unwired, and use
> `is_general_arith_op`. TensorCumulative (`0x4E`/`0x5E`) rides `S4D4_TR`, is `// Y` maintained-and-wired, and
> uses the broader `is_arith_op`. The numeric adjacency is coincidence, not membership. `[HIGH/OBSERVED —
> common.h opcode table + struct2opcode]`

### 2.1 The three scan opcodes — family disambiguation

`common.h` + `instruction_mapping.json` confirm **three distinct** prefix-scan opcodes, on **three distinct**
structs, with **three distinct** operand arities:

| opcode | name (`// Y`) | struct | meaning |
|---|---|---|---|
| `0x4E`/`0x5E` | `TENSOR_CUMULATIVE_ARITH`/`_BITVEC` | `S4D4_TR` (64 B) | **this op** — tensor-only scan |
| `0xe5` | `TENSOR_TENSOR_SCAN_ARITH` | `S2S2D2_STT` (64 B) | two-tensor combine then scan (arith-only) |
| `0xe6` | `TENSOR_SCALAR_CACHE_CUMULATIVE` | `S3D3_TS` (64 B) | scalar-fold scan with persistent cache |

`common.h:305`/`:306` (mariana) verbatim. The full §9 table closes the comparison. `[HIGH/OBSERVED]`

---

## 3. The operand struct — `S4D4_TR` (64 B), compile-verified

`gcc` compile-verify this pass (a real C program that `#include`s each gen's
`aws_neuron_isa_tpb_s4d4_tr.h` and prints `offsetof`/`sizeof`) printed **byte-identical** layouts on
sunda/mariana/maverick:

```c
// sizeof(NEURON_ISA_TPB_S4D4_TR_STRUCT) == 64   (ISA_STATIC_ASSERT, every gen)
typedef struct NEURON_ISA_TPB_S4D4_TR_STRUCT {
    NEURON_ISA_TPB_HEADER        header;            //  0    opcode = 0x4E (Arith) or 0x5E (Bitvec)
    NEURON_ISA_TPB_EVENTS        events;            //  4    wait/update semaphores
    NEURON_ISA_TPB_MEM_PATTERN4D src_mem_pattern;   // 12    *** THE INPUT TENSOR (in), 20 B ***
    NEURON_ISA_TPB_DTYPE         in_dtype;          // 32    is_valid_dtype(in,  AllowFP32R::False)
    NEURON_ISA_TPB_DTYPE         out_dtype;         // 33    is_valid_dtype(out, AllowFP32R::True)
    uint8_t                      num_active_channels;// 34   partition count 1..128 (POOLING_NUM_CHANNELS)
    uint8_t                      negated;           // 35    *** FORCED 0 (has_zero_negated_field) ***
    NEURON_ISA_TPB_ALU_OP        op;                // 36    *** THE SINGLE COMBINER (Arith/Bitvec, §4) ***
    NEURON_ISA_TPB_TENSOR_SUBDIM op_dim;            // 37    *** THE SCAN AXIS — X/XY/XYZ/XYZW ***
    uint8_t                      mask_enable;       // 38    FORCED 0 (mask_enable_zero)
    uint8_t                      reserved1[5];      // 39    must be 0 (s4d4_tr_reserved_zero)
    NEURON_ISA_TPB_MEM_PATTERN4D dst_mem_pattern;   // 44    *** THE OUTPUT TENSOR (out), 20 B; SCAN-emit ***
} NEURON_ISA_TPB_S4D4_TR_STRUCT;                    // sizeof MEM_PATTERN4D = 20, sizeof SUBDIM = 1
```

Compile-output (this pass, all three testable gens identical): `sizeof=64`,
`src=12 in_dtype=32 out_dtype=33 nchan=34 negated=35 op=36 op_dim=37 mask=38 rsvd=39 dst=44`,
`sizeof MEM_PATTERN4D=20 sizeof SUBDIM=1`. `[HIGH/OBSERVED — s4d4_tr.h:28-43 + gcc offsetof]`

> **NOTE — what the struct does NOT carry.** There is **no** `imm`/`imm0`/`imm1` (unlike
> [CacheCumulative](ts-cache-cumulative.md)), **no** `accumulator_cmd` (unlike CacheCumulative), **no**
> second tensor (unlike [TensorTensorScan](tensor-tensor-scan.md)), and **no** `REDUCE_OP` field — that enum
> lives in the *different* `S4D4_CR` struct ([CrossLaneReduce](cross-lane-reduce.md)), not here. The struct is
> exactly `{1 src tensor, 1 dst tensor, 1 op, 1 axis, 2 dtypes, channels, negated/mask}`. `[HIGH/OBSERVED]`

### 3.1 The `S4D4_TR` family — 11 opcodes, one struct

`struct2opcode[NEURON_ISA_TPB_S4D4_TR_STRUCT]` (`jq`, mariana, verbatim) binds exactly:

```
TENSOR_REDUCE_ARITH 0x42, TRANSPOSE_TENSOR_REDUCE_ARITH 0x83, TENSOR_REDUCE_BITVEC 0x52,
TRANSPOSE_TENSOR_REDUCE_BITVEC 0x84, TENSOR_CUMULATIVE_ARITH 0x4E, TENSOR_CUMULATIVE_BITVEC 0x5E,
COPY 0x46, CAST 0x47, RECIPROCAL 0x48, STREAM_SHUFFLE 0x6a, STREAM_TRANSPOSE 0x6b
```

The struct's own header banner (`s4d4_tr.h:13-24`, all 4 gens verbatim) names the same set and pins the scan
semantics: *"TensorCumulative[Arith/Bitvec]Op — performs a cumulative operation on all elements in an input
tensor, along a given axis."* TensorCumulative is the **scan member** of this 11-op reduce/movement family.
`[HIGH/OBSERVED]`

---

## 4. The validity contract — `is_valid_tensor_cumulative`

Source: `aws_neuron_isa_tpb_s4d4_tr.h` (mariana `190-237`), verbatim this pass. As annotated pseudocode:

```c
// s4d4_tr.h:190-211 — verbatim, all 4 gens (mariana/maverick add the two indirect_quadrant lines)
bool is_valid_tensor_cumulative(Inst i) {
  return has_valid_neuron_header(i)
      && has_valid_neuron_events(i)
      && has_tensor_cumulative_opcode(i)                       // opcode ∈ {0x4E, 0x5E} ONLY (:367)
      && s4d4_tr_reserved_zero(i)                              // reserved1[0..4] == 0
      && mask_enable_zero(i)                                   // mask_enable@38 == 0
      && is_valid_dtype(i.s4d4_tr.in_dtype,  DtypeAllowFP32R::False)   // §7
      && is_valid_dtype(i.s4d4_tr.out_dtype, DtypeAllowFP32R::True)    // §7
      && is_valid_enum(AluOp, i.s4d4_tr.op)                    // single op@36
      && is_valid_subdim(i.s4d4_tr.op_dim)                     // op_dim ∈ {X,XY,XYZ,XYZW} = scan axis
      && has_valid_active_channel_range(i.s4d4_tr.num_active_channels, POOLING_NUM_CHANNELS)  // 1..128
      && check_m4d_active_channels(i.s4d4_tr.src_mem_pattern, i.s4d4_tr.num_active_channels)  // sunda/cayman: start_addr_*
      && check_m4d_active_channels(i.s4d4_tr.dst_mem_pattern, i.s4d4_tr.num_active_channels)
      && mem4d_valid(i.s4d4_tr.src_mem_pattern, i.s4d4_tr.in_dtype,  WriteTensor::False, PSUM::True, SBUF::True)
      && mem4d_valid(i.s4d4_tr.dst_mem_pattern, i.s4d4_tr.out_dtype, WriteTensor::True,  PSUM::True, SBUF::True)
      && indirect_quadrant_check_src4d(i.s4d4_tr.src_mem_pattern)               // mariana/maverick only
      && indirect_quadrant_check_src4d_dst4d(i.s4d4_tr.src_mem_pattern, i.s4d4_tr.dst_mem_pattern)  // ditto
      && s4d4_tr_same_src_dst_count(i)            // *** SCAN-EMIT: same_element_count_m4d(src,dst) ***
      && tensor_cumulative_valid_alu_op(i)        // the Arith/Bitvec op-class split (§4.1)
      && tensor_cumulative_valid_dtype(i)         // the bitvec dtype narrowing (§7)
      && has_zero_negated_field(i);               // negated@35 == 0 (UNLIKE reduce-arith)
}
```

> **CORRECTION — TensorCumulative is NOT a "REDUCE_OP path" op.** Earlier survey notes described `0x4E`/`0x5E`
> as a reduce-family cumulative on a `REDUCE_OP` field. That is wrong: there is **no** `REDUCE_OP` field in
> `S4D4_TR`. The combiner is the **same** single `op@36` (`ALU_OP`) that Tensor-Reduce uses; the **only**
> distinction is the element-count contract — `same_element_count_m4d` (SCAN-emit, one-out-per-in) here vs the
> collapse contract in `is_valid_tensor_reduce`. Same op, same axis, same struct; **reduce collapses, cumulative
> emits a running partial.** `[HIGH/OBSERVED — both validity functions verbatim, s4d4_tr.h]`

> **QUIRK — `negated` is forced 0 for the scan but free for the reduce.** Tensor-Reduce arith uses
> `has_valid_reduce_negated` (`negated ∈ {0,1}`, `s4d4_tr.h:96-105`); TensorCumulative uses
> `has_zero_negated_field` (`negated == 0`, `:107-108`). A "negated cumulative" must apply the negation in a
> preceding op. `[HIGH/OBSERVED]`

### 4.1 The Arith-vs-Bitvec split — an op-class gate, not a second operator

```c
// s4d4_tr.h:213-222 — verbatim, byte-identical all 4 gens
bool tensor_cumulative_valid_alu_op(Inst i) {
  return is_valid_enum(AluOp, i.s4d4_tr.op)
      && i.s4d4_tr.op != AluOp::Divide      // 0x07  — forbidden in BOTH classes
      && i.s4d4_tr.op != AluOp::Pow         // 0x1A  — forbidden in BOTH classes
      && i.s4d4_tr.op != AluOp::Mod         // 0x1B  — forbidden in BOTH classes
      && ( !tensor_cumulative_bitvec(i) || is_bitvec_op(i.s4d4_tr.op) )  // 0x5E ⇒ op ∈ is_bitvec_op
      && ( !tensor_cumulative_arith(i)  || is_arith_op(i.s4d4_tr.op)  ); // 0x4E ⇒ op ∈ is_arith_op
}
// tensor_cumulative_arith(i)  := (opcode == 0x4E)     (s4d4_tr.h:132-134)
// tensor_cumulative_bitvec(i) := (opcode == 0x5E)     (s4d4_tr.h:128-130)
```

The op sets (`common.h`, verbatim; AluOp ordinals from `common.h:1034-1072`):

```c
// is_arith_op (common.h:1859) — the FULL arith set, INCLUDING the int band + Rsqrt:
//   {Bypass, Add, Subtract, Mult, Divide(0x07), Max, Min, LogicalAnd/Or/Xor,
//    IsEQ/GT/GE/LE/LT/NE, AbsoluteDiff, AbsoluteValue, AbsMax, AbsMin, ReLU, Square,
//    Pow(0x1A), Mod(0x1B), AddInt(0xC4), MultInt(0xC5), SubtractInt(0xC6),
//    DivideInt(0xC7), ModInt(0xC8), Rsqrt(0x1D)}
//   ⇒ TensorCumulative ARITH (0x4E) = is_arith_op MINUS {Divide, Pow, Mod}

// is_bitvec_op (common.h:1892):
//   {Bypass, BitwiseNot, ArithShiftLeft, ArithShiftRight, LogicalShiftLeft,
//    LogicalShiftRight, BitwiseAnd, BitwiseOr, BitwiseXor, Crc32(0x1C)}
//   ⇒ TensorCumulative BITVEC (0x5E) = the full bitvec set incl. Crc32
//     (the Div/Pow/Mod exclusion is a no-op — none are bitvec ops)
```

> **NOTE — `op@36` is one byte; the opcode picks the class predicate.** `0x4E` and `0x5E` carry the **same**
> `op@36` field and the **same** struct; the opcode chooses **which** op-class `op@36` may belong to. This is
> the identical split-pattern as Tensor-Reduce `0x42`/`0x52` and `PtrMulti` `0x4F`/`0x5F` — a datapath-class
> gate, not a second operator slot. `[HIGH/OBSERVED]`

**The distinguishing capability — integer scans.** The arith branch uses `is_arith_op`, **not**
`is_general_arith_op`:

```c
// common.h:1910 — verbatim
bool is_general_arith_op(AluOp op) {
  return is_arith_op(op)
      && op != AluOp::Divide && op != AluOp::Pow && op != AluOp::Mod
      && !is_valid_int_aluop(op)   // strips the AddInt/MultInt/SubtractInt/DivideInt/ModInt band
      && op != AluOp::Rsqrt;
}
```

So `TensorCumulative-Arith` (`is_arith_op` minus `{Div,Pow,Mod}`) is broader than general-arith by exactly
**`{AddInt, MultInt, SubtractInt, DivideInt, ModInt, Rsqrt}`**. [TensorTensorScan](tensor-tensor-scan.md)
(`0xe5`), `PtrMulti` (`0x4F`/`0x5F`), and [CacheCumulative](ts-cache-cumulative.md) (`0xe6`) **all** use
`is_general_arith_op` — so TensorCumulative is the **only** scan op that supports the integer-engine AluOps
and `Rsqrt`, and the **only** one with a bitvec twin (shift/`Crc32`/`Bitwise*` scans via `0x5E`). This mirrors
Tensor-Reduce's own broader gate (`tensor_reduce_valid_alu_op` allows `is_general_arith_op ||
is_valid_tensor_reduce_int_op`). `[HIGH/OBSERVED — all three predicate bodies verbatim]`

---

## 5. The dispatch surface — DVE grouped worker (not POOL, not standalone)

The dispatch surface is resolved from the device DEBUG self-name strings carved from
`libnrtucode_internal.so` (sha256 `b7c67e89…`) with `strings -a`, applying the POOL-vs-DVE discriminator
(`"S:"` = DVE / `"P:"` = POOL self-name prefix; per-gen blob multiplicity; Q7_POOL `kernel_info` membership).

### 5.1 The grouped DVE self-name

The blob carries exactly **one** self-name for the cumulative op, and it is **grouped** — confirmed this pass:

```
$ strings -a -t x libnrtucode_internal.so | rg 'Tensor-Cumulative/Copy/Cast/Stream-Shuffle'
 18d1c3 S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle    -> cayman        (BEGIN on cayman @0x18c119)
 4274a4 S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle    -> mariana
 6ef1c9 S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle    -> mariana_plus
 8af573 S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle    -> maverick
```

4 copies = the 4 DVE-equipped gens (cayman/mariana/mariana_plus/maverick). There is **no** `"S:
TensorCumulative"` standalone name. The grouped name bundles the four `S4D4_TR` **movement-class** ops that
move/transform elements one-out-per-in **without** a cross-lane fold: Copy (`0x46`), Cast (`0x47`),
StreamShuffle (`0x6a`), and the scan-emit Cumulative (`0x4E`/`0x5E`). `[HIGH/OBSERVED — strings -a -t x +
the per-gen BEGIN markers, this pass]`

The DVE worker self-name table reads contiguously, listing this group **alongside** separate workers (cayman,
verbatim ordering):

```
S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle   <- this op's worker (movement-class, scan-emit)
S: Stream-Transpose                              (separate — the cross-lane transpose)
S: Tensor-Reduce                                 (separate — the COLLAPSE twin, §1)
S: TensorScalarCacheCumulative                   (separate — 0xe6, ts-cache-cumulative.md)
S: TensorTensorScan                              (separate — 0xe5, tensor-tensor-scan.md)
```

So the reduce/movement family splits its DVE workers by datapath: the **collapse** (Tensor-Reduce) is one
worker, the cross-lane **transpose** (Stream-Transpose) another, and the **one-out-per-in movement** group
(incl. the scan-emit cumulative) is the grouped worker. `[HIGH/OBSERVED — contiguous self-name table,
cayman+mariana cross-checked]`

### 5.2 Not a POOL kernel

There is **no** `"P:"` cumulative self-name — re-confirmed this pass (`strings -a | rg -i 'P[0-9%]*: .*Cumulative'`
returns nothing). The blob's only `"P:"` reduce/movement names are
`P%i: TensorReduceBitvec : num_chans=%0d`, `P%i: Copy : num_chans = %0d`, `P%i: Cast : num_chans = %0d` (and
the POOL `unimplemented tensor-reduce ALU op` handler). And `0x4E`/`0x5E` are **absent** from both carved
Q7_POOL `kernel_info` opcode lists:

```
SUNDA  POOL opcodes : 41 43 44 46 47 49 67 68 74 79 7a 7c 7d 7e 92 b8 bb e7   (no 4E/5E)
CAYMAN POOL opcodes : 41 45 46 47 51 52 7b 7c 7d 7e be f0 f2                  (no 4E/5E)
```

> **GOTCHA — Copy/Cast/Tensor-Reduce are dual-surface; TensorCumulative is DVE-only.** Copy (`0x46`), Cast
> (`0x47`), and Tensor-Reduce-bitvec (`0x52`) appear in **both** the `"P:"` POOL table and an `"S:"` DVE name.
> The scan-emit cumulative has **only** the DVE `"S:"` name. A reimplementation must route `0x4E`/`0x5E` to the
> DVE engine; the POOL `kernel_info` table will not resolve them. `[HIGH/OBSERVED self-names; POOL-table
> opcode lists CARRIED]`

### 5.3 The canonical dispatch chain (per DVE gen)

```
[SEQ opcode 0x4E/0x5E]
  -> DVE dispatcher  ("S: Dispatch opcode=0x%x")
  -> grouped "S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle" DVE worker
  -> per-op branch on opcode (Cumulative vs Copy vs Cast vs Shuffle)
  -> the prefix-scan datapath: rotrn ⊕ combine over op@36 along op_dim (§6)
  -> writes out[i] = running cumulative   (NO POOL kernel_info hop, NO accumulator_cmd cache)
```

The SEQ-side `"S: Dispatch opcode=0x%x"` + `"S: BEGIN on cayman"` markers anchor the dispatcher (`@0x18c100`/
`@0x18c119`, this pass). The dispatcher + grouped-worker self-name are HIGH/OBSERVED; the per-op intra-worker
branch is MED — the worker body FLIX/literal-desyncs under stock `xtensa-elf-objdump` (the recurring
`.byte 0x2f/0x4f/0x5f/0x6f/0x8f` FLIX literal-pool lead bytes). `[dispatcher + grouped worker HIGH/OBSERVED;
per-op branch MED/INFERRED]`

---

## 6. The scan datapath — `rotrn ⊕ combine` (the DVE software scan)

There is **no hardware "scan" opcode** on this core (the decoded TIE source has no `ivp_sem_vec_scan`
semantic and no single scan TIE opcode). Every DVE scan kernel runs a **software Hillis-Steele recurrence**
built from two TIE primitives:

- **(a) the ROTATE** — `op_ROT ∈ {ROTRI2NX8, ROTRINX16, ROTRIN_2X32, ROTRNX16, ROTRN_2X32}` from
  `ivp_sem_vec_shift`: a **closed** lane-rotate (wrap mod N) that shifts the partial-accumulator vector by a
  lane stride.
- **(b) the COMBINE** — a per-step ALU/MUL op folding the rotated partial with the un-rotated vector. **For
  TensorCumulative this is the single `op@36` directly** (the scan combiner). Examples: `ivp_bmaxn_2x32`
  (`op=Max`), `ivp_addn_2x32t` (`op=Add`), `ivp_mulpn16xr16` (`op=Mult`), `ivp_babssub2nx8` (`op=AbsoluteDiff`).

The recurrence, as pseudocode (combiner = `op@36`; the rotated register **is** the loop-carried accumulator):

```c
// Inclusive prefix scan along op_dim, in-register, log2(N) passes
// partial[] := in[]   (one SIMD vector window over the op_dim axis)
for (int stride = 1; stride < N; stride <<= 1) {           // p = 0,1,2,...  (stride = 2^p)
    rotated  = rotrn(partial, stride);                     // op_ROT, closed lane-rotate
    partial  = combine(partial, rotated, /*op=*/op@36);    // op@36 is the combiner DIRECTLY
}
// after log2(N) passes: partial[i] = in[0] op in[1] op ... op in[i]  (inclusive)
// out[i] = partial[i]                                      (one out-element per in-element)
```

The byte-exact `rotrn ⊕ combine` FLIX bundles (rotate and combine co-issued in **one** FLIX VLIW word) are
carried from the FLIX-decode pass:

```
F3  @0x4916 : { … ivp_bmaxn_2x32  | ivp_rotrn_2x32 }   rotate ⊕ MAX     (op=Max scan step)
F3  @0x29c5 : { … ivp_mulpn16xr16 | ivp_rotrn_2x32 }   rotate ⊕ MUL     (op=Mult)
F3  @0x939d : { … ivp_babssub2nx8 | ivp_rotrn_2x32 }   rotate ⊕ AbsSub
F11 @0xdf03 : { … ivp_babssub2nx8 | ivp_rotri2nx8 }    immediate-rotate ⊕ AbsSub
```

**How TensorCumulative differs at the datapath from the other two scans:**

- **One op, no pre-transform.** TensorCumulative's combine op **is** `op@36` (the simplest mapping — no scalar
  fold, no two-tensor combine). [CacheCumulative](ts-cache-cumulative.md) first folds `in[i] op0 scalar0` then
  scans under `op1`; [TensorTensorScan](tensor-tensor-scan.md) first combines `src0 op0 src1` then scans.
- **Explicit axis.** TensorCumulative scans along the **explicit** `op_dim@37` subdim(s) of a 4-D
  `MEM_PATTERN4D`; the address generator walks the nested tile and the scan runs along the selected subdim(s).
  The other two scans use an implicit per-element-sequence axis.
- **No cross-tile carry.** TensorCumulative has **no** `accumulator_cmd` field — like TensorTensorScan
  (Idle-forced) and **unlike** CacheCumulative (the persistent `ACCUM_CMD` cache). A multi-tile cumulative
  must be expressed as a single instruction over the full `MEM_PATTERN4D`, not chained across calls.

> **NOTE — confidence boundary.** The `rotrn ⊕ combine` vocabulary and slot-packing are HIGH/OBSERVED/CARRIED
> at the **DVE-scan-family** level, and "`op@36` is the combiner" is structural. The PERF image strips
> self-names and the worker body FLIX-desyncs, so the **bind of these specific bundles to the `0x4E`/`0x5E`
> grouped worker**, and the **inclusive-vs-exclusive emit-index boundary**, are MED. `[vocabulary
> HIGH/CARRIED; per-0x4E/0x5E bind MED]`

---

## 7. The dtype matrix

### 7.1 The generic gate

```c
// is_valid_tensor_cumulative §4: in_dtype is_valid_dtype(AllowFP32R::False),
//                                out_dtype is_valid_dtype(AllowFP32R::True)
bool is_valid_dtype(Dtype d, DtypeAllowFP32R allow_fp32r) {   // common.h:1455 verbatim
  return dtype_invalid_check(d)
      && dtype_fp32r_illegal_check(d, allow_fp32r)
      && dtype_uint64_illegal_check(d, DtypeAllowU64::False)   // U64 hard-illegal
      && dtype_int64_illegal_check(d,  DtypeAllowI64::False)   // I64 hard-illegal
      && dtype_fp4_illegal_check(d,    DtypeAllowFP4::False)   // FP4 hard-illegal
      && is_valid_enum(Dtype, d);
}
```

| dtype (ordinal) | in (`AllowFP32R::False`) | out (`AllowFP32R::True`) |
|---|---|---|
| `FP8_EXP3` `0xD` / `FP8_EXP4` `0xE` / `FP8_EXP5` `0xF` | ✓ | ✓ |
| `BFLOAT16` `0x6` / `FP16` `0x7` / `FP32` `0xA` | ✓ | ✓ |
| `FP32R` `0xB` | ✗ (rejected) | ✓ (out-only) |
| `INT8` `0x2` / `INT16` `0x4` / `INT32` `0x8` | ✓ | ✓ |
| `UINT8` `0x3` / `UINT16` `0x5` / `UINT32` `0x9` | ✓ | ✓ |
| `INT64` `0xC` / `UINT64` `0x1` | ✗ | ✗ |
| `INVALID` `0x0` / FP4 | ✗ | ✗ |

Dtype ordinals per `common.h`: `INVALID=0, UINT64=1, INT8=2, UINT8=3, INT16=4, UINT16=5, BFLOAT16=6,
FP16=7, INT32=8, UINT32=9, FP32=0xA, FP32R=0xB, INT64=0xC, FP8_EXP3=0xD, FP8_EXP4=0xE, FP8_EXP5=0xF`.
Channels `1..128`. `[HIGH/OBSERVED]`

> **Two dtype deltas vs the scan siblings:**
>
> - **vs Tensor-Reduce — TensorCumulative excludes 64-bit.** Tensor-Reduce's gate is `is_valid_dtype_64(…,
>   AllowU64::True, AllowI64::True)` (`s4d4_tr.h:79-80`), which **permits** INT64/UINT64. TensorCumulative
>   uses plain `is_valid_dtype` (U64/I64 hard-illegal), so it **excludes** the 64-bit types Tensor-Reduce
>   allows. `[HIGH/OBSERVED — the two gates verbatim, s4d4_tr.h]`
> - **vs CacheCumulative — TensorCumulative accepts int32/uint32.** CacheCumulative excludes int32/uint32 (its
>   persistent cross-tile accumulator could overflow). TensorCumulative has **no** persistent accumulator, so
>   no int32-overflow guard is needed and plain `is_valid_dtype` accepts int32/uint32. `[HIGH/OBSERVED — the
>   absence of the int32-exclusion predicate]`

### 7.2 The bitvec-only narrowing

```c
// tensor_cumulative_valid_dtype — s4d4_tr.h:224-233, verbatim, byte-identical all 4 gens
bool tensor_cumulative_valid_dtype(Inst i) {
  return !tensor_cumulative_bitvec(i)                            // ARITH (0x4E): no extra constraint
      || (   i.s4d4_tr.in_dtype == i.s4d4_tr.out_dtype           // BITVEC (0x5E): in == out
          && ( i.s4d4_tr.in_dtype == UINT8  || i.s4d4_tr.in_dtype == UINT16
            || i.s4d4_tr.in_dtype == UINT32 || i.s4d4_tr.in_dtype == INT32 )   // ∈ {U8,U16,U32,I32}
          && ( i.s4d4_tr.op != AluOp::LogicalShiftRight
            || i.s4d4_tr.in_dtype == INT32 ) );                  // LSR restricted to INT32
}
```

So `0x5E` requires `in==out`, the dtype ∈ `{UINT8, UINT16, UINT32, INT32}`, and `LogicalShiftRight` only on
INT32. The `0x4E` arith opcode carries **no** extra dtype constraint beyond §7.1. This mirrors the bitvec
narrowing in `tensor_reduce_valid_dtype`. `[HIGH/OBSERVED]`

---

## 8. Per-generation presence

| gen | opcode `0x4E`/`0x5E` | `S4D4_TR` | scan/count check | DVE grouped worker | wired? |
|---|---|---|---|---|---|
| SUNDA (NC-v2) | defined `// Y` | 64 B identical | `start_addr_*` / `t4d`, no indirect-quadrant | (no DVE blob carved) | defined |
| CAYMAN (NC-v3) | defined `// Y` | 64 B identical | `start_addr_*` / `t4d`, no indirect-quadrant | `@0x18d1c3` | **WIRED** |
| MARIANA (NC-v4) | defined `// Y` | 64 B identical | `check_m4d_*` / `m4d` + indirect-quadrant | `@0x4274a4` | **WIRED** |
| MARIANA_PLUS (v4+) | (mariana header) | 64 B identical | `check_m4d_*` / `m4d` + indirect-quadrant | `@0x6ef1c9` | **WIRED** |
| MAVERICK (NC-v5) | defined `// Y` | 64 B identical | `check_m4d_*` / `m4d` + indirect-quadrant | `@0x8af573` | **WIRED** |

The **scan-defining invariant is byte-identical on all four gens**: opcode `0x4E`/`0x5E` (`// Y`), the 64-B
`S4D4_TR` struct (compile-verified, sizeof + offsets identical), the single `op@36` + `op_dim@37` axis,
`s4d4_tr_same_src_dst_count` (SCAN-emit), `has_zero_negated_field`, `tensor_cumulative_valid_alu_op` (the
Arith/Bitvec split), and `tensor_cumulative_valid_dtype` (the bitvec narrowing). `[HIGH/OBSERVED — header
diff + compile-verify]`

The per-gen deltas are **evolutionary plumbing, not scan semantics**:

- **(i) channel/count check.** SUNDA/CAYMAN use `start_addr_active_channels` + `same_element_count_t4d` and
  **no** indirect-quadrant checks; MARIANA/MAVERICK use `check_m4d_active_channels` +
  `same_element_count_m4d` + `indirect_quadrant_check_src4d{,_dst4d}`. Both enforce `src.count == dst.count`
  (the SCAN-emit contract). `[HIGH/OBSERVED]`
- **(ii) int-band width.** SUNDA defines **only** `AddInt`/`MultInt`/`SubtractInt` (`0xC4`-`0xC6`) of the int
  band (no `DivideInt`/`ModInt` — confirmed `common.h` sunda `:948-950` with no `0xC7`/`0xC8`), so its
  `is_arith_op` int support is narrower; the full band (`AddInt`/…/`ModInt`) is MARIANA+. The practical `0x4E`
  arith op set is therefore slightly larger on mariana/maverick. `[HIGH/OBSERVED]`

> **NOTE — MAVERICK / v5 is header-observed.** The MAVERICK opcode line, struct, and validity functions are
> read from the shipped MAVERICK arch-isa header (byte-grounded) and the MAVERICK DVE self-name is present in
> the blob (`@0x8af573`). The *interior* MAVERICK worker datapath is not independently byte-decoded here; v5
> interior behaviour is INFERRED from the shared family engine. SUNDA defines the opcode+struct but its DVE
> worker blob was not separately carved this pass (it shares the earliest blob layout). `[header
> HIGH/OBSERVED; v5 interior INFERRED]`

---

## 9. The scan family — closed

The ISA has **exactly three** prefix-scan ("cumulative"/"scan") opcodes, all now decoded. They share the
**same** software `rotrn ⊕ combine` Hillis-Steele scan **engine** (no hardware scan op) but differ in operand
arity, op-control, carry model, and struct:

| property | **TensorCumulative** `0x4E`/`0x5E` (this) | [TensorTensorScan](tensor-tensor-scan.md) `0xe5` | [CacheCumulative](ts-cache-cumulative.md) `0xe6` |
|---|---|---|---|
| maint | `// Y` (Arith + Bitvec) | `// Y` (**ARITH-ONLY**) | `// Y` |
| struct | `S4D4_TR` (64 B) | `S2S2D2_STT` (64 B) | `S3D3_TS` (64 B) |
| family | reduce / movement | scalar-tensor-tensor (STT) | TensorScalar\* |
| inputs | **1 tensor** (`src`) | 2 tensor + 1 scalar | 1 tensor + 2 scalar |
| tensor pattern | `MEM_PATTERN4D` (4-D) | `TENSOR2D` (2-D) | `MEM_PATTERN3D` (3-D) |
| op control | **single** `op@36` | `op0@36` + `op1@37` | `op0@36` + `op1@37` |
| op-class set | Arith=`is_arith_op` / Bitvec=`is_bitvec_op` (**incl. INT band + Rsqrt**) | `op0,op1` general_arith (no bitvec twin) | `op0` general_arith / `op1 ∈ {Add,Sub,Mult,Max,Min}` |
| scan axis | **EXPLICIT** `op_dim@37` | implicit | implicit |
| pre-scan transform | **NONE** (`op@36` IS the combiner) | `c[i]=(src0 op0 imm0) op1 src1[i]` | `tmp[i]=src[i] op0 scalar0` |
| `accumulator_cmd` | **ABSENT** (no field) | FORCED `Idle` | FORCED non-Idle `{Z/A/LA}` |
| cross-tile carry | **NONE** | NONE (intra-instr) | **YES** (the `ACCUM_CMD` cache) |
| scan-emit contract | `same_element_count_m4d` | `s2s2d2_stt_dst_cnt` | `same_element_count_m3d` |
| dtype int32/uint32 | **ACCEPTED** | ACCEPTED | EXCLUDED (overflow guard) |
| dtype int64/uint64 | **EXCLUDED** (`is_valid_dtype`) | ACCEPTED (`is_valid_dtype_*`) | EXCLUDED (cache rule) |
| dispatch | DVE **grouped** worker `"…Cumulative/Copy/Cast/Stream-Shuffle"` | DVE `"S: TensorTensorScan"` (standalone) | DVE `"S: TensorScalarCacheCumulative"` (standalone) |
| POOL kernel? | NO | NO | NO |
| scan datapath | `rotrn ⊕ combine` (Hillis-Steele) | `rotrn ⊕ combine` (same engine) | `rotrn ⊕ combine` (same engine) |

**Synthesis.** TensorCumulative is the **simplest and most general** of the three: the pure single-tensor
prefix scan `out[i] = out[i-1] op in[i]` along an **explicit** axis, with the **widest op set** (it alone
admits the integer-engine AluOps `AddInt`/…/`ModInt` + `Rsqrt` via `is_arith_op`, where the other two use
`is_general_arith_op`), the **only** one with a bitvec twin (`0x5E` — scans `LogicalShiftRight`/`BitwiseXor`/
`Crc32`), and the **only** one on the 4-D reduce-family struct (so it integrates with the
[Tensor-Reduce](tensor-reduce.md) COLLAPSE on the same `op@36`/`op_dim@37` control). TensorTensorScan adds a
two-tensor combine before the scan; CacheCumulative adds a scalar fold plus a persistent cross-tile
`ACCUM_CMD` cache. The **carry model** is the cleanest fingerprint: TensorCumulative and TensorTensorScan are
self-contained (no cross-tile cache), CacheCumulative carries the accumulator across calls. With this op, the
scan family is closed — three opcodes, three structs, one shared `rotrn ⊕ combine` DVE scan engine.
`[HIGH/OBSERVED inputs; the synthesis is direct from the three headers + the three self-name surfaces]`

---

## 10. Honesty ledger

**HIGH / OBSERVED** (direct header verbatim / compile-output / `jq` / `strings -a` self-name string, this
pass):

- Opcode `TENSOR_CUMULATIVE_ARITH 0x4E` / `BITVEC 0x5E`, `// Y` maintained, byte-identical value all four gens
  (`common.h` per-gen lines §2); the opcode neighbourhood (RNG `0x4D` / PtrMulti `0x4F`/`0x5F`) and the
  three-scan-op disambiguation.
- `S4D4_TR` binds TensorCumulative + the 11-op reduce/movement family (`struct2opcode`, `jq`, verbatim §3.1);
  the 64-B struct (`src@12, in_dtype@32, out_dtype@33, nchan@34, negated@35, op@36, op_dim@37, mask@38,
  reserved@39, dst@44`; sizeof 64) **compile-verified byte-identical** sunda/mariana/maverick this pass — no
  imm field, no `accumulator_cmd`, no `REDUCE_OP`, no second tensor.
- `is_valid_tensor_cumulative` (§4): single `op@36`, `is_valid_subdim(op_dim)` axis, `s4d4_tr_same_src_dst_count`
  = `same_element_count_m4d` (SCAN-emit one-out-per-in), `has_zero_negated_field` (`negated` forced 0, unlike
  reduce-arith's `{0,1}`), `mask_enable_zero`, `reserved_zero` — verbatim header.
- `tensor_cumulative_valid_alu_op` (§4.1): the Arith=`is_arith_op` / Bitvec=`is_bitvec_op` op-class split, both
  minus `{Div,Pow,Mod}` — verbatim, byte-identical all 4 gens; `is_arith_op`/`is_bitvec_op`/
  `is_general_arith_op` bodies verbatim; the int-band breadth (TensorCumulative Arith alone admits
  `AddInt`/…/`ModInt` + `Rsqrt`).
- `tensor_cumulative_valid_dtype` (§7.2): the bitvec-only `in==out ∈ {U8/16/32,I32}` + LSR→INT32 constraint;
  the in/out `is_valid_dtype` gate (FP32R-asymmetric, U64/I64 excluded — so TensorCumulative excludes 64-bit
  that Tensor-Reduce allows, and accepts int32/uint32 that CacheCumulative excludes).
- Dispatch: the grouped DVE self-name `"S: Tensor-Cumulative/Copy/Cast/Stream-Shuffle"` (4 copies = cay/mar/
  mar_plus/mav; offsets `0x18d1c3`/`0x4274a4`/`0x6ef1c9`/`0x8af573`), its absence as a standalone or `"P:"`
  name; the `"P:"` reduce/copy/cast surface present but **no** `"P:"` cumulative; `0x4E`/`0x5E` absent from
  both Q7_POOL `kernel_info` tables (CARRIED) — DVE-only via the movement-class grouped worker.
- Container sha256 `b7c67e89…` (10,276,288 B) re-verified this pass.

**MED / INFERRED:**

- The bind of the `rotrn ⊕ combine` FLIX bundles to the `0x4E`/`0x5E` grouped worker **specifically**: the
  scan vocabulary is established at the DVE-scan-**family** level (the PERF image strips self-names; the worker
  body FLIX-desyncs); "`op@36` IS the scan combiner" is structural; the inclusive-vs-exclusive emit-index
  boundary sits in the desynced worker compute body. `[CARRIED HIGH at family level, MED for per-worker bind]`
- The per-op intra-worker branch (Cumulative vs Copy vs Cast vs Shuffle inside the grouped worker) — inferred
  from the grouped self-name + the `S4D4_TR` family layout; the exact branch is in the desynced worker body.
- MAVERICK / v5 **interior** worker datapath — header + self-name observed; interior behaviour inferred from
  the shared family engine.

**LOW / UNRECOVERED:**

- The `0x4E`/`0x5E` → grouped-worker descriptor **bytes** (the DVE `kernel_info` registration literal):
  out-of-carve / runtime-bound.

> **FLIX-DESYNC FLAG.** The DVE worker bodies desync under stock `xtensa-elf-objdump` on the recurring
> `.byte 0x2f/0x4f/0x5f/0x6f/0x8f` FLIX literal-pool lead bytes. The opcode values, the struct
> (compile-verified, not disasm), the validity-function pseudocode (header comments, not disasm), and the
> self-name strings (`strings -a`, not disasm) are **all** byte-clean and unaffected. The scan datapath
> vocabulary is CARRIED HIGH from the FLIX-decode/TIE-semantics passes; only the per-`0x4E`/`0x5E`-worker bind
> and the emit-index boundary are MED.

---

## 11. Reproduction

```sh
H=…/custom_op/c10/include
F=…/custom_op/c10/lib/libnrtucode_internal.so          # sha256 b7c67e89…, 10,276,288 B

# opcodes (0x4E/0x5E // Y all gens):
rg 'TENSOR_CUMULATIVE_(ARITH|BITVEC)' $H/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h

# struct + validity (s4d4_tr.h): struct @28; is_valid_tensor_cumulative @190;
#   tensor_cumulative_valid_alu_op @213; tensor_cumulative_valid_dtype @224;
#   s4d4_tr_same_src_dst_count @235; has_tensor_cumulative_opcode @367

# alu-op sets (common.h): is_arith_op @1859; is_bitvec_op @1892; is_general_arith_op @1910;
#   is_valid_int_aluop @1654; AluOp enum @1034; TENSOR_SUBDIM @1195; is_valid_subdim @1551;
#   is_valid_dtype @1455

# binding (S4D4_TR -> the 11 opcodes incl. 0x4E/0x5E):
jq -r '.struct2opcode.NEURON_ISA_TPB_S4D4_TR_STRUCT' $H/.../instruction_mapping.json

# struct compile-verify (sizeof 64 + offsets, every gen):
gcc -I $H/neuron_<gen>_arch_isa/tpb tc_verify.c && ./a.out

# dispatch self-name (4 copies = DVE gens cay/mar/mar_plus/mav):
strings -a -t x $F | rg 'Tensor-Cumulative/Copy/Cast/Stream-Shuffle'   # -> 0x18d1c3/4274a4/6ef1c9/8af573
strings -a    $F | rg -i 'P[0-9%]*: .*Cumulative'                      # -> (none: DVE-only, no POOL name)
```
