# JF/DF BarnaCore Address-Handler Bundle

> *Every address, bit offset, mask, and enum value on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The ELF is **not** stripped; full C++ symbols are present. `.text` and `.rodata` VMAs equal their file offsets (`.text` `0xe63c000`, `.rodata` `0x84a0000`), so proto descriptor bytes are read directly. All addresses are analysis VMAs. Other versions will differ.*

## Abstract

The **BarnaCore Address Handler (BCAH)** is the Jellyfish/Dragonfish embedding-address personality — `tpu::TpuSequencerType` = `BarnaCoreAddressHandler` = 2 (the C++ enum `TpuSequencerTypeToString` `@0x20b362e0` indexes: TC=0, BCS=1, BCAH=2, SCS=3, TAC=4, TEC=5; the proto wire enum `TpuSequencerTypeProto` carries an extra `INVALID`=0, so its values are +1), the v3-era counterpart to the full BarnaCore Sequencer (BCS = 1). Its instruction word is a **23-byte / 184-bit VLIW bundle** filled by **direct struct-field writes**: each per-slot encoder does a read-modify-write of the bundle's qwords/dword/word/byte with hard-coded `shl N; and MASK; or` constants. This is a *fundamentally different* mechanism from the Pufferfish BCS path, which packs every field through a shared `BitCopy` primitive against an absolute-bit `Span` (see [BCS 32-Byte Bundle](bcs-32byte-bundle.md)). The two encodings reach the same embedding datapath family across silicon generations by independent means; the JF/DF direct-write codec pre-dates the BCS `BitCopy` codec.

The bundle is a **3-way VLIW**: a `Common` header (iteration state + operand selectors + two immediates), a `ScalarSlot` (control lane), and a `VectorSlot` (a 5-wide vector datapath). All three regions are live simultaneously at dedicated, non-overlapping bit positions; only the sub-forms *within* `ScalarSlot` (`Loop`/`ShiftMask`/`Push`/`Branch`/`prog_end`) and *within* `VectorSlot` (`Store`/`Load`/`Alu0`/`Alu1`/`Result`) are mutually exclusive. One bundle therefore expresses, in a single cycle: advance the feature-length loop, branch or end the program, run two vector-ALU ops, store the previous embedding row, load the next, drain an EUP result, apply per-operand index overrides, and carry two 16-bit literals.

This page documents three artifacts a reimplementer must reproduce: (1) the **23-byte bundle bit-layout** — the absolute bit map of `Common`/`ScalarSlot`/`VectorSlot`, the direct-struct-write mechanism, and the 32-byte-aligned 23-byte DMA stride; (2) the **5-bit BCS predication field value law** and the 16-value `BarnaCorePredication.Condition` enum; and (3) the **immediate-operand model** — two 16-bit `ImmSlot`s allocated by `YIsImm`. The Pufferfish 32-byte BCS Sequencer / Channel bundle, the scalar ISA roster, and the merged vector-ALU opcode space are owned by sibling pages (see [Cross-References](#cross-references)).

For reimplementation, the contract is:

- **The encoding is direct struct write, not `BitCopy`.** Each slot encoder receives the same 23-byte `BarnaCoreAddressHandlerBundleBits*` and writes its fields into qword0 (`+0`) / qword1 (`+8`) / dword (`+16`) / word (`+20`) / byte (`+22`) with literal shift/mask constants. A reimplementer who models BCAH on the BCS `BitCopy` Span will mis-build every field.
- **23 bytes per bundle, 32-byte aligned.** The DMA buffer is `(3*N)*8 - N = 23*N` bytes, `posix_memalign`-ed to 32; each bundle occupies 23 contiguous bytes.
- **Absent slots are encoded as `kNeverExecute` (0x1f), not as a distinct opcode.** The dispatcher pre-fills all six scalar/vector slot predication fields with `kNeverExecute` before any slot is encoded; a present slot overwrites its own field. Slot presence is a predication question.
- **The 5-bit predication field = `condition[3:0] | (~value)<<4`.** `Always` = 0x0f (`kAlwaysExecute`), `Never` = 0x1f (`kNeverExecute`). The condition is the embedding iteration-boundary predicate alphabet (16 values, one alias).
- **JF and DF differ only in the scalar-slot superset.** The Dragonfish scalar-slot encoder calls the Jellyfish helper, then adds `Branch` + `prog_end`. Same 23-byte frame, same vector slots.

| | |
|---|---|
| **Personality** | BCAH — BarnaCore Address Handler; `tpu::TpuSequencerType` = 2 (C++ enum: TC=0, BCS=1, BCAH=2) (Jellyfish / Dragonfish) |
| **Bundle size** | **23 bytes / 184 bits**, direct struct write; DMA stride `23*N`, 32-byte aligned |
| **Bit packer** | none — per-slot read-modify-write (`shl N; and MASK; or`) of the 23-byte struct |
| **Top encoders** | `tpu::EncodeBarnaCoreAddressHandler<EncoderJf,…>` `@0x1e841640`; `<EncoderDf,…>` `@0x1e836ea0` |
| **Bundle dispatcher** | `EncoderJf::EncodeBarnaCoreAddressHandlerBundle` `@0x1e86fd80` |
| **Struct image** | qword0 `+0` (bits 0..63) · qword1 `+8` (64..127) · dword `+16` (128..159) · word `+20` (160..175) · byte `+22` (176..183) |
| **Predication law** | `pred5 = condition + ((16*value) ^ 0x10)` = `condition[3:0] \| (~value)<<4`; default 0x0f |
| **Immediate model** | 2 × 16-bit `ImmSlot` (`imm_0`/`imm_1`) allocated by `YIsImm` `@0x14169c20` |
| **Confidence** | CONFIRMED (decompiled shift/mask extraction + 23-byte stride proof + proto string decode) unless a row says otherwise |

> **GOTCHA — the BCAH struct is 23 bytes, even though `BundleSizeBytes` reports 16 for the BarnaCore component.** The Jellyfish codec-metadata sizer `JellyfishCodecMetadata::BundleSizeBytes` `@0x1ecf7460` returns **16** for its BarnaCore component (component 0 = TensorCore → 41; component 1 = BarnaCore → 16; both DMA and HBM, `BundleSizeBytesForHbm` `@0x1ecf74c0` agrees), and the [Overview](overview.md) personality table and sibling-page navigation carry that "16 B" figure. The byte-exact BCAH *encoder* evidence is larger: the top encoder allocates `23*N`, advances the pointer by **23** per bundle, and the highest written bit is 180 (`imm_1`), so the `BarnaCoreAddressHandlerBundleBits` struct image is **23 bytes / 184 bits**. The 16-byte codec figure matches the `xmm0` (qword0+qword1) low half the top encoder stores first; that store is followed by separate dword/word/byte stores carrying the struct to 23 bytes. Treat 23 as authoritative for the address-handler struct layout; the "16 B" label is the codec-metadata BarnaCore-component size used as a coarse personality tag on the enum-presence pages. [Confidence: CONFIRMED — `23*N` stride + per-slot bit offsets reaching 180; the 16 is `BundleSizeBytes`'s BarnaCore-component return.]

---

## 1. The Bundle Encoding Model

### Purpose

The BCAH bundle is a flat 23-byte struct (`BarnaCoreAddressHandlerBundleBits`) whose fields are written *in place* by each per-slot encoder. There is no shared bit-copy primitive and no slot-relative rebasing: every encoder receives the same struct pointer and writes its field at a hard-coded shift into a hard-coded qword/dword/word/byte member. Understanding this mechanism is the prerequisite for reading every offset table below — the offsets are recovered from the `<<N` shift and `& MASK` constant before each store, not from a `BitCopy` immediate.

### Entry Point

```text
tpu::EncodeBarnaCoreAddressHandler<EncoderJf,…> (0x1e841640)   ── DMA buffer = 23*N, posix_memalign(.,0x20,.)
  └─ (per bundle) EncoderJf::EncodeBarnaCoreAddressHandlerBundle (0x1e86fd80)
       ├─ prefill 6 slot preds with kNeverExecute (@6/48/79/110/126/141)
       ├─ write Common header fields (compared_feature_id, indexed_*, vs0/vs1/vs2, branch_target_pc…)
       ├─ ScalarSlot encoder           (vtable +152)   — Loop/ShiftMask/Push (+ DF Branch/prog_end)
       ├─ EncodeBarnaCoreAddressHandlerVectorStore  (0x1e86e020)
       ├─ EncodeBarnaCoreAddressHandlerVectorLoad   (0x1e86e5c0)
       ├─ EncodeBarnaCoreAddressHandlerVectorAlu(0)  (0x1e86f5c0)  — lane 0
       ├─ EncodeBarnaCoreAddressHandlerVectorAlu(1)  (0x1e86f5c0)  — lane 1
       └─ EncodeBarnaCoreAddressHandlerVectorResult (0x1e86eb40)
```

The Dragonfish path is identical in shape (`tpu::EncodeBarnaCoreAddressHandler<EncoderDf,…>` `@0x1e836ea0`, same `23*N` stride); only the scalar-slot encoder differs (§5).

### Algorithm

```c
// Models tpu::EncodeBarnaCoreAddressHandler<EncoderJf,…> @0x1e841640.
// a2 + 32 = num_bundles (int32).
function EncodeAddressHandler(program):
    n   = program.num_bundles                       // *(int*)(a2+32)
    buf = posix_memalign(align=0x20, size=23*n)      // (3*n)*8 - n = 23*n ; 32-byte aligned
    p   = buf
    for i in 0 .. n-1:
        bits = EncodeBarnaCoreAddressHandlerBundle(program.bundle[i])   // 0x1e86fd80 → 23-byte struct
        // store the 23-byte struct image, member by member:
        store_xmm(p,        bits.qword0_qword1)      // bytes 0..15  (vmovups)
        store_dword(p+16,   bits.dword)              // bytes 16..19
        store_word(p+20,    bits.word)               // bytes 20..21
        store_byte(p+22,    bits.byte)               // byte  22
        p += 23                                      // <-- the per-bundle stride is 23
    return TpuTypedHostDmaBuffer<unsigned char, 32>{ buf, 23*n }

// Models a per-slot field write (no BitCopy; direct read-modify-write):
function WriteField(struct_member, value, shift, clear_mask):
    struct_member = (value << shift) | (struct_member & clear_mask)
```

> **QUIRK —** the top encoder stores the struct as a 16-byte `vmovups xmm0` (qword0+qword1) *plus* three scalar stores (dword@16, word@20, byte@22), then advances the pointer by **23**. The 16-byte SIMD store covers only bits 0..127; the dword/word/byte stores carry bits 128..183. The DMA buffer is 32-byte *aligned* (so the SIMD store is legal), but the per-bundle *stride* is 23, not 16 or 32 — confirmed by `_R14 += 23` in the encoder loop and the `23*N` allocation size. [Confidence: CONFIRMED.]

> **NOTE —** the address-handler encode path appends **no** check byte. The framing-byte machinery of the TensorCore (`tpu::TpuSequencerType` = 0, 41-byte HardwareBundleBits) is not used as the BCAH DMA payload; the 23 raw struct bytes are the entire payload per bundle. (The per-lane vector-ALU body *does* call `EncoderJf::EncodeBundleInternal` to build the 41-byte TensorCore encoding, then extracts and merges its instruction bits into qword1 — see §2 — but no check byte reaches the BCAH buffer.) The codec sizer `JellyfishCodecMetadata::BundleSizeBytes` `@0x1ecf7460` reports 41 for its component-0 (TensorCore) / 16 for component-1 (BarnaCore), but the address-handler top encoder writes 23 bytes directly.

### Function Map

| Function | Address | Role |
|---|---|---|
| `tpu::EncodeBarnaCoreAddressHandler<EncoderJf,…>` | `0x1e841640` | top encoder; `23*N` DMA buffer, 32-byte aligned |
| `tpu::EncodeBarnaCoreAddressHandler<EncoderDf,…>` | `0x1e836ea0` | DF top encoder; identical `23*N` stride |
| `tpu::EncodeBarnaCoreAddressHandlerBundle<EncoderJf>` | `0x1e837980` | `std::vector<uint8>` single-bundle form |
| `EncoderJf::EncodeBarnaCoreAddressHandlerBundle` | `0x1e86fd80` | per-bundle dispatcher; kNeverExecute prefill + Common + slot calls |
| `ApplyEupResultTargetWorkaround` | `0x1e8478a0` | only known reader; reuses `23*N` stride; patches result region |

---

## 2. The 23-Byte Bundle Bit-Layout

### Purpose

The bundle is `BarnaCoreAddressHandlerBundle` proto = `{ common (f1), scalar (f2), vector (f3) }`. The C++ struct image holds the three sub-messages packed at **absolute** bundle-bit positions; each per-slot encoder writes at a hard-coded offset. Unlike a real proto `oneof` (which stores a case number), the encoder uses per-field has-bits and a dedicated, non-overlapping bit region per slot — so scalar, vector, and common can all be live at once. The bundle is a true VLIW.

### Encoding

The struct stack image is five members: **qword0** (`+0`, bits 0..63), **qword1** (`+8`, bits 64..127), **dword** (`+16`, bits 128..159), **word** (`+20`, bits 160..175), **byte** (`+22`, bits 176..183). Bits 181..183 are reserved. The dispatcher pre-fills the six slot predication fields with `kNeverExecute` (§3), writes the `Common` header into qword0/dword/word/byte, then calls each slot encoder.

```text
BarnaCoreAddressHandlerBundle (23 B / 184 bits; absolute bundle bits)

byte:  0        8        16       20   22
       +--------+--------+--------+----+-+
       | qword0 | qword1 | dword  |word|b|
       | 0..63  | 64..127| 128..159|...|.|
       +--------+--------+--------+----+-+

 ScalarSlot   ┌ Loop      bits   0..5
 (control)    │ ShiftMask pred   6..10
 Common       ┤ compared_feature_id 13..17
 (header)     │ indexed_*  (6 bools) 18..23
              │ vs0/vs1/vs2 (2b ea)  24..29
 ScalarSlot   │ Branch pred (DF)     30..34
 (control)    │ branch_type          36
              │ branch_target_pc     37..43
              └ prog_end (DF)        44
 VectorSlot   ┌ Alu0 pred           48..52
 (datapath)   │ Alu1 pred           79..83
              │ Store slot         110..125   (TABLE B)
              │ Load slot          126..140   (TABLE C)
              └ Result slot        141..146   (TABLE D)
 Common       ┌ imm_0 (16b)        149..164
 (literals)   └ imm_1 (16b)        165..180
                                    [181..183 reserved]
```

#### TABLE A — top-level bit map

| Region | Bits | Source field | Role |
|---|---|---|---|
| `ScalarSlot.Loop` | 0..5 | `Loop.loop_size_minus_one` (5b @1) | loop trip-count (size − 1) |
| `ShiftMask` pred | 6..10 | `ScalarSlot.ShiftMask.predication` | 5-bit BCS pred (§3) |
| `compared_feature_id` | 13..17 | `Common.compared_feature_id` (5b) | operand for `COMPARE_FEATURE_ID` |
| `indexed_*` | 18..23 | `Common.indexed_{load_dst,store_src,alu0_x,alu0_dst,alu1_x,alu1_dst}` | 6 per-operand index overrides |
| `vs0/vs1/vs2` | 24..29 | `Common.vs0/vs1/vs2` (2b each) | scalar-reg selectors (§4 TABLE G) |
| `Branch` pred | 30..34 | `ScalarSlot.Branch.predication` | 5-bit BCS pred (DF only) |
| `branch_type` | 36 | `ScalarSlot.Branch.branch_type` (1b) | branch kind (DF) |
| `branch_target_pc` | 37..43 | `ScalarSlot.Branch.branch_target_pc` (7b) | branch target PC (DF) |
| `prog_end` | 44 | `ScalarSlot.prog_end` (1b) | program-end marker (DF) |
| `Alu0` pred | 48..52 | `VectorSlot.alu_0.predication` | 5-bit BCS pred, lane 0 |
| `Alu1` pred | 79..83 | `VectorSlot.alu_1.predication` | 5-bit BCS pred, lane 1 |
| `Store` slot | 110..125 | `VectorSlot.store` (TABLE B) | embedding-row store |
| `Load` slot | 126..140 | `VectorSlot.load` (TABLE C) | embedding-row load |
| `Result` slot | 141..146 | `VectorSlot.result` (TABLE D) | EUP-result drain |
| `imm_0` | 149..164 | `Common.imm_0` (f11, 16b) | 16-bit literal slot 0 (`<<21` into dword, gated by has-bit `0x400`) |
| `imm_1` | 165..180 | `Common.imm_1` (f12, 16b) | 16-bit literal slot 1 (`<<37` into dword, gated by has-bit `0x800`) |

The `Common` header fields above are recovered from the dispatcher `@0x1e86fd80`: `compared_feature_id` at `v7 |= v12<<13`; the six `indexed_*` bools at `<<18/19/20/22/21/23`; `vs0/vs1/vs2` at `<<24/26/28`; `imm_0` as a 16-bit field `<<21` into the dword (bundle bit 149, has-bit `0x400`) and `imm_1` `<<37` into the dword (bundle bit 165, has-bit `0x800`). The DF `branch_target_pc` is written `<<37` into **qword0** (bundle bit 37) by the DF scalar-slot encoder `@0x1e85e8a0` — a distinct Branch sub-form field that happens to share the literal shift 37 but lands in qword0, not the dword `imm_1`.

> **NOTE — the VectorAlu instruction body is the standard JF vector-ALU ISA, merged into qword1.** `EncodeBarnaCoreAddressHandlerVectorAlu(lane,…)` `@0x1e86f5c0` builds a temporary `isa::Bundle`, calls `EncoderJf::EncodeBundleInternal` (the regular JF vector-ALU encoder), then extracts the instruction bits and merges them into qword1 with mask `0xffffc000000fffff` (shifts `0x29`/`0x20`/`0x10`). So the *opcode/operand space* of an address-handler ALU op equals the main JF vector-ALU ISA; only the lane predication (lane-0 has-bit `0x4` → pred @48; lane-1 has-bit `0x8` → pred @79) is address-handler-specific. The per-field landing positions of the merged opcode/Vx/YSrc bits inside qword1 are a separate JF-ISA decode (see [Cross-References](#cross-references)). [Confidence: HIGH for the merge mechanism; the merged-field positions are not isolated here.]

---

#### TABLE B — `VectorSlot.Store` (`EncodeBarnaCoreAddressHandlerVectorStore` `@0x1e86e020`)

Struct base in `r15`; all stores target qword1 (`+8`, base bit 64), so bundle bit = 64 + shift.

| Field | Bundle bit | Width | Proto field | Shift (on qword1) |
|---|---|---|---|---|
| `predication` | 110 | 5 | `Store.predication` | `<<46` |
| `use_loop_index` | 115 | 1 | `Store.use_loop_index` (f5) | `<<51` |
| `source` (`VectorRegister`) | 116 | 5 | `Store.source` (f7) | `<<52` |
| `base` (`BaseAddressEncoding`) | 121 | 2 | `Store.base` (f3) | `<<57` |
| `feature_length_multiplier` | 123 | 2 | `Store.feature_length_multiplier` (f4) | `<<59` |
| `push_to_concat_register` | 125 | 1 | `Store.push_to_concat_register` (f6) | `<<61` |

Store extent: bits 110..125 (16 bits). Has-bit test `~hasword & 0x22` requires `source` + one more field; the `base` and `feature_length_multiplier` widths are confirmed by the `< 4` range checks before their stores.

#### TABLE C — `VectorSlot.Load` (`EncodeBarnaCoreAddressHandlerVectorLoad` `@0x1e86e5c0`)

The Load predication straddles the qword1/dword boundary; the remaining fields live in the dword (`+16`) window.

| Field | Bundle bit | Width | Proto field | Placement |
|---|---|---|---|---|
| `predication` | 126 | 5 | `Load.predication` | bits 126,127 (qword1) + 128..130 (dword) |
| `use_loop_index` | 131 | 1 | `Load.use_loop_index` (f5) | dword window `<<3` |
| `destination` (`VectorRegister`) | 132 | 5 | `Load.destination` (f6) | dword window `<<4` |
| `base` (`BaseAddressEncoding`) | 137 | 2 | `Load.base` (f3) | dword window `<<9` |
| `feature_length_multiplier` | 139 | 2 | `Load.feature_length_multiplier` (f4) | dword window `<<11` |

Load extent: bits 126..140 (15 bits). Has-bit test `~hasword & 0x12`.

#### TABLE D — `VectorSlot.Result` (`EncodeBarnaCoreAddressHandlerVectorResult` `@0x1e86eb40`)

| Field | Bundle bit | Width | Proto field | Notes |
|---|---|---|---|---|
| `predication` | 141 | 5 | `Result.predication` | dword window `<<13` |
| result-valid | 146 | 1 | (slot-present) | dword bit 18 (= byte18 bit 2) |
| `which_destination` | (sub-form) | — | `Result.which_destination` (f2) | `cmp 1/2` inside `@0x1e86eb40` selects EUP target form |
| `destination` (`VectorRegister`) | (in merge) | 5 | `Result.destination` (f4) | EUP result target vreg |

Bit 146 (dword bit 18) is the EUP-result-present bit that `ApplyEupResultTargetWorkaround` `@0x1e8478a0` re-targets on Jellyfish silicon (it tests the result region — dword bits 13..17 = `Result.predication`, dword bits 19..20 = the 2-bit result-target it patches).

> **NOTE — Store/Load `base` and `Result.which_destination` enum *values* are not decoded here.** `BaseAddressEncoding` (the 2-bit `< 4`-checked `base` field) and `VectorResultDestination` (`which_destination`) are shared JF-ISA enums, not address-handler-specific; the *field bit positions* above are exact, but the value→name rosters were not byte-decoded. [Confidence: HIGH on positions, LOW on the value tables — out of scope here.]

---

## 3. Predication — the 5-Bit Field and the Condition Enum

### Purpose

Every slot carries a 5-bit predication field that gates whether the slot fires on each loop iteration. The field is a single packed byte: a 4-bit condition selector plus a 1-bit polarity. Because absent slots are pre-filled with `Never` (§1), the predication field is *also* the slot-presence mechanism — there is no separate "slot empty" opcode.

### Encoding

`EncoderJf::EncodeBarnaCorePredication<T>` has six identical instantiations (Branch `@0x1e85ea00`, Store `@0x1e86e3a0`, Load `@0x1e86e920`, Result `@0x1e86f020`, ShiftMask `@0x1e86f4a0`, AluInstruction `@0x1e86fc60`). Each computes:

```c
// Models EncodeBarnaCorePredication<…> @0x1e85ea00.
// pred is a BarnaCorePredication proto: [+24] = condition (int32), [+28] = value (bool).
function EncodePredication(pred):
    if not pred.has_predication():
        return 0x0f                                   // kAlwaysExecute (unset default)
    if not (pred.has_condition() and pred.has_value()):
        return error("Message's predication is invalid")
    // byte-exact: pred5 = condition + ((16*value) ^ 0x10)
    return pred.condition + ((16 * pred.value) ^ 0x10)
    //  value=1  -> (16 ^ 0x10) = 0     -> pred5 = condition           (0x00..0x0f)
    //  value=0  -> ( 0 ^ 0x10) = 0x10  -> pred5 = 0x10 | condition    (0x10..0x1f)
```

Equivalently `pred5 = condition[3:0] | (~value)<<4`: bit 4 is `NOT(value)`, bits 3..0 are the condition (0..15). The polarity bit inverts the condition test — every condition has an "execute-if" (0x00..0x0f) and an "execute-unless" (0x10..0x1f) encoding.

The two constructors pin the boundary values. `MakeBarnaCorePredication(cond, value)` `@0x14169b20` writes `[+24] = cond`, `[+28] = value`, `[+16] |= 3` (both has-bits). `Always()` = `Make(ALWAYS=15, value=1)` → `15 + 0` = **0x0f** (= `kAlwaysExecute`, `.rodata` `@0xb834cf8`). `Never()` = `Make(ALWAYS=15, value=0)` → `15 + 0x10` = **0x1f** (= `kNeverExecute`, `.rodata` `@0xb834cfc`).

> **NOTE — the dispatcher pre-fills six slot preds with `kNeverExecute` before any slot is encoded.** `EncoderJf::EncodeBarnaCoreAddressHandlerBundle` `@0x1e86fd80` masks `kNeverExecute` to 5 bits and replicates it into the six slot predication fields: bit 6 (ShiftMask), bit 48 (Alu0), bit 79 (Alu1), bit 110 (Store), bit 126 (Load), bit 141 (Result) — verified from the prefill stores (`kNeverExecute<<6`, `<<48`, and into qword1 `<<15`/`<<46`/`<<62` = bits 79/110/126, and into dword `<<13` = bit 141). A present slot's encoder overwrites its own field. The `Branch` pred (bit 30) has *no* pre-fill — it is written only when the Branch sub-form is present. This is byte-level proof that an absent BarnaCore slot is a `Never` no-op, not a distinct opcode. [Confidence: CONFIRMED.]

#### TABLE E — `BarnaCorePredication.Condition` (4-bit; `EnumDescriptorProto` `@0xc017b98`)

| Value | Name |
|---|---|
| 0 | `FIRST_ID` |
| 1 | `FIRST_ID_IN_FEATURE` |
| 2 | `NEW_FEATURE_ID` |
| 3 | `NEW_TOKEN_ID` |
| 4 | `NEW_SAMPLE` |
| 5 | `LAST_ID_IN_BATCH` |
| 5 | `ONLY_ID_IN_FEATURE_SAMPLE` *(alias of value 5)* |
| 6 | `FIRST_ID_IN_BATCH` |
| 7 | `NEW_TILE` |
| 8 | `COMPARE_FEATURE_ID` *(compares against `Common.compared_feature_id` @13..17)* |
| 9 | `REPEATED_TOKEN_FEATURE` |
| 10 | `FIRST_ITERATION` |
| 11 | `LAST_ITERATION` |
| 12 | `NEW_SAMPLE_OR_TILE_FOR_THE_SAME_ID` |
| 13 | `REPEATED_TILE_SAMPLE` |
| 14 | `NEW_FEATURE_OR_TOKEN_FOR_THE_SAME_ID` |
| 15 | `ALWAYS` |

Seventeen names over sixteen values: `ONLY_ID_IN_FEATURE_SAMPLE` aliases `LAST_ID_IN_BATCH` (both value 5), proving `allow_alias`. `ALWAYS` = 15 is the all-ones default. The set is the embedding-table iteration-boundary predicate alphabet — the hardware-evaluated "feature-row / token / sample / tile / iteration boundary" conditions that gate which slots fire on each loop iteration of an embedding gather/scatter handler.

> **GOTCHA — do not confuse this with `BARNACORE_CHANNEL_PREDICATION_*`.** The binary also carries a parallel Pufferfish-Channel predication enum (`BARNACORE_CHANNEL_PREDICATION_FIRST_ID`, `…NOT_NEW_SAMPLE`, etc.) with an explicit polarity baked into the *name* (`NOT_*` variants). That is the BCS Channel personality's enum, not the JF/DF address-handler `BarnaCorePredication.Condition` decoded here — the latter expresses polarity through the 1-bit `value` field, not the name. [Confidence: CONFIRMED — both enums are distinct symbols in the binary.]

---

## 4. Operands — Immediates and Scalar-Register Selectors

### Purpose

An ALU/store/load operand either names a scalar register, names the loop-advanced index, or names an immediate literal. The `Common` header carries the literal slots and the scalar-register selectors; the per-operand `indexed_*` bools choose whether an operand's register index is the loop-advanced BarnaCore-ID or a direct register.

### Encoding — the immediate model

The `Common` header has exactly two immediate fields: `imm_0` (f11, uint32) at bundle bits 149..164, `imm_1` (f12, uint32) at bits 165..180 — both 16-bit literals. An operand that references an immediate (rather than a scalar register) is routed through `ProtoUtils::GetConstants<VectorAluYEncoding>` (the Y-operand selector map, keyed by the operand-selector value) → `YIsImm(u32 v, ImmSlot* slot)` `@0x14169c20`, which allocates or reuses an `ImmSlot`.

```c
// Models YIsImm @0x14169c20 (simplified).
// ImmSlot layout: [+0] half-A (u16), [+2] half-B (u16), [+4] in-use flag, [+5] dual/32-bit flag.
function YIsImm(v, slot):
    sel = GetConstants<VectorAluYEncoding>().lookup(v)    // operand-selector value -> Y encoding
    if sel found: return sel                               // operand is a register/selector, not an imm
    if (v >> 16) != 0:                                     // 32-bit literal: needs both halves of one slot
        if slot.dual_flag != 1 and slot.in_use == 0:
            slot.half_B = HIWORD(v); slot.flags = 0x0101; slot.half_A = v
            return 26                                      // dual-slot Y-encoding
        else: error("All IMM slots are occupied.")
    else:                                                  // 16-bit literal: one half of one slot
        h = LOWORD(v)
        allocate h into a free half (in_use / dual_flag bookkeeping)
        return 8 or 9 (or the zero-literal selector)       // single 16-bit Y-encoding
```

`YIsImm` verifies the literal fits the slot model: a 16-bit value takes one slot half; a value with a non-zero high word takes both halves of one slot (the `dual` flag). Two 16-bit immediates can co-exist (one per slot); the "All IMM slots are occupied" error fires when a third distinct literal is requested. This is the JF/DF analog of the Pufferfish BCS 4×16-bit immediate region — narrower (2 slots) because the address-handler bundle is 23 bytes vs the BCS bundle's 32.

#### TABLE F — immediate slots

| Field | Bundle bits | Width | Proto field | Allocator |
|---|---|---|---|---|
| `imm_0` | 149..164 | 16 | `Common.imm_0` (f11, uint32) | `YIsImm` `@0x14169c20` |
| `imm_1` | 165..180 | 16 | `Common.imm_1` (f12, uint32) | `YIsImm` `@0x14169c20` |

> **NOTE —** the encoder writes the full 16-bit literal into each slot (`YIsImm` enforces a 16-bit fit). The two literals occupy bits 149..180, entirely within the 184-bit struct. Whether the *hardware* immediate field is the full 16 bits or a narrower sub-field is INFERRED — the encode side writes 16; no decode-side reader was found to cross-validate the consumed width. [Confidence: CONFIRMED the struct holds 16 bits per slot; INFERRED the hardware reads all 16.]

### Encoding — the scalar-register selectors

`Common.vs0/vs1/vs2` (fields 8/9/10, 2 bits each, bundle bits 24..29) select among four physical scalar source registers the address-handler datapath reads.

#### TABLE G — `BarnaCoreAddressHandlerScalarRegister` (2-bit; `EnumDescriptorProto` `@0xc018caf`)

| Value | Name | Role |
|---|---|---|
| 0 | `BARNA_CORE_ID_VMEM_ADDRESS` | embedding-row VMEM base (gather/scatter dest/src pointer) |
| 1 | `GRADIENT_VMEM_ADDRESS` | gradient VMEM base (backward-pass scatter source) |
| 2 | `BARNA_CORE_ID_WEIGHT` | weight-id register |
| 3 | `BARNA_CORE_ID_ARGUMENTS` | arguments pointer (descriptor / metadata base) |

The six `indexed_*` bools (`Common` fields 2..7, bundle bits 18..23: `indexed_load_destination`, `indexed_store_source`, `indexed_alu_0_x`, `indexed_alu_0_destination`, `indexed_alu_1_x`, `indexed_alu_1_destination`) select, per vector operand, whether its register index is the loop-advanced BarnaCore-ID index (`indexed = true`) or a direct register. `compared_feature_id` (f1, 5-bit, bits 13..17) is the operand to the `COMPARE_FEATURE_ID` predicate condition (§3 TABLE E value 8).

---

## 5. JF vs DF — the Scalar-Slot Superset

### Purpose

Jellyfish and Dragonfish emit the *same* 23-byte frame and the *same* vector slots; they differ only in how much of the scalar control lane each exposes. Dragonfish is the richer surface.

### Encoding

`EncoderJf::EncodeBarnaCoreAddressHandlerScalarSlot` `@0x1e86f2c0` is a `jmp` to the helper `…ScalarSlotHelper` `@0x1e86f2e0`, which writes `Loop` (bits 0..5), the `ShiftMask` predication (bit 6), and the `Push` has-bit. `EncoderDf::EncodeBarnaCoreAddressHandlerScalarSlot` `@0x1e85e8a0` first **calls** the JF helper (inheriting `Loop`/`ShiftMask`/`Push`), then adds the Dragonfish-only sub-forms: `Branch` predication (bit 30), `branch_type` (bit 36), `branch_target_pc` (bits 37..43), and `prog_end` (bit 44).

So the Dragonfish address-handler bundle is a **superset** of the Jellyfish one: it adds a control-flow branch + a program-end marker in the scalar lane that Jellyfish does not expose through its standalone helper. The frame width (23 bytes), the `Common` header, and the five vector slots are identical between the two.

| Field | Bundle bit | JF | DF | Encoder |
|---|---|:---:|:---:|---|
| `Loop.loop_size_minus_one` | 0..5 | Y | Y | `…ScalarSlotHelper` `@0x1e86f2e0` |
| `ShiftMask.predication` | 6..10 | Y | Y | `…ScalarSlotHelper` `@0x1e86f2e0` |
| `Push` (has-bit) | — | Y | Y | `…ScalarSlotHelper` `@0x1e86f2e0` |
| `Branch.predication` | 30..34 | – | Y | `EncoderDf::…ScalarSlot` `@0x1e85e8a0` |
| `Branch.branch_type` | 36 | – | Y | `EncoderDf::…ScalarSlot` `@0x1e85e8a0` |
| `Branch.branch_target_pc` | 37..43 | – | Y | `EncoderDf::…ScalarSlot` `@0x1e85e8a0` |
| `prog_end` | 44 | – | Y | `EncoderDf::…ScalarSlot` `@0x1e85e8a0` |

---

## What We Do Not Yet Have

1. **`VectorResultDestination` (`Result.which_destination`, f2) value table** — the field bit position is exact (the `cmp 1/2` sub-form selector inside the Result encoder `@0x1e86eb40`; result-valid bit @146; the 2-bit result-target at dword bits 19..20 that `ApplyEupResultTargetWorkaround` patches), but the value→name roster is not decoded.
2. **`BaseAddressEncoding` (`Store.base`/`Load.base`, 2-bit) value table** — a shared JF-ISA enum (`< 4`-checked); the value→name roster is not address-handler-specific and was not decoded here.
3. **The merged `VectorAluInstruction` opcode/operand bit-layout inside qword1** — `EncodeBarnaCoreAddressHandlerVectorAlu` delegates to `EncoderJf::EncodeBundleInternal` and merges via mask `0xffffc000000fffff` + shifts `0x29`/`0x20`/`0x10`; the per-field opcode/Vx/YSrc landing positions of the merged form were not isolated (the address-handler-specific predication + lane are exact; the opcode body is the JF ISA, a separate decode).
4. **The decode-side inverse** — no symmetric `BarnaCoreAddressHandler*Decoder` was found in this build; the encode-side 184-bit map is authoritative but not cross-validated by an independent reader (`ApplyEupResultTargetWorkaround` reads only the result region).
5. **The hardware width of `imm_0`/`imm_1`** — the encoder writes the full 16 bits per slot, but whether the hardware consumes all 16 or a narrower sub-field is INFERRED.

---

## Related Components

| Name | Relationship |
|---|---|
| `BarnaCoreAddressHandlerBundleBits` | the 23-byte struct each slot encoder writes in place |
| `EncoderJf` / `EncoderDf` (`jellyfish::isa`) | the JF/DF encoder leaves; DF scalar slot is the JF superset |
| `MakeBarnaCorePredication` / `Always` / `Never` | the predication constructors pinning 0x0f / 0x1f |
| `YIsImm` / `ProtoUtils::GetConstants<VectorAluYEncoding>` | the immediate-operand allocator + Y-operand selector map |
| `ApplyEupResultTargetWorkaround` | the only known reader; reuses the `23*N` stride; patches the Result region on Jellyfish |
| `HardwareBundleBits` | holds `kAlwaysExecute=0x0f` (`@0xb834cf8`), `kNeverExecute=0x1f` (`@0xb834cfc`), `kPredicateRegisterCount=0x0f` (`@0xb834cf4`) |

## Cross-References

- [Overview](overview.md) — BarnaCore, the legacy embedding accelerator; the BCS / BCAH personality split and the C++ `tpu::TpuSequencerType` enum (TC=0, BCS=1, BCAH=2; note the coarse "16 B" codec-component label there — see the GOTCHA above for the byte-exact 23 B reconciliation)
- [BCS 32-Byte Bundle](bcs-32byte-bundle.md) — the Pufferfish BCS Sequencer / Channel bundle, the `BitCopy` absolute-bit packing path this page contrasts with; the BCS 4×16-bit immediate region the 2-slot model here mirrors
- [BCS Scalar0/Scalar1 ISA](bcs-scalar-isa.md) — the Pufferfish dual-scalar control + memory opcode roster
- [Merged-ALU Bit Layout](merged-alu.md) — `VectorResultDestination` / `BaseAddressEncoding`, the shared-JF vector-ALU operand enums referenced (but not value-decoded) in §2 TABLES B/C/D
- [Retirement Evidence](retirement.md) — the BarnaCore → SparseCore transition; BCAH is the v3-era personality that retires onto SparseCore
- [Index](../index.md) — Part IX — SparseCore & BarnaCore / BarnaCore (legacy v2–v4)
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
