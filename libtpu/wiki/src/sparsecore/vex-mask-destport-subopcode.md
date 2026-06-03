# VEX Mask / Dest-Port / Sub-Opcode Map

> *Every bit position, field width, opcode immediate, register band, and proto offset on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Addresses are VMA; in `.text`/`.rodata`, VMA == file offset. Other versions differ.*

## Abstract

The SparseCore [VectorExtended (VEX)](vectorextended-vex.md) slot is the scan / sort / dedup / uniquify family of the [TEC](tec-engine.md) vector datapath. Where the [VEX operand-port binding](vex-operand-port.md) page owns the seven `V0..V6` source read-port selectors and the greedy allocator that fills them, this page decodes the *three remaining* VEX bundle control fields whose bit positions were already pinned but whose **meaning and source operand** were open:

- the **vector-mask field** `@bit0x104` (5 bits) — what masks the lanes,
- the **destination read-port field** `@bit0x10c` (3 bits) — where the VEX result is routed for write-back, and
- the **sub-opcode field** `@bit0x10f` (6 bits) — which member of the 48-encoder VEX op family the bundle carries.

The central structural finding is that the vector-mask field is **not** a lane bitmask and **not** a sublane count: it is a 5-bit *index* naming one of 32 architectural mask registers `M0..M31`. The selected M-register itself holds the 2D (lane × sublane) active/inactive predicate; the bundle field only chooses which register supplies it. Every scan/sort op is masked — the emit body attaches a mask unconditionally — so the VEX scan datapath is a fully *masked scan*. This per-lane vector mask is **orthogonal** to whole-instruction predication (the last `MCInst` operand, handled by `EmitPredicationToSlot`): a VEX op carries both.

The destination read-port field `@bit0x10c` is written by the `VectorExtended` encoder and has two real cases: for `Sort` it is the allocated read-port index of the first source operand (and Sort writes a *second* 3-bit port field `@bit0x109` for its second source); for the pure scan ops it is **absent** — the scan result is committed out-of-line by a separate `PopXrf` result-commit op in another bundle slot. The proto `+0x18` slot it reads from is *also* used by the `VresMove` op, but `VresMove` is a separate `SparseCoreTecVectorResult`-slot op encoded to a different bundle region (its dest vreg lands at bundle bit `245`, not `bit0x10c`); the shared *proto offset* is what differs by emit body, not the bundle bit. The sub-opcode field selects among 48 byte-exact encoders, contiguous `0x04..0x33` with no gaps and no duplicates.

For reimplementation, the contract is:

- **The vector mask is a 5-bit M-register selector, not a bitmask.** Decode `bit0x104..0x108` as a register index `m ∈ [0,31]`; the architectural register id is `m + 0x5f` (`M0 = 0x5f .. M31 = 0x7e`). The named M-register supplies the per-(lane,sublane) execution predicate. Mask-*write* destinations (index-scan / uniquify mask results) are restricted to the lower half `M0..M15`.
- **The dest read-port at `bit0x10c` (in the `VectorExtended` slot) is routing, not data.** `Sort` → first source's allocated port (+ second source at `bit0x109`); pure scans → field absent, result via `PopXrf`. The 3-bit width (0..6) matches the 7-entry read-port allocator `V0..V6`. (The `VresMove` op reuses the same proto `+0x18` slot for its dest vreg, but is a different *slot* op encoded to bundle bit `245` — see §2.2.)
- **The sub-opcode at `bit0x10f` (6 bits) selects one of 48 contiguous encoders `0x04..0x33`.** 32 are dispatch-reachable; 16 are present-but-unreachable variants; 4 reachable ops share an F32/U32-sibling encoder (the dtype distinction is carried by the pack-format attribute layer, not by the sub-opcode).

| | |
|---|---|
| **Slot** | `VectorExtended` (VEX) — TEC scan/sort/dedup family |
| **Mask field** | `bit0x104..0x108` (5b) — M-register selector `M0..M31` |
| **Mask getter** | `GetVectorMask<glc::isa::SparsecoreVectorMask>` `@0x13a33320` (Ghostlite; band `[0x5f,0x7e]`, value `id − 0x5f`) |
| **Mask source operand** | `MCInst` operand[1] |
| **Dest read-port field** | `bit0x10c..0x10e` (3b), proto `+0x18`, present `+0x10 & 0x1`, range 0..6 |
| **2nd port (Sort only)** | `bit0x109..0x10b` (3b), proto `+0x1c`, present `+0x10 & 0x2` |
| **Sub-opcode field** | `bit0x10f..0x114` (6b), proto `+0x50` selects encoder; encoder writes `bit0x10f` |
| **Sub-opcode range** | `0x04..0x33` contiguous, 48 encoders (32 reachable, 16 unreachable, 4 dtype-shared) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page decodes three VEX *control* fields; the seven `V0..V6` *operand* read-ports and the 7-entry greedy allocator (`FindAndEmitToUnusedPort`) live on the [VEX operand-port binding](vex-operand-port.md) page and are not re-derived here.** The masked-scan *inactive-lane output* micro-semantic and the internal layout of the M-register predicate word are one layer below this encoding and are covered on the [M-register predicate](m-register-predicate.md) page. The VEX opcode → op dispatch table and the full op roster live on the [VectorExtended / VEX](vectorextended-vex.md) page.

---

## 1. The Vector-Mask Field `@bit0x104` — a 5-bit M-register selector

### 1.1 The getter proves it is a register, not a bitmask

The mask field is written from the value returned by `GetVectorMask<SparsecoreVectorMask>` `@0x13a33320`. Its body is short and decisive:

```c
// xla::tpu::sparse_core::isa_emitter::GetVectorMask<...SparsecoreVectorMask>  @0x13a33320
__int64 GetVectorMask(__int64 a1) {              // a1 = &MCOperand
  if ( *(_BYTE *)a1 != 1 ) { /* "operand.isReg()"      */ LogFatal(...); }
  unsigned int v1 = *(_DWORD *)(a1 + 8);         // the register id
  if ( v1 <= 0x5E ) { /* "regno >= llvm::TPU::M0"  (id < 0x5f) */ LogFatal(...); }
  if ( v1 >= 0x7F ) { /* "regno <= llvm::TPU::M31" (id > 0x7e) */ LogFatal(...); }
  return v1 - 95;                                // 95 == 0x5f, so value in [0,0x1f]
}
```

Three facts fall out of this:

1. **The operand must be a register.** `*a1 != 1` (the `MCOperand` kind tag) traps with `operand.isReg()` (source `isa_emitter_base.h:555`). A bitmask immediate would never be a register; this is a register *name*.
2. **Its id must lie in the M-register band `[0x5f, 0x7e]`.** The two range traps cite `regno >= llvm::TPU::M0` (`isa_emitter_base.h:557`) and `regno <= llvm::TPU::M31` (`:558`). That is exactly 32 values: `M0 = 0x5f .. M31 = 0x7e`.
3. **The returned value is `id − 0x5f ∈ [0,0x1f]`** — a dense 5-bit index, which is exactly the width of the `bit0x104` field. A 5-bit field can name 32 registers; a sublane count or lane bitmask would not be a register operand at all (the `isReg()` trap rejects any immediate). **The field is an M-register selector.**

Both instances shown here are the Ghostlite (`gxc::glc`) target; a second instantiation, `GetVectorMask<glc::isa::SparsecoreVmask>` `@0x13a2d900`, is byte-identical (same band, same `id − 0x5f`), and both are used interchangeably by the VEX emitters. (The 6acc60406/`gfc` target carries its own structurally-identical copies at `0x13aa9b60` / `0x13ab1c80`.)

> **NOTE — what an M-register *is*.** The named register holds a 2D (lane-range × sublane-range) active/inactive predicate, constructed by the region builder from four bound arguments checked against `Target::SublaneCount` and `Target::LaneCount` (the exact lane/sublane counts are a target-table value, not re-derived here). The bundle field selects *which* M-register; the *content* of the predicate word (its (lane,sublane) bit packing and the inactive-lane output disposition) is one layer below this encoding — see [M-register predicate](m-register-predicate.md).

### 1.2 The proto layout and the unconditional present-bit

The selected mask value reaches the bundle through the per-op proto message and a `BitCopy` in the encoder. Two layouts exist, by op family.

**Scan family.** The mask value lands at proto `+0x38`, with present-bit proto `+0x11 & 0x1`. The encoder tail copies it to the bundle (from `EncodeSparseCoreTecVectorExtendedAddScanF32` `@0x1eb32380`):

```c
// tail of the AddScanF32 encoder — the mask copy
if ( (*((_BYTE *)proto + 17) & 1) != 0 ) {       // present  +0x11 & 1   (byte 17 == 0x11)
  v13[0] = *((int *)proto + 14);                 // value    +0x38       (int[14] == 0x38)
  BitCopy(a1, 260, v13, 0, 5);                    // dest bit 260 == 0x104, width 5
}
```

The emit body attaches the mask **unconditionally** for every scan op (it always sets the proto `+0x11 & 0x1` present bit). There is no "unmasked scan" form in the encoding — every VEX scan/sort/dedup op is a masked operation. Inactive lanes are gated out of the scan/reduce datapath (they contribute the reduction identity and produce no output write); this masked-scan datapath behavior is **INFERRED** at the datapath layer and **CONFIRMED** only at the encoding/register-class layer.

**Sort family.** Sort carries the mask at proto `+0x3c`, present-bit proto `+0x11 & 0x2`, copied to the same bundle position `bit0x104` (5b). From `EncodeSparseCoreTecVectorExtendedSortIntegerAscending` `@0x1eb3ac00`:

```c
// tail of the SortIntegerAscending encoder — the mask copy
if ( (*((_BYTE *)proto + 17) & 2) != 0 ) {       // present  +0x11 & 2
  v14[0] = *((int *)proto + 15);                 // value    +0x3c   (int[15] == 0x3c)
  BitCopy(a1, 260, v14, 0, 5);                    // dest bit 260 == 0x104, width 5
}
```

The corresponding emit body `EmitVectorSort<SparsecoreVectorMask, SparsecoreVregReadPort, SparseCoreTecVectorExtended_SortIntegerAscending>` `@0x13a4a3a0` (the op is the *third* template arg) reads the mask from `MCInst` operand[1] and sets both present bits in one store:

```c
int v4 = GetVectorMask<...SparsecoreVectorMask>(*(operands) + 0x10);  // operand[1] → mask reg
// ... two source vregs via GetVregno + FindAndEmitToUnusedPort<Sort> ...
*(proto + 0x10 dword) |= 0x203u;   // +0x10 &= 0x03 (two dest ports present) ; +0x11 &= 0x02 (mask present)
```

The single `|= 0x203` sets proto `+0x10 & 0x03` (the two dest read-port present bits, §2) and proto `+0x11 & 0x02` (the Sort mask present bit) at once.

### 1.3 The mask-*write* band — `M0..M15`

Index-scan and uniquify ops *produce* a mask result; the destination of such a write is read by `GetVMDestregno` `@0x13a65b20`, whose band is **half** the read band:

```c
// xla::tpu::sparse_core::isa_emitter::GetVMDestregno  @0x13a65b20
if ( *(_BYTE *)this != 1 )  { /* "operand.isReg()"          */ LogFatal(...); }   // :110
unsigned int v2 = *((_DWORD *)this + 2);
if ( v2 <= 0x5E )           { /* "regno >= llvm::TPU::M0"    */ LogFatal(...); }   // :112
if ( v2 >= 0x6F )           { /* "regno <= llvm::TPU::M15"   */ LogFatal(...); }   // :113  (id > 0x6e)
return v2 - 95;
```

Band `[0x5f, 0x6e]` = `M0..M15` (16 registers). So the mask *read* path can name any of `M0..M31`, but the mask *write* path is restricted to the lower 16. Whether `M16..M31` are read-only predicate inputs with no write path is **INFERRED** from this band split (not proven by a write-path absence search).

### 1.4 Orthogonality to whole-op predication

The per-lane vector mask is independent of instruction-level predication. The latter is emitted by `EmitPredicationToSlot<PredicateDest,Predication,...>` `@0x13a4a160`, which reads the **last** `MCInst` operand (derived from `NumOperands`) and gates the *whole* instruction. A VEX op therefore carries two predication mechanisms at once: a per-(lane,sublane) M-mask (this field) and a whole-op predicate (a separate slot field). They are produced from different operands and serve different granularities; a reimplementer must encode both.

---

### TABLE A — the vector-mask field `@bit0x104` (5b)

| Property | Value | Confidence |
|---|---|---|
| Bundle bits | `bit0x104..0x108` (5 bits) | CONFIRMED |
| Semantics | index naming one of 32 M-registers `M0..M31` (not a bitmask, not a count) | CONFIRMED |
| Getter | `GetVectorMask<glc::isa::SparsecoreVectorMask>` `@0x13a33320` (Ghostlite; == `<glc::isa::SparsecoreVmask>` `@0x13a2d900`) | CONFIRMED |
| Operand-kind check | `MCOperand.isReg()` (`isa_emitter_base.h:555`) | CONFIRMED |
| Register band | `[0x5f, 0x7e]` = `M0..M31` (asserts `:557 / :558`) | CONFIRMED |
| Encoded value | `regid − 0x5f` ∈ `[0,0x1f]` | CONFIRMED |
| Mask source operand | `MCInst` operand[1] | CONFIRMED |
| Scan-family proto | value `+0x38`, present `+0x11 & 0x1` | CONFIRMED |
| Sort-family proto | value `+0x3c`, present `+0x11 & 0x2` | CONFIRMED |
| Presence | always attached by the emit body (every VEX op is masked) | CONFIRMED |
| Mask-write band | `GetVMDestregno` `@0x13a65b20` → `[0x5f, 0x6e]` = `M0..M15` (16) | CONFIRMED |
| Inactive-lane output (zero/preserve/skip) | gated out, contributes reduction identity, no write | INFERRED |
| Orthogonal to | whole-op predication (`EmitPredicationToSlot` `@0x13a4a160`, last operand) | CONFIRMED |

---

## 2. The Destination Read-Port Field `@bit0x10c` — write-back routing

The dest read-port field is `bit0x10c..0x10e` (3 bits), sourced from proto `+0x18`, present-bit proto `+0x10 & 0x1`. The 3-bit width gives range 0..6 — exactly the 7-entry read-port set `V0..V6` of the VEX operand allocator (see [VEX operand-port binding](vex-operand-port.md)). Within the `VectorExtended` encoder this field has two real cases (Sort writes it, pure scans omit it). The proto `+0x18` slot is *also* read by the separate `VresMove` op, but `VresMove` is a `SparseCoreTecVectorResult`-slot op (oneof tag `13`) whose encoder maps `+0x18` to a *different* bundle bit; what differs across emit bodies is the meaning of the shared proto offset, not the meaning of `bit0x10c`.

### 2.1 Pure scan ops — the field is absent

For the pure scan ops (`AddScan`, `MinScan`, `MaxScan`, the index-scans, the segmented variants, `DuplicateCount`, `Uniquify`) the emit body never writes proto `+0x18`; the present bit stays clear, and the encoder skips the `bit0x10c` copy. The `AddScanF32` encoder gates the copy on the present bit:

```c
// AddScanF32 encoder  @0x1eb32380  — the dest read-port copy is conditional
if ( (*((_BYTE *)proto + 16) & 1) != 0 ) {       // present  +0x10 & 1
  v13[0] = *((int *)proto + 6);                  // value    +0x18  (int[6] == 0x18)
  BitCopy(a1, 268, v13, 0, 3);                    // dest bit 268 == 0x10c, width 3
}
```

For pure scans the result is **not** written back through an inline dest read-port. It is committed *out-of-line* by a separate `PopXrf` result-commit op (`EmitXrfResultOp` `@0x13a14180`), occupying its own bundle slot. The PopXrf op carries an XRF *write-group* selector — also at proto `+0x18`, but in a **different** submessage (`SparseCoreTecVectorResult`-PopXrf), so it is not the same field as the dest read-port. The inline dest read-port (this field) and the out-of-line PopXrf write-group are the two halves of the VEX result path; the PopXrf write-group encoding itself is documented on the [VEX operand-port binding](vex-operand-port.md) page.

### 2.2 `VresMove` — a separate `VectorResult`-slot op, NOT `bit0x10c`

`VresMove` (the "move a read-port-held value into a vreg" op, proto oneof tag `13`) is the op that drains a read-port-held value into a named vreg. It does **not** write `bit0x10c`: it is a `SparseCoreTecVectorResult`-slot op, emitted by `EmitVectorResultMove<...VregReadPort, SparseCoreTecVectorResult>` `@0x13a4a220` and encoded by `EncodeSparseCoreTecVectorResultVresMove` `@0x1eb41fa0`, which lands its fields in a *different* bundle region. The emit body:

```c
// EmitVectorResultMove<...>  @0x13a4a220
int  dst = GetVregno(this,        a2);   // operand[0] → destination vreg
unsigned src = GetVregno(this+16, a2);   // operand[1] → source vreg
// ... DefaultConstruct<SparseCoreTecVectorResult_VresMove>(...) , oneof tag = 13 ...
*(_DWORD *)(vresmove + 24) = dst;        // proto +0x18 gets dest vreg
*(_BYTE  *)(vresmove + 16) |= 1u;        //       +0x10 & 1   present
FindAndEmitToUnusedPort<...>(&st, a2, src, vresmove);   // src to an unused read-port
*(_DWORD *)(vresmove + 28) = port_idx;   // proto +0x1c gets source's allocated read-port
*(_BYTE  *)(vresmove + 16) |= 2u;        //       +0x10 & 2   present
```

The `VresMove` encoder maps those proto slots to its own slot's bundle bits, **not** to `bit0x10c`/`bit0x109`:

```c
// EncodeSparseCoreTecVectorResultVresMove  @0x1eb41fa0
v13[0] = 7;             BitCopy(a1, 252, v13, 0, 3);   // VectorResult slot opcode 7 @ bit 252, 3b
v13[0] = proto[+0x18];  BitCopy(a1, 245, v13, 0, 6);   // dest vreg     @ bit 245, 6b  (present +0x10&1)
v13[0] = proto[+0x1c];  BitCopy(a1, 235, v13, 0, 3);   // source port   @ bit 235, 3b  (present +0x10&2)
// ... then the V0..V6 vreg-array fields at 346/443/455/406/418/369/381 ...
```

So `VresMove` shares the proto `+0x18`/`+0x1c` *offsets* with the `VectorExtended` dest read-port, but its bundle position is bit `245` (dest vreg, 6b) and bit `235` (source port, 3b) — it is structurally a `VectorResult`-slot op, not part of the `bit0x10c` field. The only thing the two share is the proto submessage byte layout.

### 2.3 `Sort` — two 3-bit read-ports, key and value

`Sort` (opcodes for ascending/descending × integer/float) needs two source read-ports — a key and a value — so it uses `bit0x10c` for the first and a *second* 3-bit field `bit0x109` for the second. The `SortIntegerAscending` encoder writes both back-to-back, right after the sub-opcode:

```c
// SortIntegerAscending encoder  @0x1eb3ac00  (head)
v14[0] = 20;          BitCopy(a1, 271, v14, 0, 6);   // sub-opcode 0x14  @ bit 0x10f (271), 6b
v14[0] = proto[+0x18]; BitCopy(a1, 268, v14, 0, 3);   // dest port 1      @ bit 0x10c (268), 3b
v14[0] = proto[+0x1c]; BitCopy(a1, 265, v14, 0, 3);   // dest port 2      @ bit 0x109 (265), 3b
// ... then the V0..V6 vreg-array fields and the mask copy (§1.2) ...
```

The emit body `EmitVectorSort<...Sort>` `@0x13a4a3a0` fills proto `+0x18` and `+0x1c` from two `FindAndEmitToUnusedPort<Sort>` allocations (the first source's port and the second source's port). Both present bits are set by the `|= 0x203` store shown in §1.2 (`+0x10 & 0x03`). For Sort the two 3-bit fields at `bit0x10c`/`bit0x109` are *allocated read-port indices*; pure scans leave both absent (§2.1). `VresMove` reaches the same proto `+0x18`/`+0x1c` offsets but, being a `VectorResult`-slot op, emits to bundle bits `245`/`235` instead (§2.2).

---

### TABLE B — the dest read-port field `@bit0x10c` (3b) by op family

TABLE B-1 — cases of the `bit0x10c` field (the `VectorExtended`-slot dest read-port):

| Op family | proto `+0x18` source → `bit0x10c` | Extra fields | Confidence |
|---|---|---|---|
| Pure scan (`AddScan`/`MinScan`/`MaxScan`/index-scans/`Segmented*`/`DuplicateCount`/`Uniquify`) | not written — field absent; result via `PopXrf` (`EmitXrfResultOp` `@0x13a14180`) | — | CONFIRMED |
| `Sort` (asc/desc × int/float) | first source's **allocated read-port** (`FindAndEmitToUnusedPort<Sort>`) | second source's port → `+0x1c` → `bit0x109` (3b, present `+0x10 & 0x2`) | CONFIRMED |

TABLE B-2 — `VresMove` shares the proto offsets but is a separate `VectorResult`-slot op (NOT `bit0x10c`):

| Op | proto slot | bundle bit | width | source | Confidence |
|---|---|---|---|---|---|
| `VresMove` (oneof tag `13`) dest vreg | `+0x18` (present `+0x10 & 0x1`) | `245` | 6b | operand[0] (`GetVregno` `@0x13a659c0`) | CONFIRMED |
| `VresMove` source read-port | `+0x1c` (present `+0x10 & 0x2`) | `235` | 3b | `FindAndEmitToUnusedPort<...VresMove>` | CONFIRMED |
| `VresMove` slot opcode | — | `252` | 3b | constant `7` | CONFIRMED |

| Field | Bits | proto | Present | Range | Confidence |
|---|---|---|---|---|---|
| Dest read-port 1 (`VectorExtended`) | `bit0x10c..0x10e` (3b) | `+0x18` | `+0x10 & 0x1` | 0..6 | CONFIRMED |
| Dest read-port 2 (Sort) | `bit0x109..0x10b` (3b) | `+0x1c` | `+0x10 & 0x2` | 0..6 | CONFIRMED |

> **NOTE — `bit0x10c` ≠ the PopXrf write-group, even though both are at proto `+0x18`.** The dest read-port lives in the `VectorExtended` / `VectorResult` submessage and names which inline read-port carries the result for write-back. The PopXrf XRF write-group lives in the PopXrf submessage and names which XRF partition the out-of-line scan result commits to. They are distinct fields reached by distinct opcodes; do not conflate them.

---

## 3. The Sub-Opcode Field `@bit0x10f` — the 48-encoder VEX op map

The sub-opcode is `bit0x10f..0x114` (6 bits), written as the *first* `BitCopy` in every VEX encoder. The recovery enumerates all 48 `EncodeSparseCoreTecVectorExtended<Op>` encoders, reads the constant the encoder loads, and confirms it against `bit0x10f`. The pattern is invariant:

```c
// every VEX encoder begins with the sub-opcode copy, e.g. AddScanF32 @0x1eb32380:
v13[0] = 5;                  // the sub-opcode constant
BitCopy(a1, 271, v13, 0, 6); // dest bit 271 == 0x10f, width 6
```

Spot-checked constants (decompile-verified this pass): `MaxIndexScanU32 = 4 (0x04)`, `AddScanF32 = 5 (0x05)`, `MinScanF32 = 6 (0x06)`, `MaxScanF32 = 7 (0x07)`, `MinIndexScanF32 = 8 (0x08)`, `SortIntegerAscending = 20 (0x14)`, `SortFloatDescending = 23 (0x17)`, `UniquifyFloat = 27 (0x1b)`, `SegmentedMaxIndexScanBf16 = 51 (0x33)`. All nine agree with the map below, and the constants land exactly on the contiguous range endpoints (`0x04` and `0x33`).

The map is **contiguous `0x04..0x33`** — 48 encoders, zero gaps, zero duplicates. Reachability splits it into three classes:

- **32 dispatch-reachable encoders** — wired through the VEX opcode dispatch table.
- **16 present-but-unreachable encoders** (marked `*`) — the `U16-index` / `Bf16-index` / `*PartialSumS32` / `*PartialSumF32` variants. They exist with valid sub-opcodes but are not reached by the dispatch table in this generation; whether they are reachable in other SparseCore generations is **INFERRED** (not arm-traced here).
- **4 dtype-sharing reachable ops** (marked `‡`) — `AddScanS32`, `MaxScanU32`, `MinScanU32`, `MinIndexScanU32` have **no dedicated encoder**; they reuse their F32/U32-sibling's encoder at the *same* sub-opcode. The S32/U32-vs-F32 distinction is carried by the pack-format attribute layer (`VpackFormat`), not by `bit0x10f`. So 36 reachable ops ≡ 32 reachable encoders + 4 encoder-sharing ops, and 48 − 32 = 16 unreachable encoders.

---

### TABLE C — the complete 48-encoder VEX sub-opcode map (`bit0x10f`, 6b; CONFIRMED byte-exact; CONTIGUOUS)

`*` = encoder present but dispatch-unreachable in this generation. `‡` = a reachable op shares this encoder via a dtype sibling (dtype carried by the pack-format layer).

| sub-op | op (`SparseCoreTecVectorExtended_<X>`) | encoder @ | sub-op | op | encoder @ |
|---|---|---|---|---|---|
| `0x04` | `MaxIndexScanU32` | `0x1eb32000` | `0x1c` | `AddScanS16PartialSumS16` | `0x1eb33500` |
| `0x05` | `AddScanF32` (‡also `AddScanS32`) | `0x1eb32380` | `0x1d` `*` | `AddScanS16PartialSumS32` | `0x1eb33880` |
| `0x06` | `MinScanF32` (‡also `MinScanU32`) | `0x1eb32700` | `0x1e` | `MinScanU16` | `0x1eb33c00` |
| `0x07` | `MaxScanF32` (‡also `MaxScanU32`) | `0x1eb32a80` | `0x1f` | `MaxScanU16` | `0x1eb33f80` |
| `0x08` | `MinIndexScanF32` (‡also `MinIndexScanU32`) | `0x1eb32e00` | `0x20` `*` | `MinIndexScanU16` | `0x1eb34300` |
| `0x09` | `MaxIndexScanF32` | `0x1eb33180` | `0x21` `*` | `MaxIndexScanU16` | `0x1eb34680` |
| `0x0a` | `SegmentedAddScanS32` | `0x1eb35f00` | `0x22` | `AddScanBf16PartialSumBf16` | `0x1eb34a00` |
| `0x0b` | `SegmentedMinScanU32` | `0x1eb36280` | `0x23` `*` | `AddScanBf16PartialSumF32` | `0x1eb34d80` |
| `0x0c` | `SegmentedMaxScanU32` | `0x1eb36600` | `0x24` | `MinScanBf16` | `0x1eb35100` |
| `0x0d` | `SegmentedMinIndexScanU32` | `0x1eb36980` | `0x25` | `MaxScanBf16` | `0x1eb35480` |
| `0x0e` | `SegmentedMaxIndexScanU32` | `0x1eb36d00` | `0x26` `*` | `MinIndexScanBf16` | `0x1eb35800` |
| `0x0f` | `SegmentedAddScanF32` | `0x1eb37080` | `0x27` `*` | `MaxIndexScanBf16` | `0x1eb35b80` |
| `0x10` | `SegmentedMinScanF32` | `0x1eb37400` | `0x28` | `SegmentedAddScanS16PartialSumS16` | `0x1eb38200` |
| `0x11` | `SegmentedMaxScanF32` | `0x1eb37780` | `0x29` `*` | `SegmentedAddScanS16PartialSumS32` | `0x1eb38580` |
| `0x12` | `SegmentedMinIndexScanF32` | `0x1eb37b00` | `0x2a` `*` | `SegmentedMinScanU16` | `0x1eb38900` |
| `0x13` | `SegmentedMaxIndexScanF32` | `0x1eb37e80` | `0x2b` `*` | `SegmentedMaxScanU16` | `0x1eb38c80` |
| `0x14` | `SortIntegerAscending` | `0x1eb3ac00` | `0x2c` `*` | `SegmentedMinIndexScanU16` | `0x1eb39000` |
| `0x15` | `SortIntegerDescending` | `0x1eb3afc0` | `0x2d` `*` | `SegmentedMaxIndexScanU16` | `0x1eb39380` |
| `0x16` | `SortFloatAscending` | `0x1eb3b380` | `0x2e` | `SegmentedAddScanBf16PartialSumBf16` | `0x1eb39700` |
| `0x17` | `SortFloatDescending` | `0x1eb3b740` | `0x2f` `*` | `SegmentedAddScanBf16PartialSumF32` | `0x1eb39a80` |
| `0x18` | `DuplicateCountInteger` | `0x1eb3bb00` | `0x30` `*` | `SegmentedMinScanBf16` | `0x1eb39e00` |
| `0x19` | `DuplicateCountFloat` | `0x1eb3be80` | `0x31` `*` | `SegmentedMaxScanBf16` | `0x1eb3a180` |
| `0x1a` | `UniquifyInteger` | `0x1eb3c200` | `0x32` `*` | `SegmentedMinIndexScanBf16` | `0x1eb3a500` |
| `0x1b` | `UniquifyFloat` | `0x1eb3c580` | `0x33` `*` | `SegmentedMaxIndexScanBf16` | `0x1eb3a880` |

**Totals:** 48 encoders, sub-ops `0x04..0x33` contiguous (0 gaps, 0 dups). 32 dispatch-reachable, 16 `*`-unreachable, 4 `‡` reachable ops sharing an F32/U32-sibling encoder.

---

## 4. End-to-end: how a masked VEX scan bundle is built

Putting the three fields together, a single VEX scan op (e.g. `AddScanF32`) is assembled as:

```text
MCInst (VEX scan)
  operand[0]      result placeholder ─────────────────────────► (pure scan: no inline dest port)
  operand[1] ──► GetVectorMask ─► reg id ∈ [0x5f,0x7e] ─ 0x5f ─► proto +0x38 (present +0x11&1)
  operand[2..]──► GetVregno / FindAndEmitToUnusedPort ────────► V0..V6 ports (see VEX operand-port page)
  last operand ─► EmitPredicationToSlot ─────────────────────► whole-op predicate slot
                                  │
  encoder EncodeSparseCoreTecVectorExtendedAddScanF32 @0x1eb32380
        v13[0]=5;  BitCopy(.,0x10f,.,6)   ── sub-opcode 0x05            ──► bit0x10f
        if(+0x10&1) BitCopy(.,0x10c,.,3)  ── dest read-port (absent for pure scan)
        ... V0..V6 source-port BitCopys (0x15a/0x1bb/0x1c7/0x196/0x1a2/0x171/0x17d) ...
        if(+0x11&1) BitCopy(.,0x104,.,5)  ── mask register (M0..M31)     ──► bit0x104
  scan RESULT committed out-of-line by PopXrf (EmitXrfResultOp @0x13a14180), separate bundle slot.
```

For `Sort`, the dest read-port (`bit0x10c`) and a second read-port (`bit0x109`) are present (key + value), and the mask sits at proto `+0x3c` (present `+0x11 & 0x2`). `VresMove` is a separate `VectorResult`-slot op: its dest vreg goes to bundle bit `245` and its source port to bit `235` (not `bit0x10c`/`bit0x109`). The sub-opcode field always carries the encoder's constant; the mask field always carries an M-register selector.

---

## 5. What is not yet pinned

- **The inactive-lane output micro-semantic of the masked scan** (zero-fill vs register-preserve vs no-drive). The mask register *selection* (`M0..M31`) and the masked-scan classification are CONFIRMED; the per-lane write behavior on masked-off lanes is a datapath layer below the encoding — see [M-register predicate](m-register-predicate.md). **INFERRED.**
- **The internal layout of the M-register predicate word** (the exact (lane,sublane) bit packing). The 5-bit selector is CONFIRMED; the stored predicate bit-order is not decoded here.
- **The per-generation reachability of the 16 `*`-marked encoders.** They exist with valid sub-opcodes; their dispatch-reachability in other SparseCore generations was not arm-traced. **INFERRED.**
- **Whether `M16..M31` have any write path.** The read band is 32-deep, the write band (`GetVMDestregno`) is 16-deep; the upper half being read-only predicate inputs is inferred from the band split, not from a write-path absence proof. **INFERRED.**

---

## Cross-References

- [VectorExtended / VEX](vectorextended-vex.md) — the VEX op family, opcode dispatch, and full op roster.
- [VEX operand-port binding](vex-operand-port.md) — the `V0..V6` source read-ports, the 7-entry greedy allocator (`FindAndEmitToUnusedPort`), and the PopXrf write-group.
- [M-register predicate](m-register-predicate.md) — the M-register predicate word and the masked-scan inactive-lane semantics.
- [TEC (Vector) Engine](tec-engine.md) — the 64-byte SparseCore vector bundle that hosts the VEX slot.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the `VectorAlu` opcode roster (the sibling compute-slot recovery).
- [SparseCore Overview](overview.md) — where the TEC/VEX datapath sits in the SparseCore architecture.
