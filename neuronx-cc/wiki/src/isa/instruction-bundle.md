# The 64-Byte Instruction Bundle & Header Skeleton

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The encoder bodies live in `libwalrus.so` (build-id `92b4d331`; `.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset); the `bir::InstructionType` enum and engine ordinals come from `libBIR.so` (build-id `a9b1ea38`). Other wheels differ — treat every address as version-pinned. See [versions](../reference/versions.md).*

## Abstract

Every TPB engine stream — PE, Pool, Act, DVE, SP, DMA — is a flat array of **fixed 64-byte instruction bundles**. There is no variable-length encoding, no bundle framing, no per-op length field that ever takes a value other than 16. The backend (`neuronxcc::backend::CoreV{2,3,4}GenImpl`) builds each bundle the same way: `emplace_back` a `std::array<u8,64>`, **zero all 64 bytes**, stamp a 4-byte header, then let the per-op `visitInst*` body fill the body, then `fwrite(bundle, 1, 0x40, …)`.

The single piece of structure that is **truly universal** across all ops and all three generations is the **4-byte header word** at `+0x00`: `{opcode, 0x10, 0x00, 0x00}`. It is written by one function, `setupHeader`, which is **byte-for-byte identical** in `CoreV2`, `CoreV3`, and `CoreV4` — six instructions, no data dependence on anything but the opcode byte. `byte[1] = 0x10` is `inst_word_len` = 16 dwords = 64 bytes, a hardcoded immediate. That is the whole header.

The other 60 bytes are **not** a fixed struct. They are an op-specific union body (the `NEURON_ISA_TPB_INST_UNION`). But the descriptor *slots* — the encoded memory-access patterns for the operands — do not land at arbitrary offsets. They land at one of **three family-fixed offset patterns** (A / B / C), chosen by descriptor width and operand count, with a control-byte band wedged into whichever 16-byte region the slots leave free. This page documents the universal header, the emission skeleton, the three slot families, and the single most-repeated error in earlier field maps — that there is a descriptor at `+0x48`. There is not.

This is the skeleton the per-engine encoding pages (2.10 PE matmul, 2.11 Pool, … 2.22 DMA) fill in. After this page a reimplementer can lay out an empty 64-byte bundle and place the header for any opcode in the [master opcode table](../walrus/opcode-master.md).

| | |
|---|---|
| **Bundle size** | exactly 64 bytes (`std::array<u8,64>`); `fwrite(…, 0x40, …)` — CONFIRMED |
| **Header word** | `+0x00..+0x03 = {opcode, 0x10, 0x00, 0x00}` little-endian `*(u16*)bundle = 0x10NN` — CONFIRMED |
| **`inst_word_len`** | `byte[1] = 0x10` = 16 dwords = 64 B, hardcoded, no non-16 length exists — CONFIRMED |
| **Header writer** | `CoreV2/V3/V4GenImpl::setupHeader` @ `0x1172120` / `0x1369280` / `0x143f440` — byte-identical — CONFIRMED |
| **Reached via** | virtual call, vtable slot 9 = `call [rax+0x48]` (a *code* offset, not a bundle byte) — CONFIRMED |
| **Slot families** | A `0x10/0x30`, B `0x10/0x20/0x30`, C `0x0C/0x2C` — CONFIRMED |
| **Slot sizes** | TENSOR1D=8 · 2D=12 · 3D=16 · 4D=20 bytes (`.rodata` asserts) — CONFIRMED |
| **No `+0x48` descriptor** | a 16-B slot at `+0x48` ends at `0x58` > `0x40` — would overflow the bundle — CONFIRMED |

---

## The universal header word

`setupHeader` is the only function every compute encoder calls unconditionally, and it writes the only bytes that mean the same thing for every op. Disassembled directly from `libwalrus.so` (md5 `1d93972b…`), all three generations are the same six instructions; only the entry RVA differs.

```asm
; CoreV2GenImpl::setupHeader(void* this, void* bundle, void* opcodeByte)   @ 0x1172120
; System V: rdi=this, rsi=&bundle (64-B array), rdx=&opcodeByte
 1172120:  0f b6 02        movzx  eax, BYTE PTR [rdx]      ; al = *opcodeByte
 1172123:  c6 46 01 10     mov    BYTE PTR [rsi+0x1], 0x10 ; bundle[1] = inst_word_len = 16
 1172127:  88 06           mov    BYTE PTR [rsi],     al   ; bundle[0] = opcode byte
 1172129:  31 c0           xor    eax, eax
 117212b:  66 89 46 02     mov    WORD PTR [rsi+0x2], ax   ; bundle[2:4] = 0x0000
 117212f:  c3              ret
; CoreV3 @ 0x1369280 and CoreV4 @ 0x143f440 are byte-identical (re-disassembled this pass).
```

So the 4-byte header word is:

| Off | Field | Value | Set by | Notes |
|-----|-------|-------|--------|-------|
| `+0x00` | `opcode` byte | per-op L3 ISA opcode | `setupHeader` | the wire opcode, **not** the `bir::InstructionType` ordinal |
| `+0x01` | `inst_word_len` | `0x10` = 16 dwords = 64 B | `setupHeader` | hardcoded immediate, zero data dependence |
| `+0x02` | format / reserved | `0x0000` (word) | `setupHeader` | always zero on the wire |

Read as a little-endian 16-bit word, `*(u16*)bundle = 0x10NN` where `NN` is the opcode byte and the high byte `0x10` is the length. This is why the MX opcodes show up in earlier reports as `0x1009 / 0x100A / 0x10E3` — those are the full *word*, and the opcode proper is the low byte (`0x09 / 0x0A / 0xE3`); the high `0x10` is `inst_word_len`, not part of the opcode. The full per-`InstructionType` × generation opcode table is the [opcode-word authority](../walrus/opcode-master.md).

> **QUIRK —** `inst_word_len` is named as if a bundle could be a different number of dwords. It cannot. `byte[1]` is the literal immediate `0x10` in all three bodies with no branch, no operand, no field read. Every CoreV\* bundle is exactly 64 bytes — one `emplace_back<std::array<u8,64>>` and one `fwrite(…, 0x40, …)`. There are no non-16 lengths in this ISA.

> **GOTCHA —** the high byte of the header word is **not** a constant you can ignore. Exactly one op perturbs it: `QuantizeMx` (gen4, opcode `0xE3`) OR-s bit 0 of `byte[1]` (`0x10 → 0x11`) when its SATURATE flag is set, writing the word directly rather than via `setupHeader`. That is the only op that touches `byte[1]` after stamping. Every other op leaves it `0x10`.

The MX/Quantize ops bypass `setupHeader` and write the word in one store (`mov esi, 0x1009; mov WORD PTR [bundle], si`), but the resulting wire bytes are byte-identical to what `setupHeader` would have produced. There is no second header format.

---

## The emission skeleton

Every GENERATE-mode encoder shares one prologue. Witnessed at `CoreV2GenImpl::generateMatMul` (`0x1248650`); the structure is identical in `visitInstTensorTensor` / `visitInstActivation` / `generateTensorScalarOrPtr` / `visitInstPool` / `visitInstTensorCopy`.

```asm
; --- the common bundle prologue (generateMatMul, 0x12487a2..0x12487f1) ---
 12487a2:  call  SmallVectorImpl<array<u8,64>>::emplace_back()  ; rax = &bundle  (kept in r15)
 12487a7:  pxor  xmm0, xmm0
 12487d7:  movups XMMWORD PTR [rax+0x00], xmm0
 12487da:  movups XMMWORD PTR [rax+0x10], xmm0
 12487de:  movups XMMWORD PTR [rax+0x20], xmm0
 12487e2:  movups XMMWORD PTR [rax+0x30], xmm0   ; <- FULL 64-byte zero-fill (4 x 16 B)
 12487e6:  mov   rax, QWORD PTR [r12]            ; rax = vptr (the Generator vtable)
 12487ea:  mov   BYTE PTR [rbp-0x472], 0x2       ; opcodeByte = 2 (matmul) in a stack local
 12487f1:  call  QWORD PTR [rax+0x48]            ; setupHeader  <- vtable slot 9 (vtbl+0x48)
; ... then op-specific field writes (dtype / ALU / perf / accumulate / descriptor slots) ...
; ... then fwrite(bundle, 1, 0x40, findBin(I)) + census bookkeeping ...
```

The annotated C, naming the real symbols:

```c
/* The shared GENERATE-mode bundle emission skeleton (every visitInst*/generate*).
 * Mirrors generateMatMul @ 0x1248650 byte-for-byte in structure. */
void emit_bundle(CoreVxGenImpl *self, bir::Instruction *I, uint8_t opcode)
{
    /* 1. allocate one 64-byte slot in the engine's bundle stream (this+120) */
    std::array<uint8_t,64> *b = self->bundleVec.emplace_back();

    /* 2. ZERO ALL 64 BYTES.  Every byte the op does not explicitly write reads 0
     *    on the wire (header reserved word, unused control bytes, slot spare bytes). */
    memset(b, 0, 64);                 /* 4x movups xmm0 in the binary */

    /* 3. the ONLY universal write: stamp the 4-byte header via the vtable.
     *    opcode lives in a stack local; setupHeader takes it BY REFERENCE (rdx). */
    uint8_t opByte = opcode;
    self->vptr[9](self, b, &opByte);  /* call [vptr+0x48] -> setupHeader        */
                                      /* writes b[0]=opByte, b[1]=0x10, b[2:4]=0 */

    /* 4. op-specific body: control bytes + descriptor slots (the three families) */
    fill_op_specific_fields(self, I, b);

    /* 5. serialize.  Always 0x40 bytes. */
    fwrite(b, 1, 0x40, findBin(I));   /* one bundle, fixed length */
}
```

Two facts a reimplementer must internalize:

- **Zero-first.** The 64 bytes are zeroed before anything is written. Every byte the op leaves untouched is a hard zero on the wire — the header's `+0x02/+0x03` reserved word, any unused control byte, any descriptor spare byte. You do not need to know what those bytes "mean"; you need to write zero.
- **The opcode is a stack local passed by pointer.** `setupHeader` reads `*rdx`, so the encoder stages the opcode byte in a frame slot and passes its address. The opcode value itself comes from the per-op table, not from the BIR `InstructionType` ordinal.

> **CORRECTION (N11) —** the recurring `+0x48` in earlier per-op field maps is **not a bundle byte**. It is the *vtable offset* through which `setupHeader` is reached: `call QWORD PTR [rax+0x48]` (slot 9, since `0x48/8 = 9`). It is a code address. No descriptor lives at bundle byte `+0x48`: the narrowest descriptor that could sit there is a TENSOR3D (16 B), which would span `+0x48..+0x57` and overflow the 64-byte bundle (`0x58 > 0x40`). The "`+0x48`" and the on-the-wire `+0x48` are unrelated.

### Codegen modes

The encoder runs in one of three modes (read from `this+0x270`):

| Mode | Name | Behaviour | Wire bundle? |
|------|------|-----------|--------------|
| 0 | `COLLECT_OPCODES` | inserts the opcode **int** into a per-inst `set<u32>`; builds a small scratch struct at low offsets | no |
| 1 | `GENERATE_ISACODE` | the skeleton above + `fwrite` | **yes** |
| 2 | `RUN_ISA_CHECKS` | builds the bundle, runs `runSingleISACheck`, no `fwrite` | no (transient) |

> **GOTCHA —** in modes 0 and 2 the encoder writes its scratch through `rbp`-relative `lea`s (e.g. `lea esi,[rbp+0xc]`), not through the bundle base register. Disassembling a `visitInst*` body, you will see *both* an `rbp`-relative descriptor write (the check scratch) and an `[r12/r13/r14/r15/rbx + OFF]` write (the live wire slot). Only the latter is the 64-byte bundle. Reading the `rbp` form as the wire layout is the cause of several earlier mis-pinned offsets.

Engine binding is **not** in the 64-byte bundle. The physical engine rides the BIR `Instruction` at `+0x90` (`EngineType`), read only to key a per-engine census `DenseMap`. The wire bundle carries no engine field. (Engine ordinals: Pool=1, Act=2, PE=3, DMA=4, DVE=5, SP=6 — see [arch object model](../arch/arch-object-model.md).)

---

## The byte-offset map

The 60 bytes after the header word are an op-specific union. The table below is the *union* of the byte-verified attributions — it shows where each region lands and which family/op writes it. **Bytes not listed are zero** (the memset). Offsets that fork by family are tagged.

| Off | Field (common interpretation) | Set by | Users / notes | Conf |
|-----|-------------------------------|--------|---------------|------|
| `+0x00` | `opcode` byte (L3 ISA op) | `setupHeader` | ALL ops | CONFIRMED |
| `+0x01` | `inst_word_len` = `0x10` | `setupHeader` | ALL ops (hardcoded) | CONFIRMED |
| `+0x02..03` | format/reserved = `0x0000` | `setupHeader` | ALL ops | CONFIRMED |
| `+0x04` | sync WAIT mode | `setupSyncWait` | SyncInfo ops (DVE/Pool/Act/SP/DMA); absent on matmul | STRONG |
| `+0x05` | sync `wait_idx` | `setupSyncWait` | — | STRONG |
| `+0x06` | sync UPDATE mode | `setupSyncUpdate` | — | STRONG |
| `+0x07` | sync `update_idx` | `setupSyncUpdate` | — | STRONG |
| `+0x08..0B` | sync wait/update value (dword) | `setupSync*` | shared `+8` dword | STRONG |
| `+0x0C` | **(Family B)** in0\|in1 dtype nibbles · **(Fam A TS)** accum byte | `visitInst` | TensorTensor control band starts here | CONFIRMED |
| `+0x0C` | **(Family C)** IN TENSOR4D slot START (20 B → `+0x0C..+0x1F`) | `assignAccess<4D>` | Copy/Pool/Reduce/BN in | CONFIRMED |
| `+0x0D` | **(Family B)** out dtype | `visitInst` | TensorTensor | CONFIRMED |
| `+0x0E` | **(Family B)** AluOp wire byte | `visitInst` | TensorTensor | CONFIRMED |
| `+0x0F` | **(Family B)** `num_active_partitions` | `visitInst` | TensorTensor | CONFIRMED |
| `+0x10` | **(Family A)** SRC/MOVING TENSOR3D slot (16 B → `+0x10..+0x1F`) | `assignAccess<3D>` | matmul ifmap, activation src, TS input | CONFIRMED |
| `+0x10` | **(Family B)** DST TENSOR3D slot | `assignAccess<3D>` | TensorTensor out | CONFIRMED |
| `+0x20` | INPUT dtype (wire tag) · **control-band base** (Fam A/C) | `visitInst` | A/C; **(Fam B)** in0 slot lives here | CONFIRMED |
| `+0x21` | OUTPUT dtype · matmul perf-sel (0/2/3) | `visitInst` | A/C | CONFIRMED |
| `+0x22` | scalar base/partition · misc | `visitInst` | TensorScalar, Activation | CONFIRMED |
| `+0x23` | perf-opt / format / `act_tbl_sel` · **DENSE matmul DST DTYPE** (op `0x02`, CoreV2/V3) | `visitInst` | dense matmul PSUM-dst dtype, LoadActFuncSet | CONFIRMED |
| `+0x24` | op0 ALU wire · ROW group (matmul) · Pool func | `visitInst` | TS op0; Pool {Max=1, Avg=2}; PE row | CONFIRMED |
| `+0x25` | op1 ALU wire · COL group (matmul) · Pool mode=3 | `visitInst` | TS op1; PE col | CONFIRMED |
| `+0x26` | reverse-combo · `wtbase`/`num_rows` (matmul) | `visitInst` | TS reverse; PE | STRONG |
| `+0x27` | `num_active_cols` (matmul) · scalar AP | `visitInst` | PE ncols | STRONG |
| `+0x28` | **MX matmul DST DTYPE** (op `0x100A`, CoreV4) · fill imm dword (Memset) | `visitInst` | MX PSUM-dst dtype; Memset fill | CONFIRMED |
| `+0x2B` | **ACCUMULATE** byte {b0 START / b1 STOP / b2 ACCUMULATE} | `visitInst` (set upstream) | matmul/MX PSUM ops | CONFIRMED |
| `+0x2C` | **(Family C)** OUT TENSOR4D slot START (20 B → `+0x2C..+0x3F`) | `assignAccess<4D>` | Copy/Pool/Reduce/BN out; Memset (1D); PE grp | CONFIRMED |
| `+0x2F` | `psum_zero_region` (matmul/MX) | `visitInst` | PE | CONFIRMED |
| `+0x30` | **(Family A)** DST · **(Family B)** in1 TENSOR3D slot START (16 B → `+0x30..+0x3F`) | `assignAccess<3D/2D>` | matmul PSUM-dst, TS out, TT in1, BNStats 2D-out | CONFIRMED |
| `+0x3F` | last bundle byte | — | end of 64-B / 16-dword bundle | CONFIRMED |

> **NOTE —** `+0x20..+0x2F` is the canonical **control band** for Families A and C (it sits *between* their two descriptor slots). For Family B the three 16-B slots fill `+0x10..+0x3F` completely, leaving no room after the slots — so Family B's control band is squeezed into `+0x0C..+0x0F` *before* the slots. There is no single fixed location for the control band; it is family-dependent.

---

## The three descriptor-slot families

Each operand role gets a descriptor *template* (TENSOR1D/2D/3D/4D or a MEM_PATTERN sibling) placed at a **family-fixed** bundle offset by an explicit `lea rsi, [base + OFF]` immediately before `call assignAccess<…>`. The base register is the `emplace_back`'d array pointer (`r12`/`r13`/`r14`/`r15`/`rbx`, frame-dependent). Slot sizes are pinned by `.rodata` asserts:

```text
.rodata 0x1d6e810  "ISA mem pattern 1D must have 8 bytes to encode"   -> assignAccess<TENSOR1D>
.rodata 0x1d6e8b0  "ISA mem pattern 2D must have 12 bytes to encode"  -> assignAccess<TENSOR2D>
.rodata 0x1d6e920  "ISA mem pattern 3D must have 16 bytes to encode"  -> assignAccess<TENSOR3D>
.rodata 0x1d6e990  "ISA mem pattern 4D must have 20 bytes to encode"  -> assignAccess<TENSOR4D>
```

The geometry that decides the family is: **descriptor width** (3D=16 B vs 4D=20 B) and **operand count** (2 slots vs 3). The control band always wants the `+0x20..+0x2F` 16-byte region; the slots are positioned to straddle it without colliding.

### Family A — "3D 2-slot" (src `+0x10`, dst `+0x30`)

Two TENSOR3D slots with the control band *between* them at `+0x20..+0x2F`. The canonical witness is `generateMatMul`:

```asm
; CoreV2GenImpl::generateMatMul  @ 0x1248650   (r15 = &bundle)
 1248c35:  lea rsi, [r15+0x10]   ; call assignAccess<TENSOR3D>   <- MOVING/ifmap (SBUF)
 1248c3e:  lea rsi, [r15+0x30]   ; call assignAccess<TENSOR3D>   <- PSUM destination
;  control band +0x20 ifmap-dtype / +0x21 perf-sel / +0x23 dense-dst-dtype /
;  +0x24 row-grp / +0x25 col-grp / +0x28 MX-dst-dtype / +0x2B accumulate / +0x2F zero-region
;  (dst-dtype is per-generation: dense op 0x02 -> +0x23; MX op 0x100A -> +0x28; see 2.10)
```

`src @+0x10` ends at `0x20`; `dst @+0x30` ends at `0x40`. The `+0x20..+0x2F` gap is exactly one missing 16-B slot, filled by the control band.

**Users:** Matmul (ifmap `+0x10`, PSUM-dst `+0x30`), Activation, TensorScalar, TensorScalarCache, the DVE scalar ops.

> **CORRECTION (N11/C-1 to J04) —** an earlier TensorScalar map placed its TENSOR3D descriptors at `+0x28..+0x3F`. The real slot offsets are `+0x10` (input) and `+0x30` (output) — byte-verified at `generateTensorScalarOrPtr` `0x1265c8a` (`lea [r14+0x10]`) and `0x1265c9e` (`lea [r14+0x30]`). TensorScalar is **Family A**, identical to matmul and activation — *not* a `+0x28` packing. The `+0x20..+0x27` region J04 cited is the control band (dtype/ALU/scalar), which overlaps the *start* of the `+0x30` output slot's region only in the reader's mind, not on the wire.

### Family B — "3D 3-slot" (dst `+0x10`, in0 `+0x20`, in1 `+0x30`)

Three TENSOR3D slots, **contiguous**, filling `+0x10..+0x3F` exactly (3 × 16 = 48 = `0x30`, base `+0x10` → ends at `0x40`). The witness is `visitInstTensorTensor`:

```asm
; CoreV2GenImpl::visitInstTensorTensor  @ 0x12356d0   (r15 = &bundle)
 1235aac:  lea rsi, [r15+0x10]   ; call assignAccess<TENSOR3D>   <- dst (out)
 1235ac9:  lea rsi, [r15+0x20]   ; call assignAccess<TENSOR3D>   <- in0 (tensor0/arg0)
 1235ade:  lea rsi, [r15+0x30]   ; call assignAccess<TENSOR3D>   <- in1 (tensor1/arg1)
;  control band BEFORE the slots: +0x0C in0|in1 dtype / +0x0D out dtype /
;  +0x0E AluOp wire / +0x0F num_active_partitions
```

Because the three slots leave no room after them, the control band is at `+0x0C..+0x0F`.

**Users:** TensorTensor (2-input elementwise), ScalarTensorTensor (2D variant, shifted packing), the 3-tensor-operand DVE class.

> **QUIRK —** Family B *is* the layout that earlier reports described as "slots at `+16/+32/+48`" (= `0x10/0x20/0x30`). That premise is correct — **but only for Family B**. It is not the universal layout. Family A has only two slots (`0x10`/`0x30`) with the control band at `0x20` where the "third slot" would be. The `+0x48` in that phrasing is a typo for `+0x30`'s end and/or a confusion with the `vtbl+0x48` code offset; there is no third descriptor and no `+0x48` descriptor.

### Family C — "4D 2-slot" (in `+0x0C`, out `+0x2C`)

Two TENSOR4D slots (20 B each) with the control band between at `+0x20..+0x2B`. Witnesses:

```asm
; CoreV2GenImpl::visitInstTensorCopy  @ 0x1237f50   (rbx = &bundle)
 1238177:  lea rsi, [rbx+0xc]    ; call assignAccess<TENSOR4D>   <- src  (+0x0C..+0x1F)
 123818f:  lea rsi, [rbx+0x2c]   ; call assignAccess<TENSOR4D>   <- dst  (+0x2C..+0x3F)
;  control +0x20 src-dtype / +0x21 dst-dtype

; CoreV2GenImpl::visitInstMemset   @ 0x125b320   (r12 = &bundle)
 125c595:  lea rsi, [r12+0x2c]   ; call assignAccess<TENSOR1D>   <- out (1-D pattern @+0x2C)
```

`in @+0x0C` ends at `0x20`; `out @+0x2C` ends at `0x40`. Again the slots straddle the `+0x20`-region control band.

**Users:** TensorCopy, Pool, TensorReduce, Reciprocal, Iota, Memset (out `+0x2C`), StreamShuffle/Transpose, Gather, BNStats, the BN free-dim ops.

> **NOTE — mixed-width slots.** The two anchor positions (low `+0x0C`/`+0x10`, high `+0x2C`/`+0x30`) are *position slots* that accept a variable-width descriptor sized per operand. `visitInstBNStats` (`0x123ab00`) proves it: its IN anchor is `+0x0C` (TENSOR4D, 20 B) but its OUT lands at `+0x30` (`lea [r13+0x30]`, `assignAccess<TENSOR2D>`, 12 B) when the output is a narrow 2-D stat pattern. The anchor is fixed; the width is not.

> **STRONG —** the structural rule behind all three families: the control band occupies a 16-byte region (canonically `+0x20..+0x2F`). The LOW slot sits below it (`+0x10` for a 16-B 3D slot ending at `+0x20`; `+0x0C` for a 20-B 4D slot ending at `+0x20`) and the HIGH slot sits above it (`+0x30` for 3D ending `+0x40`; `+0x2C` for 4D ending `+0x40`). The band width equals exactly one missing descriptor, which is why the low-slot end and high-slot start straddle it. This is inferred from four byte-verified families that all agree, not asserted from a spec.

### What rides at slot byte +0

Every descriptor slot begins with a 32-bit **ADDR4** word at slot offset 0 (see [ADDR4](addr4.md)): a 29-bit byte address with the SBUF/PSUM discriminator and the indirect/active/register-mode flags in the high bits. After the ADDR4 come two contiguous arrays — `N` signed i16 strides then `N` u16 num words (the "4+4N" rule), with unused dims `{stride=1, num=1}`-filled. SRC operands use the `TENSOR*` template; DST operands use a `MEM_PATTERN*` sibling that is a byte-identical packer differing only in wire-type name and validator role. The descriptor layout itself is [tensor descriptors](tensor-descriptors.md).

> **NOTE —** DMA `DIRECT2D` (opcode `0xD4`, engine DMA=4) is a fourth, wholly distinct body — it reuses `+0x00..+0x0B` (header + sync) but carries 8-byte ADDR8 addresses (src `+0x10`, dst `+0x28`) and its own 2-D step/num body, not mem-pattern slots. It is not one of the A/B/C families; treat it as its own case on the DMA encoding page.

---

## Shared-vs-op-specific split

The definitive partition for a reimplementer:

- **Truly common (every CoreV\* compute op):** the 4-byte header (`setupHeader`, byte-identical); the `emplace + memset-64 + vcall(vtbl+0x48) + fwrite-0x40` skeleton; the 4+4N descriptor geometry of whatever slots the op uses; the wire-byte helper functions (`Dtype→byte`, `AluOp→byte`, `accum→byte`).
- **Common-when-present (ops carrying SyncInfo — DVE/Pool/Act/SP/DMA; absent on matmul):** `+0x04` wait mode, `+0x05` wait_idx, `+0x06` update mode, `+0x07` update_idx, `+0x08..0x0B` sync value (written by `setupSyncWait`/`setupSyncUpdate`). See [execution & sync model](../arch/execution-sync-model.md).
- **Family-fixed (chosen by width + operand count, not free):** the descriptor slot offsets (A=`0x10`/`0x30`, B=`0x10`/`0x20`/`0x30`, C=`0x0C`/`0x2C`) and the control-band position (A/C = `+0x20..+0x2F`, B = `+0x0C..+0x0F`).
- **Op-specific (each `visitInst`):** the opcode value, the control-byte *meanings*, which slots exist, which descriptor *type* each slot is, and multi-bundle emission (matmul 2-pass, MaxIndex `0x6D`+`0x6E`, Gather, CustomOp).

---

## Confidence ledger

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `setupHeader` byte-identical across V2/V3/V4; writes only `{opcode,0x10,0,0}` | CONFIRMED | objdump of `0x1172120` / `0x1369280` / `0x143f440` — 6 identical instrs |
| `inst_word_len` always `0x10`; no non-16 lengths | CONFIRMED | hardcoded immediate, zero data dependence in all 3 bodies |
| emit skeleton: emplace + 4× movups zero-fill + `call [rax+0x48]` | CONFIRMED | `generateMatMul` `0x12487a2..0x12487f1` |
| Family A slots `0x10`/`0x30` | CONFIRMED | `generateMatMul` `lea [r15+0x10]`/`[r15+0x30]` |
| Family B slots `0x10`/`0x20`/`0x30` contiguous | CONFIRMED | `visitInstTensorTensor` `lea [r15+0x10/0x20/0x30]` |
| Family C slots `0x0C`/`0x2C` | CONFIRMED | `visitInstTensorCopy` `lea [rbx+0xc]`/`[rbx+0x2c]` |
| Mixed-width (BNStats 4D-in `+0x0C` / 2D-out `+0x30`) | CONFIRMED | `visitInstBNStats` `lea [r13+0x30]` + `assignAccess<TENSOR2D>` |
| Slot sizes 8/12/16/20 B | CONFIRMED | `.rodata` `0x1d6e810/8b0/920/990` assert strings |
| No descriptor at bundle byte `+0x48` | CONFIRMED | `+0x48` = vtable slot; `0x48+16 = 0x58 > 0x40` overflows |
| TensorScalar = Family A (corrects J04 `+0x28`) | CONFIRMED | `generateTensorScalarOrPtr` `lea [r14+0x10]`/`[r14+0x30]` |
| Sync header `+0x04..+0x0B` common-when-present | STRONG | per-op `setupSyncWait`/`Update` calls, not one universal write |
| Low/high anchor straddling rationale | STRONG | structural inference from 4 agreeing byte-verified families |
| Exhaustive family membership of all ~68 CoreV2 encoders | INFERRED | 10 disassembled directly; the rest assigned by descriptor-width evidence |

---

## Cross-References

- [ADDR4 — the 32-bit slot address word](addr4.md) — what rides at byte 0 of every descriptor slot (2.2).
- [Tensor descriptors — the 4+4N mem-pattern layout](tensor-descriptors.md) — the strides/num words that fill each slot (2.3).
- [PE Engine — the systolic matmul array](../arch/pe-engine.md) — Family A's canonical user; the accumulate `+0x2B` and zero-region `+0x2F` semantics.
- [Execution & sync model](../arch/execution-sync-model.md) — the `+0x04..+0x0B` sync header band (1.14).
- [Arch object model](../arch/arch-object-model.md) — the `EngineType` ordinal map (Pool=1 … SP=6) that the census reads from `Inst+0x90`.
- [setupHeader & the opcode-word table](../walrus/opcode-master.md) — the per-`InstructionType` × generation L3 opcode authority (Part 8).
- [Per-engine `.bin` members](../formats/per-engine-bin.md) — where the emitted 64-byte bundle streams land in the NEFF container (12.4).
