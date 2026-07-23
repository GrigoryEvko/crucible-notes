# avg_pool / max_pool — the POOL engine's namesake compute (`decode_pool`, opcode `0x45`)

Windowed **pooling** is the GPSIMD POOL engine's *namesake* compute op: the `Pool` instruction
(opcode **`0x45`** = ASCII `'E'`) that merges every element of a 2-D (or 3-D/4-D) spatial window of a CNN
fmap down to one output element, either by **maximum** (`max_pool`) or by **average** (`avg_pool`). There
is **no separate `avg_pool` kernel and no separate `max_pool` kernel** — both are one device worker,
`decode_pool(bool)`, switching on a 1-byte `pool_func` field of its operand struct. The two inner runners
self-name `"max_pool"` / `"avg_pool"` only in the DEBUG-build firmware log strings. The dispatch arbitration
that resolves the *POOL-engine-vs-pool-op* naming collision is documented separately on the
[`decode_pool` disambiguation page](decode-pool.md); **this** page is the avg/max **compute**: the operand
struct, the `POOL_TYPE` selector, the windowing model, the per-type reduce, the avg-divide implementation,
the dtype handling, and the precise geometric distinction from [CrossLaneReduce](cross-lane-reduce.md) /
[Tensor-Reduce](tensor-reduce.md).

Everything below is derived from static analysis of the shipped GPSIMD device images
(`libnrtucode_extisa.so` / `libnrtucode_internal.so` and the carved `extisa_*_POOL_PERF_EXTISA_*.so`
blobs), read with the Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) and stock binutils, plus the
in-package CAYMAN/MARIANA/MAVERICK/SUNDA arch-isa interface headers
(`aws_neuron_isa_tpb_s4d4_pl.h`, `aws_neuron_isa_tpb_common.h`, `instruction_mapping.json`) shipped in the
`aws-neuronx-gpsimd-customop-lib` package. The `P%i:` Pool log strings are ASCII baked into the
DEBUG-build firmware, used purely as name anchors. Every claim carries a confidence tag
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`.

---

## 1. Executive summary

* **Pool is `0x45`, one kernel, two paths.** The `Pool` opcode (`NEURON_ISA_TPB_OPCODE_POOL = 0x45`,
  `aws_neuron_isa_tpb_common.h:174`) dispatches to **`decode_pool(bool)`** (`.xt.prop` symbol
  `_Z11decode_poolb`, present 6× across the embedded POOL blobs of `libnrtucode_extisa.so`). `decode_pool`
  reads a single 1-byte `pool_func` field from its operand struct and switches into one of two **inline**
  inner runners whose DEBUG log names are literally `"max_pool"` and `"avg_pool"`. avg-vs-max is a
  **parameter**, not a kernel. `[HIGH/OBSERVED — opcode header + mangled symbol + the four DEBUG strings §4]`

* **The selector enum is `NEURON_ISA_TPB_POOL_TYPE` (1 byte, packed):**
  **`NONE = 0`, `MAX_POOL = 1`, `AVG_POOL = 2`** — a complete 3-value closed enum
  (`aws_neuron_isa_tpb_s4d4_pl.h:277-281`). The `Pool` opcode requires `MAX_POOL` or `AVG_POOL`; `NONE`
  is reserved for the sibling `MaxPoolSelect` opcode (`0x58`, §7). The switch's default arm prints
  `"P%i: Error, invalid pooling function."`

* **The operand struct is `NEURON_ISA_TPB_S4D4_PL_STRUCT` — 64 bytes,
  `ISA_STATIC_ASSERT(sizeof == 64)`, compile-verified** (§3). One 4-D src `Tensor4d`, one 4-D
  dst `Tensor4d`, in/out dtype, `num_active_channels`, `pool_func` (`POOL_TYPE`), `pool_dim`
  (`TensorSubdim`), and `pool_scale` (fp32). `[HIGH/OBSERVED — header + gcc compile]`

* **The windowing is software-streamed, not a kernel-side sliding window.** The header is explicit: *"the
  pooling engine depends on software to deliver a stream of data where all of the elements to be pooled
  together come sequentially in a stream."* The kernel's job is: compute `pool_size` = product of the
  `pool_dim`'d src dims (the DEBUG **`period`**); read `pool_size` elements; apply max/avg; emit one
  output; repeat for each pool (the DEBUG **`num`**). The src `Tensor4d`'s 4-D strided access pattern is
  what arranges the window-major stream (low dims XY navigate *within* a window; high dims ZW jump
  *between* windows/tiles). `[HIGH/OBSERVED — header §"Pool instruction behavior" + the period/num DEBUG strings]`

* **The per-type reduce.** `max_pool` = streaming windowed fp32 **MAX** over `pool_size` elements; output =
  the largest element, no scale. `avg_pool` = streaming windowed fp32 **SUM**, then **multiply by
  `pool_scale`** (= 1/N, the host-precomputed reciprocal of the window size). `[HIGH/OBSERVED — header §pool_func + §pool_scale]`

* **The avg-divide is a multiply-by-1/N, NOT a device divide and NOT a reciprocal Parameter-RAM.**
  `pool_scale` (struct off 40, fp32) holds 1/(R·S) precomputed by the **host** (`0.25` for 2×2, `0.1111`
  for 3×3). The kernel issues no division and no Newton reciprocal/rsqrt — it scale-multiplies the fp32
  accumulator. For ops that don't use `pool_scale` (`max_pool`, `MaxPoolSelect`) the ISA enforces
  `pool_scale == 1.0`. `[HIGH/OBSERVED — header §pool_scale verbatim]`

* **All pooling math is fp32.** Header: *"All pooling calculations are performed in FP32 data-type"* /
  *"Internal accumulation must be done at fp32 single precision."* In/out dtypes may be
  `UINT8/INT32/FP16/BFLOAT16/FP32` (+`INT64` in only), each converted to internal fp32 for the reduce.

* **SIMD vectorization is across `num_active_channels`** (the partition/channel axis); the SIMD width is
  `POOLING_NUM_CHANNELS = 128` (`common.h:35`). The window fold (`period`) runs *serially along the data
  stream*, orthogonal to the lane (channel) axis.

* **Reduce-geometry distinction (the key reconciliation):** pooling is a **windowed-spatial** reduce
  *along the data stream* (over `pool_size` sequential elements), vectorized *across* the 128 channel
  lanes. [CrossLaneReduce / Tensor-Reduce](cross-lane-reduce.md) is an **intra-register / cross-partition**
  reduce that folds *across the SIMD lanes* of one 512-bit register to a scalar. **Orthogonal geometries,
  distinct kernels, distinct structs+enums** (`S4D4_PL`/`POOL_TYPE` vs `S4D4_CR`/`REDUCE_OP`). Both enums
  carry an "average"/"avg", but pool's `AVG_POOL` multiplies *once* by `pool_scale`(1/N) *after* the
  window sum, while CrossLaneReduce's `AVERAGE` multiplies *each lane* by `scale` *before* the lane-fold.
  `[HIGH — §8]`

---

## 2. The dispatch chain — `'E'` → `kernel_info_table` `0x45` → `decode_pool`

A `Pool` instruction reaches the kernel through three independently cross-checked hops.

**(A) The SEQ front-end.** The NX_POOL sequencer's 178-table keys the compute op by its ASCII opcode byte:
`'E'` = `0x45` (`chr(0x45) == 'E'`). The SEQ decodes the `'S:'` instruction stream and routes opcode `0x45`
to the Q7 POOL compute core; the device disassembler carries the sequencer name string `"Pool"` (the
companion `"S: Pool"` / `"S: PoolPeriod=%d PoolNum=%d"` tokens are OBSERVED at `libnrtucode_internal.so`
file offsets `0x18d318` / `0x18d34c`). `[HIGH/OBSERVED string; MED/CARRIED routing]`

**(B) The Q7 `kernel_info_table`** (the compute back-end). The carved CAYMAN POOL `EXTISA_0` image binds
opcode `0x45` to the Pool entry funcVA. From the carved image: `kernel_info_table` @ VMA `0x02000380`
(file `0x7400`, 17 entries, 8-byte stride); **idx3 = `{spec 0, opcode 0x45 (69), funcVA 0x01000b90}`** — the
only relocated field is the funcVA. The dispatcher's linear scan matches the packed `(spec, opcode)` key
then `callx8 0x01000b90`. The per-core trace prints
`"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."` (= `0x45`). `[HIGH/CARRIED — prior carve; opcode HIGH/OBSERVED]`

**(C) The entry trampoline → the body.** The standard POOL kernel entry: a thin trampoline @ `0x01000b90`
(`entry a1, 64`; a setup `callx8`; a work `callx8` into `0x01006ee8` → the `decode_pool` body; `retw.n`)
tails into **`decode_pool` @ funcVA `0x01000bc0`** (`entry a1, 32`). `[HIGH/CARRIED — prior disasm]`

> **NOTE — `0x45` is the only pool opcode in the GPSIMD Q7 ucode.** `instruction_mapping.json`
> (`struct2opcode`) binds the `S4D4_PL` struct to **two** opcodes —
> `NEURON_ISA_TPB_OPCODE_POOL` and `NEURON_ISA_TPB_OPCODE_MAX_POOL_SELECT` — but only `0x45` (`Pool`) is
> registered in the CAYMAN `kernel_info_table` and reaches `decode_pool`; `0x58` (`MaxPoolSelect`,
> `chr(0x58) == 'X'`) is a hardware-datapath op absent from the Q7 firmware (§7). `[HIGH/OBSERVED — instruction_mapping.json + the kernel_info_table idx set]`

---

## 3. The operand struct — `NEURON_ISA_TPB_S4D4_PL_STRUCT` (64 bytes, compile-verified)

Source: `aws_neuron_isa_tpb_s4d4_pl.h` (CAYMAN, "ISA header for NC-v3"). The header's own format comment
names it: *"Neuron 'S4D4_PL' Format — one 4d SRC Tensor, one 4d DST Tensor. Use for: Pool instruction."*
**S4D4_PL** = one 4-D src, one 4-D dst, **PL** = poo**L**. `instruction_mapping.json` seals the binding:

```json
"NEURON_ISA_TPB_S4D4_PL_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_POOL",
    "NEURON_ISA_TPB_OPCODE_MAX_POOL_SELECT"
]
```

> **The `sizeof == 64` static-assert is *compile-verified*.** Reproducing the header's
> `typedef struct NEURON_ISA_TPB_S4D4_PL_STRUCT` (with `NEURON_ISA_PACKED = __attribute__((packed))` and
> the 1-byte packed `POOL_TYPE`/`TENSOR_SUBDIM`/`DTYPE` enums), `gcc -std=c11` confirms
> **`sizeof == 64`**, `sizeof(TENSOR4D) == 20`, `sizeof(POOL_TYPE) == 1`, and every field offset:
> `header@0 events@4 src@12 in_dtype@32 out_dtype@33 num_active_channels@34 reserved0@35 pool_func@36
> pool_dim@37 reserved1@38 pool_scale@40 dst@44`. The header's
> `ISA_STATIC_ASSERT(sizeof(...) == 64, ...)` holds and the offsets are byte-exact. `[HIGH/OBSERVED — compile-verified]`

### 3.1 Field layout

| off | size | field | type | notes |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode:1 (0x45 Pool \| 0x58 MaxPoolSelect), inst_word_len:1, debug_cmd:1, debug_hint:1}` |
| 4–11 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12–31 | 20 | `src_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | INPUT tensor (4-D strided access pattern, §3.2) |
| 32 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` | source element dtype |
| 33 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | destination element dtype |
| 34 | 1 | `num_active_channels` | `uint8_t` | channels (lanes) to pool over; `1..128` |
| 35 | 1 | `reserved0[1]` | `uint8_t[1]` | must be 0 (`s4d4_pl_reserved_zero`) |
| **36** | 1 | **`pool_func`** | `NEURON_ISA_TPB_POOL_TYPE` | **the pool-type selector — MAX vs AVG (§3.3)** |
| **37** | 1 | **`pool_dim`** | `NEURON_ISA_TPB_TENSOR_SUBDIM` | **which src dims form the pool WINDOW (§3.4)** |
| 38–39 | 2 | `reserved1[2]` | `uint8_t[2]` | must be 0 |
| **40–43** | 4 | **`pool_scale`** | `float` (fp32) | **the AVG_POOL 1/N multiplier (= 1/(R·S), §6)** |
| 44–63 | 20 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | OUTPUT tensor |

`4 + 8 + 20 + 1 + 1 + 1 + 1 + 1 + 1 + 2 + 4 + 20 = 64`.

### 3.2 `NEURON_ISA_TPB_TENSOR4D` (20 bytes, `common.h:660`)

```c
typedef struct NEURON_ISA_TPB_TENSOR4D {
    NEURON_ISA_TPB_ADDR4 start_addr;    //  4  (union: addr_immediate | addr_reg; the SBUF/PSUM partition-offset)
    int16_t              step_elem[4];  //  8  per-dim SIGNED element stride
    uint16_t             num_elem[4];   //  8  per-dim element count
} NEURON_ISA_TPB_TENSOR4D;             // = 4 + 8 + 8 = 20  (compile-verified)
```

The header's *Typical usage* (§`src_mem_pattern`) for a CNN pool step:

* `dim0` (X) — pooling-window **column** index (row-major), `x_step` = 1 (sequential within a row)
* `dim1` (Y) — pooling-window **row** index, `y_step` = the fmap width (jump between window rows)
* `dim2` (Z) — FMAP column index *× the pooling stride* (jump from one window/tile to the next)
* `dim3` (W) — FMAP row index *× the pooling stride*

`start_addr` carries the SBUF/PSUM partition-offset. The **low two dims (XY) navigate inside one pooling
window; the high two dims (ZW) jump between windows/tiles** — this is exactly the window-major stream the
software is required to deliver (§5). `[HIGH/OBSERVED — header §src_mem_pattern + the worked 4×4 → 2×2 example]`

### 3.3 `NEURON_ISA_TPB_POOL_TYPE` (1 byte, packed) — the pool-type selector

The **complete, closed** enumeration of every value `pool_func@36` can hold and the worker switches on
(`aws_neuron_isa_tpb_s4d4_pl.h:277-281`, read byte-exact; identical across CAYMAN/MARIANA/MAVERICK/SUNDA):

| code | enumerator | legal-in | semantics |
|---|---|---|---|
| **`0`** | `NEURON_ISA_TPB_POOL_TYPE_NONE` | `MaxPoolSelect` only | reserved; `Pool` opcode disallows it |
| **`1`** | `NEURON_ISA_TPB_POOL_TYPE_MAX_POOL` | `Pool` (`0x45`) | windowed max; fp32 math; `pool_scale` forced to 1.0 |
| **`2`** | `NEURON_ISA_TPB_POOL_TYPE_AVG_POOL` | `Pool` (`0x45`) | windowed sum × `pool_scale`(1/N); fp32 math |

The enum closes at `0x2`; total = 3. The header's `has_valid_pooltype` predicate (OBSERVED verbatim)
enforces the legal-in column:

```c
// fn has_valid_pooltype(i) -> bool {
//       (i.header.opcode == Opcode::Pool          && (pool_func == MaxPool || pool_func == AvgPool))
//    || (i.header.opcode == Opcode::MaxPoolSelect && (pool_func == None))
// }
```

`[HIGH/OBSERVED — header enum + predicate]`

### 3.4 `NEURON_ISA_TPB_TENSOR_SUBDIM` (`pool_dim@37`, 1 byte) — the window dimensionality

The `pool_dim` field tells the engine **which source dimensions form one pooling window**
(`common.h:1056-1062`, read byte-exact):

| code | enumerator | window dims |
|---|---|---|
| `0x00` | `TENSOR_SUBDIM_UNUSED` | — |
| `0x02` | `TENSOR_SUBDIM_X` | X (1-D pool) |
| `0x03` | `TENSOR_SUBDIM_XY` | X,Y (the typical 2-D pool) |
| `0x04` | `TENSOR_SUBDIM_XYZ` | X,Y,Z |
| `0x05` | `TENSOR_SUBDIM_XYZW` | X,Y,Z,W |

> **GOTCHA — `TENSOR_SUBDIM` is *not* a dense 0..4 enum.** `UNUSED = 0x00`, then `X` **jumps to `0x02`**
> (there is no `0x01`), `XY = 0x03`, `XYZ = 0x04`, `XYZW = 0x05`. The value `0x01` is not a member; a
> reimplementer treating the field as "number of window dims minus one" (which would put X at `0x00`)
> mis-decodes every window. The header's inclusion predicates (`pl_x_in_dim`/`pl_y_in_dim`/`pl_z_in_dim`/
> `pl_w_in_dim`) test set-membership against these exact codes, never an arithmetic compare.
> `[HIGH/OBSERVED — common.h enum + the four `pl_*_in_dim` predicates]`

`pool_size` (the DEBUG `period`) = the product of the `num_elem[]` counts of exactly the dims named by
`pool_dim`. For the header's worked 4×4 → 2×2 example with `pool_dim = XY`, `pool_size = x_num · y_num =
2 · 2 = 4`. `[HIGH/OBSERVED — header §"Figure out how many elements will be merged"]`

### 3.5 DType encoding & the supported-pool set

`NEURON_ISA_TPB_DTYPE` (4-bit, `common.h:722-739`): `INVALID 0x0`, `UINT64 0x1`, `INT8 0x2`, `UINT8 0x3`,
`INT16 0x4`, `UINT16 0x5`, `BFLOAT16 0x6`, `FP16 0x7`, `INT32 0x8`, `UINT32 0x9`, `FP32 0xA`, `FP32R 0xB`,
`INT64 0xC`, `FP8_EXP3/4/5 0xD/0xE/0xF`. The header's *recommended-usage* table fixes the pool-supported
sets (§5.2).

### 3.6 Validity predicates (`is_valid_s4d4_pl`, OBSERVED verbatim)

```c
// fn is_valid_s4d4_pl(i) -> bool {
//       has_valid_neuron_header(i) && has_valid_neuron_events(i)
//    && has_s4d4_pl_opcode(i)                                  // opcode == Pool(0x45) || MaxPoolSelect(0x58)
//    && s4d4_pl_reserved_zero(i)                               // reserved0[0], reserved1[0..1] == 0
//    && is_valid_dtype(i.in_dtype,  DtypeAllowFP32R::False)    // src may NOT be fp32r
//    && is_valid_dtype(i.out_dtype, DtypeAllowFP32R::True)     // dst MAY be fp32r
//    && start_addr_active_channels(i.src_mem_pattern.start_addr, i.num_active_channels)
//    && start_addr_active_channels(i.dst_mem_pattern.start_addr, i.num_active_channels)
//    && has_valid_pooltype(i)                                  // §3.3
//    && is_valid_subdim(i.pool_dim)                            // §3.4
//    && has_valid_active_channel_range(i.num_active_channels, POOLING_NUM_CHANNELS)  // 1..128
//    && s4d4_pl_dtype_check(i)                                 // MaxPoolSelect → out ∈ {u8,u16,u32}
//    && tensor4d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)  // src RO, SBUF|PSUM
//    && tensor4d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::True, AllowedInSBUF::True)  // dst W,  SBUF|PSUM
//    && s4d4_pool_scale_valid(i)                               // == 1.0 if unused (§6)
// }
```

> **NOTE — `in_dtype` may NOT be `FP32R`, but `out_dtype` MAY be** (same asymmetry as CrossLaneReduce).
> The src is read with `DtypeAllowFP32R::False`, the dst with `True`. A reimplementer who accepts the
> round-mode `FP32R` (`0xB`) on the *input* side accepts an instruction the hardware decoder rejects.

> **NOTE — pool src and dst may live in EITHER SBUF or PSUM** (`AllowedInPSUM::True, AllowedInSBUF::True`
> for both), unlike CrossLaneReduce whose `S4D4_CR` tensors are SBUF-only (`AllowedInPSUM::False`). The src
> is read-only; the dst is written. Pool is frequently *fused after MatMul/Activate*, so its src is often
> the **PSUM-resident fp32 accumulator** — which is exactly why PSUM is allowed and why the recommended
> in_dtype for fused pooling is `FP32`. `[HIGH/OBSERVED — header tensor4d_valid flags + §recommended-usage]`

---

## 4. The kernel body — `decode_pool` @ `0x01000bc0` (dispatch + the two runners)

`decode_pool(bool)` — `.xt.prop` `_Z11decode_poolb` @ funcVA `0x01000bc0`, the `kernel_info_table` idx3
work body reached via the `0x01000b90` trampoline. The `bool` argument is the per-instruction
descriptor/decode flag handed by the dispatcher (the same convention as `cross_lane_reduce_impl(bool)`); the
`MAX`/`AVG` selection is read from the operand struct at `off 36`, **not** from this bool. `[HIGH sig — mangled name; MED/CARRIED bool-role]`

The body is ~`0x1da` bytes of hand-scheduled **FLIX/VLIW** with eight interleaved literal/data spans (the
`.xt.prop` records carry a function-start record then alternating code/`DATA` spans). Those embedded
literal pools are why stock `xtensa-elf-objdump`'s linear sweep loses bundle sync inside `decode_pool` (the
documented native-disasm limitation; ~25 % `.byte` mis-decode measured in the `0xbc0..0xdff`
window). The function-start, the `entry a1,32` prologue, the code/data span boundaries, and the
cross-references are HIGH/OBSERVED; the exact **mid-bundle dispatch arms are reported structurally**
(MED/INFERRED), never byte-fabricated.

### 4.1 The `pool_func` switch + the two runners (from the DEBUG strings)

The only place the PERF kernel self-names is the DEBUG-build firmware's log strings (compiled out of the
PERF/RELEASE image). Carved from `libnrtucode_internal.so` `.rodata`, **OBSERVED at these byte offsets**
(one contiguous code-module cluster):

```
0x2699db  "P%i: Pool : num_chans = %0d"                              (Pool kernel entry; num_active_channels)
0x2699f8  "P%i: Error, invalid pooling function."                    (the pool_func switch DEFAULT arm)
0x269a1f  "P%i: Running max_pool with period = %0d and num = %0d."   (MAX_POOL=1 path)
0x269a57  "P%i: Done running max_pool."
0x269a74  "P%i: Running avg_pool with period = %0d and num = %0d."   (AVG_POOL=2 path)
0x269aac  "P%i: Done running avg_pool."
0x269bd1  "P%i: pool_num = %0d, pool_per = %0d"                       (the pool count + per-pool size)
```

`[HIGH/OBSERVED — string offsets from the shipped internal.so]`

The control flow these strings + the `POOL_TYPE` enum + the header behavior pin (the structure is
HIGH; the exact in-bundle register binding is MED):

```c
// decode_pool(bool) @ 0x01000bc0  — structure from .xt.prop + DEBUG strings + POOL_TYPE enum + header.
// Recovered symbol: _Z11decode_poolb. [HIGH structure / MED in-bundle slots]
void decode_pool(bool descriptor_flag) {
    s4d4_pl_t op = decode_operand();                 // the 64-byte NEURON_ISA_TPB_S4D4_PL_STRUCT
    log("P%i: Pool : num_chans = %0d", core, op.num_active_channels);

    // period = pool_size = product of num_elem[] over the dims named by pool_dim (§3.4)
    uint32_t period = pool_size_from_subdim(op.src_mem_pattern, op.pool_dim);  // e.g. 4 for a 2x2 window
    uint32_t num    = output_pool_count(op);          // number of output elements (outer trip count)

    switch (op.pool_func) {                            // pool_func @ off 36, the POOL_TYPE selector
    case POOL_TYPE_MAX_POOL:                           // = 1
        log("P%i: Running max_pool with period = %0d and num = %0d.", core, period, num);
        run_max_pool(op, period, num);                 // §5.1
        log("P%i: Done running max_pool.", core);
        break;
    case POOL_TYPE_AVG_POOL:                           // = 2
        log("P%i: Running avg_pool with period = %0d and num = %0d.", core, period, num);
        run_avg_pool(op, period, num, op.pool_scale);  // §5.2 — pool_scale = 1/N @ off 40
        log("P%i: Done running avg_pool.", core);
        break;
    default:                                           // POOL_TYPE_NONE or any other
        log("P%i: Error, invalid pooling function.", core);  // matches has_valid_pooltype
        break;
    }
}
```

The two runners are **inline** (private) in the PERF image — they carry no `.xt.prop` section of their own
(only `decode_pool` does), consistent with the compiler inlining the max/avg loops into `decode_pool`.
`[HIGH — only `_Z11decode_poolb` has a prop section]`

**`period` / `num` semantics** (HIGH/OBSERVED from the format strings; the register binding MED):

* `period` = `pool_size` = number of elements merged into one output (the window cardinality; 4 for 2×2).
* `num` = number of output elements = the number of pools the kernel iterates (the outer loop trip count).
* `pool_per` (string `0x269bd1`) = the same per-pool element count; `pool_num` = the pool index/count.

---

## 5. The windowing model + the per-type reduce

### 5.1 The SW-streamed window (the geometry)

The pooling engine does **not** maintain its own 2-D sliding window. The header's *Pool instruction
behavior* section is the contract (verbatim, OBSERVED):

> *"the pooling engine depends on software to deliver a stream of data where all of the elements to be
> pooled together come sequentially in a stream."*

For the worked 4×4 fmap `a,b,c,d, m,n,o,p, q,r,s,t, v,w,x,y` (unrolled low→high) pooled 2×2, software
arranges the **window-major** order `a,b,m,n, c,d,o,p, q,r,v,w, s,t,x,y` via a 4-D tensor
(`x_num=2,x_step=1; y_num=2,y_step=4; z_num=2,z_step=2; w_num=2,w_step=8`). The engine then:

```c
// run_max_pool / run_avg_pool inner loop (header §"The job of the pooling engine"). [HIGH structure]
// The src Tensor4d's strided walk delivers elements window-major; the kernel folds along the STREAM.
for (uint32_t p = 0; p < num; ++p) {            // num output pools (DEBUG "num")
    acc_t acc = identity(pool_func);            // -INF for MAX, 0.0f for SUM
    for (uint32_t e = 0; e < period; ++e) {     // period = pool_size sequential stream elements
        float x = to_fp32(read_next_stream_element(in_dtype));   // ALWAYS converted to internal fp32
        acc = fold(pool_func, acc, x);          // MAX: ivp_maxnxf16t/maxnx16t ; AVG: fp32 running SUM
    }
    if (pool_func == POOL_TYPE_AVG_POOL)
        acc = acc * pool_scale;                 // × 1/N  (ivp_muln_2xf32t)   — §6
    write_output_element(to_dtype(acc, out_dtype));   // emit ONE element, cast to out_dtype
}
```

The fold runs **serially along the SW-delivered stream** (the `period` axis), producing one output per
window — a windowed-spatial reduce, *not* an intra-register-lane fold (cf. §8). `[HIGH/OBSERVED — header §behavior + the period/num strings]`

### 5.2 dtype / accumulator

The header (§recommended-usage + §Notes):

* *"All pooling calculations are performed in FP32 data-type."* / *"Internal accumulation must be done at
  fp32 single precision."* The `in_dtype` is **converted to internal fp32**, the fold runs in fp32, the
  result is cast to `out_dtype` on store.
* **Supported pool IN dtypes:** `UINT8 / INT32 / FP16 / BFLOAT16 / FP32 / INT64`.
* **Supported pool OUT dtypes:** `UINT8 / INT32 / FP16 / BFLOAT16 / FP32` (`INT64` not writable to SB/PSUM —
  header: *"pooling engine can't write INT64 results to SB/PSUM"*).
* There is **no int-widening accumulator scheme** (unlike CrossLaneReduce-ADD which widens int8→16/16→32).
  Pooling *always* accumulates in fp32, so overflow is bounded by fp32 range — which is exactly what
  licenses the scale-before-sum order for AVG (the header warns fp32/bf16 sums could overflow fp32, §6).

> **NOTE — `UINT8`/`INT32` "advanced" max-pool runs on the quantized representation but still converts to
> internal fp32.** The header's UINT8-option2 path performs MaxPooling directly on the quantized form (no
> dequant), but warns: *"Conversion to an internal FP32 data-type still occurs. However … all UINT8 numbers
> are exactly representable in FP32."* So even the integer max path is an fp32 fold — there is no separate
> integer pooling datapath. `[HIGH/OBSERVED — header UINT8 option2]`

### 5.3 The value primitives

The per-element value primitives the image issues (image-wide census; these are inlined Xtensa
TIE intrinsics, not symbol-table strings, so they're pinned by the disasm op-set, not a string grep —
`[HIGH presence / MED exact in-body slot]`):

| primitive | role |
|---|---|
| `ivp_maxnxf16t` | fp16/bfloat16 vector MAX — the `max_pool` fold (fp16/bf16 input path) |
| `ivp_maxnx16t` | int16 vector MAX — the quantized/int `max_pool` path |
| `ivp_muln_2xf32t` | fp32 vector MULTIPLY — the `avg_pool` × `pool_scale`(1/N) step |
| `ivp_mulsonen_2xf32` | fp32 multiply-by-one — the `pool_scale == 1.0` / no-op-scale path |

Their exact scalar semantics (signed max, IEEE binary32 add/mul) are pinned by the `xdref`
oracle (`xdref max_16_16_16` signed-max; `xdref add_*_32f` IEEE binary32 add; the fp32 multiply) — the same
`libfiss-base.so` ground-truth that the [CrossLaneReduce §5 live drive](cross-lane-reduce.md) executes.
`[HIGH op-set; CARRIED ISS semantics]`

### 5.4 SIMD vectorization (lanes = channels)

The SIMD lanes are the `num_active_channels` (the FMAP/neuron channel axis); the vector width is
`NEURON_ISA_TPB_POOLING_NUM_CHANNELS = 128` (`common.h:35`). The kernel processes the pool stream for all
active channels **in parallel across the vector lanes** (the `"num_chans = %0d"` log), while the window
fold (`period`) runs **serially along the stream**. Each Q7 pool core (`P%i`) handles its
`get_cpu_id()`-assigned share of the channels (the work-partition gate; the firmware asserts
`total_cpus == 1 || total_cpus == NUM_POOL_CORES`, OBSERVED at `internal.so` `0x4d281`). **Two orthogonal
axes: the LANE axis (channels, 128-wide vector) × the STREAM axis (the window, serial fold of `period`
elements).** This is not a lane-fold. `[HIGH/OBSERVED — POOLING_NUM_CHANNELS + the num_chans log + the cpu-count assert]`

---

## 6. The avg-divide implementation — multiply by 1/N (NOT a divide, NOT a Parameter-RAM reciprocal)

This is the question the task names explicitly, and the header answers it byte-exact (§`pool_scale`,
verbatim OBSERVED):

> *"This field is relevant for Average-Pooling only. It holds the value of 1/(RS), with R & S being the
> pooling window dimensions (so 0.25 for 2x2 pooling, and 0.1111 for 3x3 pooling, etc). The role of this
> field is to allow the hardware engine to simply scale the input values and then accumulate, without the
> need to count the number of elements in a pooling window."*

So `avg_pool` performs:

```
out = ( Σ_{i ∈ window} fp32(x_i) ) × pool_scale      where  pool_scale = 1/(R·S) = 1/N
```

— a single fp32 **MULTIPLY** by the **host-precomputed reciprocal**. **No integer or float division
instruction is issued; no Newton reciprocal/rsqrt is run.** `pool_scale` lives at `S4D4_PL` off 40 (fp32),
and the device step is `ivp_muln_2xf32t` (§5.3). `[HIGH/OBSERVED — header §pool_scale + struct off 40]`

> **QUIRK — the scale may be applied *before* the sum, not only after.** Header §Notes (verbatim): *"It is
> legal for an implementation to do the full summation over the pool before the multiplication by the scale
> factor. This should be taken into account for any pooling done on FP32 or BF16 numbers where the
> intermediate sum could overflow an FP32 number."* So the device may compute `Σ(x_i · scale)`
> (scale-then-sum) **or** `(Σ x_i) · scale` (sum-then-scale); both produce the average, but they are *not*
> bit-identical in fp32 — a reimplementer matching exact bits must determine the chosen order per kernel
> build.

> **CORRECTION — pooling does NOT use the batch-norm reciprocal Parameter-RAM.** The Newton
> reciprocal/rsqrt machinery (`recip0`/`rsqrt0`/`div0`/`divn` over a Param-RAM seed table) belongs to
> **batch-norm** (1/variance, 1/N for the mean) and is a *separate* kernel/datapath. Pooling's 1/N arrives
> precomputed in `pool_scale` and is a plain fp32 multiply — it never touches the reciprocal seed RAM. For
> `max_pool` and `MaxPoolSelect` the ISA forces `pool_scale == 1.0` (`s4d4_pool_scale_valid`: *"For ops
> that don't use pool_scale, the ISA enforces it to be 1.0"*). `[HIGH/OBSERVED — header + the batch-norm reconciliation]`

---

## 7. `MaxPoolSelect` (`0x58`) — the argmax sibling, NOT in the Q7 ucode

`NEURON_ISA_TPB_OPCODE_MAX_POOL_SELECT = 0x58` (`chr(0x58) == 'X'`, `common.h:187`) shares the **same
`S4D4_PL` struct** (`has_s4d4_pl_opcode` accepts `0x45` *or* `0x58`; `instruction_mapping.json` binds both),
but `pool_func` **must be `NONE`** (the opcode itself specifies behavior). It runs max-pool but emits the
**unsigned-integer position** (`0..N−1`) of the max element in each window, not the max *value* (header:
*"required for training"*; `s4d4_pl_dtype_check` forces `out_dtype ∈ {UINT8, UINT16, UINT32}`, the argmax
index type). There is no average analogue (header: *"there is no comparable instruction for average pool —
not needed"*). `[HIGH/OBSERVED — header §MaxPoolSelect + opcode + dtype predicate]`

> **NOTE — `MaxPoolSelect` (`0x58`) is absent from the Q7 POOL ucode.** No `"MaxPoolSelect"`/`"argmax"`
> string appears in the Q7 pool DEBUG cluster, and `0x58` is not registered in the CAYMAN
> `kernel_info_table` (only `0x45` reaches `decode_pool`). The `S4D4_PL` struct documents `0x58` because the
> struct is shared *at the ISA level*; the argmax op is handled by the dedicated hardware pooling datapath,
> while the GPSIMD Q7 software pool kernel implements only the general `Pool` max/avg (`0x45`). The token
> `max_pool_select` is present in `internal.so` `.rodata` (e.g. `0x18d370`) as a sequencer/source label,
> not a Q7 kernel entry. `[HIGH for the 0x45-only-in-Q7 finding; the HW-datapath routing of 0x58 is INFERRED-HIGH]`

---

## 8. Reduce-geometry distinction — pool (windowed-spatial) vs CrossLaneReduce (cross-lane/partition)

This is the reconciliation the task requires, and it is the single most important thing to get right when
placing pooling among the Neuron reduce primitives. **Pooling and [CrossLaneReduce](cross-lane-reduce.md) /
[Tensor-Reduce](tensor-reduce.md) are different kernels, with different opcodes, different operand structs,
different selector enums, and — crucially — different *reduce geometries*.**

| property | **POOL** (`decode_pool`, this page) | **CrossLaneReduce / Tensor-Reduce** ([those pages](cross-lane-reduce.md)) |
|---|---|---|
| opcode | `0x45` `'E'` `Pool` (+`0x58` `MaxPoolSelect`) | `0x7c` `'\|'` Arith / `0x7d` `'}'` Bitvec |
| operand struct | `NEURON_ISA_TPB_S4D4_PL` (64 B) | `NEURON_ISA_TPB_S4D4_CR` (64 B) |
| selector enum | `POOL_TYPE` `{NONE 0, MAX 1, AVG 2}` (3) | `REDUCE_OP` `{ADD 0, AVERAGE 1, MAX 2, OR 3, AND 4, XOR 5}` (6) |
| worker | `decode_pool(bool)` @ `0x01000bc0` | `cross_lane_reduce_impl(bool)` @ `0x01000440` |
| **reduce geometry** | **WINDOWED-SPATIAL: fold over `period` SEQUENTIAL stream elements per output; vectorized ACROSS the 128 channel lanes** | **CROSS-LANE / CROSS-PARTITION: fold ACROSS the SIMD lanes of one 512-bit register to a scalar (the lanes ARE the SBUF partitions)** |
| AVG/AVERAGE divide | × `pool_scale`(1/N), host-precomputed, **once after** the window sum (off 40) | × `S4D4_CR.scale` **per-lane before** the lane-fold (off 40) |
| reduce-axis field | `pool_dim` (`TensorSubdim` X/XY/XYZ/XYZW) | `reduce_axis` (`All` / `Partitions`) |
| dtype / accumulator | always fp32 (no int widen) | `ADD` widens int8→16/16→32; `MAX` no widen; bitvec raw |

**The geometric statement, explicitly:**

* **Pooling folds the SW-streamed pool *window*** — `period` sequential elements per output — **parallel
  across the 128 channel lanes.** The reduce axis is the *spatial data stream* (the X/Y/Z/W window dims of
  the fmap). Output count = `num` windows.
* **CrossLaneReduce folds the *lanes* of one register** — a single `ivp_r*` op collapses the 32/64/16 SIMD
  lanes (= the SBUF/PSUM partitions, since the partition dim is laid out across the register lanes) → a
  scalar. The reduce axis is the *partition/lane* axis.

They use the **same ground-truth element math** (the `xdref` max/add/mul primitives) — both even
carry an "average"/"avg" mode — but they fold over **orthogonal axes**. They *compose* in a full CNN
reduce: a spatial-window reduce (POOL) followed by a channel/partition reduce (CrossLaneReduce →
Tensor-Reduce → SDMA-CCE cross-core). Each is associative, so the global order is free. `[HIGH]`

> **CORRECTION — do NOT attribute opcodes `0x46`/`0x47`/`0x52` to pooling.** An earlier note on the
> [CrossLaneReduce page](cross-lane-reduce.md) mis-attributed `0x46`/`0x47`/`0x52` to a "cross-partition
> Tensor-Reduce"; the [Tensor-Reduce page §2.1](tensor-reduce.md) corrected this byte-exact from
> `common.h`: **`0x46 = COPY`, `0x47 = CAST`, `0x52 = TENSOR_REDUCE_BITVEC_OP`** (the bitvec half of the
> `S4D4_TR` Vector/Pool Tensor-Reduce, arith half `0x42`) — none of which is a GPSIMD reduce and none of
> which is pooling. **Pooling is `0x45` only.** This page does not propagate the retracted attribution.
> `[HIGH/OBSERVED — common.h opcode map]`

---

## 9. Cross-image / cross-gen stability

* `decode_pool` sits in the `0xa260` `EXTISA_0` POOL image shared CAYMAN / MARIANA / MARIANA_PLUS; the
  MARIANA carve carries the **identical** `_Z11decode_poolb` `.xt.prop` section at the same function VMA
  `0x01000bc0` and the same dispatch. The mangled symbol occurs **6×** across the embedded POOL blobs of
  `libnrtucode_extisa.so` (one per generation/variant blob). `[HIGH/OBSERVED symbol census; CARRIED per-VMA]`
* The `S4D4_PL` struct (64 B), the `POOL_TYPE` enum `{NONE 0, MAX 1, AVG 2}`, `pool_func@36` / `pool_dim@37`
  / `pool_scale@40`, and the full 64-byte layout are **byte-identical across the CAYMAN, MARIANA, MAVERICK,
  and SUNDA arch-isa headers** (`diff` of the enum and struct bodies = identical).
  Opcode `0x45 = Pool` is stable across all four gens. `instruction_mapping.json` binds `S4D4_PL →
  {POOL, MAX_POOL_SELECT}` identically in the CAYMAN and SUNDA maps.
* The firmware core-kind assert at `internal.so` `0x4b9a` enumerates all five POOL cores —
  `SUNDA_NX_POOL`, `CAYMAN_NX_POOL`, `MARIANA_NX_POOL`, `MARIANA_PLUS_NX_POOL`, `MAVERICK_NX_POOL` — the
  cross-gen breadth of the same pool ucode.

> **NOTE — v5 / MAVERICK.** The MAVERICK (NC-v5) arch-isa header exposes the identical `S4D4_PL` struct,
> `POOL_TYPE` enum, and `0x45` opcode (**header-OBSERVED**), but the v5 kernel *interior* (the `decode_pool`
> body micro-schedule) is not byte-grounded — the v2–v4 (cayman/mariana/sunda) device bodies are
> the byte-verified substrate; the v5 interior is `[INFERRED]` from header + opcode-map parity.
> `[HIGH header / INFERRED interior]`

---

## 10. Adversarial self-verification — five strongest claims re-challenged

1. **"`S4D4_PL` is 64 bytes with `pool_func@36`, `pool_dim@37`, `pool_scale@40`."** Re-challenged by
   *compiling* the header struct: `gcc -std=c11` reports `sizeof == 64`, `sizeof(TENSOR4D) == 20`,
   `sizeof(POOL_TYPE) == 1`, and offsets `header@0 events@4 src@12 in_dtype@32 out_dtype@33
   num_active_channels@34 reserved0@35 pool_func@36 pool_dim@37 reserved1@38 pool_scale@40 dst@44`. The
   header's `ISA_STATIC_ASSERT(sizeof == 64)` holds. **Holds — compile-verified.**
2. **"`POOL_TYPE = {NONE 0, MAX_POOL 1, AVG_POOL 2}`, a closed 3-value enum."** Re-challenged: read
   byte-exact from `aws_neuron_isa_tpb_s4d4_pl.h:277-281`; `diff` confirms it is **identical across
   cayman/sunda/maverick**; the enum closes at `0x2`. **Holds.**
3. **"Windowing is SW-streamed window-major; `period` = `pool_size`, `num` = #outputs."** Re-challenged
   against the header §"Pool instruction behavior" (the worked 4×4 → 2×2 example with explicit
   `x/y/z/w_num`/`step`, `pool_size = x_num · y_num = 4`) and the DEBUG strings `"Running max_pool with
   period = %0d and num = %0d"` (`0x269a1f`). The header *names the SW-stream dependency verbatim*.
   **Holds.**
4. **"avg-divide = × `pool_scale`(1/N) fp32 multiply, NOT a device divide and NOT the batch-norm Param-RAM
   reciprocal."** Re-challenged against the header §`pool_scale` verbatim (*"1/(RS) … 0.25 for 2x2 …
   to … scale the input values and then accumulate, without the need to count"*) and §Notes (the
   scale-before/after-sum license), plus `s4d4_pool_scale_valid` forcing `1.0` for non-avg ops, plus the
   `ivp_muln_2xf32t` op presence. No division/reciprocal-seed primitive is in the pool path. **Holds.**

5. **"dtype set: IN `{UINT8,INT32,FP16,BF16,FP32,INT64}`, OUT `{UINT8,INT32,FP16,BF16,FP32}`, calc always
   fp32."** Re-challenged against the header §in/out_dtype (*"Supported input dtypes are
   UINT8/INT32/FP16/BFLOAT16/FP32/INT64 … Supported output dtypes are UINT8/INT32/FP16/BFLOAT16/FP32"*) and
   the recommended-usage table (`CALC_DTYPE = FP32` on every row; INT64 not writable to SB/PSUM).
   **Holds.**

**Flagged items.** (i) The `decode_pool` **body mid-bundle arms** are FLIX-desynced under stock objdump's
linear sweep (~25 % `.byte` in the `0xbc0..0xdff` window); the `pool_func` switch, the period/num loops, and
the in-bundle max/sum/scale ops are reported *structurally* from the `.xt.prop` records + DEBUG strings +
header, not byte-pinned — `[MED/INFERRED]`. (ii) The `period↔register` / `num↔register` exact binding is in
the desynced body — the format-string args are HIGH, the live regs are MED. (iii) `MaxPoolSelect (0x58)`
HW-datapath routing is `[INFERRED-HIGH]` (absence in Q7 ucode + argmax/training semantics). (iv) The
`ivp_*` in-body slot order is desynced — the op *set* is pinned (§5.3), the in-`decode_pool` slot order is
not. (v) **v5/MAVERICK interior** is header-OBSERVED / interior-INFERRED (§9). The funcVA `0x01000bc0` /
`kernel_info_table` idx3 / trampoline bytes are CARRIED from the prior carve (the IDA host-lib disasm
does not cover the embedded Xtensa POOL blobs).

---

## 11. Cross-references

* [`decode_pool` (the "Pool" kernel disambiguation)](decode-pool.md) — the POOL-engine-vs-pool-op naming
  arbitration and the full dispatch chain (`'E'` → `kernel_info_table` `0x45` → `decode_pool`); this page is
  the avg/max **compute** that `decode_pool` runs.
* [CrossLaneReduce](cross-lane-reduce.md) — the **intra-register / cross-lane** reduce (`0x7c`/`0x7d`,
  `S4D4_CR`, `REDUCE_OP`); the *orthogonal* reduce geometry contrasted in §8 (lane-fold vs window-fold).
* [Tensor-Reduce (cross-partition)](tensor-reduce.md) — the cross-partition altitude of the CrossLaneReduce
  kernel, and the byte-exact correction of the `0x46`/`0x47`/`0x52` opcode attribution this page does not
  propagate (§8 CORRECTION).
* [ISA Batch 08 — Cross-Lane Reduce](../../isa/ref/b08-reduce.md) — the per-instruction reference for the
  `ivp_r*` reduce mnemonics CrossLaneReduce issues; pooling uses the *element* primitives (`ivp_maxnxf16t`,
  `ivp_muln_2xf32t`), not the `r`-prefix lane-fold ops — the cleanest mnemonic-level proof of the §8
  geometric distinction.

---

*Provenance: the `Pool`/`MaxPoolSelect` opcodes (`0x45`/`0x58`), the `S4D4_PL` struct, the `POOL_TYPE`
enum, the `TENSOR_SUBDIM`/`DTYPE` encodings, `POOLING_NUM_CHANNELS = 128`, and the validity/behavior
predicates are read byte-exact from the CAYMAN/MARIANA/MAVERICK/SUNDA arch-isa interface headers
(`aws_neuron_isa_tpb_s4d4_pl.h`, `aws_neuron_isa_tpb_common.h`) shipped in the
`aws-neuronx-gpsimd-customop-lib` package, cross-checked against `instruction_mapping.json`
(`struct2opcode`: `S4D4_PL → {POOL, MAX_POOL_SELECT}`). The `S4D4_PL` `sizeof == 64` and every field offset
are **compile-verified** with `gcc`. The `decode_pool` symbol (`_Z11decode_poolb`) is read from
the carved POOL `EXTISA` blobs of `libnrtucode_extisa.so`; the `P%i:` Pool DEBUG runner strings are read at
the cited byte offsets (`0x2699db..0x269bd1`) of the shipped `libnrtucode_internal.so` `.rodata` with stock
`strings`/`xxd`. The funcVA `0x01000bc0` / `kernel_info_table` idx3 binding / trampoline disasm are carried
from a prior carved-blob `xtensa-elf-objdump` pass (`XTENSA_CORE=ncore2gp`); the per-element value
semantics are carried from the `libfiss-base.so` `xdref` oracle. The `extracted/` tree is
gitignored (reached with absolute paths). `.text` VMA == file-offset; config-DLL `.data.rel.ro` file =
VMA − `0x200000` (`readelf -SW`-confirmed). All prose is derived from static analysis of the shipped
artifacts only; nothing here is read from a vendor source tree.*
