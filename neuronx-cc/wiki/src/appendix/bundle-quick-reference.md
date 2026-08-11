# The 64-Byte Bundle Quick-Reference

> *All offsets, masks, and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 share the `.text` logic, VAs drift). Encoders/decoders live in `libwalrus.so` (build-id `92b4d331`, `.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset); enums in `libBIR.so` (build-id `a9b1ea38`). Every cell is distilled from the Part-2 normative pages — re-confirmed this pass against `libwalrus.so` and the `.rodata` size asserts. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This is the one-glance cheat-sheet for reading a raw TPB instruction-bundle `.bin` dump by eye — the byte map a reimplementer keeps open while staring at `xxd` output. It distills four normative Part-2 pages into fast tables: the [64-byte bundle & header word](../isa/instruction-bundle.md) (2.1), the [ADDR4 32-bit address word](../isa/addr4.md) (2.2), the [NEURON_ISA_TPB struct family](../isa/neuron-isa-tpb-capstone.md) capstone (2.9), and the descriptor sizes from the [4+4N tensor descriptors](../isa/tensor-descriptors.md) (2.3). It carries **no new findings** — every value here is byte-verified on its source page; this page is the lookup, those pages are the proof.

The whole format reduces to four facts. **(1)** A bundle is exactly **64 bytes** (16 dwords), always — there is no other length. **(2)** Bytes `+0x00..+0x03` are the universal header word `{opcode, 0x10, 0x00, 0x00}`; the rest is an op-specific union keyed on the opcode byte. **(3)** Descriptor slots land at one of **three family-fixed offset sets** (A/B/C), not at arbitrary offsets, with a control band filling the gap. **(4)** Every descriptor slot opens with a 4-byte **ADDR4** word and follows the **`4 + 4N`** size rule (1D=8, 2D=12, 3D=16, 4D=20).

> **GOTCHA — there is no descriptor at bundle byte `+0x48`.** The recurring `+0x48` in older field maps is the *vtable* offset for `setupHeader` (`call [rax+0x48]`, slot 9) — a **code** offset, not a bundle byte. A 16-byte TENSOR3D at `+0x48` would span `+0x48..+0x57` and overflow the 64-byte bundle (`0x58 > 0x40`). [2.1]

| | |
|---|---|
| **Bundle size** | exactly **64 B** = 16 dwords; `fwrite(…, 0x40, …)` |
| **Header word** | `+0x00..+0x03 = {opcode, 0x10, 0x00, 0x00}`; LE `*(u16*)b = 0x10NN` |
| **`inst_word_len`** | `byte[1] = 0x10` hardcoded; no non-16 length exists |
| **ADDR4** | 4-byte LE `u32` @ slot+0 of every descriptor; 29-bit addr + mode/reg flags |
| **Descriptor rule** | `slot_size(N) = 4 + 4N` (1D=8 · 2D=12 · 3D=16 · 4D=20) |
| **Slot families** | A `0x10/0x30` · B `0x10/0x20/0x30` · C `0x0C/0x2C` |

---

## 1. The header word (`+0x00..+0x03`)

Written by `setupHeader` (`0x1172120`/`0x1369280`/`0x143f440`, byte-identical V2/V3/V4). The only universal field; every other byte is op-specific or zero. [2.1 §"universal header word"]

| Off | Field | Value | Note |
|---|---|---|---|
| `+0x00` | `opcode` | L3 ISA op byte | union discriminant; **not** the `bir::InstructionType` ordinal |
| `+0x01` | `inst_word_len` | `0x10` | 16 dwords = 64 B; hardcoded immediate, zero data dependence |
| `+0x02..+0x03` | reserved | `0x0000` | always zero on the wire |

```text
 byte:   +0      +1      +2      +3
       +-------+-------+-------+-------+
       |opcode | 0x10  | 0x00  | 0x00  |
       +-------+-------+-------+-------+
        \__ LE word *(u16*)b = 0x10NN __/   (NN = opcode, hi 0x10 = inst_word_len)
```

> **QUIRK —** only one op perturbs `byte[1]`: `QuantizeMx` (gen4, opcode `0xE3`) ORs bit 0 (`0x10 → 0x11`) when SATURATE is set, writing the word directly. Every other op leaves `byte[1] = 0x10`. [2.1 §GOTCHA]

### Optional sync band (`+0x04..+0x0B`)

Present **iff** the inst carries SyncInfo (DVE/Pool/Act/SP/DMA; **absent on matmul**), written by `setupSyncWait`/`setupSyncUpdate`. Not part of the header word. [2.1 §"shared-vs-op-specific split"]

| Off | Field | Off | Field |
|---|---|---|---|
| `+0x04` | wait mode | `+0x06` | update mode |
| `+0x05` | `wait_idx` | `+0x07` | `update_idx` |
| `+0x08..+0x0B` | sync wait/update value (dword) | | |

---

## 2. The ADDR4 word (32-bit, `u32` LE @ slot+0)

Opens **every** descriptor slot. Bits `0..28` are a 29-bit byte address; bits `25..28` double as the SBUF/PSUM region discriminator; bits `29/30` are the mode nibble; bit `31` is register-mode. [2.2]

| Field | Bits | Mask | Meaning |
|---|---|---|---|
| Byte address | `0..28` | `0x1FFFFFFF` | 29-bit on-chip byte address |
| Region class | `25..28` | `0x1E000000` | `0` ⇒ SBUF · nonzero ⇒ PSUM |
| Mode bit0 (INDIRECT) | `29` | `0x20000000` | gather/index slot; the **only** bit the encoder stamps (`or [slot+3],0x20`) |
| Mode bit1 (ACTIVE) | `30` | `0x40000000` | dynamic-AP; set **upstream**, never by the ADDR4 encoder |
| Register-mode (RM) | `31` | `0x80000000` | RM=1 ⇒ byte0 = regid, bits `8..30` = 0 |
| Register id | `0..7` | `0xFF` | only when RM=1; valid `0..63` (`< 0x40`) |

```text
 bit 31   30   29   28 ........... 25 24 ......................... 0
    +----+----+----+----------------+----------------------------+
    | RM | M1 | M0 |  region class  |     byte address (29-bit)  |
    +----+----+----+----------------+----------------------------+
     0x80 0x40 0x20 \_ 0x1E000000 _/ \________ 0x1FFFFFFF ______/
                                       (region bits 25..28 are part of the address)

 mode nibble = byte3 & 0x60 = bits[30:29]:
   0b00 STATIC · 0b01 INDIRECT-gather (0x20) · 0b10 ACTIVE (0x40) · 0b11 DEAD (0x60)
 RM (bit31, &0x80, sign-of-byte3) is ORTHOGONAL to the nibble.
 RM=1  ⇒  word = 0x80000000 | (regid & 0xFF),  bits 8..30 = 0.
 PSUM  ⇔  (addr29 - 0x2000000) <= 0x3FFFFF   (4 MiB window @ 32 MiB); SBUF clears bits 25..28.
```

> **GOTCHA — register-mode is bit 31 (`0x80`), not bit 30.** Bit 30 (`0x40`) is the ACTIVE nibble bit. The decoder takes the register branch on a sign test of byte 3 (= word bit 31), and the encoder stamps `or [slot+3], 0x80`. RM and the mode nibble (`&0x60`) are independent. [2.2 §GOTCHA]

---

## 3. The three slot families

Each op places its operand descriptors at a **family-fixed** offset (`lea [base+OFF]` before `assignAccess<…>`), chosen by descriptor width (3D=16 B vs 4D=20 B) and operand count (2 vs 3 slots). The control band fills the gap exactly one missing descriptor wide. [2.1 §"three descriptor-slot families"]

| Family | Shape | Slot offsets | Control band | Canonical witness |
|---|---|---|---|---|
| **A** | 3D, 2-slot | src `+0x10` · dst `+0x30` | `+0x20..+0x2F` | Matmul `0x02` (`generateMatMul`) |
| **B** | 3D, 3-slot (contiguous) | dst `+0x10` · in0 `+0x20` · in1 `+0x30` | `+0x0C..+0x0F` *(before slots)* | TensorTensor `0x41` |
| **C** | 4D, 2-slot | in `+0x0C` · out `+0x2C` | `+0x20..+0x2B` | Pool `0x45` / Copy `0x46` |

```text
 byte:  00      0C    10        20        2C  30        3F
        |hdr|   |     |         |         |   |          |
  A  :  [HD]----band'----[3D@10]-[ BAND ]------[3D@30..3F]   src@10 dst@30
  B  :  [HD][band@0C..0F][3D@10][3D@20]-------[3D@30..3F]   dst@10 in0@20 in1@30 (fill 10..3F)
  C  :  [HD]------[4D@0C........20][BAND 20..2B][4D@2C..3F]  in@0C out@2C
```

The anchor ops above are byte-verified. The family of the remaining ~90 ops in the [110-row opcode table](master-opcode-table.md) is assigned from descriptor-width evidence on their per-engine pages rather than from a direct byte read, so treat the tail as a well-supported reading rather than a measurement. The rule itself — *width + operand count picks the offsets, the band fills the gap* — holds across all four byte-verified families. [2.9 §NOTE]

> **QUIRK — "slots at `+16/+32/+48`" is Family B only.** That phrasing (`0x10/0x20/0x30`) is correct for B's three contiguous slots, not for A (two slots `0x10/0x30`, band at `0x20`) or C (`0x0C/0x2C`). It is **not** the universal layout, and the `+0x48` in it conflates the vtable offset with a wire byte. [2.1 §Family B QUIRK]

> **NOTE — mixed-width anchors.** The low/high positions are *slots* that accept a variable-width descriptor. BNStats puts a 4D (20 B) at the `+0x0C` anchor but a 2D (12 B) at the `+0x30` anchor. The anchor is fixed; the width is not. [2.1 §"mixed-width slots"]

---

## 4. Descriptor sizes — the `4 + 4N` rule

Every descriptor = ADDR4 (4 B) + `N` × i16 stride + `N` × u16 num. Sizes pinned by `.rodata` asserts (`"ISA mem pattern ND must have K bytes to encode"` @ `0x1d6e810`/`8b0`/`920`/`990`). SRC operands use the `TENSOR*` name, DST operands the byte-identical `MEM_PATTERN*` name (role, not layout). [2.3 / 2.9]

| Descriptor | `N` | Size | stride array | num array (base `4+2N`) |
|---|---|---|---|---|
| `TENSOR1D` | 1 | **8 B** | `+0x04` | `+0x06` |
| `TENSOR2D` / `MEM_PATTERN2D` | 2 | **12 B** | `+0x04`/`+0x06` | `+0x08`/`+0x0A` |
| `TENSOR3D` / `MEM_PATTERN3D` | 3 | **16 B** | `+0x04`/`+0x06`/`+0x08` | `+0x0A`/`+0x0C`/`+0x0E` |
| `TENSOR4D` / `MEM_PATTERN4D` | 4 | **20 B** | `+0x04`..`+0x0A` | `+0x0C`/`+0x0E`/`+0x10`/`+0x12` |

```text
 TENSOR_ND slot = [ ADDR4 u32 @+0 ][ N x i16 stride @+4 ][ N x u16 num @+(4+2N) ]
                  two SEPARATE contiguous arrays (all strides, then all nums) — NOT interleaved.
 stride = signed i16, ELEMENT units (no dtype scale).  num = unsigned u16, 0 illegal.
 spare dims unit-filled {stride=1, num=1} (0x00010001), never 0.  partition dim folds into ADDR4.
```

### The MX / indirect descriptors (16/20 B)

Same `4 + 4N` footprint; the body re-purposes the static stride/num region for index/scale ADDR4s. [2.9 §2.4/2.5]

| Descriptor | Size | Body (after first ADDR4) |
|---|---|---|
| `MXMEM_PATTERN1D` | 16 B | data ADDR4 `+0`, scale ADDR4 `+4`, `k_extent` `+8`, `step_dir` `+0xA`, `scale_part` `+0xB` (CoreV4-only; 12 written) |
| `MXINDIRECT16B` | 16 B | index ADDR4 `+0` (bit29 set), data `+4`, scale `+8`, `k_extent` `+0xC` |
| `INDIRECT16B` | 16 B | index ADDR4 `+0` (bit29 set), data `+4`, `num` `+8`, inert `+0xA..+0xF` |
| `INDIRECT20B` | 20 B | same payload, 20-byte form (4-D AP) |

> **NOTE — DMA `DIRECT2D` (`0xD4`) is a fourth, distinct body (Family D).** It reuses `+0x00..+0x0B` (header + sync) but carries 8-byte **ADDR8** addresses (src `+0x10`, dst `+0x28`) and its own 2-D step/num body — *not* mem-pattern slots. Handle it as its own case. [2.1 §NOTE]

---

## 5. One-glance reading recipe

To read a raw 64-byte bundle dump:

```text
1. byte +0  = opcode (look up family + meaning in the master opcode table)
   byte +1  should be 0x10 (0x11 only on QuantizeMx+SATURATE); +2,+3 = 0.
2. opcode -> family (A/B/C/D) -> slot offsets + control-band offset (§3).
3. at each slot offset, read 4 bytes LE as ADDR4 (§2): mask 0x1FFFFFFF = address,
   bit31 = register (byte0 = regid), bit29 = indirect, region nonzero = PSUM.
4. after ADDR4: N strides (i16) then N nums (u16) per the 4+4N map (§4),
   N fixed by the slot's descriptor TYPE (3D for A/B, 4D for C).
5. control band bytes carry dtypes / ALU / accumulate / perf — per-op meanings
   live on the per-engine encoding page for that opcode.
6. any byte not written by the op reads ZERO (the emitter memset-zeroes all 64).
```

---

## Cross-References

- [2.1 The 64-byte instruction bundle & header](../isa/instruction-bundle.md) — the header word, emission skeleton, and the three families in full (the proof for §1, §3).
- [2.2 ADDR4 — the 32-bit address word](../isa/addr4.md) — the bit-field, mode nibble, PSUM window, and register-mode derivation (the proof for §2).
- [2.9 NEURON_ISA_TPB struct-family capstone](../isa/neuron-isa-tpb-capstone.md) — the compilable `neuron_isa_tpb.h` header that types every bundle and descriptor (the proof for §4 and the union).
- [2.3 TENSOR1/2/3D descriptors — the 4+4N rule](../isa/tensor-descriptors.md) — the per-type byte map, stride/num signedness, and `{1,1}` unit-fill (the proof for §4).
- [Master opcode reference](master-opcode-table.md) — opcode → engine → family for all 110 `InstructionType` arms.
- [12.4 Per-engine `.bin` members](../formats/per-engine-bin.md) — where these 64-byte bundle streams land in the NEFF container.
