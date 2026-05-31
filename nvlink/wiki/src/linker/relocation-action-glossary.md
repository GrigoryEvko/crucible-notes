# Relocation Action Glossary

The nvlink relocation pipeline uses two distinct numeric enumerations that are routinely conflated in informal descriptions. The first is the **descriptor `action` byte** stored at offset 20 of every 64-byte descriptor record in `off_1D3DBE0` (R_CUDA) and `off_1D3CBE0` (R_MERCURY), holding values in the range 0..56. The second is the **dispatcher operation code** that the application engine `sub_468760` switches on -- also stored at offset 20 inside each 16-byte action slot, but with a different value space that includes the aliases 0x12, 0x2E, 0x37, 0x38 and the 16 masked-shift codes 0x16..0x1D and 0x2F..0x36. The two spaces overlap on numeric values but diverge in semantics: a descriptor `slot0.action = 1` denotes a 64-bit absolute write at the encoded `(bit_offset, bit_width)`, while a dispatcher case for `0x01` is one of three aliases that all map to the same fast-path branch. This page catalogues every numeric value the application engine accepts, plus every value the descriptor table actually stores, and explains the relationship between the two enumerations.

The decompiled source for the dispatcher is `sub_468760` (renamed `reloc_apply_engine` in [Relocation Application Engine](relocation-engine.md)). The descriptor table layout is documented in [R_CUDA Relocation Catalog](../reference/r-cuda-catalog.md#action-enum-dword-at-byte-20) and the per-type breakdown lives in [R_CUDA Relocations § Action Types](r-cuda-relocations.md). Both tables share the descriptor format -- the only difference is which table the engine indexes, gated by `r_type & 0x10000`.

## At a Glance

| Property | Value |
|---|---|
| Total dispatcher case values | 31 acknowledged opcodes across 11 case-body branches (5 are alias values that fall through to other branches) |
| Total descriptor action values used | 23 (per R_CUDA catalog enum 0..56) |
| Dispatcher slots per descriptor | 3 (offsets +12, +28, +44; sentinel at +60) |
| Action stride | 16 bytes per slot (4 x uint32) |
| Sentinel value | Address pointer, not a magic byte |
| Unrecognized action behavior | `sub_468760` returns 0 → caller emits "unexpected NVRS" |

The dispatcher's switch statement covers values 0x00, 0x01, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x10, 0x12, 0x13, 0x14, 0x16..0x1D (8 values), 0x2E, 0x2F..0x36 (8 values), 0x37, 0x38. Every other byte in `0x00..0xFF` falls through to `default: return 0;`. The descriptor table, in contrast, uses a denser enumeration 0..56 where action values 10, 11, 21, 22..29, 30..37, 38..45, 47..54, 55, 56 carry semantic meaning ("continuation piece", "8-bit data patch at byte 0", etc.) that the dispatcher itself does not see -- those semantics are unpacked by the apply-class dispatcher `sub_4698A0` and the symbol-resolution gate `sub_469620`, not by `sub_468760`.

## Section 1 -- Dispatcher Opcode Table (sub_468760 switch arms)

The following enumeration is the *complete* set of values that `sub_468760` recognizes. Anything not in this table is rejected with return code 0 at the `default:` arm.

### 1.1 Terminator and No-op

| Code | Mnemonic | Behavior |
|---:|---|---|
| 0x00 | END | Advance `action_ptr += 4` (16 bytes). If `action_ptr == sentinel` return 1; otherwise continue to next slot. Zero-action slots are not skipped silently -- the engine still enters the case body to perform the advance-and-compare. |

This is the only opcode that always succeeds without touching the target word. Slot[1] and slot[2] of single-action descriptors are filled with `action_type = 0`, so most relocations dispatch through this case twice before reaching the sentinel.

### 1.2 Absolute Address Writers

| Code | Mnemonic | Value computation | Notes |
|---:|---|---|---|
| 0x01 | ABS_FULL | `S + A` | Default absolute write. Fast path when `bit_offset == 0 && bit_width == 64`: direct 64-bit word store bypassing all bit-field logic. |
| 0x12 | ABS_FULL alias 1 | identical | Same case body as 0x01. |
| 0x2E | ABS_FULL alias 2 | identical | Same case body as 0x01. |
| 0x06 | ABS_LO | `(uint32_t)(S + A)` | Low 32 bits of relocation value, written into a `(bit_offset, bit_width)` field. |
| 0x37 | ABS_LO alias | identical | Same case body as 0x06. |
| 0x07 | ABS_HI | `(uint32_t)((S + A) >> 32)` | High 32 bits of relocation value. |
| 0x38 | ABS_HI alias | identical | Same case body as 0x07. |
| 0x08 | ABS_SIZE | `extra_offset + symbol_size` (when `is_absolute`) or `extracted_old + symbol_size` (otherwise) | Overwrites the running value: the case body discards the `symbol_value` (and the optional `+ extra_offset` from prologue) and emits `record.extra + size` or `extracted_old + size`. Used for relocations against array-typed symbols where the patched field must carry the symbol's byte size combined with an addend stored in `record.extra` (when absolute) or pre-encoded in the instruction field (otherwise). |

Three of the seven entries here are aliases -- 0x12, 0x2E, 0x37, 0x38 all share case bodies with their lower-numbered siblings. The aliases exist because the descriptor table at `off_1D3DBE0` packs three semantically-distinct relocation families into the action-byte space: standard, attribute, and unified. Each family has its own numerical "ABS_FULL" code (`1` for standard, `0x12` = 18 for attribute, `0x2E` = 46 for unified). The dispatcher fuses them into one branch to keep the binary compact -- a single bit-field write subroutine serves all three callers.

### 1.3 Shifted and PC-Relative

| Code | Mnemonic | Value computation |
|---:|---|---|
| 0x09 | SHIFTED_2 | `(S + A) >> 2` -- byte offset to DWORD index |
| 0x10 | PC_REL | `(int32_t)(S + A) - section_offset` |

`SHIFTED_2` converts a byte address to a 32-bit-aligned DWORD index, used by relocation types whose target instruction field encodes a DWORD offset rather than a byte offset (notably `R_CUDA_TEX_BINDLESSOFF13_32` and the leading slot of the split-field `R_CUDA_ABS55_16_34` / `R_CUDA_ABS56_16_34` types). `PC_REL` produces a section-relative branch displacement: the engine sign-extends the running value to 32 bits, then subtracts the patch-site offset within its section. Gating logic in `sub_469D60` line 309 (`*((_DWORD *)v24 + 5) == 16`) checks `slot0.action == 16` and routes the relocation through a same-section validation path that emits `"PC relative branch address should be in the same section"` if the symbol resolves outside the current section.

### 1.4 Section-Type Encoders

| Code | Mnemonic | Value computation |
|---:|---|---|
| 0x0A | SEC_TYPE_LO | `section_type_delta & (255 >> (8 - bit_width))` |
| 0x0B | SEC_TYPE_HI | `(section_type_delta >> 4) & (255 >> (8 - bit_width))` |

These encode the low and high nibbles of `section_type - 0x70000064` into instruction immediate fields. The constant `0x70000064` (= `1879048292` decimal) is the value of `SHT_CUDA_CONSTANT0`; the engine subtracts it to map NVIDIA's constant-bank section types (`0x70000064`..`0x7000007E`, i.e. `SHT_CUDA_CONSTANT0`..`SHT_CUDA_CONSTANT26`) into a small unsigned offset that fits into 4 bits per nibble. The exact subtraction `*(_DWORD *)(v25 + 4) - 1879048292` appears in the caller `sub_469D60` at the point that invokes `sub_468760`. The pair is used in tandem -- a single descriptor entry typically has `action[0] = SEC_TYPE_LO` and `action[1] = SEC_TYPE_HI` writing to different bit positions of the same instruction word.

> **QUIRK -- SEC_TYPE encoders use the constant-bank base, not `SHT_LOPROC` or `SHT_LOOS`.** Standard ELF defines `SHT_LOPROC = 0x70000000` and `SHT_LOOS = 0x60000000`. NVIDIA could have re-based the encoded value at either standard boundary, but instead chose the `SHT_CUDA_CONSTANT0` value `0x70000064` -- because the only section types that ever reach these dispatch arms are constant banks. The choice keeps the encoded nibble small (0..0x1A for the 27 banks 0..26) and the high nibble at zero for bank ids 0..15, matching the instruction-field widths the assembler reserves for bank selectors.

### 1.5 Field Clearers

| Code | Mnemonic | Behavior |
|---:|---|---|
| 0x13 | CLEAR | Write `0` into `(bit_offset, bit_width)`. No symbol resolution, no addend, no extraction. |
| 0x14 | CLEAR alias | Same case body as 0x13. |

`CLEAR` is the action backing `R_CUDA_UNUSED_CLEAR32` and `R_CUDA_UNUSED_CLEAR64`. It exists because the assembler emits placeholder instruction fields that must be zeroed at link time -- for example, a reserved instruction modifier slot whose value depends on relocation-time information that the assembler did not know. The 0x14 alias is reserved for the attribute table parallel.

### 1.6 Masked-Shift Family (16 codes)

| Code range | Index range | Operation |
|---|---|---|
| 0x16..0x1D | `action - 22` = 0..7 | `(value & mask_table[idx]) >> shift_table[idx]` |
| 0x2F..0x36 | `action - 22` = 25..32 | `(value & mask_table[idx]) >> shift_table[idx]` |

The 16 masked-shift codes share a single case body. The dispatcher pre-loads four 16-byte SSE constants (`xmmword_1D3F8E0`, `xmmword_1D3F8F0`, `xmmword_1D3F900`, `xmmword_1D3F910`) into local storage at function entry, totaling 64 bytes of mask data. The shift table (`xmmword_1D3F920`, `xmmword_1D3F930`) loads on-demand inside the case. The eight-element layout matches eight byte positions: `mask_table[0] = 0xFF`, `shift_table[0] = 0`; `mask_table[1] = 0xFF00`, `shift_table[1] = 8`; ...; `mask_table[7] = 0xFF000000_00000000`, `shift_table[7] = 56`. The combined `(action_type - 22)` indexing yields:

- `R_CUDA_G8_0` → action 22 (0x16) → mask 0xFF, shift 0 → low byte of (S+A)
- `R_CUDA_G8_8` → action 23 (0x17) → mask 0xFF00, shift 8 → byte 1 of (S+A)
- `R_CUDA_G8_56` → action 29 (0x1D) → mask 0xFF00000000000000, shift 56 → byte 7 of (S+A)

The second range (0x2F..0x36, indices 25..32) reuses the same table but at higher offsets -- the SSE-loaded local array is 64 bytes wide, and the larger indices read past the first eight entries into a parallel set that lives contiguously in the same `.rodata` region. These are the unified-table 8-bit patch family `R_CUDA_UNIFIED_8_0 .. R_CUDA_UNIFIED_8_56`.

The dispatcher uses `(action - 22)` as the table index regardless of which numeric block (0x16..0x1D or 0x2F..0x36) the opcode came from. The gap between 0x1D (decimal 29) and 0x2F (decimal 47) is exactly 18 codes, matching the gap between table indices 7 and 25 = 18. The eight intermediate codes 0x1E..0x25 are reserved for other action families (the descriptor enumeration uses them for actions 30..37, the `R_CUDA_FUNC_DESC_8_*` family) and are dispatched through the *default* arm of the switch -- the dispatcher never sees them, because the apply-class router in `sub_4698A0` re-targets those types to a different code path before reaching `sub_468760`.

## Section 2 -- Descriptor Action Enum (catalog values 0..56)

This is the value space stored in byte 20 of each 64-byte descriptor and consumed by three distinct readers: `sub_4698A0` (apply-class router), `sub_469620` (symbol-resolution gate, via the bitmask `0x3FFFE002C6`), and `sub_468760` (low-level dispatcher). The same byte serves all three readers.

| Action | Behavior | Where consumed |
|---:|---|---|
| 0 | no-op | All readers skip. |
| 1 | absolute write at `(bit_offset, bit_width)` | Dispatcher 0x01. |
| 2 | global-segment absolute | Dispatcher 0x01 (fast-path 64-bit). Apply-class router treats specially. |
| 3 | texture/sampler header index | Apply-class router; bypasses `sub_468760`. |
| 4 | surface descriptor HW-only | Apply-class router; bypasses dispatcher. |
| 5 | surface descriptor HW+SW | Apply-class router; bypasses dispatcher. |
| 6 | low 16 bits of 32-bit absolute | Dispatcher 0x06. |
| 7 | high 16 bits of 32-bit absolute (aux carries shift constant 0x20) | Dispatcher 0x07. |
| 8 | absolute write with symbol-size addend (`R_CUDA_ABS_SIZE`-family) | Dispatcher 0x08. The case body emits `record.extra + size` (absolute) or `extracted_old + size` (otherwise) and does not consume the symbol value. |
| 9 | `SHIFTED_2`; also leading slot of split-field types | Dispatcher 0x09. |
| 10, 11 | continuation pieces of split-field relocations | Read only by `sub_46ADC0` (preserve-relocs emitter); the dispatcher never sees them in slot[0] -- they appear only in slot[1] or slot[2] of `R_CUDA_CONST_FIELD*`. |
| 12 | function-descriptor 32-bit full | Apply-class router (gated by `sub_4698A0` range check `action - 12 <= 3`). |
| 13 | function-descriptor 32-bit LO | Apply-class router. |
| 14 | function-descriptor 32-bit HI | Apply-class router. |
| 15 | function-descriptor 32/64 raw | Apply-class router. |
| 16 | PC-relative branch offset | Dispatcher 0x10. |
| 17 | whole-instruction replacement | Dispatcher 0x01 with `bit_width = 64` (or 128 for `R_CUDA_INSTRUCTION128`). |
| 18 | YIELD opcode rewrite | Dispatcher 0x12 (the ABS_FULL alias). |
| 19 | YIELD-clear-predicate rewrite | Dispatcher 0x13 (CLEAR) -- the predicate field is zeroed. |
| 20 | zero-fill (`UNUSED_CLEAR`) | Dispatcher 0x14 (CLEAR alias). |
| 21 | `piece_cont` -- second piece of split-field, pairs with leading slot of action 9 | Read by `sub_46ADC0`; the dispatcher does not have a switch arm for 0x15. |
| 22..29 | 8-bit data patches at byte offsets 0, 8, ..., 56 (`R_CUDA_8_0 .. R_CUDA_8_56`) | Dispatcher 0x16..0x1D (MASKED_SHIFT). |
| 30..37 | 8-bit global patches (`R_CUDA_G8_0 .. R_CUDA_G8_56`) | Apply-class router; dispatched as 0x16..0x1D after class-aware preprocessing. |
| 38..45 | 8-bit func-desc patches (`R_CUDA_FUNC_DESC_8_0 .. R_CUDA_FUNC_DESC_8_56`) | Apply-class router; bypasses `sub_468760`. |
| 46 | unified descriptor full | Dispatcher 0x2E (ABS_FULL alias 2). |
| 47..54 | unified 8-bit patches (`R_CUDA_UNIFIED_8_0 .. R_CUDA_UNIFIED_8_56`) | Dispatcher 0x2F..0x36 (MASKED_SHIFT range 2). |
| 55 | unified-32 LO 16-bit piece (`R_CUDA_UNIFIED32_LO_32`) | Dispatcher 0x37 (ABS_LO alias). |
| 56 | unified-32 HI 16-bit piece (`R_CUDA_UNIFIED32_HI_32`) | Dispatcher 0x38 (ABS_HI alias). |

The bitmask `0x3FFFE002C6` in `sub_469620` line 46 -- the symbol-resolution gate -- decodes as the set `{1, 2, 6, 7, 9, 17, 18, 19, 20, 21, 22..33, 37}`. Any descriptor whose `slot0.action` is in this set triggers late symbol fixup; the others (3, 4, 5, 8, 10, 11, 12..16, 34..36, 38..56) are either resolved earlier or applied without symbol lookup. The mask is a compact way to encode the union of three categories: standard absolute writes (1, 2, 6, 7), wide-immediate writes (9, 17), opcode rewrites (18, 19, 20, 21), and the eight-way 8-bit families (22..29 and 30..37 partially overlap with the masked range, plus 37 as an outrider).

## Section 3 -- Dispatcher-to-Catalog Mapping

The two enumerations relate as follows:

```text
catalog action  dispatcher opcode   notes
--------------  -----------------   ---------------------------------------------
       0        0x00                exact match
       1        0x01                exact match
       2        0x01                same dispatcher path; class-aware caller
       6        0x06                exact match
       7        0x07                exact match
       8        0x08                exact match (ABS_SIZE; uses record.extra + size)
       9        0x09                exact match
      16        0x10                exact match
      17        0x01                falls through to fast path (bit_width == 64/128)
      18        0x12                first ABS_FULL alias
      19        0x13                CLEAR
      20        0x14                CLEAR alias
      21        --                  preserve-relocs only; not dispatched
      22..29    0x16..0x1D          arithmetic: dispatcher_code = catalog + 0
      30..37   (0x16..0x1D)         class-router rewrites before dispatch
      38..45    --                  bypassed (func-desc class)
      46        0x2E                second ABS_FULL alias
      47..54    0x2F..0x36          arithmetic: dispatcher_code = catalog + 0
      55        0x37                ABS_LO alias
      56        0x38                ABS_HI alias
```

Catalog actions 22..29 and 47..54 map directly to dispatcher opcodes by numeric identity (the catalog value *is* the dispatcher opcode). Catalog actions 30..37 are class-routed: the apply-class dispatcher `sub_4698A0` notices the `R_CUDA_G8_*` family (global section addressing rather than data-section addressing), performs the global-section base lookup, and then re-invokes the patch path with a dispatcher opcode in the 0x16..0x1D range. This is why the dispatcher never sees opcodes 0x1E..0x25 -- they correspond to catalog actions 30..37, which are intercepted upstream.

## Section 4 -- The Numeric Gaps

Five gaps appear in the dispatcher's switch space; each has a documented reason:

| Gap | Decimal | Why |
|---|---|---|
| 0x02..0x05 | 2..5 | Catalog actions 2..5 (`G32` and surface descriptor classes) are class-routed; they reach the dispatcher only as a 64-bit ABS_FULL (0x01), not as their own opcodes. |
| 0x0C..0x0F | 12..15 | Catalog actions 12..15 (`R_CUDA_FUNC_DESC32_*` family) bypass `sub_468760` entirely. The gate in `sub_4698A0` at `action - 12 <= 3` catches them. |
| 0x11 | 17 | Catalog action 17 (whole-instruction) is dispatched as ABS_FULL with `bit_width = 64` or 128. No distinct opcode is needed. |
| 0x15 | 21 | Catalog action 21 (`piece_cont`) is metadata for the preserve-relocs emitter `sub_46ADC0`; the dispatcher's job is done by the leading slot's action 9 (`SHIFTED_2`), and the continuation slot's `(bit_offset, bit_width)` is read from the descriptor without dispatching a new opcode. |
| 0x1E..0x2D | 30..45 | Catalog actions 30..45 (the `R_CUDA_G8_*` and `R_CUDA_FUNC_DESC_8_*` families) are class-routed. The G8 family ends up dispatched in 0x16..0x1D after global-base addition; the FUNC_DESC_8 family never reaches `sub_468760`. |
| 0x39..0xFF | 57..255 | Reserved / never appears in any descriptor. The diagonal-0xFF `R_CUDA_NONE_LAST` poison entry uses 255 as a tripwire (see [R_CUDA Relocation Catalog § QUIRK -- diagonal-0xFF terminator](../reference/r-cuda-catalog.md#decoded-sample-entries)), but its descriptor is never indexed because the bounds check at `sub_42F6C0` line 23 rejects `a1 >= 0x75` (117) first. |

## Section 5 -- Pre-Loaded SSE Mask Constants

The four 128-bit SSE registers loaded at function entry of `sub_468760` form a 64-byte constant table used by the masked-shift family. The decoded contents are:

```text
xmmword_1D3F8E0:  0x000000FF_00000000_000000FF_00000000
                  -> mask_table[0] = 0xFF                  (G8_0)
                     mask_table[1] = 0xFF00                (G8_8)
xmmword_1D3F8F0:  0x000000FF_00000000_000000FF_00000000
                  -> mask_table[2] = 0xFF0000              (G8_16)
                     mask_table[3] = 0xFF000000            (G8_24)
xmmword_1D3F900:  0x000000FF_00000000_000000FF_00000000
                  -> mask_table[4] = 0xFF00000000          (G8_32)
                     mask_table[5] = 0xFF0000000000        (G8_40)
xmmword_1D3F910:  0x000000FF_00000000_000000FF_00000000
                  -> mask_table[6] = 0xFF000000000000      (G8_48)
                     mask_table[7] = 0xFF00000000000000    (G8_56)
xmmword_1D3F920:  shift_table[0..3] = {0, 8, 16, 24}
xmmword_1D3F930:  shift_table[4..7] = {32, 40, 48, 56}
```

(The byte patterns above represent the abstract mask values; the actual SSE encoding stores the 8 byte-locations as 64-bit constants packed into the 16-byte registers.) The shift values are simply `idx * 8`. The second range 0x2F..0x36 reuses the same registers but at indices 25..32 -- this works because the dispatcher's `(action - 22)` indexing yields 25..32 for those opcodes, and the local-stack copy of the mask table extends to 33 entries by virtue of how the SSE stores spill across the local frame. The shift values for indices 25..32 are also 0, 8, ..., 56 -- the unified-table 8-bit patches mirror the global-table 8-bit patches byte-for-byte, just landing in a different `.rodata` index.

## Section 6 -- Multi-Slot Descriptors

The dispatcher iterates up to 3 action slots before hitting the sentinel at descriptor offset +60. Of the 117 standard R_CUDA descriptors:

- **108 entries** use slot[0] only. Slots 1 and 2 carry `action_type = 0` (END), so the dispatcher's case-0 arm advances twice and returns 1.
- **8 entries** use slot[0] + slot[1]. These are wide split-field types: `R_CUDA_ABS55_16_34`, `R_CUDA_ABS56_16_34`, and the section-type encoders that pair `SEC_TYPE_LO` (action 10) with `SEC_TYPE_HI` (action 11).
- **1 entry** (`R_CUDA_CONST_FIELD19_28`, descriptor index 24) uses all three slots: slot[0] writes 14 bits at position 28 with action 9, slot[1] writes 4 bits at position 42 with action 10, slot[2] writes 1 bit at position 26 with action 11.

For the 8 two-slot entries that use action 9 + action 21 (the `*_16_34` split-field family), the dispatcher executes the 0x09 case for slot[0], advances to slot[1] whose `action_type = 21` (0x15) -- and *falls into the default arm*, returning 0. This would be a bug except that `sub_469D60` recognizes the action-9 leading slot and short-circuits: it processes the wide field with custom logic before invoking `sub_468760` only for the leading piece. The action-21 slot is never actually dispatched -- it serves as a marker for `sub_46ADC0` during preserve-relocs emission, where the leading-piece and continuation-piece `(bit_offset, bit_width)` pairs are recombined into a single resolved RELA record.

## QUIRKs

> **QUIRK -- the dispatcher's 31 opcodes cannot represent every descriptor action**
> The descriptor `action` enum spans 0..56, but `sub_468760`'s switch covers only 31 distinct numeric values (collapsing into 11 case-body branches once aliases are folded in). The arithmetic does not balance: 23 distinct catalog actions are in use, of which 7 (catalog 3, 4, 5, 10, 11, 12..15, 21, 38..45) never reach the dispatcher at all. They are intercepted by the apply-class router `sub_4698A0` or the preserve-relocs emitter `sub_46ADC0`. The cleanest mental model is: the dispatcher is one of three terminal consumers of the action byte, and only the dispatcher's *own* switch arms behave like opcodes in the conventional sense. Pre-decoding the descriptor table by mapping `action` to "operation" without accounting for which consumer the action belongs to will misclassify the texture/surface/funcdesc families.

> **QUIRK -- catalog action 21 (0x15) is a dispatcher non-event**
> Catalog action 21 lives in the gap between the two masked-shift ranges (0x16..0x1D and 0x2F..0x36). It is the only "real" action value in that gap -- the others (0x1E..0x2D) are class-routed away. But 21 itself has no switch arm. `sub_468760` falls through to `default: return 0` if it ever encounters action 21 in slot[0], because slot-1/slot-2 dispatch is controlled by the outer loop in `sub_469D60`, which only enters the dispatcher for action-9-leading split fields and *skips* the continuation slot. This is the cleanest example of a value that is both "in the catalog" and "not in the dispatcher" -- you cannot infer one from the other.

> **QUIRK -- ABS_FULL has three numeric aliases for one operation**
> The case `0x01: case 0x12: case 0x2E:` fuses three semantically-distinct relocation families into one case body. The three values exist because each family (standard / attribute / unified) has its own block of the action enum -- standard absolutes start at 1, attribute absolutes at 18 (0x12), unified absolutes at 46 (0x2E). The dispatcher's switch could have used three separate case bodies with identical code, but the compiler (or the author) chose to express the equivalence directly via fall-through aliases. The same pattern applies to ABS_LO (1 alias: 0x06 / 0x37) and ABS_HI (1 alias: 0x07 / 0x38). If a future relocation family is added, the natural place for it is to introduce a fourth ABS_FULL alias somewhere in the unallocated gap -- which is why opcodes 0x39..0xFF are reserved rather than rejected with a more aggressive error.
