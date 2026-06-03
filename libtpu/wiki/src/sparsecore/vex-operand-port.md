# VEX Operand-Port Binding

> *Every enum value, switch case, proto offset, present-mask bit, and bundle bit position on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`) — from the `GetVexSourcePortEncoding` switch (gfc `0x1c5ee280`, vfc `0x1c5d2e80`), the `FindAndEmitToUnusedPort` 7-port allocator body (glc `0x13a4b840`), the per-op `SparseCoreTecVectorExtendedEncoder` `BitCopy` immediates, and the `EmitXrfResultOp` PopXrf result-commit (gfc/glc `0x13a14180`). Addresses apply to this build; other versions differ.*

## Abstract

This page owns the **operand-to-port** layer of the SparseCore [VectorExtended (VEX)](vectorextended-vex.md) scan/sort engine: how a VEX op's vector operands are bound to read-ports, and where those bindings land in the 64-byte [TEC bundle](tec-engine.md). There are **two distinct port spaces**, and conflating them is the single most common modeling error:

- A **logical** `VregReadPort` (0..9) the *front end* speaks — resolved by `GetVexSourcePortEncoding` into the 8-value **`VexSourcePortEncoding`** that a VEX scan's `SourceOne`/seed selector carries. This is the *source-port encoding* of the page title: 8 legal source ports `0..7`, with port `8` (`V3_X`) and port `9` (`MISC_AUX`) explicitly *rejected* for VEX.
- A **physical** `SparsecoreVregReadPort` (V0..V6) the *encoder* speaks — allocated greedy-first-free by `FindAndEmitToUnusedPort` and BitCopied into 7 scattered 6-bit selector fields of the VEX bundle word.

The decisive structural facts: the source-port encoding is a `StatusOr`-returning **resolver switch** (no table), so a reimplementer must replicate the *exact* legal set `{0..7}` and the two rejection cases; the physical allocator is **op-invariant and generation-independent** (the per-op type only changes an error string); and the result-commit op (`PopXrf`) selects its write-group by a *per-opcode constant* index and its written-lane subset by the *operand-presence pattern*, never the reverse.

For reimplementation, the contract is:

- **`VexSourcePortEncoding` is 8 values `0..7`, a 1:1 image of legal `VregReadPort`.** `GetVexSourcePortEncoding(port)` returns encoding `== port` for `port ∈ [0,7]` with an `OK` status, and `InvalidArgument` for `port == 8` (`V3_X`) and `port == 9` (`MISC_AUX`). The enum names interleave `Y_VREG`/`X` sub-ports of V0..V3 with the `VST_SOURCE` bus at 0 (see [§The 8 source-port encodings](#the-8-source-port-encodings)).
- **The physical operand allocator is a 7-port greedy-first-free btree set.** `FindAndEmitToUnusedPort` pops the lowest free `SparsecoreVregReadPort ∈ [0,6]`, erases it from `port_is_free`, writes the operand's vregno into proto slot `+0x1c+4*p`, and sets present-mask bit `1<<(p+1)` in proto `+0x10`. The body is byte-identical across every VEX op and across gens.
- **The 7 physical port selectors occupy NON-contiguous 6-bit bundle fields.** The `SparseCoreTecVectorExtendedEncoder` BitCopies each present proto slot to its absolute bundle bit: V0→`0x15a`, V1→`0x1bb`, V2→`0x1c7`, V3→`0x196`, V4→`0x1a2`, V5→`0x171`, V6→`0x17d`. This layout is op-invariant (the per-op sub-opcode and the mask/dest-port bits live on [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md)).
- **`PopXrf` (pop vector-register-file) commits VEX results: index = write-group, presence = lane subset.** `EmitXrfResultOp` gates `index < 3` (`vres_unit ∈ {0,1,2}`), takes the write-group index from a *per-opcode constant*, and selects `WriteAll`/`Partial0..4` purely from the `isVoidOp` presence of the three result operands — each present lane committed as a vreg (`GetVregno`) or a vector-mask-dest (`GetVMDestregno`).

| | |
|---|---|
| **Layer** | operand→port binding for the [VEX](vectorextended-vex.md) slot of the 64-byte [TEC bundle](tec-engine.md#the-tec-bundle-64-bytes) |
| **Logical port space** | `VregReadPort` 0..9 → `VexSourcePortEncoding` 0..7 (resolver, 8 legal) |
| **Resolver** | `GetVexSourcePortEncoding` — gfc `0x1c5ee280`, vfc `0x1c5d2e80` (`StatusOr`, switch) |
| **Physical port space** | `SparsecoreVregReadPort` V0..V6 (7 ports) |
| **Allocator** | `FindAndEmitToUnusedPort` — greedy-first-free btree set; glc `0x13a4b840`, gfc `0x13ab2aa0` |
| **Proto slot map** | port `p` → vregno `[proto+0x1c+4*p]`, present bit `1<<(p+1)` in `[proto+0x10]` |
| **Bundle selector bits** | V0=`0x15a` V1=`0x1bb` V2=`0x1c7` V3=`0x196` V4=`0x1a2` V5=`0x171` V6=`0x17d` (6-bit each) |
| **Result commit** | `PopXrf` via `EmitXrfResultOp` (gfc/glc `0x13a14180`); index∈{0,1,2}, oneof tags 7..0xc |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the operand→port BINDING (the two port spaces, the allocator, the source-port encoding, PopXrf).** The VEX opcode→op dispatch roster lives on [VectorExtended (VEX)](vectorextended-vex.md); the per-op sub-opcode constants, the dest-read-port field, and the vector-mask field live on [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md); the 64-byte bundle geometry lives on [TEC Engine](tec-engine.md). They are linked, not repeated.

---

## Two Port Spaces, One Datapath

A reimplementer who builds a single "port" abstraction will mis-decode VEX. The binary keeps two:

```text
 logical (front-end)                     physical (encoder)
 ────────────────────                    ──────────────────
 VregReadPort  0..9                       SparsecoreVregReadPort  V0..V6 (0..6)
   │                                        │
   │ GetVexSourcePortEncoding (resolver)     │ FindAndEmitToUnusedPort (allocator)
   ▼                                        ▼
 VexSourcePortEncoding 0..7                 proto slot +0x1c+4*p  (present 1<<(p+1) @ +0x10)
   (the scan's SourceOne / seed selector)    │
                                            │ SparseCoreTecVectorExtendedEncoder (BitCopy)
                                            ▼
                                           bundle 6-bit selector  @ {0x15a,0x1bb,0x1c7,0x196,0x1a2,0x171,0x17d}
```

- The **logical** space is what a `SourceOne`-style selector references: "feed the scan's seed/carry-in from *this* source bus." Its legal images are the 8 `VexSourcePortEncoding` values. Two `VregReadPort` values — `V3_X` (8) and `MISC_AUX` (9) — are *not* reachable as VEX source encodings and are hard-rejected by the resolver.
- The **physical** space is the per-bundle read-port allocation: the encoder has 7 physical operand-selector fields (V0..V6) in the VEX bundle word, and `FindAndEmitToUnusedPort` assigns each emitted operand the lowest still-free one. The operand's vregno (0..0x3f) is what lands in the selector, not the source-bus code.

The connection: the logical `VexSourcePortEncoding` names the *bus* a seed reads from; the physical V0..V6 selectors carry the *vregnos* the bundle reads each cycle. The [SourceOne seed](vectorload-slot.md#the-sourceone-seed-enum) carried in the bundle is the logical encoding; the operand reads it allocates are the physical selectors.

---

## The Source-Port Encoding (`VexSourcePortEncoding`)

### The resolver

`GetVexSourcePortEncoding(VregReadPort)` is a per-target free function returning `StatusOr<VexSourcePortEncoding>` (here `absl::internal_statusor::Helper*` carrying the int payload at `+0x8` and the status word at `+0x0`). The gfc (Ghostlite) instance at `0x1c5ee280` and the vfc (Viperfish) instance at `0x1c5d2e80` are byte-for-byte the same switch shape; only the error-string suffix and the source-file path differ:

```c
// xla::ghostlite::GhostliteProtoUtils::GetVexSourcePortEncoding(VregReadPort port)   (gfc 0x1c5ee280)
//   this = StatusOr<VexSourcePortEncoding> out; *(int*)(this+8) = encoding; *(qword)this = 1 (=OK)
switch (port) {
  case 0: *(int*)(this+8) = 0; *(qword)this = 1; return this;   // VST_SOURCE  → OK
  case 1: *(int*)(this+8) = 1; goto ok;                         // V0_Y_VREG
  case 2: *(int*)(this+8) = 2; goto ok;                         // V0_X
  case 3: *(int*)(this+8) = 3; goto ok;                         // V1_Y_VREG
  case 4: *(int*)(this+8) = 4; goto ok;                         // V1_X
  case 5: *(int*)(this+8) = 5; goto ok;                         // V2_Y_VREG
  case 6: *(int*)(this+8) = 6; goto ok;                         // V2_X
  case 7: *(int*)(this+8) = 7; ok: *(qword)this = 1; return this; // V3_Y_VREG → OK
  case 8: /* InvalidArgument: "The V3_X slot (port number 8) cannot be used by a VEX instruction." (len 66) */
  case 9: /* InvalidArgument: "MISC_AUX not supported on GLC" (len 29) */
}
```

The legal range is **exactly `[0,7]`**: the resolver returns the identity encoding (`encoding == port`) with `OK` for those eight, and constructs an `InvalidArgument` `Status` for `8` and `9`. The encoding is the value, not a table index — a reimplementer must hard-code the `{0..7}→{0..7}` identity and the two rejection messages, not derive them.

> **GOTCHA — the resolver is identity over `[0,7]` but is NOT a no-op.** It exists to *validate* that the requested logical read port is a legal VEX source: `V3_X` (port 8) and `MISC_AUX` (port 9) are valid `VregReadPort` values elsewhere but are unreachable from a VEX seed selector. Skipping the validation lets an illegal `SourceOne` reach the encoder, which silently mis-packs. The check is the gate; reimplement it as a switch returning a `Result`/`StatusOr`, not as `encoding = port`.

> **NOTE — gfc says "GLC", vfc says "VFC".** The `MISC_AUX not supported on …` message is gen-named: the Ghostlite copy at `0x1c5ee280` reads "GLC" (29 bytes), the Viperfish copy at `0x1c5d2e80` reads "VFC" (29 bytes), each pointing at its own `…_proto_utils.cc`. The `V3_X` message (66 bytes) is identical text in both. The legal `{0..7}` set and the two rejection ports are gen-stable; the message string is the only delta. CONFIRMED both gens.

### The 8 source-port encodings

The encoding interleaves the `Y_VREG` and `X` sub-ports of vector operands V0..V3, with the VST (vector-store) source bus at 0. Cross-confirmed against the [VectorLoad `SourceOne` seed](vectorload-slot.md#the-sourceone-seed-enum), which carries this same enum in the bundle:

| value | enum name | source bus | Confidence |
|---:|---|---|---|
| 0 | `VEX_SOURCE_PORT_ENCODING_VST_SOURCE` | the VST (vector-store) source bus | CONFIRMED |
| 1 | `VEX_SOURCE_PORT_ENCODING_V0_Y_VREG` | V0 operand, `Y_VREG` sub-port | CONFIRMED |
| 2 | `VEX_SOURCE_PORT_ENCODING_V0_X` | V0 operand, `X` sub-port | CONFIRMED |
| 3 | `VEX_SOURCE_PORT_ENCODING_V1_Y_VREG` | V1 operand, `Y_VREG` sub-port | CONFIRMED |
| 4 | `VEX_SOURCE_PORT_ENCODING_V1_X` | V1 operand, `X` sub-port | CONFIRMED |
| 5 | `VEX_SOURCE_PORT_ENCODING_V2_Y_VREG` | V2 operand, `Y_VREG` sub-port | CONFIRMED |
| 6 | `VEX_SOURCE_PORT_ENCODING_V2_X` | V2 operand, `X` sub-port | CONFIRMED |
| 7 | `VEX_SOURCE_PORT_ENCODING_V3_Y_VREG` | V3 operand, `Y_VREG` sub-port | CONFIRMED |
| — | (`V3_X`, logical port 8) | rejected: "cannot be used by a VEX instruction" | CONFIRMED |
| — | (`MISC_AUX`, logical port 9) | rejected: "not supported on GLC/VFC" | CONFIRMED |

The rejection of `V3_X` (the would-be encoding 8) is the reason the *legal* set stops at 7: V3 contributes only its `Y_VREG` sub-port to VEX sources, never its `X`. So the four V-pairs supply `{V0_Y, V0_X, V1_Y, V1_X, V2_Y, V2_X, V3_Y}` — seven V sources plus `VST_SOURCE`, totalling the 8 encodings.

> **QUIRK — `V3_X` exists as a `VregReadPort` but is structurally inadmissible as a VEX source.** A reimplementer enumerating the V sub-ports as a regular `{V0..V3} × {X,Y_VREG}` (8-cell) product will produce one illegal cell. The hardware/ISA carves it out explicitly: the seventh V source is `V3_Y_VREG`, and `V3_X` raises `InvalidArgument`. Why V3 is asymmetric (only `Y_VREG`) versus the micro-architectural reason is `HIGH` — structurally confirmed by the resolver, the silicon rationale inferred.

---

## The Physical Port Allocator (`FindAndEmitToUnusedPort`)

### Greedy first-free over a btree set

The physical read-port assignment is a single template, `FindAndEmitToUnusedPort<SparsecoreVregReadPort, SparseCoreTecVectorExtended_<Op>>`, instantiated once per VEX op. Every instantiation has a **byte-identical body** — only the relocated branch targets and the per-op error-string template arg differ (verified `MinScanU32` `0x13a4b840` vs the `SegmentedAddScanF32`/`MaxScanU32` siblings). The decompiled `MinScanU32` instance:

```c
// FindAndEmitToUnusedPort<…SparsecoreVregReadPort, …VectorExtended_MinScanU32>   (glc 0x13a4b840)
//   a1 = StatusOr<port>* out ; a2 = &port_is_free (btree_set) ; a3 = vregno ; a4 = proto submessage (DWORD*)
if ( *(qword*)(a2 + 16) ) {                       // RetCheck !port_is_free.empty()
    port = *(int*)(**(qword**)a2 + 12);           // btree FIRST node, [node+0xc] = LOWEST free port
    btree_container::erase(a2, &port);            //   greedy: take it, remove from free set
    switch (port) {                               //   port ∈ [0,6] (else "Unsupported Port Value")
      case 0: a4[7]  = vregno; mask = 0x02; break;  //   a4[7]=+0x1c  bit1
      case 1: a4[8]  = vregno; mask = 0x04; break;  //   a4[8]=+0x20  bit2
      case 2: a4[9]  = vregno; mask = 0x08; break;  //   a4[9]=+0x24  bit3
      case 3: a4[10] = vregno; mask = 0x10; break;  //   a4[10]=+0x28 bit4
      case 4: a4[11] = vregno; mask = 0x20; break;  //   a4[11]=+0x2c bit5
      case 5: a4[12] = vregno; mask = 0x40; break;  //   a4[12]=+0x30 bit6
      case 6: a4[13] = vregno; mask = 0x80; break;  //   a4[13]=+0x34 bit7
      default: /* MakeError "Unsupported Port Value: $0" (isa_emitter_base.h:2664) */
    }
    a4[4] |= mask;                                // a4[4]=+0x10 present-mask: set bit 1<<(port+1)
    *(int*)(a1 + 8) = port;                       // StatusOr payload = allocated port index
    *(qword*)a1 = 1;                              // status = OK
} else {
    // RetCheckFailSlowPath(…:2637, "!port_is_free.empty()")
}
```

Mechanism, step by step:

1. **Empty check.** `port_is_free` is an `absl::btree_set<SparsecoreVregReadPort>`; `[a2+0x10]` is its size. Empty ⇒ `RetCheck` failure at `isa_emitter_base.h:2637` (`"!port_is_free.empty()"`).
2. **Lowest free port.** `*(**a2 + 0xc)` reads the value in the btree's first (leftmost) node — the **lowest** free port index. This is the greedy-first-free policy: ports are handed out in ascending V0→V6 order.
3. **Erase.** `btree_container::erase` removes the chosen port so the next operand cannot reuse it within the bundle.
4. **Bound + slot write.** The 7-arm switch dispatches `[0,6]`; out-of-range ⇒ `MakeError "Unsupported Port Value: $0"` (`isa_emitter_base.h:2664`). For a valid port `p`, the operand's vregno is stored at `[proto+0x1c+4*p]` and the present bit `1<<(p+1)` is OR'd into `[proto+0x10]`.
5. **Return.** The allocated port index is returned in the `StatusOr` payload (`[out+0x8]`) with `OK`.

### The proto port-slot map

| physical port | proto slot (vregno) | present-mask bit (`[proto+0x10]`) | Confidence |
|---|---|---|---|
| V0 | `+0x1c` | `0x02` (bit 1) | CONFIRMED |
| V1 | `+0x20` | `0x04` (bit 2) | CONFIRMED |
| V2 | `+0x24` | `0x08` (bit 3) | CONFIRMED |
| V3 | `+0x28` | `0x10` (bit 4) | CONFIRMED |
| V4 | `+0x2c` | `0x20` (bit 5) | CONFIRMED |
| V5 | `+0x30` | `0x40` (bit 6) | CONFIRMED |
| V6 | `+0x34` | `0x80` (bit 7) | CONFIRMED |
| (dest read-port) | `+0x18` | `0x01` (bit 0) | CONFIRMED |

Present-mask bit 0 (`0x01`) is the **dest read-port** slot at `+0x18` (which V-port holds the op's result), written by the per-op emit body, not by this allocator. The seven source ports occupy bits 1..7. This is the proto-message representation; the encoder maps each present slot to the bundle (next section).

> **NOTE — the allocator is generation-independent.** The gfc copy `utils::FindAndEmitToUnusedPort` (`0x13ab2aa0`) is structurally identical: same empty-check, same `[node+0xc]` lowest-port read, same `erase`, same `[0,6]` bound, same `+0x1c`/`0x2` port-0 arm. The 7-port count, the proto offsets, and the present-mask bit assignment are all gen-stable. The *btree free-set lifecycle* — who inserts V0..V6 at bundle start and how the dest read-port is chosen versus the source ports — is `LOW`: this allocator only *consumes* the free set.

### The caller's operand read

Before allocating, the per-op emit body extracts the operand register from the `MCInst`. Operands sit at `[MCInst+0x10]` with stride `0x10`; the helpers validate the register band and rebase the id:

| helper | reg-id band | rebase | yields | Confidence |
|---|---|---|---|---|
| `GetVregno` (`0x13a659c0`) | `[0xd0, 0x10f]` | `id − 0xd0` | vregno 0..0x3f (64 vregs) | CONFIRMED |
| `GetVectorMask` (`0x13a33320`) | `[0x5f, 0x7e]` | `id − 0x5f` | mask 0..0x1f (32 vmasks) | CONFIRMED |
| `GetVMDestregno` (`0x13a65b20`) | `[0x5f, 0x6e]` | `id − 0x5f` | VM-dest 0..0xf (16) | CONFIRMED |

`GetVregno` LogFatals if the `MCOperand` is not a register (kind byte `!= 1`); the reg-id is at operand `+0x8`. The vector mask (when present) is stored to a separate proto field and OR'd into a mask-present bit; the source vregno is what `FindAndEmitToUnusedPort` allocates a port for.

---

## The Bundle Selector Bit Layout

The proto port slots are mapped to absolute TEC-VEX-bundle bit positions one layer down, by the per-op `SparseCoreTecVectorExtendedEncoder<Op>` (`Encode` at `0x1eb30ee0` → per-op `EncodeSparseCoreTecVectorExtended<Op>` at `0x1eb32000+`). Each present proto field is copied by `BitCopy(dst=bundle, dst_bit, src=&proto_field, src_bit=0, nbits)` (`0x1fa0a900`), gated by the `[proto+0x10]` present-mask and a `cmp [proto+0x50],<oneof-tag>` union guard. The 7 source-port selector positions are **op-invariant** (verified identical across `AddScanF32` `0x1eb32380` sub-op `0x05` and `MaxScanF32` `0x1eb32a80` sub-op `0x07`):

```text
 TEC-VEX bundle — the 7 physical port selectors (6-bit each), NON-contiguous
   proto slot   present bit   bundle dst_bit   nbits   field
   ──────────   ───────────   ──────────────   ─────   ─────────────────────
   +0x1c        0x02          0x15a            6       V0 read-port vregno
   +0x20        0x04          0x1bb            6       V1 read-port vregno
   +0x24        0x08          0x1c7            6       V2 read-port vregno
   +0x28        0x10          0x196            6       V3 read-port vregno
   +0x2c        0x20          0x1a2            6       V4 read-port vregno
   +0x30        0x40          0x171            6       V5 read-port vregno
   +0x34        0x80          0x17d            6       V6 read-port vregno
```

The fields are scattered (`0x15a, 0x1bb, 0x1c7, 0x196, 0x1a2, 0x171, 0x17d` — not ascending) — this is the physical operand-selector layout of the VEX slot, not a packed array. A reimplementer must use the absolute bit per port, never `base + 6*p`.

> **NOTE — the dest read-port, the vector mask, and the per-op sub-opcode share this encoder but are documented elsewhere.** The same `SparseCoreTecVectorExtendedEncoder` also BitCopies the dest read-port (`+0x18` → bundle `0x10c`, 3-bit), the vector mask (`+0x38` → bundle `0x104`, 5-bit), and the per-op 6-bit sub-opcode constant (bundle `0x10f`). Those three fields and the full sub-opcode roster are owned by [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md); this page owns only the 7 source-port selectors. They share the encoder, not the page.

> **QUIRK — the same physical sub-opcode `0x1b` is reused outside VEX.** Sub-opcode `0x1b` is `UniquifyFloat` in the VEX slot, but `0x1b` is *also* the primary opcode of the `VectorAlu` `combine_four_lanes` cross-lane fold — they live in different bundle slots (VEX vs VectorAlu0), so no conflict. The `combine_four_lanes` op (glc-only) routes through the same TEC orchestrator *because* its X-source consumes a `SparsecoreVregReadPort` allocation via this allocator; its bit layout is on the [VectorAlu opcode pages](vector-opcode-enum.md), not here.

---

## PopXrf — The VEX Result Commit

### What PopXrf is

`PopXrf` (pop vector-register-file) is the [`VectorResult`](vector-opcode-enum.md) slot's commit op for VEX scan/sort results: it pulls a result out of the extended-result file (XRF) and writes it back to up to three result lanes in the VRF. It is emitted by `EmitXrfResultOp`, one template covering all six commit variants `{WriteAll, Partial0, Partial1, Partial2, Partial3, Partial4}`. The gfc/glc instance is at `0x13a14180` (vfc at `0x139a8240`):

```c
// EmitXrfResultOp<…PopXrfWriteAll, …Partial0..4, …VectorResult>   (gfc/glc 0x13a14180)
//   a1 = MCInst* ; a2 = index (vres_unit) ; a3 = VectorResult& slot proto
if ( (unsigned)a2 >= 3 )                              // RetCheck: vres_unit ∈ {0,1,2}
    return RetCheckFail(…:3171, "vres_unit == 0 || vres_unit == 1 || vres_unit == 2");
// the 3 result operands op0/op1/op2 (MCInst operand stride 0x10), each tested for void:
v0 = isVoidOp(&op0, a2);  v1 = isVoidOp(&op1, a2);  v2 = isVoidOp(&op2, a2);
// the present/void pattern of (op0,op1,op2) selects the oneof variant (tags 7..0xc);
// each PRESENT lane is committed: GetVregno → +0x1c/+0x20, GetVMDestregno → +0x24/+0x20;
// index (a2) → proto +0x18 with present bit 0x01.
```

### Index = write-group, presence = lane subset

Two independent selectors govern PopXrf, and they must not be swapped:

1. **The `index` (`vres_unit`) is the XRF write-group selector — a per-OPCODE constant.** It is *not* operand-derived: the consuming arm hardcodes it. Opcode `0x10e9` passes `index = 0` (`xor esi,esi`; MCInst descriptor `+0x38 == 0x150`); opcode `0x10ea` passes `index = 1` (`mov esi,1`; descriptor `== 0x151`). The `index < 3` RetCheck permits `2`, but glc wires only `{0,1}` (exactly two call sites). The index lands at `proto+0x18` with present bit `0x01`.
2. **The variant (which lanes are written) is selected by the `isVoidOp` presence pattern of the three result operands.** `isVoidOp` (`0x13a659a0`) returns 1 for a void operand. The pattern of `(op0,op1,op2)` chooses the oneof tag and the fields written:

| op0 | op1 | op2 | variant | oneof tag (`[proto+0x50]`) | fields written (present bit) | Confidence |
|:---:|:---:|:---:|---|---:|---|---|
| P | P | P | `WriteAll` | `0x7` | op0→`+0x1c` `GetVregno` (0x2), op1→`+0x20` `GetVregno` (0x4), op2→`+0x24` `GetVMDestregno` (0x8) | CONFIRMED |
| P | V | V | `PopXrfWritePartial0` | `0x8` | op0→`+0x1c` `GetVregno` (0x2) | CONFIRMED |
| P | V | P | `PopXrfWritePartial1` | `0x9` | op0→`+0x1c` (0x2), op2→`+0x20` `GetVMDestregno` (0x4) | CONFIRMED |
| V | P | V | `PopXrfWritePartial2` | `0xa` | op0→`+0x1c` `GetVregno` (0x2) | CONFIRMED |
| V | P | P | `PopXrfWritePartial3` | `0xb` | op0→`+0x1c` (0x2), op2→`+0x20` `GetVMDestregno` (0x4) | CONFIRMED |
| (other) | | | `PopXrfWritePartial4` | `0xc` | op0→`+0x1c`, op2→`+0x20`; index→`+0x18` | CONFIRMED |
| (any other) | | | — | — | `MakeError "Invalid operands for Pop XRF Result."` | CONFIRMED |

The proto present-mask `[proto+0x10]` bits are: bit0 (`0x1`) = index/`+0x18`, bit1 (`0x2`) = `+0x1c`, bit2 (`0x4`) = `+0x20`, bit3 (`0x8`) = `+0x24`. `WriteAll` is the full 3-lane commit (two vregs + one VM-dest); the `Partial*` variants are the operand-presence-determined subsets. Each result oneof submessage is lazily `DefaultConstruct`ed into the `SparseCoreTecVectorResult` union (`WriteAll` `0x1fb7be80`, `Partial0..4` `0x1fb7bec0`..`0x1fb7bfc0` for glc) when the `[proto+0x50]` tag does not already match.

> **GOTCHA — the partial-write "index" is the write-group, not a lane index.** A reimplementer who reads `index` as "which result lane" mis-models PopXrf: the lane subset comes from operand presence, and the index is the XRF partition (write-group 0 or 1). The op name (`WriteAll`/`Partial0..4`) is derived from the *presence pattern*, never from `index`. Treat `index` and the variant as two orthogonal selectors.

> **NOTE — PopXrf index value 2 has a code path but no glc caller.** The `index < 3` RetCheck allows `vres_unit == 2` (a third XRF write-group), but glc's TEC orchestrator wires only opcodes `0x10e9`→0 and `0x10ea`→1. Whether vfc/gfc wire a third PopXrf opcode (index 2) — i.e. whether the XRF has 2 or 3 write-groups on those gens — was not traced. `LOW` for the index-2 reachability; the `{0,1}` glc-wired set is CONFIRMED.

---

## The Three-Layer Emit Composition

The operand→port binding spans three layers; this page owns layers 2 and 3 (the source-port half) and the result commit:

```text
 VEX op emit — operand→port→bundle, three layers
   1. DISPATCH   ConsumeOneTecVexBundleInstruction      opcode → op leaf       (vectorextended-vex)
   2. READ+ALLOC per-op emit body                       MCInst operands →
                  + FindAndEmitToUnusedPort               proto slot +0x1c+4*p   ◄── THIS PAGE
                                                          present-mask +0x10
   3. PACK       SparseCoreTecVectorExtendedEncoder      proto slot → bundle bit
                  + BitCopy                               V0..V6 @ scattered 6b  ◄── THIS PAGE
                  (+ sub-op/dest/mask)                                            (vex-mask-destport-subopcode)
   ── result ──
      COMMIT     EmitXrfResultOp (PopXrf)                index = write-group,
                                                          presence = lane subset ◄── THIS PAGE
```

Layer 1 (dispatch) selects the op leaf and is the [VectorExtended](vectorextended-vex.md) roster's job. Layer 2 reads each `MCInst` operand and allocates it a physical read-port (this page). Layer 3 packs each present proto slot into its absolute bundle bit (this page, for the 7 source selectors; the sub-opcode/dest/mask are [the sibling's](vex-mask-destport-subopcode.md)). The result commit (`PopXrf`) is the symmetric write-back through the [`VectorResult`](vector-opcode-enum.md) slot.

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `xla::ghostlite::GhostliteProtoUtils::GetVexSourcePortEncoding` | gfc `0x1c5ee280` | `VregReadPort`→`VexSourcePortEncoding`; identity `[0,7]`, reject 8/9 | CONFIRMED |
| `xla::viperfish::ViperfishProtoUtils::GetVexSourcePortEncoding` | vfc `0x1c5d2e80` | same switch; "MISC_AUX not supported on VFC" | CONFIRMED |
| `…isa_emitter::FindAndEmitToUnusedPort<…SparsecoreVregReadPort,…VectorExtended_MinScanU32>` | glc `0x13a4b840` | 7-port greedy-first-free allocator (representative instance) | CONFIRMED |
| `…isa_emitter::utils::FindAndEmitToUnusedPort<…>` | gfc `0x13ab2aa0` | gen-identical allocator (structural twin) | CONFIRMED |
| `…container_internal::btree_container<…>::erase` | `0x13a0dd60` | removes the allocated port from `port_is_free` | CONFIRMED |
| `SparseCoreTecVectorExtendedEncoder::Encode` | `0x1eb30ee0` | per-op encoder dispatch (calls per-op `Encode…<Op>`) | CONFIRMED |
| `EncodeSparseCoreTecVectorExtendedAddScanF32` | `0x1eb32380` | sub-op `0x05`; V0..V6 selector bit anchors | CONFIRMED |
| `EncodeSparseCoreTecVectorExtendedMaxScanF32` | `0x1eb32a80` | sub-op `0x07`; same port bits (op-invariant proof) | CONFIRMED |
| `BitCopy(dst,dst_bit,src,src_bit,nbits)` | `0x1fa0a900` | the per-field bit-pack primitive | CONFIRMED |
| `…isa_emitter::EmitXrfResultOp<…PopXrfWriteAll,…Partial0..4,…VectorResult>` | gfc/glc `0x13a14180` | PopXrf result commit; index + presence selection | CONFIRMED |
| `EmitXrfResultOp<…>` (vfc) | vfc `0x139a8240` | vfc PopXrf instance | CONFIRMED |
| `…isa_emitter::isVoidOp` | `0x13a659a0` | per-operand void test driving the variant select | CONFIRMED |
| `…isa_emitter::GetVregno` | `0x13a659c0` | reg band `[0xd0,0x10f]` → vregno `id−0xd0` | CONFIRMED |
| `…isa_emitter::GetVectorMask` | `0x13a33320` | reg band `[0x5f,0x7e]` → mask `id−0x5f` | CONFIRMED |
| `…isa_emitter::GetVMDestregno` | `0x13a65b20` | reg band `[0x5f,0x6e]` → VM-dest `id−0x5f` | CONFIRMED |

---

## Considerations

- **Keep the two port spaces separate.** `VregReadPort`/`VexSourcePortEncoding` (logical, 8 source encodings) is the seed/source selector; `SparsecoreVregReadPort` (physical, V0..V6) is the per-bundle operand allocation. A single "port" abstraction will mis-decode — the logical encoding names a *bus*, the physical selector carries a *vregno*.
- **Replicate the resolver as a validating switch.** `GetVexSourcePortEncoding` is identity over `[0,7]` but must reject `V3_X` (8) and `MISC_AUX` (9). Implement it as a `Result`/`StatusOr`-returning switch with the two rejection messages, not `encoding = port`.
- **The allocator is greedy-first-free and op-invariant.** Lowest free V0..V6 wins; the body is byte-identical across ops and gens. Model the per-bundle free set as a btree/sorted set and erase on allocation; the only per-op difference is an error string.
- **Use absolute bundle bits for the 7 selectors.** V0=`0x15a`, V1=`0x1bb`, V2=`0x1c7`, V3=`0x196`, V4=`0x1a2`, V5=`0x171`, V6=`0x17d` (6-bit each) — scattered, not `base + 6*p`.
- **PopXrf: index = write-group (per-opcode constant), presence = lane subset.** The variant name is derived from the `isVoidOp` pattern, never from `index`; the two selectors are orthogonal.
- **Unmapped (LOW/inferred).** The micro-architectural reason V3 contributes only `Y_VREG` (V3_X inadmissible) — `HIGH`; the btree free-set insert/reset lifecycle (who populates V0..V6 per bundle, how the dest read-port is chosen) — `LOW`; PopXrf index-2 reachability on vfc/gfc (third XRF write-group) — `LOW`.

---

## Related Components

| Name | Relationship |
|---|---|
| `GetVexSourcePortEncoding` (`0x1c5ee280` gfc / `0x1c5d2e80` vfc) | the source-port resolver; `VregReadPort`→the 8 `VexSourcePortEncoding` values |
| `FindAndEmitToUnusedPort` (`0x13a4b840` glc / `0x13ab2aa0` gfc) | the 7-port greedy-first-free physical allocator; gen-independent body |
| `SparseCoreTecVectorExtendedEncoder` (`0x1eb30ee0`) | the bit-packing layer mapping proto port slots → scattered bundle selector bits |
| `EmitXrfResultOp` (`0x13a14180` gfc/glc) | the PopXrf result-commit; index = XRF write-group, presence = lane subset |
| `SparseCoreTecVectorResult` (oneof tags 7..0xc) | the union the PopXrf variants are constructed into |

## Cross-References

- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/dedup op roster (the opcode→op dispatch layer 1) whose operands this page binds to ports.
- [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md) — the sibling fields that share the `VectorExtendedEncoder`: the 5-bit vector mask (`0x104`), the 3-bit dest read-port (`0x10c`), and the per-op 6-bit sub-opcode (`0x10f`) constant map.
- [VectorLoad Slot](vectorload-slot.md) — its `SourceOne` seed field carries the `VexSourcePortEncoding` this page's resolver produces; the seam between the load datapath and the VEX scan.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the `VectorResult` slot's 8-value op set (`EupResult`, `PopXrfWriteAll`, `PopXrfWritePartial0..4`, `VresMove`) that PopXrf is part of; the `VectorAlu`/`combine_four_lanes` slot that reuses the port allocator.
- [TEC (Vector) Engine](tec-engine.md) — the 64-byte bundle geometry, the slot bases, and the encoder-dispatch model the VEX encoder plugs into.
- [SparseCore Overview](overview.md) — the three SC engine classes, per-gen presence, and where the TEC vector slots sit.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore ISA — [back to index](../index.md)
