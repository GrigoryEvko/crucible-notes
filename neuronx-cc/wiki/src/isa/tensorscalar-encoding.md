# TensorScalar / Cumulative / ScalarTensorTensor / Exp Encoding

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; the C++ core logic is shared across cp310/11/12). The encoders, look-up tables and the pybind/validator string pool live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331…`, md5 `1d93972b…`); the BIR member offsets and the `AluOpType` / `EngineAccumulationType` enums originate in `libBIR.so` (`a9b1ea38`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the **tensor-scalar family** — every instruction that walks a tensor against one or two ALU scalars on the DVE (Vector) engine. The remarkable structural fact, and the reason these four KLR-level operations share one page, is that they **collapse onto one wire struct**. `NEURON_ISA_TPB_S3D3_TS_STRUCT` — a 64-byte bundle with two ALU-op selector bytes, a 2-bit reverse-operand pack, two immediate/scalar slots, and a dtype/channel control band — is the *same* layout for plain **TensorScalar** (`0x43`/`0x93`/`0x53`/`0x9A`), for **TensorScalarCacheCumulative** (the scan/cumulative variant, `0x9A`/`0xE6`), and for **Exponential** (`0x30`, the fused softmax primitive). Two sibling structs cover the operands that do not fit the symmetric 3-D shape: `S2S2D2_STT_STRUCT` packs three tensor operands of **ScalarTensorTensor** (`0x9D`/`0x9E`/`0xE5`), and `S2D2_TS_AS_STRUCT` carries the two addressable scalars of **TensorScalarAddr** (`0x74`).

What discriminates the datapaths is not the struct but the **opcode plus a handful of mode fields**: the `op0`/`op1` ALU selectors and their reverse flags, the `accumulator_cmd`/`reduce_cmd` byte, and — for the cache/scan path — a `TSCMode` enum read straight from the BIR instruction that picks Reduce (`0x9A`) versus Cumulative/Scan (`0xE6`). Exponential is the extreme case: it reuses the full `S3D3_TS` layout but **hard-forces** `op0`/`op1` to Bypass and reverse to None, so the exp function is implicit in the opcode and the only live op-specific field is the fused-accumulate command.

The encoders are dispatched from `CoreV2GenImpl::visitInstTensorScalarPtr` (`@0x1266d60`), a pure three-way fan-out over `bir::InstTensorScalarPtr`; the cache/scan path has its own visitor (`visitInstTensorScalarCache @0x125db00`); and Exponential is the *one* member of the family with a dedicated CoreV4 emitter (`visitInstExponential @0x1439d30`) — everything else is CoreV2-emitted and reached by CoreV4/V5 through the CoreV2 bodies. The bar for this page: a reader can **byte-encode any tensor-scalar, scan, scalar-tensor-tensor, or exp instruction by hand**, knowing for each control byte its offset, width, semantic, value source, the validator/pybind string that *names* it, and the disassembly store-site that pins it. Every field row carries a confidence tag; the ALU-op wire bytes tie to `AluOpType` ordinals owned by [2.23 ISA Enum Ordinals](isa-enum-ordinals.md) and [Part 7.7](../bir/) — named here, not re-tabulated.

## At a glance

The whole family shares the 64-byte bundle skeleton — header `0x00..0x03`, the access-pattern sub-bands at `0x10`/`0x30` ([2.1 the bundle](instruction-bundle.md), Family A) — and differs only in the **control band** (`0x0C` and `0x20..0x2F`) this page maps. Every body is a `std::array<unsigned char,64>` emplaced and zero-filled (so any unwritten byte is a hard `0x00`), header-stamped, field-filled, ISA-checked, and `fwrite(buf,1,0x40,bin)`-ed.

| Opcode(s) | KLR op | Generator (libwalrus) | Engine | Wire struct |
|---|---|---|---|---|
| `0x43`/`0x93`/`0x53`/`0x9A` | TensorScalar / …Reduce | `CoreV2GenImpl::generateTensorScalarOrPtr` `@0x12651c0` | DVE (5) | `S3D3_TS_STRUCT` |
| `0x9D`/`0x9E`/`0xE5` | NcScalarTensorTensor | `CoreV2GenImpl::generateScalarTensorTensor` `@0x125cba0` | DVE (5) | `S2S2D2_STT_STRUCT` |
| `0x74` | TensorScalarAddr | `CoreV2GenImpl::generateTensorScalarAddr` `@0x1240970` | DVE (5) | `S2D2_TS_AS_STRUCT` |
| `0x9A`/`0xE6` | TensorScalarCache (Reduce/Cumulative/Scan) | `CoreV2GenImpl::visitInstTensorScalarCache` `@0x125db00` | DVE (5) | `S3D3_TS_STRUCT` |
| `0x30` | Exponential | `CoreV4GenImpl::visitInstExponential` `@0x1439d30` | DVE (5) | `S3D3_TS_STRUCT` |
| `0x92` | TensorScalarAffineSelect | (see OPEN note below) | DVE (5) | wire map not recovered |

**Header skeleton (all opcodes), from `setupHeader` (`@0x1172120` V2, `@0x143f440` V4):**

```text
 byte +0x00  opcode        (low byte of the opcode word)
 byte +0x01  inst_word_len = 0x10   (16 dwords = 64 bytes)
 byte +0x02..+0x03  reserved = 0x0000
```

The 16-bit word at `bundle[0:2]` is `(0x10<<8) | opcode`, little-endian — `0x1043` (plain TS), `0x10E6` (cumulative), `0x1030` (Exp). The CoreV4 Exponential path inlines the word store (`mov $0x1030,%r8d @0x1439eb0`) instead of calling the virtual `setupHeader`; the wire result is identical.

> **OPEN — `0x92` TensorScalarAffineSelect: wire byte-map not recovered.** This row previously pointed at a `[2.x]` placeholder with no body coverage and a `visitInstTensorScalarAffineSelect` byte-emitter that is **not present** in the analysed binary set. The op is real — it carries a BIR class (`bir::InstTensorScalarAffineSelect`, with `Gen3Hwm`/`CoreV4Hwm`/`TrainiumHwm::getLatency*` cost-model overloads) and is produced by the generated codegen `BirCodeGenLoopGen::codegenAffSelTensorScalarOp` / `AffSelTensorScalarOpGen`. From those codegen symbols its **logical** operand set is recoverable: `idx_partition_ap`, `idx_free_ap`, `index_expr` (an `AffineExpr`), `fill_value`, `cmp_mode` (the predicate/compare op), and `dtype` — i.e. it selects between a tensor-scalar result and a `fill_value` per an affine index predicate. But the codegen resolves the opcode **symbolically** (`Opcode.TensorScalarAffineSelect`) and appends a BIR instruction; the layer that packs the 64-byte wire bundle (the per-byte offsets, the opcode seed, the `NEURON_ISA_TPB_*_STRUCT` shape) is not in any decompiled artifact this pass. The byte-level field map is therefore left as an open gap rather than fabricated. It very likely reuses the `S3D3_TS`/`S2D2`-family layout the rest of this page maps, but that is not byte-confirmed here.

> **NOTE — one struct, N ops.** `S3D3_TS_STRUCT` is the shared 64-byte layout for **three** distinct KLR operations: plain TensorScalar, TensorScalarCacheCumulative/Reduce, and Exponential. This is not an inference — it is confirmed three independent ways in the binary: (a) the Exponential CoreV4 emitter instantiates `setupSyncWait<…core_v4::NEURON_ISA_TPB_S3D3_TS_STRUCT>` (symbol present); (b) the `exponential_info` pybind module string is `ISAInstructionInfo for s3d3_ts_struct - Exponential`; (c) the `tensor_scalar_cumulative_info` string is `…s3d3_ts_struct - TensorScalarCacheCumulative`. The opcode and which control bytes are live discriminate the three datapaths on one layout. A reimplementer who builds three separate wire formats here is wrong: build one, switch on opcode + mode fields.

---

## The dispatch — `visitInstTensorScalarPtr`

`CoreV2GenImpl::visitInstTensorScalarPtr @0x1266d60` is a **pure three-way dispatcher** over `bir::InstTensorScalarPtr` (IR type IT29); it emits no bundle of its own. It reads three mode flags from the BIR instruction and fans out:

```c
// CoreV2GenImpl::visitInstTensorScalarPtr @0x1266d60
function visitInstTensorScalarPtr(InstTensorScalarPtr &I):
    if  bir::isTensorScalarAddr(I):                 // addressable-scalar datapath
        return generateTensorScalarAddr(I);         //   opcode 0x74
    if  I.is_scalar_tensor_tensor /*@+0x1A0(416)*/  // arg2 is a tensor, not a scalar
     || I.stt_variant_tag        /*@+0x1C8(456)*/:  // the scan-STT variant
        return generateScalarTensorTensor(I);        //   opcode 0x9D/0x9E/0xE5
    return generateTensorScalarOrPtr(I);             //   opcode 0x43/0x93/0x53/0x9A
```

The four KLR nodes collapse onto **two** BIR instruction types: `TensorScalar`, `TensorScalarReduce` and `NcScalarTensorTensor` all become `InstTensorScalarPtr` (IT29); `TensorScalarCumulative` becomes `InstTensorScalarCache` (IT30). There is **no** plain `InstTensorScalar` (IT28) wire emitter in CoreV2 — the immediate-scalar IT28 form is produced only by the BIR JSON reader and never reaches the wire path.

> **NOTE — the whole family rides the DVE (Vector) engine.** `TensorScalarCache` opens its body with `reportError("TensorScalarCache must be on the DVE engine")` (string verified); the IT29 family and AffineSelect ride DVE by codegen; Exponential forces engine = DVE as well. When a 2-output fused-reduce form is emitted, every encoder appends a trailing `DveReadAccumulator` (opcode `0x9B`) to drain the per-partition accumulator.

### CodeGenMode and the shared tail

Each `visit*`/`generate*` body runs one of three `CodeGenMode` arms selected by `*((int*)this+156)` (= `this+0x270`): **1 = GENERATE_ISACODE** (writes a fresh zeroed 64-byte bundle and `fwrite`s `0x40`); **2 = RUN_ISA_CHECKS** (builds a byte-identical bundle on a stack frame, runs ISA asserts, no `fwrite`); **0 = COLLECT_OPCODES** (records the opcode int into a census, writes nothing). GENERATE and RUN write the **same field offsets** — the offsets below are read from the GENERATE arm's clean `*(bundle+N)=` stores (or, where the GENERATE arm builds on a `%rbp`-relative scratch, from the byte-identical RUN-arm layout). The shared emit tail:

```c
Bin = findBin(I); fwrite(bundle, 1, 0x40, Bin);
if (this+697) { InstBin = createInstBin(I); fwrite(bundle, 1, 0x40, InstBin); }
// 2-output (fused-reduce) forms additionally append DveReadAccumulator (0x9B)
```

---

## TensorScalar (OrPtr) — `0x43`/`0x93`/`0x53`/`0x9A`

`CoreV2GenImpl::generateTensorScalarOrPtr @0x12651c0` (bundle base in `r14`). The baseline of the family: walk an input tensor against one or two ALU scalars and write an output tensor. Wire struct `S3D3_TS_STRUCT`; the in/out access patterns are `assignAccess<…TENSOR3D>` ×2 into the `0x28..0x3F` band.

### Source fields

`bir::InstTensorScalarPtr` member offsets, pinned in this encoder's disasm:

| BIR member | Offset | Read at | Role |
|---|---|---|---|
| `op0` (`AluOpType`) | `+0xF0` (240) | `mov 0xf0(%r15) @0x1265942` | first ALU op |
| `op1` (`AluOpType`) | `+0x120` (288) | `mov 0x120(%r15) @0x1265966` | second ALU op |
| `reverse0` value / tag | `+0xF8` / `+0x118` | (MaybeAffine\<bool\>) | swap op0 operands |
| `reverse1` value / tag | `+0x128` / `+0x148` | (MaybeAffine\<bool\>) | swap op1 operands |
| `apply_transpose` value / tag | `+0x150` / `+0x170` | | transpose marker |
| `is_tensor_scalar_addr` | `+0x178` (376) | | dispatch flag |
| `is_scalar_tensor_tensor` | `+0x1A0` (416) | | dispatch flag |
| `acc` (`EngineAccumulationType`) | `+0x1F0` (496) | `@0x1265d..` → `+0x0C` | fused-reduce accum |

Operands: `arg[0]` = input AP (`getArgument<AP>(0)`); `out[0]` = output AP (`getOutput(0)`); `scalar0 = sub_123DF20(I)` (arg-list operand 0); `scalar1 = sub_1240360(I)` (operand 1, present iff `NumArgs==3`). `NumArgs` is walked at `I+160`; the legal set is `{2,3}` (else `NeuronAssertion "NumArgs == 2 || NumArgs == 3"`).

### Opcode byte selection

The opcode is chosen by a fork over `apply_transpose`, the bitvec class, the output count, and the accumulate command (every arm traced in the disasm):

```c
if (bir::isBitVecInstruction(I))            opcode = 0x53;  // bitvec  (mov $0x53 @0x1265823)
    // + transpose on a bitvec -> reportError "TensorScalarPtr bitvec does not support transpose"
else if (I.apply_transpose /*@+0x150*/ == 0) opcode = 0x43; // plain arith
else                                         opcode = 0x93; // transposed arith (add $0x93 @0x126559f)
if (NumOutputs == 2 || I.acc /*@+0x1F0*/ != 0) opcode = 0x9A;  // fused-reduce  (mov $0x9a @0x126560b)
```

`0x9A` is the `TensorScalarCacheReduce` opcode — a fused-reduce TensorScalar shares the reduce opcode. The four arith/bitvec/transpose/reduce bytes are `{0x43 TensorScalarArithOp, 0x53 TensorScalarBitvecOp, 0x93 TransposeTensorScalarArithOp, 0x9A TensorScalarCacheReduce}`.

> **GOTCHA — a 2-output TS that folds its accumulate to 0 is illegal.** If `NumOutputs==2` but `acc` resolves to `0`, the encoder fires `reportError("Reduce Accum result is not outputed")` and forces `acc=3` (ZeroAccumulate). The fused-reduce contract is: 2 outputs ⇒ a real accumulate command ⇒ a trailing `DveReadAccumulator(0x9B)`.

### Bundle field map (`S3D3_TS`)

| Off | W | Field (`s3d3_ts` / role) | Value source | Store-site | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode | `setupHeader` | hdr | CONFIRMED |
| `+0x01` | 1 | `inst_word_len = 0x10` | constant | hdr | CONFIRMED |
| `+0x0C` | 1 | `accumulator_cmd` | `sub_12038D0(acc @+0x1F0)` | `0x1265dba` | CONFIRMED |
| `+0x20` | 1 | `in_dtype` | `sub_120E650(arg0 AP dtype @+0x30)` | `0x126592b` | CONFIRMED |
| `+0x21` | 1 | `out_dtype` | `sub_120E650(out0 AP dtype @+0x30)` | `0x126593e` | CONFIRMED |
| `+0x22` | 1 | `num_active_channels` | `*(*(arg0 AP+0x50)+8)` (**input** AP) | `0x1265c95` | CONFIRMED |
| `+0x23` | 1 | `imm0` mem-pattern AP byte (scalar0) | `sub_12051E0(scalar0)` | `0x1265b16` | STRONG |
| `+0x24` | 1 | `op0` ALU wire byte | `sub_12039C0(op0 @+0xF0)` | `0x1265962` | CONFIRMED |
| `+0x25` | 1 | `op1` ALU wire byte | `sub_12039C0(op1 @+0x120)` | `0x126597c` | CONFIRMED |
| `+0x26` | 1 | `reverse_operands` (2-bit pack) | `reverse0 \| reverse1<<1` | `0x1265c6a` | CONFIRMED |
| `+0x27` | 1 | `imm1` mem-pattern AP byte (scalar1) | `sub_12051E0(scalar1)`, `NumArgs==3` only | — | STRONG |
| `+0x28..+0x3F` | 24 | src/dst `TENSOR3D` AP descriptors | `assignAccess<…TENSOR3D>` ×2 | — | STRONG |

The `imm0`/`imm1` datum: `sub_12051E0` materialises the scalar operand into the `+0x23`/`+0x27` AP slot either as an `ImmediateValue` (fp32 only — else `reportError("TensorScalarPtr arith immediate dtype must be fp32")`; bitvec rule "bitvec immediate dtype size must be >= input dtype") **or** as a pointer/runtime AP. That immediate-versus-pointer choice is the "Ptr" in the op name.

> **QUIRK — `rsqrt` flips a per-module flag.** If `op0`'s raw `AluOpType` ordinal is `28` (`rsqrt`), the encoder calls `bir::Module::setAttribute` to set a per-module rsqrt-in-use flag *before* converting the op to its wire byte. A reimplementer that only translates the byte will miss the module-level side effect.

**Semantics:** `out = op1(op0(in, s0), s1)`. With `NumArgs==2`, `op1` is the bypass identity and only `s0` participates.

### The reverse byte `+0x26`

`reverse0`/`reverse1` are `MaybeAffine<bool>` values; the encoder reads value `+0xF8`/`+0x128` and the variant tags `+0x118`/`+0x148`, and an engaged-but-unset tag throws `__throw_bad_variant_access`. The store is a 2-bit pack:

```c
if (reverse0_byte)  b[0x26] = (reverse1_byte == 0) ? 1 : 3;
else                b[0x26] = 2 * reverse1_byte;
// == reverse0 | (reverse1 << 1)   (mov %al,0x26(%r14) @0x1265c6a)
// bit0 = swap op0 operands (scalar on LHS), bit1 = swap op1 operands
```

This identical pack appears in STT, Addr, and Cache, and matches the `TensorScalarReverseOps` enum `{none, first, second, both} -> {0,1,2,3}`.

---

## ScalarTensorTensor — `0x9D`/`0x9E`/`0xE5`

`CoreV2GenImpl::generateScalarTensorTensor @0x125cba0`. A 3-operand op: a scalar combined with two tensors, `out = (scalar op0 tensor0) op1 tensor1`. Wire struct `S2S2D2_STT_STRUCT` — a **2-D pack of three tensor operands**, so the dtype/channel/accum control shifts into the `+0x28..+0x2C` band (the symmetric 3-D struct cannot carry a third operand). The in/out/operand access patterns are `assignAccess<…TENSOR2D>` ×2–3.

Operand order: `tensor0 = getArgument<AP>(0)`; `scalar = sub_123DF20(I)` (1st operand-list entry); `tensor1 = sub_1240360(I)` (2nd operand-list entry); `output = getOutput<AP>(0)`. Asserts (RUN/verifier arm): `NumArgs==3` ("ScalarTensorTensor must have 3 inputs"); no transpose; the 3rd operand must be an AP ("ScalarTensorTensor's 3rd argument must be AP" — `(tensor1+24)-1 <= 2`, the AP rank).

### Opcode byte selection

```c
if (I.stt_variant_tag /*@+0x1C8(456)*/)     opcode = 0xE5;  // movb $0xe5 @0x125cd48 — scan STT
else  opcode = (u8)(bir::isBitVecInstruction(I) - 0x63);    // sub $0x63 @0x125ce98
      //   non-bitvec: 0 - 99 = -99 = 0x9D = 157  (ScalarTensorTensorArith)
      //   bitvec:     1 - 99 = -98 = 0x9E = 158  (ScalarTensorTensorBitvec)
```

The non-bitvec arith byte is **`0x9D`** (157), not `0x9C` (156 — that is `TensorReduceRangeCheck`, a different op). The three bytes are `{0x9D ScalarTensorTensorArith, 0x9E ScalarTensorTensorBitvec, 0xE5 TensorTensorScanArith}`.

> **CORRECTION (D-J04) — STT arith opcode is `0x9D`, and the scalar/tensor1 dtype nibbles are swapped.** An earlier reading gave the non-bitvec opcode as `0x9C` and placed the *scalar* dtype in the `+0x28` hi-nibble. Disassembly shows the `isBitVecInstruction - 0x63` signed-byte computation yields `0x9D`/`0x9E`, and that `sub_1240360` (`tensor1`) — not the scalar — is the operand that gets the AP-rank check and the third `assignAccess<TENSOR2D>`. Therefore the `+0x28` hi-nibble is **tensor1** dtype and `+0x2B` is the **scalar** dtype. The `+0x28` lo-nibble (tensor0/arg0 dtype) is unchanged.

### Bundle field map (`S2S2D2_STT`)

| Off | W | Field (`s2s2d2_stt` / role) | Value source | Tag |
|---|---|---|---|---|
| `+0x00` | 1 | opcode (`0x9D`/`0x9E`/`0xE5`) | `setupHeader` | CONFIRMED |
| `+0x24` | 1 | `op0` ALU wire byte | `sub_12039C0(op0 @+0xF0)` | CONFIRMED |
| `+0x25` | 1 | `op1` ALU wire byte | `sub_12039C0(op1 @+0x120)` | CONFIRMED |
| `+0x26` | 1 | `reverse_operands` (2-bit pack) | `reverse0 \| reverse1<<1` | CONFIRMED |
| `+0x27` | 1 | scalar `imm0` AP byte | `sub_12051E0(scalar)` | STRONG |
| `+0x28` | 1 | `in_dtype[3:0] \| tensor1_dtype[7:4]` | lo = `sub_120E650(arg0 dt)`; hi = `16*sub_120E650(tensor1 dt)` | CONFIRMED |
| `+0x29` | 1 | `out_dtype` | `sub_120E650(out0 dt)` | CONFIRMED |
| `+0x2A` | 1 | `num_active_channels` | `*(*(tensor0=arg0 AP+0x50)+8)` | CONFIRMED |
| `+0x2B` | 1 | scalar dtype | `sub_120E650(scalar dt @+48)` | CONFIRMED |
| `+0x2C` | 1 | `accumulator_cmd` | `sub_12038D0(acc @+0x1F0)` | CONFIRMED |
| `+0x28..+0x3F` | overlap | `TENSOR2D` AP descriptors (t0/t1/out) | `assignAccess<…TENSOR2D>` | STRONG |

> **NOTE — STT uses the CoreV2 dtype LUT, not the CoreV4 one.** The CoreV2 STT body calls `sub_120E650` (LUT `byte_1DF5760`), not the CoreV4 `sub_14347C0` (LUT `byte_1DFBAD0`). The two LUTs differ only at index 2 (`float4_e2m1fn_x4`: CoreV2 = `0x05`, CoreV4 = `0x10`). STT is CoreV2-only — there is no `CoreV4GenImpl::generateScalarTensorTensor` in the binary; CoreV4 dispatches STT through the CoreV2 path.

`is_scalar_tensor_tensor @+0x1A0` is what distinguishes this from the plain two-scalar TS (here `arg2` is a tensor, not a scalar). Fused-reduce is supported, with the same "Reduce Accum result is not outputed" guard.

---

## TensorScalarAddr — `0x74`

`CoreV2GenImpl::generateTensorScalarAddr @0x1240970`. The addressable-scalar datapath: the scalar(s) come from a per-partition **addressable tensor** (a pointer read), not an immediate. Selected by `bir::isTensorScalarAddr(I)` (dispatcher arm 1). Wire struct `S2D2_TS_AS_STRUCT` (2-D, two addressable scalar operands). The opcode is **fixed `0x74`** — there is no transpose/bitvec/reduce fork in the addr mode.

Two scalar operands `s0 = sub_123DF20`, `s1 = sub_1240360` (`s1` iff `NumArgs==3`); each materialised into a bundle AP slot via `sub_12051E0`. `NumArgs ∈ {2,3}`.

### Bundle field map (`S2D2_TS_AS`)

| Off | W | Field (`s2d2_ts_as` / role) | Value source | Tag |
|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x74` | `setupHeader` | CONFIRMED |
| `+0x28` | 1 | `scalar0_dtype[3:0] \| scalar1_dtype[7:4]` | lo = `sub_120E650(s0 dt)`; hi = `16*sub_120E650(s1 dt)` (s1 hi iff `NumArgs==3`) | CONFIRMED |
| `+0x29` | 1 | `in_dtype[3:0] \| out_dtype[7:4]` | lo = `sub_120E650(in dt)`; hi = `16*sub_120E650(out dt)` | CONFIRMED |
| `+0x2A` | 1 | scalar0 addr/imm AP byte | `sub_12051E0(s0)` | STRONG |
| `+0x2B` | 1 | scalar1 addr/imm AP byte (`NumArgs==3`) | `sub_12051E0(s1)` | STRONG |
| `+0x2C` | 1 | `op0` ALU wire byte | `sub_12039C0(op0 @+0xF0)` | CONFIRMED |
| `+0x2D` | 1 | `op1` ALU wire byte | `sub_12039C0(op1 @+0x120)` | CONFIRMED |
| `+0x2E` | 1 | `num_active_channels` | `*(*(in AP+0x50)+8)` | CONFIRMED |
| `+0x2F` | 1 | `reverse_operands` (2-bit pack) | `reverse0 \| reverse1<<1` | CONFIRMED |
| `+0x28..+0x3F` | overlap | `TENSOR2D` AP descriptors (in & out) | `assignAccess<…TENSOR2D>` ×2 | STRONG |

The `0x74` op is gated by the `_addr_*` validator family (`d2_ts_as_valid_elem_count`, `*_addr_reserved_z*`, `*_addr_immediate*`, `*_addr_same_start_partition_in_sr*`, `*_addr_valid_ops*`, `*_addr_reverse_ch*` — the `s2d2_ts_as` substring recovered from the validator string pool).

---

## TensorScalarCache (Cumulative / Scan) — `0x9A`/`0xE6`

`CoreV2GenImpl::visitInstTensorScalarCache @0x125db00`. The **cumulative/scan** path — IR type IT30. It carries a per-partition running accumulator: Reduce folds the input to one result, Cumulative/Scan emits a running prefix at every element. Wire struct `S3D3_TS_STRUCT` — the **same** 64-byte layout as plain TensorScalar and Exponential. The body opens with the DVE engine guard (`reportError("TensorScalarCache must be on the DVE engine")`).

### Source fields and the `TSCMode` opcode fork

`bir::InstTensorScalarCache`: `op0 @+0xF0`, `op1 @+0x120`, `mode (TSCMode) @+0x150`, `acc (EngineAccumulationType) @+0x154`, `reverse0 @+0xF8` (tag `+0x118`), `reverse1 @+0x128` (tag `+0x148`).

```c
// CoreV2GenImpl::visitInstTensorScalarCache @0x125db00
mode = I.TSCMode /*@+0x150*/;                   // mov 0x150(%rax) @0x125dbc4
if      (mode == 0)            opcode = 0x9A;    // Reduce        (mov $0x9a @0x125dd49)
else if ((u32)(mode-1) <= 1)   opcode = 0xE6;    // Cumulative(1) or TensorScan(2)  (mov $0xe6 @0x125dbdb)
else                           reportError("Unhandled TSCMode");
```

`TSCMode` is `{0 = Reduce, 1 = Cumulative, 2 = TensorScan}`. **Cumulative and TensorScan share `0xE6`** (`TensorScalarCacheCumulative`); they are disambiguated downstream by the accumulate command and bundle bits, not by a distinct opcode. `0x9A` is `TensorScalarCacheReduce` — the same byte the fused-reduce plain TS uses.

### Bundle field map (`S3D3_TS`, cumulative role)

| Off | W | Field (cumulative role) | Value source | Store-site | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode (`0x9A` or `0xE6`) | `setupHeader` | hdr | CONFIRMED |
| `+0x0C` | 1 | `accumulator_cmd` | `sub_12038D0(acc @+0x154)` | `v16[12]` | CONFIRMED |
| `+0x20` | 1 | `in_dtype` | `sub_120E650(arg0 dt @+0x30)` | `0x125deed` | CONFIRMED |
| `+0x21` | 1 | `out_dtype` | `sub_120E650(out0 dt @+0x30)` | `v16[33]` | CONFIRMED |
| `+0x22` | 1 | `num_active_channels` | `*(*(arg0 AP+0x50)+8)` | `v16[34]` | CONFIRMED |
| `+0x23` | 1 | `imm0` / scalar0 AP byte | `sub_12051E0(scalar0)` | `v16[35]` | STRONG |
| `+0x24` | 1 | `op0` ALU wire byte | `sub_12039C0(op0 @+0xF0)` | `v16[36]` | CONFIRMED |
| `+0x25` | 1 | `op1` ALU wire byte | `sub_12039C0(op1 @+0x120)` | `v16[37]` | CONFIRMED |
| `+0x26` | 1 | `reverse_operands` (2-bit; `reverse0` = scan direction) | `reverse0 \| reverse1<<1` | `v16[38]` | CONFIRMED |
| `+0x27` | 1 | `imm1` AP byte (`NumArgs==3` only) | `sub_12051E0(scalar1)` | `v16[39]` | STRONG |
| `+0x28..+0x3F` | 24 | src/dst `TENSOR3D` AP descriptors | `assignAccess<…TENSOR3D>` ×2 | — | STRONG |

**Immediate rules (RUN arm):** the immediate must be fp32 ("TensorScalarCache immediate value must be fp32"); a second immediate (`imm1`) is allowed **only** when the accumulation mode is `LoadAccumulate` ("TensorScalarCache can only have imm1 if accumulation mode is LoadAccumulate" — `acc @+0x154 == 5`).

The scan binding: `reverse0` selects scan **direction** (forward/reverse). The accumulate command selects the scan init — `ZeroAccumulate` resets the cache to 0; `LoadAccumulate` seeds it from `imm1` (the reduce-init). A 2-output cumulative appends `DveReadAccumulator(0x9B)`.

> **NOTE — "reduce vs cumulative vs scan" is the opcode + `TSCMode`, not a wire enum.** The distinction lives in the BIR `TSCMode @+0x150` (which picks `0x9A` vs `0xE6` at opcode level) plus the `accumulator_cmd @+0x0C`. There is **no** separate reduce-command wire field — `reduce_cmd`/`accumulator_cmd` is one `EngineAccumulationType` byte. A reimplementer must not allocate a distinct on-wire ReduceCmd enum; it is dormant.

---

## Exponential — `0x30`

`CoreV4GenImpl::visitInstExponential @0x1439d30` (RUN_ISA arm `@0x14321c0`). The dedicated hardware fused-softmax primitive — `exp` plus optional accumulate, on the DVE engine. It **reuses the full `S3D3_TS` 64-byte layout** (the `setupSyncWait<…core_v4::NEURON_ISA_TPB_S3D3_TS_STRUCT>` symbol and the `exponential_info` pybind string `…s3d3_ts_struct - Exponential` both pin this), but with `op0`/`op1` **hard-forced to Bypass** and reverse **forced to None**. The exp function is implicit in the opcode — there is no activation-function-type field.

Exponential is the **only** member of the family with a dedicated CoreV4 emitter (no `CoreV2GenImpl::visitInstExponential` exists). It uses the CoreV4 helper set: `sub_14347C0` (CoreV4 dtype LUT `byte_1DFBAD0`, index 2 = `0x10`), `sub_142DF40` (CoreV4 accum mapper), and `sub_142E370` (CoreV4 mem-pattern AP-byte packer).

### Source field

`bir::InstExponential`: `reduce_cmd (EngineAccumulationType) @+0xF0(240)` is the **only** op-specific BIR field — note it is an `EngineAccumulationType`, not a `ReduceCmdType`. Operands: `arg[0]` input AP, `arg[1]` `imm0`, `arg[2]` `imm1`, `out[0]` output AP.

### Bundle field map (`S3D3_TS`, exp role)

| Off | W | Field (exp role) | Value source | Store-site | Tag |
|---|---|---|---|---|---|
| `+0x00` | 2 | opcode word `0x1030` (op `0x30` + hdr) | `mov $0x1030` | `0x1439eb0` | CONFIRMED |
| `+0x0C` | 1 | `reduce_cmd` / `accumulator_cmd` | `sub_142DF40(reduce_cmd @+0xF0)` | `0x143a158` | CONFIRMED |
| `+0x20` | 1 | `in_dtype` | `sub_14347C0(arg0 dt @+0x30)` | `0x1439f2c` | CONFIRMED |
| `+0x21` | 1 | `out_dtype` | `sub_14347C0(out0 dt @+0x30)` | `0x143a140` | CONFIRMED |
| `+0x22` | 1 | `num_active_channels` | `*(*(arg0 AP+0x50)+8)` | `0x1439f11` | CONFIRMED |
| `+0x23` | 1 | `imm0` mem-pattern AP byte | `sub_142E370(getArgument(1))` | — | STRONG |
| `+0x24` | 2 | `op0 \| op1` = `0x0000` (forced Bypass) | `*(u16*)(b+0x24) = 0` | `0x143a163` | CONFIRMED |
| `+0x26` | 1 | `reverse_operands` = `0` (forced None) | `*(b+0x26) = 0` | `0x143a153` | CONFIRMED |
| `+0x27` | 1 | `imm1` mem-pattern AP byte | `sub_142E370(getArgument(2))` | — | STRONG |
| `+0x28..+0x3F` | 24 | `MEM_PATTERN3D` AP descriptors (in & out) | `assignAccess<…MEM_PATTERN3D>` ×2 | — | STRONG |

```c
// CoreV4GenImpl::visitInstExponential @0x1439d30  (bundle base = r13)
u8 *b = emplace_zeroed_array64();
*(u16*)&b[0x00] = 0x1030;                          // op 0x30 + inst_word_len  @0x1439eb0
b[0x22] = *(*(arg0.AP+0x50)+8);                    // num_active_channels      @0x1439f11
b[0x20] = byte_1DFBAD0[arg0.dtype];                // in_dtype                 @0x1439f2c
b[0x21] = byte_1DFBAD0[out0.dtype];                // out_dtype                @0x143a140
b[0x26] = 0;                                       // reverse_operands = None  @0x143a153
*(u16*)&b[0x24] = 0;                               // op0|op1 = Bypass         @0x143a163
b[0x0C] = sub_142DF40(I.reduce_cmd /*@+0xF0*/);    // accumulator_cmd          @0x143a158
b[0x23] = sub_142E370(getArgument(1));             // imm0  (bias/scale mem-pattern)
b[0x27] = sub_142E370(getArgument(2));             // imm1  (reduce-init when LoadAccumulate)
assignAccess_MEM_PATTERN3D(&b[0x28], in, out);
runISACheck(b); fwrite(b, 1, 0x40, bin);
```

The fused exp+accumulate is driven entirely by `reduce_cmd/accumulator_cmd @+0x0C`: `ZeroAccumulate`/`Idle`/`Accumulate` require `imm1 == 0` (`imm1_src = InstructionImmediate`); `LoadAccumulate` lets `imm1` carry the reduce-init. The drain is `DveReadAccumulator(0x9B)` when fused — exp lives on DVE, so this is distinct from the Activation engine's `ReadActivationAccumulator`. The RUN_ISA validators (`@0x14321c0`) are exactly the `exponential_info` pybind names: `has_valid_exponential_accum_cmd`, `op0 and op1 must be Bypass, reverse_operands must be None`, `has_valid_exponential_num_elements`, and the immediate-validity check.

---

## Shared field-mapper LUTs (BIR enum → wire byte)

The whole family resolves enums to wire bytes through four shared mappers in `.rodata` (VA == file offset).

### dtype — `sub_120E650` (CoreV2) / `sub_14347C0` (CoreV4)

`byte_1DF5760[idx]` (CoreV2) and `byte_1DFBAD0[idx]` (CoreV4), `idx ∈ [0,0x13]`, xxd-verified this pass:

```text
CoreV2 byte_1DF5760: 03 02 05 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c
CoreV4 byte_1DFBAD0: 03 02 10 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c
                           ^^ only index 2 differs
```

> **QUIRK — the fp4 wire tag moved on CoreV4.** `float4_e2m1fn_x4` (BIR ordinal 2) is `0x05` on CoreV2 (`byte_1DF5760[2]`) but `0x10` on CoreV4 (`byte_1DFBAD0[2]`). Every other dtype tag is byte-identical. STT and the CoreV2 ops take the `0x05` tag; Exponential (CoreV4) takes `0x10`. The full ordinal→dtype name table (`0 uint8 … 19 int64`) is in [2.3 tensor descriptors](tensor-descriptors.md) and matches the PE matmul page's dtype column.

### ALU op — `sub_12039C0` (CoreV2)

A 30-case jump table (`jpt_12039EF`, ordinals `0..29`): identity for `0..18`; then `19→0x18, 20→0x13, 21→0x14, 22→0x16, 23→0x15, 26→0x1A, 27→0x1B, 28→0x1D, 29→0x19` (the `0x13/0x14/0x15/0x16/0x18/0x19/0x1D` targets verified as literal `mov $imm,%eax` arms in the table). Cases `24`/`25` fall to the `"Invalid enum variant for enum AluOpType"` default. This band is **byte-identical** to the CoreV4 mapper `sub_142E030` for ordinals `0..29`; CoreV4 merely extends it with `30→0x20, 31→0x21, 32→0xC8`. The `AluOpType` ordinal→name table (`0 bypass … 29 abs`) is owned by [2.23 ISA Enum Ordinals](isa-enum-ordinals.md) and [Part 7.7](../bir/) — not re-tabulated here.

> **CORRECTION (D-J04) — CoreV2 and CoreV4 share one ALU-op band.** An earlier brief claimed the CoreV2 converter mapped the comparison family differently from CoreV4 (a separate "compressed" scheme). Disassembly shows `sub_12039C0` emits the *same* bytes as the CoreV4 band for ordinals `0..29` (`not_equal=19→0x18`, `is_gt=20→0x13`, `is_ge=21→0x14`, `is_lt=22→0x16`, `is_le=23→0x15`, `rsqrt=28→0x1D`, `abs=29→0x19`). They are one band; CoreV4 only adds `30/31/32`. CoreV2 lacks only ordinals `24/25` and the gen4 extensions.

### accum — `sub_12038D0` (CoreV2) = `sub_142DF40` (CoreV4)

`EngineAccumulationType` → wire byte, identical on both: `0→0 (Idle)`, `1→1 (Zero)`, `3→3 (ZeroAccumulate)`, `4→2 (AddAccumulate)`, `5→4 (LoadAccumulate)`; ordinal `2 (Accumulate)` and others fall to the error default (no wire form). The `4→2`/`5→4` reorder is verified in the jump-table arms (`mov $0x02`, `mov $0x04`).

### num_active_channels accessor

All of `§TensorScalar/STT/Addr/Cache/Exp` read the channel/start-partition byte the same way: `*(_QWORD*)(*(_QWORD*)(AP + 0x50) + 8)` — the **input** AccessPattern's first `APPair` num/partition field (gated by `AP+0x88 != 0`). This is the identical accessor used by the Activation family at its `+0x22`. It lands at `+0x22` for the 3-D structs and at `+0x2A`/`+0x2E` for the 2-D STT/Addr packs.

---

## JOIN — bundle offset ↔ struct field ↔ BIR member

Three physical wire structs cover the whole family; the field names come from the libwalrus validator string pool (`is_valid_*`, `.rodata` `0x1dd5000..`), the `exponential_info`/`tensor_scalar_cumulative_info` pybind modules, and the encoders' own range-check strings.

| Struct | Opcodes | Shape | Where the dtype/channel/accum band lives |
|---|---|---|---|
| `S3D3_TS_STRUCT` | `0x43`/`0x93`/`0x53`/`0x9A`/`0xE6`/`0x30` | 3-D, in/out symmetric | `0x0C` accum, `0x20..0x27` control, `0x28..0x3F` two AP descriptors |
| `S2S2D2_STT_STRUCT` | `0x9D`/`0x9E`/`0xE5` | 2-D pack of 3 operands | `0x24..0x2C` (steals the band for the 3rd operand's dtypes/channel/accum) |
| `S2D2_TS_AS_STRUCT` | `0x74` | 2-D, two addressable scalars | `0x28..0x2F` (two scalar AP slots + dtype nibbles) |

All three carry `op0 @+0xF0` / `op1 @+0x120` and the reverse 2-bit pack; they differ only in how the dtype nibbles, `num_active_channels`, and `accum` land — the 3-D struct leaves `0x28..0x3F` entirely to the two AP descriptors, while the 2-D packs steal the `0x28..0x2C` band because they carry an extra operand.

BIR member offsets (`bir::InstTensorScalarPtr` / `…Cache` / `…Exponential`, pinned in the encoder disasm): Ptr/Cache — `op0 +0xF0`, `reverse0 +0xF8` (tag `+0x118`), `op1 +0x120`, `reverse1 +0x128` (tag `+0x148`), `apply_transpose +0x150` (tag `+0x170`), `is_tensor_scalar_addr +0x178`, `is_scalar_tensor_tensor +0x1A0` (tag `+0x1C0`); Ptr `acc +0x1F0`; Cache `TSCMode +0x150` + `acc +0x154`; Exponential `reduce_cmd +0xF0`, `engine +0x90`.

> **NOTE — the AP-descriptor sub-byte packing is shared, not pinned here.** The internal layout of the `0x28..0x3F` (`TENSOR3D`/`TENSOR2D`/`MEM_PATTERN3D`) access-pattern descriptors is produced by the generic `assignAccess<…>` templates shared across every DVE op; it is byte-pinned by [2.3–2.5 tensor descriptors](tensor-descriptors.md), not re-derived here (STRONG at the silicon level). The control-band bytes (`0x0C`, `0x20..0x2F`) are all CONFIRMED from clean `*(bundle+N)=` disassembly stores.

---

## Confidence summary

Every control-band byte — opcode, `accumulator_cmd`/`reduce_cmd`, the `op0`/`op1` selectors, the reverse pack, the dtype nibbles, and `num_active_channels` — is **CONFIRMED** from a traced store site plus the source-field BIR offset and (where applicable) the `.rodata` LUT byte. The `imm0`/`imm1` mem-pattern AP slots are **STRONG** (the value path is the generic `sub_12051E0`/`sub_142E370` immediate packer; the slot offsets are pinned). The `0x28..0x3F` AP descriptors are **STRONG** (generic `assignAccess<…>`, owned elsewhere). No field name on this page is fabricated: every one joins to a recovered validator/pybind string (`s3d3_ts`, `s2s2d2_stt`, `s2d2_ts_as`, `exponential_*`, `tensor_scalar_cache_*`) or a const-purpose. There is no NEFF-fixture hexdiff against a real emitted bundle — absolute positions are from the GENERATE-arm stores plus the BIR offset pins.

---

## Cross-References

- [2.10 PE Matmul Encoding](pe-matmul-encoding.md) — the sibling encoding page; same field-table shape, the dtype wire-tag LUT (`byte_1DF5760`/`byte_1DFBAD0`) shared with this family, and the `assignAccess`/`setupHeader` bundle lifecycle.
- [2.1 The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — Family A, the `setupHeader` skeleton and the `CodeGenMode` arms shared by every encoder here.
- [2.2–2.5 ADDR4 / Tensor descriptors / MEM_PATTERN](addr4.md) — the `0x28..0x3F` AP descriptors (`TENSOR3D`/`TENSOR2D`/`MEM_PATTERN3D`) and the dtype ordinal→name table.
- [2.23 ISA Enum Ordinals](isa-enum-ordinals.md) — the full `AluOpType` ordinal table the `op0`/`op1` wire bytes resolve from, and the `EngineAccumulationType` / `TensorScalarReverseOps` / `TSCMode` enums.
- [Part 7 — BIR Codegen Tensor-Scalar (I05) + AluOpType (7.7)](../bir/) — the upstream `InstTensorScalarPtr`/`InstTensorScalarCache`/`InstExponential` that feed these encoders, and the `AluOpType` definition.
- [Build & Version Provenance](../reference/versions.md) — the single pinned build all addresses are read from.
