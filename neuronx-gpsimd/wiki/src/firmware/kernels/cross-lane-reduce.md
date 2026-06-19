# CrossLaneReduce — `pool_cross_lane_reduce_arith` / `_bitvec`

The **CrossLaneReduce** kernel is the GPSIMD POOL-engine primitive that folds the values **across the
32 SIMD lanes** of a vector register down to a single scalar (or, in partition mode, one scalar per
within-partition lane position). It is the innermost of the three reduce layers in the Neuron stack —
the **intra-register lane fold** the cross-partition Tensor-Reduce engine and the cross-core SDMA-CCE
all-reduce build on top of. Two POOL opcodes share one 64-byte operand struct and one 6-entry reduce-op
enum; both route into a single worker that issues the dedicated **`ivp_r*` cross-lane reduce** vector ops
(the [B08 reduce ISA batch](../../isa/ref/b08-reduce.md)), whose exact value/widening/saturation
semantics are pinned by driving the `libfiss-base.so` `module__xdref_r*` oracle leaves **live via
`ctypes`**.

Everything below is derived from static analysis of the shipped GPSIMD device images, the shipped
`ncore2gp/config/` ISS oracle (`libfiss-base.so`), and the shipped CAYMAN/SUNDA/MAVERICK arch-isa
interface headers, read with the Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) and stock
binutils/`ctypes`. Every claim carries a confidence tag `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`.

---

## 1. Executive summary

CrossLaneReduce is **two POOL opcodes** sharing **one 64-byte operand struct**
(`NEURON_ISA_TPB_S4D4_CR_STRUCT`) and **one reduce-op enum** (`NEURON_ISA_TPB_REDUCE_OP`), structurally
parallel to the Tensor-Tensor arith/bitvec split:

| opcode | name | kernel_info idx | funcVA | entry symbol |
|---|---|---|---|---|
| **0x7c** (124) | `CrossLaneReduceArith` | idx1 | `0x010003f8` | `pool_cross_lane_reduce_arith()` |
| **0x7d** (125) | `CrossLaneReduceBitvec` | idx2 | `0x01000410` | `pool_cross_lane_reduce_bitvec()` |

Both trampolines tail into **one** worker `cross_lane_reduce_impl(bool)` @ `0x01000440` — the `bool`
argument selecting arith (`0x7c`, `bool=0`) vs bitvec (`0x7d`, `bool=1`). The worker decodes the
S4D4_CR struct, reads the 1-byte `reduce_op` (the §4 enum), the 1-byte `reduce_axis`, the in/out dtypes,
`num_active_channels`, and the fp32 `scale`; it walks the strided 4-D src `Tensor4d` and dispatches the
per-(op, dtype) reduce through `clr_reduce_local(uint, uchar, NEURON_ISA_TPB_REDUCE_OP, float)` @
`0x0100088c` — the function whose **mangled name literally carries the `REDUCE_OP` selector and the float
scale**. `[HIGH/OBSERVED]` (the `.xt.prop` demangle names every symbol).

The **reduce-op table** is recovered byte-exact from the arch-isa enum `NEURON_ISA_TPB_REDUCE_OP`: six
ops — **`ADD=0, AVERAGE=1, MAX=2, BITWISE_OR=3, BITWISE_AND=4, BITWISE_XOR=5`** — a small dedicated reduce
enum, *not* the 60-entry `NEURON_ISA_TPB_ALU_OP` of Tensor-Tensor. The **algorithm** is the log-step lane
fold: read all 32 (or 64 int8 / 16 int32) lanes of the 512-bit register, fold with `N−1` associative merge
steps of the matching scalar element primitive, write one scalar. **Accumulator widening** is op-specific:
`ADD` widens (int8→int16, int16→int32); `MAX`/`MIN` do not. The **segmented** model is the `reduce_axis`
field: `All` collapses the whole src to one element; `Partitions` reduces across partitions only,
preserving the within-partition lane layout.

> **The three-tier reduce distinction (read this first).** CrossLaneReduce is the **intra-register /
> intra-core** leg. It is distinct from BOTH (a) the **cross-PARTITION** Tensor-Reduce engine (SEQ opcodes
> `0x46`/`0x47`/`0x52`, DRAM string `"Tensor-Reduce"`), which folds across SBUF/PSUM partitions, AND
> (b) the **cross-CORE / cross-BUFFER** SDMA-CCE reduce of the collective stack
> (`SDMA_CCETYPE {ADD0, FMA1, MAX2, MIN3}`). The three **compose**: lane-fold (this kernel) →
> partition-fold (Tensor-Reduce) → ring/mesh buffer-fold (SDMA CCE). The header's own associativity license
> ("the order … is not architecturally guaranteed") is what makes the composition order-free.
> `[HIGH/OBSERVED]`

---

## 2. Dispatch chain — `.xt.prop` function starts (CAYMAN_0)

The handler chain sits at the **front** of the `0xa260` `EXTISA_0` POOL image. Function-start VMAs are
read from the first 12-byte record of each `.xt.prop.<mangled>` section
(`[VMA:LE32][size:LE32][flag:LE32]`, flag `0x2804` = function-start), demangled with `c++filt`. All
`[HIGH/OBSERVED]`.

| funcVA | demangled signature | role |
|---|---|---|
| `0x010003f8` | `pool_cross_lane_reduce_arith()` | ARITH entry (op `0x7c`) |
| `0x01000410` | `pool_cross_lane_reduce_bitvec()` | BITVEC entry (op `0x7d`) |
| `0x01000440` | `cross_lane_reduce_impl(bool)` | the shared worker |
| `0x0100088c` | `clr_reduce_local(unsigned int, unsigned char, NEURON_ISA_TPB_REDUCE_OP, float)` | per-(op,dtype) dispatcher |

Verified prologue bytes (`file_off = VMA − 0x1000000 + 0x100`, `xxd` OBSERVED):

```
0x010003f8: 36 41 00          entry a1, 32          ; pool_cross_lane_reduce_arith (thin trampoline)
0x01000410: 36 41 00          entry a1, 32          ; pool_cross_lane_reduce_bitvec (thin trampoline)
0x01000440: 36 01 03 ...      entry a1, 0x180 ;     ; cross_lane_reduce_impl — the big worker
                              movi a10,-64 ; and a8,a1,a10 ; movsp a1,a8   (SP aligned 64B for vec spills)
0x0100088c: 36 41 00          entry a1, 32          ; clr_reduce_local
```

The two 24-byte trampolines are **byte-identical except one selector byte** — the `bool` sub-op handed to
`cross_lane_reduce_impl`:

```
@0x3f8 (arith):  36 41 00  2f 00 a0 05 00 00  c2 42 24  40 04 e0 02  00 0c 02  1d f0  ...
@0x410 (bitvec): 36 41 00  2f 00 a1 05 00 00  c2 42 24  40 04 e0 02  00 0c 02  1d f0  ...
                              ^^ a0 vs a1 = the bool selector (0=arith, 1=bitvec). retw.n (1d f0) closes.
```

`[HIGH/OBSERVED]` on the entry/selector/`retw` skeleton; `[MED/INFERRED]` on the mid-bundle route (the
body is dense FLIX VLIW — the `2f` is the F-format selector byte — and desyncs under stock objdump's
linear sweep, the documented native-disasm limitation).

**Dispatch routing.** The POOL `kernel_info_table` binds `idx1 → 0x7c → 0x010003f8` and
`idx2 → 0x7d → 0x01000410`. The SEQ master dispatch hub carries `0x7c '|' → CrossLaneReduce` and
`0x7d '}' → CrossLaneReduce (variant)`; the sequencer name string `"CrossLaneReduce"` is baked at DRAM
`0x820b4`, and the device-side log token `"S: CrossLaneReduce"` is OBSERVED at `libnrtucode_internal.so`
file-offset `0x1cfcf4`. `[HIGH/OBSERVED]`

> **NOTE — the SEQ hub also carries a *separate* Tensor-Reduce family `'F'`=`0x46` / `'G'`=`0x47` /
> `'R'`=`0x52` (DRAM string `"Tensor-Reduce"` @ `0x81f8b`).** That is the **cross-partition** reduce
> engine, a *distinct* datapath from this kernel — do not conflate the two. CrossLaneReduce's worker is
> `cross_lane_reduce_impl` @ `0x440`; the `'F'`/`'R'` impl lives elsewhere. See §8. `[HIGH/OBSERVED]`

The opcode numbering is **stable across generations**: the SUNDA (newest-gen) `all.stripped.so` opcode→
pool-function JSON map lists `pool_cross_lane_reduce_arith` @ `124` and `pool_cross_lane_reduce_bitvec` @
`125` — byte-identical to CAYMAN. `[HIGH/OBSERVED for op #]`

---

## 3. The operand struct — `NEURON_ISA_TPB_S4D4_CR_STRUCT` (64 bytes)

Source: `aws_neuron_isa_tpb_s4d4_cr.h` (CAYMAN arch-isa header, marked "ISA header for NC-v3"). The
header's own format comment names it: *"Neuron 'S4D4_cr' Format … one 4d SRC Tensor, one 4d DST Tensor.
Use for: CrossLaneReduce"* (**S4D4_CR** = one 4-D src, one 4-D dst, **C**ross-**R**educe). The
`ISA_STATIC_ASSERT(sizeof(...) == 64)` is the compile-time width guarantee, and
`instruction_mapping.json` binds the struct to **both** opcodes (OBSERVED):

```json
"NEURON_ISA_TPB_S4D4_CR_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_CROSS_LANE_REDUCE_ARITH",
    "NEURON_ISA_TPB_OPCODE_CROSS_LANE_REDUCE_BITVEC"
]
```

### 3.1 Field layout

| off | size | field | type | notes |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode:1 (0x7c\|0x7d), inst_word_len:1, debug_cmd:1, debug_hint:1}` |
| 4–11 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12–31 | 20 | `src_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | INPUT tensor (4-D strided access pattern) |
| 32 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` | source element dtype |
| 33 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | destination (accumulator) dtype |
| 34 | 1 | `num_active_channels` | `uint8_t` | partition/channel count to reduce over |
| **35** | 1 | **`reduce_op`** | `NEURON_ISA_TPB_REDUCE_OP` | **the reduce-op selector (§4)** |
| **36** | 1 | **`reduce_axis`** | `NEURON_ISA_TPB_REDUCE_AXIS` | `All(0)` / `Partitions(1)` — the segment model (§7) |
| 37–39 | 3 | `reserved0[3]` | `uint8_t[3]` | must be zero (`s4d4_cr_reserved_zero`) |
| **40–43** | 4 | **`scale`** | `float` (fp32) | **the AVERAGE multiplier** (each elem `*= scale`) |
| 44–63 | 20 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | OUTPUT tensor |

`4 + 8 + 20 + 1 + 1 + 1 + 1 + 1 + 3 + 4 + 20 = 64`. The `STATIC_ASSERT` and the per-field arithmetic
agree. `[HIGH/OBSERVED]`

### 3.2 `NEURON_ISA_TPB_TENSOR4D` (20 bytes)

```c
typedef struct NEURON_ISA_TPB_TENSOR4D {
    NEURON_ISA_TPB_ADDR4 start_addr;    //  4  (a union: addr_immediate | addr_reg; markers IMM=0x00,
                                        //      ADDR_REG=0x80, SHAPE_REG=0x40, INDIRECT_IMM=0x20, …)
    int16_t              step_elem[4];  //  8  per-dim signed element stride
    uint16_t             num_elem[4];   //  8  per-dim element count
} NEURON_ISA_TPB_TENSOR4D;             // = 4 + 8 + 8 = 20
```

> **NOTE — CrossLaneReduce uses a *4-D* access pattern (`TENSOR4D`), unlike the Tensor-Tensor 3-D
> `TENSOR3D`.** The extra dimension is the **reduce dimension** the lane fold walks. The `start_addr`
> `ADDR4` union carries the SBUF partition-offset encoding; src spans `num_active_channels` partitions, dst
> spans exactly 1 (the reduce collapses partitions to one). `[HIGH/OBSERVED]`

### 3.3 DTYPE encoding (`NEURON_ISA_TPB_DTYPE`, 4-bit)

`INVALID 0x0` (used in RTL for bitvec) · `UINT64 0x1` · `INT8 0x2` · `UINT8 0x3` · `INT16 0x4` ·
`UINT16 0x5` · `BFLOAT16 0x6` · `FP16 0x7` · `INT32 0x8` · `UINT32 0x9` · `FP32 0xA` · `FP32R 0xB` ·
`INT64 0xC` · `FP8_EXP3 0xD` · `FP8_EXP4 0xE` · `FP8_EXP5 0xF`. `[HIGH/OBSERVED]`

### 3.4 Validity predicates (the rust-style assertion comments, OBSERVED)

```c
// is_valid_cross_lane_reduce(i) =
//    has_valid_neuron_header && has_valid_neuron_events
// && has_cross_lane_reduce_opcode(i)                                  // opcode ∈ {0x7c, 0x7d}
// && s4d4_cr_reserved_zero(i)                                         // reserved0[0..2] == 0
// && is_valid_dtype(in_dtype,  AllowFP32R=False)                      // src MAY NOT be fp32r
// && is_valid_dtype(out_dtype, AllowFP32R=True)                       // dst MAY be fp32r
// && start_addr_active_channels(src.start_addr, num_active_channels)  // src spans N partitions
// && start_addr_active_channels(dst.start_addr, 1)                    // dst spans exactly 1
// && check_active_channels(num_active_channels)
// && tensor4d_valid(src, in_dtype,  WriteTensor=False, AllowedInPSUM=False, AllowedInSBUF=True)  // src in SBUF, RO
// && tensor4d_valid(dst, out_dtype, WriteTensor=True,  AllowedInPSUM=False, AllowedInSBUF=True)  // dst in SBUF, written
// && valid_reduce_axis_enum(i)                                        // reduce_axis ∈ {All, Partitions}
// && valid_axis_dst_element_cnt(i)                                    // §7
```

> **GOTCHA — `in_dtype` may NOT be `FP32R`, but `out_dtype` MAY be.** The src is read with
> `AllowFP32R=False`; the accumulator/dest is read with `AllowFP32R=True`. A reimplementer who allows the
> round-mode `FP32R` (`0xB`) on the *input* side will accept an instruction the hardware decoder rejects.
> Both src and dst must live in **SBUF** (`AllowedInPSUM=False`); the src tensor is read-only, the dst is
> written. `[HIGH/OBSERVED]`

---

## 4. The reduce-op table — `NEURON_ISA_TPB_REDUCE_OP` (the complete, closed enum)

Source: `aws_neuron_isa_tpb_s4d4_cr.h`, `enum NEURON_ISA_TPB_REDUCE_OP`, `NEURON_ISA_PACKED` (1 byte). This
is the **complete, closed** enumeration of every value `s4d4_cr.reduce_op` can hold and the worker
switches on — **six ops**, a small dedicated reduce enum. The header's own *"Behavior"* comment gives the
exact semantics of each (quoted verbatim, OBSERVED):

| code | name | legal-in | header semantics (verbatim) | value primitive (xdref / ivp) | conf |
|---|---|---|---|---|---|
| `0` | `ADD` | arith (0x7c) | *"add all elements, return single result. fp32 math."* | `radd*` / soft-float `add_*_32f` | HIGH(enum)/HIGH(sem) |
| `1` | `AVERAGE` | arith (0x7c) | *"multiply each element by scale field (fp32), then add them."* | per-lane `×scale` then `radd` (scale @ off 40) | HIGH(enum)/HIGH(sem) |
| `2` | `MAX` | arith (0x7c) | *"find the max element (fp32 math)"* | `rmax*` / `rmaxnum*` (NaN-suppress fp) | HIGH(enum)/HIGH(sem) |
| `3` | `BITWISE_OR` | bitvec (0x7d) | *"bitwise OR of all elements (raw u8/u16/u32, no fp32 conversion)"* | `rorb*` (full-vector OR fold) | HIGH(enum)/HIGH(sem) |
| `4` | `BITWISE_AND` | bitvec (0x7d) | *"bitwise AND of all elements (raw …, no fp conv)"* | `randb*` (full-vector AND fold) | HIGH(enum)/HIGH(sem) |
| `5` | `BITWISE_XOR` | bitvec (0x7d) | *"bitwise XOR of all elements (raw …, no fp conv)"* | XOR-fold (no dedicated reduce leaf — see CORRECTION) | HIGH(enum)/MED(prim) |

The enum closes at `0x5`; total = 6. The **arith** handler (`0x7c`) takes `ADD`/`AVERAGE`/`MAX` (value
reductions, fp32 math); the **bitvec** handler (`0x7d`) takes `BITWISE_OR`/`AND`/`XOR` (raw integer
bit-fold, no fp conversion). This is the exact arith-vs-bitvec partition that motivates the two opcodes.
The enum is **byte-identical across the CAYMAN, MARIANA, SUNDA, and MAVERICK arch-isa headers** (verified:
same six values, same packing). `[HIGH/OBSERVED]`

> **The header's associativity license (verbatim, OBSERVED):** *"The operators are all
> associative/commutative and the order in which the operations is done is not architecturally
> guaranteed."* This is the architectural permission for the device to use a **balanced log-step tree**
> (32 → 16 → 8 → 4 → 2 → 1) or any lane-pairing; the result is order-independent. The ISS oracle realises
> the *same* fold as a flat left-to-right scan — bit-identical for the associative ops. `[HIGH/OBSERVED]`

### 4.1 `reduce_op → ivp r-prefix` value-primitive mapping

The device kernel issues the dedicated single-instruction **`ivp_r*` cross-lane reduce** TIE ops
(`s3_alu`-slot for arith, `s1_ld` load-slot for the bool fold — see [B08](../../isa/ref/b08-reduce.md));
one FLIX slot op reduces a whole 512-bit register in hardware). The mapping, tied to the B08 roster and
confirmed by driving the `libfiss-base.so` `module__xdref_r*` oracle (§5):

```
ADD        -> ivp_radd{nx16,2nx8,n_2x32} (int, widening) / radds (saturating) / raddu (unsigned)
            / per-lane add_*_32f soft-float chain (fp; "fp32 math")
AVERAGE    -> the same radd path PREFIXED by an elementwise (lane * scale) fp32 multiply
MAX        -> ivp_rmax{nx16,2nx8,n_2x32} (int) / ivp_rmaxnum{nxf16,_2xf32} (fp, NaN-suppressing)
BITWISE_OR -> ivp_rorb{n,2n,n_2}         (full-vector OR fold)
BITWISE_AND-> ivp_randb{n,2n,n_2}        (full-vector AND fold)
BITWISE_XOR-> [no dedicated ivp_rxorb reduce leaf — folded via the xorb element prim; name INFERRED]
```

> **CORRECTION — there is NO dedicated `module__xdref_rxorb*` reduce leaf.** An `nm -D libfiss-base.so | rg
> -c 'xdref_rxorb'` returns **0**. What exists is `module__xdref_xorb_64_64_64` @ `0x856fa0` — a 2-input
> *element* bitwise-XOR primitive (it copies `(%rsi)`→`(%rdx)` for two dwords), not a full-vector reduce
> fold. The `BITWISE_XOR` reduce is therefore realised by chaining the `xorb` element op across lanes (the
> XOR fold has no single-instruction `r`-prefix form like `randbn`/`rorbn` do). The `ivp_rxorb*` name in
> earlier notes is **family analogy, not a recovered symbol** — `[MED/INFERRED]`. The `BITWISE_OR` and
> `BITWISE_AND` reduces DO have dedicated leaves (`rorbn_64_64` @ `0x81ce30`, `randbn_64_64` @ `0x81cc90`),
> driven live in §5.4.

The full `ivp_r*` cross-lane vocabulary harvested across the ISA
(`radd`/`radds`/`raddu`/`rmin`/`rmax`/`rminu`/`rmaxu`/`rminnum`/`rmaxnum`/`rbmin`/`rbmax`/`randb`/`rorb`
and the `_t` predicated forms — see [B08 §2](../../isa/ref/b08-reduce.md)) is larger than the 6 ops
CrossLaneReduce's enum reaches: the `MIN` and signed/unsigned int min/max forms in the ISA serve the
Tensor-Reduce / Tensor-Scalar consumers, not all reachable from this kernel's 6-op enum. `[HIGH]` for the
6 ops CrossLaneReduce uses; `[HIGH]` for the broader vocabulary.

---

## 5. The cross-lane reduce algorithm — byte-exact, driven live

The device issues the single-instruction `ivp_r*` reduce ops; to pin the **exact** value/order/widening
those ops realise, the in-package ISS functional model `libfiss-base.so` is disassembled and **executed**.
Each `ivp_r*` op maps to a `module__xdref_r*` primitive whose body *is* the executable lane fold. The
`.text` is VMA==file-offset; the leaves are callable in-process with no license. ABI (OBSERVED from the
`radd` prologue): `rdi` = ctx (unused), `rsi` = pointer to the 512-bit `vec`, `rdx` = pointer to the
output. Eleven leaves driven live this pass.

### 5.1 REDUCE-ADD — widening, exact (the required sum case)

Leaf `module__xdref_radd_nx16_32_512` @ `0x858690`, annotated:

```c
// module__xdref_radd_nx16_32_512  @ 0x858690
// ABI: rdi = ctx (unused), rsi = ptr to 512-bit vec (32 × int16), rdx = ptr to int32 out
void radd_nx16(const int16_t in[32], int32_t *out) {
    int32_t acc = 0;
    for (int l = 31; l >= 0; --l)                 // 0x8586a1: movzwl 0x3e(%rsi) … stepping DOWN by 2 to 0x00
        acc += (int32_t) sext_32_16(in[l]);       // each lane SIGN-EXTENDED to 32b (module__xdref_sext_32_16@plt,
                                                  //   called EXACTLY 32 times) — the accumulator WIDENS 16->32
    *out = acc;                                   // 31 `add …(%rsp),%eax` folds (N-1 for N=32); mov %eax,(%rbx)
}
```

Because the accumulator is 32-bit and `32 × |int16| ≤ 32 × 2¹⁵ = 2²⁰ ≪ 2³¹`, **`radd` is exact and
never overflows**. The width grid (lane count = `512 / elemW`; ADD widens):

| leaf | lanes | accum | widen |
|---|---|---|---|
| `radd_2nx8_16_512` @ `0x80ee60` | 64 × int8 | int16 | 8→16 |
| `radd_nx16_32_512` @ `0x858690` | 32 × int16 | int32 | 16→32 |
| `radd_n_2x32_32_512` @ `0x5c0410` | 16 × int32 | int32 | **none** (wraps mod 2³²) |

`raddu_*` are the unsigned siblings (`zext` instead of `sext`). Driven live:

```
== radd (signed widening 16->32, exact, NO saturate) ==
radd(32 × 1)        = 32
radd(32 × 32767)    = 1048544    (= 32×32767 exactly — 32-bit accum, no overflow)
radd(32 × -32768)   = -1048576   (= 32×-32768 exactly)
radd([100,-50,0…])  = 50
== raddu (unsigned widening) ==
raddu(32 × 0xFFFF)  = 0x1FFFE0   (= 32×65535 exactly)
== width grid edges ==
radd_2nx8(64 × 127)         = 8128        (= 64×127, widened 8->16)
radd_n_2x32(16 × 0x10000000)= 0x00000000  (= 0x100000000 WRAPS — the one non-widening sum)
```

`[HIGH/OBSERVED by execution]`

> **GOTCHA — the 16×int32 sum (`raddn_2x32`) is the one that wraps.** Unlike `radd_2nx8` (8→16) and
> `radd_nx16` (16→32), `radd_n_2x32` does **not** widen — 32-bit lanes accumulate into a 32-bit result, so
> `16 × 0x10000000 = 0x100000000` wraps to `0`. A reimplementer modelling all `radd` forms as "widening
> never overflows" diverges here: the int32 reduce can overflow. `[HIGH/OBSERVED by execution]`

### 5.2 SATURATING REDUCE-ADD — `radds` (a fractional pack, NOT an int16 clamp)

Leaf `module__xdref_radds_nx16_16_512` @ `0x814970`. It accumulates in a **21-bit** intermediate
(`and $0x1fffff` after each `movswl` — 16 value bits + 5 = log₂32 carry bits) and re-packs into a 16-bit
field, rather than clamping to `int16`:

```c
// module__xdref_radds_nx16_16_512  @ 0x814970   (saturating / fractional-pack reduce)
void radds_nx16(const int16_t in[32], uint16_t *out) {
    int32_t acc = 0;
    for (int l = 0; l < 32; ++l)
        acc = (acc + (sext16(in[l]) & 0x1FFFFF)) & 0x1FFFFF;   // 0x8149d1: and $0x1fffff — 21-bit running acc
    *out = pack_sign_mag_16(acc);   // re-pack the 21-bit acc into a {sign, magnitude} 16-bit field
}
```

```
== radds (fractional / packed SATURATING reduce, 16-bit out) ==
radds(32 × 32767)  = 0x7FFF (32767)     <- saturates toward the +max field
radds(32 × -32768) = 0x8000 (32768u)    <- sign-flagged field, NOT a clamp to INT16_MIN
radds([100,-50,0…])= 0x0032 (50)        <- in-range: pass-through
```

> **QUIRK — `radds` is a *fractional / packed* saturating reduce, not an int16 clamp.** Driven live,
> `radds(32 × −32768)` returns **`0x8000`** (the sign-flagged field, = 32768 unsigned), *not* a clamp to
> `INT16_MIN`. The 21-bit accumulator + sign-and-magnitude re-pack is the fixed-point/normalized
> saturation idiom (the same "normalize-not-clamp" philosophy as the vector `baddnorm`), aimed at
> fixed-point dot-product tails where the sum is a Q-format fraction. A reimplementer who models `radds`
> as `clamp(Σ, INT16_MIN, INT16_MAX)` diverges on every overflow case. `[HIGH/OBSERVED by execution]`

### 5.3 REDUCE-MAX / MIN — the fold, signed vs unsigned (the required tie case)

Leaf `module__xdref_rmax_nx16_16_512` @ `0x858990` folds with the per-lane `module__xdref_max_16_16_16`
@ `0x858500` (called exactly 31 times = `N−1`):

```c
// module__xdref_rmax_nx16_16_512  @ 0x858990
// ABI: rsi = 512-bit vec (32 × int16), rdx = int16 out
void rmax_nx16(const int16_t in[32], int16_t *out) {
    int16_t acc = in[0];
    for (int l = 1; l < 32; ++l)
        acc = max_s16(acc, in[l]);    // 31 chained max_16_16_16@plt — signed (sign-bit-flip bias)
    *out = acc;                       // result is ONE 16-bit lane (NO widen for min/max)
}
```

`MAX`/`MIN` do **not** widen (output width == input width; max/min cannot overflow). The width grid:
`rmax_2nx8_8_512` (64→8b), `rmax_nx16_16_512` (32→16b), `rmax_n_2x32_32_512` (16→32b, 15 calls to
`max_32_32_32` = `N−1` for N=16); `rmaxu`/`rminu` are the unsigned forms. Driven live over the **same lane
bits**:

```
== rmax (signed) vs rmaxu (unsigned) over identical lane data ==
lanes = [-1, +1, -1000 ×30]
  signed   rmax  = 0x0001 (+1)     <- -1 < +1 signed, so +1 wins
  unsigned rmaxu = 0xFFFF          <- 0xFFFF(=-1) > 1 unsigned, so the "-1" lane wins
  signed   rmin  = -1000           <- the polarity flip
lanes = [0x8000, 0x0001, 0 ×30]
  signed   rmax  = 0x0001          <- 0x8000 = -32768 signed, smaller than +1
  unsigned rmaxu = 0x8000          <- 0x8000 is the largest UNSIGNED value present
```

The same physical lane bits reduce to **different** extrema under the signed vs unsigned interpretation —
the cleanest proof that `rmax`/`rmaxu` are genuinely distinct ops, not aliases. `[HIGH/OBSERVED by
execution]`

The fp forms `rmaxnum`/`rminnum` use the **NaN-suppressing** primitive: `module__xdref_rmaxnum_nx16f_1_16f_512f`
@ `0x524e00` tail-calls `function__fp_hp_rmaxnum_32` (the IEEE-754-2019 `maximumNumber` flavour — a NaN
lane is treated as the identity, never poisoning the reduction), consistent with the header's "fp32 math".
`[HIGH/OBSERVED]` on the leaf call; `[MED/INFERRED]` on the exact NaN/−0 corner (read from the `num` leaf
name + the fp16 tail-call, not sweep-driven this pass).

### 5.4 BITWISE REDUCE — `randbn` (AND) / `rorbn` (OR) — the bitvec path

Leaves `module__xdref_randbn_64_64` @ `0x81cc90` (reduce-AND) and `module__xdref_rorbn_64_64` @ `0x81ce30`
(reduce-OR) fold a 64-bit `vbool` to a single bit. **The predicate is the LOW bit of each 2-bit lane
pair**; the high bit is ignored:

```c
// module__xdref_randbn_64_64  @ 0x81cc90   (reduce-AND: all lanes true?)
// module__xdref_rorbn_64_64   @ 0x81ce30   (reduce-OR : any lane true?)
// ABI: rsi = ptr to 64-bit vbool in, rdx = ptr to 64-bit vbool out (result in low bit)
void randbn(uint64_t vb, uint64_t *out) {
    int acc = 1;
    for (int l = 0; l < 32; ++l)
        acc &= (vb >> (2*l)) & 1;   // low bit of each 2-bit lane is the predicate; high bit ignored
    *out = acc;                     // 1 iff EVERY lane's predicate bit is set
}
```

```
== reduce-AND / reduce-OR over a 64-bit vbool (low bit of each 2-bit lane = predicate) ==
v = all bits 1                 randbn = 1   rorbn = 1
v = 0x5555… (low bit / lane)   randbn = 1   rorbn = 1     <- every lane TRUE
v = 0xAAAA… (high bit / lane)  randbn = 0   rorbn = 0     <- NO lane true (high bit ignored)
v = all 0                      randbn = 0   rorbn = 0
v = 0x3 (lane0 both bits)      randbn = 0   rorbn = 1     <- not all true, but some true
v = 0x1 (lane0 low bit only)   randbn = 0   rorbn = 1
```

`randbn` = AND-reduce (`BITWISE_AND`), `rorbn` = OR-reduce (`BITWISE_OR`); the `0xAAAA…` row proves the
predicate is the **low** bit of each 2-bit lane pair. The bitwise reduces keep the raw integer width — no
widen, no fp conversion (matching the header's "raw u8/u16/u32, no fp32 conversion"). `[HIGH/OBSERVED by
execution]`

### 5.5 PREDICATED / SEGMENT-BOUNDED reduce — the `_t` and `rb*` forms

Every reduce primitive has a `_t` predicated sibling that folds only the active (`vbool`-true) lanes —
e.g. `module__xdref_radd_nx16_32_512_t` @ `0x85ab00`, whose frame is `sub $0x108,%rsp` (a **larger**
`0x108`-byte frame than the base `radd`'s `0x80`, to hold the `vbool` mask). The **`rb*` family**
(`module__xdref_rbmax_16` @ `0x859e60`, `rbmax_nx16_64_512_512` @ `0x859f00`) is the bool-bounded
reduce-with-argmax: `rbmax_16` builds a per-lane position bit (`mov $0x1,%r9d ; shl %cl,%r9d` @ `0x859e6f`)
and writes a **second** word (the predicate) alongside the value — the horizontal **argmax** (value +
winning-lane mask).

```c
// module__xdref_rbmax_16  @ 0x859e60   (one fold step: combine a new lane into {value, argmax-pred})
void rbmax_step(int16_t nv, int lane, const acc_t *in, acc_t *out) {
    uint32_t bit = 1u << lane;                    // 0x859e6f: shl %cl — this lane's predicate bit
    int16_t  nu  = (uint16_t)nv ^ 0x8000;         // signed compare via sign-flip bias (shl $0xf at 0x859e94)
    int16_t  au  = (uint16_t)in->val ^ 0x8000;
    if (nu >  au) { out->val = nv;       out->pred = bit;            }  // strictly greater -> replace, fresh flag
    else if (nu == au) { out->val = in->val; out->pred = in->pred | bit; } // 0x859ed8 je -> TIE: keep val, OR the bit
    else          { out->val = in->val;  out->pred = in->pred;       }  // smaller -> keep
}
```

> **QUIRK — the argmax flag is an *inclusive* tie set.** The equal-branch at `0x859ed8` (`je`) **ORs** the
> new lane's bit into the running predicate rather than replacing it, so an `rbmax` over a vector with the
> maximum in two lanes returns a `vbool` with **both** bits set — the full equality set, not a one-hot
> first/last argmax. A reimplementer building one-hot argmax on top of `rbmax` must add a priority-encode
> pass. `[HIGH/OBSERVED]` (tie branch disassembled).

> **CORRECTION — the segmented reduce-max leaf calls its fold step `N−1` times, NOT "8 segments".** A call
> census of `module__xdref_rbmax_nx16_64_512_512` @ `0x859f00` shows it calls `module__xdref_rbmax_16`
> **21** times (`objdump … | rg -c 'rbmax_16>'` = 21), not 8. An earlier reading interpreted this as an
> "8-segment fold" implementing `ReduceAxis::Partitions`; the binary does not support a fixed-8 count. The
> count is consistent with a per-step fold over the lane set (the `64_512_512` signature is the bool-bounded
> full-register form), and the on-device segment count for a given `num_active_channels` sits in the
> FLIX-desynced worker — so the "device realises partitions in exactly 8 segments" claim is **withdrawn**;
> the segment count is data-dependent on `num_active_channels`, not a constant. `[HIGH/OBSERVED]` on the
> 21-call census; `[MED/INFERRED]` on the exact device partition-segment wiring.

---

## 6. The device worker — `cross_lane_reduce_impl` / `clr_reduce_local`

`cross_lane_reduce_impl(bool)` @ `0x01000440` (`entry a1, 0x180` — a 384-byte frame for vector spills).
Recovered structurally (the body is FLIX VLIW with interleaved literal pools that desync under stock
`xtensa-elf-objdump`'s linear sweep — the documented native-disasm limitation):

* SP aligned to 64B (`movi a10,-64 ; and a8,a1,a10 ; movsp a1,a8`) for 512-bit vector-register spills.
  `[HIGH/OBSERVED]`
* A counted loop is present: `movi.n a9,17 ; loop a9,0x10005a0` (OBSERVED) — a 17-trip loop, the outer
  walk over the strided src `Tensor4d` / active-channel partitions; `movi.n a14,2` materialises a
  sub-op/dtype constant; many `l32r` to a `.text`-embedded literal pool (the decode/dispatch constants);
  `call0` to internal helpers including `clr_reduce_local`. `[HIGH loop+frame / MED exact bounds]`
* IVP ops surviving the desync in the `0x3f8..0xbc0` region (binary-mode harvest, OBSERVED): a
  select/blend + extract + abs-diff set — `ivp_dselnx16t`, `ivp_dextrprn_2x32`, `ivp_labvdcmprs2nx8_xp`,
  `ivp_minormax2nx8`, `ivp_avgrnx16` (the **AVERAGE** rounding helper), `ivp_babssub*` / `ivp_dmul*`
  (the per-lane scale-multiply for AVERAGE), `ivp_firintnxf16t`. The dedicated `ivp_r*` reduce ops
  themselves sit in the desynced `s3_alu` bundles and are pinned via the ISA encoding tables + the xdref
  oracle (§5), **not** mis-reported as linear-sweep `.byte` spans. `[MED — best correspondence]`

`clr_reduce_local(uint, uchar, NEURON_ISA_TPB_REDUCE_OP, float)` @ `0x0100088c` (`entry a1, 32`) is the
per-(op, dtype) dispatcher. OBSERVED: a chain of FLIX F4-format bundles (`2f 45 …`) comparing a value and
branching — a compiled C++ switch over the `REDUCE_OP` arg, materialising the op codes `0..5` and the
dtype, then issuing the matching `ivp_r*` reduce; the `float` arg is the AVERAGE scale. The switch
structure is OBSERVED; the exact per-arm encoding is FLIX-desynced and recovered structurally (the op set
is the closed 6-entry §4 enum; the primitives are the §5 oracle bodies). `[HIGH switch-structure / MED
exact arms]`

The `.rodata` literal pool (`0x02000000`, `0x1ec` bytes, OBSERVED): two `0x00000010` (=16) run-tables
each terminated by a `0x00000001` marker (a lane-group descriptor), followed by fp constants
(`0x3f800000`=1.0f, `0x3f317215`=ln2, exp/poly coefficient blocks) used by the fp32-math reduce path
(AVERAGE scaling / fp normalization). `[HIGH bytes / MED the "16 = segment" reading]`

> **NOTE — the CrossLaneReduce PERF kernel emits no per-op decode print.** Unlike Tensor-Tensor (which
> prints `num_tensor_elements/alu_op`), the only baked token is the sequencer name `"S: CrossLaneReduce"`.
> It is a leaner kernel: the 6-entry enum is validated by `has_cross_lane_reduce_opcode` +
> `valid_reduce_axis_enum` at *decode* time, before the kernel runs, so there is no error-default string in
> the worker. `[HIGH/OBSERVED — string search confirms no CLR error string exists]`

---

## 7. DType / accumulator + segmented-reduce model

### 7.1 Accumulator width (from the §5 oracle bodies + the header)

* **reduce-ADD widens:** int8→int16 (`radd_2nx8`), int16→int32 (`radd_nx16`). Prevents the 32/64-way sum
  from overflowing the element width. `out_dtype` carries the (wider) accumulator type. (The 16×int32
  form `radd_n_2x32` is the exception — no widen, wraps — §5.1 GOTCHA.) `[HIGH/OBSERVED]`
* **reduce-ADD-SATURATING (`radds`)** does NOT widen the output — it uses a wide (21-bit) intermediate and
  a final sign-and-magnitude 16-bit pack instead. `[HIGH/OBSERVED]`
* **reduce-MAX/MIN do NOT widen** (output width == input width; max/min can't overflow). `[HIGH/OBSERVED]`
* **ADD/AVERAGE/MAX use fp32 math** (header) — integer dtypes are accumulated as fp32 where the header
  says "fp32 math"; the soft-float path provides the fp16/fp32 reductions; the int `radd`/`rmax` oracle
  forms are the integer-engine realisations for the int dtypes.
* **BITWISE_OR/AND/XOR operate on RAW u8/u16/u32 — no fp32 conversion, no widening.** `[HIGH/OBSERVED]`
* `in_dtype`: any valid dtype **except FP32R**. `out_dtype`: any valid dtype **including FP32R** (§3.4).

### 7.2 Segmented / partial reduce — the `reduce_axis` field

`NEURON_ISA_TPB_REDUCE_AXIS` (1-byte packed enum):

* **`ReduceAxis::All` (=0):** *"Reduce along all dimensions → single value out."* The whole src (all lanes,
  all active channels) folds to ONE element. `valid_axis_dst_element_cnt` requires
  `dst.num_elem[0..3] == 1` (or a register-shaped dst). `[HIGH/OBSERVED]`
* **`ReduceAxis::Partitions` (=1):** *"Only reduce across different SBUF/PSUM partitions (no reduction
  within a single partition)."* Reduce **across partitions only**; dst shape == src shape
  (`same_element_count_t4d(dst, src)`). Lane `i` of every partition folds into lane `i` of the output — a
  partition-axis reduce that **preserves** the within-partition lane layout. `[HIGH/OBSERVED]`

The validity predicate from the header:

```c
// valid_axis_dst_element_cnt(i):
//    (reduce_axis == All  && (dst is register-shaped || dst.num_elem[0..3] all == 1))
// || (reduce_axis == Partitions && same_element_count_t4d(dst, src))
```

On-device, the segment is bounded by the predicated `rb*` / `_t` reduce forms (§5.5): an active-lane /
active-partition `vbool` mask bounds the fold to the requested segment. `num_active_channels` (off 34) sets
how many partitions participate; the worker partitions the channels across POOL cores by `get_cpu_id()`.
`[HIGH axis semantics / MED exact device mask wiring]`

### 7.3 Output shape (scalar vs per-lane vector)

* `ReduceAxis::All` → a **single scalar** element written to dst (dst is 1 element). It is **not**
  broadcast back to all 32 lanes by this instruction — the consumer reads the one element. `[HIGH/OBSERVED]`
* `ReduceAxis::Partitions` → a **per-lane vector** (one reduced value per within-partition lane position),
  dst same shape as src. `[HIGH/OBSERVED]`

> **GOTCHA — `ReduceAxis::All` writes ONE element, not a broadcast.** A reimplementer who expects the
> reduced scalar replicated across all lanes (as some SIMD ISAs do for `reduce_add`) will read garbage from
> the other lanes. To broadcast the reduced scalar back to a full vector you must follow with a `rep`/
> `splat` ([B16](../../isa/ref/b08-reduce.md) cross-links the replicate batch). `[MED/INFERRED]`

---

## 8. Reconciliation with the other reduce layers

There are **three** distinct reduce layers in the Neuron stack; CrossLaneReduce is the innermost (lane)
leg. They share abstract operation **names** (ADD/MAX/MIN) but each carries its own packed enum and folds
over a different axis. `[HIGH/OBSERVED]`

| | axis folded | engine / opcodes | op enum | this kernel? |
|---|---|---|---|---|
| **(1) intra-vector / cross-lane** | the 32 (or 64/16) SIMD **lanes** of one register | **POOL `0x7c`/`0x7d`**, `ivp_r*` ops | `NEURON_ISA_TPB_REDUCE_OP` (6 ops) | **YES — this page** |
| **(2) cross-partition** | SBUF/PSUM **partitions** | SEQ Tensor-Reduce `'F'`=0x46 / `'G'`=0x47 / `'R'`=0x52 | (Tensor-Reduce datapath) | no (distinct engine) |
| **(3) cross-buffer / cross-core** | DMA **buffers** and **cores** | SDMA CCE (collective all-reduce) | `SDMA_CCETYPE {ADD0, FMA1, MAX2, MIN3}` | no (collective layer) |

**Op-enum reconciliation** — the three vocabularies do **not** share encoding:

```
NEURON_ISA_TPB_REDUCE_OP (CrossLaneReduce):  ADD=0 AVERAGE=1 MAX=2 OR=3 AND=4 XOR=5   (6 ops)
SDMA_CCETYPE / CCE_OP     (SDMA collective):  ADD=0 FMA=1     MAX=2 MIN=3              (+EXT/GCE)
NEURON_ISA_TPB_ALU_OP     (Tensor-Tensor):    ADD=0x04 MAX=0x08 MIN=0x09 …            (60 ops)
```

They **agree only on the abstract names** (ADD/MAX/MIN), each in its own packed enum. The **value
semantics are the same ground-truth element primitives** (`libfiss-base` `xdref add`/`max`/`min`): the
cross-lane fold and the cross-core fold reduce with byte-identical per-element math, differing only in
**what** they fold over (lanes vs partitions vs buffers) and the carrier enum. `[HIGH/OBSERVED]`

> **QUIRK — `ReduceAxis::Partitions` overlaps the Tensor-Reduce engine conceptually, but is the POOL-engine
> implementation.** CrossLaneReduce's `Partitions` mode reduces across partitions *within the POOL kernel*;
> the dedicated `'F'`/`'G'`/`'R'` Tensor-Reduce opcodes are a *separate* datapath for the same conceptual
> operation. They are distinct kernels: CrossLaneReduce's worker is `cross_lane_reduce_impl` @ `0x440`; the
> Tensor-Reduce impl lives elsewhere. Do not assume one calls the other. `[HIGH/OBSERVED]`

**Composition.** A full reduce-scatter / all-reduce lowers as **lane-fold** (CrossLaneReduce) →
**partition-fold** (Tensor-Reduce) → **ring/mesh buffer-fold** (SDMA CCE), each layer associative so the
global order is free — exactly the header's "associative/commutative, order not architecturally
guaranteed". `[HIGH/INFERRED-composition]`

---

## 9. Cross-image / cross-gen stability

* The handler chain (`pool_cross_lane_reduce_arith` @ `0x3f8`, `_bitvec` @ `0x410`, the worker @ `0x440`,
  `clr_reduce_local` @ `0x88c`) sits at the front of the `0xa260` `EXTISA_0` image, shared
  **CAYMAN / MARIANA / MARIANA_PLUS** (the three generations that ship the `0xa260` image; the MARIANA
  images are byte-identical to MARIANA_PLUS, CAYMAN differs only by a small build delta). `[HIGH/OBSERVED]`
* **SUNDA** (newest gen) carries the same two opcodes in its `all.stripped.so` JSON map (124/125) — opcode
  numbering stable across generations. `[HIGH for op #]`
* The S4D4_CR struct + `REDUCE_OP`/`REDUCE_AXIS` enums + the `0x7c`/`0x7d` opcodes are **byte-identical
  across the CAYMAN, MARIANA, SUNDA, and MAVERICK arch-isa headers** (verified: same six enum values, same
  64B struct, same behavior comment, same opcode bytes). `[HIGH/OBSERVED]`

> **NOTE — v5 / MAVERICK.** The MAVERICK (NC-v5) arch-isa header exposes the identical `REDUCE_OP` enum
> and `0x7c`/`0x7d` opcodes (header-OBSERVED), but the v5 kernel *interior* (the worker body) was not
> byte-grounded this pass — the v2–v4 (cayman/mariana/sunda) device bodies are the byte-verified substrate;
> the v5 interior is `[INFERRED]` from header + opcode-map parity. `[HIGH header / INFERRED interior]`

---

## 10. Adversarial self-verification — five strongest claims re-challenged

1. **"Dispatch is two opcodes `0x7c`/`0x7d` into one worker via a `bool` selector."** Re-challenged: the
   two trampolines @ `0x3f8`/`0x410` are byte-identical except the `a0`/`a1` selector byte; `common.h` has
   `CROSS_LANE_REDUCE_ARITH=0x7c` / `_BITVEC=0x7d`; `instruction_mapping.json` binds **both** to the one
   `S4D4_CR_STRUCT`; the worker symbol is `cross_lane_reduce_impl(bool)`. Identical opcodes in
   cayman/sunda/maverick. **Holds.** `[HIGH/OBSERVED]`
2. **"The S4D4_CR struct is 64 bytes with `reduce_op`@35, `reduce_axis`@36, `scale`@40."** Re-challenged
   against the header: `ISA_STATIC_ASSERT(sizeof == 64)`, field offsets sum exactly
   (`4+8+20+1+1+1+1+1+3+4+20 = 64`), `TENSOR4D` = `ADDR4(4) + int16[4](8) + uint16[4](8) = 20`. **Holds.**
   `[HIGH/OBSERVED]`
3. **"`REDUCE_OP = {ADD=0, AVERAGE=1, MAX=2, BITWISE_OR=3, AND=4, XOR=5}`, a closed 6-entry enum."**
   Re-challenged: read byte-exact from `aws_neuron_isa_tpb_s4d4_cr.h`, identical across all four gens; the
   enum closes at `0x5`. **Holds.** `[HIGH/OBSERVED]`
4. **"The `reduce_op → ivp_r*` mapping, confirmed by live xdref execution."** Re-challenged by driving
   eleven leaves live: `radd` exact/widening (`radd(32×32767)=1048544`), `radds` packed-sat (`0x7FFF`/
   `0x8000`, not int16 clamp), `rmax`/`rmaxu` diverge signed/unsigned (`+1` vs `0xFFFF`), `randbn`/`rorbn`
   bitwise folds, `ltrn` tail-predicate, the width grid (8→16, 16→32, 32→32-wraps). **Holds — and corrects
   the `rxorb` name** (no dedicated reduce leaf; only the `xorb` element prim). `[HIGH/OBSERVED by
   execution]`
5. **"The intra-lane vs cross-partition vs cross-core distinction."** Re-challenged: `REDUCE_OP` (6 ops,
   POOL) ≠ `SDMA_CCETYPE` (4 ops, +FMA, collective) ≠ `ALU_OP` (60 ops, Tensor-Tensor); the SEQ hub carries
   a *separate* `'F'`/`'G'`/`'R'` Tensor-Reduce family with its own DRAM string. Three disjoint enums, three
   axes. **Holds.** `[HIGH/OBSERVED]`

**Flagged / corrected items.** (i) **`BITWISE_XOR` value primitive** — no `module__xdref_rxorb*` reduce
leaf exists (`nm | rg -c` = 0); only `xorb_64_64_64` (element prim). The reduce-XOR is realised by chaining
the element op; the `ivp_rxorb*` name is `[MED/INFERRED]`. (ii) **The "8-segment" partition claim is
WITHDRAWN** — `rbmax_nx16_64_512_512` calls `rbmax_16` **21** times, not 8; the device segment count is
data-dependent on `num_active_channels`, not a constant (`[HIGH/OBSERVED]` census, `[MED/INFERRED]` device
wiring). (iii) **fp `num` (NaN-skip)** semantics read from the leaf tail-call to `function__fp_hp_rmaxnum_32`,
not sweep-driven — `[MED/INFERRED]` on the exact NaN/−0 corner. (iv) **The worker mid-bundle decode** is
FLIX-desynced — `[HIGH]` on the entry/loop/selector skeleton, `[MED]` on the exact dispatch arms. (v) **v5
interior** is header-OBSERVED / interior-INFERRED (§9).

---

## 11. Cross-references

* [ISA Batch 08 — Cross-Lane Reduce](../../isa/ref/b08-reduce.md) — the per-instruction reference for the
  `ivp_r*` reduce mnemonics this kernel issues: `radd`/`rmax`/`rmin`/`rbmax`/`rbmin` (the `s3_alu`
  arithmetic reduces) and `ltr*`/`randb*`/`rorb*` (the `s1_ld` load-slot fold/tail forms), the `word0`
  selector bands, and the live-driven value semantics §4–§5 here re-use.
* [Tensor-Reduce kernel](tensor-reduce.md) *(planned)* — the **cross-PARTITION** reduce engine (SEQ
  opcodes `0x46`/`0x47`/`0x52`), the second tier of the §8 composition; distinct from this POOL kernel.
* The **SDMA-CCE collective reduce** (CCL-03, `SDMA_CCETYPE {ADD0, FMA1, MAX2, MIN3}`) — the
  **cross-CORE / cross-BUFFER** outermost tier of the §8 composition.
* [The Tensor-Tensor kernel](tensor-tensor.md) *(if present)* — the structurally-parallel arith/bitvec
  opcode split (`NEURON_ISA_TPB_ALU_OP`, 60 ops) the §8 op-enum reconciliation contrasts against.

---

*Provenance: opcodes, the S4D4_CR struct, and the `REDUCE_OP`/`REDUCE_AXIS` enums are read from the
CAYMAN/MARIANA/SUNDA/MAVERICK arch-isa interface headers (`aws_neuron_isa_tpb_s4d4_cr.h`,
`aws_neuron_isa_tpb_common.h`) shipped in the customop-lib package and cross-checked against
`instruction_mapping.json` (struct2opcode) and the SUNDA opcode JSON. The handler chain VMAs / prologues
are read from the `.xt.prop` function-start records of the carved CAYMAN device image with
`xtensa-elf-objdump`/`readelf`/`c++filt` (`XTENSA_CORE=ncore2gp`). The reduce algorithm, accumulator
widening, saturation, and per-op value semantics in §4–§5 are recovered byte-exact from the
`ncore2gp/config/libfiss-base.so` ISS oracle — `.text` VMA==file-offset, `.data.rel.ro` file = VMA −
`0x200000` (`readelf -SW`-confirmed) — and **driven live via `ctypes`** (`radd`/`raddu`/`radds`,
`rmax`/`rmaxu`/`rmin`, `randbn`/`rorbn`, `ltrn`, the width grid). Counts via `nm -D | rg -c`. The
`extracted/` tree is gitignored (reached with absolute paths). All prose is derived from static analysis
and in-process execution of the shipped artifacts only; nothing here is read from a vendor source tree.*
