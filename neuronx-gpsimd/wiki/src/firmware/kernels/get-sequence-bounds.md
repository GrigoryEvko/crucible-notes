# GetSequenceBounds

This page decodes the **GetSequenceBounds** kernel of the NeuronCore GPSIMD ISA — the
**POOL-codec** *sequence min/max bounds-finder* (opcode **`0xbe`**) that scans **one**
3-D source tensor of `INT32` or `FP32` elements and writes the `{min, max}` envelope of
that sequence into a destination tensor holding **exactly 2×** the source element count,
with the highest destination dimension **pinned to 2** (the min/max pair). It pins the
opcode (`0xbe`), the 64-byte `NEURON_ISA_TPB_S3D3_SEQ_BOUNDS_STRUCT` operand struct
byte-for-byte from the shipped ISA header (compile-verified `sizeof == 64` on all three
supporting generations), the **DTYPE-keyed two-leg datapath** recovered from
the carved POOL firmware (an integer min/max leg vs an ordered-float-compare leg), the
**`{INT32, FP32}` hard-whitelist dtype gate** (`has_valid_seq_bounds_dtype`, both
`in_dtype` *and* `out_dtype`), the per-dtype valid-bounds contract
(`num_active_channels == 1`, `2 * t3d_count(src) == t3d_count(dst)`,
`dst.num_elem[2] == 2`, SBUF-only, reserved-zero), and the
`kernel_info_table → trampoline → body` dispatch chain.

GetSequenceBounds runs on the **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW
DSP** (`ncore2gp` config, one per NeuronCore) — specifically on the **POOL** (Pooling)
sequencer, in *both* the engine's MAIN image and its POOL-codec sub-image. Its self-name
strings (`"P%i: GetSequenceBounds : num_chans = %0d"` at decode and
`"P%i: Decode : GetSequenceBounds : active_chans = %d"`) are ASCII-baked in the
CAYMAN/MARIANA/MARIANA_PLUS POOL DEBUG DRAM and are themselves binary evidence. This places
it as the immediate `.xt.prop` predecessor and POOL **codec-family** sibling of
[NonzeroWithCount](./nonzero-with-count.md) (the `0xf2` index-compaction-with-count op
that follows it in section order). Both compose from the same compare + lane-select
ncore2gp ISA primitives, and both instantiate their scan helpers once per
`{int, float}` — the structural fingerprint of the int-vs-float dtype dichotomy this
page documents.

> **NOTE — GetSequenceBounds (data bounds) is NOT PC-Bounds (instruction-pointer range).**
> Reimplementers routinely conflate two unrelated "bounds" mechanisms because the names
> collide. **GetSequenceBounds** (this page, opcode `0xbe`) computes the *data* `{min, max}`
> of a *value* sequence in SBUF — it is a compute kernel that reads tensor data and emits
> two reduced values. **PC-Bounds** (see [SEQ PC-Bounds Enforcement + Host
> API](../seq/pc-bounds.md)) is the SEQ sequencer's *instruction-pointer range
> enforcement* — a hardware watchdog that traps the program counter when it leaves the
> permitted instruction window. They share zero code, zero structs, and zero opcodes:
> `0xbe`/`S3D3_SEQ_BOUNDS`/POOL-engine data-reduction here, versus the SEQ
> control-register/IP-window machinery there. The word "bounds" is the only thing they
> have in common. Do not wire the data-bounds kernel into the IP-range trap or vice versa.

---

## 1. Identity, opcode, and the dispatch chain

GetSequenceBounds is a **first-class POOL-engine ISA instruction**, not a private helper.
Three independent anchors — the demangled `.xt.prop` symbol, the compiled opcode/struct,
and the baked DEBUG self-name strings — converge on the same instruction (all HIGH/OBSERVED):

| Anchor | Value | Source |
|---|---|---|
| Opcode | `NEURON_ISA_TPB_OPCODE_GET_SEQUENCE_BOUNDS = 0xbe` | `common.h:261` |
| Operand struct | `NEURON_ISA_TPB_S3D3_SEQ_BOUNDS_STRUCT` (64 B) | `s3d3_seq_bounds.h:19-31` |
| `0xbe → struct` binding | `instruction_mapping.json:259-260` | compile + JSON |
| Device symbol (mangled) | `_Z24get_sequence_bounds_implj20NEURON_ISA_TPB_DTYPE` | `.xt.prop` section name |
| Device symbol (demangled) | `get_sequence_bounds_impl(unsigned int, NEURON_ISA_TPB_DTYPE)` | `c++filt` |
| DEBUG self-name (decode) | `"P%i: GetSequenceBounds : num_chans = %0d"` @ `0x1be4` | POOL DEBUG DRAM |
| DEBUG self-name (active) | `"P%i: Decode : GetSequenceBounds : active_chans = %d"` @ `0x1c0e` | POOL DEBUG DRAM |

The mangled symbol is the single most informative anchor: the suffix
`j20NEURON_ISA_TPB_DTYPE` proves the device kernel takes **two by-value arguments** —
a `j` (`unsigned int`) and a `20NEURON_ISA_TPB_DTYPE` (the enum by value, length-20 name).
The `unsigned int` is the element/loop count the decoder hands to the kernel; **the
`NEURON_ISA_TPB_DTYPE` is the dispatch key passed as a function argument**, not a struct
byte re-read inside a generic loop. The DTYPE-keyed dispatch is therefore an explicit C++
function parameter — the body branches on register `a3` (the windowed-ABI second arg) into
one of two compute legs.

> **GOTCHA — the `.xt.prop` section name *is* the symbol table.** The carved POOL images
> have no `.symtab`; the function name survives only inside the per-function
> `.xt.prop._Z24get_sequence_bounds_implj20NEURON_ISA_TPB_DTYPE` section. Recovering the
> name (and thus the by-value DTYPE parameter) requires `readelf -SW` over the `.xt.prop`
> section list, not `nm`. The same `0x18c`-byte prop record block appears in CAYMAN_0,
> CAYMAN_3, and MARIANA_PLUS — identical prop-table size implies an identical body.

### 1.1 The `kernel_info_table` → trampoline → body chain (byte-exact)

The Q7 POOL dispatcher uses the FW-18 `kernel_info_table` record format:

```c
// kernel_info_table record (FW-18 format) — packed 8 bytes
struct kernel_info_record {
    uint8_t  zero0;     // 0
    uint8_t  zero1;     // 0
    uint8_t  spec;      // engine/spec selector
    uint8_t  opcode;    // <== the ISA opcode, 0xbe here
    uint32_t funcVA;    // little-endian entry-trampoline VA
};
```

The dispatcher linear-scans the packed `(spec, opcode)` key, then `callx8 funcVA` into a
small **entry trampoline**; the trampoline does FLIX register setup and then `callx8` again
into the **body** (the address the `.xt.prop` func-start names). Byte-exact:

| Image | Table location | Index | `{opcode, funcVA}` | Trampoline | Body |
|---|---|---|---|---|---|
| CAYMAN_0 (MAIN) | file `0x7400`, 17 entries | idx 14 | `{0xbe, 0x01004204}` | `0x01004204` | `0x01004284` |
| CAYMAN_3 (codec) | file `0x4748`, 9 entries | idx 4 | `{0xbe, 0x01000dac}` | `0x01000dac` | `0x01000e2c` |
| MAVERICK_0 (stripped) | rel `kernel_info` | — | `{0xbe, rel 0x4770}` | rel `0x4770` | (rel interior) |

The CAYMAN_3 codec table's siblings at idx 0..8 are the POOL codec set
(`0x7e`/`0x7c`/`0x7d` clear+reduce, `0x45` pool, `0xf2` NonzeroWithCount, `0x7b` dequant,
`0xe4`, `0x07f0`) — GetSequenceBounds is one of nine codec entries. The CAYMAN_0 MAIN
table registers it at idx 14, so the op is **present in both images** (this corrects the
implicit "codec-image-only" framing of the earlier Cast/Copy survey).

The trampoline tail is the standard `const16`-built indirect call (the
`decode_pool`/FW-70 pattern):

```asm
; CAYMAN_0 trampoline @0x01004204
entry   a1, 32              ; FLIX windowed-ABI frame setup (partial desync)
...                         ; const16 partial-prop setup
const16 a2, 0x100           ; build high half of body VA
const16 a2, 0x4284          ; build low half  -> a2 = 0x01004284
callx8  a2                  ; => body get_sequence_bounds_impl @0x01004284

; CAYMAN_3 trampoline @0x01000dac
entry   a1, 32
...
const16 a2, 0x100
const16 a2, 0xe2c           ; a2 = 0x01000e2c
callx8  a2                  ; => body @0x01000e2c
```

The body func-start (`0x01004284` MAIN / `0x01000e2c` codec) equals the `.xt.prop`
func-start in each image. `.text` VMA `0x01000000` maps to file offset `0x100`
(`file_off = VMA - 0x01000000 + 0x100`).

> **GOTCHA — `.text`/`.rodata` are VMA==fileoffset, but the ncore2gp config DLLs'
> `.data`/`.data.rel.ro` are offset by `0x200000`.** When parsing the `kernel_info_table`
> or any `.data`-resident dispatch struct, confirm the section base per-image with
> `readelf -SW <image>` and subtract the right delta. The POOL `.text` dispatch above uses
> the `0x100` `.text` mapping; do **not** over-generalize a single delta across sections.

---

## 2. The operand struct — `NEURON_ISA_TPB_S3D3_SEQ_BOUNDS_STRUCT` (64 B)

The `instruction_mapping.json` binds opcode `0xbe` (`GET_SEQUENCE_BOUNDS`) to exactly one
struct. Recompiled (`gcc -std=c11 -I<hdr>`, `offsetof`/`sizeof`; the header's
`ISA_STATIC_ASSERT(sizeof == 64)` passes):

| off | size | field | type | role |
|---|---|---|---|---|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{ opcode=0xbe, inst_word_len, … }` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync |
| 12 | 1 | `num_active_channels` | `uint8_t` | **MUST == 1** (single-partition) |
| 13 | 1 | `reserved0[1]` | `uint8_t` | MUST be 0 |
| 14 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` | **MUST ∈ {INT32(0x8), FP32(0xA)}** |
| 15 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | **MUST ∈ {INT32(0x8), FP32(0xA)}** |
| 16 | 16 | `src_mem_pattern` | `NEURON_ISA_TPB_TENSOR3D` | INPUT seq (read-only, SBUF) |
| 32 | 16 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR3D` | OUTPUT `{min,max}` (write, SBUF) |
| 48 | 16 | `reserved1[16]` | `uint8_t[16]` | ALL 16 MUST be 0 |

```text
verify_seqb (gcc 16.1.1, -std=c11) — OBSERVED:
  sizeof=64
  num_active@12 in_dtype@14 out_dtype@15 src@16 dst@32 reserved1@48
  sizeof(TENSOR3D)=16 sizeof(DTYPE)=1 sizeof(HEADER)=4 sizeof(EVENTS)=8
  opcode=0xbe INT32=0x8 FP32=0xa FP32R=0xb POOLING_NUM_CHANNELS=128
```

The embedded `NEURON_ISA_TPB_TENSOR3D` is the standard 3-D strided descriptor
(`common.h:649`):

```c
typedef struct NEURON_ISA_TPB_TENSOR3D {  // 16 B
    NEURON_ISA_TPB_ADDR4 start_addr;    //  4  ( 0 -  3)  SBUF partition-offset base
    int16_t              step_elem[3];  //  6  ( 4 -  9)  per-dim signed stride
    uint16_t             num_elem[3];   //  6  (10 - 15)  per-dim element count
} NEURON_ISA_TPB_TENSOR3D;
```

> **NOTE — `in_dtype`/`out_dtype` are full bytes at off 14/15, not a packed nibble pair.**
> Some sibling structs co-pack the two dtypes into one byte; S3D3_SEQ_BOUNDS does **not** —
> read `in_dtype` from byte 14 and `out_dtype` from byte 15 directly. The struct is
> **S3D3** (one 3-D src + one 3-D dst), distinct from the **S4D4_TR** (4-D, tensor-reduce)
> family that Cast/Copy/TensorReduce use. It shares the *S3D3-NonZero* descriptor shape
> with its codec neighbor `S3D3_NONZERO_WITH_COUNT` — which is why
> [NonzeroWithCount](./nonzero-with-count.md) (`0xf2`) is its immediate `.xt.prop`
> follower.

> **QUIRK — `dst` is sized for the min/max *pair* expansion.** Unlike a normal reduce
> (which writes one scalar), GetSequenceBounds writes a *2-element* output per the
> `2 * t3d_count(src) == t3d_count(dst)` and `dst.num_elem[2] == 2` rules. The header
> comment (`s3d3_seq_bounds.h:104-109`) states it verbatim: *"We need dst_mem_pattern to
> hold 2x more elements than src_mem_pattern, since we are outputting min and max
> bounds."* The "strictly 2" highest dim is reserved *"in case we ever want to support
> sequence length not a multiple of num_active_channels in the future."*

---

## 3. The DTYPE-keyed bounds computation — two compute legs

### 3.1 The dtype is the dispatch key (a function parameter)

The demangled signature `get_sequence_bounds_impl(unsigned int, NEURON_ISA_TPB_DTYPE)`
takes the dtype **by value** (`a2` = `uint` element/loop count, `a3` = dtype). The body
branches on `a3` into one of two min/max legs. Because the ISA validity gate (§4) already
restricts both `in_dtype` and `out_dtype` to `{INT32, FP32}`, the in-body dtype switch is a
clean 2-way: *is-INT32* vs *is-FP32*. The exact in-body `bnei`/`beqi` arm bytes are MED
(the body is hand-scheduled FLIX with interleaved literal spans that desync stock
`xtensa-elf-objdump`; see §5), but the by-value DTYPE parameter and the two-leg structure
are HIGH/OBSERVED.

### 3.2 The IVP value-primitive census (CAYMAN_3 body, merged-prop scan)

A merged-prop code-mode resync of the body recovers both an **integer min/max** vocabulary
and a **float-compare** vocabulary — the decisive evidence of the INT32/FP32 split:

| Leg | Primitive | Operation | Role |
|---|---|---|---|
| INT32 | `ivp_maxun_2x32` | unsigned 32-bit vector MAX | the i32 max-bound |
| INT32 | `ivp_bmaxnx16` | signed int16 vector MAX | 16-bit-lane max-bound |
| INT32 | `ivp_babssubunx16` | unsigned `\|a-b\|` | range/spread helper |
| FP32 | `ivp_ultn_2xf32t` | fp32 unordered less-than | the fp32 compare |
| FP32 | `ivp_oltnxf16` | fp16 ordered less-than | fp narrowed compare |
| FP32 | `ivp_uneqnxf16t` | fp16 unordered not-equal | fp compare helper |
| both | `ivp_dselnx16t` (×3) / `ivp_sel2nx8i` / `ivp_sel2nx8i_s4` | dual-select lane MOVE | packs the `{min,max}` pair into the 2× dst (`num_elem[2]==2`) |
| both | `ivp_lavnx8u_xp` / `ivp_svnx8s_i` | strided vector LOAD / STORE | streams the src / writes the dst |
| both | `ivp_mulus4ta2n8xr8` / `ivp_mulusp2n8xr16` | index/stride scaling | address generation |

The INT32 leg builds its max with `ivp_maxun_2x32` / `ivp_bmaxnx16` and its spread/min via
the swapped compare (and `ivp_babssubunx16` as a range helper); the FP32 leg uses the
ordered/unordered float compare (`ivp_oltnxf16` / `ivp_ultn_2xf32t`) to extract the lower
and upper bound. Both outputs are SELECT-packed (`ivp_dselnx16t`) into the 2× destination.

> **GOTCHA — the FP32 *max* is direct, the FP32 *min* is INFERRED via the swapped compare.**
> The body has direct vector-max primitives (`ivp_maxun_2x32`, `ivp_bmaxnx16`) but no
> matching named vector-min in the recovered clean bundles. The min is realized by the
> compare-and-swap (`ivp_ult*`/`ivp_olt*` then select), not a `vmin` opcode. This is
> MED/INFERRED — the compare-op presence is OBSERVED, the exact min derivation is the
> swapped-compare reading.

### 3.3 The reduction, as annotated C pseudocode

The header-grounded semantics plus the recovered IVP set yield the following reconstruction
of `get_sequence_bounds_impl`. The `uint elem_count` is the kernel's first argument; the
`dtype` is the second (the dispatch key). Loads/stores are strided per
`src_mem_pattern`/`dst_mem_pattern`:

```c
// get_sequence_bounds_impl(unsigned int elem_count, NEURON_ISA_TPB_DTYPE dtype)
//   reads one INT32-or-FP32 src Tensor3d (num_active_channels == 1),
//   writes dst = { min(src), max(src) } with dst element count == 2 * src element count
//   and dst.num_elem[2] == 2.  HIGH structure / MED per-arm slot order (FLIX desync).
void get_sequence_bounds_impl(unsigned int elem_count, NEURON_ISA_TPB_DTYPE dtype)
{
    // a3 == dtype: the body branches HERE (the 2-value whitelist is enforced by §4 already).
    if (dtype == NEURON_ISA_TPB_DTYPE_INT32) {           // 0x8 — integer leg
        ivp_int32x16 vmin = INT32_MAX_SPLAT;             // seed
        ivp_int32x16 vmax = INT32_MIN_SPLAT;
        for (unsigned i = 0; i < elem_count; i += LANES) {
            ivp_int32x16 v = ivp_lavnx8u_xp(src_ptr);    // strided vector load
            vmax = ivp_maxun_2x32(vmax, v);              // OBSERVED: i32 vector max
            vmin = ivp_maxun_2x32(vmin, ivp_neg(v));     // min via swapped/negated max (INFERRED)
            // (ivp_bmaxnx16 used for the 16-bit-lane reduction stage;
            //  ivp_babssubunx16 forms the spread for the lane-tree reduce)
        }
        // horizontal reduce the lanes of vmin/vmax to a single {min,max}
        int32_t mn = lane_reduce_min(ivp_neg(vmin));
        int32_t mx = lane_reduce_max(vmax);
        // pack the PAIR into the 2x dst (the num_elem[2]==2 axis):
        ivp_svnx8s_i(dst_ptr, ivp_dselnx16t(mn, mx));    // OBSERVED: dual-select pair-pack
    } else { /* dtype == FP32 (0xA) — only other gate-allowed value */
        ivp_floatx16 vmin = FP32_PLUS_INF_SPLAT;
        ivp_floatx16 vmax = FP32_MINUS_INF_SPLAT;
        for (unsigned i = 0; i < elem_count; i += LANES) {
            ivp_floatx16 v  = ivp_lavnx8u_xp(src_ptr);
            ivp_vbool lt    = ivp_oltnxf16(v, vmin);     // OBSERVED: ordered fp less-than
            vmin            = ivp_movf(vmin, v, lt);     // select-min (compare-and-swap)
            ivp_vbool gt    = ivp_ultn_2xf32t(vmax, v);  // OBSERVED: fp compare for max
            vmax            = ivp_movf(vmax, v, gt);
        }
        float mn = lane_reduce_fmin(vmin);
        float mx = lane_reduce_fmax(vmax);
        ivp_svnx8s_i(dst_ptr, ivp_dselnx16t(mn, mx));    // {min,max} -> 2x dst
    }
    // On-device DEBUG: logs "GetSequenceBounds : num_chans = %0d" (== 1) at entry.
}
```

> **NOTE — single active channel ⇒ the reduce runs over ONE partition's stream.** Because
> `num_active_channels == 1` is a hard validity rule (§4), the loop reduces along the
> Tensor3d stream of a *single* partition. The op produces the lower+upper envelope of one
> sequence per call. Whether the kernel additionally loops the single channel's period is
> read from the load/store IVPs + the `num_chans=%0d` (==1) log, not a byte-pinned loop
> bound (MED loop, HIGH `==1` gate).

### 3.4 The codec-neighbor template (corroborating the int/float split)

The `.xt.prop` section immediately following `get_sequence_bounds_impl` is the pair (both
demangled and confirmed present in the carved `libnrtucode` POOL images):

```text
_Z23nonzero_with_count_implIfEvjiijj  = nonzero_with_count_impl<float>(...)
_Z23nonzero_with_count_implIiEvjiijj  = nonzero_with_count_impl<int>(...)
```

The same POOL codec module instantiates its sequence-scan helpers **once per `{float,
int}`** — exactly the `{INT32, FP32}` partition GetSequenceBounds gates to. This is
structural confirmation that the codec module's sequence ops are dtype-keyed on the
int-vs-float dichotomy, not the full 16-dtype space. See
[NonzeroWithCount](./nonzero-with-count.md) (`0xf2`) for the full template and
[The Unified Datatype Model](./dtype-model.md) for the ordinal map and the engine-wide
dtype dispatch philosophy.

---

## 4. The validity / bounds contract — the per-dtype valid bounds

The "per-dtype bounds table" reimplementers must enforce *is* the
`is_valid_get_sequence_bounds` predicate, read verbatim from the header (all comment
clauses; the generator's Rust-pseudocode), **identical across cayman/mariana/maverick**
(the three seq_bounds headers are semantically identical modulo the NC-version comment
line). HIGH/OBSERVED.

```rust
fn is_valid_get_sequence_bounds(i: Inst, nc: NeuronCoreVersion) -> bool {
       has_valid_neuron_header(i)
    && has_valid_neuron_events(i)
    && has_get_sequence_bounds_opcode(i)                      // header.opcode == 0xbe
    && has_valid_nc_get_sequence_bounds(nc)                   // nc >= V3
    && check_active_channels(i.s3d3_seq_bounds.num_active_channels)        // != 0 && <= 128
    && has_valid_s3d3_seq_bounds_channels(i.s3d3_seq_bounds.num_active_channels)  // == 1
    && s3d3_seq_bounds_reserved_zero(i)                       // reserved0[0]==0 && reserved1[0..15]==0
    && is_valid_dtype(i.s3d3_seq_bounds.in_dtype,  DtypeAllowFP32R::False)
    && is_valid_dtype(i.s3d3_seq_bounds.out_dtype, DtypeAllowFP32R::True)
    && has_valid_seq_bounds_dtype(i)                          // in&out in {FP32, INT32}
    && start_addr_active_channels(i.s3d3_seq_bounds.src_mem_pattern.start_addr, num_active)
    && start_addr_active_channels(i.s3d3_seq_bounds.dst_mem_pattern.start_addr, num_active)
    && tensor3d_valid(src, in_dtype,  WriteTensor::False, AllowedInPSUM::False, AllowedInSBUF::True)
    && tensor3d_valid(dst, out_dtype, WriteTensor::True,  AllowedInPSUM::False, AllowedInSBUF::True)
    && s3d3_seq_bounds_element_count_check_t3d(i)             // 2*t3d(src)==t3d(dst) && dst.num_elem[2]==2
}
```

Clause-by-clause (each HIGH/OBSERVED, read verbatim 3 gens):

| Clause | Rule | Why it matters |
|---|---|---|
| `has_get_sequence_bounds_opcode` | `header.opcode == 0xbe` | the opcode pin |
| `has_valid_nc_get_sequence_bounds` | `nc >= V3` (CAYMAN+; SUNDA no) | per-gen presence |
| `check_active_channels` | `num_active != 0 && num <= 128` | generic channel sanity |
| `has_valid_s3d3_seq_bounds_channels` | **`num_active_channels == 1`** | the binding channel bound — single-partition |
| `s3d3_seq_bounds_reserved_zero` | `reserved0[0]==0 && reserved1[0..15]==0` | 17 reserved bytes must be zero |
| `is_valid_dtype(in, FP32R=False)` | `in` ∈ valid set minus FP32R/64b | generic dtype sanity (in) |
| `is_valid_dtype(out, FP32R=True)` | `out` ∈ valid set (+FP32R allowed) | generic dtype sanity (out) |
| `has_valid_seq_bounds_dtype` | **`in ∈ {FP32,INT32}` AND `out ∈ {FP32,INT32}`** | the strong dtype whitelist |
| `start_addr_active_channels` (src,dst) | partition-aligned start addrs | SBUF placement alignment |
| `tensor3d_valid(src,…)` | read-only, **PSUM=False, SBUF=True** | src is SBUF-only, read |
| `tensor3d_valid(dst,…)` | write, **PSUM=False, SBUF=True** | dst is SBUF-only, write |
| `s3d3_seq_bounds_element_count_check_t3d` | **`2*t3d(src) == t3d(dst)` AND `dst.num_elem[2] == 2`** | the 2× pair-expansion |

Where (`common.h`):

```c
// t3d_element_count(t) = t.num_elem[0] * t.num_elem[1] * t.num_elem[2]   (each uint16)
```

**The numeric bounds derived from the field widths and the predicate:**

- **Max element count** — each `num_elem[*]` is `uint16` (per-dim cap 65535). The src
  product must satisfy `2*product == dst product` with the dst dims also `uint16`; since
  `dst.num_elem[2]` is fixed at 2, the 2× spread lives in dst dims 0/1. Practical src cap
  is whatever keeps `2*count` inside the dst `uint16` dims.
- **Channel bound** — `num_active_channels == 1` (the single decisive per-op bound — NOT
  `1..128`).
- **2× output size** — `2 * t3d_count(src) == t3d_count(dst)` AND `dst.num_elem[2] == 2`.
- **Stride/period limit** — `step_elem[*]` are `int16` (`-32768..32767`) per dim.
- **Placement** — SBUF-only (`AllowedInPSUM=False`), partition-aligned start addrs.
- **Alignment/multiple** — there is **no** channel-multiple rule (unlike S4D4_TR pooling);
  the only multiple constraint is the 2× dst-vs-src + `num_elem[2]==2` relation.

### 4.1 The per-dtype "table" (strongest form: a 2-row whitelist)

Every dtype outside `{INT32, FP32}` is **gate-rejected** at `has_valid_seq_bounds_dtype`,
**before** the kernel body runs:

| DTYPE | in? | out? | max_count | channel | alignment/multiple | compute leg |
|---|---|---|---|---|---|---|
| `INT32` (0x8) | Y | Y | per-dim `uint16` product (≤65535) | ==1 | dst=2×src, `dst.num_elem[2]==2` | int min/max (`maxun_2x32`/`bmaxnx16`) |
| `FP32` (0xA) | Y | Y | same | ==1 | same | fp ordered compare (`oltn`/`ultn_2xf32`) |
| `INT8`/`UINT8`/`INT16`/`UINT16`/`UINT32` | – | – | gate-rejected | | | |
| `FP16`/`BFLOAT16` | – | – | gate-rejected | | | |
| `FP8_E3`/`E4`/`E5` | – | – | gate-rejected | | | |
| `FP32R` (0xB) | – | – | rejected (even though `is_valid_dtype` *out*-allows it — the whitelist still excludes it) | | | |
| `INT64`/`UINT64` | – | – | rejected (`is_valid_dtype` excludes 64-bit *and* the whitelist would too) | | | |
| `FP4_EXP2` (0x10)/`INT4` (0x12)/`SFP8_E5..E8` (0x13-0x16)/`CPTC1..7` (0x19-0x1F) (MAVERICK) | – | – | rejected — the MX dtypes do NOT widen the gate | | | |

> **CORRECTION — the dtype gate is the *narrowest* of any decoded GPSIMD kernel, and it is
> NOT a dtype-*width* dispatch.** Earlier dtype-dispatch synthesis treated POOL-codec ops
> as keyed over the dtype *width band* (the full 16-dtype space). GetSequenceBounds is not:
> `has_valid_seq_bounds_dtype` is a hard **2-value whitelist** `{INT32(0x8), FP32(0xA)}`,
> applied to *both* `in_dtype` and `out_dtype`. The dtype-width *principle* (a kernel keys
> on dtype) holds; the *surface* is a 2-value whitelist, not a band. The MAVERICK MX
> additions (`FP4_EXP2`, `INT4`, `SFP8_*`, `CPTC*`) are verified present in MAVERICK's
> `common.h` DTYPE enum but are **not** admitted by the seq_bounds whitelist — confirmed
> byte-identical predicate across all three gens.

### 4.2 The error path

The host-side decoder runs `dbg_is_valid_get_sequence_bounds(i, nc)`
(`debug_assert.h:390-391`, `case NEURON_ISA_TPB_OPCODE_GET_SEQUENCE_BOUNDS`) — the full
validity predicate above. A failing instruction — wrong dtype (anything outside
`{INT32,FP32}` on either `in_dtype` or `out_dtype`), `num_active != 1`, wrong 2× count or
`dst.num_elem[2] != 2`, nonzero reserved bytes, a PSUM operand, or `nc < V3` — **fails the
assert at decode, before dispatch**. On-device, the kernel's own DEBUG entry logs
`"GetSequenceBounds : num_chans = %0d"` (@`0x1be4`) and
`"Decode : GetSequenceBounds : active_chans = %d"` (@`0x1c0e`); validity is enforced by the
decode-time assert, not a separate in-kernel "Error" string (no seq_bounds-specific error
string was found in the carved DEBUG DRAM). HIGH for the `debug_assert` binding + the DEBUG
entry strings; the on-device assert-arm is MED.

---

## 5. Honest limitations — FLIX-desync flags

- **FLIX-literal desync (the body).** The `get_sequence_bounds_impl` body
  (`0x01000e2c..0x010013e2` CAYMAN_3 / `0x01004284…` CAYMAN_0) is hand-scheduled FLIX VLIW
  with 6 interleaved literal/data spans that desync stock `xtensa-elf-objdump`'s linear
  sweep (~25% `.byte`). The `.xt.prop` records (12 B each: `[addr LE32][size LE32][flags
  LE32]`; `0x2804`=func-start, `0x08`=literal/data span, `0xa2/0x82/0x92`=FLIX code span)
  recover the func-start, span boundaries, and prologue (HIGH/OBSERVED), and a merged-prop
  code-mode resync recovers many clean bundles (the §3.2 IVP census is from those) — but
  does **not** cleanly walk every dtype-switch arm. The mis-decoded `.byte` / spurious
  jump targets (e.g. stock-objdump `l32r 0xfdefc4` literal-pool mislabels) are **not**
  reported as real.
  - **HIGH/OBSERVED:** the `.xt.prop` func-start + span boundaries; the `kernel_info_table`
    bytes; the trampoline→body bridge (`const16`-built `callx8`); the demangled symbol (the
    by-value DTYPE param); the IVP primitive *set* (both int + fp min/max/compare present);
    the DEBUG self-name strings; the compile-verified struct + opcode + dtype gate.
  - **MED/INFERRED:** the exact in-body `INT32`-vs-`FP32` `bnei`/`beqi` arm bytes; the
    min/max reduce-loop slot order; the FP32-min-via-swapped-compare derivation; the
    single-channel stream loop bound (the `==1` *gate* is HIGH).
- **The bounds VALUES are header-grounded, not body-walked.** The per-dtype bounds table
  (§4) is read verbatim from the arch-isa validity predicate (compile-verified constants +
  the `has_valid_seq_bounds_dtype` / channel / 2×-count clauses) — a higher bar than a
  desynced body walk. The body **corroborates** (the int + fp primitives, the `dsel`
  pair-pack) but the header is the authoritative source.
- **No host/NKI API surface.** No `.py`/`.json`/`.md` reference to GetSequenceBounds exists
  in the extracted customop-lib or the NKI wheel (v0.21.2.0). It is an internal ucode/codec
  op (POOL codec module, neighbor to nonzero/dequant/cptc), not surfaced through the
  `at::Tensor` custom-op or the NKI ISA layer. The `c10::ScalarType` bridge is irrelevant
  (`INT32`/`FP32` map trivially to `Int`/`Float`).

---

## 6. Per-generation presence

The op is **NC-v3+ only** (`has_valid_nc_get_sequence_bounds: nc >= V3`). The struct
(64 B), opcode (`0xbe`), dtype whitelist (`{INT32, FP32}`), channel gate (`==1`), and
2× / `num_elem[2]==2` rule are **byte-identical across the three supporting gens** — there
is no per-gen bounds change.

| GEN (NC) | opcode `0xbe` | seq_bounds hdr | dtype gate | POOL images (binary) | body |
|---|---|---|---|---|---|
| TONGA (V1) | – | – | – | (no codec) | absent |
| SUNDA (V2) | – (no `0xbe`) | – (no header) | – (`nc<V3`) | `SUNDA_NX_POOL` (legacy RELEASE_EXTISA) | **absent** (not in JSON) |
| CAYMAN (V3) | `0xbe` | present | `{INT32,FP32}` | `CAYMAN_NX_POOL` PERF + DEBUG + TEST | `0x01004284` (MAIN) / `0x01000e2c` (codec) |
| MARIANA (V4) | `0xbe` | present | `{INT32,FP32}` | `MARIANA_NX_POOL` + `MARIANA_PLUS_NX_POOL` PERF + DEBUG + TEST | identical `.xt.prop` |
| MAVERICK (V5) | `0xbe` | present | `{INT32,FP32}` | `MAVERICK_NX_POOL` PERF + TEST (**no DEBUG**) | `kernel_info 0xbe → rel 0x4770` |

The per-gen POOL image names and the DEBUG self-name strings are read directly out of the
shipped `libnrtucode_internal.so` (the embedded firmware archive):
CAYMAN/MARIANA/MARIANA_PLUS ship `POOL_PERF` + `POOL_DEBUG` + `POOL_TEST`; **MAVERICK ships
`POOL_PERF` + `POOL_TEST` with no `POOL_DEBUG`** — which is why the MAVERICK image is
stripped and its interiors are header-OBSERVED only.

> **v5 / MAVERICK INFERRED flag.** MAVERICK's seq_bounds *header* (struct + predicate) is
> byte-identical to CAYMAN/MARIANA (OBSERVED). MAVERICK's *body interior* — the FLIX slot
> order, the exact min/max reduce schedule — is **INFERRED**: the carved MAVERICK image is
> stripped ET_DYN with relative funcVAs and no DEBUG strings, so the v5 body is not
> byte-grounded the way v2–v4 are. The opcode (`0xbe`), the `kernel_info` entry
> (`0xbe → rel 0x4770`), the struct, and the dtype gate ARE byte/header-grounded for v5;
> the *micro-schedule* is not.

> **NOTE — SUNDA ships a POOL engine but not this op.** `SUNDA_NX_POOL` (and its legacy
> `SUNDA_Q7_POOL_RELEASE_EXTISA_0`) is present in the binary, but SUNDA's `common.h`
> defines no `GET_SEQUENCE_BOUNDS` / `0xbe` opcode and ships no `s3d3_seq_bounds.h` — the
> op was added at NC-v3. Do not back-port GetSequenceBounds onto an NC-v2 target.

---

## 7. Reimplementation checklist

1. **Decode** `S3D3_SEQ_BOUNDS` (64 B): `num_active_channels@12`, `in_dtype@14`,
   `out_dtype@15`, `src_mem_pattern@16`, `dst_mem_pattern@32`, `reserved1@48`. Assert
   `sizeof == 64`.
2. **Validate** with the full `is_valid_get_sequence_bounds` predicate (§4) at decode time,
   *before* dispatch: opcode `0xbe`, `nc >= V3`, `num_active_channels == 1`, both dtypes ∈
   `{INT32(0x8), FP32(0xA)}`, all 17 reserved bytes zero, both tensors SBUF/no-PSUM,
   `2*t3d(src) == t3d(dst)`, `dst.num_elem[2] == 2`.
3. **Dispatch** via `kernel_info_table` `{spec, 0xbe} → funcVA`, then the `const16`-built
   `callx8` trampoline → body.
4. **Compute** the two-leg reduction: pass `(elem_count, dtype)` by value; branch on dtype;
   INT32 → integer vector min/max (`ivp_maxun_2x32`/`ivp_bmaxnx16`); FP32 → ordered-float
   compare-and-swap (`ivp_oltnxf16`/`ivp_ultn_2xf32t`); SELECT-pack the `{min,max}` pair
   (`ivp_dselnx16t`) into the 2× dst.
5. **Do NOT** confuse this with [SEQ PC-Bounds](../seq/pc-bounds.md) — that is the
   instruction-pointer range trap, a completely separate mechanism.

## See also

- [NonzeroWithCount](./nonzero-with-count.md) — the `0xf2` POOL-codec neighbor (the
  immediate `.xt.prop` follower), same `int<int>`/`float<float>` template, same
  compare + lane-select primitives, the index-compaction-with-count codec.
- [The Unified Datatype Model](./dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` ordinal map
  (`INT32=0x8`, `FP32=0xA`, `FP32R=0xB`, the MAVERICK MX additions) and the engine-wide
  dtype-dispatch philosophy this op's 2-value whitelist narrows.
- [SEQ PC-Bounds Enforcement + Host API](../seq/pc-bounds.md) — the **distinct**
  instruction-pointer-range enforcement (NOT data bounds); see the NOTE callout at the top
  of this page.
