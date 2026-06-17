# Pool / TensorReduce / Reciprocal / Iota Encoding

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core logic). The encoders, look-up tables and the pybind assert roster live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331…`); the BIR enums (`NEURON_ISA_TPB_DTYPE`, `bir::PoolFunctionType`, `bir::AluOpType`, `bir::AxisListType`) live in `libBIR.so` (`a9b1ea38`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the **Family-C control band** — the `+0x20..+0x2F` block of the 64-byte bundle that the **Pool / reduce leg** of the TPB writes for its scalar-ish, window-reduction opcodes. The engine-level model (which BIR op becomes which opcode, and *why*) is [1.09 the Pool engine](../arch/pool-engine.md); this page is the bit layout that lets a reimplementer **byte-encode** one of these instructions by hand.

Five `CoreV2GenImpl::visitInst*` bodies are covered, producing six distinct wire opcodes:

- **`InstPool`** → opcode **`0x45`** (69), wire struct `S4D4_PL` — 2-D average/max pooling.
- **`InstTensorReduce`** → opcodes **`124`/`125`** (free-axis, `S4D4_TR`) **or** **`66`/`82`/`131`/`132`** (cross-lane, `S4D4_CR`) — forked on the BIR `axis`.
- **`InstReciprocal`** → opcode **`0x48`** (72) — the leanest Family-C op, just `1/x`.
- **`InstIota`** → opcode **`0x7E`** (126), wire struct `D4_IOTA` — a synthesised counting pattern, *no input ifmap*.
- **`InstGather`** → a **co-issued pair** `{reg_load 0x68 + pool_buffer_load 0x67}` (wire struct `S4_PB` for the latter) — gather lowers to a buffer-load of indices plus a register/vector load.

Every bundle is a `std::array<unsigned char, 64>`: emplaced into a `SmallVector`, fully `pxor`/`movups`-zeroed (so any byte the encoder does not write is a hard `0x00`), header-stamped by `setupHeader`, field-filled, validated by the ISA checker, and `fwrite(buf, 1, 0x40, bin)`-ed ([2.1 the bundle](instruction-bundle.md), Family C). The three fields at the *head* of the control band — in-dtype `+0x20`, out-dtype `+0x21`, lane `+0x22` — are shared by the whole leg; the per-opcode fields `+0x23..+0x2F` diverge and are mapped one section per op below.

The bar for this page: a reader can encode any Pool / TensorReduce / Reciprocal / Iota / pool-buffer-load instruction by hand, knowing for each control byte its offset, width, semantic, value source, the pybind assert string that *names* it, and the disassembly store-site that pins it. Every field row carries a confidence tag (CONFIRMED = exact store/cmp disassembled byte-exact; STRONG = validator / LUT / pybind-roster xref; INFERRED = zero-init implied, no direct store; SPECULATIVE). Bit positions are pinned against literal store constants in the disassembly, never inferred. Where a byte has no recovered name, it is tagged INFERRED/reserved — no field name is fabricated.

## At a glance

The whole family shares one bundle skeleton: bytes `+0x00..+0x03` are the header, the access-pattern sub-bands sit at `+0x0C`/`+0x10` (src) and `+0x2C`/`+0x30` (dst), and `+0x20..+0x2F` is the instruction-specific control band that this page maps.

| Opcode | Generator (`CoreV2GenImpl`, libwalrus) | Wire struct | Validator | Operand band(s) |
|---|---|---|---|---|
| `0x45` Pool | `visitInstPool` `@0x1239e50` (3236 B) | `S4D4_PL` | `dbg_is_valid_s4d4_pl @0x1318b90` | src/dst `TENSOR4D` @`+0x0C`/`+0x2C` |
| `124`/`125` TR | `visitInstTensorReduce` `@0x12383a0` (5807 B) | `S4D4_TR` | `dbg_is_valid_tensor_reduce @0x132d310` | src/dst `TENSOR4D` |
| `66`/`82`/`131`/`132` CR | `visitInstTensorReduce` `@0x12383a0` | `S4D4_CR` | `dbg_is_valid_cross_lane_reduce @0x130fe80` | src/dst `TENSOR4D` |
| `0x48` Reciprocal | `visitInstReciprocal` `@0x1239a50` (1024 B) | reciprocal struct | `dbg_is_valid_reciprocal @0x1321830` | src/dst `TENSOR4D` |
| `0x7E` Iota | `visitInstIota` `@0x123d4a0` (4208 B) | `D4_IOTA` | `dbg_is_valid_iota @0x12a0220` | dst `TENSOR4D` @`+0x2C` only |
| `0x67`+`0x68` Gather | `visitInstGather` `@0x12532e0` (4976 B) | `S4_PB` + reg_load | `dbg_is_valid_pool_buffer_load @0x1300540` | src `TENSOR4D` |

**Header skeleton (all six opcodes), from `setupHeader` (vtable slot 9, `call *0x48(%rax)`):**

```
 byte +0x00  opcode        (Pool 0x45, TR 124/125, CR 66/82/131/132, Recip 0x48, Iota 0x7E, PBL 0x67, RegLoad 0x68)
 byte +0x01  inst_word_len = 0x10  (16 dwords = 64 bytes)
 byte +0x02..+0x03  reserved = 0x0000
```

The 16-bit word at `bundle[0:2]` is `(0x10<<8) | opcode`, little-endian. The opcode reaches `setupHeader` as a 1-byte seed placed in a stack scratch slot (`movb $op,-0xNN(%rbp)`) just before the call. CONFIRMED for every op by its seed store and validator `cmp` (below).

> **NOTE — `inst_word_len = 0x10` is hard-pinned for the whole TPB compute ISA.** Sixteen dwords is exactly 64 bytes; this is why every `visitInst*` emplaces a `std::array<uchar,64>` and `fwrite`s `0x40`.

---

## The bundle lifecycle (shared by all five bodies)

Every body runs the same cycle; the `CodeGenMode` at `*(this+156)` selects the sink (`cmp $0x1`/`$0x2` at the body head):

1. **Emplace + zero.** `SmallVectorImpl<std::array<u8,64>>::emplace_back(this+120)`, then `pxor xmm0` + `movups xmm0,[base+0x00/0x10/0x20/0x30]` blanket the whole 64 bytes. *Consequence: every reserved/unwritten byte reads `0x00`.* The bundle base lands in a callee-saved register: Reciprocal `%r12`, Pool/TR-CR/Iota `%r13`, the TR-store sub-block `%r15`, Gather `%r14` (reg_load) / `%r15` (pool_buffer_load).
2. **Header.** `setupHeader` writes `bundle[0]=opcode`, `bundle[1]=0x10`, `bundle[2:3]=0`.
3. **Sync.** `setupSyncWait<…_STRUCT> @0x1228970` / `setupSyncUpdate<…_STRUCT> @0x1230f80` render the semaphore wait/update command regions (N-strand common code, interior layout out of scope here).
4. **Access patterns.** `assignAccess<NEURON_ISA_TPB_TENSOR4D> @0x603ee0` renders a `bir::AccessPattern` into the in-bundle wire descriptor at `+0x0C` (src) / `+0x2C` (dst).
5. **Control band.** the per-opcode field fills mapped below.
6. **ISA check + emit.** `runISACheck (sub_12095A0)` appends the bundle pointer to the engine stream and fires the in-place validity check, then `fwrite(bundle,1,0x40,findBin(inst))`.

`CodeGenMode` arms: **1 = GENERATE_ISACODE** (real bundle in a callee-saved reg, then `fwrite`); **2 = RUN_ISA_CHECKS** (builds a *byte-identical* field layout on a stack scratch struct, e.g. `-0xb0(%rbp)`, calls the validator via vtable slot 0 `call *(%rax)`, *no* `emplace_back`, *no* `fwrite`); **0 = COLLECT_OPCODES** (`movl $<opcode>,-0xb0(%rbp)` + `_Rb_tree` insert into the per-inst opcode set, the cleanest opcode witness).

> **GOTCHA — every body has a near-duplicate field-store block.** The mode-2 stack-scratch block mirrors the mode-1 bundle block field-for-field. Do not double-count it as a second emitted bundle. (Reciprocal in particular has *two* `movb $0x48` seeds — GENERATE at `-0xb1(%rbp)` and RUN_ISA_CHECKS at `-0x70(%rbp)` — but emits exactly one bundle.)

### Shared field-build helpers (CONFIRMED — addresses re-resolved this task)

| Helper | Address | Role | Output → |
|---|---|---|---|
| `sub_120E650` | `@0x120E650` | `bir::Dtype → NEURON_ISA_TPB_DTYPE` wire-tag; byte-LUT (BIR `0x00..0x13` → ISA dtype); `edi = AP.Dtype@+0x30` | `+0x20` / `+0x21` |
| `sub_12039C0` | `@0x12039C0` | `CoreV2Convert::convert(AluOpType) → ISA ALU_OP` byte; `0..18` identity, comparison family reorders (`19→24,20→19,21→20,23→21,28→29,29→25`); a `jmp *%rax` jump-table (a `0x19`=25 default-case arm is visible) | TR reduce-op `+0x24` |
| `sub_120EAB0` | `@0x120EAB0` | `AxisListType → ISA axis` byte `= (a1>3 ? assert : a1+2)`: `X(0)→2, XY(1)→3, XYZ(2)→4, XYZW(3)→5` | TR axis `+0x25` |

CONFIRMED: `sub_120EAB0` shows `cmp $0x3,%edi` then `add $0x2,%eax` at `0x120eac5`/`0x120ead4`; `sub_12039C0` ends in `add %rdx,%rax ; jmp *%rax` at `0x12039ec`.

---

## The Family-C control band (`+0x20..+0x2F`) — common skeleton

The control band sits between the source descriptor (`+0x0C`/`+0x10`) and the dest descriptor (`+0x2C`/`+0x30`). Three fields are common to the whole Pool / reduce leg:

| Off | W | Field | Value source | Tag |
|---|---|---|---|---|
| `+0x20` | 1 | IN dtype wire-tag | `sub_120E650(in.Dtype@+0x30)` | CONFIRMED |
| `+0x21` | 1 | OUT dtype wire-tag | `sub_120E650(out.Dtype@+0x30)` | CONFIRMED |
| `+0x22` | 1 | LANE / partition count | `in.Pattern[0].num` (= `*(*(AP+0x50)+8)`) | CONFIRMED |

Iota is the exception: it has **no** in-dtype, so `+0x20` is reserved-zero and `+0x21` carries the *output* dtype (see §4). Every body guards `in.Pattern.size@+0x58 != 0` (`test ; je <assert>` → `"idx < size()"` — a zero-dim AP is illegal).

---

## 1 — `visitInstPool` (opcode `0x45` / `S4D4_PL`)

The pooling reduction. BIR field `InstPool.func @+0xF0 = PoolFunctionType {0=Max, 1=Avg}` — *no Sum, no Min*. The pooling window is **not a discrete field**: the window `N = in.Pattern[3].num · in.Pattern[4].num` (the two innermost AP dims), and for average pooling it is folded into a single reciprocal constant `1/N` written at `+0x28`, so the silicon does sum-then-multiply, never a divide ([1.09 the window-as-reciprocal trick](../arch/pool-engine.md)). GEN base reg `%r13`.

**Field stores (GENERATE path, byte-exact):**

```asm
1239fd9:  movb  $0x45,-0x270(%rbp)        ; opcode seed → setupHeader
123a023:  mov   %al,0x20(%r13)            ; in  dtype = sub_120E650(in0.Dtype)
123a037:  mov   %al,0x21(%r13)            ; out dtype = sub_120E650(out.Dtype)
123a04f:  mov   %al,0x22(%r13)            ; lane = in0.Pattern[0].num
123a068:  movb  $0x1,0x24(%r13)           ; func: Max(0) → wire 1
123a06d:  movl  $0x3f800000,0x28(%r13)    ; scale = 1.0f  (Max path)
123a075:  movb  $0x3,0x25(%r13)           ; mode = 3 (Pool sub-mode const)
;  Avg path:
123a45c:  movb  $0x2,0x24(%r13)           ; func: Avg(1) → wire 2
123a977:  movss %xmm0,0x28(%r13)          ; scale = 1.0f / N
```

| Off | W | Field | pybind (`s4d4_pl`/`d4_pl`) | Value | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x45` | `s_s4d4_pl_opcode` | 69 | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | (setupHeader) | 16 | CONFIRMED |
| `+0x0C` | * | src AP (T4D) | `valid_s4d4_pl_ap` | — | CONFIRMED |
| `+0x20` | 1 | in_dtype | `d4_pl_dtype_check` (in arm) | `NEURON_ISA_TPB_DTYPE` | CONFIRMED |
| `+0x21` | 1 | out_dtype | `d4_pl_dtype_check` (out arm) | `NEURON_ISA_TPB_DTYPE` | CONFIRMED |
| `+0x22` | 1 | lane / part cnt | `valid_subdim` | `in0.Pattern[0].num` | CONFIRMED |
| `+0x24` | 1 | pool func | `valid_pooltype` | Max→`1`, Avg→`2` | CONFIRMED |
| `+0x25` | 1 | mode / subdim | `valid_subdim` | `3` (const) | CONFIRMED |
| `+0x28` | 4 | pool_scale (f32) | `"pool_scale must be …"` | `1.0f` / `1.0f / N` | CONFIRMED |
| `+0x2C` | * | dst AP (T4D) | `valid_s4d4_pl_ap` | — | CONFIRMED |
| `+0x23`,`+0x26`,`+0x27`,`+0x29..+0x2B`,`+0x2D..+0x2F` | — | reserved-zero | `d4_pl_reserved_z` | `0` | INFERRED |

> **CORRECTION — `func@+0x24` carries a `+1` shift.** BIR `PoolFunctionType {Max=0,Avg=1}` maps to **wire `{Max→1, Avg→2}`** — wire `0` is reserved/invalid. The validator `dbg_is_valid_s4d4_pl @0x1318b90` pins the opcode with `cmp $0x45,%al @0x1318d66` (CONFIRMED). An unimplemented func reports `"has unimplemented Pooling type"` but still emits.
>
> **GUARD (Avg only):** the Avg arm asserts `in0.size()==5` (it must have the two window dims) and rejects `"POOL with Average Function doesn't support TensorIndirect AP as src AP"`.

---

## 2 — `visitInstTensorReduce` (`@0x12383a0`) — two datapaths

`InstTensorReduce` carries `reduce_op @+0xF0` (`AluOpType`), `axis @+0xF4` (`AxisListType`), an `apply_transpose` `MaybeAffine {tag@+0xF8}`, and a `negate` `MaybeAffine {val@+0x120}`. The encoder **forks on `axis`** into two physically distinct datapaths sharing one BIR op:

- **Free-axis (TR, `S4D4_TR`)** — `axis>1` (`X..XYZW`, BIR `0..3`): reduces innermost loop dimensions. Field block at base `%r15`.
- **Cross-lane (CR, `S4D4_CR`)** — `axis<=1` (`XYZWC=4`, `C=5`): reduces across the partition (channel) axis. Field block at base `%r13`.

> **NOTE — different reduce-op channels.** TR carries the *full* `AluOp→ALU_OP` wire byte at `+0x24` (via `sub_12039C0`) **plus** the axis byte at `+0x25` **plus** a negate at `+0x23`. CR carries a *separate* 6-entry inline reduce-cmd at `+0x23`, a cross-lane *flag* at `+0x24`, and the average mean-fold at `+0x28` — **no** axis byte, **no** negate. `bir::ReduceCmdType {Idle/Reset/Reduce/ResetReduce}` is dormant (no Instruction consumer); the live reduce command is this inline CR switch.

**Opcode selection (CONFIRMED disasm):**

```asm
1238470:  sub  $0x7d,%r13d              ; TR opcode 125 (bitvec-int)
1238738:  sub  $0x7c,%r13d              ; TR opcode 124 (float)
123845c:  add  $0x83,%ebx              ; CR opcode 131  (apply_transpose tag1, non-bitvec)
1238724:  add  $0x84,%ebx              ; CR opcode 132  (apply_transpose tag1, bitvec)
```

CR opcode = `f(isBitVec, apply_transpose-tag@+0xF8)`: non-bitvec/tag0 → **66**, bitvec/tag0 → **82**, non-bitvec/tag1 → **131**, bitvec/tag1 → **132**. The bf16-deduction path goes through `tensor_reduce_bf16 @0x132c5a0`.

### 2A — Free-axis (TR), opcode 124/125, base `%r15`

```asm
1239367:  mov  %al,0x20(%r15)          ; in  dtype = sub_120E650(in.Dtype)
123937b:  mov  %al,0x21(%r15)          ; out dtype = sub_120E650(out.Dtype)
12393ba:  mov  %al,0x22(%r15)          ; lane = in.Pattern[0].num
123938b:  mov  %al,0x24(%r15)          ; reduce-op = sub_12039C0(op@+0xF0)
12393a2:  mov  %al,0x25(%r15)          ; axis = sub_120EAB0(axis@+0xF4) = axis+2
12393e9:  mov  %al,0x23(%r15)          ; negate = movzbl inst.negate@+0x120
```

### 2B — Cross-lane (CR), opcode 66/82/131/132, base `%r13`

```asm
1238ac5:  mov  %al,0x20(%r13)          ; in  dtype
1238ad8:  mov  %al,0x21(%r13)          ; out dtype
1238afc:  mov  %al,0x22(%r13)          ; lane = in.Pattern[0].num
1238ae3:  mov  %al,0x23(%r13)          ; reduce-cmd (6-entry inline switch on op@+0xF0)
1239650:  movb $0x0,0x24(%r13)         ; cross-lane flag: axis XYZWC(4) → 0 (OFF)
12397c0:  movb $0x1,0x24(%r13)         ; cross-lane flag: axis C(5)     → 1 (ON)
12396a7:  movss %xmm0,0x28(%r13)       ; mean 1/N (average op only)
```

The **CR reduce-cmd** at `+0x23` is a 6-entry switch on the BIR ALU op (NOT the full `AluOp` table): `add(4)→0`, `average(24)→1`, `max(8)→2`, `bitwise_or(11)→3`, `bitwise_and(10)→4`, `bitwise_xor(12)→5`, else → assert `"false"`. The cross-lane flag at `+0x24` rejects any axis other than `C`/`XYZWC` (`"getAxis() == bir::AxisListType::XYZWC"`).

| Off | W | Field | pybind | Value | Tag |
|---|---|---|---|---|---|
| **TR** | | | | | |
| `+0x00` | 1 | opcode | `s_transpose_tensor_reduce_opcode` | 124/125 | CONFIRMED |
| `+0x20` | 1 | in_dtype | `tensor_reduce_in_dt` / `deduce_bf16_in_dt` | ISA_DTYPE | CONFIRMED |
| `+0x21` | 1 | out_dtype | `d4_tr_…dtype` | ISA_DTYPE | CONFIRMED |
| `+0x22` | 1 | lane | `transpose_src_element_count` | `in.Pattern[0].num` | CONFIRMED |
| `+0x23` | 1 | negate | (negate field) | `inst.negate@+0x120` | CONFIRMED |
| `+0x24` | 1 | reduce_op | `tensor_reduce_valid_alu_op` | `sub_12039C0(AluOp)` | CONFIRMED |
| `+0x25` | 1 | axis | `valid_reduce_n` | `axis+2` | CONFIRMED |
| `+0x26..+0x2F` | — | reserved-zero | `d4_tr_reserved_z` | `0` | INFERRED |
| **CR** | | | | | |
| `+0x00` | 1 | opcode | `s_cross_lane_reduce_opcode` | 66/82/131/132 | CONFIRMED |
| `+0x20` | 1 | in_dtype | `d4_cr_…` (in arm) | ISA_DTYPE | CONFIRMED |
| `+0x21` | 1 | out_dtype | `d4_cr_…` (out arm) | ISA_DTYPE | CONFIRMED |
| `+0x22` | 1 | lane | `valid_axis_dst_element_cnt` | `in.Pattern[0].num` | CONFIRMED |
| `+0x23` | 1 | reduce_cmd | `valid_reduce_op_for_opcode` | `{0..5}` (6-entry) | CONFIRMED |
| `+0x24` | 1 | cross_lane flag | `valid_reduce_axis` | `C→1, XYZWC→0` | CONFIRMED |
| `+0x28` | 4 | mean 1/N (f32) | (mean) | `1.0f/N` (avg only) | CONFIRMED |
| `+0x25..+0x27`,`+0x29..+0x2F` | — | reserved-zero | `d4_cr_reserved_z` | `0` | INFERRED |

> **GOTCHA — CR has no negate.** `"Cross-lane-reduce cannot perform negate"`. CR also rejects TensorIndirect AP: `"TODO: support CROSS_LANE_REDUCE with TensorIndirect AP once ISA supports it"`.

---

## 3 — `visitInstReciprocal` (opcode `0x48` / 72)

The `1/x` op — the leanest Family-C bundle. It needs no per-instruction parameter (unlike Avg pool's `1/N`), so it carries only `{in-dtype, out-dtype, lane, mode=0, src/dst AP}`. GEN base reg `%r12`. pybind: `s_reciprocal_opcode`.

```asm
1239bb6:  movb  $0x48,-0xb1(%rbp)       ; opcode seed (GENERATE)
1239bf9:  mov   %al,0x20(%r12)          ; in  dtype = sub_120E650(in.Dtype@+0x30)
1239c07:  mov   %al,0x21(%r12)          ; out dtype = sub_120E650(out.Dtype@+0x30)
1239c31:  movb  $0x0,0x25(%r12)         ; mode = 0  (Reciprocal sub-mode)
1239c3a:  mov   %al,0x22(%r12)          ; lane = in.Pattern[0].num
```

| Off | W | Field | pybind | Value | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x48` | `s_reciprocal_opcode` | 72 | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | (setupHeader) | 16 | CONFIRMED |
| `+0x0C` | * | src AP (T4D) | (valid ap) | — | CONFIRMED |
| `+0x20` | 1 | in_dtype | (reciprocal dtype) | ISA_DTYPE | CONFIRMED |
| `+0x21` | 1 | out_dtype | (reciprocal dtype) | ISA_DTYPE | CONFIRMED |
| `+0x22` | 1 | lane | (num-elem) | `in.Pattern[0].num` | CONFIRMED |
| `+0x25` | 1 | mode | (subdim/mode) | `0` (const) | CONFIRMED |
| `+0x2C` | * | dst AP (T4D) | (valid ap) | — | CONFIRMED |
| `+0x23`,`+0x24`,`+0x26..+0x2F` | — | reserved-zero | (reserved_z) | `0` | INFERRED |

> **CORRECTION — Reciprocal opcode is `0x48` (72), NOT `0x7E`.** `0x7E` is Iota (§4). Grounded three ways: the GENERATE seed `movb $0x48,-0xb1(%rbp) @0x1239bb6`, the validator `dbg_is_valid_reciprocal @0x1321830` with `cmp $0x48,%r8b @0x1321990`, and the Iota validator using `cmp $0x7e,%r14b` for its own opcode.
>
> **QUIRK — the `+0x25` byte is shared by Pool and Reciprocal but carries a different constant:** Pool writes `3`, Reciprocal writes `0`. There is **no** func byte at `+0x24`, no scale at `+0x28`, no negate/axis on Reciprocal.

---

## 4 — `visitInstIota` (opcode `0x7E` / 126 / `D4_IOTA`)

Iota synthesises a counting pattern — it has **no data input**. Three consequences shape its encoding:

- there is **no in-dtype store at `+0x20`** (the body contains zero `mov %al,0x20(%r13)` stores — CONFIRMED by exhaustive grep over `0x123d4a0..0x123e510`);
- the **source access-pattern band `+0x10..+0x1E` is built manually** as a degenerate descriptor — unit sentinels plus the output geometry's dim counts, all `u16`;
- only **one** `assignAccess<TENSOR4D>` runs (the OUTPUT, → `+0x2C`).

BIR: `addIota(out_MemLoc, out_AP, value:uint const&, channel, …, pattern_AP)`. `inst+0x120` is the axis/pattern-type (checked `==4` on one arm); `inst+0x168` is the 32-bit iota value/step (→ `+0x24`); `inst+0x188` is a guard byte (must be 0, else `out_of_range` throw). pybind: `s_iota_opcode`, `d4_iota_reserved_z`, `iota_same_src_dst_count`. GEN base reg `%r13`.

```asm
123d58b:  movl  $0x7e,-0xb0(%rbp)       ; opcode seed (COLLECT/GEN)
123d87a:  mov   %ax,0x10(%r13)          ; src dim0 sentinel = 1 (mov $0x1,%ax)
123d884:  mov   %ax,0x18(%r13)          ; src dim  sentinel = 1
123d892:  mov   %al,0x21(%r13)          ; OUT dtype = sub_120E650(out.Dtype@+0x30)
123d8aa:  mov   %al,0x22(%r13)          ; lane = out.Pattern[0].num
123d8d7:  mov   0x168(%rbx),%eax        ; load iota value/step
123d8dd:  mov   %eax,0x24(%r13)         ; IOTA PATTERN/VALUE (32-bit)
```

The remaining `+0x12`/`+0x14`/`+0x16` and `+0x1A`/`+0x1C`/`+0x1E` `u16` writes carry the src dim counts / valid sentinels derived from the output geometry; `checkIota @0xfc7050` gates them with `"ISA requirement: Data Pattern Num (…) cannot exceed uint16 range"`.

| Off | W | Field | pybind | Value | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x7E` | `s_iota_opcode` | 126 | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | (setupHeader) | 16 | CONFIRMED |
| `+0x10..+0x1E` | 2× | src pattern band | `iota_same_src_dst_count` | unit + dim sentinels | CONFIRMED |
| `+0x21` | 1 | out_dtype | (iota dtype) | ISA_DTYPE | CONFIRMED |
| `+0x22` | 1 | lane | (num-elem) | `out.Pattern[0].num` | CONFIRMED |
| `+0x24` | 4 | iota_pattern | (iota value/step) | `inst+0x168` (u32) | CONFIRMED |
| `+0x2C` | * | dst AP (T4D) | (valid ap) | — | CONFIRMED |
| `+0x20`,`+0x23`,`+0x25..+0x2B`,`+0x2D..+0x2F` | — | reserved-zero | `d4_iota_reserved_z` | `0` | INFERRED |

> **QUIRK — `+0x20` is reserved-zero on Iota.** It is the only Family-C op where `+0x20` is *not* the in-dtype: Iota has no ifmap, and `d4_iota_reserved_z` asserts `+0x20` is `0`. The output dtype rides `+0x21` exactly as the other ops do. The `iota_same_src_dst_count` predicate cross-checks that the manual `+0x10..+0x1E` src band's counts equal the dst counts.

---

## 5 — `visitInstGather` (`@0x12532e0`) — the `{reg_load 0x68 + pool_buffer_load 0x67}` pair

`InstGather` does **not** lower to a single visitor — it emits a **co-issued micro-op pair**: a `reg_load` (opcode `0x68`/104, the index/vector load) and a `pool_buffer_load` (opcode `0x67`/103, wire struct `S4_PB`, the load into the pool buffer). Two `emplace_back`, two `fwrite`. The census loop after each `fwrite` scans the engine stream for `0x66`/`0x67`/`0x68` (`cmpl $0x6N,0x20(%rXX)`), confirming the pair is real, not a disasm artefact. The pybind groups `s4_pb_*` next to `reg_load`/`rl_`/`gload_*` exactly because gather decomposes this way.

GEN: bundle #1 (reg_load `0x68`) base `%r14`; bundle #2 (pool_buffer_load `0x67`) base `%r15`.

```asm
125372e:  movb  $0x68,-0x70(%rbp)       ; reg_load opcode seed (bundle #1)
1253756:  mov   %al,0x20(%r14)          ; reg_load in  dtype
125376b:  mov   %al,0x21(%r14)          ; reg_load out dtype
1253785:  mov   %al,0x22(%r14)          ; reg_load lane = in.Pattern[0].num
125379c:  mov   %ax,0x24(%r14)          ; reg_load active-channels / vector-depth (u16)
125377d:  movl  $0x0,0x28(%r14)         ; reg_load param = 0
;  --- bundle #2: pool_buffer_load ---
1253a58:  movb  $0x67,-0xb9(%rbp)       ; pool_buffer_load opcode seed (bundle #2)
1253a88:  mov   %r14d,0x28(%r15)        ; POOL-BUFFER ELEMENT-COUNT (load span)
1253a9a:  mov   %al,0x20(%r15)          ; pool_buffer_load dtype = sub_120E650(in.Dtype@+0x30)
1253aa1:  movl  $0x1ff,0x2c(%r15)       ; vector-depth / mask = 0x1FF (511)
1253ab0:  mov   %cl,0x22(%r15)          ; pool_buffer_load lane = in.Pattern[0].num
1253b27:  mov   %dx,0x18(%r15)          ; src-AP dim count (u16)
```

**pool_buffer_load (opcode `0x67`, `S4_PB`):**

| Off | W | Field | pybind | Value | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x67` | `s_pool_buffer_load_opcode` | 103 | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | (setupHeader) | 16 | CONFIRMED |
| `+0x0C` | * | src AP (T4D) | (valid ap) | — | CONFIRMED |
| `+0x18` | 2 | src dim count | (`s4_pb` subdim) | out geometry | CONFIRMED |
| `+0x20` | 1 | dtype | `gload_valid_dtype` | ISA_DTYPE (range-guarded `cmp $0x13`) | CONFIRMED |
| `+0x22` | 1 | lane | `valid_pool_buffer_load_element_cnt` | `in.Pattern[0].num` | CONFIRMED |
| `+0x28` | 4 | element-count | `valid_pool_buffer_load_element_count` | load span | CONFIRMED |
| `+0x2C` | 4 | vector-depth / mask | `gload_vector_depth` | `0x1FF` (const) | CONFIRMED |
| `+0x21`,`+0x23..+0x27`,`+0x29..+0x2B`,`+0x2D..+0x2F` | — | reserved-zero | `s4_pb_reserved_zero` | `0` | INFERRED |

**reg_load (opcode `0x68`):**

| Off | W | Field | pybind | Value | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0x68` | `s_reg_load_opcode` | 104 | CONFIRMED |
| `+0x20` | 1 | in_dtype | `gload_valid_dtype` | ISA_DTYPE | CONFIRMED |
| `+0x21` | 1 | out_dtype | `gload_valid_dtype` | ISA_DTYPE | CONFIRMED |
| `+0x22` | 1 | lane | (num-elem) | `in.Pattern[0].num` | CONFIRMED |
| `+0x24` | 2 | active-channels | `gload_active_channels` | u16 | CONFIRMED |
| `+0x28` | 4 | reg_load param | `rl_reserved_zero` / param | `0` | CONFIRMED |
| reserved | — | reserved-zero | `rl_reserved_zero` | `0` | INFERRED |

> **GOTCHA — no `+0x21` out-dtype on the pool_buffer_load bundle.** It is a one-sided load *into* the pool buffer; only the single load dtype at `+0x20` is meaningful (and the validator range-guards it with `cmp $0x13` on the BIR dtype). The reg_load half *does* carry both `+0x20`/`+0x21`.
>
> **CORRECTION — `0x67` is the GATHER lowering, not a standalone "pool-buffer-load" visitor.** It is emitted by `visitInstGather @0x12532e0`, co-issued with `reg_load 0x68` — not by `visitInstTensorLoad`. The `+0x24`/`+0x2C` `u16`/`u32` split (active-channels vs vector-depth) is named from the `gload_*` roster; the exact slot order within those near-`+0x24`/`+0x2C` fields is INFERRED.

---

## Lookup tables / enum crosswalks (re-grounded this task)

| Map | Function | Behaviour | Drives |
|---|---|---|---|
| Dtype → wire tag | `sub_120E650 @0x120E650` | `edi = AP.Dtype@+0x30`; byte LUT (BIR `0x00..0x13` → ISA dtype); `"Unknown dtype"` diag | `+0x20` (all) / `+0x21` (Pool/TR/CR/Recip/reg_load/Iota-out) |
| AluOp → ISA ALU_OP | `sub_12039C0 @0x12039C0` | `0..18` identity; comparison reorder (`19→24,20→19,21→20,23→21,28→29,29→25`) | TR reduce-op `+0x24` |
| Axis → ISA axis | `sub_120EAB0 @0x120EAB0` | `a1>3 ? assert : a1+2`; `X→2,XY→3,XYZ→4,XYZW→5` | TR axis `+0x25` |
| CR reduce-cmd | inline switch (`@0x1238ae3`) | `add4→0, average24→1, max8→2, or11→3, and10→4, xor12→5` | CR `+0x23` |

---

## Confidence ledger

**CONFIRMED** (direct store/cmp disassembled byte-exact this task):

- Opcodes pinned by validator `cmp`: Pool `0x45` (`@0x1318d66`), Reciprocal `0x48` (`@0x1321990`), Iota `0x7E` (`@0x12a03a8`), pool_buffer_load `0x67` (`@0x130069b`); TR `124`/`125` (seeds `@0x1238470`/`@0x1238738`), CR transpose `131`/`132` (seeds `@0x123845c`/`@0x1238724`).
- Pool field stores `+0x20`/`+0x21`/`+0x22`/`+0x24`/`+0x25`/`+0x28` (byte-exact addresses, both Max and Avg arms).
- TR `{+0x20..+0x25}` (reduce-op via `sub_12039C0`, axis via `sub_120EAB0`, negate from `inst+0x120`) and CR `{redcmd +0x23, X-lane flag +0x24 (C→1/XYZWC→0), mean +0x28}`.
- Reciprocal `{+0x20,+0x21,+0x22, mode0 +0x25}`; one bundle only (2nd `0x48` seed = ISA-check).
- Iota `{NO in-dtype, out-dtype +0x21, lane +0x22, 32-bit pattern +0x24 from inst+0x168, manual src band +0x10/+0x18 u16}`.
- Gather → `{reg_load 0x68 + pool_buffer_load 0x67}` pair; pool_buffer_load `{+0x18 srcdim, +0x20 dtype, +0x22 lane, +0x28 elem-count, +0x2c 0x1FF}`; reg_load `{+0x20/+0x21 dtype, +0x22 lane, +0x24 u16, +0x28 param=0}`.
- Shared helpers re-resolved: `sub_120E650` dtype, `sub_12039C0` AluOp (`jmp *%rax` jump-table), `sub_120EAB0` axis+2 (`cmp $0x3`/`add $0x2`).

**STRONG** (validator / pybind xref): wire struct tags `S4D4_PL` / `S4D4_TR` / `S4D4_CR` / `S4_PB` from the `dbg_is_valid_*` symbol names; the pybind field-name → offset joins (`s_*_opcode`, `d4_*_dtype_check`, `valid_pooltype`, `valid_subdim`, `pool_scale`, `valid_pool_buffer_load_element_count`, the `*_reserved_z` predicates) are name-matched from the packed predicate-name tables, high confidence per-op.

**INFERRED** (zero-init only, asserted zero by `*_reserved_z`): all reserved-zero pad bytes per section (covered by the `movups`-xmm0 bulk init; the `d4_*_reserved_z` / `s4_pb_reserved_zero` predicates assert them `0`). Iota `inst+0x120` (`==4` axis/pattern-type) and `inst+0x188` (guard byte) semantic labels are inferred from the guard branches; the value/step at `inst+0x168` is CONFIRMED as the `+0x24` source by the direct `mov`.

**OPEN / cross-ref:** exact sub-bit field widths *within* the `S4D4`/`S4_PB` sync-command regions and the `TENSORxD` descriptor interior (handled by `assignAccess`/`setupSync*`, N-strand common code — see [2.4 TENSOR4D / MEM_PATTERN4D](tensor4d-mempattern4d.md)); the reg_load `+0x24` u16 vs `+0x2C` split slot order.

## Cross-References

- [1.09 — The Pool Engine](../arch/pool-engine.md) — the engine model: op→datapath→opcode routing, the window-as-reciprocal-fold, the free-axis-vs-cross-lane fork (this page is its bit-level companion).
- [2.1 — The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the Family-C bundle shape and the `+0x20..+0x2F` control-band convention.
- [2.9 — PE Matmul Encoding](pe-matmul-encoding.md) — the sibling Family-A encoding page; same lifecycle, same `setupHeader`/`assignAccess`/ISA-check skeleton.
- [2.4 — TENSOR4D / MEM_PATTERN4D](tensor4d-mempattern4d.md) — the `+0x0C`/`+0x2C` operand descriptor interiors rendered by `assignAccess<TENSOR4D>`.
- Part 7 (`../bir/`) — codegen TensorReduce (I07): the front-end pass that fixes `reduce_op`/`axis`/`negate` long before this encoder reads them.
