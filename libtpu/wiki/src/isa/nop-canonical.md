# NOP / Unused-Slot Canonical Encoding

> *Every offset, opcode value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous anchor; the runtime-reported `0.103` is not statically verifiable in the binary). Other versions differ.*

## Abstract

A TensorCore bundle is a fixed-width VLIW issue word — 41 B (Jellyfish / TPU v2), 51 B (Pufferfish / TPU v4), 64 B (Viperfish / Ghostlite / `6acc60406`, TPU v5p / v6e / TPU7x) — with one slot per execution unit (MXU, two scalar lanes, two vector-ALU lanes, vector load / store / cmem-load, two vector-extended-to-MXU ports, two vector-result-FIFO reads, and a misc/sequencer slot). On almost every cycle the compiler has no operation for some of those slots, so the encoder must emit a *defined* "this slot does nothing" pattern that the decoder can round-trip and the hardware will reliably skip. This page is the encode/decode support reference for that pattern: what an idle slot and a whole idle bundle look like on the wire.

There are **two distinct no-op mechanisms**, and a reimplementer must keep them separate. The first is the **empty-slot predicate fill**: every slot carries a predicate field, and an unfilled slot is stamped with the architectural sentinel `kNeverExecute = 31` (`0xb834cfc`), which the sequencer reads as "this slot is predicated off this cycle." This is the mechanism that turns an absent slot into a no-op, and it is what makes the whole-bundle NOP. The second is the **opcode-space NOP**: each slot's opcode enum reserves a dedicated `Noop` value — a `<Slot>_Noop` proto sub-message whose opcode field is the **all-ones maximum** of that field (`31` for a 5-bit opcode, `15` for the 4-bit Viperfish vector-extended opcode). The opcode-space NOP is used when a slot must be *present in the bundle's has-mask* (so the decoder visits it) yet carry no work — most importantly inside the default bundle the codec materializes when it needs a valid-but-inert template.

The reason the two coexist is the bundle's two-level occupancy model. A bundle has a per-slot *has-bit* (does this slot exist in the proto at all?) and, independently, a per-slot *predicate* (if it exists, does it fire this cycle?). The cheapest empty slot has its has-bit clear and is never encoded — but the codec's `FillDefaultBundle` path needs every slot present (all has-bits set) so the bytes are deterministic, and for those present-but-inert slots it pairs the `kNeverExecute` predicate with an opcode-space `Noop` (or, for the scalar-0 slot, a `ScalarHalt`). The page proceeds: the whole-bundle NOP at a glance; the per-gen empty-slot predicate fill; the per-slot opcode-space `Noop` value and its bit position; and how the bundle packer leaves unused slots when it lowers an LLO stream.

For reimplementation, the contract is:

- The **empty-slot sentinel** `kNeverExecute = 31` and where each generation stamps it (`EncoderJf::EncodeBundleInternal` prologue; `FillDefaultBundle`; the V5+ bundle header).
- The **opcode-space `Noop`** per slot: a distinct proto type whose opcode field is the all-ones value of that field's width, recognized by `<Slot>NoopOpcode::Matches`.
- The **default-bundle exception**: `FillDefaultBundle` stamps predicate `15` (`kAlwaysExecute`) + a `ScalarHalt` op into scalar-0, and `31` (`kNeverExecute`) into every other slot.
- The **packer fill rule**: the packer leaves an unused slot's has-bit clear; the encoder's prefill (`kNeverExecute`) supplies the no-op, so a freshly built bundle is *not* an all-zero buffer.

| | |
|---|---|
| **Empty-slot sentinel** | `kNeverExecute = 31` (`0x1F`) @ `0xb834cfc` |
| **Always-execute sentinel** | `kAlwaysExecute = 15` (`0x0F`) @ `0xb834cf8` |
| **Predicate field width** | 5 bits per slot (`[0:3]` reg index, `[4]` polarity; `0x1F` = never) |
| **Opcode-space NOP** | `<Slot>_Noop` proto; opcode field = all-ones of field width (5-bit → `31`, VF VEx 4-bit → `15`) |
| **JF empty-slot stamp** | `EncoderJf::EncodeBundleInternal` @ `0x1e86c7c0` (inline prologue stores) |
| **PF default bundle** | `TensorCoreCodecBase<…>::FillDefaultBundle` @ `0x1d222ee0` |
| **NOP recognizer** | `<Slot>NoopOpcode::Matches` — `(~field & mask) == 0` (every opcode bit set) |
| **Default scalar-0 op** | `TensorCoreScalar0_ScalarHalt` (default-constructed in `FillDefaultBundle`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Whole-Bundle NOP at a Glance

The canonical idle bundle is **every slot predicated off**. It is not an all-zero buffer: the predicate field of each slot must hold the nonzero sentinel `31`. The table gives, per generation, the slot count whose predicate must be stamped, the value stamped, and the function that does the stamping.

| Gen | Bundle | Idle-bundle definition | Stamp value | Stamping function | Confidence |
|---|---:|---|---|---|---|
| Jellyfish (v2) | 41 B | all 8 slot-predicate fields = `kNeverExecute` | `31` | `EncoderJf::EncodeBundleInternal` @ `0x1e86c7c0` (inline prologue) | CONFIRMED |
| Pufferfish (v4) | 51 B | all 12 slot predicates = `31`; scalar-0 default = `ScalarHalt`@`15` | `31` / `15` | `…CodecBase<TensorCoreBundle,…>::FillDefaultBundle` @ `0x1d222ee0` | CONFIRMED |
| Viperfish (v5p) | 64 B | per-slot predicate field = `31`; absent slots not encoded | `31` | bundle-header prefill (`vxc` encoder set) | HIGH |
| Ghostlite (v6e) | 64 B | per-slot predicate field = `31` | `31` | bundle-header prefill (`glc` encoder set) | HIGH |
| `6acc60406` (TPU7x) | 64 B | two-entry `TensorCorePredicates` pool + per-slot selector; idle selector points at a `31`-equivalent pool entry | pool index | bundle-header prefill (`gfc` encoder set) | MEDIUM |

> **GOTCHA — the idle bundle is nonzero.** Because the empty mark is `kNeverExecute = 31` (`0x1F`) — a *nonzero* 5-bit pattern replicated into every slot's predicate field — a `memset(bundle, 0, width)` followed by filling only the active slots leaves the inactive slots at predicate `0`, which is a *valid* reference to predicate register P0 (the slot fires, gated on P0). The empty value is the explicit `31` stamp, never zero. A bit-exact round-trip test that builds the "expected idle bundle" by zeroing memory will silently disagree with the real encoder on every unused slot.

> **NOTE —** "whole-bundle NOP" has two flavors and they encode differently. The *packer's* idle bundle leaves unused slots' has-bits **clear**, so they are not serialized at all and only their header-prefilled predicate bytes appear. The *codec's* `FillDefaultBundle` template sets **all** has-bits and gives each present slot a predicate-`31` + opcode-`Noop` pair (scalar-0 excepted). The first is what a real schedule emits for a sparse cycle; the second is the deterministic all-present template used for sizing, zero-init, and decoder warm-up.

---

## Empty-Slot Predicate Fill

The predicate field is the universal "skip this slot" lever, and the encoder writes the sentinel into every slot before any real operation overwrites it. The predicate is a 5-bit field: bits `[0:3]` are the predicate-register index (`0..14` = P0..P14, `15` = `kAlwaysExecute` constant-true), bit `[4]` is the polarity/negate bit, and the all-set value `0x1F = 31` is `kNeverExecute` — the slot does not run. The three sentinel constants are adjacent in `.rodata`: `kPredicateRegisterCount = 15` @ `0xb834cf4`, `kAlwaysExecute = 15` @ `0xb834cf8`, `kNeverExecute = 31` @ `0xb834cfc`.

### Jellyfish — inline prologue stamp

`EncoderJf::EncodeBundleInternal` (`0x1e86c7c0`) is a direct-pack encoder: it works in a 53-byte scratch struct, zeroes the slot region with two `ymm` stores, then stamps `kNeverExecute & 0x1F` into each slot's predicate field by writing the sentinel directly into the struct qwords at the slot's absolute bit. The prologue, decompiled and de-obfuscated:

```c
// EncoderJf::EncodeBundleInternal prologue  @ 0x1e86c7c0
vmovups [struct+0x20] = 0;  vmovups [struct+0x14] = 0;     // clear the slot region
nx = kNeverExecute & 0x1F;                                  // = 31

*(uint64*)(struct+0x2D) = (nx << 53) | (nx << 26);          // scalar_0 pred @abs 317, scalar_1 @abs 290
*(uint16*)(struct+0x1D) = nx << 11;                         // VALU lane0 pred @abs 147
*(uint32*)(struct+0x16) = nx << 5;                          // VALU lane1 opcode-region init
*(uint16*)(struct+0x1A) = nx << 4;          // = 0x1F0      // VALU lane1 pred @abs 116
*(uint8 *)(struct+0x1C) = 0;                                // window high byte clear
*(uint64*)(struct+0x0C) = (0x400000800000000 * nx)         // VLoad pred @abs 58 + MXU pred @abs 35
                        | (nx << 22)                        // VResult pred @abs 22
                        | (nx << 13);                       // Misc predication @abs 13
```

The constant `0x400000800000000` has exactly two set bits — bit 35 and bit 58 — so multiplying it by `31` places `kNeverExecute` at the MXU predicate (abs 35) and the vector-load predicate (abs 58) in one multiply. Because struct byte `0x0C` is bundle bit 96, the qword-0 shift constants (`<<35`, `<<58`, `<<22`, `<<13`) read as the absolute bundle bit positions verbatim. A present slot's per-slot writer (e.g. `EmitVectorBinop`) later overwrites its predicate field with the real `EncodePredication` value; an absent slot keeps `31`.

| Slot | Predicate abs bit | Prefill store | Confidence |
|---|---:|---|---|
| Scalar 0 | 317 | `[struct+0x2D] |= 31<<53` | CONFIRMED |
| Scalar 1 | 290 | `[struct+0x2D] |= 31<<26` | CONFIRMED |
| VALU lane 0 | 147 | `[struct+0x1D] = 31<<11` | CONFIRMED |
| VALU lane 1 | 116 | `[struct+0x1A] = 0x1F0` | CONFIRMED |
| Vector load | 58 | `[struct+0x0C] |= 31·0x400000800000000` (bit 58) | CONFIRMED |
| MXU | 35 | `[struct+0x0C] |= 31·0x400000800000000` (bit 35) | CONFIRMED |
| Vector result | 22 | `[struct+0x0C] |= 31<<22` | CONFIRMED |
| Misc | 13 | `[struct+0x0C] |= 31<<13` | CONFIRMED |

### Pufferfish — `FillDefaultBundle`

Pufferfish is record-based: the codec builds an all-present default bundle and lets the byte serializer read it. `TensorCoreCodecBase<TensorCoreBundle, …>::FillDefaultBundle` (`0x1d222ee0`) default-constructs each of the twelve slot sub-messages, sets all twelve has-bits in the word at `proto+0x10`, and writes the predicate value into each sub-message's predicate field (at sub-message offset `+0x1c` or `+0x20` depending on slot type, decompiled as `*(_DWORD*)(slot+28) = 31` / `*(_DWORD*)(slot+32) = 31`). There are **eleven** `= 31` stores plus one `= 15` store — the scalar-0 exception below.

```c
// FillDefaultBundle(TensorCoreBundle*)  @ 0x1d222ee0 (decompiled, de-obfuscated)
TensorCoreBundle(bundle, /*arena*/0);
bundle->has_mask |= 1;                                       // proto+0x10 bit0 = scalar_0 present

s0 = DefaultConstruct<TensorCoreScalar0>(arena);            // slot 0
s0->predicate = 15;                                          // kAlwaysExecute — NOT 31
s0->has_mask |= 4;
s0->inst_case = 6;                                           // ScalarHalt oneof tag
s0->inst = DefaultConstruct<TensorCoreScalar0_ScalarHalt>(arena);   // proto+0x48

s1 = DefaultConstruct<TensorCoreScalar1>(arena);  s1->predicate = 31;   // kNeverExecute
valu0 = DefaultConstruct<TensorCoreVectorAlu0>(arena);  valu0->predicate = 31;
valu1 = DefaultConstruct<TensorCoreVectorAlu1>(arena);  valu1->predicate = 31;
vstore = DefaultConstruct<TensorCoreVectorStore>(arena);  vstore->predicate = 31;
vload  = DefaultConstruct<TensorCoreVectorLoad>(arena);   vload->predicate  = 31;
cmem   = DefaultConstruct<TensorCoreCmemLoad>(arena);     cmem->predicate   = 31;
vex0   = DefaultConstruct<TensorCoreVectorExtended0>(arena);  vex0->predicate = 31;
vex1   = DefaultConstruct<TensorCoreVectorExtended1>(arena);  vex1->predicate = 31;
vres0  = DefaultConstruct<TensorCoreVectorResult0>(arena);    vres0->predicate = 31;
vres1  = DefaultConstruct<TensorCoreVectorResult1>(arena);    vres1->predicate = 31;
misc   = DefaultConstruct<TensorCoreMisc>(arena);            misc->predicate   = 31;
// all 12 has-bits now set; serializer produces the deterministic default 51-byte word
```

> **CORRECTION (NOP-1) —** an earlier reading treated the default bundle as "all slots predicated off." The decompilation shows scalar-0 is the exception: `FillDefaultBundle` stamps predicate **`15` (`kAlwaysExecute`)** into scalar-0 and gives it a default `ScalarHalt` op, while every other slot gets predicate `31` (`kNeverExecute`). The default bundle therefore *executes one instruction* — a halt — and skips the rest. This is a deliberate safety template: a bundle that falls through to the default state stops the program rather than running garbage. A reimplementation that stamps `31` into scalar-0 to make a "fully idle" default would let execution run off the end instead of halting.

### Viperfish / Ghostlite / `6acc60406` — header prefill

The V5+ generations (`vxc`, `glc`, `gfc`) build the 64-byte buffer with the universal bit-packer `BitCopy(dst, dst_bit, src, src_bit, nbits)` (`0x1fa0a900`), driven by each slot's `<Slot>Encoder::Encode`. The empty-slot predicate is written into the bundle header before any slot is filled, identically in spirit to the JF/PF stamp; a populated slot's per-slot predicate field then overwrites the default. On Viperfish the predicate is a per-slot 4+1 field local to each scalar slot (4-bit reg index + 1-bit inversion). On `6acc60406` the per-slot field shrinks to a 2-bit selector indexing a bundle-level two-entry `TensorCorePredicates` pool; an idle slot's selector resolves to a never-execute pool entry. (HIGH for VF/GL header prefill; MEDIUM for the `6acc60406` pool-selector idle value — the exact pool-entry encoding of "never" was not pinned to a single store, see Residual Gaps.)

---

## Opcode-Space NOP per Slot

Independent of the predicate, each slot's opcode enum reserves a `Noop` value. It exists so a slot can be **present in the has-mask and visited by the decoder** while carrying no operation — the case `FillDefaultBundle` produces and the case the disassembler must round-trip. The `Noop` is a distinct proto sub-message type (`<Slot>_Noop`) holding no fields — its constructor only installs the vtable and arena pointer (`TensorCoreScalar0_Noop::TensorCoreScalar0_Noop` @ `0x1fa64240`, `TensorCoreMisc_Noop` @ `0x1fa62000`, `vxc::isa::TensorCoreVectorExtended_Noop` @ `0x1fb0a120`) — and its opcode field is the **all-ones maximum** of that field's bit width.

### The all-ones rule

The canonical evidence is each slot's `<Slot>NoopOpcode::Matches` predicate. `Matches` returns true iff every bit of the slot's opcode field is set, written in the binary as the idiom `(~field & mask) == 0` — i.e. `~field` has no bit inside `mask`, so `field` has *all* of `mask`'s bits. The mask is the contiguous opcode field for that slot:

| Gen / slot | `<Slot>NoopOpcode::Matches` @ | Opcode mask | Field width / position | NOP opcode value | Confidence |
|---|---|---|---|---:|---|
| PF VectorAlu0 | `0x1ed40820` | `0x1F00000000000` | 5-bit @ bit 44 | `31` | CONFIRMED |
| PF VectorAlu1 | `0x1ed63c80` | (5-bit, sym to Alu0) | 5-bit | `31` | CONFIRMED |
| PF Scalar0 | `0x1ed14480` | `0xF80000` | 5-bit @ bit 19 | `31` | CONFIRMED |
| PF Scalar1 | `0x1ed27be0` | (5-bit) | 5-bit | `31` | CONFIRMED |
| PF Misc | `0x1ed016c0` | `0x1F000000000` | 5-bit @ bit 36 | `31` | CONFIRMED |
| PF VectorLoad | `0x1ee280e0` | `0x1F00` | 5-bit @ bit 8 | `31` | CONFIRMED |
| PF VectorStore | `0x1ee38e00` | `0x7C00000000` | 5-bit @ bit 34 | `31` | CONFIRMED |
| PF CmemLoad | `0x1ecf87e0` | `0x7C000000000000` | 5-bit @ bit 50 | `31` | CONFIRMED |
| PF VectorExtended0 | `0x1eda6400` | `0x7C00000000` | 5-bit @ bit 34 | `31` | CONFIRMED |
| PF VectorExtended1 | `0x1edfdc60` | (5-bit) | 5-bit | `31` | CONFIRMED |
| PF VectorResult0 | `0x1ee2ae40` | `0x7C00000000000000` | 5-bit @ bit 58 | `31` | CONFIRMED |
| PF VectorResult1 | `0x1ee2ce00` | (5-bit) | 5-bit | `31` | CONFIRMED |
| VF VectorExtended0 | `0x1ef98480` | `0xF` | 4-bit @ bit 0 | `15` | CONFIRMED |
| VF VectorExtended1 | `0x1efe3540` | (4-bit) | 4-bit | `15` | CONFIRMED |

Worked example — `TensorCoreVectorAlu0NoopOpcode::Matches` (`0x1ed40820`) decompiles to a single expression:

```c
// asic_sw::deepsea::pxc::isa::TensorCoreVectorAlu0NoopOpcode::Matches  @ 0x1ed40820
bool Matches(this) {
    return (~*(uint64*)(this + 32) & 0x1F00000000000LL) == 0;   // every bit of the 5-bit
}                                                                // opcode field (bits 44..48) set
```

`0x1F00000000000` is `0b11111 << 44` — a 5-bit field whose all-set value is `31`. The same shape recurs for every PF slot (always 5 bits, always all-ones = `31`) and shrinks to 4 bits (`0xF`, all-ones = `15`) for the Viperfish vector-extended slots, whose opcode space is one bit narrower.

> **QUIRK — the NOP opcode is the *last* opcode, not the first.** Each slot's opcode enum places `Noop` at the maximum encodable value (all-ones), not at `0`. Opcode `0` is a real operation in every slot (e.g. the first arithmetic op), so a reimplementation that emits opcode `0` for "no work" will issue a live ALU op. The reliable "no work in this slot's opcode space" value is the field's all-ones maximum, and the decoder recognizes it by the all-bits-set test, not by a single named constant.

> **NOTE — opcode-`Noop` and predicate-`kNeverExecute` are orthogonal and usually combined.** In `FillDefaultBundle` a non-scalar-0 slot is both opcode-`Noop` *and* predicate-`31`. Either alone suffices to make the slot inert: predicate-`31` skips it regardless of opcode, and a slot whose has-bit is clear is never even decoded. The opcode-`Noop` matters specifically for the present-and-visited case, where the decoder must classify the opcode bits and would otherwise reject an unknown value.

### The LLO-level no-ops

Above the bundle, the [LLO opcode enum](llo-opcode-enum.md) carries its own named no-ops that lower into these slot encodings: `kVectorNop` (`0x047`, the vector no-op, sharing `GhPerf` cost row `0x1B4` with `kVectorMaskMove`) lowers into a VALU slot whose opcode-space value is the all-ones `Noop`; `kScalarHalt` (`0x025`, with `…YieldConditional`/`…OnError` at `0x026`/`0x027`) is the sequencer-halt the scalar-0 default op materializes. These are the IR-visible names; the bundle bytes are the all-ones opcode and the predicate sentinel documented above.

---

## Bundle Packer Fill of Unused Slots

The [bundle packer](../sched/llo-bundle-packing.md) groups independent LLO ops into a `Bundle` proto, one typed sub-message per filled slot, and leaves the rest. It does **not** synthesize `Noop` ops or zero anything — it relies entirely on the encoder prologue's `kNeverExecute` prefill. The model, as seen on the JF `MakeInstruction` / `FindFreeSlot` path and the slot-mask dispatch in `EncodeBundleInternal`:

```c
// Conceptual packer → encoder flow (JF; PF/V5+ analogous)
function PackAndEncode(llo_ops, bundle):
    bundle.slot_mask = 0;                       // proto+0x10 — all slots absent
    for op in llo_ops_in_this_cycle:
        slot = FindFreeSlot(bundle, op.kind);   // 0x140c0180 — legality + occupancy
        if slot == NONE: break;                 // start a new bundle
        slot.fill(op);                          // EmitVectorBinop / EmitScalarLoad / …
        bundle.slot_mask |= (1 << slot.index);  // mark has-bit set

    EncodeBundleInternal(bundle):               // 0x1e86c7c0
        prefill_all_predicates(kNeverExecute);  // 31 into every slot's predicate field
        for slot in slots:
            if bundle.slot_mask & (1 << slot.index):
                slot_encoder.write(bundle.sub[slot]);   // overwrites predicate with real value
            // else: leave the prefilled kNeverExecute — this is the canonical empty slot
```

Three consequences a reimplementer must reproduce:

- **Unused slots are absent in the proto, never an explicit `Noop`.** The packer leaves the has-bit clear; the slot's bytes come solely from the prefill. The explicit `<Slot>_Noop` op only appears via `FillDefaultBundle`, not via normal packing.
- **The prefill is the contract.** Because the encoder zeroes the slot region and *then* stamps `31` into the predicate sub-fields, the empty-slot bytes are deterministic and decode-stable. Skip the prefill and the empty slots inherit whatever was in the scratch buffer.
- **`ValidatePacking` re-checks the predicate range.** Every slot's predicate field must be in `{0..14, 15, 31}`; the packer's hazard analysis (`add_consumes_predicate_register` `0x1e851be0`, `add_produces_predicate_register` `0x1e851c60`) tracks predicate def-use so a packed bundle's predicates are legal, and an unfilled slot's prefilled `31` passes by construction.

> **GOTCHA — no intra-bundle forwarding means an empty slot is genuinely free.** Because all slots in a bundle issue in the same cycle with no write→read forwarding (see [Bundle Model](bundle-model-overview.md)), leaving a slot empty has no correctness cost — it costs only the wasted issue width. The packer's job is to *fill* slots for throughput, not to satisfy dependencies within a bundle; a true dependency is always serialized across bundles or via a sync-flag wait. So the canonical NOP is purely a density artifact, not a hazard fix.

---

## Decode Side

The decoder round-trips both mechanisms. The predicate read is `DecoderJf::GetPredicateRegister` (`0x1e84eae0`, returns `v & 15` — the 4-bit reg index, where `15` = always) and `GetPredicateValue` (`0x1e84eb00`, returns `(v & 16) == 0` — the polarity bit); a full 5-bit read of `0x1F` is the `kNeverExecute` skip. The opcode-space `Noop` is recognized by the `<Slot>NoopOpcode::Matches` all-bits-set test inside each slot's `Decode` ladder. The V5+ decoders try opcode candidates in a fixed order — `gfc::isa::TensorCoreVectorExtended0Decoder::Decode` (`0x1f96d020`) walks `LoadMultiplicandUpperBlock…` → `B32Masked` → `B16Masked` → `B8Masked` opcode `Matches` predicates; the vector slots that own a `Noop` (e.g. the `MatrixMultiply*`/`PushMatrix*`-bearing extended slots) test `Noop` early so an inert slot resolves without falling through to a "value does not match any encodings" status error. See [Decode-Side: JF / PF](decode-side-jf-pf.md) and [Decode-Side: VF / GXC](decode-side-vf-gxc.md).

---

## Residual Gaps

- **`6acc60406` (`gfc`) idle-slot value — MEDIUM.** `6acc60406` replaces the per-slot predicate field with a 2-bit selector into a two-entry `TensorCorePredicates` pool. The whole-bundle idle pattern therefore depends on the pool's contents, and the exact pool-entry encoding for "never execute" (and the default selector an unfilled slot carries) was not pinned to a single store the way the JF/PF `31` stamp was. The all-ones opcode-`Noop` rule is expected to hold (the `gfc` vector slots carry the same `Noop` opcode family), but no `gfc::…NoopOpcode::Matches` body was read to confirm the field width; marked Inferred for `6acc60406`. *Evidence missing:* the `gfc` bundle-header prefill store and a `gfc` `NoopOpcode::Matches` body.
- **Viperfish / Ghostlite header prefill site — HIGH not CONFIRMED.** The `vxc`/`glc` empty-slot predicate is written in the bundle-header construction before slot fill (consistent with the per-slot 4+1 predicate field and the VF `TensorCoreVectorExtended0NoopOpcode::Matches` 4-bit `0xF` confirmed above), but the single prefill instruction was not isolated to one address the way `EncodeBundleInternal`/`FillDefaultBundle` were for JF/PF. *Evidence missing:* the exact `vxc`/`glc` header-stamp store address.

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW no-scoreboard contract and the `kNeverExecute` empty-slot convention this page expands per slot
- [Bundle: Jellyfish 41 B](bundle-jf-41b.md) — the `EncodeBundleInternal` prologue and the per-slot predicate abs-bit map for the JF prefill
- [Bundle: Pufferfish 51 B](bundle-pf-51b.md) — `FillDefaultBundle` and the twelve-slot `31`-stamp / `ScalarHalt` default
- [Bundle: Viperfish 64 B](bundle-vf-64b.md) — the per-slot 4+1 predicate field the VF empty mark writes
- [Bundle: Ghostlite 64 B](bundle-gl.md) — V5+ `BitCopy` encoder set and header predicate prefill
- [Bundle: 6acc60406 (GF) 64 B](bundle-gf.md) — the `TensorCorePredicates` pool model and the `Noop`-first decoder ladder
- [LLO Opcode Enum](llo-opcode-enum.md) — `kVectorNop` (`0x047`) and `kScalarHalt` (`0x025`), the IR-level no-ops that lower into these slot encodings
- [Predicate Slot](slot-predicate.md) — the 5-bit predicate field, the `15`/`31` sentinels, and `ValidatePacking`'s range check
- [ISA Emitter Registry](isa-emitter-registry.md) — where the per-gen `Encoder<gen>` / `Decoder<gen>` codecs register
- [ISA Overview](overview.md) — the encode/decode pipeline this page supports
