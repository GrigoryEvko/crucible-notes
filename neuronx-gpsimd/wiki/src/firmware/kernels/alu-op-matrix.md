# The ALU-Op Datapath + Dtype Matrix

> **What this page is.** The canonical reference for `alu_op.cpp` — the **shared, engine-agnostic
> scalar ALU-op semantic-reference evaluator** that *every* GPSIMD elementwise kernel funnels its
> per-element math through. [Tensor-Tensor](tensor-tensor.md), [Tensor-Scalar](tensor-scalar.md),
> [Scalar-Tensor-Tensor](scalar-tensor-tensor.md), [TensorScalarSelect](tensor-scalar.md),
> Pool, Activate and the conditional-branch `BranchCompare` instruction all decode their own wire
> formats, then call the **same** `R[d] = OP(R[s0], R[s1]/imm)` evaluator implemented here. This
> page delivers, reimplementation-grade: (1) the **full `NEURON_ISA_TPB_ALU_OP` enum** with the
> per-generation growth **33 → 60 → 64 → 65** and the exact added ops; (2) **what each op computes**
> (per-op datapath semantics + the native Xtensa scalar primitive and the `ivp_*` vector micro-op it
> maps to); (3) the **per-op × per-dtype support matrix** (the validity predicates that gate which op
> is legal for which dtype); (4) the **arith-vs-bitvec split** and the **32-bit vs 64-bit dtype
> dispatch**; (5) the reverse-operand / saturation / rounding (RNE) semantics. It is the dtype-matrix
> prerequisite the whole elementwise-kernel family depends on.

The `alu_op.cpp` file name and the line numbers `141/196/220/231/262` are the firmware DEBUG build's
**own baked assert strings** — used here purely as structure/order anchors. Every confidence tag is
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`. The arch-isa C headers were compiled-and-read this
session; the firmware was disassembled with the shipped Cadence `xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`). FLIX/literal-pool desync of the function *bodies* is flagged honestly and
never papered over.

---

## 1. What `alu_op.cpp` is — the shared scalar ALU evaluator

`alu_op.cpp` is **not** one engine's kernel. It is a single translation unit,
`/opt/workspace/NeuronUcode/src/decode/alu_op.cpp`, that implements the
`NEURON_ISA_TPB_ALU_OP` semantics on the **SEQ (sequencer) scalar register file `R[]`**, and it is
linked into *every* NeuronCore engine firmware of *every* generation.

**Universal linkage (byte-grounded).** The bare assert string `alu_op.cpp:262 0` occurs in **59**
distinct firmware images inside the host loader. `[HIGH/OBSERVED]`

```text
$ strings libnrtucode_internal.so | rg -c 'alu_op\.cpp:262'
59
```

The container is the FW-49/50 anchor `libnrtucode_internal.so`
(`sha256 b7c67e89…a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, 10,276,288 bytes), which
embeds every per-gen/per-engine firmware image as `*_get.data` blobs; `.rodata` file-offset ==
in-image offset (the FW-00 carve convention). The 59 images span CAYMAN / MARIANA / MARIANA_PLUS
`{ACT,DVE,PE,POOL,SP} × {DEBUG,PERF,TEST}`, MAVERICK `{DVE,PE,POOL,SP} × {PERF,TEST,+DVE DEBUG}`, and
SUNDA `{ACT,DVE,PE,POOL,SP} RELEASE`. (MAVERICK ACT and some DEBUG flavours are simply not packaged in
this container — a packaging gap, **not** a code absence.) Every NeuronCore engine sequencer therefore
carries the **same** ALU evaluator. `[HIGH/OBSERVED — 59-image string map]`

**The operand model.** The DEBUG self-trace strings, all byte-read in the carved DVE DRAM, prove the
two operand shapes and the per-element evaluator. `[HIGH/OBSERVED]`

```text
$ strings libnrtucode_internal.so | rg '^S: (OP|AluOp|64-bit|BRCMP)'
S: OP=%x R[%d] = OP(R[%d], imm)                                        <- tensor-SCALAR arm (R, imm)
S: OP=%x R[%d] = OP(R[%d], R[%d])                                      <- tensor-TENSOR arm (R, R)
S: OP(%x, %x) = %x                                                     <- the per-op EVALUATOR trace
S: 64-bit: src0=%016llx src1=%016llx dst=%016llx src_dtype=%d dst_dtype=%d   <- 64-bit ARITH (2-dtype)
S: 64-bit: src0=%016llx src1=%016llx dst=%016llx dtype=%d                    <- 64-bit BITVEC (1-dtype)
S: AluOp                                                               <- the worker self-name
S: BRCMP: CMPOP=%x OP(%x, %x)=%x                                       <- the BranchCompare reuse
```

So `alu_op.cpp` evaluates element-wise on the scalar `R[]` file (`R[d] = OP(R[s0], R[s1])`), with a
**tensor-tensor** form (two register operands) and a **tensor-scalar** form (register `op` immediate)
— exactly the two shapes [Tensor-Tensor](tensor-tensor.md) (`S3S3D3_TT`, two tensors) and
[Tensor-Scalar](tensor-scalar.md) (`S3D3_TS`, tensor + scalar) decode at the wire level, both
delegating the per-element `OP` here. The two trace strings sit **adjacent** in DVE DRAM (the
`…OP(R[d], imm)` scalar trace immediately followed by the `…OP(R[d], R[d])` tensor trace) — the
byte-level proof the two kernels share one datapath. `[HIGH/OBSERVED]`

**The branch reuse.** `BranchCompare` (`branch.cpp:75 0` — byte-present) calls the *same* compare
evaluator to compute its taken/not-taken predicate (the `S: BRCMP: CMPOP=%x OP(%x, %x)=%x` trace). The
compare ops (`IS_EQ`/`IS_GT`/`IS_LT`/…, `0x12..0x18`) live in `alu_op.cpp` and are shared by
conditional branches. `[HIGH/OBSERVED]`

**The on-device evaluator.** The CAYMAN `NX_DVE` DEBUG IRAM scalar evaluator `@~0x11330` loads
`S: OP(%x,%x)=%x` (`const16 a10,0x2afc @0x113a6`), reads four packed 16-bit register lanes
(`l16ui a2,28/30/32/34 @0x1130d..0x11316`), then an `(op&mask)`-scaled jump selects a **native Xtensa
scalar primitive at the operand width**:

```asm
0x11381  mul16s a7,a4,a12     ; signed 16-bit MULT  (dtype INT16)
0x113b0  mull   a3,a5,a7      ; 32-bit MULT         (dtype INT32)
; + add/sub/and/or/xor/slli/srai elsewhere in the body
0x11392  movi a5,3 ; and a4,a5,a4 ; xor a4,a5,a4    ; (op & mask)
0x1139a  addx4 a5,a4,a2 ; addx2 a8,a4,a2            ; scale-by-op jump
```

`[HIGH for the primitives + dispatch shape / OBSERVED ; MED for the exact per-op arm bodies through
the FLIX desync]`

> **NOTE — `R[]` is read as packed 16-bit lanes.** The evaluator reads operand register slots at byte
> offsets 28/30/32/34 with `l16ui` (four 16-bit fields), then a per-op + per-dtype switch picks the
> native primitive at the *operand width*: `mul16s` for 16-bit, `mull` for 32-bit, the half-synthesis
> handlers for 64-bit (§4), the soft-float bodies for fp. This is the concrete "op + dtype selects the
> instruction" mechanism a reimplementation must reproduce.

---

## 2. The full `NEURON_ISA_TPB_ALU_OP` enum — per-gen, compile-verified

Storage: `NEURON_ISA_PACKED` (**1 byte**). The base band is contiguous `0x00..0x1D` (and `0x20..0x24`
on later gens, with a **`0x1E/0x1F` gap**); the integer-engine band is `0xC4..0xE1`, where
`bit[7:6] == 0x3` (`0xC4 = 0b11000100`) selects the integer compute engine.

The enum lives at `aws_neuron_isa_tpb_common.h` line **sunda:917 / cayman:939 / mariana:1033 /
maverick:1188**. The growth is **strictly additive — no removals, ever; ordinals are reserved.**
`[HIGH/OBSERVED — gcc-read, all four gens]`

| code | `NEURON_ISA_TPB_ALU_OP_*` | S | C | M | V | class | what it computes |
|---|---|:-:|:-:|:-:|:-:|---|---|
| **base band** | | | | | | | |
| `0x00` | `BYPASS` | ● | ● | ● | ● | bitvec+arith | pass-through (copy `src0`); legal in **both** classes |
| `0x01` | `BITWISE_NOT` | ● | ● | ● | ● | bitvec | `~src0` (one's complement) |
| `0x02` | `ARITH_SHIFT_LEFT` | ● | ● | ● | ● | bitvec | `src0 << src1` (== logical shl) |
| `0x03` | `ARITH_SHIFT_RIGHT` | ● | ● | ● | ● | bitvec | `src0 >> src1` (sign-replicating, `sra`) |
| `0x04` | `ADD` | ● | ● | ● | ● | arith | `src0 + src1` |
| `0x05` | `SUBTRACT` | ● | ● | ● | ● | arith | `src0 - src1` |
| `0x06` | `MULT` | ● | ● | ● | ● | arith | `src0 * src1` |
| `0x07` | `DIVIDE` | ● | ● | ● | ● | arith\* | `src0 / src1` (fp recip/Newton; **not** general-arith) |
| `0x08` | `MAX` | ● | ● | ● | ● | arith | `max(src0, src1)` |
| `0x09` | `MIN` | ● | ● | ● | ● | arith | `min(src0, src1)` |
| `0x0A` | `BITWISE_AND` | ● | ● | ● | ● | bitvec | `src0 & src1` |
| `0x0B` | `BITWISE_OR` | ● | ● | ● | ● | bitvec | `src0 \| src1` |
| `0x0C` | `BITWISE_XOR` | ● | ● | ● | ● | bitvec | `src0 ^ src1` |
| `0x0D` | `LOGICAL_AND` | ● | ● | ● | ● | arith | `(src0!=0)&&(src1!=0)` → 0/1 |
| `0x0E` | `LOGICAL_OR` | ● | ● | ● | ● | arith | `(src0!=0)\|\|(src1!=0)` → 0/1 |
| `0x0F` | `LOGICAL_XOR` | ● | ● | ● | ● | arith | `(src0!=0)^(src1!=0)` → 0/1 |
| `0x10` | `LOGICAL_SHIFT_LEFT` | ● | ● | ● | ● | bitvec | `src0 << src1` (logical shl) |
| `0x11` | `LOGICAL_SHIFT_RIGHT` | ● | ● | ● | ● | bitvec | `src0 >> src1` (zero-fill, `lsr`) |
| `0x12` | `IS_EQ` | ● | ● | ● | ● | arith | `(src0 == src1)` → 0/1 |
| `0x13` | `IS_GT` | ● | ● | ● | ● | arith | `(src0 > src1)` → 0/1 |
| `0x14` | `IS_GE` | ● | ● | ● | ● | arith | `(src0 >= src1)` → 0/1 |
| `0x15` | `IS_LE` | ● | ● | ● | ● | arith | `(src0 <= src1)` → 0/1 |
| `0x16` | `IS_LT` | ● | ● | ● | ● | arith | `(src0 < src1)` → 0/1 |
| `0x17` | `ABSOLUTE_DIFF` | ● | ● | ● | ● | arith | `\|src0 - src1\|` (header comment) |
| `0x18` | `IS_NE` | ● | ● | ● | ● | arith | `(src0 != src1)` → 0/1 |
| `0x19` | `ABSOLUTE_VALUE` | ● | ● | ● | ● | arith | `\|src0\|` |
| `0x1A` | `POW` | ● | ● | ● | ● | arith\* | `src0^src1` (Pool only; poly via `.rodata` coeffs) |
| `0x1B` | `MOD` | ● | ● | ● | ● | arith\* | `src0 % src1` (**not** general-arith; is a valid int-aluop) |
| `0x1C` | `CRC32` | ● | ● | ● | ● | bitvec\* | CRC32 (Pool only; **not** general-bitvec) |
| `0x1D` | `RSQRT` | ● | ● | ● | ● | arith\* | `1/sqrt(src0)` (Pool only; Newton / `.rodata`) |
| `0x20` | `ABS_MAX` | – | – | ● | ● | arith | `max(\|src0\|, \|src1\|)` |
| `0x21` | `ABS_MIN` | – | – | ● | ● | arith | `min(\|src0\|, \|src1\|)` |
| `0x22` | `RE_LU` | – | – | ● | ● | arith | `max(src0, 0)` (ReLU) |
| `0x23` | `SQUARE` | – | – | ● | ● | arith | `src0 * src0` |
| `0x24` | `SYMMETRIC_CLAMP` | – | – | – | ● | arith | `sign(src0)·max(\|src0\|, \|src1\|)` (header comment) |
| **integer-engine band** (`bit[7:6]==0x3`) | | | | | | | |
| `0xC4` | `ADD_INT` | ● | ● | ● | ● | int (s\|u) | int add (shared signed/unsigned) |
| `0xC5` | `MULT_INT` | ● | ● | ● | ● | int (s) | signed int multiply |
| `0xC6` | `SUBTRACT_INT` | ● | ● | ● | ● | int (s\|u) | int subtract (shared) |
| `0xC7` | `DIVIDE_INT` | – | ● | ● | ● | int (s) | signed int divide |
| `0xC8` | `MOD_INT` | – | ● | ● | ● | int (s) | signed C-style modulo in 32b (`np.fmod`) |
| `0xC9` | `IS_EQUAL_INT` | – | ● | ● | ● | int (s\|u) | int `==` (shared) |
| `0xCA` | `IS_NE_INT` | – | ● | ● | ● | int (s\|u) | int `!=` (shared) |
| `0xCB` | `ABS_MAX_INT` | – | ● | ● | ● | int (s) | signed abs-max |
| `0xCC` | `ABS_MIN_INT` | – | ● | ● | ● | int (s) | signed abs-min |
| `0xCD` | `ABS_DIFF_INT` | – | ● | ● | ● | int (s) | signed `\|a-b\|` |
| `0xCE` | `ABS_VALUE_INT` | – | ● | ● | ● | int (s) | signed `\|a\|` (**single-source**, see §3) |
| `0xCF` | `MAX_INT` | – | ● | ● | ● | int (s) | signed int max |
| `0xD0` | `MIN_INT` | – | ● | ● | ● | int (s) | signed int min |
| `0xD1` | `IS_GT_INT` | – | ● | ● | ● | int (s) | signed int `>` |
| `0xD2` | `IS_GE_INT` | – | ● | ● | ● | int (s) | signed int `>=` |
| `0xD3` | `IS_LE_INT` | – | ● | ● | ● | int (s) | signed int `<=` |
| `0xD4` | `IS_LT_INT` | – | ● | ● | ● | int (s) | signed int `<` |
| `0xD5` | `MAX_UINT` | – | ● | ● | ● | int (u) | unsigned max |
| `0xD6` | `MIN_UINT` | – | ● | ● | ● | int (u) | unsigned min |
| `0xD7` | `IS_GT_UINT` | – | ● | ● | ● | int (u) | unsigned `>` |
| `0xD8` | `IS_GE_UINT` | – | ● | ● | ● | int (u) | unsigned `>=` |
| `0xD9` | `IS_LE_UINT` | – | ● | ● | ● | int (u) | unsigned `<=` |
| `0xDA` | `IS_LT_UINT` | – | ● | ● | ● | int (u) | unsigned `<` |
| `0xDB` | `MULT_UINT` | – | ● | ● | ● | int (u) | unsigned multiply |
| `0xDC` | `DIVIDE_UINT` | – | ● | ● | ● | int (u) | unsigned divide |
| `0xDD` | `MOD_UINT` | – | ● | ● | ● | int (u) | unsigned modulo |
| `0xDE` | `AMAX_INT` | – | ● | ● | ● | int (s) | argmax (`out_dtype` = u32) |
| `0xDF` | `AMIN_INT` | – | ● | ● | ● | int (s) | argmin (`out_dtype` = u32) |
| `0xE0` | `AMAX_UINT` | – | ● | ● | ● | int (u) | unsigned argmax (`out_dtype` = u32) |
| `0xE1` | `AMIN_UINT` | – | ● | ● | ● | int (u) | unsigned argmin (`out_dtype` = u32) |

(`S`=SUNDA/NC-v2, `C`=CAYMAN/NC-v3, `M`=MARIANA/NC-v4, `V`=MAVERICK/NC-v5.)

**The per-gen counts, ground-truthed by literally counting the enumerators:** `[HIGH/OBSERVED]`

```text
$ rg 'NEURON_ISA_TPB_ALU_OP_.* = 0x' neuron_<gen>_arch_isa/.../aws_neuron_isa_tpb_common.h | wc -l
sunda 33   cayman 60   mariana 64   maverick 65
```

| gen | count | base | int band | what enters here |
|---|---|---|---|---|
| **SUNDA** (v2) | **33** | `0x00..0x1D` (30) | `0xC4/C5/C6` (3) | the 30-op base + the 3 seminal int ops (`ADD_INT`/`MULT_INT`/`SUBTRACT_INT`) |
| **CAYMAN** (v3) | **60** | same 30 | `0xC4..0xE1` (30) | **+27 int ops** — the full integer-engine band (div/mod/int-compares/abs/min/max/argmax/argmin) |
| **MARIANA** (v4) | **64** | + `0x20..0x23` | `0xC4..0xE1` | **+4 base** — `ABS_MAX 0x20`, `ABS_MIN 0x21`, `RE_LU 0x22`, `SQUARE 0x23` (the fused activation primitives) |
| **MARIANA_PLUS** | **64** (== v4) | same | same | (same ISA as MARIANA) |
| **MAVERICK** (v5) | **65** | + `0x20..0x24` | `0xC4..0xE1` | **+1 base** — `SYMMETRIC_CLAMP 0x24` (the quantization-symmetric clamp) |

```c
// aws_neuron_isa_tpb_common.h (maverick:1188) — verbatim, NEURON_ISA_PACKED (1 byte)
typedef enum NEURON_ISA_TPB_ALU_OP {
    NEURON_ISA_TPB_ALU_OP_BYPASS          = 0x00,  // bitvec
    /* ... 0x01..0x1D ... */
    NEURON_ISA_TPB_ALU_OP_RSQRT           = 0x1D,  // Pool only
    NEURON_ISA_TPB_ALU_OP_ABS_MAX         = 0x20,  // MARIANA+
    NEURON_ISA_TPB_ALU_OP_ABS_MIN         = 0x21,  // MARIANA+
    NEURON_ISA_TPB_ALU_OP_RE_LU           = 0x22,  // MARIANA+
    NEURON_ISA_TPB_ALU_OP_SQUARE          = 0x23,  // MARIANA+
    NEURON_ISA_TPB_ALU_OP_SYMMETRIC_CLAMP = 0x24,  // MAVERICK+  (sign(src0) * max(abs(src0), abs(src1)))
    NEURON_ISA_TPB_ALU_OP_ADD_INT         = 0xC4,  // bit 7:6 == 0x3 -> integer engine; shared s/u
    /* ... 0xC5..0xE0 ... */
    NEURON_ISA_TPB_ALU_OP_AMIN_UINT       = 0xE1,  // unsigned argmin, out_dtype u32
} NEURON_ISA_PACKED NEURON_ISA_TPB_ALU_OP;
```

> **CONSISTENCY — the "60 ops" of [tensor-tensor.md](tensor-tensor.md) is the CAYMAN count.** The
> committed Tensor-Tensor ALU-OP table (`common.h:939`, 30 base + 30 int) is the **CAYMAN** generation
> snapshot, and it is byte-identical to this page's CAYMAN column. The **64th** op (relative to CAYMAN's
> 60) is the MARIANA set `{ABS_MAX, ABS_MIN, RE_LU, SQUARE}` at `0x20..0x23`; the **65th** is MAVERICK's
> `SYMMETRIC_CLAMP` at `0x24`. SUNDA's 33 is the floor (base 30 + 3 int). No op is ever removed.
> `[HIGH/OBSERVED]`

> **QUIRK — the integer band is a *disjoint* high-code range, not a flag on the base ops.** `ADD_INT`
> (`0xC4`) is a **distinct enum value**, *not* `ADD` (`0x04`) with an "int" modifier. The base
> `ADD`/`MULT`/… run the generic fp-capable, type-converting datapath; the `*_INT` codes route to the
> dedicated integer engine with strict signedness gating (§3). A reimplementation **must** keep both
> as separate opcodes.

> **NOTE — `SparsityCompress` reuses ALU-OP ordinals with a deliberate Min↔Max swap.** The MAVERICK
> header (immediately after the enum) carries the `CompressOp` aliasing: `CompressOp::Min = AluOp::Max`,
> `CompressOp::Max = AluOp::Min`, `CompressOp::AbsMin = AluOp::AbsMax`, `CompressOp::AbsMax =
> AluOp::AbsMin`. Compress encodes its reduction by **inverting** the comparator op — a downstream
> consumer of this enum must not assume the ALU-OP byte means the same thing in every opcode context.
> `[HIGH/OBSERVED — header comment, maverick:1256]`

---

## 3. The arith-vs-bitvec split + the op-class predicates

The arch-isa header carries the op-class predicates as Rust-pseudocode comments (the ISA-generator
source). The consumer validity functions (`S3S3D3_TT` / `S3D3_TS` / `S4D4_PL`) call them to gate which
`ALU_OP` is legal for which opcode-class — **before** the `alu_op.cpp` evaluator runs. The bodies were
read field-exact this session. `[HIGH/OBSERVED — predicate bodies, cayman; mariana adds the 4
activation ops]`

```c
// is_bitvec_op(op)  [common.h:1653] — the bitwise/shift set (10 ops)
is_bitvec_op = { Bypass, BitwiseNot, ArithShiftLeft, ArithShiftRight,
                 LogicalShiftLeft, LogicalShiftRight, BitwiseAnd, BitwiseOr,
                 BitwiseXor, Crc32 }
// is_general_bitvec_op(op) = is_bitvec_op(op) && op != Crc32      [common.h:1666]  (9 ops)
//   => TensorTensorBitvec(0x51) / TensorScalarBitvec(0x53) accept the 9-op set; CRC32 is Pool-only.

// is_arith_op(op)  [cayman common.h:1624] — 26 ops; MARIANA(common.h:1859) = 30 (adds the 4 below)
is_arith_op = { Bypass, Add, Subtract, Mult, Divide, Max, Min,
                LogicalAnd, LogicalOr, LogicalXor,
                IsEQ, IsGT, IsGE, IsLE, IsLT, IsNE, AbsoluteDiff, AbsoluteValue,
                Pow, Mod, AddInt, MultInt, SubtractInt, DivideInt, ModInt, Rsqrt
                /* MARIANA+ adds: */  /* AbsMax, AbsMin, ReLU, Square */ }

// is_general_arith_op(op)  [common.h:1671]
is_general_arith_op(op) = is_arith_op(op)
                       && op != Divide && op != Pow && op != Mod && op != Rsqrt
                       && !is_valid_int_aluop(op)
//   => TensorTensorArith(0x41) accepts:  is_general_arith_op(op) || op == Pow || is_valid_int_aluop(op)
//      (the general-arith set, PLUS Pow, PLUS the int band — NOT raw Divide/Rsqrt; MOD re-enters via
//       is_valid_int_aluop, Divide/Rsqrt do not.)

// is_valid_int_aluop(op)  [common.h:1419] — the int band 0xC4..0xDD MINUS the 4 argmax/argmin,
//                                            PLUS Mod(0x1B)  (26 ops)
is_valid_int_aluop = { AddInt, MultInt, SubtractInt, Mod, DivideInt, ModInt,
                       DivideUint, ModUint, MultUint, IsEqualInt, IsNeInt, AbsDiffInt,
                       AbsMaxInt, AbsMinInt, MaxInt, MinInt, IsGtInt, IsGeInt, IsLeInt, IsLtInt,
                       MaxUint, MinUint, IsGtUint, IsGeUint, IsLeUint, IsLtUint }
//   NOTE: AMAX_INT/AMIN_INT/AMAX_UINT/AMIN_UINT (0xDE..0xE1) are NOT in is_valid_int_aluop.
//   NOTE: ABS_VALUE_INT (0xCE) is single-source and is NOT in is_valid_int_aluop either.
```

### 3.1 `is_general_arith_op` — pinned to **17** (CAYMAN) / **21** (MARIANA+)

The membership has wavered (16 vs 17) across drafts; it is settled here by **applying the predicate to
the byte-exact `is_arith_op` set**, not by recollection. `[HIGH/OBSERVED — computed from the verbatim
header bodies, common.h:1624 + 1671 + 1419]`

```text
start:  is_arith_op (CAYMAN) = 26 ops
remove: Divide, Pow, Mod, Rsqrt                              -> 22
remove: is_valid_int_aluop members in that 22
        = { AddInt, MultInt, SubtractInt, DivideInt, ModInt } (5 ops)   -> 17
=> is_general_arith_op (CAYMAN) = 17 ops:
   { Bypass, Add, Subtract, Mult, Max, Min, LogicalAnd, LogicalOr, LogicalXor,
     IsEQ, IsGT, IsGE, IsLE, IsLT, IsNE, AbsoluteDiff, AbsoluteValue }
```

On **MARIANA/MAVERICK**, `is_arith_op` gains `AbsMax/AbsMin/ReLU/Square` (none of them special-excluded,
none in `is_valid_int_aluop`), so `is_general_arith_op` becomes **21 ops** (the CAYMAN 17 + those 4).

> **CORRECTION — `is_general_arith_op` is 17, not 16.** Some prior count dropped `Bypass` or one
> compare and reported 16. The verbatim `is_arith_op` body enumerates 26 (CAYMAN), and the four
> `is_general_arith_op` exclusions plus the five int-aluop overlaps leave exactly **17**. This page
> pins **17 (SUNDA/CAYMAN) / 21 (MARIANA+)**, and is consistent with the
> [Tensor-Scalar](tensor-scalar.md) §1 note that the MARIANA general-arith set *includes*
> `AbsMax/AbsMin/ReLU/Square`. `[HIGH/OBSERVED]`

### 3.2 The signedness partition (int band)

```c
// common.h:2477 / 2497 — the int-band src/dst signedness gate
is_signed_alu_op(op)   = { AddInt, MultInt, SubtractInt, DivideInt, ModInt, Mod,
                           IsEqualInt, IsNeInt, AbsDiffInt, AbsMaxInt, AbsMinInt,
                           MaxInt, MinInt, IsGtInt, IsGeInt, IsLeInt, IsLtInt }
is_unsigned_alu_op(op) = { AddInt, MultInt, SubtractInt, DivideInt, ModInt, Mod,
                           DivideUint, ModUint, MultUint, IsEqualInt, IsNeInt,
                           MaxUint, MinUint, IsGtUint, IsGeUint, IsLeUint, IsLtUint }
```

The **shared** int ops (`AddInt`, `MultInt`, `SubtractInt`, `DivideInt`, `ModInt`, `Mod`, `IsEqualInt`,
`IsNeInt`) appear in **both** lists — legal with either signedness provided **all three** operands
(`src0`/`src1`/`out`) agree. The sign-specific ops (`MaxInt`/`MaxUint`, `AbsMaxInt`, the `_INT`/`_UINT`
compares) appear in only one. `[HIGH/OBSERVED — both bodies]`

> **QUIRK — `MOD`(`0x1B`) is the cross-over op.** It lives in the **base** band but is a member of
> `is_valid_int_aluop` *and* of **both** `is_signed_alu_op` and `is_unsigned_alu_op`. It is the one
> base-band op that participates in the integer-engine signedness machinery — which is why a 64-bit
> tensor-tensor `MOD` is legal (see [tensor-tensor-64bit.md](tensor-tensor-64bit.md) §4.3) while a
> base-band 64-bit `Divide`/`Rsqrt` is not.

### 3.3 The MAVERICK DVE int-band relaxation

MAVERICK adds a tighter int-band predicate so a *subset* of the integer ops can run on the **DVE**
engine (NC-v5 only): `[HIGH/OBSERVED — maverick common.h:1948]`

```c
// is_valid_int_aluop_dve(op)  [maverick common.h:1948]  (15 ops)
is_valid_int_aluop_dve = { Bypass, AddInt, MultInt, SubtractInt, MaxInt, MinInt,
                           AbsMaxInt, AbsMinInt, AbsDiffInt, IsEqualInt,
                           IsGtInt, IsGeInt, IsLeInt, IsLtInt, IsNeInt }
```

This is the integer-engine subset the [Tensor-Scalar](tensor-scalar.md) DVE op-relaxation cites — it
omits divide/mod/abs-value/argmax-argmin and the entire `_UINT` half, keeping the cheap
add/sub/mult/min/max/abs/compare ops the DVE lane datapath can host. `[HIGH/OBSERVED]`

### 3.4 The shift-composition rule

```c
// alu_shift_check(op0, op1, dtype) = alu_shift_left_right_chk(op0,op1,dtype)
//                                 && alu_arith_shift_right_chk(op0,op1,dtype)   [common.h:1680]
```

A shift-left `op0` paired with a shift-right `op1` (or any arith-shift-right) requires
`type_size_check(dtype, 4)` — i.e. the two-op shift composition needs a **4-byte** dtype. `[HIGH/OBSERVED]`

---

## 4. The `alu_op.cpp` dispatch structure — the four+ switches

The five assert sites are the switch-defaults of (at least) **four distinct** dispatch functions,
mapped on-device in the CAYMAN `NX_DVE` DEBUG IRAM (VA == in-image offset; the `const16` string-loaders
are byte-clean). The assert strings themselves are byte-confirmed in the container at file offsets
`0xf120/0xf157/0xf1a4/0xf1f1` (and 58 more image copies): `[HIGH/OBSERVED]`

```text
/opt/workspace/NeuronUcode/src/decode/alu_op.cpp:262 0
/opt/workspace/NeuronUcode/src/decode/alu_op.cpp:196 0 && "not supported op"
/opt/workspace/NeuronUcode/src/decode/alu_op.cpp:141 0 && "not supported op"
/opt/workspace/NeuronUcode/src/decode/alu_op.cpp:220 0 && "not supported op"
/opt/workspace/NeuronUcode/src/decode/alu_op.cpp:231 0 && "not supported dtype"
```

| `alu_op.cpp` line | on-device loader | role `[structure HIGH / arm-body MED]` |
|---|---|---|
| `:141` "not sup op" | `const16 a10,0x2c17 @0xd571` | **default of the 64-bit ALU-OP switch** (the int-64-bit op dispatch, reached after the dtype gate) |
| `:231` "not sup dtype" | `const16 a10,0x2c64 @0xd633` | **default of the 64-bit DTYPE switch** — accepts only UINT64/INT64 (below) |
| `:196` "not sup op" | `const16 a10,0x2bca @0xd85b` | default of a **32-bit OP switch** (the imm/scalar form, `OP(R, imm)`) |
| `:220` "not sup op" | `const16 a10,0x2cb4 @0xda24/0xdb6c` | default of a 32-bit OP switch whose dtype sub-dispatch tests INT16/UINT16 (`beqi a2,4`/`a2,5` @`0xda8f`/`0xda95`) — the **16-bit-width arm** |
| `:262` `0` (bare) | `const16 a10,0x2b93 @0xd228` | a bare unreachable-guard assert (the main `R[]`-evaluator region; entry `a1,96 @0xd650`) |

### 4.1 The 64-bit dtype switch — gated to UINT64/INT64 only

The decisive new datapath fact, byte-observed: `[HIGH/OBSERVED — the bnei tests + the default]`

```asm
@0xd5bb  bnei a2, 1,  0xd5f4   ; if dtype != UINT64(0x1) -> next
;        ... (UINT64 64-bit op handlers, call0 0x1eec4 etc.) ...
@0xd5f7  bnei a2, 12, 0xd630   ; if dtype != INT64(0xC)   -> default
;        ... (INT64 64-bit op handlers, call0 0x1ef00 etc.) ...
@0xd630/0xd633 -> alu_op.cpp:231 "not supported dtype"     ; default
```

So the 64-bit ALU path admits **only** `UINT64(0x1)` and `INT64(0xC)`. This reconciles
[tensor-tensor-64bit.md](tensor-tensor-64bit.md)'s `is_valid_64b_int_dtype(d) == (d==INT64 || d==UINT64)`
gate (`common.h:2451`) and the two `S: 64-bit:` traces (the 2-dtype **arith** form, the 1-dtype
**bitvec** form). The `ncore2gp` has **no native 64-bit ALU**; 64-bit is synthesised from 32-bit halves
in these handlers. `[HIGH/OBSERVED for the bnei dtype tests + default ; LOW for the per-op 32×2
synthesis bodies — not byte-walked]`

> **CONSISTENCY — 64-bit `MUL`/`DIV`/`MOD` *are* admitted; only the 4 argmax/argmin are excluded.**
> [tensor-tensor-64bit.md](tensor-tensor-64bit.md) §4.3 establishes the 64-bit ARITH arm fires on the
> `is_valid_int_aluop` set (26 ops) — which **includes** `MULT_INT`/`MULT_UINT`,
> `DIVIDE_INT`/`DIVIDE_UINT`, `MOD_INT`/`MOD_UINT`, and the base-band `MOD`. The *only* int-band ops
> excluded from `is_valid_int_aluop` (and hence from the 64-bit arm) are the four
> `AMAX_INT`/`AMIN_INT`/`AMAX_UINT`/`AMIN_UINT`. There is **no fp64** anywhere. This page does not
> contradict that — the §2 enum + §3 `is_valid_int_aluop` + this §4.1 gate are exactly the same set
> viewed from the evaluator side. `[HIGH/OBSERVED]`

### 4.2 The 32-bit per-op scalar evaluator + the discrete jump table

An independent re-decode of the MARIANA `NX_DVE` DEBUG image upgrades the scalar-path arm bodies from
MED to HIGH. The per-element evaluator entry `@0xd618` (`entry a1,96`) is a **discrete DRAM jump
table**, byte-observed: `[HIGH/OBSERVED — the addx4/l32i/jx + the table bytes]`

```asm
l8ui    a2, a1, 96       ; op selector byte
addi    a2, a2, -1
const16 a3, 0x2aa0       ; DRAM jump-table base
addx4   a2, a2, a3       ; &table[op-1]
l32i    a2, a2, 0        ; load IRAM handler VA
jx      a2               ; INDEXED JUMP -> per-op leaf
```

The table is a 32-entry array of IRAM targets at DRAM `0x2a98/0x2aa0` (entries
`0x0000d542..0x0000da97`). This is the **same** mechanism as CAYMAN's `(op&mask)`-scaled `addx4/addx2`
(`@0x1139a`): `const16` loads the table base, `addx4` scales the op index, `l32i`+`jx` jumps —
complementary, not a contradiction with [tensor-tensor.md](tensor-tensor.md)'s "compiled switch, no
discrete `.rodata` op→address table" (which held for the *vectorized FLIX* POOL compute; the
DVE/scalar `alu_op.cpp` path **does** carry discrete DRAM jump tables). `[HIGH/OBSERVED]`

The leaf handlers are **one native Xtensa instruction each** — ground truth for "what each op computes"
on the scalar path. Each computes into `[a1+12]` and tail-jumps to `j 0xdb95` (`l32i.n a2,[a1+12];
retw.n`). Byte-observed: `[HIGH/OBSERVED — every cited handler disassembled byte-clean]`

```asm
0xd8e7 sll              ; ARITH/LOGICAL_SHIFT_LEFT  (src0 << src1)
0xd8f6 srl              ; LOGICAL_SHIFT_RIGHT        (zero-fill)
0xd906 sra              ; ARITH_SHIFT_RIGHT          (sign-replicating)
0xd916 add.n            ; ADD
0xd921 sub              ; SUBTRACT
0xd92d mull             ; MULT  (low 32 of product)
0xd939 div (bnez 0-chk; call 0x13c20)  ; DIVIDE
0xd951 quou             ; DIVIDE_UINT (unsigned quotient)
0xd95d quos             ; DIVIDE_INT  (signed quotient)
0xd96c bltu+sel         ; MIN (unsigned-branch select)
0xd9bc bltu+sel / 0xd9d6 blt+sel  ; MAX (unsigned / signed branch)
0xd9f3 and / 0xd9ff or / xor      ; BITWISE_AND / _OR / _XOR
0xda30 saltu            ; bool(x) normalize (LOGICAL_*)
0xda4c saltu;saltu;xor  ; LOGICAL_XOR  (bool(a)^bool(b))
0xda60 sub; saltu a2,1  ; IS_EQ  ((a-b)==0)
0xda71 salt/saltu (sign-sel)      ; IS_GT  (b<a)
0xda97 salt/saltu; xor ,1         ; IS_GE  (!(a<b))
0xdad8 salt/saltu; xor ,1         ; IS_LE / IS_NE family
```

> **GOTCHA — signedness on the scalar path is a runtime branch, not a separate opcode.** One leaf
> serves **both** the signed and the unsigned variant of a compare/min/max: a `bbci a2,0,…` test on
> the dtype-signedness flag at stack `[a1+20]` selects `saltu/bltu` (UNSIGNED) vs `salt/blt` (SIGNED).
> This is the byte-exact realisation of the §5 "s\|u" cells. The **vector** path instead uses
> *distinct* signed/unsigned IVP opcodes (§6) — the semantics agree, the encodings differ. A
> reimplementation choosing the scalar style must read the dtype-signedness flag at compute time.
> `[HIGH/OBSERVED]`

> **FLIX-DESYNC FLAG.** The `alu_op.cpp` function bodies are hand-scheduled FLIX VLIW with interleaved
> literal pools that desync under stock `objdump` (~18% `.byte` in the `0xcae0..0xe200` region, the
> documented FW-00 limitation). The function **entries**, the `const16` **string-loaders**, the
> **bnei dtype tests**, the **jump-table bytes**, and the **leaf primitives** above are byte-OBSERVED;
> the mid-bundle per-op arm *selection* in the desynced 32-bit width switch is reported structurally
> (MED), never fabricated. Mis-decoded `.byte` / spurious `call0`-to-negative-literal bundles are not
> reported as real instructions.

---

## 5. The per-op × per-dtype support matrix

This is the matrix the dtype-dispatch synthesis depends on. It is the **composition** of (a) the
op-class predicates of §3 (which op is legal for which opcode-class) and (b) the per-op dtype legality
(the `is_valid_dtype` gates) and the on-device dtype-width arms (§1, §4). DTYPE ordinals are pinned in
[dtype-model.md](dtype-model.md): `INVALID 0x0, UINT64 0x1, INT8 0x2, UINT8 0x3, INT16 0x4, UINT16 0x5,
BF16 0x6, FP16 0x7, INT32 0x8, UINT32 0x9, FP32 0xA, FP32R 0xB, INT64 0xC, FP8_E3 0xD, FP8_E4 0xE,
FP8_E5 0xF` (+ MARIANA `FP4_E2 0x10`; + MAVERICK `FP8_E2 0x11`/`INT4 0x12`/`SFP8_E8..E5 0x13..0x16`).

### 5.1 The dtype gates (header-verbatim)

```c
// is_valid_dtype(dtype, allow_fp32r)   [common.h:1312]
is_valid_dtype = dtype_invalid_check(dtype)                          // dtype != INVALID
              && dtype_fp32r_illegal_check(dtype, allow_fp32r)       // FP32R only if AllowFP32R
              && dtype_uint64_illegal_check(dtype, /*AllowU64=*/False)  // U64 illegal on 32-bit path
              && dtype_int64_illegal_check(dtype, /*AllowI64=*/False)   // I64 illegal on 32-bit path
              && is_valid_enum(Dtype, dtype)
//  => the 32-bit ALU path accepts { INT8/16/32, UINT8/16/32, FP16, BF16, FP32, FP8_E3/E4/E5,
//     (FP32R only if AllowFP32R) }; it REJECTS INVALID and routes INT64/UINT64 to the 64-bit path.

// is_valid_dtype_64(dtype, allow_fp32r, allow_u64, allow_i64)   [common.h:1324]
//  same body but the U64/I64 illegal-checks are PARAMETERISED — the 64-bit path passes
//  AllowU64/AllowI64 = True to admit UINT64/INT64 (the §4.1 bnei 1/12 arms).
```

### 5.2 The per-op-class dtype contract (from the consumer validity fns)

* **ARITH** ops (`TensorTensorArith 0x41` / `TensorScalarArith 0x43` / `Pool 0x45`):
  `out_dtype` = `is_valid_dtype(.., AllowFP32R=True)` (any valid + FP32R as **output**);
  `src` dtype = `is_valid_dtype(.., AllowFP32R=False)` (any valid **minus** FP32R as source).
  Int-band op (`is_valid_int_aluop`): `is_signed_alu_op` requires **all** src/dst signed-int;
  `is_unsigned_alu_op` requires **all** unsigned-int. 64-bit (`INT64`/`UINT64`) only via the int band +
  `is_valid_dtype_64` (the §4.1 path).
* **BITVEC** ops (`TensorTensorBitvec 0x51` / `TensorScalarBitvec 0x53`): `out_dtype` must be a valid
  **INT** dtype (bitvec is integer-only); the Tensor-Scalar bitvec form additionally requires
  `in == out ∈ {INT8/16/32, UINT8/16/32}` (identity in/out). `[HIGH/OBSERVED]`

### 5.3 The complete table (legal dtype set per op)

`SUPPORTED` = the op-class gate ∧ the dtype gate ∧ an on-device handler exists. Columns: `i8/u8`,
`i16/u16`, `i32/u32`, `i64/u64`, `fp16`, `bf16`, `fp32`, `fp32r`, `fp8(E3/E4/E5)`. `[HIGH for the
gate-derived legality / MED for the exact per-cell on-device handler-existence through the FLIX desync]`

| op | i8/u8 | i16/u16 | i32/u32 | i64/u64 | fp16 | bf16 | fp32 | fp32r | fp8 |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `BYPASS` 0x00 | Y | Y | Y | Y\* | Y | Y | Y | out | Y |
| `BITWISE_NOT` 0x01 | Y | Y | Y | Y\* | – | – | – | – | – |
| `ARITH_SHIFT_L/R` 0x02/03 | Y | Y | Y | Y\* | – | – | – | – | – |
| `ADD` 0x04 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `SUBTRACT` 0x05 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `MULT` 0x06 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `DIVIDE` 0x07 | – | – | – | – | Y | Y | Y | out | Y |
| `MAX` 0x08 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `MIN` 0x09 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `BITWISE_AND/OR/XOR` 0x0A–0C | Y | Y | Y | Y\* | – | – | – | – | – |
| `LOGICAL_AND/OR/XOR` 0x0D–0F | Y | Y | Y | – | Y | Y | Y | out | Y |
| `LOGICAL_SHIFT_L/R` 0x10/11 | Y | Y | Y | Y\* | – | – | – | – | – |
| `IS_EQ` 0x12 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `IS_GT/GE/LE/LT` 0x13–16 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `ABSOLUTE_DIFF` 0x17 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `IS_NE` 0x18 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `ABSOLUTE_VALUE` 0x19 | Y | Y | Y | – | Y | Y | Y | out | Y |
| `POW` 0x1A | – | – | – | – | Y | Y | Y | out | – |
| `MOD` 0x1B | Y | Y | Y | – | – | – | – | – | – |
| `CRC32` 0x1C | – | Y | Y | – | – | – | – | – | – |
| `RSQRT` 0x1D | – | – | – | – | Y | Y | Y | out | – |
| `ABS_MAX/ABS_MIN` 0x20/21 (M+) | Y | Y | Y | – | Y | Y | Y | out | Y |
| `RE_LU` 0x22 (M+) | Y | Y | Y | – | Y | Y | Y | out | Y |
| `SQUARE` 0x23 (M+) | Y | Y | Y | – | Y | Y | Y | out | Y |
| `SYMMETRIC_CLAMP` 0x24 (V+) | Y | Y | Y | – | Y | Y | Y | out | Y |
| `ADD_INT` 0xC4 | s\|u | s\|u | s\|u | s\|u | – | – | – | – | – |
| `MULT_INT` 0xC5 | s | s | s | s | – | – | – | – | – |
| `SUBTRACT_INT` 0xC6 | s\|u | s\|u | s\|u | s\|u | – | – | – | – | – |
| `DIVIDE_INT` 0xC7 | s | s | s | s | – | – | – | – | – |
| `MOD_INT` 0xC8 | s | s | s | s | – | – | – | – | – |
| `IS_EQUAL_INT` 0xC9 | s\|u | s\|u | s\|u | s\|u | – | – | – | – | – |
| `IS_NE_INT` 0xCA | s\|u | s\|u | s\|u | s\|u | – | – | – | – | – |
| `ABS_MAX/MIN_INT` 0xCB/CC | s | s | s | s | – | – | – | – | – |
| `ABS_DIFF_INT` 0xCD | s | s | s | s | – | – | – | – | – |
| `ABS_VALUE_INT` 0xCE | s | s | s | s† | – | – | – | – | – |
| `MAX_INT/MIN_INT` 0xCF/D0 | s | s | s | s | – | – | – | – | – |
| `IS_GT/GE/LE/LT_INT` 0xD1–D4 | s | s | s | s | – | – | – | – | – |
| `MAX_UINT/MIN_UINT` 0xD5/D6 | u | u | u | u | – | – | – | – | – |
| `IS_GT/GE/LE/LT_UINT` 0xD7–DA | u | u | u | u | – | – | – | – | – |
| `MULT_UINT` 0xDB | u | u | u | u | – | – | – | – | – |
| `DIVIDE_UINT` 0xDC | u | u | u | u | – | – | – | – | – |
| `MOD_UINT` 0xDD | u | u | u | u | – | – | – | – | – |
| `AMAX/AMIN_INT` 0xDE/DF | s | s | s | s | – | – | – | – | – |
| `AMAX/AMIN_UINT` 0xE0/E1 | u | u | u | u | – | – | – | – | – |

**Legend.** `Y` = supported; `s` = signed-int dtype only; `u` = unsigned-int only; `s|u` = either
signedness (op is signedness-shared, all three operands must agree); `out` (in the `fp32r` column) =
FP32R legal for **output only** (`AllowFP32R=True`), never as a **source**; `–` = gate-rejected.
`Y*` (i64/u64 in the bitvec rows) = via the 64-bit **bitvec** dispatcher (the `S: 64-bit: …dtype=%d`
1-dtype INT64/UINT64 path). `†` `ABS_VALUE_INT(0xCE)` is single-source and **not** in
`is_valid_int_aluop`, so it does **not** form a tensor-tensor 64-bit arm (see
[tensor-tensor-64bit.md](tensor-tensor-64bit.md) §4.3) — the `s` i64 cell is the scalar/single-source
realisation only. `fp8(E3/E4/E5)` is legal at the dtype gate and reached for the elementwise arith
ops, but **computed through the fp16/fp32 soft-float** (no native fp8 ALU value-path); `–` for the
int-only / Newton ops.

`[CONFIDENCE: the op-class × dtype-gate legality is HIGH/OBSERVED (header predicates); the per-cell
on-device handler-existence is MED — the 16-bit (`mul16s`/`beqi a2,4/5`) and 32-bit (`mull`) width arms
and the 64-bit `bnei a2,1/12` tests are OBSERVED; not every cell's switch-arm was byte-walked through
the FLIX desync. The argmax/argmin `out_dtype=u32` rule is HIGH (header comment).]`

### 5.4 The unsupported-combo error paths

The §4 asserts are the on-device realisation of [Tensor-Tensor](tensor-tensor.md)'s "Unimplemented
tensor-tensor ALU op(0x%x)" and [Tensor-Scalar](tensor-scalar.md)'s "not supported op/dtype":
`[HIGH/OBSERVED]`

* op outside the dispatched class → `"not supported op"` (`alu_op.cpp:141/196/220`)
* dtype outside the dispatched width set → `"not supported dtype"` (`alu_op.cpp:231`; the 64-bit path's
  non-`{UINT64,INT64}` default)
* the unreachable guard → `alu_op.cpp:262 0` (bare assert)

---

## 6. What each op computes — the IVP vector mapping

On the DVE/POOL **vector** path the *same* `ALU_OP` selects an IVP intrinsic. The clean vocabulary
below was harvested from the CAYMAN `NX_DVE` PERF IRAM (the PERF image has clean-FLIX bundles). This
pins the op→mnemonic *family*; the exact per-op selection arithmetic sits in the desynced compute (MED).
The same mnemonics appear in the ISA batches [b01](../../isa/ref/b01-vec-alu-int.md) /
[b02](../../isa/ref/b02-vec-alu-fp.md) / [b03](../../isa/ref/b03-vec-alu-rest.md). `[HIGH presence
(PERF byte-clean) / MED exact per-op binding]`

| `ALU_OP` | IVP intrinsic family (DVE PERF, OBSERVED) | sat / round / sign |
|---|---|---|
| `ADD` | `ivp_addn_2x32`(i32)/`ivp_add2nx8t`(i8)/`ivp_addnxf16t`/`ivp_addn_2xf32t`(fp); `ivp_addsnx16t`(sat i16) | int=wrap; fp=RoundMode |
| `SUBTRACT` | `ivp_subn_2x32t`/`sub2nx8t`/`subnx16t`(int)/`subn_2xf32t`/`subnxf16t`(fp); `subsnx16t`(sat) | int=wrap; fp=RoundMode |
| `MULT` | `ivp_mulan_2x32`/`mulnx16c`/`mulpan16xr16`/`mulus*`/`mulsup*`/`dmuluuq2n8xr8`/`muln_2x32c` | int=wrap (widening MAC); fp=RoundMode |
| `DIVIDE` | `ivp_div0nxf16t`(fp16 recip seed) + `ivp_divnn_2xf32t`(fp32 div-step) | fp Newton; **not** general-arith |
| `MAX` | `ivp_bmaxnx16`/`bmaxn_2x32`(signed), `ivp_bmaxunx16`/`bmaxu2nx8`(unsigned), `ivp_maxnxf16t`/`maxn_2xf32t`(fp) | signed/unsigned **distinct opcodes** |
| `MIN` | `ivp_bminnx16`/`bminn_2x32`(s), `ivp_bminunx16`/`bminu2nx8`(u), `ivp_minn_2xf32t`(fp) | ditto |
| `BITWISE_AND/OR/XOR` | `ivp_and2nx8` / `ivp_or2nx8` / `ivp_xor2nx8` (+ `ornotb`) | bitwise; int-only |
| `BITWISE_NOT` | `notb` / xor-with-`-1` form | bitwise |
| `ARITH_SHIFT_L` | `ivp_sllnx16` (shl forms) | logical == arith for left |
| `ARITH_SHIFT_R` | `ivp_sranx16` / `ivp_sran_2x32` | sign-replicating (`sra`) |
| `LOGICAL_SHIFT_R` | `ivp_srlinx16` / `ivp_srln_2x32` / `ivp_lsr2nx8_i` | zero-fill (`lsr`/`srl`) |
| `IS_EQ` | `ivp_eq2nx8`/`eqnx16`/`eqn_2x32`(int); `ivp_oeqn_2xf32`/`oeqnxf16t`(fp ordered) | ordered fp eq; int eq |
| `IS_NE` | `ivp_neqn_2x32`(int); `ivp_une*`/`ulen`(fp unordered) | – |
| `IS_LT` | `ivp_lt2nx8`/`ltn_2x32`(signed)/`ltunx16`(unsigned); `ivp_oltn_2xf32`/`oltnxf16`(fp) | int s vs u distinct; fp ordered |
| `IS_LE/GE/GT` | `ivp_olen_2xf32`/`olenxf16`(fp `<=`); `>`/`>=` via operand-swap of lt/le | (swap is LOW) |
| `ABSOLUTE_DIFF` / `ABS_DIFF_INT` | `ivp_babssubnx16`/`babssub2nx8`(signed); `ivp_babssubunx16`/`babssubu2nx8`(unsigned) | signed/unsigned distinct opcodes |
| `ABSOLUTE_VALUE` | `ivp_abs*` (`\|a\|`; INT_MIN wraps unless saturating) | wrap on INT_MIN |
| `ABS_MAX/ABS_MIN` (M+) | `ivp_bmax`/`bmin` of `\|a\|,\|b\|` (composed) | LOW (composition) |
| `RE_LU` 0x22 (M+) | `ivp_bmax*` of `(src0, 0)` (max-with-zero) | MED |
| `SQUARE` 0x23 (M+) | `ivp_mul*` of `(src0, src0)` | MED |
| `SYMMETRIC_CLAMP` 0x24 (V+) | composed `sign(src0)·max(\|src0\|,\|src1\|)` (abs/max/sign) | LOW (header formula authoritative) |
| **cross-dtype legs** | | |
| int→fp | `ivp_float*`(signed) / `ivp_ufloat*`(unsigned) | RoundMode (FSR; RNE default) |
| fp→int | `ivp_trunc*`(signed) / `ivp_utrunc*`(unsigned) | round-toward-zero |
| fp16↔fp32 | `ivp_cvtf32f16`(widen, lossless) / `ivp_cvtf16f32`(narrow) | narrow = RoundMode |
| narrow/pack | `ivp_cvt*`/`sats`(saturating narrow); `packl`(wrap)/`pack`(round)/`packv*`(saturate) | sat / wrap / round |
| splat (TS broadcast) | `ivp_rep2nx8t` / `ivp_repnx16t` / `ivp_repn_2x32t` | (scalar broadcast; see [tensor-scalar.md](tensor-scalar.md)) |

> **NOTE — signed vs unsigned vs saturating are *distinct opcodes*, not a runtime mode.** `ivp_bmaxnx16`
> (signed) and `ivp_bmaxunx16` (unsigned) are different instructions; `ivp_addsnx16t` (saturating) is a
> different instruction from `ivp_addnx16` (wrapping). The lane-geometry tokens — `2NX8` (i8),
> `NX16` (i16), `N_2X32` (i32), `NXF16` (fp16), `N_2XF32` (fp32) — encode the element width directly in
> the mnemonic. A reimplementation selects the variant at lowering time from `(op, dtype-width,
> signedness, saturation-requested)`; there is no per-op "mode bit."

---

## 7. Reverse-operands / saturation / rounding

* **REVERSE-OPERANDS.** `alu_op.cpp` is **order-respecting**: it computes `R[d] = OP(R[s0], R[s1])`
  with the operands exactly as handed to it. The operand-**order** control lives in the **consumer**
  instruction. [Tensor-Scalar](tensor-scalar.md) carries a 4-valued `reverse_operands`
  `{None/First/Second/Both}` (`common.h:1383`) that flips `(tensor op scalar)` ↔ `(scalar op tensor)`
  per AluOp — the `{scalar−src vs src−scalar}` control. For non-commutative ops
  (`SUBTRACT`/`DIVIDE`/`MOD`/shifts/compares) the order is the consumer's `reverse_operands`, and
  `alu_op.cpp` evaluates whichever order it receives. `[HIGH/OBSERVED — tensor-scalar.md §4a header
  formula]`
* **SATURATION.** **Not** a runtime mode in `alu_op.cpp` — it is **encoded in the chosen primitive.**
  Integer `ADD`/`SUB`/`MULT` **wrap** (2's-complement modular: the scalar `add.n`/`sub`/`mull`, the
  vector `ivp_add*`/`sub*`); the saturating IVP variants (`ivp_addsnx16t`/`subsnx16t`) are **distinct**
  opcodes the tensor-engine selects in a separate clamp/dtype stage and have **no own `ALU_OP` enum
  value** (bound by name). The narrowing cross-dtype casts saturate (`sats`/`packv*`). `[HIGH int-wrap
  /OBSERVED ; MED sat-stage]`
* **ROUNDING.** Integer ops carry **no** rounding. Float ops round per the **FSR** special register
  (RoundMode; default **RNE**): int→fp and fp32→fp16-narrow round per FSR; **fp→int trunc is always
  round-toward-zero** (ignores FSR); fp16→fp32 widen is lossless. `[HIGH/OBSERVED]`
* **SIGNEDNESS.** A per-op property of the int-band ops (`is_signed_alu_op`/`is_unsigned_alu_op`, §3.2)
  *and* of the chosen IVP variant (`ivp_bmaxnx16` signed vs `ivp_bmaxunx16` unsigned are distinct
  opcodes). The src/dst dtype signedness **must match** the op's signedness. On the scalar path one leaf
  serves both via the runtime `bbci` signedness-flag branch (§4.2 GOTCHA). `[HIGH/OBSERVED]`

---

## 8. Per-generation presence (summary)

| GEN | `ALU_OP` count | base adds | int band | `alu_op.cpp` linked into |
|---|---|---|---|---|
| SUNDA (v2) | **33** | base `0x00..0x1D` | `0xC4/C5/C6` | all 5 engines (RELEASE) |
| CAYMAN (v3) | **60** | (same base) | `0xC4..0xE1` | all 5 engines × {DBG,PERF,TEST} |
| MARIANA (v4) | **64** | + `0x20..0x23` | `0xC4..0xE1` | all 5 engines × {DBG,PERF,TEST} |
| MARIANA_PLUS | **64** (==v4) | (mariana ISA) | `0xC4..0xE1` | all 5 engines × {DBG,PERF,TEST} |
| MAVERICK (v5) | **65** | + `0x20..0x24` | `0xC4..0xE1` (+ `is_valid_int_aluop_dve` relaxation) | DVE/PE/POOL/SP × {PERF,TEST,+DVE DBG} (ACT/some DBG not in this container) |

The enum and the op-class predicates are byte-identical across the four gens for the ops they share;
growth is strictly additive. On **SUNDA** the int compares / min / max / abs / div / mod `_INT`/`_UINT`
ops are **not available** (only `Add/Mult/Sub_INT`); they enter at **CAYMAN**.
`ABS_MAX/MIN/RE_LU/SQUARE` enter at **MARIANA**; `SYMMETRIC_CLAMP` at **MAVERICK**. The per-op presence
column of §5.3 is gated by this per-gen enum membership. `[HIGH/OBSERVED]`

> **PROVENANCE — v2–v4 are byte-grounded; v5 interiors are header-OBSERVED only.** The SUNDA/CAYMAN/
> MARIANA enum, predicates, and dispatch structure are read from both the headers **and** the carved
> firmware. The MAVERICK (`MAVERICK`) enum and predicates are read from the **header** (compile-
> verified), but several MAVERICK *interior* facts (the exact 0x24 `SYMMETRIC_CLAMP` lowering, the DVE
> int-band relaxation's on-device dispatch) are header-OBSERVED / INFERRED, not byte-walked in the
> firmware. Flagged accordingly.

---

## 9. Reimplementation checklist

1. **Implement the full per-gen `ALU_OP` enum** as `NEURON_ISA_PACKED` (1 byte): SUNDA = base
   `0x00..0x1D` (30) + `ADD_INT/MULT_INT/SUBTRACT_INT` (`0xC4/C5/C6`); CAYMAN = + full int band
   `0xC4..0xE1`; MARIANA = + `0x20..0x23`; MAVERICK = + `0x24`. Reserve the `0x1E/0x1F` and `0x25..0xC3`
   gaps; never reuse them.
2. **Two operand shapes, one evaluator.** Decode the wire format in the consumer
   ([Tensor-Tensor](tensor-tensor.md)/[Tensor-Scalar](tensor-scalar.md)/…), then funnel into a single
   `R[d] = OP(R[s0], R[s1]/imm)` evaluator. The compare ops are shared with `BranchCompare`.
3. **Gate before evaluate.** Apply `is_general_arith_op (17/21) || POW || is_valid_int_aluop (26)` for an
   arith opcode; `is_general_bitvec_op (9)` for a bitvec opcode; the int-band signedness gate
   (`is_signed_alu_op`/`is_unsigned_alu_op`); and `is_valid_dtype`/`is_valid_dtype_64`. Reject out-of-class
   → "not supported op"; reject out-of-width-set → "not supported dtype".
4. **Dispatch by dtype width.** 8/16-bit → 16-bit native ops (`mul16s`/…); 32-bit → 32-bit (`mull`/…);
   64-bit (`UINT64`/`INT64` **only**) → the 32-bit-half-synthesis handlers; fp → soft-float through the
   FP32 hub; cross-dtype → `float`/`trunc`/`cvt` legs.
5. **Encode sat/sign in the primitive, not a mode bit** (vector path); on the scalar path, read the
   dtype-signedness flag at compute time to pick `saltu/bltu` vs `salt/blt`. Integer ops wrap; fp rounds
   per FSR (RNE default); fp→int truncates toward zero.
6. **Operand order is the consumer's job** (`reverse_operands`), not the evaluator's.

See also: [tensor-tensor.md](tensor-tensor.md) (the 60-op CAYMAN base table this page is the full
reference for), [tensor-tensor-64bit.md](tensor-tensor-64bit.md) (the `is_valid_int_aluop` 26-op 64-bit
arm), [tensor-scalar.md](tensor-scalar.md) (the reverse-operands + two-AluOp composition),
[scalar-tensor-tensor.md](scalar-tensor-tensor.md) (the STT fused form), and
[dtype-model.md](dtype-model.md) (the dtype ordinals).
