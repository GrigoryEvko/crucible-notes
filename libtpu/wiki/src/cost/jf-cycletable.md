# JfCycleTable

> *Every offset, value, mask, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. All `.rodata` addresses are virtual addresses; for this binary `.rodata` VMA == file offset (section `[11]` at `0x84a0000`), and `.text` VMA == file offset. The four LUTs below were dumped by `struct.unpack` against the raw `.rodata` bytes and the `lea` displacements re-resolved arithmetically against the disassembly.*

## Abstract

`xla::jellyfish::JfCycleTable` is the **throughput half** of the cost model for the two oldest TensorCore generations — Jellyfish (`TpuVersion 0`, v2) and Dragonfish (`TpuVersion 1`, v3). It is the only `CycleTable` subclass that reads its cycle numbers through a *flat byte-offset LUT* rather than a `switch` over `Performance::GetResourceUsage`; from Pufferfish onward the family changes shape (see [CycleTable Family](cycletable-family.md)). This page is the byte-level transcription of the JF/DF read path: the four-instruction `GetCyclesForThroughput` body, the 33-bit priced mask `0x19FFC0821`, the full 33-entry `int64` `offsetLUT` (`@0xb438b70`), the full 33-entry `int32` `resLUT` (`@0xb438aec`), and the two MXU-modifier classifier LUTs (`latchLUT @0xb4389f4`, `fmtLUT @0xb438ac4`) that produce the outer index.

The single design fact to carry away: a `JfCycleTable` holds **no cycle numbers**. It holds a `Performance*` at `+0x10` and a pair of `.rodata` LUTs that turn a *cycle class* (`CycleTable::Instruction`, a dense `0x00..0x20` enum, **not** an LLO opcode) into (a) a byte offset into that `Performance` grid and (b) a `ResourceVector` slot index. The grid itself — the `0xe00`-byte `PerformanceJf`/`PerformanceDf` POD and its constructor source blocks — is documented in full on [Performance: JF/DF](performance-jf-df.md); this page covers only the *index logic* over it and transcribes the four lookup tables verbatim.

The contract a reimplementer must honor:

- **The throughput read is `valid ? Performance[offsetLUT[cls]] : 1`.** `valid = (cls < 0x21) && ((0x19FFC0821 >> cls) & 1)`. Both the bound and the mask are required — see the GOTCHA below.
- **Only 16 of the 33 classes are priced.** The other 17 short-circuit to the default `1` cycle. The `offsetLUT` slot of every unpriced class is literally `0x0`.
- **`GetResource(cls)` is a separate flat LUT (`resLUT`) returning `0..6`.** Those are direct slot indices into the 23-slot `ResourceVector`; the scheduler does `ResourceVector[resLUT[cls]] += (double)cycles`.
- **The seven priced MXU ports cost 8 cycles; the nine priced vector/EUP stages cost 1.** None of the priced offsets is `+0x28`/`+0x2c`, so the JF→DF two-cell delta never reaches the throughput LUT — the throughput table is byte-identical JF and DF.
- **Transcendentals are priced by scalar virtual overrides** (`EstimateSinCosCost = 198`, `EstimateTanCost = 219`), not by the cycle-class LUT.

| | |
|---|---|
| **Class** | `xla::jellyfish::JfCycleTable` — serves `TpuVersion` 0 (Jellyfish v2) and 1 (Dragonfish v3) |
| **`Performance*`** | held at `JfCycleTable+0x10` (read by `GetCyclesForThroughput`) |
| **Throughput reader** | `JfCycleTable::GetCyclesForThroughput(Instruction)` `@0x1c89dce0` (vtable slot `+0x10`) |
| **Throughput formula** | `valid = (cls < 0x21) && ((0x19FFC0821 >> cls) & 1)`; `valid ? Performance[offsetLUT[cls]] : 1` |
| **Priced mask** | `0x19FFC0821` — 16 of 33 classes priced; rest default `1` |
| **offsetLUT** | `qword_B438B70` `@0xb438b70` — 33 × `int64`, class → `Performance` byte offset |
| **Resource reader** | `CycleTable::GetResource(Instruction)` `@0x1c89ce20` (gen-invariant) |
| **resLUT** | `dword_B438AEC` `@0xb438aec` — 33 × `int32`, class → `ResourceVector` slot `0..6` |
| **MXU classifier** | `CycleTableInstruction(LloInstruction*)` `@0x1c89ca80` (MXU band only) |
| **latchLUT / fmtLUT** | `unk_B4389F4` `@0xb4389f4` (52 × `int32`) / `unk_B438AC4` `@0xb438ac4` (11 × `int32`) |
| **Transcendentals** | `EstimateSinCosCost` `@0x1c89dd20` = 198; `EstimateTanCost` `@0x1c89dd40` = 219 |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Throughput Read Path — `GetCyclesForThroughput`

`JfCycleTable::GetCyclesForThroughput` is a four-instruction function. It bounds the cycle-class ordinal at `< 0x21`, tests it against the 33-bit valid mask, and on a hit indexes the `Performance` grid (held at `JfCycleTable+0x10`) by a byte offset pulled from the 33-entry `int64` `offsetLUT`. On a miss it returns the default `1`. Verbatim from the decompile:

```c
// xla::jellyfish::JfCycleTable::GetCyclesForThroughput  @0x1c89dce0  (decompiled, exact)
__int64 GetCyclesForThroughput(JfCycleTable *this, unsigned int cls) {
    __int64 result = 1;
    if ( ((cls < 0x21) & (unsigned __int8)(0x19FFC0821uLL >> cls)) == 1 )
        return *(unsigned int *)( *(_QWORD *)(this + 0x10)     // Performance*
                                  + qword_B438B70[cls] );       // offsetLUT[cls]
    return result;                                              // default 1
}
```

The raw machine code pins every constant (`objdump -d -M intel`):

```text
1c89dce0:  89 f1                 mov    ecx,esi                          ; ecx = cls
1c89dce2:  83 fe 21              cmp    esi,0x21                         ; cls < 0x21 ?
1c89dce5:  0f 92 c0             (setb  al)                               ; al = (cls < 0x21)
1c89dce8:  48 ba 21 08 fc 9f 01  movabs rdx,0x19ffc0821                  ; the priced mask
1c89dcf2:  48 d3 ea              shr    rdx,cl                           ; rdx >>= cls
1c89dcf5:  20 c2                 and    dl,al                            ; dl = mask_bit & in_bound
1c89dcf7:  b8 01 00 00 00        mov    eax,0x1                          ; default = 1
1c89dcfc:  80 fa 01              cmp    dl,0x1                           ; valid ?
       (   75 ..                 jne    1c89dd15)                        ; -> ret 1
1c89dd01:  89 c8                 mov    eax,ecx                          ; eax = cls
1c89dd03:  48 8d 0d 66 ae b9 ee  lea    rcx,[rip+0xeeb9ae66]             ; = 0xb438b70 (offsetLUT)
1c89dd0a:  48 8b 04 c1           mov    rax,QWORD PTR [rcx+rax*8]        ; rax = offsetLUT[cls]
1c89dd0e:  48 8b 4f 10           mov    rcx,QWORD PTR [rdi+0x10]         ; rcx = Performance*
1c89dd12:  8b 04 01              mov    eax,DWORD PTR [rcx+rax*1]        ; eax = Performance[offset]
1c89dd15:  c3                    ret
```

The `lea` displacement is RIP-relative from the *next* instruction address (`0x1c89dd0a`): `0x1c89dd0a + 0xeeb9ae66 = 0x1_0b438b70`, and the 64-bit add carries out of bit 32, leaving the effective address `0xb438b70` — the `offsetLUT`. (`objdump` annotates the target VA directly.) The `Performance*` is at `[rdi+0x10]` = `JfCycleTable+0x10`, the cycle read is a 4-byte `int32` load.

> **GOTCHA — the bound and the mask are not redundant.** A reimplementation must apply *both* `cls < 0x21` *and* the 33-bit mask. The bound guards the 33-entry LUT against out-of-range reads; the mask selects the 16 priced classes inside the bound. Relying on the `offsetLUT` alone would read `Performance[0]` (the vtable pointer, not a cycle value) for every unpriced class, because the LUT slot of every unpriced class is literally `0x0`. The mask short-circuits before that read is ever reached.

### The priced mask `0x19FFC0821`

Reading the mask bit-for-bit (`(0x19FFC0821 >> cls) & 1`) partitions the 33-value enum into 16 priced and 17 unpriced classes:

```text
mask = 0x1_9FFC_0821  =  0b1 1001 1111 1111 1100 0000 1000 0010 0001
bit:     32                    ...                              0
```

| | classes | count |
|---|---|---|
| **Priced** (read `Performance[offsetLUT[cls]]`) | `0x00, 0x05, 0x0b, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1f, 0x20` | 16 |
| **Unpriced** (return default `1`) | `0x01, 0x02, 0x03, 0x04, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0c, 0x0d, 0x0e, 0x0f, 0x10, 0x11, 0x1d, 0x1e` | 17 |

---

## The offsetLUT — `qword_B438B70` (33 × `int64`, byte-exact)

The class→byte-offset table, transcribed directly from `.rodata @0xb438b70` as 33 little-endian `int64`. Each priced entry's resolved value is the cell read back from the reconstructed `PerformanceJf` in-memory image at that byte offset (every MXU-band cell is explicit in the [PerformanceJf constructor](performance-jf-df.md#the-rodata-constant-blocks); the 8-cycle cells come from `xmmword_A2DA220 = {8,8,8,1}` at `[+0x910]`, the `dword_84A2D0C = 8` broadcasts over `[+0x920..+0x980]`, and `xmmword_A2CF810 = {8,1,1,8}` at `[+0x940]`).

| Class | `offsetLUT[cls]` | priced | JF/DF cyc | Source idiom (where the cell is written) |
|------:|-----------------:|:------:|----------:|------------------------------------------|
| `0x00` | `0x910` | yes | **8** | block `xmmword_A2DA220 = {8,8,8,1}` `[0]` |
| `0x01` | `0x000` | no  | 1 | — (mask short-circuits) |
| `0x02` | `0x000` | no  | 1 | — |
| `0x03` | `0x000` | no  | 1 | — |
| `0x04` | `0x000` | no  | 1 | — |
| `0x05` | `0x92c` | yes | **8** | bcast `dword_84A2D0C = 8` (`[+0x920..+0x930]` run) |
| `0x06` | `0x000` | no  | 1 | — |
| `0x07` | `0x000` | no  | 1 | — |
| `0x08` | `0x000` | no  | 1 | — |
| `0x09` | `0x000` | no  | 1 | — |
| `0x0a` | `0x000` | no  | 1 | — |
| `0x0b` | `0x92c` | yes | **8** | bcast `8` (**shares offset `0x92c` with `0x05`**) |
| `0x0c` | `0x000` | no  | 1 | — |
| `0x0d` | `0x000` | no  | 1 | — |
| `0x0e` | `0x000` | no  | 1 | — |
| `0x0f` | `0x000` | no  | 1 | — |
| `0x10` | `0x000` | no  | 1 | — |
| `0x11` | `0x000` | no  | 1 | — |
| `0x12` | `0x33c` | yes | 1 | bcast `dword_84A2B08 = 1` (`[+0x334]` run) |
| `0x13` | `0x340` | yes | 1 | bcast `1` (`[+0x344]` run) |
| `0x14` | `0x344` | yes | 1 | bcast `1` |
| `0x15` | `0x39c` | yes | 1 | bcast `1` (`[+0x394]` run) |
| `0x16` | `0x398` | yes | 1 | bcast `1` |
| `0x17` | `0x954` | yes | **8** | bcast `8` (`[+0x950..+0x980]` run) |
| `0x18` | `0x3f8` | yes | 1 | imm `1` (`*((_DWORD*)this+254) = 1`) |
| `0x19` | `0x368` | yes | 1 | bcast `1` (`[+0x364]` run) |
| `0x1a` | `0x3f4` | yes | 1 | bcast `1` (`[+0x3e8]` run) |
| `0x1b` | `0x960` | yes | **8** | bcast `8` |
| `0x1c` | `0x94c` | yes | **8** | block `xmmword_A2CF810 = {8,1,1,8}` `[3]` |
| `0x1d` | `0x000` | no  | 1 | — |
| `0x1e` | `0x000` | no  | 1 | — |
| `0x1f` | `0x958` | yes | **8** | bcast `8` |
| `0x20` | `0x39c` | yes | 1 | bcast `1` (**shares offset `0x39c` with `0x15`**) |

The seven **8-cycle** cells are the MXU matprep/matmul/matrix-result throughput ports — classes `0x00, 0x05, 0x0b, 0x17, 0x1b, 0x1c, 0x1f`. The nine **1-cycle** priced cells are the vector-ALU / cross-lane / EUP result stages — classes `0x12, 0x13, 0x14, 0x15, 0x16, 0x18, 0x19, 0x1a, 0x20`.

> **QUIRK — distinct classes alias a shared cell.** The flat-cell model does not require a distinct `Performance` offset per class. `0x05`/`0x0b` both read `0x92c` (the transposed-bf16 latch and the plain latch land on the same throughput cell), and `0x15`/`0x20` both read `0x39c`. The eight distinct 8-cycle offsets collapse to six values (`0x910, 0x92c, 0x94c, 0x954, 0x958, 0x960`), and the throughput cell is decoupled from the `resLUT` resource column — two classes can share a cell yet block different lanes (`0x05`→`R[0]` vs `0x0b`→`R[0]` here happen to match, but `0x15`/`0x20` both→`R[5]`). A reimplementation that keys a cache on the offset alone will silently merge these classes.

---

## The resLUT — `CycleTable::GetResource` / `dword_B438AEC` (33 × `int32`, byte-exact)

`CycleTable::GetResource` is gen-invariant (shared by all six `CycleTable` subclasses, not a `JfCycleTable` override) and is a single flat lookup:

```c
// xla::jellyfish::CycleTable::GetResource  @0x1c89ce20  (decompiled, exact)
__int64 GetResource(CycleTable *this, int cls) {
    return dword_B438AEC[cls];      // resLUT @0xb438aec, 33 × int32, values 0..6
}
```

The displacement check matches: the `lea rcx,[rip+0xeeb9bcc3]` at `0x1c89ce22` resolves to `0x1c89ce29 + 0xeeb9bcc3 = 0x1_0b438aec` → effective `0xb438aec`. The full 33 entries, transcribed from `.rodata`:

```text
resLUT @0xb438aec (cls 0x00 .. 0x20):
  1 1 1 1 1  0 0 0 0 0  0 0 0 0 0  0 0  6  4 4 3 5 5  2  6 5 6  2 2 2 2 2  5
```

| Class band | `resLUT[cls]` | `ResourceVector` slot |
|------------|--------------:|-----------------------|
| `0x00`–`0x04` | `1` | `R[1]` Matmul |
| `0x05`–`0x10` | `0` | `R[0]` Matpush |
| `0x11` | `6` | `R[6]` VectorEup |
| `0x12`, `0x13` | `4` | `R[4]` VectorAlu1 |
| `0x14` | `3` | `R[3]` VectorAlu0 |
| `0x15`, `0x16` | `5` | `R[5]` VectorAluAny |
| `0x17` | `2` | `R[2]` Xlu |
| `0x18` | `6` | `R[6]` VectorEup |
| `0x19` | `5` | `R[5]` VectorAluAny |
| `0x1a` | `6` | `R[6]` VectorEup |
| `0x1b`–`0x1f` | `2` | `R[2]` Xlu |
| `0x20` | `5` | `R[5]` VectorAluAny |

### Why these values are `ResourceVector` slot indices

`GetResource` returns a raw integer, but the only consumer treats it directly as a slot index into the 23-slot `ResourceVector`. The cost lambda `AccumulateInstructionUsage` `@0x144fd720` is the binding proof:

```c
// xla::jellyfish::sdc_checker::...::AccumulateInstructionUsage::operator()  @0x144fd720  (decompiled)
__int64 operator()(CycleTable &ct, ResourceVector &rv, unsigned int cls) {
    unsigned int Resource = CycleTable::GetResource(&ct, cls);     // resLUT[cls]
    __int64 cyc = (*(vtable + 0x10))(&ct, cls);                    // GetCyclesForThroughput(cls)
    ResourceVector::Acc(&rv, Resource, (double)cyc);               // rv[Resource] += cyc
    return 1;
}
```

and `ResourceVector::Acc` `@0x1c89adc0` is a bounds-checked `+=` keyed on that integer:

```c
// xla::jellyfish::ResourceVector::Acc  @0x1c89adc0  (decompiled, exact)
__int64 Acc(ResourceVector *this, unsigned int resource, double cycles) {
    if ( resource >= 0x17 ) __ud1();        // hard bound 0x17 = 23 slots
    this[resource] += cycles;               // vaddsd [rdi + resource*8]
    return resource;
}
```

The `cmp esi, 0x17 / jae ud1` bound fixes the vector at **23** `double` slots; the JF/DF `resLUT` emits only the values `0..6`, i.e. the MXU/vector **head** of the 23-slot accumulator. The remaining slots `R[7..22]` (vector load/store, the four memory-transfer terms, six ICI links, three SparseCore engines) are deposited into by other cost paths, never by this flat LUT — see the [Resource Enum](resource-enum.md) for the whole vector and the `MaxResourceCycles` overlap reduction.

---

## The 7 JF Resource Columns — Named

The seven distinct `resLUT` values are not a private cost enum; they are the first seven `ResourceVector::Resource` ordinals. The names come straight from the `printf` template inside `ResourceVectorToString` `@0x1c89bde0`, which formats the 22 leading slots in physical-offset order, so the string order *is* the enum order. The template head (read byte-for-byte):

```text
RV[Matpush: %.0f, Matmul: %.0f, Xlu: %.0f, VectorAlu0: %.0f, VectorAlu1: %.0f,
   VectorAluAny: %.0f, VectorEup: %.0f, VectorLoad: %.0f, VectorStore: %.0f, ...]
```

The first seven `%.0f` slots map one-for-one onto the `resLUT` values `0..6`:

| Res | `ResourceVector` slot | Name | JF/DF occupant classes | Throughput |
|----:|-----------------------|------|------------------------|-----------:|
| `0` | `R[0]` `+0x00` | `Matpush` | `0x05`–`0x10` (latch / push-gains band) | `0x05`,`0x0b` = 8; rest dflt 1 |
| `1` | `R[1]` `+0x08` | `Matmul` | `0x00`–`0x04` (matprep band) | `0x00` = 8; rest dflt 1 |
| `2` | `R[2]` `+0x10` | `Xlu` | `0x17`, `0x1b`–`0x1f` (matrix-result / cross-lane) | `0x17`,`0x1b`,`0x1c`,`0x1f` = 8 |
| `3` | `R[3]` `+0x18` | `VectorAlu0` | `0x14` | 1 |
| `4` | `R[4]` `+0x20` | `VectorAlu1` | `0x12`, `0x13` | 1 |
| `5` | `R[5]` `+0x28` | `VectorAluAny` | `0x15`, `0x16`, `0x19`, `0x20` | 1 |
| `6` | `R[6]` `+0x30` | `VectorEup` | `0x11`, `0x18`, `0x1a` | `0x18`,`0x1a` = 1; `0x11` dflt 1 |

This is how the JF/DF cost model expresses **resource conflict**: two classes mapped to the same column add (they serialize on that functional-unit lane); two classes in different columns overlap (the bundle's contribution is the per-lane max, not the sum — `MaxResourceCycles`). Note the column index is decoupled from the matmul/matprep *opcode* intuition — `resLUT` maps the matprep band (`0x00`–`0x04`) to `R[1] Matmul` and the latch band (`0x05`–`0x10`) to `R[0] Matpush`, which is the matmul-pipeline ordering, not a naive opcode-to-name pairing.

> **NOTE — `R[k]` names are binding-confirmed; micro-port semantics are not.** The seven names are CONFIRMED from the `ResourceVectorToString` template and the `Acc` slot index. The deeper physical mapping (which MXU sub-stage each column reserves) has no `ToString` for the `CycleTable::Resource` or `CycleTable::Instruction` cost enums and remains functional/INFERRED.

---

## Producing the Outer Index — `CycleTableInstruction` and the two MXU LUTs

The `GetCyclesForThroughput`/`GetResource` argument is a cycle *class*, not an opcode. For the MXU band, the class is produced by `CycleTableInstruction(LloInstruction*)` `@0x1c89ca80`, the **only** LLO→class classifier in the binary. It classifies exactly two opcode bands and is fatal on anything else:

```c
// xla::jellyfish::CycleTableInstruction  @0x1c89ca80  (decompiled, exact shape)
uint32_t CycleTableInstruction(const LloInstruction *insn) {
    uint32_t op = insn->opcode;
    if ((uint16_t)(op - 141) <= 9) {                 // opcodes 0x8d..0x96 = matmul / latch band
        uint8_t lm = insn->latch_mode();
        if (lm >= 0x34 || !bittest64(0xF000003FFFC3F, lm))
            LogFatal("Unsupported gain latch mode ", /*cycle_table.cc:431*/);
        return unk_B4389F4[lm];                       // latchLUT @0xb4389f4, 52 × int32
    }
    if ((uint16_t)(op - 155) <= 0xA) {                // opcodes 0x9b..0xa5 = matprep / matpush band
        uint8_t f = insn->matmul_data_format() - 1;
        if (f >= 0xA)
            LogFatal("Unsupported matmul data format ", /*cycle_table.cc:464*/);
        return unk_B438AC4[f];                         // fmtLUT @0xb438ac4, first 11 × int32
    }
    LogFatal("Unsupported instruction ", /*cycle_table.cc:470*/);
}
```

The classifier covers only the MXU band (classes `0x00`–`0x10`). The vector/EUP/matrix-result classes `0x11`–`0x20` are not produced here — they are emitted by the HLO-level cost model as direct ordinal immediates keyed on HLO opcode / `PrimitiveType` / memory-transfer role (see [IARS Per TensorCore](iars-per-tensorcore.md)); their producing LLO opcode set is not decoded (MEDIUM).

### latchLUT — `unk_B4389F4` (52 × `int32`, valid mask `0xF000003FFFC3F`)

`GainLatchMode → CycleTable::Instruction`, indexed by the raw `latch_mode()` byte. Only the modes set in the valid mask are reachable; all others `LogFatal` before the read. Transcribed byte-exact (only the non-zero / reachable rows shown; every other index reads `0`):

| `latch_mode()` | `latchLUT[lm]` → class | `latch_mode()` | `latchLUT[lm]` → class |
|---------------:|:----------------------:|---------------:|:----------------------:|
| `0x00`, `0x02`, `0x04` | `5` | `0x12`, `0x14`, `0x16`, `0x18` | `9` |
| `0x01`, `0x03`, `0x05` | `11` | `0x13`, `0x15`, `0x17`, `0x19` | `15` |
| `0x0a` | `12` | `0x30` | `7` |
| `0x0b`, `0x0e`, `0x10` | `6` | `0x31` | `13` |
| `0x0c` | `9` | `0x32` | `8` |
| `0x0d` | `15` | `0x33` | `14` |
| `0x0f`, `0x11` | `12` | (all others, mask-rejected) | `0` |

The valid mask `0xF000003FFFC3F` is checked by `_bittest64` *before* the lookup; the four high bits (`0x30`–`0x33`, the `0xF` at bit 48..51) map to classes `{7, 13, 8, 14}` (CONFIRMED from the `.rodata` dump) and are only reachable on the gens that emit those latch modes. Their attribution to a specific later generation is not byte-anchored (INFERRED).

### fmtLUT — `unk_B438AC4` (first 11 × `int32`)

`MatmulDataFormat → CycleTable::Instruction`, indexed by `matmul_data_format() - 1` (the JF reader validates `< 0xA`, so it reads the first 11 entries). Transcribed byte-exact:

```text
fmtLUT @0xb438ac4 (index = matmul_data_format()-1):
  index:  0  1  2  3  4  5  6  7  8  9  10
  class:  0  1  1  1  4  4  4  4  2  3   1
```

i.e. `fmt 1 → class 0`, `fmt 2/3/4 → class 1`, `fmt 5/6/7/8 → class 4`, `fmt 9 → class 2`, `fmt 10 → class 3`, `fmt 11 → class 1`. The MXU-modifier semantics (which numeric format is bf16 / fp8 / int8) are documented with the [matmul mode modifiers](matmul-mode-modifiers.md).

> **NOTE — the format LUT is wider than the JF reader uses.** Beyond index 10, additional packed-int8 / int4 format values exist in `.rodata` but the JF classifier's `< 0xA` validity check rejects them (they are used by later-gen paths). They are flagged INFERRED for JF/DF here because the JF classifier `LogFatal`s on them.

---

## JF vs DF — The Throughput Table Is Identical

`PerformanceDf::PerformanceDf` `@0x1d493060` builds the full `PerformanceJf` image, swaps the vtable, and issues exactly one quadword store:

```c
// platforms_deepsea::jellyfish::isa::PerformanceDf::PerformanceDf  @0x1d493060  (decompiled, exact)
PerformanceJf::PerformanceJf(this, dev);
*(_QWORD *)this        = off_21CC7478;            // swap vtable → PerformanceDf
*((_QWORD *)this + 5)  = 0xD00000042uLL;          // store at this+0x28
```

`this + 5` (qword) is `Performance[+0x28]`; the `int64 0x0000000D_00000042` writes `[+0x28] = 0x42 = 66` (down from JF's `88`) and `[+0x2c] = 0x0D = 13` (up from JF's `8`). **Neither `0x28` nor `0x2c` appears in the `offsetLUT`** (the priced offsets are `{0x910, 0x92c, 0x94c, 0x954, 0x958, 0x960, 0x33c, 0x340, 0x344, 0x368, 0x398, 0x39c, 0x3f4, 0x3f8}`), so `GetCyclesForThroughput` returns the same 16 values on JF and DF. The two changed cells are matmul / matprep *base latencies* consumed by `LatencyTableJellyfish`, not throughput cells — the entire v2→v3 cost difference lives on the latency axis ([MXU Latency: JF/DF](mxu-latency-jf-df.md)), and the throughput table on this page is byte-identical for both generations.

---

## Transcendentals — Scalar Virtual Overrides

Transcendental cost on JF/DF does **not** go through the cycle-class LUT. The `JfCycleTable` vtable carries two scalar `const` virtual overrides (slots `+0x18` / `+0x20`) that return a fixed estimate regardless of operand size:

```c
__int64 JfCycleTable::EstimateSinCosCost(...) @0x1c89dd20 { return 198; }   // sin / cos
__int64 JfCycleTable::EstimateTanCost(...)    @0x1c89dd40 { return 219; }   // tan
```

These are added on top of a per-step pipeline count by the cost model's transcendental path. The values match Pufferfish (also 198 / 219) and then shrink across the later gens (VF 154 / 170, GL/GF 142 / 151) as the XLU pipeline speeds up; the full per-gen table is on [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md).

---

## Reimplementation Recipe

A faithful `JfCycleTable` needs only the four LUTs above plus the `Performance` grid:

```c
int GetCyclesForThroughput(const Performance *perf, uint32_t cls) {
    if (cls < 0x21 && ((0x19FFC0821ULL >> cls) & 1))
        return *(const int32_t *)((const char *)perf + offsetLUT_B438B70[cls]);
    return 1;                                  // default
}
int GetResource(uint32_t cls) {
    return resLUT_B438AEC[cls];                // 0..6 = ResourceVector slot
}
// per-op accumulation (the only consumer):
//   resource_vector[GetResource(cls)] += (double)GetCyclesForThroughput(perf, cls);
// transcendentals bypass the LUT:  sin/cos -> 198,  tan -> 219.
```

The four `.rodata` tables to embed verbatim:

| Table | Address | Shape | Maps |
|-------|---------|-------|------|
| `offsetLUT` | `0xb438b70` | 33 × `int64` | class → `Performance` byte offset (this page) |
| `resLUT` | `0xb438aec` | 33 × `int32` | class → `ResourceVector` slot `0..6` (this page) |
| `latchLUT` | `0xb4389f4` | 52 × `int32` (mask `0xF000003FFFC3F`) | `GainLatchMode` → class |
| `fmtLUT` | `0xb438ac4` | 11 × `int32` (index `fmt-1`) | `MatmulDataFormat` → class |

Build `PerformanceJf` per [Performance: JF/DF](performance-jf-df.md) (the `0xe00`-byte POD, sentinel `0x7FFFFFFF`, 419 ctor stores), apply the one DF cell patch for Dragonfish, and the throughput half is complete.

---

## Cross-References

- [CycleTable Family](cycletable-family.md) — the abstract base, the six-factory registry, the per-version dispatch, and the shared cycle-class enum framing.
- [Performance: JF/DF](performance-jf-df.md) — the `0xe00`-byte `PerformanceJf`/`PerformanceDf` POD that this page's `offsetLUT` indexes into: the layout, the 419 constructor stores, the 15 `.rodata` source blocks, and the 2-cell DF delta.
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the cross-gen throughput integers and the transcendental scalar table; the JF/DF flat-cell model in context.
- [VfCycleTable](vf-cycletable.md) — the Viperfish `switch`-over-`GetResourceUsage` path the model evolves into.
- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector` whose head `R[0]..R[6]` the `resLUT` emits, and the `MaxResourceCycles` overlap reduction.
- [MXU Latency: JF/DF](mxu-latency-jf-df.md) — the orthogonal latency axis; the `LatencyTableJellyfish` copy map and the JF→DF `88→66 / 8→13` base-latency delta.
- [Matmul Mode Modifiers](matmul-mode-modifiers.md) — the `GainLatchMode` / `MatmulDataFormat` modifiers that `latchLUT` / `fmtLUT` fold into cycle classes.
- [IARS Per TensorCore](iars-per-tensorcore.md) — the non-MXU emitter path that produces cycle classes `0x11..0x20` as direct ordinal immediates.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VII — Cost & Latency Model / CycleTable — [back to index](../index.md)
