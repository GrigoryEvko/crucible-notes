# Tensor-Tensor 64-bit Path (`tensor_tensor_64bit_dispatch` + `setup_64bit_rw`)

This page documents the **`INT64`/`UINT64` width path** of the GPSIMD NeuronCore's binary
elementwise kernel — the five firmware functions the base tensor-tensor decoder branches into
when an operand's datatype is 64-bit. It is the *width* sibling of the 32-bit
[base Tensor-Tensor kernel](tensor-tensor.md) (`S3S3D3_TT`, the `NEURON_ISA_TPB_ALU_OP`
table): the **same** `decode_tensor_tensor_arith` entry, the **same** 64-byte wire struct, the
**same** 60-op ALU enum — but a different *execution* family, because the `ncore2gp` vector
datapath has **no native 64-bit lane**. A 64-bit element is carried as a **pair of 32-bit
lanes** (a low half + a high half), and the operation is **synthesised** from 32-bit primitives:
add/sub thread a carry/borrow across the pair, multiply runs a 32×32→64 partial-product MAC,
and the bitwise ops run as two *independent* 32-bit lane ops.

This path is **distinct from** the [Extended (0xF0) Tensor-Tensor variant](ext-tensor-tensor-arith.md):
the 0xF0 escape is a separate decode route (a different struct, `op` at a different offset, 2-D
patterns, add/multiply-restricted) and — confirmed reloc-pinned in both directions — it **never
calls** any of the five 64-bit functions. The two are *siblings under different parents*, sharing
only the `NEURON_ISA_TPB_ALU_OP` and `NEURON_ISA_TPB_DTYPE` ABI enums, never code (§7).

This is the **Cadence Tensilica Vision-Q7 *Cairo* (`ncore2gp`)** GPSIMD compute core's POOL-engine
firmware — hand-scheduled, windowed-ABI Xtensa FLIX/VLIW (Xtensa24, RI-2022.9, NX1.1.4, 512-bit
datapath). Device facts are byte-pinned to the carved `CAYMAN` POOL **PERF** EXTISA image
(`extisa_CAYMAN_POOL_PERF_EXTISA_0`, embedded in `libnrtucode_internal.so`; `.text` VMA
`0x01000000` == file offset `0x100`), re-disassembled with the native `xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`); host-ISA facts (the predicates, the dtype enum, the register-pair rule)
are read field-exact from the `aws_neuron_isa_tpb_*.h` arch-isa headers shipped in the
customop-lib (`0.21.2.0`) package, and the function signatures are read from the demangled string
table of `libnrtucode_internal.so` itself. The `extracted/` and `ida/` trees are gitignored — reach
them with `fd --no-ignore` or absolute paths. Confidence/evidence tags follow the project
[Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / read-from-header this pass, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a cited sibling page's confidence.

> **Scope of the device disassembly.** The CAYMAN POOL PERF image is **FLIX VLIW with interleaved
> literal pools** that desyncs on the recurring `4f 00 6f ..` / `5f ..` lead bytes under *stock*
> `objdump`; the native `ncore2gp`-configured `xtensa-elf-objdump` recovers `entry` prologues,
> function-start `.xt.prop` records, and individual FLIX bundles (exposing the IVP mnemonics), but
> the *exact* per-element carry/partial-product schedule sits inside the desyncing spans. Every
> structural claim below is anchored to a **relocation**, a **`.xt.prop` function-start record**, an
> **`entry` prologue byte**, a **compile-cited arch-isa predicate**, or a **surviving IVP mnemonic**
> — never to a guessed bundle interior. v2–v4 (`SUNDA`/`CAYMAN`/`MARIANA`/`MARIANA_PLUS`) are
> byte-grounded; v5/`MAVERICK` is structurally fingerprinted (frame signature) but symbol-stripped —
> flag any v5 interior claim INFERRED.

> **STRUCT-SIZE vs DATA-WIDTH — do not conflate.** The base struct `S3S3D3_TT` is **64 *bytes***
> (`sizeof == 64`, compile-asserted). That is the *wire-record* size and is identical for the 8-bit
> and 64-bit cases. This page is about 64-bit *data width* (`INT64`/`UINT64`, 8 bytes per element),
> which is an orthogonal axis. The 64-byte struct never changes; what changes is how many bytes one
> *element* occupies and how it flows through the datapath.

---

## 0. TL;DR — the 64-bit path in seven facts

1. **Sub-case of the BASE decode, not the 0xF0 extended one.** `decode_tensor_tensor_arith`
   (`0x01000f60`) detects a 64-bit dtype and branches (7 reloc-pinned call edges, *all inside its
   own body*) to five dedicated functions. The 0xF0 extended decoder is uninvolved (§7). `[HIGH/OBSERVED]`
2. **Detection predicate.** `is_valid_64b_int_dtype(d) == (d == INT64 || d == UINT64)`
   (`common.h:2451`), with `INT64 = 0xC`, `UINT64 = 0x1`. The Pool legality gate
   (`is_valid_tensor_tensor_arith_pool`) requires the 64-bit dtype on **all three** operands
   (`out`, `dtype_lo`, `dtype_hi`) **and** an int-aluop. `[HIGH/OBSERVED]`
3. **Five functions:** `setup_64bit_rw` (the register-pair r/w setup, called first by *both*
   sub-branches), `tensor_tensor_64bit_dispatch<VectorInt64>`/`<VectorUint64>` (arith), and
   `tensor_tensor_64bit_bitvec_dispatch<VectorInt64>`/`<VectorUint64>` (bitwise). Signedness is the
   **template parameter**, not a runtime flag. `[HIGH/OBSERVED — demangled signatures]`
4. **Register-PAIR carry.** Each 64-bit element = an even-numbered register/lane pair
   (`reg n` = lo32, `reg n+1` = hi32); `mem_2d.h:67` makes the even-pair rule explicit and
   `mem2d_u64_register_check` *rejects* odd registers for u64. Throughput halves (4 u64 vs 8 u32 per
   immediate store) — the addressing stride per *element* doubles. `[HIGH/OBSERVED]`
5. **Emulated, not native.** `ncore2gp` has no 64-bit-lane ALU (the IVP roster tops at `*_2x32` /
   `*_2x16`). Add/sub thread a carry/borrow; multiply is a 32×32→64 partial-product MAC; max/min/
   abs-diff are 2-lane-wide compares; the halves are reassembled with `ivp_dextrprn_2x32` /
   `ivp_dseln_2x32t`. `[HIGH model / MED exact schedule]`
6. **Which ops.** The ARITH dispatchers admit exactly the `is_valid_int_aluop` set (26 ops: the int
   band `0xC4..0xDD` minus the four argmax/argmin, **plus** `MOD 0x1B`) — which *includes*
   `MULT_INT`/`MULT_UINT`, `DIVIDE_INT`/`DIVIDE_UINT`, `MOD_INT`/`MOD_UINT`. The BITVEC dispatchers
   run the bitwise set (`BYPASS`/`NOT`/`AND`/`OR`/`XOR` + shifts). There is **no fp64** at all. `[HIGH/OBSERVED]`
7. **Per-gen.** Present gen-wide across the POOL_PERF Neuron+ family
   (`CAYMAN`/`MARIANA`/`MARIANA_PLUS`/`MAVERICK`), **absent in `SUNDA`**, which merged the path into
   one `tensor_tensor_arith` worker (§6). `[HIGH/OBSERVED]`

---

## 1. The function chain — `.xt.prop` function starts (CAYMAN_0)

Function-start VMAs are read from the first 4 bytes (LE VMA) of each `.xt.prop.<mangled>` section
(a `.xt.prop` section *is* a function-start record, so these are EXACT); names via `c++filt`. The
five 64-bit functions sit **between** the base decoder and the 32-bit core worker (`setup_64bit_rw`)
and after it (the four dispatchers):

| VMA (CAYMAN) | `entry` prologue | demangled signature | role |
|---|---|---|---|
| `0x01000f60` | `36 41 00` `entry a1,32` | `decode_tensor_tensor_arith(unsigned int)` | BASE decode + the 64-bit branch |
| `0x01001108` | `36 61 00` `entry a1,48` | `setup_64bit_rw(unsigned int, NEURON_ISA_TPB_ALU_OP)` | **64-bit register-PAIR r/w setup** |
| `0x01001280` | `36 81 02` `entry a1,0x140` | `tensor_tensor_arith_impl(uint, uint, uint)` | 32-bit core worker (≤32-bit fallthrough) |
| `0x01001f7c` | `36 81 0b` `entry a1,0xb0` | `tensor_tensor_64bit_dispatch<VectorInt64>(uint, uint, ALU_OP)` | **i64 ARITH** (signed) |
| `0x01002720` | `36 81 0b` `entry a1,0xb0` | `tensor_tensor_64bit_dispatch<VectorUint64>(uint, uint, ALU_OP)` | **u64 ARITH** (unsigned) |
| `0x01002c2c` | `36 41 00` `entry a1,32` | `tensor_tensor_64bit_bitvec_dispatch<VectorInt64>(uint, ALU_OP)` | **i64 BITVEC** (signed) |
| `0x01002fc4` | `36 41 00` `entry a1,32` | `tensor_tensor_64bit_bitvec_dispatch<VectorUint64>(uint, ALU_OP)` | **u64 BITVEC** (unsigned) |
| `0x010034b0` | `36 41 00` `entry a1,32` | `decode_extended_inst_tensor_tensor_arith(bool, uint)` | the SEPARATE 0xF0 path (§7) |

`[HIGH/OBSERVED — .xt.prop function-start records + entry prologue bytes, CAYMAN POOL PERF EXTISA;
demangled names re-read from libnrtucode_internal.so this pass]`

The mangled symbols recovered directly from the host driver's string table corroborate the
signatures *byte-for-byte* — in particular the **two extra `j` (`unsigned int`) parameters** on the
arith dispatcher vs the bitvec dispatcher:

```text
# rg -ao over libnrtucode_internal.so, c++filt:
_Z14setup_64bit_rwj21NEURON_ISA_TPB_ALU_OP
    -> setup_64bit_rw(unsigned int, NEURON_ISA_TPB_ALU_OP)
tensor_tensor_64bit_dispatchI11VectorInt64 E v j j 21NEURON_ISA_TPB_ALU_OP    # ...Ev j j 21... = (uint, uint, ALU_OP)
tensor_tensor_64bit_dispatchI12VectorUint64Evjj21NEURON_ISA_TPB_ALU_OP
tensor_tensor_64bit_bitvec_dispatchI11VectorInt64 E v j 21NEURON_ISA_TPB_ALU_OP  # ...Ev j 21...   = (uint, ALU_OP)
tensor_tensor_64bit_bitvec_dispatchI12VectorUint64Evj21NEURON_ISA_TPB_ALU_OP
```

`[HIGH/OBSERVED — mangled symbols from libnrtucode_internal.so]`

> **NOTE — the frame sizes ARE the algorithm's fingerprint.** The two **arith** dispatchers carry
> the rare `entry a1,0xb0` (176-byte) prologue (`36 81 0b`); the two **bitvec** dispatchers carry
> only `entry a1,32` (`36 41 00`). That difference is not incidental: a 64-bit *arith* op must hold
> the lo-half result, the hi-half result, **and the carry/borrow vector** threaded between the two
> 32-bit passes (plus partial-product temporaries for multiply) — hence the 176-byte frame. A 64-bit
> *bitwise* op is two **independent** 32-bit lane ops with **no** carry to thread, so the bitvec
> dispatcher needs no carry/temp spill and runs in the 32-byte frame. `[HIGH/OBSERVED prologue bytes /
> HIGH/INFERRED frame rationale]`

> **NOTE — `setup_64bit_rw`'s 48-byte frame.** `setup_64bit_rw` (`entry a1,48`) sits between the
> decoder (32 B) and the arith dispatchers (176 B): larger than the decoder because it materialises
> the 64-bit *lo/hi* strided read/write temporaries, smaller than the dispatchers because it does
> not perform the carry-chained arithmetic — it only *prepares* the dual-lane operand streams. `[HIGH/OBSERVED]`

---

## 2. The dispatch — how a 64-bit op is detected and routed out of the 32-bit base

The 64-bit decision lives **inside** the base decoder. The relocation set (OBSERVED via
`readelf -r`; the `R_XTENSA_SLOT0_OP`/`ALT` pairs carry the target VMA) shows **seven** call edges,
and **every** call site falls inside the base decode body span `0x01000f60..0x01001108` (up to the
next function start). None lies in the 0xF0 extended body — so the 64-bit branch is a property of
the *base* decoder, exactly as the base page (`tensor-tensor.md` §5.1/§7) sketched:

| call-site VMA | target VMA | target function | inside decode body? |
|---|---|---|---|
| `0x01000fb3` / `fbb` | `0x01001108` | `setup_64bit_rw` (1st site) | yes (`< 0x1108`) |
| `0x01001090` / `098` | `0x01001108` | `setup_64bit_rw` (2nd site) | yes |
| `0x01001039` / `03c` | `0x01001280` | `tensor_tensor_arith_impl` (≤32-bit core) | yes |
| `0x01001044` / `04c` | `0x01001f7c` | `tensor_tensor_64bit_dispatch<VectorInt64>` | yes |
| `0x0100102b` / `033` | `0x01002720` | `tensor_tensor_64bit_dispatch<VectorUint64>` | yes |
| `0x010010de` / `0e6` | `0x01002c2c` | `tensor_tensor_64bit_bitvec_dispatch<VectorInt64>` | yes |
| `0x010010f0` / `0f8` | `0x01002fc4` | `tensor_tensor_64bit_bitvec_dispatch<VectorUint64>` | yes |

`[HIGH/OBSERVED — call edges reloc-pinned / MED exact in-body sequencing — FLIX desync]` The **two**
`setup_64bit_rw` call sites (`0xfb3`, `0x1090`) correspond to the arith-64 and bitvec-64 sub-branches
each preparing the 64-bit read/write *before* its templated dispatcher runs.

### 2.1 The detection predicate (arch-isa, header-exact)

```c
// aws_neuron_isa_tpb_common.h:2451  (cayman; identical in mariana/maverick)
// fn is_valid_64b_int_dtype(dtype: Dtype) -> bool {
//       (dtype == Dtype::INT64)        //  NEURON_ISA_TPB_DTYPE_INT64  = 0xC   (common.h:738)
//    || (dtype == Dtype::UINT64)       //  NEURON_ISA_TPB_DTYPE_UINT64 = 0x1   (common.h:734)
// }
```

`[HIGH/OBSERVED — common.h:2451-2454, 734, 738]` There is **no width arithmetic**: the kernel tests
the two specific codes. (Note the codes are non-contiguous — `UINT64`=`0x1` sits between
`INVALID`=`0x0` and `INT8`=`0x2`; `INT64`=`0xC`. A reimplementation must table-map dtype→width.)

The decode-stage runtime test is corroborated independently by the scalar move/dtype path: the
firmware uses a `bnei a*, 1` (UINT64) / `bnei a*, 12` (INT64) comparison chain to gate the 64-bit
handlers (cross-pinned at the [dtype-model page](dtype-model.md), §2.2 device dispatch). `[HIGH/CARRIED — dtype-model]`

### 2.2 The full Pool legality gate (what the *base validator* actually requires)

The base decode is the device-side realisation of the host validator
`is_valid_tensor_tensor_arith_pool` — and that predicate is **sharper** than "the out dtype is
64-bit". It requires the 64-bit dtype on **all three** operands *and* an int-aluop, as the third
arm of an OR over the Pool-engine arith cases:

```c
// aws_neuron_isa_tpb_common.h:2522  (cayman) — fn is_valid_tensor_tensor_arith_pool(i)
//       (   (i.s3s3d3_tt.op == AluOp::Pow)                                   // fp poly  -> 32-bit core
//        || (   is_valid_int_aluop(i.s3s3d3_tt.op)                           // 32-bit INT band
//            && is_valid_32b_int_dtype(out) && is_valid_32b_int_dtype(lo) && is_valid_32b_int_dtype(hi))
//        || (i.s3s3d3_tt.op == AluOp::Add)                                   // generic fp/int add -> 32-bit core
//        || (i.s3s3d3_tt.op == AluOp::Subtract)
//        || (i.s3s3d3_tt.op == AluOp::Mult)
//        || (   is_valid_int_aluop(i.s3s3d3_tt.op)                           // *** THE 64-bit ARM ***
//            && is_valid_64b_int_dtype(out)                                  //   out  is INT64/UINT64
//            && is_valid_64b_int_dtype(lo)                                   //   src0 is INT64/UINT64
//            && is_valid_64b_int_dtype(hi)))                                 //   src1 is INT64/UINT64
//    && is_s3s3d3_tt_tensors_in_sbuf(i)
```

`[HIGH/OBSERVED — common.h:2522-2538]`

> **GOTCHA — the 64-bit arm fires only on `is_valid_int_aluop` ops with *all three* operands 64-bit.**
> A reimplementation that routes to the 64-bit dispatchers on `out_dtype == INT64` alone is wrong in
> two ways: (1) the ABI requires `dtype_lo` and `dtype_hi` 64-bit too (mixed-width 64-bit
> tensor-tensor is *not* a legal POOL form here — there is no INT64×INT32→INT64 base op), and (2) the
> op must be in the int-aluop set, so a *base-band* `ADD`(`0x04`)/`MULT`(`0x06`) with 64-bit operands
> would not satisfy this arm at all (it falls to the generic `Add`/`Subtract`/`Mult` arms, which feed
> the 32-bit core and convert through fp — a different, lossy path). 64-bit integer math must use the
> `*_INT`/`*_UINT` opcodes. `[HIGH/OBSERVED — predicate body]`

> **NOTE — the symmetric DVE form.** The header also carries `is_valid_tensor_tensor_arith_dve`
> (`common.h:2540`), which is the *negation*: `op != Pow && !is_valid_64b_int_dtype(out/lo/hi) &&
> !is_valid_int_aluop(op)`. I.e. the **DVE** engine handles the fp/≤32-bit non-int-aluop cases; the
> **POOL** engine owns the int-aluop + 64-bit work. This GPSIMD POOL firmware is the Pool branch;
> the 64-bit synthesis is a Pool-engine capability. `[HIGH/OBSERVED — common.h:2540-2545]`

### 2.3 The branch, as C pseudocode (skeleton HIGH, in-body order MED)

```c
// decode_tensor_tensor_arith(unsigned int idx)  @0x01000f60
// (structure from the 7 reloc-pinned call edges + the header predicates; exact compare ORDER is
//  MED — the decode body FLIX-desyncs)
void decode_tensor_tensor_arith(unsigned int idx) {
    const NEURON_ISA_TPB_S3S3D3_TT_STRUCT *s = sbuf_instr(idx);     // SBUF-resident inst word
    Dtype  od = s->out_dtype;                                       // @13
    Dtype  d0 = dtype_lo(s->in0_in1_dtype);                         // @12 low nibble  (src0)
    Dtype  d1 = dtype_hi(s->in0_in1_dtype);                         // @12 high nibble (src1)
    AluOp  op = s->op;                                              // @14

    if (is_valid_64b_int_dtype(od)) {                              // od == INT64(0xC) || UINT64(0x1)
        // ABI guarantees d0 and d1 are also 64-bit and op is an int-aluop (S2.2),
        // so the only remaining branch is opcode-class (arith vs bitvec) x signedness.
        setup_64bit_rw(idx, op);                                    // @0x01001108 - prep even-pair r/w
        if (tensor_tensor_bitvec(s)) {                             // opcode == TensorTensorBitvecOp(0x51)
            if (od == INT64)  tensor_tensor_64bit_bitvec_dispatch<VectorInt64 >(idx, op); // 0x01002c2c
            else /*UINT64*/   tensor_tensor_64bit_bitvec_dispatch<VectorUint64>(idx, op); // 0x01002fc4
        } else {                                                   // opcode == TensorTensorArithOp(0x41)
            if (od == INT64)  tensor_tensor_64bit_dispatch<VectorInt64 >(idx, idx, op);   // 0x01001f7c
            else /*UINT64*/   tensor_tensor_64bit_dispatch<VectorUint64>(idx, idx, op);   // 0x01002720
        }
        return;
    }
    // everything <= 32-bit (fp8/fp16/bf16/fp32/fp32r, int8/16/32, uint8/16/32):
    tensor_tensor_arith_impl(idx, num_tensor_elements(s), s->num_active_channels); // 0x01001280
}
```

`[HIGH/OBSERVED — route + struct + predicates; MED — exact in-body compare order, FLIX desync]`

---

## 3. `setup_64bit_rw` — the register-PAIR (even/odd) dual-lane setup

`setup_64bit_rw(unsigned idx, NEURON_ISA_TPB_ALU_OP op)` @ `0x01001108`, prologue `entry a1,48`
(OBSERVED). It is called by **both** 64-bit sub-branches (the two sites at `0xfb3`, `0x1090`)
*before* the templated dispatcher runs, to prepare the strided 64-bit read/write of the three
`TENSOR3D` operand patterns (`src0`@16, `src1`@32, `dst`@48 of the `S3S3D3_TT` struct). `[HIGH/OBSERVED]`

### 3.1 The even-register-PAIR model (header-exact)

Because `ncore2gp` has no native 64-bit lane, a 64-bit value occupies a **pair** of 32-bit
registers/lanes — an *even* register `n` holds the low 32 bits, `n+1` holds the high 32 bits. This
is the same model the scalar TensorLoad/TensorStore (`mem_2d`) path enforces, and the header makes
the rule and its wire enforcement explicit:

```c
// aws_neuron_isa_tpb_mem_2d.h:67  (cayman)
//   - for u64 dtype, all registers must be even numbered (e.g. r0, r14, etc.)

// aws_neuron_isa_tpb_mem_2d.h:187 — the validator that ENFORCES it
// fn mem2d_u64_register_check(i) -> bool {
//   ! (   (i.mem_2d.src_datasrc == DataSrc::Register)
//      && (i.mem_2d.dtype == Dtype::UINT64)
//      && mem2d_odd_register_used(i) )          // any registers[k] % 2 != 0  -> REJECT (mem_2d.h:193)
// }
```

`[HIGH/OBSERVED — mem_2d.h:67, 187-200]` `setup_64bit_rw` is the **tensor-tensor analogue**: it
walks the `src0`/`src1` `TENSOR3D` patterns reading **two** 32-bit halves per element (a low half and
a high half) into paired vector registers, and prepares the `dst` pattern to write back the two
halves. `[HIGH model from the even-pair rule + the Vector{Int,Uint}64 template names / MED that the
exact lane-pairing scheme in the desynced body matches byte-for-byte — FLIX + literal-pool desync]`

### 3.2 Stride/throughput doubling — the cost of the pair

A 64-bit element is **two** memory words. The `mem_2d` immediate-store caps spell out the
half-throughput directly:

```c
// aws_neuron_isa_tpb_mem_2d.h:39-50  (cayman) — immediate element/store caps per dtype
//   i8/u8:        32 elements        u32/i32/f32:  8 elements
//   i16/u16:      16 elements        u64:          4 elements   <== HALF of the 32-bit width
```

`[HIGH/OBSERVED — mem_2d.h:39-50]` So a 512-bit vector lane that fits **8** `u32` fits only **4**
`u64`; the per-element byte stride doubles and the effective vector occupancy halves. A
reimplementation must (a) allocate operand registers in **even** pairs, (b) double the element
stride for the 64-bit walk, and (c) budget **half** the per-bundle element throughput vs the 32-bit
core. `[HIGH/OBSERVED caps / MED that the device loop doubles stride exactly as stated]`

### 3.3 IVP vocabulary that survives the desync in `setup_64bit_rw`

OBSERVED (native `ncore2gp` objdump, the mnemonics that decode cleanly between the desynced spans):
`ivp_lvn_2x16u_i` / `ivp_lvn_2x16s_i` (the 2×16 half-word lane loads), `ivp_lanx8s_xp` /
`ivp_labvdcmprs2nx8_xp` (strided aligning vector loads for the `TENSOR3D` pattern walk),
`ivp_dselnx16t` / `ivp_sel2nx8i_s4` (dual-lane select — the lo/hi interleave/deinterleave),
`ivp_rep2nx8` (lane replicate). The recurring **`_2x32` / `_2x16`** lane width *is* the byte-level
signature of the two-32-bit-halves representation: the engine processes 64-bit data as 2×32-bit (or
4×16-bit) lanes, **never** as a single 64-bit lane. `[HIGH that 2x32/2x16 ops are emitted / MED exact
role per op — FLIX desync]`

---

## 4. The algorithm — 64-bit EMULATED from 32-bit halves

### 4.1 Native vs emulated

`ncore2gp` has **no native 64-bit vector ALU** — the IVP intrinsic roster tops out at `*_2x32` /
`*_2x16` lane widths; there is no 64-bit-lane add/mul/cmp intrinsic. This is precisely *why* the path
exists as **separate template functions** rather than a wider dtype arm inside
`tensor_tensor_arith_impl`: the 32-bit worker cannot widen a lane, so 64-bit gets its own lo/hi-lane
synthesis. `[HIGH — IVP roster lane-width OBSERVED; the scalar 64-bit path is itself a UINT64/INT64-only
special case]`

### 4.2 ARITH 64-bit — carry/borrow chains + partial-product MAC

`tensor_tensor_64bit_dispatch<VectorInt64/VectorUint64>` (`entry a1,176`). The 176-byte frame holds
the lo result, the hi result, and the carry/borrow vector threaded between the two 32-bit passes.

```c
// tensor_tensor_64bit_dispatch<VectorIntN>(uint idx, uint /*idx2*/, ALU_OP op)
// 64-bit add as two 32-bit lane passes with an explicit carry vector.
// (model HIGH from the 176B frame + the surviving IVP add/select set; exact carry intrinsic MED)
for (each dst element e over the TENSOR3D iteration space) {
    u32x V lo0 = load_lo32(src0, e), hi0 = load_hi32(src0, e);     // even-pair: reg n / n+1
    u32x V lo1 = load_lo32(src1, e), hi1 = load_hi32(src1, e);

    switch (op) {
    case ADD_INT:  // 0xC4 (shared signed/unsigned)
        u32x V slo = lo0 + lo1;                                    // low half
        boolx V c  = (slo < lo0);                                  // carry-out of the low add
        u32x V shi = hi0 + hi1 + (u32x)c;                          // high half + carry-in
        store_pair(dst, e, slo, shi);                             // recombine: ivp_dextrprn_2x32
        break;
    case SUBTRACT_INT:  // 0xC6 (shared)
        u32x V dlo = lo0 - lo1;
        boolx V b  = (lo0 < lo1);                                  // borrow-out of the low sub
        u32x V dhi = hi0 - hi1 - (u32x)b;                          // high half - borrow
        store_pair(dst, e, dlo, dhi);
        break;
    case MULT_INT:   // 0xC5  /  MULT_UINT 0xDB
        // schoolbook 32x32 -> 64 partial products; cross terms folded into the 64-bit result:
        //   full = lo0*lo1 + ((hi0*lo1 + lo0*hi1) << 32)   (the hi0*hi1 term overflows int64, dropped)
        u64x V pp_ll = mul32x32_64(lo0, lo1);                      // ivp_mulan_2x32 / ivp_mul4t2n8xr8 ...
        u32x V pp_hl = hi0 * lo1, pp_lh = lo0 * hi1;
        u64x V full  = pp_ll + (((u64x)(pp_hl + pp_lh)) << 32);
        store_pair(dst, e, lo32(full), hi32(full));
        break;
    case MAX_INT: case MIN_INT: case MAX_UINT: case MIN_UINT:
        // 2-lane-wide compare: hi-half compare decides; lo-half breaks ties.
        boolx V hi_gt = cmp_hi(hi0, hi1), hi_eq = (hi0 == hi1);
        boolx V take1 = hi_gt | (hi_eq & cmp_lo(lo0, lo1));        // ivp_bmaxun_2x32 / ivp_bminn_2x32
        store_pair(dst, e, select(take1, lo1, lo0), select(take1, hi1, hi0));
        break;
    case ABS_DIFF_INT:  // 0xCD
        // |a-b| over the 64-bit pair: ivp_babssubunx16 / ivp_babssubu2nx8 datapath
        ...
    case DIVIDE_INT: case DIVIDE_UINT: case MOD_INT: case MOD_UINT:
        // legal at the ABI gate (is_valid_int_aluop) for 64-bit operands; the device
        // realises these via the int-engine multi-step sequence (MED - the body desyncs).
        ...
    default:
        debug_log("P%i: Error, Unimplemented tensor-tensor ALU op.");  // the 64-bit-path error string
    }
}
```

`[HIGH structure: carry/borrow add/sub, partial-product mul, 2-lane compare / MED the exact micro-
schedule — FLIX desync]`

**IVP harvest, `dispatch<VectorInt64>` (OBSERVED, surviving mnemonics):** `ivp_dseln_2x32t` (×4),
`ivp_la2nx8_ipi` (×3), `ivp_dselnx16t` (×2), `ivp_maxnx16t` (×2), `ivp_mul4t2n8xr8` (×2),
`ivp_mulan_2x32`, `ivp_mulus4tn16xr16`, `ivp_mulsupan16xr16`, `ivp_mul4tan16xr16` (the partial-product
MAC pieces), `ivp_bmaxunx16`, `ivp_bmaxun_2x32`, `ivp_bminn_2x32`, `ivp_bminu2nx8` (max/min),
`ivp_babssubunx16`, `ivp_babssubu2nx8` (abs-diff), `ivp_subsnx16` (sub), **`ivp_dextrprn_2x32`** (the
lo/hi → 64-bit recombine), `ivp_lvn_2x16u_i`/`s_i` (the half loads). **`dispatch<VectorUint64>`** is
the same structure with unsigned compares/shifts; its surviving set is sparser under desync
(`ivp_dseln_2x32t`, `ivp_dselnx16t`, `ivp_lsr2nx8_i` logical shift-right, `ivp_avgu2nx8`,
`ivp_ufloatn_2x32t`) — the same 2×32 lane machinery. `[OBSERVED mnemonics / MED per-op binding]`

> **CORRECTION — the 64-bit ALU table *does* include MUL/DIV/MOD, contrary to a "add/sub/cmp/bitwise
> only" reading.** The legality gate for the 64-bit arith arm is `is_valid_int_aluop(op)`
> (`common.h:1419`), whose membership set is **26 ops** — and it explicitly contains `MULT_INT`,
> `MULT_UINT`, `DIVIDE_INT`, `DIVIDE_UINT`, `MOD_INT`, `MOD_UINT`, `MOD` (the base-band cross-over op),
> alongside `ADD_INT`/`SUBTRACT_INT`, all the int/uint compares, `MAX/MIN_INT/UINT`, `ABS_DIFF_INT`,
> and `ABS_MAX/MIN_INT`. So **64-bit multiply, divide, and modulo are admitted at the ABI** — the
> partial-product MAC mnemonics (`ivp_mul*`) in the dispatcher body are exactly their device
> realisation. The *only* int-band ops **excluded** from `is_valid_int_aluop` are the four argmax/
> argmin (`AMAX_INT` `0xDE` .. `AMIN_UINT` `0xE1`). Any reimplementation that hard-codes "no 64-bit
> mul" will reject legal `MultInt` over INT64. `[HIGH/OBSERVED — common.h:1419-1446]`

### 4.3 The `is_valid_int_aluop` set — the exact 64-bit ARITH op band

| op (`NEURON_ISA_TPB_ALU_OP_*`) | code | sign | 64-bit ARITH? |
|---|---|---|---|
| `ADD_INT` | `0xC4` | shared s/u | yes (carry chain) |
| `MULT_INT` | `0xC5` | signed | yes (partial-product MAC) |
| `SUBTRACT_INT` | `0xC6` | shared s/u | yes (borrow chain) |
| `DIVIDE_INT` | `0xC7` | signed | yes (int-engine multi-step) |
| `MOD_INT` | `0xC8` | signed | yes |
| `IS_EQUAL_INT` | `0xC9` | shared s/u | yes (2-lane eq) |
| `IS_NE_INT` | `0xCA` | shared s/u | yes |
| `ABS_MAX_INT` | `0xCB` | signed | yes |
| `ABS_MIN_INT` | `0xCC` | signed | yes |
| `ABS_DIFF_INT` | `0xCD` | signed | yes (`ivp_babssub*`) |
| `MAX_INT` | `0xCF` | signed | yes (`ivp_bmaxn*`) |
| `MIN_INT` | `0xD0` | signed | yes (`ivp_bminn_2x32`) |
| `IS_GT_INT`..`IS_LT_INT` | `0xD1..0xD4` | signed | yes (2-lane compare) |
| `MAX_UINT` | `0xD5` | unsigned | yes (`ivp_bmaxun*`) |
| `MIN_UINT` | `0xD6` | unsigned | yes (`ivp_bminu*`) |
| `IS_GT_UINT`..`IS_LT_UINT` | `0xD7..0xDA` | unsigned | yes |
| `MULT_UINT` | `0xDB` | unsigned | yes (partial-product MAC) |
| `DIVIDE_UINT` | `0xDC` | unsigned | yes |
| `MOD_UINT` | `0xDD` | unsigned | yes |
| `MOD` | `0x1B` | shared s/u | yes (the base-band cross-over op) |
| `AMAX_INT`..`AMIN_UINT` | `0xDE..0xE1` | — | **NO** — argmax/argmin **excluded** from `is_valid_int_aluop` |

`[HIGH/OBSERVED — common.h:1419-1446]` Note `0xCE` (`ABS_VALUE_INT`) is *single-source* (`abs(src0)`)
and is **not** in `is_valid_int_aluop` either, so it is not a tensor-tensor 64-bit arm.

### 4.4 BITVEC 64-bit — two independent 32-bit lanes (+ the lone shift cross-half case)

`tensor_tensor_64bit_bitvec_dispatch<VectorInt64/VectorUint64>` (`entry a1,32`). A 64-bit BITWISE op
(`BYPASS`/`BITWISE_NOT`/`AND`/`OR`/`XOR`) is **two INDEPENDENT 32-bit lane ops** — no carry, no
cross-lane dependency — which is exactly why these dispatchers carry only the 32-byte small frame.

```c
// tensor_tensor_64bit_bitvec_dispatch<VectorIntN>(uint idx, ALU_OP op)
// (signature takes ONE index/count arg - NOT two - because src0==src1==out share one dtype: S5)
for (each dst element e) {
    u32x V lo0 = load_lo32(src0, e), hi0 = load_hi32(src0, e);
    u32x V lo1 = load_lo32(src1, e), hi1 = load_hi32(src1, e);
    switch (op) {
    case BITWISE_AND: store_pair(dst, e, lo0 & lo1, hi0 & hi1); break;  // independent lanes, no carry
    case BITWISE_OR:  store_pair(dst, e, lo0 | lo1, hi0 | hi1); break;
    case BITWISE_XOR: store_pair(dst, e, lo0 ^ lo1, hi0 ^ hi1); break;
    case BITWISE_NOT: store_pair(dst, e, ~lo0, ~hi0);           break;
    case BYPASS:      store_pair(dst, e, lo0, hi0);             break;  // copy src0
    // SHIFT is the ONE bitvec op with a cross-half dependency (bits cross the 32-bit boundary):
    case LOGICAL_SHIFT_LEFT: case LOGICAL_SHIFT_RIGHT:
    case ARITH_SHIFT_LEFT:   case ARITH_SHIFT_RIGHT:
        // src0 = the 64-bit value (lo0/hi0); src1 = a UINT32 shift amount (NOT a 64-bit pair).
        // bits shifted out of one half feed into the other -> the cross-half splice.
        store_pair(dst, e, shift64_lo(lo0, hi0, amt), shift64_hi(lo0, hi0, amt));
        break;
    default:
        debug_log("P%i: Error, Unimplemented tensor-tensor ALU op.");
    }
}
```

`[HIGH structure: small frame + identity-dtype signature + the header shift rule / MED exact per-op
body — desync]` The recovered `ivp_injbin_2x32` (inject/pack into a 2×32 lane) is the lo/hi bit-lane
assembler; `ivp_sel2nx8i` / `ivp_sel2nx8i_s4` do the lane select.

> **QUIRK — the 64-bit SHIFT breaks the bitvec dtype-identity rule.** The bitvec validator normally
> requires `src0 == src1 == out` dtype identity (`s3s3d3_tt_src_dst_dtype`, `s3s3d3_tt.h:182-185`).
> The **shift** sub-case is the exception: for a shift on a 64-bit value, `src1` (the *shift count*)
> is allowed to be **`UINT32`** while `src0` is `INT64`/`UINT64` — the two sources are deliberately
> *different* dtypes:
>
> ```c
> // aws_neuron_isa_tpb_s3s3d3_tt.h:186-188 — the SHIFT arm of s3s3d3_tt_src_dst_dtype
> //    || (   is_shift_op(i.s3s3d3_tt.op)                                       // shl/shr (arith or logical)
> //        && is_valid_64b_int_dtype(get_dtype_from_pair(...dtype_lo))          //   value operand is 64-bit int
> //        && (get_dtype_from_pair(...dtype_hi) == Dtype::UINT32))              //   shift-count operand is u32
> ```
>
> `is_shift_op` = `{ArithShiftLeft 0x02, ArithShiftRight 0x03, LogicalShiftLeft 0x10,
> LogicalShiftRight 0x11}` (`common.h:2456`). This is why the bitvec dispatcher signature takes only
> `(uint, ALU_OP)` (one dtype) — the identity rule holds for all bitwise ops **except** the shift,
> whose asymmetric `UINT32` count is the only legal deviation. A reimplementation that hard-codes
> `src0==src1==out` for *all* bitvec ops will reject legal 64-bit shifts. `[HIGH/OBSERVED —
> s3s3d3_tt.h:182-189; common.h:2456-2461]`

### 4.5 The element loop (both classes)

Walk the `dst` `TENSOR3D` iteration space; for each element, `setup_64bit_rw` has prepared the
strided lo32/hi32 halves of `src0`/`src1` (paired vector lanes); apply the op across the lo/hi lanes
(with carry for arith add/sub, partial-products for mul, independent lanes for bitwise); recombine
the two halves (`ivp_dextrprn_2x32` / `ivp_injbin_2x32`); strided-store the 64-bit result as two
halves. The channel/partition span is `num_active_channels` (struct `@15`) partitioned across the 8
Q7 lanes by `get_cpu_id()` (inherited from the base kernel). `[HIGH structure / MED uarch]`

---

## 5. The dtype — `INT64`/`UINT64` only, signed/unsigned by template, NO fp64

The **only** 64-bit dtypes in the entire `NEURON_ISA_TPB_DTYPE` enum are `INT64`(`0xC`) and
`UINT64`(`0x1`) (`common.h:738`/`734`). There is **no `fp64` value in the enum at all** — the 64-bit
tensor-tensor path is **integer-only**. (See the [dtype-model page](dtype-model.md) §1 for the full
ordinal table; `INT64`/`UINT64` are flagged "compute, gated, 32×2 synth" there — the same synthesis
this page details.) `[HIGH/OBSERVED — common.h:722-739]`

The signed/unsigned split is carried by the **template parameter**, not a runtime flag — the compiler
instantiated **two** copies of each dispatcher and the base decoder picks the copy by the dtype:

| dispatcher | signedness | dtype |
|---|---|---|
| `tensor_tensor_64bit_dispatch<VectorInt64>` / `_bitvec_dispatch<VectorInt64>` | SIGNED | `INT64`(`0xC`) |
| `tensor_tensor_64bit_dispatch<VectorUint64>` / `_bitvec_dispatch<VectorUint64>` | UNSIGNED | `UINT64`(`0x1`) |

`[HIGH/OBSERVED — .xt.prop template names + demangled symbols]`

The int-signedness consistency rule forbids mixing — a signed int op needs **all three** operand
dtypes signed; an unsigned int op needs all three unsigned:

```c
// aws_neuron_isa_tpb_s3s3d3_tt.h:170-180 — s3s3d3_tt_valid_int_signness(i)
//       ! is_valid_int_aluop(op)                                  // non-int op: vacuously OK
//    || ( is_signed_alu_op(op)   && is_signed_int(out) && is_signed_int(lo) && is_signed_int(hi) )
//    || ( is_unsigned_alu_op(op) && is_unsigned_int(out) && is_unsigned_int(lo) && is_unsigned_int(hi) )
//
// is_signed_int   = { INT8, INT16, INT32, INT64 }      (common.h:2463)  <- INT64  here
// is_unsigned_int = { UINT8, UINT16, UINT32, UINT64 }  (common.h:2470)  <- UINT64 here
```

`[HIGH/OBSERVED — s3s3d3_tt.h:170-180, common.h:2463-2475]` The *shared* int ops (`AddInt`,
`SubtractInt`, `MultInt`, `DivideInt`, `ModInt`, `Mod`, `IsEqualInt`, `IsNeInt`) appear in **both**
`is_signed_alu_op` (`common.h:2477`) and `is_unsigned_alu_op` (`common.h:2497`), so they are legal at
either signedness as long as all three operands agree; the sign-specific ops (`MaxInt`/`MaxUint`,
`AbsMaxInt`, the `_INT`/`_UINT` compares) appear in only one list and pin the corresponding 64-bit
dispatcher.

---

## 6. Per-generation presence

The five 64-bit `.xt.prop` function-start records were byte-checked in each POOL_PERF EXTISA_0 image
carved from the host driver (`<GEN>_Q7_POOL_PERF_EXTISA_0_SO_get.data`); `SUNDA` from its separate
POOL carve:

| GEN | `setup_64bit_rw` | `disp<I64>` | `disp<U64>` | `bitvec<I64>` | `bitvec<U64>` | symbols? |
|---|---|---|---|---|---|---|
| **CAYMAN** | `0x01001108` | `0x01001f7c` | `0x01002720` | `0x01002c2c` | `0x01002fc4` | YES (5 `.xt.prop`) |
| **MARIANA** | `0x01001114` | `0x01001f9c` | `0x01002740` | `0x01002c4c` | `0x01002fe4` | YES (5 `.xt.prop`) |
| **MARIANA_PLUS** | `0x01001114` | `0x01001f9c` | `0x01002740` | `0x01002c4c` | `0x01002fe4` | YES (5 `.xt.prop`) |
| **MAVERICK** | present (struct) | present | present | present | present | NO (0 `.xt.prop`, stripped) |
| **SUNDA** | — | — | — | — | — | **ABSENT** — merged into one `tensor_tensor_arith` worker |

`[HIGH/OBSERVED — .xt.prop records / readelf -sW blob VMAs]`

Corroboration from the host driver's `.rodata` (independent of the carve): each of the seven
`.xt.prop._Z…` mangled section-name strings (`setup_64bit_rw`, the two `tensor_tensor_64bit_dispatch`,
the two `tensor_tensor_64bit_bitvec_dispatch`, plus `decode_tensor_tensor_arith` and
`tensor_tensor_arith_impl`) appears **exactly 3 times** in `libnrtucode_internal.so`'s string table —
at three distinct embedded-blob `.rodata` bases — matching the three Neuron+ images embedded in that
driver (CAYMAN / MARIANA / MARIANA_PLUS). `[HIGH/OBSERVED — strings table, 3× multiplicity per symbol]`

* **CAYMAN / MARIANA / MARIANA_PLUS** all ship all five functions with byte-identical `.xt.prop`
  *file* offsets — same image layout. `MARIANA` == `MARIANA_PLUS` funcVAs are byte-identical to each
  other and a uniform **+0x0c** (setup) / **+0x20** (the four dispatchers) over `CAYMAN` — a pure
  build/layout delta, **not** a structural change. (`tensor-tensor.md` §8 tabulates the same deltas.)
  There is **no `MARIANA_PLUS`-specific addition**: `MARIANA` already carries the family byte-for-byte
  identically. `[HIGH/OBSERVED]`
* **MAVERICK** ships the POOL_PERF image but is **fully stripped** (0 `.xt.prop`, no kernel symbol
  table). The five functions are nonetheless present **structurally**, pinned by the distinctive
  entry-frame fingerprint: the two ARITH dispatchers carry the rare `entry a1,0xb0` (176-byte)
  prologue (`36 81 0b`), and `MAVERICK` contains **exactly 2** such prologues — matching `CAYMAN`'s
  exactly 2 (`disp<Int64>`, `disp<Uint64>`). The 176-byte frame is rare enough (only 2 in the whole
  image) to be a reliable signature; `SUNDA`, which lacks the family, has **zero**. So `MAVERICK`
  carries the family with only the symbolic names stripped. `[HIGH/OBSERVED — frame fingerprint, 2-for-2]`
* **SUNDA** does **not** ship the separated family. Its POOL carve exposes a **single**
  `tensor_tensor_arith(unsigned, ALU_OP, unsigned)` — no `setup_64bit_rw`, no
  `tensor_tensor_64bit_dispatch<>`, no bitvec dispatch templates (the strings `64bit_dispatch` and
  `setup_64bit` are absent from the SUNDA blob, and SUNDA has **zero** `entry a1,0xb0` prologues).
  SUNDA re-architected the tensor-tensor path into one merged worker. SUNDA's arch-isa header still
  **defines** `is_valid_64b_int_dtype` (9 refs in `common.h`, vs 13 in cayman/mariana/maverick), so
  the 64-bit dtype is still in the wire ABI — but the device exposes no separate 64-bit functions.
  How SUNDA's merged worker services (or rejects) 64-bit is **not** determinable from the symbol
  alone. `[HIGH/OBSERVED that the separated family is absent / MED how SUNDA services 64-bit]`

> **CORRECTION — the "64-bit int allowed … POOL" comment is in ALL gens, including SUNDA (reworded).**
> A prior report read had this comment *dropped* in SUNDA. Re-checked this pass: the comment is
> present in **all four** s3s3d3_tt.h headers — `// 64-bit int allowed on Neuron+ POOL` at
> `s3s3d3_tt.h:207` (cayman) / `:237` (mariana, maverick), and **reworded** `// 64-bit int allowed on
> Cayman+ POOL` at `:256` (**sunda**). So the comment is *not* the per-gen discriminator. SUNDA's
> **real** divergences in `s3s3d3_tt.h` are: (1) it **drops the `is_valid_dtype_64`/`AllowU64`/`AllowI64`
> admission block** in `is_valid_tensor_tensor` (using plain `is_valid_dtype(…, AllowFP32R)` instead at
> `:55-57`); (2) it **drops the 64-bit SHIFT special-case** — its `s3s3d3_tt_src_dst_dtype` (`:229-233`)
> has only the bitvec dtype-identity disjunct, **no** `is_shift_op && is_valid_64b_int_dtype(lo) &&
> hi==UINT32` arm; (3) it **adds** an `is_valid_tensor_tensor_bf16(i)` arm absent in the other gens;
> and (4) `is_valid_64b_int_dtype` is referenced **0** times in its `s3s3d3_tt.h` (vs 1 in the others),
> though still **defined** in its `common.h` (count 9 vs 13). Combined with the **device-side absence**
> of the five functions, these are the actual SUNDA discriminators. `[HIGH/OBSERVED — re-read across
> all four s3s3d3_tt.h this pass]`

---

## 7. Independence from the 0xF0 extended path — reloc-pinned both directions

The [Extended (0xF0) Tensor-Tensor variant](ext-tensor-tensor-arith.md)
(`decode_extended_inst_tensor_tensor_arith`, `0xf0`/spec 2, `0x010034b0`) is a **separate decode
route**, and it shares **no code** with the 64-bit family:

* From the **extended** side: the 0xF0 decoder has zero relocs/calls to any of the five 64-bit
  functions — its only reloc targets its own `.bss` state slot, and its IVP vocabulary tops out at
  16→32 MACs with **no** 64-bit reach.
* From the **64-bit** side (this pass): all seven call edges into the five functions originate
  **inside** the base decoder's `0xf60..0x1108` span (§2); **none** of the seven call sites lies in
  the 0xF0 extended body (`0x010034b0..`).
* The 0xF0 path also reads a **different** struct (`EXTENDED_TTA`, `op` at offset **16** not 14, 2-D
  `TENSOR2D` mem patterns), and its `op` is **restricted to add/multiply** (header comment) — it
  cannot reach the int-aluop band the 64-bit arm requires anyway.

So the 0xF0 extended add/multiply (≤32-bit, 2-D) and the base-kernel 64-bit width synthesis are
**siblings under different parents, mutually non-calling**. They share only the
`NEURON_ISA_TPB_ALU_OP` and `NEURON_ISA_TPB_DTYPE` ABI enums — never code. `[HIGH/OBSERVED —
reloc-pinned both directions]`

> **GOTCHA — do NOT route 64-bit through the 0xF0 escape.** A reimplementation might be tempted to
> treat "64-bit" and "extended" as one widening mechanism. They are orthogonal: 64-bit is a *dtype*
> branch inside the **base** `0x41`/`0x51` decoder; 0xF0 is an *opcode-extension* escape with its own
> struct and an add/multiply-only `op`. A 64-bit `INT64` `MultInt` flows base-decode →
> `setup_64bit_rw` → `tensor_tensor_64bit_dispatch<VectorInt64>`; it never touches the 0xF0 decoder.
> `[HIGH/OBSERVED]`

---

## 8. The switch default (string-anchored — proves it's a compiled switch)

An `op` outside the legal class hits the C++ switch default, which logs a baked DEBUG string. The
**two distinct** strings — recovered directly from the host driver this pass — prove the 32-bit and
64-bit paths are *separate* compiled switches (the 32-bit one prints the op hex; the 64-bit dispatch
does not):

```text
# rg -aoz over libnrtucode_internal.so:
"P%i: Error, Unimplemented tensor-tensor ALU op(0x%x)"    // 32-bit path (tensor_tensor_arith_impl)
"P%i: Error, Unimplemented tensor-tensor ALU op."         // 64-bit dispatch path (no op hex)
```

`[HIGH/OBSERVED — strings on libnrtucode_internal.so]` There is **no discrete `.rodata` op→address
jump table** for the 64-bit path; dispatch is a compiled switch over `op`, consistent with the base
kernel.

---

## 9. Reimplementation checklist & honest limitations

A Vision-Q7-compatible 64-bit tensor-tensor engine must:

1. **Detect** 64-bit in the base decoder via `is_valid_64b_int_dtype(d) == (d==INT64(0xC) ||
   d==UINT64(0x1))` — a two-code membership test, **not** a width field. The Pool arm fires only when
   **all three** operand dtypes are 64-bit **and** `op ∈ is_valid_int_aluop` (§2.2). `[HIGH]`
2. **Route** by opcode-class × signedness to the five functions: `setup_64bit_rw` first (both
   sub-branches), then `tensor_tensor_64bit_dispatch<Vector{Int,Uint}64>` for `0x41` arith or
   `tensor_tensor_64bit_bitvec_dispatch<Vector{Int,Uint}64>` for `0x51` bitvec. Signedness is the
   template parameter (two compiled copies). `[HIGH]`
3. **Carry the operand as an even register/lane PAIR** (`reg n` = lo32, `reg n+1` = hi32; reject odd
   registers per `mem2d_u64_register_check`); **double** the element stride and budget **half** the
   per-bundle element throughput (4 u64 vs 8 u32). `[HIGH]`
4. **Emulate** from 32-bit halves — there is no native 64-bit lane. Add/sub: carry/borrow across the
   pair. Mul: 32×32→64 partial-product MAC (the `hi0*hi1` term overflows int64 and is dropped).
   Max/min/compare: hi-half compare with a lo-half tiebreak. Recombine with `ivp_dextrprn_2x32` /
   `ivp_injbin_2x32`. `[HIGH model / MED schedule]`
5. **Admit the full int-aluop op set for 64-bit ARITH** — including `MultInt`/`MultUint`,
   `DivideInt`/`DivideUint`, `ModInt`/`ModUint`, **not** only add/sub/cmp/bitwise. Exclude **only**
   the four argmax/argmin (`0xDE..0xE1`). `[HIGH]`
6. **Honour the shift exception** — a 64-bit shift's count (`src1`) is `UINT32`, breaking the bitvec
   `src0==src1==out` identity rule. `[HIGH]`
7. **Reject fp64** — there is no `fp64` dtype; 64-bit is integer-only. `[HIGH]`
8. Keep the 64-bit family **independent** of any 0xF0 extended path — they do not share a decode
   route. `[HIGH]`

**Honest limitations.** The dispatch **skeleton** (the 7 reloc-pinned call edges, the `entry`
frame sizes, the detection/legality predicates, the dtype set, per-gen presence, and the 0xF0
independence) is all `HIGH/OBSERVED`. The **soft** spots, honestly flagged: (a) the *exact in-body
compare/branch order* inside `decode_tensor_tensor_arith` (which dtype/opcode is tested first) is
`MED` — the decode body is hand-scheduled FLIX VLIW with interleaved literal pools that desync under
both stock and the native `ncore2gp` objdump; (b) the *byte-exact carry/borrow and partial-product
micro-schedule* inside the dispatchers is `MED` — the per-op IVP binding is the strongest
correspondence the surviving mnemonics + the `Vector{Int,Uint}64` template semantics support, never
a byte-exact jump arm; (c) how **SUNDA**'s merged `tensor_tensor_arith` services or rejects 64-bit is
`MED` — only the *absence* of the separated family is OBSERVED. The `MAVERICK` interior is
fingerprinted (the 2-for-2 176-byte-frame signature) but symbol-stripped — flag v5 interior claims
INFERRED.

---

## Related pages

* [Tensor-Tensor Elementwise Arith + the ALU-OP Table (`S3S3D3_TT`, `0x41`/`0x51`)](tensor-tensor.md)
  — the **32-bit BASE** this page branches off (the shared struct, the full 60-op `NEURON_ISA_TPB_ALU_OP`
  enum, `tensor_tensor_arith_impl`).
* [Extended Tensor-Tensor Arith (0xF0 variant)](ext-tensor-tensor-arith.md) — the **separate** 0xF0
  decode route; §7 asserts the reloc-pinned independence.
* [The ALU-Op Datapath + Dtype Matrix](alu-op-matrix.md) — the full per-op × per-dtype support matrix
  (this page pins the *64-bit cells*; that page pins the whole grid).
* [The Unified Datatype Model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` enum (`INT64`=`0xC`,
  `UINT64`=`0x1`, no fp64) and the "32×2 synth" gating.
* [Scalar-Tensor-Tensor (`S2S2D2_STT`, `0x9d`/`0x9e`)](scalar-tensor-tensor.md) ·
  [Tensor-Scalar (`S3D3_TS`, `0x43`/`0x53`)](tensor-scalar.md) — same `op` table, single-source forms.
* ISA Vector-ALU reference: [b01 int/compare/logic](../../isa/ref/b01-vec-alu-int.md) — the
  `ivp_b*` integer IVP vocabulary (`ivp_bmaxun_2x32`, `ivp_bminn_2x32`, `ivp_babssub*`,
  `ivp_dextrprn_2x32`) the 64-bit dispatchers lower to.
