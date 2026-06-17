# Collective / GPSIMD / CustomOp Encoding

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core, with a fixed per-build address delta — e.g. `visitInstCollectiveCompute` cp310 `@0x126c380` → cp311 `@0x126c330`, a constant −0x50). Every encoder, look-up table and field-name string on this page lives in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`). The BIR enums (`bir::CollectiveKind`, `bir::AluOpType`, `klr::DgeComputeOp`) live in `libBIR.so` (`a9b1ea38`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the **cross-engine** wire instructions — the ones that move data between physical cores, signal collective progress, name a worker's rank, fence on a remote completion token, and dispatch a CPU custom op. Eleven opcodes, produced by two generations of code generator:

- **CoreV2 (Sunda, arch 20)** owns the collectives and rank machinery — `CollectiveCompute` (`0xD9` primary + `0xDA` secondary), `CollectiveSend`/`CollectiveRecv` (both `0xCB`), `GetGlobalRankId` (`0xDC`), `GetCurProcessingRankID` (`0xDB`), `TensorCompletion` (`0xDE`), and `CustomOp` (`0x85` header + `0x86` payload). CoreV3/V4 **inherit** these — there is no gen3/gen4 override symbol.
- **CoreV3 (Cayman, arch 30)** adds the gen3-only fixed-function movers/fences — `GetSequenceBounds` (`0xBE`), `CoreBarrier` (`0xD8`), and `GPSIMDSB2SB` (`0xBF`, the Pool-alias cross-core SBUF↔SBUF copy). CoreV4 inherits these.

Every bundle is the same `std::array<unsigned char, 64>`: emplaced, `pxor`-zeroed in full (so any byte the encoder does not write is a hard `0x00`), header-stamped by `setupHeader`, field-filled, ISA-checked, and `fwrite(buf, 1, 0x40, bin)`-ed — the lifecycle [2.1 the bundle](instruction-bundle.md) and [2.10 PE matmul](pe-matmul-encoding.md) describe in full. This page maps only the **instruction-specific** bytes for the eleven cross-engine opcodes; the header (`+0x00..+0x03`) and the shared `SyncInfo` band (`+0x04..+0x0B`) are common to the whole TPB ISA.

The recurring structural finding of this family: **collective *geometry* is not on the wire.** Replica groups, source-target pairs, the symbolic peer rank, and the collective dimension all live in the `bir::Module`'s NEFF metadata side-tables or are folded into an access pattern's axis selection. The 64-byte word carries only the per-leg `{reduce-op, dtype, kind, one access pattern}` and a per-instruction sync band. A reimplementer who tries to find a "replica_groups" field in the bundle will not find one — it is `Module::addReplicaGroupIDs`, not a bundle byte.

The bar for this page: a reader can **byte-encode any collective, the GPSIMD SB2SB mover, or a custom-op invocation by hand**, knowing for each control byte its offset, width, semantic, value source, and the disassembly store-site that pins it. Every field row carries a confidence tag (CONFIRMED = exact store disassembled this pass; STRONG = LUT/predicate/layout cross-checked; INFERRED = zero-init implied; SPECULATIVE). No ordinal is fabricated.

## At a glance

The eleven opcodes and their encoders (all in `libwalrus.so`, VA == file offset):

| Opcode | Instruction | Encoder | Engine generation | Key operand band(s) |
|---|---|---|---|---|
| `0xD9` (217) | `CollectiveCompute` primary | `CoreV2GenImpl::visitInstCollectiveCompute @0x126c380` | V2 (V3/V4 inherit) | ADDR8 `TENSOR2D` @`+0x10` |
| `0xDA` (218) | `CollectiveCompute` secondary | *(same encoder, raw-stamped)* | V2 | ADDR8 (2nd operand) |
| `0xCB` (203) | `CollectiveSend` | `CoreV2GenImpl::visitInstCollectiveSend @0x1272440` | V2 | tensor_id AP @`+0x10` |
| `0xCB` (203) | `CollectiveRecv` | `CoreV2GenImpl::visitInstCollectiveRecv @0x1272ab0` | V2 | tensor_id AP @`+0x10` |
| `0xDC` (220) | `GetGlobalRankId` | `CoreV2GenImpl::visitInstGetGlobalRankId @0x1260ef0` | V2 | *(none — scalar out-reg)* |
| `0xDB` (219) | `GetCurProcessingRankID` | `CoreV2GenImpl::visitInstGetCurProcessingRankID @0x124dec0` | V2 | *(none — group quad + out-reg)* |
| `0xDE` (222) | `TensorCompletion` | `CoreV2GenImpl::visitInstTensorCompletion @0x1219070` | V2 | *(MemLoc completion handle)* |
| `0xBE` (190) | `GetSequenceBounds` | `CoreV3GenImpl::visitInstGetSequenceBounds @0x1355050` | V3 (V4 inherit) | in `TENSOR3D` @`+0x10`, out `TENSOR3D` @`+0x20` |
| `0xD8` (216) | `CoreBarrier` | `CoreV3GenImpl::visitInstCoreBarrier @0x1356550` | V3 (V4 inherit) | semaphore `TENSOR4D` @`+0x20` |
| `0xBF` (191) | `GPSIMDSB2SB` | `CoreV3GenImpl::visitInstGPSIMDSB2SB @0x1359840` | V3 (V4 inherit) | src `TENSOR3D` @`+0x10`, dst `TENSOR3D` @`+0x30` |
| `0x85` (133) | `CustomOpHeader` | `CoreV2GenImpl::visitInstCustomOp @0x12613c0` | V2 | *(metadata; no AP)* |
| `0x86` (134) | `CustomOpPayload` | *(same encoder)* | V2 | ADDR4 AP @`+0x10` (output then N args) |

All encoder addresses are confirmed against `nm -DC libwalrus.so`. The opcodes are the binary's own `_opcode` 256-case table (the `enum_variant_string_opcode` switch), cross-pinned against each encoder's `movb $0x<op>` stamp.

**The shared skeleton (all eleven opcodes):**

```
 byte +0x00        opcode             (low byte of the opcode word)
 byte +0x01        inst_word_len = 0x10  (16 dwords = 64 bytes)
 byte +0x02..+0x03 reserved = 0x0000
 byte +0x04..+0x0B SyncInfo band     (semaphore wait/update + value + branch-hint PC)
```

The header is stamped by `setupHeader` (`@0x1172120`=V2, `@0x1369280`=V3; byte-identical bodies — see [2.10](pe-matmul-encoding.md)). The 16-bit word at `bundle[0:2]` is `(0x10<<8) | opcode`, little-endian. The `SyncInfo` band at `+0x04..+0x0B` is the **same** generic BIR per-instruction sync overlay used by every family in this book: a semaphore-wait selector (wire LUT `07/05/85`), a semaphore-update selector (identity), wait/update values, and a branch-hint PC. It is *not* collective-specific; it is written by a pair of `getSyncInfo` helpers (the "header/first" and "last" twins) shared across all encoders. CONFIRMED — the same `sub_122ed00`/`sub_122ea40`-class twins appear in RankId, TensorCompletion, Collective, GPSIMD, and CustomOp bodies.

> **NOTE — "VA == file offset" holds for `.text` (`0x62d660+`) and `.rodata` (`0x1c72000+`) only.** The `.data` section carries a `+0x400000` VMA≠file-offset delta. Every LUT cited here (`unk_1DF5790` collective-kind, the AluOp jump table) is `.rodata`-resident, so `objdump`/`xxd` at the cited VA reads the right bytes directly.

---

## 1. The two reduce-on-copy fields — CCE-op and DGE-compute-op

Before the per-opcode maps, fix the two "reduce while you copy" selectors the brief asks for. Neither is a standalone opcode; both ride existing instructions, and both trace to the same `bir::AluOpType`.

### 1.1 `_CCE_OP` — the collective-copy compute op (an AluOpType field)

`_CCE_OP` is **not** a distinct enum. It is the `bir::AluOpType` stored at `InstCollectiveCompute+0x180` (the `cc_channel.op` field the KLR→BIR layer set). On the wire it is the byte at `CollectiveCompute` bundle `+0x0C` (§3), produced by `convert<AluOpType> @0x12039c0`. That converter is a 30-case (`cmp edi,0x1d; ja default`) jump over `jpt_12039EF` (base `0x1DF4BB8`); each case is `mov eax, <byte>; ret`. The relevant ordinals (the full `AluOpType` table, recovered from `bir::AluOpType` `to_string` in `libBIR.so @0x400600`):

| AluOpType ordinal | name | wire byte | role on the CCE path |
|---|---|---|---|
| 0 | `bypass` | `0x00` | plain copy (no reduce) — `AllGather`, `AllToAll`, `Permute` |
| 4 | `add` | `0x04` | sum-reduce — `AllReduce`, `ReduceScatter`, `PermuteReduce` |
| 6 | `mult` | `0x06` | product-reduce *(wire-encodable, not in shipped legalization)* |
| 8 | `max` | `0x08` | max-reduce *(wire-encodable, not shipped)* |
| 9 | `min` | `0x09` | min-reduce *(wire-encodable, not shipped)* |

**Shipped subset.** The CCE field width admits the full `AluOpType` space, but the in-build legalization only emits `{bypass=0, add=4}` (`cce_op @rodata 0x1C80730`). The same field surfaces as `InstDMACopy::getCceOp()` on the CCE-DMA path. CONFIRMED — the `+0x0C` store and the `convert<AluOpType>` jump table are both disassembled; the shipped-subset claim is STRONG (rodata legalization table cross-checked).

### 1.2 `_DGE_COMPUTE_OP` — the SW-DGE descriptor reduce op `{1 none, 2 add}`

`_DGE_COMPUTE_OP` (`klr::DgeComputeOp`) is the **descriptor-gen-engine** memcpy reduce op: a DMA descriptor either copies (`none`) or accumulates (`add`) at the destination. It is encoded on the DMA/DGE descriptor path — not in any opcode this page covers — and is documented byte-for-byte in [2.21 DMA encoding](dma-encoding.md). The mapping from AluOp is `backend::AluOpType2DGEComputeOp @0x120b9e0`, whose body is exactly two compares:

```asm
; AluOpType2DGEComputeOp @0x120b9e0  (libwalrus)
120b9fa: test  %esi,%esi                  ; AluOp == bypass(0) ?
120b9fc: je    120bbf0                     ;   → DgeComputeOp::none (1)
120ba02: cmp   $0x4,%esi                   ; AluOp == add(4) ?
120ba05: je    120bbd8                     ;   → DgeComputeOp::add  (2)
                                           ; else → error (only bypass/add legal on DGE)
```

| DgeComputeOp ordinal | name | from AluOp |
|---|---|---|
| 1 | `none` | `bypass` (0) |
| 2 | `add` | `add` (4) |

CONFIRMED — `AluOpType2DGEComputeOp @0x120b9e0` disassembled; the two-branch body proves only `{bypass→none, add→add}` are legal.

> **NOTE — CCE-op and DGE-compute-op are the *same semantic concept in two layers.*** "Reduce-on-copy" expressed (a) on the `InstCollectiveCompute` wire word at `+0x0C` (full `AluOpType`, gated to `{bypass,add}`) and (b) on the DMA descriptor (`{none=1, add=2}`). Both trace to `AluOp::{bypass, add}`. A reimplementer should model them as one knob with two encodings.

---

## 2. The collective-type ordinal table (`bir::CollectiveKind` → wire byte)

The collective *kind* byte at `CollectiveCompute` bundle `+0x0E` is produced by `convert<CollectiveKind> @0x1203770`, which does `sub edi,2; cmp edi,8; movzx eax, byte[unk_1DF5790 + (kind-2)]` — a 9-byte LUT indexed by `(kind − 2)`. The LUT, read directly from `.rodata` at VA `0x1DF5790`:

```
objdump -s 0x1df5790: 01 02 03 04 09 05 06 07 08
                       ↑  ↑  ↑  ↑  ↑  ↑  ↑  ↑  ↑
              kind:    2  3  4  5  6  7  8  9 10
```

The full `bir::CollectiveKind` enum (member names from `CollectiveKind` `to_string` in `libBIR.so @0x4016c0`) joined to the wire byte:

| Kind ordinal | name | wire byte | wire-encodable here? | Conf |
|---|---|---|---|---|
| 0 | `SendRecv` | — | **No** — uses opcode `0xCB` (§4) | CONFIRMED |
| 1 | `SendRecvCCE` | — | **No** — uses opcode `0xCB` | CONFIRMED |
| 2 | `AllReduce` | `0x01` | yes (`0xD9` kind byte) | CONFIRMED |
| 3 | `ReduceScatter` | `0x02` | yes | CONFIRMED |
| 4 | `AllGather` | `0x03` | yes | CONFIRMED |
| 5 | `AllToAll` | `0x04` | yes | CONFIRMED |
| 6 | `AllToAllV` | `0x09` | yes **(out of sequence)** | CONFIRMED |
| 7 | `Permute` | `0x05` | yes | CONFIRMED |
| 8 | `PermuteReduce` | `0x06` | yes | CONFIRMED |
| 9 | `PermuteImplicit` | `0x07` | yes | CONFIRMED |
| 10 | `PermuteReduceImplicit` | `0x08` | yes | CONFIRMED |

> **QUIRK — `AllToAllV` (kind 6) maps to wire `0x09`, out of sequence.** Every other kind is a `−1` shift from its ordinal (`kind 2 → 0x01`, `kind 3 → 0x02`, …). `AllToAllV` breaks the pattern: it jumps to `0x09`, and the `Permute` family (`0x05..0x08`) slides down to fill the contiguous block `kind 7..10 → 0x05..0x08`. This is a deliberate reorder that groups the permute opcodes contiguously on the wire. A naive `wire = kind − 1` reimplementation will mis-encode `AllToAllV` and the entire permute family. Use the LUT verbatim. CONFIRMED — the 9-byte LUT `01 02 03 04 09 05 06 07 08` is `xxd`-verified at `0x1DF5790` and matches `movzbl (%rax,%rdi,1),%eax` at `0x1203795`.

> **GOTCHA — `SendRecv(0)` / `SendRecvCCE(1)` are NOT wire-encodable through this LUT.** They sit *below* the LUT base (which starts at `kind 2`); the `convert<CollectiveKind>` `sub edi,2` would underflow. These two kinds reach the wire through the **dedicated** `SendRecv` opcode `0xCB` (§4) and the `Sb2sbCollective` opcode `0xBF` (§5), not through `CollectiveCompute`. The KLR layer mints local send/recv swaps as `CollectiveKind=SendRecv(0), is_local=true`, which the `lower_local_collectives` pass ([Part 8 — walrus passes](../walrus/) §H23, planned) either drops onto `0xCB` or routes to the GPSIMD `0xBF` mover under the size ceiling.

---

## 3. CollectiveCompute encoder — `0xD9` primary + `0xDA` secondary

`CoreV2GenImpl::visitInstCollectiveCompute @0x126c380` (~0x4230 bytes — the largest visitor in this family) is the wire serialiser for the `InstCollectiveCompute` node. It is a multi-arm dispatcher on `CollectiveKind` (`I+0xF8`) plus a set of CCOp-shape predicates (`is2SrcCCOp`, `isSBArgGlobalCCOp`, `isCoarseGrainCCOp`, `collectiveKindAcceptsALUOp`). Every arm emits the same `0xD9` data-record layout; they differ in how many records (1 vs 2) and whether replica-groups vs source-target-pairs are registered module-side.

### 3.1 The `0xD9` primary data record — byte-stable across all arms

Field offsets are identical at all three verified arms (`0x126ce80..`, `0x126d7b0..`, `0x126e280..`); the bundle register varies by arm (`%rcx` vs `%r13`) but the layout does not.

| off | Sz | field | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xD9` (217) | `setupHeader` (template `movb $0xd9,-0x110` @`0x126ce50`) | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | shared sync overlay | STRONG |
| `0x0C` | 1 | **CCE / reduce-op** wire byte | `convert<AluOpType>(I+0x180) @0x12039c0` → `mov %al,0xc(%rcx)` @`0x126cee2` | CONFIRMED |
| `0x0D` | 1 | **dtype** wire byte | `getReinterpretedCopyDtype(I.dtype)` → dtype→wire LUT → `mov %al,0xd(%rcx)` @`0x126cecc` | CONFIRMED |
| `0x0E` | 1 | **collective-kind** wire byte | `convert<CollectiveKind>(I+0xF8) @0x1203770` (LUT `unk_1DF5790`, §2) → `mov %al,0xe(%rcx)` @`0x126cefb` | CONFIRMED |
| `0x0F` | 1 | reserved = `0` | `movb $0x0,0xf(%rcx)` @`0x126cefe` | CONFIRMED |
| `0x10` | 48 | **SRC ADDR8 `TENSOR2D`** descriptor | `assignAddr8Tensor2D(buf+0x10, srcAP) @0x1260cc0` (the 64-bit base + 2-D AP; ADDR8 family) | CONFIRMED |

One `0xD9` record fully describes a single collective leg: `{reduce-op, dtype, kind, one ADDR8 TENSOR2D tile}`. The three byte-stores `mov %al,0xc/0xd/0xe(%rcx)` and `movb $0x0,0xf(%rcx)` are disassembled verbatim at `0x126cecc..0x126cefe`.

```c
/* CollectiveCompute 0xD9 primary record (annotated C, names binary-derived) */
uint8_t bundle[64] = {0};                         /* pxor-zeroed in full         */
setupHeader(bundle, /*opcode=*/0xD9);             /* [0]=0xD9 [1]=0x10 [2..3]=0   */
write_syncinfo(bundle + 0x04, inst->sync);        /* +0x04..+0x0B shared band     */
bundle[0x0C] = convert_AluOpType(inst->cc_op);    /* +0x180 → wire (bypass=0/add=4)*/
bundle[0x0D] = dtype_to_wire(getReinterpretedCopyDtype(inst->dtype));
bundle[0x0E] = collkind_lut[inst->kind - 2];      /* unk_1DF5790, kind∈{2..10}    */
bundle[0x0F] = 0;
assignAddr8Tensor2D(&bundle[0x10], inst->srcAP);  /* +0x10: 64-bit ADDR8 TENSOR2D */
fwrite(bundle, 1, 0x40, bin);
```

### 3.2 The `0xDA` secondary record

Emitted for 2-source CCOps (`is2SrcCCOp` true) and for the source-target-pair leg of a permute. Crucially, the `0xDA` opcode byte is **stamped raw** — `movb $0xDA,(%rax)` directly at `+0x00` (`0x126d02a` and `0x126e579`), *not* through `setupHeader` — onto a freshly emplaced-and-zeroed 64-byte record. The `0xDA` record carries the second operand's ADDR8 AP plus the same kind/op/dtype trio in the arms where present. It is the "additional leg" of a multi-operand collective (the recv-side tile of a permute, or the 2nd source of `PermuteReduce`).

| off | Sz | field | source | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xDA` (218) | `movb $0xDA,(%rax)` @`0x126d02a` (raw stamp, not template) | CONFIRMED |
| `0x04` | 2 | `instr.channel_id` (u16) — **permute arms only** | §3.4 | CONFIRMED |
| `0x0C..0x0F` | 4 | same `{CCE-op, dtype, kind, 0}` trio | mirrors §3.1 | STRONG |
| `0x10` | 48 | 2nd-operand ADDR8 AP | `assignAddr8Tensor2D` | STRONG |

CONFIRMED for the opcode and the raw-stamp emit; the exact `0xDA` body field offsets mirror the `0xD9` trio (STRONG — same arms, same packer).

### 3.3 Replica-groups / source-target-pairs → module side-table, not the wire word

The literal replica-group geometry and the permute source-target pairs are **not** serialised into the 64-byte bundle. They are registered into the `bir::Module`'s NEFF collective-metadata side-tables:

- `bir::Module::addReplicaGroupIDs(I+0x100)` — called at `0x126dc6c` / `0x126dc88` / `0x126e59e` / `0x126eb90`.
- `bir::Module::addSrcTargetPairIDs(...)` — called at `0x126d0e1` / `0x126d539`.

The return id is **discarded** at these call sites — the groups go into the Module's global replica-group registry, and the runtime collective engine reads *that* registry, not the per-instruction word. CONFIRMED — `addReplicaGroupIDs` reads `I+0x100` (the `replica_groups` vector) but stores Module-side; both helpers are imported (`U`) symbols whose call sites and argument register are disassembled.

> **GOTCHA — there is no `replica_groups`, `neff_collective_has_offset`, or peer-rank field in the bundle.** Reimplementers must build the Module-side replica-group / source-target-pair registry separately and serialise the per-leg `{kind, op, dtype, AP}` only. The collective topology is NEFF metadata; the wire word is per-leg data.

### 3.4 `channel_id` is a permute-only wire field

The permute / 2-source CCOp arms additionally wire-encode `instr.channel_id` (uint16, from `I+0x160`) into the `0xDA` record at bundle `+0x04`, via a uint16 writer at `0x126d237` (`lea 0x4(%rax),%rdi; call 0x123ca60`) and again at `0x126d685`; the field-name string `"instr.channel_id"` is verbatim `.rodata`. This is the collective stream/channel the permute leg uses. The group-collective arms (`AllReduce`/`ReduceScatter`/`AllGather`/`AllToAll`) do **not** wire a `channel_id` — their replica-group geometry is module-side (§3.3). So `channel_id` is a **permute-family wire field**, not a universal collective field. CONFIRMED — `lea 0x4(%rax),%rdi` → uint16-writer call disassembled at `0x126d237`.

### 3.5 `cc_dim` (`I+0x27C`) — validated, folded into the AP

`I+0x27C` (the `CollectiveDimension` set from `concatDim ∈ {0,1}` = Partition/Free) is read (`mov 0x27c(%r15),%edi` at `0x126e128` etc.) and passed to a validator that asserts `dim ∈ {0,1}` (`cmp $0x0` / `cmp $0x1`) and `reportError`s otherwise. The dim is **not** stored as its own bundle byte — it selects *which axis* (partition vs free) the `assignAddr8Tensor2D` descriptor at `+0x10` addresses, i.e. it is folded into the AP geometry. CONFIRMED — the only consumer of `+0x27C` is the `{0,1}` validator plus the AP axis selection; no standalone dim byte exists in the word.

### 3.6 Arch

CoreV2-only; V3/V4 inherit. There is no `CoreV3`/`CoreV4` `visitInstCollectiveCompute` symbol (`nm` sweep). The collective-compute wire is arch-common across Sunda/Cayman/Mariana. CONFIRMED.

---

## 4. CollectiveSend / CollectiveRecv — both opcode `0xCB`

`CoreV2GenImpl::visitInstCollectiveSend @0x1272440` and `CoreV2GenImpl::visitInstCollectiveRecv @0x1272ab0` **both emit opcode `0xCB`** (203 = `PseudoSendRecv`). The send-vs-recv direction is **not** in the opcode — it is carried by a `Module::setAttribute(kind=2, value)` call (`@0x1272728`, the send/recv peer-pairing NEFF metadata) and by which operand (src for Send / dst for Recv) the AP describes.

### 4.1 Wire bundle (Send; Recv is the mirror — same opcode/layout, dst AP)

| off | Sz | field | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xCB` (203) | `setupHeader` (`movb $0xcb,-0xd1` @`0x127285c`) | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | RegisterMove-region sync overlay | STRONG |
| `0x10` | 8 | operand ADDR8 base / **`instr.tensor_id`** | `mov %rax,0x10(%r13)` @`0x12726aa` (AP base; field-name string `"instr.tensor_id"`) | CONFIRMED |
| `0x30` | 8 | tensor base addr (`I+0xD0`) | `mov %rax,0x30(%r13)` @`0x1272709` | CONFIRMED |
| `0x38` | 8 | `numElementsPerPartition`-derived | `getNumElementsPerPartition(AP)` → `mov %r15,0x38(%r13)` @`0x12726e5` | CONFIRMED |

**Peer / channel** is registered module-side via `Module::setAttribute(kind=2, value) @0x1272728` (the same NEFF send/recv-pairing metadata the runtime collective library reads). The wire word carries `tensor_id` + AP; the symbolic peer rank is module-attribute metadata, **not** a wire byte. The AP-shape gate `"Hardware Restriction: DMA or Collective…"` (`@0x1d6c6d8`) validates the shape.

```c
/* CollectiveSend / CollectiveRecv 0xCB bundle */
uint8_t bundle[64] = {0};
setupHeader(bundle, /*opcode=*/0xCB);
write_syncinfo(bundle + 0x04, inst->sync);
*(uint64_t*)&bundle[0x10] = ap_base(inst->arg0_AP);     /* instr.tensor_id        */
*(uint64_t*)&bundle[0x30] = inst->tensor_base;          /* I+0xD0                 */
*(uint64_t*)&bundle[0x38] = getNumElementsPerPartition(inst->arg0_AP);
module_setAttribute(module, /*kind=*/2, peer_pair);     /* direction + peer rank  */
fwrite(bundle, 1, 0x40, bin);
```

CONFIRMED — both encoders disassembled: each stamps `movb $0xcb`, the three `0x10/0x30/0x38(%r13)` stores are byte-pinned, and `setAttribute(kind=2)` is the peer carrier. Send and Recv are a **unified `0xCB` micro-bundle**: opcode + sync + one tensor_id + one ADDR8 AP; direction and peer are module-attribute metadata. (See [Part 13 — Distribution / Collectives](../distribution/), planned, for the runtime side.)

---

## 5. GPSIMDSB2SB encoder — `0xBF` (S3D3_COLLECTIVE), the Pool-alias mover

`CoreV3GenImpl::visitInstGPSIMDSB2SB @0x1359840` (CoreV3-only; V4 inherits; no CoreV2 form). The cross-core SBUF↔SBUF mover. This page's encoding **agrees exactly** with [1.13 GPSIMD Engine](../arch/gpsimd-engine.md): `InstGPSIMDSB2SB`, InstructionType 33, opcode `0xBF` (191), a 64-byte `S3D3_COLLECTIVE` wire bundle, sole encoder `@0x1359840`, LNC2 hard-coding at `arch+0x1A4`, no core-index/core-count field. The byte-map below was re-pinned against fresh disassembly this pass.

| off | Sz | field | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xBF` (191) | `setupHeader` (CoreV3); template `movb $0xbf,-0x2a0` @`0x1359a15` | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | CoreV3 sync twins (`sub_13594c0`+`sub_1359230`) @`0x1359a25`/`a30` | CONFIRMED |
| `0x0C` | 1 | **SRC dtype** wire byte | `dtype→wire(srcAP.dtype)` → `mov %al,0xc(%r13)` @`0x1359a3d` | CONFIRMED |
| `0x0D` | 1 | **DST dtype** wire byte | `dtype→wire(dstAP.dtype)` → `mov %al,0xd(%r13)` @`0x1359a54` | CONFIRMED |
| `0x10` | 16 | **SRC `TENSOR3D`** descriptor (local) | `Generator::assignAccess<TENSOR3D>(buf+0x10, srcAP)`; `lea 0x10(%r13),%rsi` @`0x1359a4d` | CONFIRMED |
| `0x20` | 1 | **cross-core enable = 1** (LNC2) | `movb $0x1,0x20(%r13)` @`0x1359a74` (constant, post-gate) | CONFIRMED |
| `0x21` | 1 | **peer-core SB partition addr** | `*(*(srcAP+0x50)+8)` (2nd APPair elem) → `mov %al,0x21(%r13)` @`0x1359a9f` | CONFIRMED |
| `0x30` | 16 | **DST `TENSOR3D`** descriptor (peer) | `Generator::assignAccess<TENSOR3D>(buf+0x30, dstAP)`; `lea 0x30(%r13),%rsi` @`0x1359a94` | CONFIRMED |

**LNC2 gate.** `cmpl $0x2,0x1A4(arch)` at `0x1359a67` (GENERATE) — the per-arch `cores-per-LNC` field (the same `lnc_size` field [1.13](../arch/gpsimd-engine.md) and [1.07](../arch/lnc-memory-model.md) cite). The entry verify also reads `arch+0x1A4` (`mov 0x1a4(%rax),%esi` @`0x1359870`). A mismatch raises an assert. There is **no core-index or core-count field** in the word — the LNC2 topology is structural, and the peer is named only by the single scalar partition address at `+0x21`.

```c
/* GPSIMDSB2SB 0xBF bundle (S3D3_COLLECTIVE) */
assert(arch->cores_per_lnc == 2);                  /* arch+0x1A4 == 2 (LNC2 only)  */
uint8_t bundle[64] = {0};
setupHeader(bundle, /*opcode=*/0xBF);
write_syncinfo(bundle + 0x04, inst->sync);
bundle[0x0C] = dtype_to_wire(inst->srcAP.dtype);
bundle[0x0D] = dtype_to_wire(inst->dstAP.dtype);
assignAccess_TENSOR3D(&bundle[0x10], inst->srcAP); /* local read AP                */
bundle[0x20] = 1;                                  /* cross-core enable            */
bundle[0x21] = inst->srcAP.appair[1].partition;    /* peer SB partition (scalar)   */
assignAccess_TENSOR3D(&bundle[0x30], inst->dstAP); /* peer write AP                */
fwrite(bundle, 1, 0x40, bin);
```

CONFIRMED — every store re-disassembled this pass; the map is byte-identical to [1.13](../arch/gpsimd-engine.md). The AP packer is the generic `Generator::assignAccess<NEURON_ISA_TPB_TENSOR3D>` (`@0x133dc60` V2 / `@0x1425850` V3) — the **same** one `GetSequenceBounds` (§6) uses; not a bespoke per-op packer.

> **NOTE — consistency with [1.13](../arch/gpsimd-engine.md) holds in full.** Opcode `0xBF`, the `S3D3_COLLECTIVE` wire class, the encoder address, the `arch+0x1A4 == 2` gate, and the "no core-index field" finding all match. The size ceiling that decides GPSIMD-vs-DMA (`≤1024 bytes/partition`, `MaxBytesPerPartition @0x784a00 = 0x400`) lives in `libBIR`, not this encoder — it is a `lower_local_collectives` routing decision, not a bundle byte.

---

## 6. GetSequenceBounds encoder — `0xBE`

`CoreV3GenImpl::visitInstGetSequenceBounds @0x1355050` (CoreV3-only, gen3+; V4 inherits). The wire serialiser for the segment-bounds node: it scans a per-token segment-id tensor and writes per-segment start/end bounds to a destination SBUF tile (ragged / packed-attention sequence segmentation — *not* a loop trip-count).

| off | Sz | field | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xBE` (190) | `setupHeader` (CoreV3); `movb $0xbe,-0xb1` @`0x13551ab` | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | CoreV3 sync overlay | STRONG |
| `0x0C` | 1 | input(segmentIds) AP-status / flag byte | `assignAccess<TENSOR3D>` `%al` return → `mov %al,0xc(%r12)` @`0x13551fd` | CONFIRMED |
| `0x0E` | 1 | input(segmentIds) **dtype** wire byte | `dtype→wire(inAP.dtype)` → `mov %al,0xe(%r12)` @`0x135520f` | CONFIRMED |
| `0x0F` | 1 | output(dst/bounds) **dtype** wire byte | `dtype→wire(outAP.dtype)` → `mov %al,0xf(%r12)` @`0x135523d` | CONFIRMED |
| `0x10` | 16 | **INPUT segmentIds `TENSOR3D`** AP | `assignAccess<TENSOR3D>(buf+0x10, inAP)`; `lea 0x10(%r12),%rsi` @`0x13551d5` | CONFIRMED |
| `0x20` | 16 | **OUTPUT bounds `TENSOR3D`** AP | `assignAccess<TENSOR3D>(buf+0x20, outAP)`; `lea 0x20(%r12),%rsi` @`0x135521b` | CONFIRMED |

GetSequenceBounds is a 2-tensor bundle: input `segmentIds` `TENSOR3D` @`+0x10` + output `bounds` `TENSOR3D` @`+0x20`, with three flag/dtype bytes at `+0x0C/+0x0E/+0x0F`. CONFIRMED — both `lea` AP stores and all three flag/dtype stores disassembled; the dtype converter is the same one GPSIMDSB2SB (§5) uses.

---

## 7. CoreBarrier encoder — `0xD8`

`CoreV3GenImpl::visitInstCoreBarrier @0x1356550` (CoreV3-only, gen3+; V4 inherits). The cross-core rendezvous fence. The **hardware** barrier-register id is allocated *here at emit time* by `CoreV3GenImpl::getNextCoreBarrierId() @0x1346150`.

| off | Sz | field | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xD8` (216) | `setupHeader` (CoreV3); `movb $0xd8,-0x60` @`0x135669f` | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | CoreV3 sync overlay | STRONG |
| `0x0C` | 4 | **HW barrier-register id** (u32) | `getNextCoreBarrierId() @0x1346150` → `mov %eax,0xc(%r12)` @`0x13566e7` | CONFIRMED |
| `0x0D` | 1 | barrier data-AP dtype byte | `assignAccess<TENSOR1D>` `%al` (arg AP) | CONFIRMED |
| `0x0F` | 1 | barrier data-AP dtype byte (2) | `assignAccess<TENSOR1D>` `%al` (out AP) | CONFIRMED |
| `0x10` | 4 | **barrier INSTANCE / module index** | renumbered barrier index → `mov %eax,0x10(%r12)` @`0x13566d6` | CONFIRMED |
| `0x20` | 16 | **semaphore `TENSOR4D`** AP (RMW) | `assignAccess<TENSOR4D>(buf+0x20, sem AP)` (`@0x133e710`) | CONFIRMED |

> **NOTE — the barrier carries TWO ids.** The per-emit **HW barrier-register id** at `+0x0C` (allocated by `getNextCoreBarrierId` at SASS-emit time) and the module-wide **renumbered barrier instance index** at `+0x10` (assigned earlier by a barrier-renumber pass). The active-core list is consumed by `getNextCoreBarrierId`'s allocation, not stored raw in the word. The semaphore is a `TENSOR4D` AP at `+0x20` — and it is *both* the arg and the out (a read-modify-write over the SBUF barrier semaphore tile). CONFIRMED — two distinct id stores (`mov %eax,0xc/0x10(%r12)`) and the `TENSOR4D` semaphore AP disassembled.

---

## 8. Rank-id encoders — GetGlobalRankId (`0xDC`) & GetCurProcessingRankID (`0xDB`)

Both produce a scalar rank into a sequencer register. But they are **structurally distinct**: `GetGlobalRankId` is a bare `{valid, regId}` emit, while `GetCurProcessingRankID` wire-encodes a full group-geometry quad. Do not conflate them.

### 8.1 GetGlobalRankId — `0xDC`, bare `{valid@+0xC, regId@+0x17}`

| off | Sz | field | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xDC` (220) | `setupHeader` (`movb $0xdc,-0xc0` @`0x126104a`) | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | shared sync overlay | STRONG |
| `0x0C` | 1 | output-register **VALID flag** = `1` | `movb $0x1,0xc(%r13)` @`0x1261094` | CONFIRMED |
| `0x17` | 1 | **OUTPUT register id** (u8) | `getOutput(I,0)→RegisterAccess→getRegId` → `mov %al,0x17(%r13)` @`0x12610a8` | CONFIRMED |

**Guard.** The output tag must `== 0x0B` (`cmp $0xb,%eax` @`0x126108b`) — the output must be a `RegisterAccess` (StorageBase kind 11); else `reportError`. GetGlobalRankId writes **one** byte of payload (the dst register id at `+0x17`) plus the valid flag at `+0x0C`. The absolute worker rank value is computed at runtime; the wire only names where it lands. CONFIRMED.

### 8.2 GetCurProcessingRankID — `0xDB`, the rich group quad

Unlike GetGlobalRankId, `GetCurProcessingRankID` wire-encodes the full collective-group coordinates, each written via a bounded named-field writer (the name strings are verbatim `.rodata`).

| off | Sz | field (wire name) | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xDB` (219) | `setupHeader` (`movb $0xdb,-0x1d2` @`0x124e03e`) | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | shared sync overlay | STRONG |
| `0x0C` | 1 | `instr.group_id` (u8) | `[I+0x138]`; `lea 0xc(%r13),%rdi` @`0x124e0b0` | CONFIRMED |
| `0x0E` | 1 | `instr.channel_id` (u8) | `[I+0x118]`; `lea 0xe(%r13),%rdi` @`0x124e0e2` | CONFIRMED |
| `0x10` | 1 | `instr.iteration_id` (u8) | `[I+0xF0]`; `lea 0x10(%r13),%rdi` @`0x124e11a` | CONFIRMED |
| `0x12` | 1 | **OUTPUT register id** (u8) | `getOutput<RegisterAccess>(I,0)→getRegId` → `mov %bl,0x12(%r13)` @`0x124e204` | CONFIRMED |
| `0x14` | 2 | `instr.stream_id` (u16) | `[I+0x140]`; `lea 0x14(%r13),%rdi` @`0x124e15c`, masked `and $0xffff` | CONFIRMED |

> **CORRECTION — GetCurProcessingRankID is NOT a bare regId emit.** It wire-encodes the full group-geometry quad `{group_id@+0x0C, channel_id@+0x0E, iteration_id@+0x10, stream_id@+0x14 (u16)}` plus the output regId at `+0x12`. The hardware/runtime uses these to resolve the TP rank *within the current processing group* (keyed by channel/iteration). This differs from `GetGlobalRankId`, which emits only `{valid@+0xC, regId@+0x17}` — the absolute worker rank needs no group geometry. **The two ops have distinct ISA structs and distinct regId offsets (`0x12` vs `0x17`).** CONFIRMED — the four named-field bounded writes (`lea 0xc/0xe/0x10/0x14(%r13)`) and the `mov %bl,0x12(%r13)` regId store are byte-pinned; the field-name strings `instr.group_id/channel_id/iteration_id/stream_id` are all present in `.rodata`.

---

## 9. TensorCompletion encoder — `0xDE`

`CoreV2GenImpl::visitInstTensorCompletion @0x1219070`. The point-to-point completion-token instruction — the `-PTCOM` completion fence placed after a cross-core SB2SB. It binds one argument tensor (the buffer whose completion is signalled) and emits its `MemoryLocation` completion id plus a derived AP byte.

| off | Sz | field | source (encoder) | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0xDE` (222) | `setupHeader` (`movb $0xde,-0x110` @`0x12195b3`) | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | EventSemaphore-region sync overlay | STRONG |
| `0x0C` | 4 | **completion target / MemLoc id** (u32) | `getArgument<PhysAP>(I,0)→location→[MemLoc+0x234]` → `mov %eax,0xc(%r13)` @`0x121972f` | CONFIRMED |
| `0x10` | 1 | AP/dtype-derived completion byte | derived from the same AP (`call *(%rax)`, `%al` result) → `mov %al,0x10(%r13)` @`0x1219749` | CONFIRMED |

`[MemLoc+0x234]` is the per-tensor completion/handle id the runtime matches the PTCOM token against (`mov 0x234(%rax),%eax` appears twice — once into staging, once into bundle `+0x0C`). The instruction carries no opcode-specific enum; it is a pure sync/handshake bundle keyed on one tensor's completion id. CONFIRMED structure; the exact `MemLoc+0x234` = "completion handle" semantic is STRONG (cross-referenced from the SB2SB completion path).

---

## 10. CustomOp encoder — `0x85` header + `0x86` payload

`CoreV2GenImpl::visitInstCustomOp @0x12613c0` (body extends to `0x12633e0`) emits a **two-opcode** pair: a `0x85` `CustomOpHeader` followed by one or more `0x86` `CustomOpPayload` records. The full custom-op wire byte-layout is owned by [Part 11 — Custom Ops](../customop/) (planned); this section stays consistent with it and adds the field-name joins.

| off | Sz | field | source | Conf |
|---|---|---|---|---|
| `0x00` | 1 | opcode = `0x85` (133) header / `0x86` (134) payload | `movl $0x85,-0x200` @`0x12623d7`; `movl $0x86,-0x200` @`0x126243c` | CONFIRMED |
| `0x01` | 1 | `inst_word_len` = `0x10` | `setupHeader` | CONFIRMED |
| `0x02` | 2 | reserved = `0` | `setupHeader` | CONFIRMED |
| `0x04` | 8 | `SyncInfo` band | shared sync overlay | CONFIRMED |
| `0x0C` (header) | 2 | `instr.num_payloads` (u16) = N+2 | field string `"instr.num_payloads"` (`.rodata`) | STRONG |
| `0x0E` (header) | 1 | `FunctionId` | (the resolved custom-op function id) | STRONG |
| `0x0F` (header) | 1 | `instr.num_arguments` (u8) = N | field string `"instr.num_arguments"` (`.rodata`) | STRONG |
| `0x10` (header) | — | reserved = `0` + scratch metadata zone | `custom_op_header_reserved_zero` (pybind) | CONFIRMED |
| `0x0F` (payload) | 1 | payload-present flag = `1` | (D-Q08) | STRONG |
| `0x10` (payload) | — | ADDR4 AP (output, then N args) | ADDR4 access pattern | STRONG |

The opcode bytes are confirmed: `movl $0x85` (header) and `movl $0x86` (payload) both disassembled at `0x12623d7`/`0x126243c` and re-stamped at several arms. The `num_payloads` / `num_arguments` field-name strings are verbatim `.rodata` (`"instr.num_payloads"`, `"instr.num_arguments"`, both present in the binary).

> **NOTE — the `custom_op_header_scratch_space_val` field.** The CUSTOM_OP_HEADER pybind struct names a header field `custom_op_header_scratch_space_val` that does not surface as a distinct store in the byte walk — it occupies the header metadata region (the `[0x10..]` reserved zone), and is the per-custom-op scratch SBUF/DRAM budget the Xtensa CPU op may request. **In this build's encoder it is left at its zero default** on the GENERATE path (no non-zero store observed; the whole region is zero-filled). The pybind exposes it for round-trip; the compiler emits `0`. A non-zero `scratch_space_val` would require a custom op that declares scratch — not exercised on this path. STRONG (pybind name confirmed; zero-default emit matches the byte walk; the exact byte offset within `[0x10..0x3F]` is pybind-implied, not independently store-pinned).

> **GOTCHA — the Xtensa custom-op CPU cluster is NOT the Pool-alias "GPSIMD" of §5.** The `libbuiltincustomop_cpu0..7.stripped.so` libraries (Xtensa, `clang 10.0.1`) are a programmable general-purpose processor array for user custom ops, dispatched by this `0x85`/`0x86` pair. They have **nothing to do with `InstGPSIMDSB2SB` or the `Pool(1)` engine.** [1.13](../arch/gpsimd-engine.md) documents the three-way "GPSIMD" name collision in full; this page's `0xBF` op is the Pool alias, and this section's `0x85`/`0x86` pair is the Xtensa cluster dispatch.

---

## 11. Consolidated opcode → 64-byte-bundle offset matrix

| opcode | instruction | key control-band fields (off:name) | AP @ |
|---|---|---|---|
| `0xD9` | CollectiveCompute (primary) | `0xC`:CCE/reduce-op(AluOp) `0xD`:dtype `0xE`:kind(LUT) `0xF`:0 | `0x10` ADDR8 `TENSOR2D` |
| `0xDA` | CollectiveCompute (secondary) | `0x4`:channel_id(u16, permute) + 2nd-leg trio; groups → Module | ADDR8 (2nd operand) |
| `0xCB` | CollectiveSend/Recv | `0x10`:tensor_id `0x30`:base `0x38`:elems; peer → Module attr | `0x10` ADDR8 (`instr.tensor_id`) |
| `0xDC` | GetGlobalRankId | `0xC`:valid=1 `0x17`:out-regId | *(none)* |
| `0xDB` | GetCurProcessingRankID | `0xC`:group_id `0xE`:channel_id `0x10`:iteration_id `0x12`:regId `0x14`:stream_id(u16) | *(none)* |
| `0xDE` | TensorCompletion | `0xC`:completion-id(u32, MemLoc+0x234) `0x10`:AP-byte | *(MemLoc handle)* |
| `0xBE` | GetSequenceBounds | `0xC`:in-AP-flag `0xE`:in-dtype `0xF`:out-dtype | `0x10` in `TENSOR3D` / `0x20` out `TENSOR3D` |
| `0xD8` | CoreBarrier | `0xC`:HW-barrier-regId(u32) `0x10`:barrier-instance-idx `0xD`/`0xF`:dtype | `0x20` sema `TENSOR4D` |
| `0xBF` | GPSIMDSB2SB | `0xC`:src-dtype `0xD`:dst-dtype `0x20`:xcore-enable=1 `0x21`:peer-SB | `0x10` src `TENSOR3D` / `0x30` dst `TENSOR3D` |
| `0x85` | CustomOpHeader | `0xC`:num_payloads(u16=N+2) `0xE`:FnId `0xF`:num_args(u8=N) `0x10`:0 | *(none; metadata)* |
| `0x86` | CustomOpPayload | `0xF`:present=1 | `0x10` ADDR4 (output then N args) |

All: `[0]`=opcode `[1]`=`0x10` `[2..3]`=0 (setupHeader); `[4..0xB]`=shared BIR `SyncInfo` band.

---

## Confidence ledger

- **CONFIRMED** (byte-store + opcode + field disassembly this pass): all eleven opcodes; the `0xD9` primary trio `{CCE-op@+0xC, dtype@+0xD, kind@+0xE}` + ADDR8 `TENSOR2D`@`+0x10`; the `0xDA` raw stamp; the collective-kind LUT bytes (`01 02 03 04 09 05 06 07 08`, `xxd`-verified at `0x1DF5790`); the GetCurProc group quad + regId@`+0x12`; GetGlobalRankId `{valid@+0xC, regId@+0x17}` + the `cmp $0xb` guard; TensorCompletion `{handle@+0xC, byte@+0x10}`; GetSequenceBounds `{flags@+0xC/+0xE/+0xF, in TENSOR3D@+0x10, out TENSOR3D@+0x20}`; CoreBarrier `{barrierId@+0xC, idx@+0x10, sema TENSOR4D@+0x20}`; the **GPSIMDSB2SB full map**, re-verified byte-identical to [1.13](../arch/gpsimd-engine.md); Send/Recv `0xCB` + tensor_id + `setAttribute(2)`; the `channel_id@+0x4` permute field; CustomOp `0x85`/`0x86`; module-side `addReplicaGroupIDs`/`addSrcTargetPairIDs`; the `AluOpType2DGEComputeOp` two-branch body.
- **STRONG**: the `0xDA` secondary-record field detail (mirrors the `0xD9` trio); the `SyncInfo` band sub-layout (shared skeleton, not re-decoded here); `custom_op_header_scratch_space_val` byte offset (pybind-implied, zero-emitted); TensorCompletion `MemLoc+0x234` handle semantics; CustomOp `num_payloads`/`FunctionId`/`num_arguments` field semantics (cross-referenced to Part 11).
- **INFERRED / DEFER**: the 48-byte ADDR4/ADDR8 AP internal packing ([2.2 ADDR8](tensor-descriptors.md), [2.21 DMA](dma-encoding.md)); the runtime completion-handle and scratch-window semantics (Part 11 / Part 13).

The binary is authoritative for every offset cited. No SPECULATIVE claims.

## Cross-References

- [1.13 GPSIMD Engine — the Pool-Alias Cross-Core SB2SB Mover](../arch/gpsimd-engine.md) — owns the `0xBF` opcode, the `S3D3_COLLECTIVE` wire class, InstructionType 33, the LNC2 `arch+0x1A4 == 2` gate, the `≤1024 B/partition` routing ceiling, and the three-way "GPSIMD" name collision. **§5 here agrees byte-for-byte.**
- [2.1 The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the `pxor`-zero / `setupHeader` / `fwrite(0x40)` lifecycle.
- [2.10 PE Matmul Encoding](pe-matmul-encoding.md) — sibling encoding page; the `setupHeader` body and bundle-lifecycle detail.
- [2.21 DMA Encoding](dma-encoding.md) — the DGE sibling: where `_DGE_COMPUTE_OP {none=1, add=2}` (§1.2) is encoded on the descriptor, and the ADDR8 / sync skeleton.
- [2.2 TENSOR Descriptors](tensor-descriptors.md), [2.3 ADDR4](addr4.md) — the `TENSOR2D`/`TENSOR3D`/`TENSOR4D` and ADDR4/ADDR8 access-pattern internals these encoders pack.
- [Part 8 — Walrus Passes](../walrus/) *(planned)* — `lower_local_collectives` (H23): how a local SendRecv becomes either `0xCB` or the `0xBF` GPSIMD mover.
- [Part 11 — Custom Ops](../customop/) *(planned)* — the full CUSTOM_OP `0x85`/`0x86` wire byte-layout and the Xtensa CPU cluster.
- [Part 13 — Distribution / Collectives](../distribution/) *(planned)* — the runtime collective engine, replica-group registry, and the NEFF send/recv-pairing metadata these encoders register.
- [Build & Version Provenance](../reference/versions.md) — the pinned build and the cp310/11/12 parity argument.
