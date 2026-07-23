# CUSTOM_OP Wire Validators and the Scratch / Sync Handshake

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The validators and the encoder live in `neuronxcc/starfish/lib/libwalrus.so` (sha1 `4531c8f2…`, 64,973,024 B; `.dynsym` retained, so `nm -DC` resolves the named symbols); the BIR instruction / sync / memory-location bodies live in `libBIR.so`. For `.text`/`.rodata` the virtual address equals the file offset. Other wheels differ — treat every address as version-pinned. Evidence derives solely from static binary analysis.*

## Abstract

A `CUSTOM_OP` op encodes to a **chain** of 64-byte ISA bundles — one `CUSTOM_OP_HEADER` (opcode `0x85`) followed by one or more `CUSTOM_OP_PAYLOAD` words (opcode `0x86`), one payload per operand access pattern. [11.5](customop-wire-layout.md) documents how the encoder *writes* those bytes. This page documents the two predicates that *check* them on the way out, and the host-engine ↔ GPSIMD-CPU semaphore handshake that the same bundle carries in its sync overlay: `neuronxcc::core_v2::is_valid_custom_op_header` (`0x129b940`) and the inlined `is_valid_custom_op_payload` (preserved 1:1 by its diagnostic twin `dbg_is_valid_custom_op_payload` @ `0x129c610`).

Both validators take a `NEURON_ISA_TPB_INST_UNION` **by value** — the whole 64-byte bundle, passed in registers and on the stack — and run a short-circuit chain of byte-field gates: the opcode byte, the fixed header-tag `0x10`, the shared `has_valid_neuron_events` SyncInfo check, the opcode value (`0x85` / `0x86`), a **reserved-zero discipline** (every byte the record does not populate must be exactly zero), and — for the header only — a **scratch-space tri-field consistency** rule. The two validators are the *same shape* with the reserved-zero mask shifted to match which bytes each record type actually carries: the header reserves `[0x11..0x13] ∪ [0x1D..0x1F] ∪ [0x20..0x3F]` and gates a `{byte[0x10], dword[0x18], byte[0x1C]}` scratch triple; the payload reserves only `[0x0C..0x0E]` and lets `[0x10..0x3F]` be a live access pattern.

The sync handshake is **not** a custom-op-specific mechanism. It is the generic `neuronxcc::backend::AllocSemaphores` machinery (the `alloc_semaphores` pass) that stamps a `Wait` + `Update` SyncInfo onto every instruction, custom op included; the wait/update bytes land in the bundle at `[0x04..0x07]` where `has_valid_neuron_events` reads them. The primitive `setupSync` installs is a strict **1:1 producer-update → consumer-wait** pairing on a single semaphore id: the consumer's `Wait` is wired to the exact semaphore the producer's single local `Update` bumps — asserted at `alloc_semaphores.cpp:171/172`. The host TPB engine waits on the CPU-completion semaphore; the GPSIMD core (via its result DMA) updates it.

For reimplementation, the contract is:

- The **`NEURON_ISA_TPB_INST_UNION` offset map** as the validators read it: which byte is the opcode, the header-tag, the SyncInfo overlay, the count/id block, the scratch triple, and the reserved-zero regions.
- The **header validator** five-predicate chain (`0x129b940`) including the exact reserved-zero masks (`0xFFFFFF00` on `dword[0x10]|dword[0x1C]`, a qword-OR on `[0x20..0x3F]`) and the all-zero-or-all-nonzero scratch gate.
- The **payload validator** (`0x129c610`) and *why* its reserved-zero mask is `[0x0C..0x0E]` instead of the header's tail.
- The **scratch tri-field**: what it encodes, and the build fact that this encoder emits it as zero (the SB scratchpad is conveyed structurally as extra `MemoryLocation`s, tracked by `bir::isCCHavingSBScratchPad`).
- The **sync handshake**: `setupSync` = install `Wait` + (per-sem: `incrementSemaphore` + install `Update`); the single-local-update invariant; and how the semaphore ids reach the bundle's `[0x04..0x07]` overlay.

| | |
|---|---|
| **Header validator** | `core_v2::is_valid_custom_op_header(NEURON_ISA_TPB_INST_UNION)` @ `0x129b940` (327 B real body) |
| **Header dbg twin** | `dbg_is_valid_custom_op_header` @ `0x129ba90` (builds the reason string) |
| **Payload validator** | inlined — no own symbol; dbg twin `dbg_is_valid_custom_op_payload` @ `0x129c610` is authoritative |
| **Shared event gate** | `core_v2::has_valid_neuron_events` @ `0x127eb60`; `is_valid_enum` @ `0x127ab90` |
| **Header opcode / tag** | `byte[0]==0x85`, `byte[1]==0x10` (16-bit word `0x1085`) |
| **Payload opcode / tag** | `byte[0]==0x86`, `byte[1]==0x10` (16-bit word `0x1086`) |
| **Header reserved-zero** | `[0x11..0x13] ∪ [0x1D..0x1F] ∪ [0x20..0x3F]` (masks `0xFFFFFF00`, qword-OR) |
| **Payload reserved-zero** | `[0x0C..0x0E]` (mask `dword[0x0C] & 0x00FFFFFF`) |
| **Scratch tri-field** | `{byte[0x10], dword[0x18], byte[0x1C]}` — all-zero or all-nonzero; emitted **zero** this build |
| **Sync installer** | `AllocSemaphores::setupSync(Instruction*, vector<uint>, uint)` @ `0x1123ec0` |
| **SyncInfo location** | `getSyncInfo()` @ `libBIR 0x2d7dd0` → `*(Inst+0xD0) + 0x9F0` |
| **Field-name pool** | `.rodata` `0x1dd4640..0x1dd4700` (the assert strings) |

---

## The `NEURON_ISA_TPB_INST_UNION` as the validators read it

### Purpose

The validators are pure functions over a 64-byte value — the same on-wire record [11.5](customop-wire-layout.md) builds. They re-derive nothing from the IR; everything they check is a byte at a fixed union-relative offset. Pinning those offsets is the prerequisite for reading either predicate, because the masks in the disassembly are stated in stack-frame coordinates (`0x50(%rsp)` is union byte 0 in the non-dbg header) and have to be translated back to union offsets.

### Layout

The union is passed by value: in the non-dbg header it lands at `[rsp+0x50 .. rsp+0x8F]` (so `0x50(%rsp)` = union byte `0x00`); in the dbg twins it arrives as four `__m128i` arguments (`a7`=`[0x00..0x0F]`, `a8`=`[0x10..0x1F]` for the payload twin, etc.). The 64 bytes are the *generic* bundle skeleton shared with every Tonga ISA op; only the fields below `0x0C` differ between the header and payload records.

| Off | W | Field (validator view) | Checked by |
|---|---|---|---|
| `0x00` | 1 | opcode | `is_valid_enum(OPCODE=4, ·)` then `==0x85` / `==0x86` |
| `0x01` | 1 | header-tag (fixed `0x10`) | `byte[1]==0x10` |
| `0x02` | 2 | reserved (set 0 by `setupHeader`) | — (covered by tail rules / not re-checked) |
| `0x04` | 1 | SyncInfo wait **mode** | `has_valid_neuron_events`: `is_valid_enum(6,·)`, `≠0xFF` |
| `0x05` | 1 | SyncInfo wait **id** (hi byte of `i16[2]`) | `has_valid_neuron_events` (event-id gate) |
| `0x06` | 1 | SyncInfo update **mode** | `has_valid_neuron_events`: `is_valid_enum(7,·)`, `≠0xFF` |
| `0x07` | 1 | SyncInfo update **id** (hi byte of `i16[3]`) | `has_valid_neuron_events` (event-id gate) |
| `0x0C` | 2 | **HEADER**: `num_payloads` (u16) | (encoder-side; **PAYLOAD** zero-checks these bytes) |
| `0x0E` | 1 | **HEADER**: `CustomOpFunctionId` (u8) | (encoder-side) |
| `0x0F` | 1 | **HEADER**: `num_arguments` / **PAYLOAD**: const `0x01` | — (excluded from payload mask) |
| `0x10` | 1 | **HEADER**: scratch tri-field byte 0 | header §scratch (conditional) |
| `0x11` | 3 | **HEADER**: reserved-zero (hi 3 of `dword[0x10]`) | `(dword[0x10]\|dword[0x1C]) & 0xFFFFFF00 == 0` |
| `0x18` | 4 | **HEADER**: scratch tri-field `dword` | header §scratch (conditional) |
| `0x1C` | 1 | **HEADER**: scratch tri-field byte 2 | header §scratch (conditional) |
| `0x1D` | 3 | **HEADER**: reserved-zero (hi 3 of `dword[0x1C]`) | same `0xFFFFFF00` mask |
| `0x20` | 32 | reserved-zero tail (**HEADER**) / **PAYLOAD**: AccessPattern | header: `qword[0x20]\|…\|qword[0x38]==0` |
| `0x10..0x3F` | 48 | **PAYLOAD**: ADDR4 access pattern (base + per-dim steps) | **not** zero-checked |

> **GOTCHA —** the masks in the header disassembly are written in *stack* coordinates after a `rep movsl` that copies the union to `0x10(%rsp)`. `0x10(%rsp)` after that copy holds union byte `0x10`, *not* union byte 0 — the copy preserves the union's internal offsets. So `v25.m128i_i32[3]` in the decompile (`= dword at copy+0x1C = union dword[0x1C]`) and `(unsigned int)a9` (`= union dword[0x10]`) are the two dwords the `0xFFFFFF00` mask covers. Translate frame offsets back to union offsets before believing any field name.

> **NOTE —** the SyncInfo overlay at `[0x04..0x07]` is the generic per-instruction sync band, shared with every opcode — not custom-op-specific. `has_valid_neuron_events` (`0x127eb60`) reads `byte[4]` (wait mode) and `byte[6]` (update mode) — confirmed by the `movzbl 0x74(%rsp)` / `movzbl 0x76(%rsp)` pair (union at `0x70(%rsp)` there) each feeding `is_valid_enum` and a `cmp $0xff` — plus the `i16` event ids at `[4..5]` and `[6..7]`. This is the band the §sync handshake writes into.

---

## `is_valid_custom_op_header` — opcode `0x85`

### Purpose

The header validator gates a `CUSTOM_OP_HEADER` word: it confirms the opcode and tag, the SyncInfo events, the reserved-zero discipline over every byte the header does not populate, and the consistency of the scratch tri-field. It is the wire-integrity guard run in the ISA-check pass; a single failed predicate returns `false` and (in the dbg twin) names the offending field.

### Entry Point

```text
is_valid_custom_op_header (0x129b940, 327 B)   ── real body ⭐
  ├─ is_valid_enum (0x127ab90)                 ── OPCODE enum-table gate (arg=4)
  ├─ has_valid_neuron_events (0x127eb60)       ── SyncInfo [0x04..0x07] gate
  └─ (inline) reserved-zero masks + scratch tri-field gate
dbg_is_valid_custom_op_header (0x129ba90)      ── twin: same gates + reason-string build
  └─ thunks: 0x61ba10 (non-dbg), 0x5eb1e0 (dbg)
```

### Algorithm

```c
bool is_valid_custom_op_header(NEURON_ISA_TPB_INST_UNION u):   // 0x129b940
    // (1) OPCODE-enum + header-tag gate            [0x129b94a..0x129b95e]
    if (!is_valid_enum(OPCODE=4, u[0]))          return false; // call is_valid_enum
    if (u[1] != 0x10)                            return false; // cmpb 0x10, byte[1]
    // (2) shared SyncInfo events gate              [0x129b970..0x129b9a8]
    if (!has_valid_neuron_events(u))             return false; // pushes the 64B, calls
    // (3) opcode VALUE gate                         [0x129b9b1: cmp bl,0x85]
    if (u[0] != 0x85)                            return false; // bl == -123
    // (4) RESERVED-ZERO discipline                  [0x129b9bb..0x129ba18]
    //     hi-3 bytes of dword[0x10] and dword[0x1C] must be 0
    if ((u.dword[0x10] | u.dword[0x1C]) & 0xFFFFFF00) return false;   // and edx,0xffffff00
    //     the whole 32-byte tail [0x20..0x3F] must be 0
    if (u.qword[0x20]|u.qword[0x28]|u.qword[0x30]|u.qword[0x38])      return false;
    // (5) SCRATCH-SPACE tri-field consistency        [0x129ba30..0x129ba72]
    //     scratch present iff ANY subfield nonzero:
    //       (qword[0x10] & 0xFFFFFFFF000000FF) | (qword[0x18] & 0xFFFFFFFFFF)
    if ((u.qword[0x10] & 0xFFFFFFFF000000FF) | (u.qword[0x18] & 0xFFFFFFFFFF)) {
        if (u.byte[0x10] == 0 || u.dword[0x18] == 0 || u.byte[0x1C] == 0)
            return false;        // half-filled descriptor → reject
    }                            // else all-zero scratch → OK
    return true;
```

> **QUIRK —** the scratch gate is *all-zero or all-nonzero*, not a range check. The `(qword[0x10] & 0xFFFFFFFF000000FF) | (qword[0x18] & 0xFFFFFFFFFF)` mask answers "is the scratch descriptor non-empty?"; if so, all three subfields (`byte[0x10]`, `dword[0x18]`, `byte[0x1C]`) must be nonzero. A reimplementation that validates the scratch field as a plain integer will wrongly accept a `{size, addr=0}` half-descriptor — the binary forbids exactly that.

### The five named predicates

The dbg twin (`0x129ba90`) builds an `inst failed assertion check: '<field>'` string per failed predicate. Three of the field names are custom-op-specific, verbatim in `.rodata`; two are the shared enum/events predicates.

| Predicate | Field name (`.rodata` off) | Confidence |
|---|---|---|
| (3) `u[0]==0x85` | `is_custom_op_header_opcode` (`0x1dd4640` region) | CERTAIN |
| (5) scratch tri-field | `custom_op_header_scratch_space_val` (`0x1dd467e`) | CERTAIN |
| (4) reserved == 0 | `custom_op_header_reserved_zero` (`0x1dd46a0` region) | CERTAIN |
| (1) enum / inst-len | shared `is_valid_enum` predicate strings | HIGH |
| (2) SyncInfo events | shared `has_valid_*` predicate strings | HIGH |

> **NOTE — reading the three names out of the assert pool.** They are present byte-for-byte: `custom_op_header_scratch_space_val` begins at file offset `0x1dd467e`, immediately preceded by the assert template `…rtion check: '`. The pool packs the names with `'` terminators and reuses prefixes, so a naive string-search snags the next name's head; the per-predicate boundaries above are taken from the `xmmword` loads in the dbg twin's reason-string branches (`xmmword_1DD4670`/`1DD46A0` for the header field strings).

### The reserved-zero region

```text
custom_op_header_reserved_zero =
    [0x11,0x12,0x13]   (hi 3 bytes of dword@0x10 — low byte[0x10] EXCLUDED: scratch)
  ∪ [0x1D,0x1E,0x1F]   (hi 3 bytes of dword@0x1C — low byte[0x1C] EXCLUDED: scratch)
  ∪ [0x20 .. 0x3F]     (the entire 32-byte tail)
```

Everything in the bundle **except** `{opcode, header-tag, the `[0x04..0x0B]` SyncInfo band, the `[0x0C..0x0F]` count/id block, and the scratch triple `[0x10]`/`[0x18..0x1B]`/`[0x1C]`}` must be zero. This is the classic ISA "no stray bits" integrity check: any set bit in a reserved field means a malformed bundle.

> **GOTCHA — the reserved-zero region is not the whole `[0x10..0x3F]` tail.** `byte[0x10]`, `dword[0x18..0x1B]`, and `byte[0x1C]` are the scratch tri-field, masked *out* of the reserved check: `0xFFFFFF00` keeps only the hi-3 bytes of each dword, and `byte[0x10]`/`byte[0x1C]` are low bytes. Zero-checking the full tail rejects every header that carries a scratch descriptor.

---

## `is_valid_custom_op_payload` — opcode `0x86`

### Purpose

The payload validator gates a `CUSTOM_OP_PAYLOAD` word — the output AP first, then one per argument AP. It shares the header's opcode/tag/events front-end but moves the reserved-zero mask to the three bytes the payload leaves blank, and it does **not** zero-check the access-pattern tail (those bytes carry the live tensor descriptor).

### Entry Point

```text
(non-dbg is_valid_custom_op_payload)   ── INLINED into the per-opcode validity dispatch
                                          (no standalone symbol in cp310)
dbg_is_valid_custom_op_payload (0x129c610)   ── twin: predicate 1:1 + reason-string ⭐
  ├─ sub_1205930                         ── is_valid_enum dispatch wrapper
  └─ dbg_has_valid_neuron_events         ── SyncInfo gate (diagnostic form)
  └─ thunk: 0x6233c0
```

### Algorithm

```c
bool is_valid_custom_op_payload(NEURON_ISA_TPB_INST_UNION u):   // dbg twin 0x129c610
    if (!is_valid_enum(OPCODE=4, u[0]))      return false;      // (1)
    if (u[1] != 0x10)                        return false;      // (1) header-tag
    if (!has_valid_neuron_events(u))         return false;      // (2)
    if (u[0] != 0x86)                        return false;      // (3) v10 == -122
    // (4) reserved-zero: bytes [0x0C, 0x0D, 0x0E] must be 0
    if ((u.dword[0x0C] & 0x00FFFFFF) != 0)   return false;      // (v28.i32[3] & 0xFFFFFF)
    return true;
```

In the dbg twin the gate is literally `(v28.m128i_i32[3] & 0xFFFFFF) == 0`, where `v28 = u[0..15]` and `i32[3]` is `dword[0x0C..0x0F]`; the mask `0x00FFFFFF` keeps bytes `[0x0C,0x0D,0x0E]` and drops `[0x0F]`.

| Predicate | Field name (`.rodata` off) | Confidence |
|---|---|---|
| (3) `u[0]==0x86` | `is_custom_op_payload_opcode` (`0x1dd46c2`) | CERTAIN |
| (4) `[0x0C..0x0E]==0` | `custom_op_payload_reserved_zero` (`0x1dd46e2` region) | CERTAIN |

> **GOTCHA — the payload reserved-zero is `[0x0C..0x0E]`, three bytes, not four.** `byte[0x0F]` is the constant `0x01` "payload-present / has-AP" marker ([11.5](customop-wire-layout.md)), deliberately excluded from the `0x00FFFFFF` mask. Bytes `[0x10..0x3F]` are the ADDR4 access pattern (tensor base address + per-dimension steps) — live data, not zero-checked.

> **QUIRK —** the header and payload reserved regions are *complementary*. The header populates `[0x0C..0x0F]` (counts + FnId) and therefore zero-checks the tail; the payload populates the tail (the AP) and therefore zero-checks `[0x0C..0x0E]`. Same opcode/tag/events front-end, mask shifted to whichever bytes each record type leaves untouched. There is **no** second sub-opcode inside the payload body — the payload's identity is entirely `byte[0]==0x86` plus its positional rule (first `0x86` after a `0x85` is the output AP; the rest are args).

> **NOTE — `is_valid_custom_op_payload` has no standalone symbol in cp310.** It is inlined into the per-opcode validity dispatch, so the dbg twin at `0x129c610` is the authoritative source for its predicate: it preserves the opcode/reserved checks 1:1 and adds the reason-string build. The header validator keeps both forms; the payload's non-dbg form exists only as inlined code.

---

## The scratch / reserved fields

### `custom_op_header_scratch_space_val` — the wire field

A tri-part field in the `0x85` header that the validator (predicate 5) treats as a unit: `{byte[0x10], dword[0x18], byte[0x1C]}`. The validator's non-empty test masks `qword[0x10] & 0xFFFFFFFF000000FF` (= `byte[0x10]` + `dword[0x14..0x17]`) `| qword[0x18] & 0xFFFFFFFFFF` (= low 5 bytes of `[0x18]`); if non-empty, all three of `byte[0x10]` / `dword[0x18]` / `byte[0x1C]` must be nonzero. Semantically the field is the wire slot for the SBUF scratch space a custom op reserves on the CPU side; the gate forbids a half-filled `{addr, size, valid-flag}` triple.

> **QUIRK —** in *this* build's `GENERATE_ISACODE` emission, the encoder `CoreV2GenImpl::visitInstCustomOp` (`0x12613c0`) writes `byte[0x10] = 0` (decompile line 827: `*((_BYTE*)v119 + 16) = 0`) and zero-fills `[0x11..0x3F]`. It never populates the scratch triple, so the validator's **all-zero branch is always taken** — the emitted custom-op headers carry no scratch descriptor. [INFERRED] The non-zero branch reads as a validated-for-but-not-emitted wire-format slot held for a future or optional reservation; the byte store itself is read directly from the encoder.

### The SB-scratchpad bookkeeping (IR level)

The actual SBUF scratchpad a custom op uses is tracked at the IR level, not in the `0x85` header bytes. `bir::isCCHavingSBScratchPad` (`libBIR 0x312700`; thunk `0x175fb0`) decides whether an instruction owns one:

```c
bool isCCHavingSBScratchPad(Instruction const& I):   // libBIR 0x312700
    if (I.kind    @I+0x58 != 0x30)        return false;   // 0x30 = CustomOp IR kind
    if ((I.subkind @I+0xF8) - 2 > 1u)     return false;   // subkind ∈ {2,3}
    if (!isSBArgGlobalCCOp(I))            return false;   // a "global CC" SB op
    n_args    = list_len(I+0xA8, sentinel I+0xA0);        // base @+21, end @+20
    n_outputs = list_len(I+0xB8, sentinel I+0xB0);        // base @+23, end @+22
    n_scratch = list_len(I+0xC8, sentinel I+0xC0);        // base @+25, end @+24
    return (n_args + 2 + n_outputs) == n_scratch;         // the "+2" gate
```

A custom op "has an SB scratchpad" iff it carries exactly `(n_args + n_outputs + 2)` scratch `MemoryLocation`s. The `+2` mirrors the wire-format `num_payloads = n_args + 2` ([11.5](customop-wire-layout.md): header + output + N args): one SB buffer per wire record. The third list (`@I+0xC8`) is the per-op scratch-buffer set; `bir::MemoryLocation::isCustomOpRelated()` tags each such location as custom-op scratch.

> **NOTE —** the SB scratch is materialized as `MemoryLocation`s (allocated by the SB allocator), **not** as a numeric size in the `0x85` header. The header's `scratch_space_val` slot is the wire encoding of that reservation when the runtime needs an explicit handle; in this build the scratch is conveyed structurally (the extra scratch `MemoryLocation`s) and the header field stays zero. The IR count gate is read from the `0x312700` body; [INFERRED] the wire-versus-structural mapping.

### Reserved-zero and the opcode names, recapped

```text
custom_op_header_reserved_zero  = [0x11..0x13] ∪ [0x1D..0x1F] ∪ [0x20..0x3F]   // must be 0
custom_op_payload_reserved_zero = [0x0C..0x0E]                                   // must be 0
is_custom_op_header_opcode      = predicate byte[0] == 0x85 (133)
is_custom_op_payload_opcode     = predicate byte[0] == 0x86 (134)
```

The "payload_opcode" is the same `byte[0]` slot as the header opcode, distinguished only by value `0x86` vs `0x85`; it is a named *assert predicate*, not a separate sub-opcode field. The encoder guarantees the reserved-zero regions by zero-filling the 64-byte array (four `movaps`/`pxor` of `xmm0`) before stamping the live fields.

---

## The sync handshake — host engine ↔ GPSIMD-CPU

### Purpose

The custom-op host ↔ CPU synchronization is the generic BIR semaphore machinery (`neuronxcc::backend::AllocSemaphores`, the `alloc_semaphores` pass), applied to the `InstCustomOp` like any other instruction. It installs a `Wait` (block until inputs are ready) and an `Update` (signal on completion) into the instruction's SyncInfo; those land in the bundle's `[0x04..0x07]` overlay that `has_valid_neuron_events` checks. The handshake primitive it builds is a **1:1 producer-update → consumer-wait** pairing on one semaphore id.

> **GOTCHA — there is no `CUSTOM_OP`-templated sync symbol.** Anything written as `setupSyncWait<CUSTOM_OP_*>` / `setupSyncUpdate<CUSTOM_OP_*>` maps onto the generic `sub_1122190`/`sub_1123520` (Wait) and `sub_1123A30` (Update) operating on the custom op's `SyncInfo`. It is the generic `SyncInfo`/`Wait`/`Update` machinery applied to a custom-op instruction, not a specialized template instantiation.

### The SyncInfo object

```c
SyncInfo& getSyncInfo():   // libBIR 0x2d7dd0
    return *(this + 0xD0) + 0x9F0;   // *((_QWORD*)this + 26) + 2544
```

`SyncInfo` lives at `(Inst → InstructionImpl @Inst+0xD0) + 0x9F0`. Its layout, read from `addSyncWait` `0x112bdf0` + `sub_1122190`:

| Offset (from SyncInfo) | Field |
|---|---|
| `+0x00` | `bool engaged` (`boost::optional<SyncInfo>` has-value) |
| `+0x08` | `Wait* waits.begin` (`SmallVector<sync::Wait>`, **stride 40**) |
| `+0x10` / `+0x14` | `waits.size` / `waits.capacity` |
| `+0x40` | `Update* updates.begin` (`SmallVector<sync::Update>`, **stride 32**) |
| `+0x48` | `updates.size` |

```text
bir::sync::Wait   (40 B):  +0x00 vtable | +0x08 SyncType | +0x0C id(sem) |
                           +0x10 WaitMode | +0x14 value | +0x18 from-Instruction*
bir::sync::Update (32 B):  +0x00 vtable | +0x08 SyncType | +0x0C id |
                           +0x10 UpdateMode | +0x14 value |
                           +0x18 targetCore: boost::optional<uint> (engaged ⇒ REMOTE)
```

### Algorithm — `setupSync(Instruction*, vector<uint> sems, uint mult)`

```c
result setupSync(Instruction* I, vector<uint> sems, uint mult):   // 0x1123ec0
    if (I == nullptr)                                     // alloc_semaphores.cpp:214
        throw NeuronAssertion("I != nullptr");
    local = copy(sems);                                   // tc_new + memmove
    setupSyncWait(I, &local);                             // sub_1122190 — install Wait(s)
    for (semId in sems):                                  // while v28 != v15
        incrementSemaphore(this, I, semId, mult);         // 0x1120fc0 — bump expected value
        rec = find_or_insert(this+0x168 /*rotation RB-tree*/, semId);   // sub_1120E30
        setupSyncUpdate(I, semId, rec.field9 /*v18[9]*/, …);            // sub_1123A30
    return …
```

`setupSync` = install `Wait` + per-sem `(incrementSemaphore + install Update)`. The `Wait` makes the instruction block until the producer signals; the `Update` makes the instruction signal the consumer's semaphore on completion. The rotation RB-tree at `this+0x168` keys a per-semaphore record whose `field9` (`v18[9]`) feeds the `Update`.

### `incrementSemaphore` — the running-value map

```c
void incrementSemaphore(Instruction* I, uint semId, uint delta):   // 0x1120fc0
    // map<semId → expected value> at AllocSemaphores+0x168
    guard_overflow(value[semId], delta);    // alloc_semaphores.cpp:207
    value[semId] += delta;
```

This tracks the cumulative semaphore count so each `Update` gets the correct value and each `Wait` targets the right threshold. For the DMA path `delta = (num records) · mult` — one tick per wire record.

### `setupSyncWait` — the 1:1 invariant

`sub_1122190` (DMA-block form) / `sub_1123520` (Instruction twin) install a `bir::sync::Wait` into `I.getSyncInfo().waits` via `addSyncWait<uint>` (`0x112bdf0` → `bir::sync::Wait::Wait(SyncType, id, WaitMode, value, from)` @ `libBIR 0x3dfd50`). It **also validates the producer's update side**:

```c
// sub_1122190, the producer-side checks:
n_local = count(SI.OnUpdate where !Update::isRemote(u));   // v10 += (isRemote(u)==0)
assert(n_local == 1,              "hasSingleLocalUpdate(SI.OnUpdate)");   // :171
assert(getLocalSemaphore(SI.OnUpdate) == semaphore,                       // :172
       "getLocalSemaphore(SI.OnUpdate) == semaphore");
```

> **QUIRK —** the consumer's `Wait` is wired to the *exact* semaphore the producing instruction updates, and the producer must have **exactly one** non-remote (local) `Update`. The two asserts (`alloc_semaphores.cpp:171` `hasSingleLocalUpdate`, `:172` `getLocalSemaphore == semaphore`) make this a strict 1:1 producer-update → consumer-wait pairing on a single semaphore id. This is the handshake primitive: the custom op's consumer waits on the same semaphore the GPSIMD CPU (or the producing DMA) updates. A reimplementation that allows fan-out updates on a shared semaphore will trip both asserts.

### `setupSyncUpdate` — spread to all consumers

`sub_1123A30` installs a `bir::sync::Update` and **propagates** it: it walks `I.realDescendents()` (the transitive successor instructions) and adds the `Wait`/`Update` to each, so every real successor that consumes `I`'s output observes the semaphore. The underlying sink is `bir::SyncInfo::addUpdate(SyncType, id, UpdateMode, value, optional<uint> targetCore)`; `targetCore` engaged ⇒ a **remote** update (the cross-core / DMA-carried variant that [8.34](../walrus/vnc-cross-core-link.md) relinks). For a same-engine custom-op completion the `targetCore` is empty (local update).

### Semaphore id allocation

`allocateSemaphore(bool fromTop)` (`0x1121480`) hands out a fresh HW semaphore id from a finite pool — either the bottom counter (`AllocSemaphores+0x28`, `++`) or the top (`+0x2C`, `--`) — asserting `currSemaphore < currSemaIDFromUpperBound < HwmCore->NumSemaphores` (`alloc_semaphores.cpp:256`; `HwmCore->NumSemaphores` @ `HwmCore+0x34`). The host ↔ CPU handshake semaphores come from the **same** finite HW pool as every other engine sync; the custom-op completion semaphore is one allocated id, written into the header's update event bytes `[0x06..0x07]` and waited-on by the consumer's `[0x04..0x05]`. Those bytes are the SyncInfo overlay described under events; the concrete id is whatever `allocateSemaphore` returns.

### Function map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `setupSync(Instruction*, vector<uint>, uint)` | `0x1123ec0` | host-side installer: Wait + per-sem (incr + Update) | CERTAIN |
| `setupSync(InstDMABlock*, uint, vector const&, uint)` | `0x11243b0` | DMA-block twin (data-staging sync) | CERTAIN |
| `sub_1122190` | `0x1122190` | setupSyncWait (DMA): install + validate 1:1 | CERTAIN |
| `sub_1123520` | `0x1123520` | setupSyncWait (Instruction twin) | HIGH |
| `sub_1123A30` | `0x1123A30` | setupSyncUpdate: spread across realDescendents | CERTAIN |
| `incrementSemaphore` | `0x1120fc0` | running expected-value map | CERTAIN |
| `allocateSemaphore` | `0x1121480` | fresh HW sem id from finite pool | CERTAIN |
| `addSyncWait<uint>` | `0x112bdf0` | the Wait-ctor sink | CERTAIN |
| `AllocSemaphores::run(Module&)` | `0x1126d30` | pass driver | HIGH |
| `getSyncInfo` | `libBIR 0x2d7dd0` | SyncInfo locator `*(I+0xD0)+0x9F0` | CERTAIN |
| `sync::Update::Update` | `libBIR 0x3e02c0` | Update ctor (with `optional targetCore`) | CERTAIN |

---

## The full host ↔ GPSIMD-CPU sequence

Composing the encoder ([11.5](customop-wire-layout.md)), these validators, the CPU image ([the GPSIMD CPU ABI](customop-cpu-abi.md)), and the cross-core link ([8.34](../walrus/vnc-cross-core-link.md)):

```text
COMPILE-TIME (libwalrus, per InstCustomOp):
 (C1) penguin.ir.CustomOp → bir::InstCustomOp: opLibFile + opFunctionName +
      arg/output AccessPatterns + the SB scratch MemoryLocations
      (isCCHavingSBScratchPad bookkeeping).
 (C2) AllocSemaphores::run (0x1126d30): allocateSemaphore hands out the completion
      sem id; setupSync (0x1123ec0) stamps a Wait + Update into the InstCustomOp
      SyncInfo. The tensor-staging DMAs get setupSync(InstDMABlock*) (0x11243b0) —
      their Updates are the "data arrived" / "result written" signals.
 (C3) CoreV2GenImpl::visitInstCustomOp (0x12613c0) emits the 64-byte bundle GROUP:
        0x85 HEADER:  [0]=0x85,[1]=0x10; SyncInfo→[0x04..0x07]; [0x0C..0x0D]=num_payloads,
                      [0x0E]=FunctionId, [0x0F]=num_arguments, [0x10]=0 (scratch slot zero),
                      [0x11..0x3F]=0.
        0x86 OUTPUT:  [0x0F]=1, [0x10..0x3F]=output AccessPattern.
        N×0x86 ARG:   [0x0F]=1, [0x10..0x3F]=arg[k] AccessPattern.
 (C4) VALIDATION (ISA-check): is_valid_custom_op_header + is_valid_custom_op_payload
      gate each bundle (opcode/tag/events/reserved-zero/scratch). checkCustomOp
      (0xfdbf90) separately gates the IR: ≤1 output, args/outputs in SBUF|HBM,
      Arch ∈ {Sunda, Tonga}.

RUN-TIME (the TPB engine + 8 GPSIMD/Xtensa cores):
 (R1) The issuing TPB engine decodes the 0x85 HEADER: byte[0x0E]=FunctionId selects
      the registered (lib, function); [0x0C..0x0D]/[0x0F] tell it how many 0x86 follow.
 (R2) Engine WAITs on header bytes [0x04..0x05] — blocks until input-staging DMAs
      signal their completion semaphores.
 (R3) Runtime launches the GPSIMD CPU op, builds at::Tensors over the windowed bytes
      from the 0x86 APs, calls the named compute fn directly (no dispatcher), splitting
      across ≤8 cores. [Xtensa-side detail — INFERRED at this granularity.]
 (R4) On completion the GPSIMD side writes the output back through the window; the
      result-DMA UPDATEs the completion semaphore (header [0x06..0x07]) — the single
      local update sub_1122190 validated (getLocalSemaphore(OnUpdate)==semaphore).
 (R5) The next consumer's Wait (set up by setupSyncUpdate's realDescendents spread)
      observes that semaphore and proceeds.
```

> **NOTE —** the handshake in one line: host engine `Wait` (header `[0x04..05]`) ← input-DMA `Update`; GPSIMD-CPU compute; result-DMA `Update` → header `[0x06..07]` → consumer `Wait`. For a multi-core (LNC) custom op the cross-core `Update`s are remote (`targetCore` set) and the VNC link ([8.34](../walrus/vnc-cross-core-link.md)) relinks them to the peer core's concrete semaphore id — the same `Wait`/`Update` primitive, cross-core. The wire fields, sync structs, and validator gates are all read from `libwalrus`; [INFERRED] the `(R3)` runtime launch immediates, which are Xtensa-side and absent from this binary.

---

## Confidence ledger

| Claim | Evidence | Confidence |
|---|---|---|
| Binary identity (sha1 `4531c8f2…`, 64,973,024 B) | `sha1sum` of the cp310 `libwalrus.so` | CERTAIN |
| Header validator full predicate @ `0x129b940` | `objdump -d` + IDA decompile (masks `0xFFFFFF00`, qword-OR, scratch masks) | CERTAIN |
| Payload predicate @ `0x129c610` (opcode `0x86`, `dword[0x0C]&0xFFFFFF==0`) | IDA decompile (`v10==-122`, `v28.i32[3]&0xFFFFFF`) | CERTAIN |
| Five field-name strings | `.rodata` `0x1dd4640..0x1dd4700` verbatim | CERTAIN |
| `has_valid_neuron_events` reads `byte[4]`/`byte[6]` | `objdump` (`movzbl 0x74/0x76(%rsp)`, `is_valid_enum`, `cmp 0xff`) | CERTAIN |
| Encoder writes `byte[0x10]=0` | `visitInstCustomOp` decompile line 827 | CERTAIN |
| `isCCHavingSBScratchPad` count gate `(n_args+2+n_outputs)==n_scratch` | `libBIR 0x312700` body (kind `0x30`, subkind `{2,3}`) | CERTAIN |
| `checkCustomOp`: ≤1 output, SBUF\|HBM, `Arch ∈ {Sunda,Tonga}` | `0xfdbf90` decompile (assert strings) | CERTAIN |
| `getSyncInfo` = `*(I+0xD0)+0x9F0`; Wait stride 40, Update stride 32 | `libBIR 0x2d7dd0` + `addSyncWait`/`sub_1122190` | CERTAIN |
| `setupSync` body, asserts :171/:172/:207/:214/:256 | `0x1123ec0`/`sub_1122190` decompile (verbatim strings) | CERTAIN |
| Scratch tri-field is a future/optional slot, unpopulated this build | the byte store is exact; the semantic role is read from context | HIGH |
| `Arch` enum values Sunda=20 / Tonga=10 | not located in the enums sidecar | HIGH |
| `(R3)` runtime launch / window-DMA immediates | Xtensa-side, outside `libwalrus` | MEDIUM |

> **NOTE —** the `core_v2` validators have `core_v3` and `core_v4` siblings (`is_valid_enum` @ `0x13696f0`/`0x143f910`, `has_valid_neuron_events` @ `0x136e390`/`0x14459b0`) for later hardware generations, plus a `dbg_has_valid_neuron_events_extended` form. This page documents the `core_v2` (Tonga/Sunda) path that the `0x85`/`0x86` custom-op encoder targets; the other generations' validators are structurally parallel but were not traced here.

---

## Related Components

| Name | Relationship |
|---|---|
| `CoreV2GenImpl::visitInstCustomOp` (`0x12613c0`) | the encoder that produces the bytes these validators check |
| `birverifier::checkCustomOp` (`0xfdbf90`) | the IR-level gate (≤1 output, placement, Arch) run alongside the wire validators |
| `AllocSemaphores::run` (`0x1126d30`) | the pass that calls `setupSync` for every instruction including the custom op |
| `BirLinker::symLinkCustomOpFiles` (`0x15d6460`) | resolves the `FunctionId` → CPU lib/function at link time |

## Cross-References

- [CUSTOM_OP Wire Byte-Layout (0x85 / 0x86)](customop-wire-layout.md) — 11.5, the encoder side: how the bytes these validators check are written
- [The Custom-Op CPU ABI](customop-cpu-abi.md) — the GPSIMD CPU side that the completion semaphore signals
- [The GPSIMD CPUs: 8-core Xtensa ELF Layout](gpsimd-xtensa-layout.md) — the cores the host engine hands off to
- [Execution & Sync Model — Semaphores & Barriers](../arch/execution-sync-model.md) — 1.14, the generic semaphore / SyncInfo model `setupSync` builds on
- [The VNC Cross-Core Link](../walrus/vnc-cross-core-link.md) — 8.34, how remote `Update`s are relinked to peer-core semaphores for multi-core custom ops
