# Tensor-Reduce (cross-partition) — `clr_reduce_local` / the `REDUCE_OP` family

The **cross-partition Tensor-Reduce** is the GPSIMD POOL-engine reduction that folds the values **across
SBUF/PSUM partitions** — the middle tier of the three-layer Neuron reduce trilogy (intra-vector lane fold
→ **cross-partition** → cross-core SDMA-CCE). On the GpSimd Vision-Q7 core this is *physically the same
kernel* the [CrossLaneReduce page](cross-lane-reduce.md) decodes as the intra-register lane fold: the
partition axis is mapped onto the SIMD lanes of the 512-bit vector register, so a single `ivp_r*` lane
fold collapses across partitions. It is reached from the NKI compiler as `nki.isa.tensor_partition_reduce`
("*across partitions … GpSimd Engine*"), lowers through `emit_cross_lane_reduce_arith/_bitvec`, and binds
to GPSIMD POOL opcodes **`0x7c`/`0x7d`** — `NEURON_ISA_TPB_S4D4_CR_STRUCT`, the closed 6-entry
`NEURON_ISA_TPB_REDUCE_OP` enum, and the device worker `clr_reduce_local`.

Everything below is derived from static analysis of the shipped GPSIMD device images
(`extisa_*_POOL_PERF_EXTISA_*.so`), the shipped CAYMAN/MARIANA/SUNDA/MAVERICK arch-isa interface headers
(`aws_neuron_isa_tpb_s4d4_cr.h`, `aws_neuron_isa_tpb_common.h`, `instruction_mapping.json`) and the
`libfiss-base.so` ISS oracle, read with the Cadence `xtensa-elf-objdump`/`readelf` (`XTENSA_CORE=ncore2gp`)
and stock binutils/`gcc`/`ctypes`. Every claim carries a confidence tag `HIGH/MED/LOW × OBSERVED/INFERRED/
CARRIED`.

---

## 1. Executive summary — the arbitration up front

> **CORRECTION — the cross-partition Tensor-Reduce is *not* a distinct GPSIMD kernel, and the
> `0x46`/`0x47`/`0x52` opcode attribution from [CrossLaneReduce §2/§8](cross-lane-reduce.md) is *wrong*.**
> The opcode enum in `aws_neuron_isa_tpb_common.h` (read byte-exact) maps `0x46 → COPY`,
> `0x47 → CAST`, and `0x52 → TENSOR_REDUCE_BITVEC_OP`. The `0x52` *is* a real "Tensor-Reduce" opcode — the
> **bitvec half of the `S4D4_TR` Tensor-Reduce** (arith half `0x42`) — but that instruction runs on the
> **DVE (Vector) / Pool** engines, **not** the GPSIMD cross-lane datapath (its engine predicate forbids
> GpSimd; §6). The GPSIMD cross-**partition** reduce reachable from NKI is **`tensor_partition_reduce` →
> `0x7c`/`0x7d` → `clr_reduce_local`, full stop.** `0x46`/`0x47` are copy/cast, never a reduce. *(For the
> reconcile pass: §2 corrects the cross-lane-reduce.md §2 NOTE and §8 table — the cross-partition leg is
> `0x7c`/`0x7d` (S4D4_CR), and the genuinely-distinct "free-axis" middle reduce is `0x42`/`0x52` (S4D4_TR)
> on Vector/Pool, which is **absent from the GPSIMD POOL ucode**.)* `[HIGH/OBSERVED]`

The dispatch is **two POOL opcodes** sharing **one 64-byte operand struct**
(`NEURON_ISA_TPB_S4D4_CR_STRUCT`) and **one reduce-op enum** (`NEURON_ISA_TPB_REDUCE_OP`), routed into a
single worker:

| opcode | name | `kernel_info` idx | funcVA | entry symbol | NKI sub-path |
|---|---|---|---|---|---|
| **`0x7c`** (124) | `CrossLaneReduceArith` | idx1 | `0x010003f8` | `pool_cross_lane_reduce_arith()` | `tensor_partition_reduce_arith` |
| **`0x7d`** (125) | `CrossLaneReduceBitvec` | idx2 | `0x01000410` | `pool_cross_lane_reduce_bitvec()` | `tensor_partition_reduce_bitvec` |

Both trampolines tail into **one** worker `cross_lane_reduce_impl(bool)` @ `0x01000440` — the `bool`
selecting arith (`0x7c`, `bool=0`) vs bitvec (`0x7d`, `bool=1`). The worker decodes the `S4D4_CR` struct,
reads the 1-byte `reduce_op`, the 1-byte `reduce_axis`, the in/out dtypes, `num_active_channels`, and the
fp32 `scale`; it walks the strided 4-D src `Tensor4d` over the active partitions and dispatches the
per-`(op, dtype)` reduce through `clr_reduce_local(unsigned int, unsigned char,
NEURON_ISA_TPB_REDUCE_OP, float)` @ `0x0100088c` — **the function whose mangled name literally carries the
`REDUCE_OP` selector and the fp32 `scale`** (`_Z16clr_reduce_localjh24NEURON_ISA_TPB_REDUCE_OPf`).

The **reduce-op table** is the closed 6-entry `NEURON_ISA_TPB_REDUCE_OP` —
**`ADD=0, AVERAGE=1, MAX=2, BITWISE_OR=3, BITWISE_AND=4, BITWISE_XOR=5`** — *byte-identical* to the
[CrossLaneReduce §4 enum](cross-lane-reduce.md) and *byte-identical across all four gens* (§9). The
algorithm is the single-instruction `ivp_r*` lane fold (the [B08 reduce ISA batch](../../isa/ref/b08-reduce.md));
`reduce_axis ∈ {All, Partitions}` is the cross-partition geometry (§5).

> **The NKI binding (the proof this == CrossLaneReduce).** The aws-neuronx NKI compiler exposes
> `nki.isa.tensor_partition_reduce(dst, op, data)` — docstring *"Apply a reduction operation across
> partitions of an input data tile using GpSimd Engine. op: the reduction operator (add, max, bitwise_or,
> bitwise_and)"*. It dispatches `is_bitvec_op(op) ? tensor_partition_reduce_bitvec :
> tensor_partition_reduce_arith`, which lower through `emit_cross_lane_reduce_arith(reduce_op, scale)` /
> `emit_cross_lane_reduce_bitvec(reduce_op)` to the **`cross_lane_reduce`** MLIR op → device `0x7c`/`0x7d`.
> The `scale` F32 on the arith path (absent on bitvec) maps 1:1 to `clr_reduce_local`'s 4th float arg and
> `S4D4_CR.scale@40` (the AVERAGE multiplier). So *"across partitions, GpSimd"* (NKI) and *"fold the 32
> SIMD lanes"* (the device) are **one kernel, two altitudes** — the partition dim *is* the lane axis.

---

## 2. The dispatch chain — byte-exact

### 2.1 The opcode arbitration (the `0x46`/`0x47`/`0x52` question, resolved)

The full reduce-family slice of the opcode enum (`aws_neuron_isa_tpb_common.h`, CAYMAN, read byte-exact;
identical across all four gens, §9):

| opcode | name | operand struct | engine | reduce? |
|---|---|---|---|---|
| `0x42` | `TENSOR_REDUCE_ARITH_OP` | `S4D4_TR` | **DVE / Pool** | free-axis (middle tier, §6) |
| `0x46` | `COPY` | `S4D4_TR` | — | **no — copy** |
| `0x47` | `CAST` | `S4D4_TR` | — | **no — cast** |
| `0x4E` | `TENSOR_CUMULATIVE_ARITH_OP` | `S4D4_TR` | DVE / Pool | scan (the [TensorCumulative](tensorcumulative.md) twin) |
| `0x52` | `TENSOR_REDUCE_BITVEC_OP` | `S4D4_TR` | **DVE / Pool** | free-axis bitvec (middle tier, §6) |
| `0x5E` | `TENSOR_CUMULATIVE_BITVEC_OP` | `S4D4_TR` | DVE / Pool | scan bitvec |
| **`0x7c`** | **`CROSS_LANE_REDUCE_ARITH`** | **`S4D4_CR`** | **GpSimd (POOL ucode)** | **cross-partition (THIS page)** |
| **`0x7d`** | **`CROSS_LANE_REDUCE_BITVEC`** | **`S4D4_CR`** | **GpSimd (POOL ucode)** | **cross-partition (THIS page)** |
| `0x83` | `TRANSPOSE_TENSOR_REDUCE_ARITH_OP` | `S4D4_TR` | DVE / Pool | transpose-then-reduce |
| `0x84` | `TRANSPOSE_TENSOR_REDUCE_BITVEC_OP` | `S4D4_TR` | DVE / Pool | transpose-then-reduce bitvec |
| `0x9a` | `TENSOR_SCALAR_CACHE_REDUCE` | (TSC) | — | scalar-cache reduce |
| `0xea` | `SELECT_REDUCE` | — | — | select-reduce |

> **CORRECTION (in-page, for the reconcile).** The [cross-lane-reduce.md §2 NOTE / §8 table](cross-lane-reduce.md)
> stated the cross-partition Tensor-Reduce is *"SEQ opcodes `0x46`/`0x47`/`0x52`"*. This is wrong on two
> counts, both byte-grounded above: (a) `0x46`/`0x47` are `COPY`/`CAST` (not reduces at all); (b) `0x52`
> *is* a Tensor-Reduce opcode but it is `TENSOR_REDUCE_BITVEC_OP` (paired with arith `0x42`), bound to the
> **`S4D4_TR`** struct on the **DVE/Pool** engines — a *separate* datapath from the GPSIMD `S4D4_CR`
> cross-lane reduce. The cross-partition reduce **on GpSimd** is `0x7c`/`0x7d`. `instruction_mapping.json`
> seals it: `S4D4_CR_STRUCT → {CROSS_LANE_REDUCE_ARITH, CROSS_LANE_REDUCE_BITVEC}` (two opcodes),
> `S4D4_TR_STRUCT → {TENSOR_REDUCE_ARITH/BITVEC, TRANSPOSE_TENSOR_REDUCE_ARITH/BITVEC,
> TENSOR_CUMULATIVE_ARITH/BITVEC, COPY, CAST, …}` (nine). Two struct families, no overlap.
> `[HIGH/OBSERVED]`

The device disassembler carries **two distinct mnemonic-name strings**, byte-anchored in
`libnrtucode_internal.so` `.rodata` — the cleanest single proof that Tensor-Reduce and CrossLaneReduce are
*two different instructions*:

```
0x18d23b   "S: Tensor-Reduce\n"      (the S4D4_TR free-axis reduce — 0x42/0x52; DVE/Pool)
0x1cfcf4   "S: CrossLaneReduce\n"    (the S4D4_CR cross-partition reduce — 0x7c/0x7d; THIS GPSIMD kernel)
0x269bf6   "P%i: Error, unimplemented tensor-reduce ALU op.\n"  (Tensor-Reduce's *ALU op* error path)
```

The `"unimplemented tensor-reduce **ALU op**"` error string (off `0x269bf6`) independently confirms that
Tensor-Reduce switches on the **`ALU_OP`** space (the `S4D4_TR.op@36` field, §6) — *not* the 6-entry
`REDUCE_OP` of this kernel.

### 2.2 `.xt.prop` function starts (CAYMAN_0)

The handler chain sits at the front of the `0xa260` `EXTISA_0` POOL image, shared CAYMAN/MARIANA/
MARIANA_PLUS. Function-start VMAs are the first 12-byte record (`[VMA:LE32][size][flag:0x2804]`) of each
`.xt.prop.<mangled>` section, demangled with `c++filt`:

| funcVA | demangled signature | role |
|---|---|---|
| `0x010003f8` | `pool_cross_lane_reduce_arith()` | ARITH entry (op `0x7c`) |
| `0x01000410` | `pool_cross_lane_reduce_bitvec()` | BITVEC entry (op `0x7d`) |
| `0x01000440` | `cross_lane_reduce_impl(bool)` | the shared worker |
| `0x0100088c` | `clr_reduce_local(unsigned int, unsigned char, NEURON_ISA_TPB_REDUCE_OP, float)` | per-`(op,dtype)` dispatcher |

The carved POOL `EXTISA` images ship as **data blobs embedded inside `libnrtucode_internal.so`** (mirrored
into `libnrtucode.so`), each generation exposing four blobs (`{CAYMAN_NX,CAYMAN_Q7,MARIANA_Q7,MAVERICK_Q7,
PLUS_Q7}_POOL_PERF_EXTISA_{0,1,2,3}` accessor functions). The four reduce/clr `.xt.prop` section-name
strings carved from each blob are exactly:

```
.xt.prop._Z28pool_cross_lane_reduce_arithv    -> pool_cross_lane_reduce_arith()
.xt.prop._Z29pool_cross_lane_reduce_bitvecv   -> pool_cross_lane_reduce_bitvec()
.xt.prop._Z22cross_lane_reduce_implb          -> cross_lane_reduce_impl(bool)
.xt.prop._Z16clr_reduce_localjh24NEURON_ISA_TPB_REDUCE_OPf  -> clr_reduce_local(uint, uchar, REDUCE_OP, float)
```

read at `.rodata` offsets `0x12aa73`/`0x12aa9e`/`0x12aaca`/`0x12aaef` (first blob) in `libnrtucode.so` —
**each symbol repeats 9 times** (= 9 embedded POOL EXTISA blobs per lib), with the `.kernel_info_table`
section name immediately preceding each cluster (e.g. `0x12aa0b kernel_info_table`). The reduce-related
`.xt.prop` set is exactly `{pool_cross_lane_reduce_arith, pool_cross_lane_reduce_bitvec,
cross_lane_reduce_impl, clr_reduce_local}` — **no `decode_tensor_reduce`, no `*_RD` worker, no
`pool_reduce`**. (The strings `pool_tensor_reduce` / `tensor_reduce.hpp` exist at `0x133f65`/`0x133f78` but
are source-file/assert labels in `.rodata`, *not* `.xt.prop` section names.) The carved GPSIMD ucode
contains **only** the CrossLaneReduce family; the `S4D4_TR` Tensor-Reduce (`0x42`/`0x52`) is *not* a GPSIMD
kernel and never appears in the POOL `.xt.prop`. The mangled `clr_reduce_local` signature is the **only**
place `NEURON_ISA_TPB_REDUCE_OP` appears as a parameter type — concrete evidence the firmware worker takes
the `S4D4_CR.reduce_op` enum.

The kernel_info_table (CAYMAN_0, VMA `0x02000380`, file `0x7400`; record = `{u8 0; u8 0; u8 spec; u8
opcode; u32_le funcVA}`):

```
xxd -s 0x7408 -l 16 :
   0000 007c | f803 0001    idx1  opcode 0x7c (124)  funcVA 0x010003f8   pool_cross_lane_reduce_arith
   0000 007d | 1004 0001    idx2  opcode 0x7d (125)  funcVA 0x01000410   pool_cross_lane_reduce_bitvec
```

Lookup is a linear scan matching the packed `(spec, opcode)` key, then `callx8 funcVA`. The opcode
numbering is **stable across generations**: the SUNDA opcode→pool-function JSON lists
`pool_cross_lane_reduce_arith @ 124` / `pool_cross_lane_reduce_bitvec @ 125` — byte-identical to CAYMAN.
`[HIGH/OBSERVED for op#]`

### 2.3 The two trampolines and the worker prologue

```
0x010003f8: 36 41 00          entry a1, 32          ; pool_cross_lane_reduce_arith (thin trampoline)
0x01000410: 36 41 00          entry a1, 32          ; pool_cross_lane_reduce_bitvec (thin trampoline)
0x01000440: 36 01 03 ...      entry a1, 0x180       ; cross_lane_reduce_impl — the big worker
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

`[HIGH/OBSERVED]` on the entry/selector/`retw` skeleton; `[MED/INFERRED]` on the mid-bundle route — the
body is dense FLIX VLIW (the `2f` is the F-format selector byte) and desyncs under stock objdump's linear
sweep, the documented native-disasm limitation (§10).

---

## 3. The operand struct — `NEURON_ISA_TPB_S4D4_CR_STRUCT` (64 bytes, compile-verified)

Source: `aws_neuron_isa_tpb_s4d4_cr.h` (CAYMAN, "ISA header for NC-v3"). The header's own format comment:
*"Neuron 'S4D4_cr' Format — one 4d SRC Tensor, one 4d DST Tensor. Use for: CrossLaneReduce"*
(**S4D4_CR** = one 4-D src, one 4-D dst, **C**ross-**R**educe). `instruction_mapping.json` binds the struct
to **both** opcodes:

```json
"NEURON_ISA_TPB_S4D4_CR_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_CROSS_LANE_REDUCE_ARITH",
    "NEURON_ISA_TPB_OPCODE_CROSS_LANE_REDUCE_BITVEC"
]
```

> **The `sizeof==64` static-assert is *compile-verified*.**
> The arch-isa header *is* present in this checkout. Reproducing the header's `typedef struct
> NEURON_ISA_TPB_S4D4_CR_STRUCT` (with `NEURON_ISA_PACKED = __attribute__((packed))` and the 1-byte packed
> `REDUCE_OP`/`REDUCE_AXIS` enums), `gcc` confirms **`sizeof == 64`** and every offset:
> `header@0 events@4 src@12 in_dtype@32 out_dtype@33 num_active_channels@34 reduce_op@35 reduce_axis@36
> reserved0@37 scale@40 dst@44`, with `sizeof(TENSOR4D) == 20` and `sizeof(REDUCE_OP) == 1`. The header's
> `ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_S4D4_CR_STRUCT) == 64, …)` holds, offsets byte-exact.
> `[HIGH/OBSERVED — compile-verified]`

### 3.1 Field layout

| off | size | field | type | notes |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode:1 (0x7c\|0x7d), inst_word_len:1, debug_cmd:1, debug_hint:1}` |
| 4–11 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12–31 | 20 | `src_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | INPUT (4-D strided; `start_addr` = SBUF partition-offset spanning `num_active_channels`) |
| 32 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` | source element dtype (may **not** be `FP32R`) |
| 33 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | destination/accumulator dtype (`FP32R` allowed) |
| 34 | 1 | `num_active_channels` | `uint8_t` | partition/channel count to fold over |
| **35** | 1 | **`reduce_op`** | `NEURON_ISA_TPB_REDUCE_OP` | **the §4 reduce-op selector (the `clr_reduce_local` 3rd arg)** |
| **36** | 1 | **`reduce_axis`** | `NEURON_ISA_TPB_REDUCE_AXIS` | **`All(0)` / `Partitions(1)` — the geometry (§5)** |
| 37–39 | 3 | `reserved0[3]` | `uint8_t[3]` | must be zero (`s4d4_cr_reserved_zero`) |
| **40–43** | 4 | **`scale`** | `float` (fp32) | **the AVERAGE multiplier** (`clr_reduce_local` 4th arg) |
| 44–63 | 20 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | OUTPUT (4-D strided; `All`→1 partition) |

`4 + 8 + 20 + 1 + 1 + 1 + 1 + 1 + 3 + 4 + 20 = 64`. `[HIGH/OBSERVED — compile-verified]`

The device mangled name `_Z16clr_reduce_localjh24NEURON_ISA_TPB_REDUCE_OPf` independently witnesses three
struct fields: the 1-byte `REDUCE_OP` (off 35) and the fp32 `scale` (off 40), with the leading
`unsigned int` = lane-base/descriptor and `unsigned char` = dtype/channel. The NKI
`emit_cross_lane_reduce_arith(dst, src, reduce_op, scale, num_r_dim)` carries the same fields. So even
without the header the field identities triangulate; with the header, the offsets are now byte-exact.

### 3.2 `NEURON_ISA_TPB_TENSOR4D` (20 bytes)

```c
typedef struct NEURON_ISA_TPB_TENSOR4D {
    NEURON_ISA_TPB_ADDR4 start_addr;    //  4  (union: addr_immediate | addr_reg; markers IMM=0x00,
                                        //      ADDR_REG=0x80, SHAPE_REG=0x40, INDIRECT_IMM=0x20, …)
    int16_t              step_elem[4];  //  8  per-dim signed element stride
    uint16_t             num_elem[4];   //  8  per-dim element count
} NEURON_ISA_TPB_TENSOR4D;             // = 4 + 8 + 8 = 20  (compile-verified)
```

> **NOTE — CrossLaneReduce uses a *4-D* access pattern (`TENSOR4D`).** The `start_addr` `ADDR4` union
> carries the SBUF partition-offset encoding; the src spans `num_active_channels` partitions, the dst
> spans exactly 1 partition for `All` (the reduce collapses partitions to one) and matches src for
> `Partitions`.

### 3.3 Validity predicates (the header's assertion comments, OBSERVED verbatim)

```c
// fn is_valid_cross_lane_reduce(i) -> bool {
//       has_valid_neuron_header(i) && has_valid_neuron_events(i)
//    && has_cross_lane_reduce_opcode(i)                                   // opcode ∈ {0x7c, 0x7d}
//    && s4d4_cr_reserved_zero(i)                                          // reserved0[0..2] == 0
//    && is_valid_dtype(i.s4d4_cr.in_dtype,  DtypeAllowFP32R::False)       // src MAY NOT be fp32r
//    && is_valid_dtype(i.s4d4_cr.out_dtype, DtypeAllowFP32R::True)        // dst MAY be fp32r
//    && start_addr_active_channels(i.s4d4_cr.src_mem_pattern.start_addr, i.s4d4_cr.num_active_channels)
//    && start_addr_active_channels(i.s4d4_cr.dst_mem_pattern.start_addr, 1)
//    && check_active_channels(i.s4d4_cr.num_active_channels)
//    && tensor4d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::False, AllowedInSBUF::True)  // src in SBUF, RO
//    && tensor4d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::False, AllowedInSBUF::True)  // dst in SBUF, written
//    && valid_reduce_axis_enum(i)                                         // reduce_axis ∈ {All, Partitions}
//    && valid_axis_dst_element_cnt(i)                                     // §5
// }
```

> **GOTCHA — `in_dtype` may NOT be `FP32R`, but `out_dtype` MAY be.** The src is read with
> `DtypeAllowFP32R::False`; the accumulator/dest is read with `DtypeAllowFP32R::True`. A reimplementer who
> allows the round-mode `FP32R` (`0xB`) on the *input* side will accept an instruction the hardware decoder
> rejects. Both src and dst must live in **SBUF** (`AllowedInPSUM::False`); the src is read-only, the dst is
> written. `[HIGH/OBSERVED]`

---

## 4. The reduce-op enum — `NEURON_ISA_TPB_REDUCE_OP` (closed, 6 entries)

Source: `aws_neuron_isa_tpb_s4d4_cr.h`, `enum NEURON_ISA_TPB_REDUCE_OP`, `NEURON_ISA_PACKED` (1 byte). This
is the **complete, closed** enumeration of every value `s4d4_cr.reduce_op` can hold and the worker switches
on. It is the dedicated *reduce* enum — **not** the 60-entry `NEURON_ISA_TPB_ALU_OP` of Tensor-Tensor, and
crucially **not** the `op@36` of the `S4D4_TR` Tensor-Reduce (which *is* `ALU_OP`, §6). Read byte-exact
and matching the [CrossLaneReduce §4 enum](cross-lane-reduce.md) exactly:

| code | name | handler | header semantics (verbatim) | value primitive (xdref / ivp) | conf |
|---|---|---|---|---|---|
| `0` | `ADD` | arith (`0x7c`) | *"add all elements, return single result. fp32 math."* | `radd*` (widening) / soft-float `add_*_32f` | HIGH(enum)/HIGH(sem) |
| `1` | `AVERAGE` | arith (`0x7c`) | *"multiply each element by scale field (fp32), then add them."* | per-lane `×scale` then `radd` (scale @ off 40) | HIGH(enum)/HIGH(sem) |
| `2` | `MAX` | arith (`0x7c`) | *"find the max element (fp32 math)"* | `rmax*` / `rmaxnum*` (NaN-suppress fp) | HIGH(enum)/HIGH(sem) |
| `3` | `BITWISE_OR` | bitvec (`0x7d`) | *"bitwise OR of all elements (raw u8/u16/u32, no fp32 conversion)"* | `rorbn*` (full-vector OR fold) | HIGH(enum)/HIGH(sem) |
| `4` | `BITWISE_AND` | bitvec (`0x7d`) | *"bitwise AND of all elements (raw …, no fp conv)"* | `randbn*` (full-vector AND fold) | HIGH(enum)/HIGH(sem) |
| `5` | `BITWISE_XOR` | bitvec (`0x7d`) | *"bitwise XOR of all elements (raw …, no fp conv)"* | XOR-fold (no dedicated `r`-prefix leaf — see below) | HIGH(enum)/MED(prim) |

The enum closes at `0x5`; total = 6. The **arith** handler (`0x7c`) takes `ADD`/`AVERAGE`/`MAX` (value
reductions, fp32 math); the **bitvec** handler (`0x7d`) takes `BITWISE_OR`/`AND`/`XOR` (raw integer
bit-fold, no fp conversion) — the exact arith-vs-bitvec partition that motivates the two opcodes. The enum
is **byte-identical across the CAYMAN, MARIANA, SUNDA, and MAVERICK arch-isa headers** (verified:
same six values, same packing, §9).

> **The header's associativity license (verbatim, OBSERVED):** *"The operators are all
> associative/commutative and the order in which the operations is done is not architecturally
> guaranteed."* This is the architectural permission for the device to use a **balanced log-step tree**
> (32 → 16 → 8 → 4 → 2 → 1) or any lane-pairing; the result is order-independent. It is also what makes the
> three-tier composition (§7) order-free.

> **CORRECTION — `BITWISE_XOR` has no dedicated `module__xdref_rxorb*` reduce leaf** (`nm -D libfiss-base.so
> | rg -c 'xdref_rxorb'` = 0). `BITWISE_OR`/`AND` *do* (`rorbn_64_64` @ `0x81ce30`, `randbn_64_64` @
> `0x81cc90`); `BITWISE_XOR` is realised by chaining the `xorb` element op across lanes. The `ivp_rxorb*`
> name is family analogy, not a recovered symbol — carried `[MED/INFERRED]` from
> [CrossLaneReduce §4 CORRECTION](cross-lane-reduce.md).

### 4.1 `reduce_op → ivp r-prefix` value-primitive mapping

The device issues the single-instruction `ivp_r*` cross-lane reduce TIE ops (an `s3_alu` FLIX-slot op for
the arith reduces, an `s1_ld` load-slot op for the bool fold — see [B08](../../isa/ref/b08-reduce.md)); one
FLIX slot op reduces a whole 512-bit register in hardware. The mapping, tied to the B08 roster and confirmed
by driving the `libfiss-base.so` `module__xdref_r*` oracle live ([CrossLaneReduce §5](cross-lane-reduce.md)):

```
ADD        -> ivp_radd{nx16,2nx8,n_2x32} (int, widening) / radds (saturating) / raddu (unsigned)
            / per-lane add_*_32f soft-float chain (fp; "fp32 math")
AVERAGE    -> the same radd path PREFIXED by an elementwise (lane * scale) fp32 multiply
MAX        -> ivp_rmax{nx16,2nx8,n_2x32} (int) / ivp_rmaxnum{nxf16,_2xf32} (fp, NaN-suppressing)
BITWISE_OR -> ivp_rorb{n,2n,n_2}         (full-vector OR fold,  load slot)
BITWISE_AND-> ivp_randb{n,2n,n_2}        (full-vector AND fold, load slot)
BITWISE_XOR-> [no dedicated ivp_rxorb reduce leaf — folded via the xorb element prim; name INFERRED]
```

---

## 5. The reduction axis / geometry — `reduce_axis`

The cross-partition geometry is the `reduce_axis` field (`S4D4_CR` off 36,
`NEURON_ISA_TPB_REDUCE_AXIS`, 1-byte packed; values byte-identical across all four gens):

```c
typedef enum NEURON_ISA_TPB_REDUCE_AXIS {
    NEURON_ISA_TPB_REDUCE_AXIS_ALL        = 0,  // Reduce along all dimensions -> single value out
    NEURON_ISA_TPB_REDUCE_AXIS_PARTITIONS = 1,  // Only reduce across different SBUF/PSUM partitions
} NEURON_ISA_PACKED NEURON_ISA_TPB_REDUCE_AXIS;
```

The header's *Behavior* comment names the two modes verbatim: *"(1) a full reduce operation on all of those
elements to create a single output element (set `reduce_axis` to `ReduceAxis::All`); (2) a reduction across
partitions (no reduction within a single partition) (set `reduce_axis` to `ReduceAxis::Partitions`)."*

* **`All` (=0):** fold **every** partition (all lanes, all `num_active_channels`) → **one** scalar element.
* **`Partitions` (=1):** fold **across partitions only**, *preserving the within-partition lane layout* →
  `dst` shape == `src` shape; lane `i` of every partition folds into lane `i` of the output. This is the
  canonical "cross-partition reduce" the NKI `tensor_partition_reduce` exposes.

### 5.1 The axis → dst element-count predicate (OBSERVED verbatim)

```c
// fn valid_axis_dst_element_cnt(i) -> bool {
//       (   (i.s4d4_cr.reduce_axis == ReduceAxis::All)
//        && (   shape_from_register(i.s4d4_cr.dst_mem_pattern.start_addr)        // register-shaped dst, OR
//            || (   (i.s4d4_cr.dst_mem_pattern.num_elem[0] == 1)                 // dst is exactly 1 element
//                && (i.s4d4_cr.dst_mem_pattern.num_elem[1] == 1)
//                && (i.s4d4_cr.dst_mem_pattern.num_elem[2] == 1)
//                && (i.s4d4_cr.dst_mem_pattern.num_elem[3] == 1))))
//    || (   (i.s4d4_cr.reduce_axis == ReduceAxis::Partitions)
//        && (same_element_count_t4d(i.s4d4_cr.dst_mem_pattern, i.s4d4_cr.src_mem_pattern)))  // dst == src shape
// }
```

So the **output shape** is enum-determined: `All` → a single scalar element (dst `num_elem` all 1, or
register-shaped); `Partitions` → a per-lane vector (dst same element count as src).

### 5.2 Why this is the same physical axis as the intra-vector lane fold

On the GpSimd vector register, **the lanes ARE the partitions** — the partition dim is laid out across the
512-bit register's SIMD lanes. So "fold the 32/64/16 lanes of the register" (the
[CrossLaneReduce](cross-lane-reduce.md) algorithm lens) and "fold across the partitions" (the NKI / this-page
tensor lens) describe the *identical* physical reduction. They are **not two kernels** — one kernel, two
altitudes: the algorithm lens emphasises the `ivp_r*` lane-fold; the NKI public-API lens names the tensor
semantic (`tensor_partition_reduce`). The `reduce_axis` field is the dial: `All` collapses every lane/
partition to a scalar; `Partitions` segments the fold so each within-partition lane position survives.

On-device, the segment for `Partitions` is bounded by the predicated `rb*` / `_t` reduce forms
([B08 §5.5](../../isa/ref/b08-reduce.md)): an active-partition `vbool` mask bounds the fold;
`num_active_channels` (off 34) sets how many partitions participate, and the worker partitions the channels
across POOL cores by `get_cpu_id()`. The exact device segment count is data-dependent on
`num_active_channels`, *not* a fixed-8 constant — the [CrossLaneReduce §5.5 CORRECTION](cross-lane-reduce.md)
withdrew that reading after a 21-call census of `rbmax_nx16_64_512_512`. `[HIGH axis semantics / MED device
mask wiring]`

> **GOTCHA — `ReduceAxis::All` writes ONE element, not a broadcast.** A reimplementer who expects the
> reduced scalar replicated across all lanes (as some SIMD ISAs do for `reduce_add`) will read garbage from
> the other lanes. To broadcast the reduced scalar back to a full vector you must follow with a `rep`/
> `splat` (the [B16 replicate batch](../../isa/ref/b08-reduce.md) cross-links it). `[MED/INFERRED]`

---

## 6. The middle-tier Tensor-Reduce contrast — `S4D4_TR` (`0x42`/`0x52`), the Vector/Pool free-axis reduce

The opcode the task's "Tensor-Reduce" name *actually* denotes (`0x42`/`0x52`) is a **distinct
instruction** bound to a **distinct struct** running on a **distinct engine**. This is the genuine middle
tier of the trilogy — the free-axis / within-partition reduce — and it is **absent from the GPSIMD POOL
ucode** (§2.2). Its operand struct `NEURON_ISA_TPB_S4D4_TR_STRUCT` (also 64B, compile-verified by the
header's own static assert) is structurally *different* from `S4D4_CR`:

| off | `S4D4_CR` (this page, `0x7c`/`0x7d`) | `S4D4_TR` (Tensor-Reduce, `0x42`/`0x52`) |
|---|---|---|
| 32–34 | `in_dtype`, `out_dtype`, `num_active_channels` | `in_dtype`, `out_dtype`, `num_active_channels` |
| **35** | **`reduce_op` : `REDUCE_OP` (6 ops)** | **`negated` : `uint8_t`** |
| **36** | **`reduce_axis` : `REDUCE_AXIS` (All/Partitions)** | **`op` : `NEURON_ISA_TPB_ALU_OP` (60 ops)** |
| 37 | `reserved0[3]` (37–39) | `op_dim` : `TENSOR_SUBDIM` (which free axis) |
| 38 | (reserved) | `mask_enable` : `uint8_t` |
| 39–43 | (reserved) / `scale` : `float` @40 | `reserved1[5]` (39–43) |
| 44–63 | `dst_mem_pattern` | `dst_mem_pattern` |

The two structs *agree on the first 35 bytes' field identities* but diverge entirely at offset 35: the
GPSIMD reduce carries a **6-op `REDUCE_OP` + `reduce_axis` + `scale`**; the Vector/Pool Tensor-Reduce
carries the **full 60-op `ALU_OP` + `negated` + `op_dim` + `mask_enable`**. They are different
instructions.

The `S4D4_TR` header comment enumerates the *nine* instructions it serves — `TensorReduce[Arith/Bitvec]`
(with a `CRC32` polynomial-reduce sub-form), `TransposeTensorReduce[Arith/Bitvec]` (a 32×32 transpose then
reduce), `TensorCumulative[Arith/Bitvec]` (the [scan twin](tensorcumulative.md)), `Copy`, `Cast`,
`Reciprocal`, `StreamShuffle`, `StreamTranspose`. The engine predicate is dispositive:

```c
// fn is_valid_tensor_reduce_arith_engine(i, engine) -> bool {
//       (   (engine == NeuronEngine::DVE)        // the DVE / Vector engine
//        && is_valid_aluop(i.s4d4_tr.op)
//        && is_valid_dtype(i.s4d4_tr.in_dtype,  DtypeAllowFP32R::False)
//        && is_valid_dtype(i.s4d4_tr.out_dtype, DtypeAllowFP32R::True)
//        && !is_argmax_argmin_int_op(i.s4d4_tr.op)
//        && !is_valid_tensor_reduce_int_op(i.s4d4_tr.op))
//    || (   (engine == NeuronEngine::Pool)       // the Pool engine (int / argmax path)
//        && is_valid_aluop(i.s4d4_tr.op)
//        && (is_valid_32b_int_dtype(i.s4d4_tr.in_dtype) || is_valid_64b_int_dtype(i.s4d4_tr.in_dtype))
//        && …
//        && is_valid_tensor_reduce_int_op(i.s4d4_tr.op)
//        && is_s4d4_tr_tensors_in_sbuf(i))
// }
```

> **The engine list is `{DVE, Pool}` — there is no `GpSimd` arm.** (`NeuronEngine` enum: `PE=0, ACT=1,
> POOL=2`, plus `DVE` = the Vector engine.) The `S4D4_TR` Tensor-Reduce (`0x42`/`0x52`) is *never* legal on
> the GpSimd cross-lane datapath; the fp path is DVE/Vector, the int / argmax path is the Pool ALU. This is
> the byte-grounded reason the carved GPSIMD ucode contains *no* `decode_tensor_reduce` worker (§2.2) — and
> the reason the task's expected distinct `decode_tensor_reduce` / `*_RD` GPSIMD kernel **does not exist**:
> the free-axis Tensor-Reduce is a Vector/Pool instruction, and the cross-partition reduce *on GpSimd* is
> the CrossLaneReduce family this page documents. By contrast, `S4D4_CR` (this kernel) carries **no
> per-engine `is_valid_*_engine` predicate** in `common.h` — its engine binding is implicit, and the device
> worker symbols are `pool_cross_lane_reduce_*` (the POOL/GpSimd ucode).

---

## 7. The three-layer reduce trilogy — placed instruction-exact

The three reduce layers each fold a **different axis**, on a **different engine**, with a **different
packed enum**. They share abstract operation *names* (ADD/MAX/…) but no encoding.

| layer | axis folded | NKI API | engine / opcodes | operand struct | op enum |
|---|---|---|---|---|---|
| **(A) within-partition / free-axis** | the **free** (non-partition) tensor axes | `nki.isa.tensor_reduce` | **DVE / Pool** · `0x42`/`0x52` (+`0x83`/`0x84` transpose) | `S4D4_TR` | `NEURON_ISA_TPB_ALU_OP` (~30 arith + bitvec) |
| **(B) cross-partition  ← THIS PAGE** | the SBUF/PSUM **partition** axis (= the GpSimd SIMD lanes) | `nki.isa.tensor_partition_reduce` | **GpSimd (POOL ucode)** · `0x7c`/`0x7d` | `S4D4_CR` | `NEURON_ISA_TPB_REDUCE_OP` `{ADD,AVG,MAX,OR,AND,XOR}` |
| **(C) cross-core / cross-buffer** | DMA **buffers** and **cores** | `nki.isa.allreduce` / `dma_compute` | **SDMA CCE** (collective) | (DGE compute path) | `NisaDmaComputeReduceOp` / `SDMA_CCETYPE {ADD,FMA,MAX,MIN}` |

> **The trilogy framing reconciled with [cross-lane-reduce.md §8](cross-lane-reduce.md).** The
> CrossLaneReduce page modelled the trilogy as (1) intra-vector lane fold / (2) cross-partition / (3)
> cross-core, and labelled tiers (1) and (2) as if they were two kernels with (2) at `0x46`/`0x47`/`0x52`.
> Corrected here: tier (1) and tier (2) are the **same GPSIMD kernel** (CrossLaneReduce ==
> `tensor_partition_reduce` — the lanes *are* the partitions, §5.2); the genuinely-distinct "free-axis"
> middle tier is the **Vector/Pool** `tensor_reduce` (`S4D4_TR`, `0x42`/`0x52`), which the CrossLaneReduce
> page did not separate out. The corrected trilogy is therefore **(A) Vector/Pool free-axis · (B) GpSimd
> partition-axis [this kernel] · (C) SDMA cross-core**. The `0x46`/`0x47` = copy/cast attribution is
> retracted entirely (§2.1).

**Op-enum reconciliation** — three disjoint packed enums, agreeing only on operation *names*:

```
NEURON_ISA_TPB_ALU_OP    (A, S4D4_TR / Tensor-Reduce):  ADD=0x04 MAX=0x08 MIN=0x09 …   (60 ops, +argmax/argmin)
NEURON_ISA_TPB_REDUCE_OP (B, S4D4_CR / CrossLaneReduce): ADD=0 AVERAGE=1 MAX=2 OR=3 AND=4 XOR=5   (6 ops)
SDMA_CCETYPE / CCE_OP    (C, SDMA collective):           ADD=0 FMA=1 MAX=2 MIN=3        (+EXT/GCE)
```

The **value semantics are the same ground-truth element primitives** (`libfiss-base` `xdref add`/`max`/
`min`): the three layers differ only in *what* they fold over (free axis / partition axis / cores) and the
carrier enum. A full ML reduce-scatter / all-reduce composes as **free-axis fold (A, Vector) →
partition-axis fold (B, this GPSIMD kernel) → ring/mesh buffer-fold (C, SDMA)**; each layer associative, so
the global order is free — exactly the header's "associative/commutative, order not architecturally
guaranteed". `[HIGH structure / INFERRED composition]`

> **NOTE — the `S4D4_TR` family also carries the *scan* twin.** `TensorCumulative[Arith/Bitvec]`
> (`0x4E`/`0x5E`) shares the `S4D4_TR` struct with Tensor-Reduce — a cumulative (prefix-scan) along the
> same axis, the reduce's running-accumulate sibling. See [TensorCumulative](tensorcumulative.md)
> for the scan; the cross-partition reduce here has no GPSIMD scan counterpart — the scan lives on the same
> Vector/Pool `S4D4_TR` datapath as the free-axis reduce, not on the GpSimd cross-lane network.

---

## 8. DType / accumulator model

Carried HIGH/OBSERVED from the [CrossLaneReduce §5/§7 oracle drive](cross-lane-reduce.md) (the
`libfiss-base.so` ISS leaves driven live via `ctypes`); the cross-partition reduce reads the *same* struct
and issues the *same* `ivp_r*` ops, so the accumulator model is identical:

* **reduce-ADD widens:** int8→int16 (`radd_2nx8`), int16→int32 (`radd_nx16`). `out_dtype` carries the
  (wider) accumulator. The 16×int32 form `radd_n_2x32` is the exception — no widen, wraps mod 2³² (the one
  overflowing sum). `[HIGH/OBSERVED by execution]`
* **reduce-AVERAGE** = the `radd` path prefixed by a per-lane `(x × scale)` fp32 multiply; `scale` is
  `S4D4_CR.scale@40` / the `clr_reduce_local` 4th arg / `emit_cross_lane_reduce_arith.scale`. *Distinct from
  POOL avg_pool's `pool_scale = 1/N`: here `scale` is applied per-lane before the fold.*
* **reduce-MAX/MIN do NOT widen** (output width == input width); fp `rmaxnum`/`rminnum` are NaN-suppressing
  (a NaN lane treated as the identity, IEEE-754-2019 `maximumNumber`).
* **BITWISE_OR/AND/XOR operate on RAW u8/u16/u32 — no fp32 conversion, no widening.**
* **ADD/AVERAGE/MAX use fp32 math** for the value dtypes (header). `in_dtype`: any valid dtype **except
  FP32R**. `out_dtype`: any valid dtype **including FP32R** (§3.3).

---

## 9. Cross-image / cross-gen stability

* `clr_reduce_local` + `cross_lane_reduce_impl` + `pool_cross_lane_reduce_arith`/`_bitvec` are present at
  the **same** function-start VMAs (`clr_reduce_local @0x0100088c`, worker `@0x01000440`, arith `@0x010003f8`,
  bitvec `@0x01000410`) in **both** CAYMAN_0 (sha256 `910d41c3…`) and CAYMAN_3 (sha256 `052ac31c…`) —
  byte-identical `0x2804` function-start records. The `0xa260` `EXTISA_0` image is shared CAYMAN/MARIANA/
  MARIANA_PLUS.
* **SUNDA** (newest gen, sha256 `444497066f5e1738…`): the reduce kernel persists as opcodes 124/125
  (`pool_cross_lane_reduce_arith`/`_bitvec`) in the SUNDA opcode JSON; **no** standalone `tensor_reduce`.
  `[HIGH/OBSERVED for op#]`
* The four reduce/clr `.xt.prop` worker symbols each appear **9 times** per lib (9 embedded POOL EXTISA
  blobs across the cayman/mariana/maverick/plus accessor set), the symbol roster byte-identical per blob.
* The `S4D4_CR` struct (64B, compile-verified), the `REDUCE_OP` enum `{ADD0,AVG1,MAX2,OR3,AND4,XOR5}`, the
  `REDUCE_AXIS` enum `{All0,Partitions1}`, and the `0x7c`/`0x7d` opcodes are **byte-identical across the
  CAYMAN, MARIANA, SUNDA, and MAVERICK arch-isa headers** (verified — same six enum values, same
  64B struct, same `0x42`/`0x52`/`0x7c`/`0x7d`/`0x83`/`0x84` opcode bytes). The `S4D4_TR` family is the
  *only* one that drifts across gens: SUNDA *adds* `TENSOR_REDUCE_ADD_BF16 = 0x8c` /
  `TENSOR_REDUCE_MAX_BF16 = 0x8d` to it — but those land on the Vector/Pool Tensor-Reduce, never on the
  GPSIMD `S4D4_CR` cross-lane reduce, which is opcode-stable `0x7c`/`0x7d` across all four gens.

> **NOTE — v5 / MAVERICK.** The MAVERICK (NC-v5) arch-isa header exposes the identical `S4D4_CR` struct,
> `REDUCE_OP`/`REDUCE_AXIS` enums, and `0x7c`/`0x7d` opcodes (header-OBSERVED), but the v5 kernel *interior*
> (the worker body) was not byte-grounded — the v2–v4 (cayman/mariana/sunda) device bodies are the
> byte-verified substrate; the v5 interior is `[INFERRED]` from header + opcode-map parity.
> `[HIGH header / INFERRED interior]`

---

## 10. Honest limitations / desync flags

* **FLIX-literal desync.** The two trampolines (`@0x3f8`/`@0x410`), the worker `cross_lane_reduce_impl`
  `@0x440`, and `clr_reduce_local` `@0x88c` are hand-scheduled FLIX VLIW with interleaved literal pools that
  desync under stock `xtensa-elf-objdump`'s linear sweep on the `2f` F-format selector byte. `[HIGH/OBSERVED]`
  on the `entry` prologues, the SP-align, the `a0`/`a1` selector byte, the kernel_info_table bytes, and the
  function-start VMAs; `[MED]` on the mid-bundle dispatch arms and the per-op `REDUCE_OP` switch inside
  `clr_reduce_local`. The bodies are reported **structurally**, never byte-fabricated.
* **Struct sizeof — now compile-verified.** An earlier pass carried the `S4D4_CR` offsets `[MED]` because
  it believed the arch-isa header was absent. The header *is* present in this checkout
  (`neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_s4d4_cr.h`), so the `sizeof==64` and every offset are
  **`[HIGH/OBSERVED]` (compile-verified with `gcc`)** here (§3).
* **The `0x46`/`0x47`/`0x52` arbitration.** Resolved byte-exact from `aws_neuron_isa_tpb_common.h`:
  `0x46=COPY`, `0x47=CAST`, `0x52=TENSOR_REDUCE_BITVEC_OP` (S4D4_TR, DVE/Pool). The
  [CrossLaneReduce](cross-lane-reduce.md) attribution of these to the cross-partition reduce is corrected
  in-page (§1, §2.1) and flagged for the reconcile pass.
* **Per-op fold algorithm / value semantics** are carried `[HIGH/OBSERVED via the CrossLaneReduce §5 live
  drive]` (the `libfiss-base.so` ISS oracle, driven via `ctypes`); not disassembled here. `rxorb`
  (reduce-XOR) has no dedicated leaf — `[MED/INFERRED]`.
* **`reduce_axis::Partitions` device segment count** is data-dependent on `num_active_channels`, not a
  fixed-8 constant (the [CrossLaneReduce §5.5 CORRECTION](cross-lane-reduce.md) withdrew the fixed-8
  reading after a 21-call `rbmax` census). `[HIGH census / MED device wiring]`

---

## 11. Adversarial self-verification — five strongest claims re-challenged

1. **"The GPSIMD cross-partition Tensor-Reduce is `0x7c`/`0x7d`, *not* `0x46`/`0x47`/`0x52`."**
   Re-challenged against `aws_neuron_isa_tpb_common.h` byte-exact: `0x46=COPY`, `0x47=CAST`,
   `0x52=TENSOR_REDUCE_BITVEC_OP`, `0x7c/0x7d=CROSS_LANE_REDUCE_ARITH/BITVEC`; `instruction_mapping.json`
   binds `S4D4_CR → {0x7c,0x7d}` and `S4D4_TR → {0x42,0x52,0x83,0x84,0x4E,0x5E,COPY,CAST,…}`. `0x52` *is*
   a real Tensor-Reduce opcode (the bitvec half of `S4D4_TR`) but its engine predicate is `{DVE,Pool}`,
   never GpSimd. **Holds — and corrects #706.** `[HIGH/OBSERVED]`
2. **"The `S4D4_CR` struct is 64 bytes with `reduce_op@35`, `reduce_axis@36`, `scale@40`."** Re-challenged
   by *compiling* the header struct: `gcc` reports `sizeof==64` and `header@0 events@4 src@12 in_dtype@32
   out_dtype@33 num_active_channels@34 reduce_op@35 reduce_axis@36 reserved0@37 scale@40 dst@44`,
   `sizeof(TENSOR4D)==20`, `sizeof(REDUCE_OP)==1`. `ISA_STATIC_ASSERT(sizeof==64)` holds. **Holds —
   compile-verified.** `[HIGH/OBSERVED]`
3. **"`REDUCE_OP = {ADD=0, AVERAGE=1, MAX=2, BITWISE_OR=3, AND=4, XOR=5}`, byte-identical to #706."**
   Re-challenged: read byte-exact from `aws_neuron_isa_tpb_s4d4_cr.h` and confirmed *identical across all
   four gens* (cayman/mariana/sunda/maverick), closing at `0x5`. Matches
   [cross-lane-reduce.md §4](cross-lane-reduce.md) exactly. **Holds.** `[HIGH/OBSERVED]`
4. **"`reduce_axis` picks the partition axis: `All`→scalar, `Partitions`→per-lane."** Re-challenged against
   the header *Behavior* comment + `valid_axis_dst_element_cnt`: `All(0)` requires dst `num_elem` all 1 (or
   register-shaped); `Partitions(1)` requires `same_element_count_t4d(dst, src)` — dst shape == src shape.
   The geometry is enum-determined. **Holds.** `[HIGH/OBSERVED]`
5. **"Three-tier: (A) Vector/Pool free-axis `S4D4_TR` / (B) GpSimd partition `S4D4_CR` / (C) SDMA
   cross-core — three structs, enums, engines."** Re-challenged: `S4D4_TR` (`op:ALU_OP@36`, `negated@35`,
   `op_dim`, `mask_enable`) vs `S4D4_CR` (`reduce_op:REDUCE_OP@35`, `reduce_axis@36`, `scale@40`) — the two
   64B structs diverge at offset 35; `is_valid_tensor_reduce_arith_engine` = `{DVE,Pool}` (no GpSimd arm);
   the GPSIMD `.xt.prop` set carries *only* CrossLaneReduce, *no* `decode_tensor_reduce`. Three disjoint
   enums (`ALU_OP` 60 · `REDUCE_OP` 6 · `SDMA_CCETYPE` 4). **Holds.** `[HIGH/OBSERVED]`

---

## 12. Cross-references

* [CrossLaneReduce kernel](cross-lane-reduce.md) — the **same GPSIMD kernel viewed at the intra-register
  lane-fold altitude** (`pool_cross_lane_reduce_arith`/`_bitvec`, the `ivp_r*` fold, the `libfiss-base`
  live drive). This page is the *cross-partition* altitude of that kernel; §2.1 / §7 here **correct** its
  `0x46`/`0x47`/`0x52` attribution and trilogy framing.
* [ISA Batch 08 — Cross-Lane Reduce](../../isa/ref/b08-reduce.md) — the per-instruction reference for the
  `ivp_radd`/`rmax`/`rmin`/`rbmax`/`randb`/`rorb` reduce mnemonics this kernel issues, the `s3_alu` vs
  `s1_ld` selector bands, and the live-driven value semantics §4/§8 re-use.
* [TensorCumulative kernel](tensorcumulative.md) — the **scan twin** sharing the `S4D4_TR`
  struct (`0x4E`/`0x5E`) with the free-axis Tensor-Reduce; the running-accumulate sibling of tier (A).
* [CCE in-transfer reduce](../../dma/cce-in-transfer.md) — the **cross-CORE / cross-BUFFER**
  SDMA-CCE collective reduce (`SDMA_CCETYPE {ADD,FMA,MAX,MIN}`), the outermost tier (C) of the §7
  composition.
* [The Tensor-Tensor kernel](tensor-tensor.md) — the structurally-parallel arith/bitvec opcode split
  (`NEURON_ISA_TPB_ALU_OP`, 60 ops) the §7 op-enum reconciliation contrasts against.

---

*Provenance: the opcode map (`0x42`/`0x46`/`0x47`/`0x4E`/`0x52`/`0x5E`/`0x7c`/`0x7d`/`0x83`/`0x84`), the
`S4D4_CR` / `S4D4_TR` struct definitions, the `REDUCE_OP`/`REDUCE_AXIS` enums, and the validity/engine
predicates are read byte-exact from the CAYMAN/MARIANA/SUNDA/MAVERICK arch-isa interface headers
(`aws_neuron_isa_tpb_s4d4_cr.h`, `aws_neuron_isa_tpb_s4d4_tr.h`, `aws_neuron_isa_tpb_common.h`) shipped in
the `aws-neuronx-gpsimd-customop-lib` package, cross-checked against `instruction_mapping.json`
(struct→opcode). The `S4D4_CR` `sizeof==64` and field offsets are **compile-verified** with `gcc`.
The handler-chain VMAs / prologues / kernel_info_table bytes are read from the `.xt.prop` function-start
records of the carved CAYMAN device image (`extisa_CAYMAN_POOL_PERF_EXTISA_0.so`) with
`xtensa-elf-objdump`/`readelf`/`c++filt` (`XTENSA_CORE=ncore2gp`). The reduce algorithm and per-op value
semantics in §4/§8 are carried HIGH/OBSERVED from the `libfiss-base.so` ISS oracle driven live via `ctypes`
([CrossLaneReduce §5](cross-lane-reduce.md)); `.text` VMA==file-offset, the config-DLL `.data.rel.ro` file =
VMA − `0x200000` (`readelf -SW`-confirmed). The NKI `tensor_partition_reduce` → `emit_cross_lane_reduce_*`
→ `0x7c`/`0x7d` binding is the in-package compiler ISA contract. Counts via `nm | rg -c`. The `extracted/`
tree is gitignored (reached with absolute paths). All prose is derived from static analysis and in-process
execution of the shipped artifacts only; nothing here is read from a vendor source tree.*
