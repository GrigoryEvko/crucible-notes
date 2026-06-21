# SASS Encoding — Function-Pointer Dispatch Tables

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The [SASS encoder](./encoding.md) compiles each Ori IR instruction by walking a small ladder of switch-dispatched megafunctions and reading per-field templates out of rodata. The address-stable infrastructure for those megafunctions is a population of **409 function-pointer dispatch tables** in `.rodata` totalling 70,528 slots (564,224 bytes if every entry is an 8-byte target pointer). Each slot is a `void(*)()` that the megafunction loads through a base-plus-index `mov rax, [rip + tbl + 8*idx]; jmp rax` sequence — the standard x86-64 jump-table emission pattern that GCC produces for dense `switch` statements with no `default` reachable from a fallthrough.

These dispatch tables are not the format descriptors in 0x23F1xxx–0x23F2xxx (those are `xmmword` blobs documented in [encoding.md](./encoding.md#instruction-format-groups)). They are the **executable counterpart** — the per-case targets that the megafunction switches actually jump to. A reimplementer who wants to encode SASS by replaying ptxas's behaviour must know not just the format constants but also which target function each opcode-category lands in, because the megafunctions never bring their dispatch logic onto the C-source level — Hex-Rays renders them as goto-soup.

| | |
|---|---|
| **Total tables** | 409 |
| **Total slots** | 70,528 |
| **Section** | 404 in `.rodata`, 4 in `.data.rel.ro` (`0x29f9a28`, `0x29f9b18`, `0x29f9b68`, `0x29f9c68`), 1 in `.data` (`0x29fca68`, the 269-slot percase table). The split reflects immutability semantics: `.rodata` entries are link-time-final, `.data.rel.ro` entries are resolved by the dynamic linker then mprotected read-only via RELRO, and the single `.data` table is genuinely mutable at runtime — the only dispatch table ptxas can rewrite mid-execution. |
| **Slot width** | 8 bytes (absolute VA, no relocation needed in a non-PIE build) |
| **Largest single table** | `0x22f1f60` — 5,560 slots (the consolidated mega-dispatcher jump table) |
| **Density** | 329 tables target the 0x0040000–0x00FFFFFF text range; 80 tables target 0x01000000–0x01FFFFFF |
| **Source** | `ptxas_data_tables.json` (8 MB), extracted via the IDA Python sweep documented in [methodology.md](../methodology.md) |

## Mega-Dispatcher Jump Table — `0x22f1f60`

The single most important table in the binary. **5,560 contiguous 8-byte slots, all six of ptxas's encoding/decoding megafunctions share this one block.** The six functions are pinned to non-overlapping address ranges within it, each function's case-target arena starting at a fixed boundary:

| Slot | Byte offset | Owning function | # slots | Role (from [encoding.md](./encoding.md#dispatch-tables----the-six-megafunctions)) | Confidence |
|---|---|---|---|---|---|
| `0` | `+0x0000` | `sub_10C0B20` | 1,018 | **setField** — write a value into a named field | **HIGH** |
| `1018` | `+0x1FD0` | `sub_10C7690` | 744 | **setOperandField** — per-operand variant | **HIGH** |
| `1762` | `+0x3710` | `sub_10CAD70` | 744 | **getOperandFieldOffset** — per-operand bit-offset | **HIGH** |
| `2506` | `+0x4E50` | `sub_10CCD80` | 1,018 | **setFieldDefault** — write hardcoded default | **HIGH** |
| `3524` | `+0x6E20` | `sub_10D5E60` | 1,018 | **getFieldOffset** — return bit-offset of named field | **HIGH** |
| `4542` | `+0x8DF0` | `sub_10E32E0` | 1,018 | **hasField** — boolean field-existence query | **HIGH** |

The four 1,018-slot arenas hold three (sometimes two) jump tables concatenated: the primary 370-case switch on the opcode category at `(WORD*)(a1+12)` consumes ~370 slots, and the secondary sub-switches on field ID consume the rest. The two 744-slot arenas additionally extend the primary switch to category 0x174 (373 cases) and need fewer secondary slots because the field-ID inner switches are smaller (range 1–30 instead of the broader field-ID enum).

A direct verification: `sub_10C0B20`'s primary switch has 370 cases per the [switch dump](../methodology.md) and 248 of them carry a real handler — the other 122 fall through to `default` (`0x10c0b40`). The table slots are still present and point at the default block, which is why every opcode category occupies a slot even when it has nothing to encode.

> ⚡ **QUIRK — six functions, one rodata block**
> All six mega-dispatchers index into a single contiguous 44,480-byte table. There is no per-function table header or terminator: the boundary between `sub_10C0B20`'s arena and `sub_10C7690`'s arena is implicit (slot 1018) and known only because each megafunction's `lea` instruction loads a different base address. A reimplementer can verify the partition by disassembling the `lea r10, [rip + 0x22F1F60]` etc. constants. The compactness is a code-size optimization — five separate `.rodata` symbols would have introduced five linker alignment gaps.

### Switch entry shape

Each primary-switch slot encodes `(*((void(**)(int64_t,int64_t,uint32_t))(tbl + 8 * category)))(...)` semantics. Decoded C pseudocode for an arbitrary call site:

```c
// Inside sub_10C0B20 (setField):
//   a1 = instruction context (192-bit packed word at a1+48)
//   a2 = field ID
//   a3 = value to write
uint16_t category = *(uint16_t*)(a1 + 12);   // opcode category (0x0..0x171)
if (category >= 370) goto LABEL_default;
void *target = ((void**)0x22F1F60)[category]; // 5,560-slot dispatch
goto *target;                                  // → one of 248 real handlers or default
// Each LABEL_xxx then performs its own sub-switch on field-ID `a2`
```

The dispatcher never returns a value through the table itself — each target either falls through to one of the four shared write-paths (`LABEL_3941`, `LABEL_3923`, `LABEL_3929`, `LABEL_3935`) or to the default. See [encoding.md § setField shared write paths](./encoding.md#setfield-shared-write-paths) for the boundary-crossing OR sequences.

## Per-SM-Tier Encoder Index Tables — `0x22a5aa0` Family

Four nearly-identical 455-slot tables sit adjacent in `.rodata`, all targeting the 0xB07F70–0xB18CB0 region (the SASS encoder family at the high end of the codegen `.text`). The slots are populated *progressively* — the first table has 93 nullsub holes, the last has only 2. **This is the SM-tier dispatch — one row per SM tier (SM75, SM80, SM89, SM90/100), each row a different per-opcode encoder selector with newer SMs filling in opcodes that older SMs didn't support.**

| Table address | Unique funcs | Nullsubs | Target VA range | SM tier (inferred) | Confidence |
|---|---|---|---|---|---|
| `0x22a5aa0` | 454 | 93 | `0xB079D0..0xB18410` | Earliest (most unimplemented) | **MEDIUM** |
| `0x22a6e70` | 454 | 25 | `0xB079E0..0xB18CB0` | Mid-generation | **MEDIUM** |
| `0x22a8248` | 454 | 25 | `0xB079E0..0xB18CB0` | Mid-generation (alt) | **MEDIUM** |
| `0x22a9bb0` | 454 | 2 | `0xB07F70..0xB18790` | Latest (fully populated) | **MEDIUM** |
| `0x22b2a58` | 455 | 40 | `0x6611B0..0x15F5800` | Distinct purpose (much wider range) | **LOW** |

The four `0x22a*` tables share most slots — e.g. slot 5 is `sub_B144E0` in all four, slot 100 is `sub_B0F6C0` in all four. They differ at the slot positions where the SM-tier-specific encoder takes over. The slot-0 nullsub is *different per table* (`nullsub_593`, `_594`, `_595`, `_596`) — this is the IDA Pro naming convention for distinct-but-empty jump targets and tells us the C++ source declared four separate `static const handler_t Encoder_SMxx[455]` arrays.

> ⚡ **QUIRK — nullsub gap progression encodes SM history**
> The nullsub count drops monotonically across the SM tiers (93 → 25 → 25 → 2). Each new SM tier "fills in" opcodes that the previous tier left unimplemented, but never removes opcodes. The two final nullsubs in `0x22a9bb0` are presumably opcodes still on NVIDIA's deprecation queue — they exist in the encoder enum but no current SM has a real handler. Forensic value: subtracting `0x22a5aa0` from `0x22a9bb0` slot-by-slot tells you exactly which opcodes were added at each SM tier without any per-SM string evidence.

> ⚡ **QUIRK — slot-0 nullsub identity is the table fingerprint**
> Every other slot can be shared by name (`sub_B144E0` appears at slot 5 in all four tables), but slot 0 holds a unique `nullsub_N` per table because IDA assigns a fresh nullsub ID to each distinct rodata reference even when the target instruction is identical. If a reimplementer collapses the four tables into one, slot 0 collapses with them — losing the SM-tier boundary marker. Treat the four tables as distinct symbols even when 80% of their slots are byte-identical.

### Decode protocol

Each slot is reached by a per-opcode index, not by the opcode-category in `(a1+12)`. The index is derived from the `opcode_master` lookup (mentioned in [encoding.md § Concrete Constants for the Top-5 Encodings](./encoding.md#concrete-constants-for-the-top-5-encodings)). A skeleton consumer:

```c
// SM-tier-aware encoder dispatch (reconstructed)
typedef int (*encode_fn)(int64_t ctx, int64_t ir_node);
extern encode_fn sm_encoders[4][455]; // 0x22a5aa0, 0x22a6e70, 0x22a8248, 0x22a9bb0

int encode_for_target(int sm_tier, int opcode_idx, int64_t ctx, int64_t ir) {
    if ((unsigned)opcode_idx >= 455) return -1;
    encode_fn fn = sm_encoders[sm_tier][opcode_idx];
    if (!fn) return -1;             // unhandled opcode for this SM
    return fn(ctx, ir);
}
```

The `sm_tier` value flows from the global SM target descriptor (the `--gpu-name sm_NN` parsing path). Section 7 of [methodology.md](../methodology.md) documents how the descriptor reaches the encoder.

## High-Density Encoder Tables (>200 Unique Targets)

These tables each point to >200 distinct functions and are the workhorses of the encoder back-end. They cover the per-opcode handler lookups that the megafunctions stand on top of.

| Table address | Slots | Unique funcs | Nullsubs | Target VA range | Likely role | Conf |
|---|---|---|---|---|---|---|
| `0x22ad230` | 2,129 | 246 | 65 | `0xA393D0..0x19F72E0` | **ISel C++ vtable** (see correction below) — NOT an encoder table | **HIGH** |
| `0x23b3a80` | 2,109 | 150 | 1 | `0x9DAA40..0x181D9B0` | **ISel C++ vtable** (see correction below) — NOT an encoder table | **HIGH** |
| `0x23f4430` | 633 | 633 | 0 | `0x1BB38B0..0x1BBD2C0` | One-to-one per-slot dispatch — 633 unique entries, no sharing | **MED** |
| `0x21f9158` | 470 | 469 | 8 | `0x7D6AE0..0xBE26C0` | Per-opcode pretty-printer or trace formatter (nullsubs at slots 4, 55, 302, 321, 331, 457, 460, 461) | **MED** |
| `0x21d6860` | 470 | 469 | 4 | `0x6611B0..0x15F4870` | Sibling of `0x21f9158`, distinct entry-point set | **MED** |
| `0x21d82b0` | 466 | 466 | 33 | `0x6611B0..0x15F5800` | Per-opcode handler, broad-range | **MED** |
| `0x21f5b70` | 443 | 443 | 0 | (similar range) | Per-opcode dispatch — fully unique | **LOW** |
| `0x22b64d8` | 503 | 208 | 46 | `0xA393D0..0xC380C0` | Per-opcode validate/encode hybrid | **LOW** |
| `0x29fca68` | 269 | 269 | 0 | (varies) | Per-opcode dispatch — fully unique | **LOW** |

The `0x21d6860 / 0x21f9158` pairing — both 470 slots, both ~469 unique — is suspicious. Diffing them slot-by-slot would tell us whether they're another SM-tier pair or two parallel pipelines (e.g. one for `mercury` mode, one for `capmerc` mode; see [mercury.md § Mercury vs SASS vs Capsule Mercury](./mercury.md#mercury-vs-sass-vs-capsule-mercury)).

### Schema (C-level reconstruction)

```c
// .rodata layout for a dispatch table (no header, no terminator):
struct sass_encoder_table {
    encode_fn slots[N];   // N is determined by the megafunction's switch range
};
// Real declaration is anonymous; the symbol exists only as a `lea` displacement
// in one or more megafunctions. There is no length field — N is hard-coded into
// the consumer.
```

The lack of a length field is critical for reverse engineering: knowing `N` requires either reading the consumer's `cmp ... , N; ja default` bound check or summing slots until you hit a different `.rodata` symbol. The IDA extractor at the source of `ptxas_data_tables.json` uses the next rodata symbol boundary, which produces correct slot counts in practice but should not be trusted blindly when a table sits at a section boundary.

### Representative entry (ASCII bit-layout)

```text
.rodata, 8 bytes per slot:
+-----------------------------------------------------------------+
| 63                                                              0
| <-- 64-bit absolute virtual address of target function -------> |
+-----------------------------------------------------------------+

Example slot 0 of 0x22a5aa0 (VA 0x22a5aa0 = rodata_base 0x1ce2e00 + offset 0x5c2ca0):
    0x22a5aa0:  A0 80 B0 00 00 00 00 00     ; → 0x00B080A0 (nullsub_593)
                ^^^^^^^^^^^^^^^^^^^^^^^^^^
                little-endian, no relocation in non-PIE binary
                (ptxas is a non-PIE statically-linked-style ELF —
                 confirmed by absence of R_X86_64_RELATIVE entries)
```

The slot-0 nullsub at `0x00B080A0` is the **sentinel convention** — every Group-A SM-tier table reserves index 0 for an empty `ret`-only target (one nullsub per table, hence `nullsub_593`/`_594`/`_595`/`_596`). Reading slot 0 of any such table tells you whether index 0 is a real opcode (rare) or a reserved "OP_INVALID" probe slot; for the encoder family it is the latter. See [Targeting Behavior — `nullsub` Sentinels](#targeting-behavior--nullsub-sentinels) below.

The slots are *not* relocations — ptxas is built non-PIE so absolute addresses are baked into rodata at link time. A PIE rebuild would convert all 70,528 slots into `R_X86_64_RELATIVE` entries and balloon the dynamic relocation count from 146 (PLT-only) to ~70,000.

## Dense Single-Target Tables — Computed-Goto Mode

Several tables hold thousands of slots that all point into a single function. These are not dispatch tables in the conventional sense — they are *computed-goto label tables* for one mega-switch.

| Table address | Slots | Unique funcs | Target function | Target range | What it represents |
|---|---|---|---|---|---|
| `0x23f6d00` | 2,064 | 1 | `sub_B12920` | `0x1C38C35..0x1C3C646` | Per-case basic-block labels inside one giant switch |
| `0x237a1b0` | 2,455 | 2 | `sub_15F5510`, `sub_169B190` | (within those two) | Pair of computed-goto tables for two siblings |
| `0x21d3ac8` | 1,148 | 7 | (7 funcs) | `0x6E66D0..0x855867` | Computed-goto across 7 closely-related funcs |

The `0x23f6d00` row is the most extreme: 2,064 absolute addresses, all landing inside a 14-KB region of `sub_B12920`. Hex-Rays cannot recover the surface-level `switch (...)` for this one — the function is rendered as goto soup. The table address tells you the function uses a *threaded* dispatch (every case ends with `goto *jumptable[next_idx]`), which is the typical interpreter loop pattern. `sub_B12920` is the encoder dispatch core for one specific SASS instruction family — likely the tensor/MMA opcodes given the 14-KB body size.

```c
// Computed-goto threaded dispatch (reconstructed for sub_B12920):
static const void *jt[2064] = { &&L0, &&L1, &&L2, ... };  // == 0x23F6D00
goto *jt[opcode_idx];
L0: /* encode handler 0 */ goto *jt[next_idx];
L1: /* encode handler 1 */ goto *jt[next_idx];
// ...
```

The 2,064 slots span only 14,353 bytes inside `sub_B12920` (`0x1C3C646 - 0x1C38C35`), meaning each "case" averages 6.95 bytes — barely enough for a single instruction plus the next computed jump. This is the classic GCC `__builtin_goto` interpreter idiom; the source is almost certainly a single mega-switch on a small `uint16_t` opcode-subfield with no shared epilogue.

> ⚡ **QUIRK — Hex-Rays surrenders on computed gotos**
> The four mega-dispatchers (`sub_10C0B20`, `sub_10CCD80`, `sub_10D5E60`, `sub_10E32E0`) and the threaded `sub_B12920` are the five functions that Hex-Rays cannot decompile to clean C in [methodology.md § Scope and Scale](../methodology.md#scope-and-scale). The dispatch tables in `.rodata` are the *only* readable surface for these functions' control flow. Reading the tables in isolation gives you the case→target topology even when the decompiler gives up — which is why the JSON extractor preserves them as first-class artifacts.

## Cross-Reference With Format Descriptors

The encoding picture is two-layered. [Format descriptors](./encoding.md#instruction-format-group-catalog) in `0x23F1xxx–0x23F2xxx` carry the *data* (slot sizes, slot types, opcode header widths). The function-pointer tables documented here carry the *code* (which encoder runs for each opcode). The wiring between them:

1. The opcode-category in `(a1+12)` selects one of 248 live cases in the mega-switch via `0x22f1f60[category]`. **(table → handler)**
2. The selected handler loads the format-descriptor xmmword at `a1+8` from one of 38 named addresses in `0x23F1CE8..0x23F2EF8`. **(handler → format data)**
3. The format descriptor's three trailing DWORD[10] arrays are copied into the encoding context at `a1+24..a1+140`. **(format data → context state)**
4. The handler then makes 8–12 calls into the bitfield packer `sub_7B9B80`, indexing operands via the context state. **(context state → encoded bits)**

If you want to predict what ptxas emits for a given instruction without running ptxas, you must trace this entire chain. The function-pointer tables are the entry point: without them, you cannot reach step 2 because Hex-Rays cannot tell you which mega-switch case fires for a particular opcode-category integer.

## Dispatch Reconciliation — Four Encoder Layers Plus an ISel Vtable

The encoder back-end does not have one dispatch table; it has **four function-pointer layers plus an opcode→slot scalar table**, and they all draw their handler targets from a **single shared handler pool**. The four layers route the same logical instruction through different keying schemes; the per-SM layer is the authoritative override when present. A reimplementer who treats any one of these as "the" dispatch will mis-encode every architecture-specific instruction.

| Layer | Region VA | Indexing scheme | Keyed by | Distinct handlers | Role |
|---|---|---|---|---|---|
| `opcode_to_encoding` | `0x22B4B60` | flat `u16[222]` | opcode index | — | opcode → encoding slot (sentinel **355** = extended/macro-lowered) |
| `encoding_tree_1` | `0x233BE00`–`0x2353E00` | 16-byte slotted radix tree (274 internal / 5,169 leaf) | `(format_id<<8) \| minor` via slot | 4,618 | primary format + handler selection |
| `encoding_tree_2` | `0x235CE00`–`0x2379E00` | 16-byte slotted tree (427 internal / 6,251 leaf) | `(format_id<<8) \| minor` via slot | — | secondary/extended tree (shares the handler pool) |
| `sass_handler_dispatch_1` | `0x22C0E00`–`0x22F1E00` | `(opcode, category, variant)` sub-tables | opcode id | 6,915 | superset emit-handler list (6,915 rows) |
| `sass_handler_dispatch_2` | `0x2379E00`–`0x2399E00` | `(opcode, category, variant)` sub-tables | opcode id | 3,478 | reduced prefix of `_1` (`_2` is a strict subset) |
| `per_sm_handler_dispatch` | `0x22E7AD0`–`0x23B99D0` | 5 × `{u64 op, u64 ptr, u64 pad}` arrays | `(format_id<<8) \| minor` | 2,421 | **per-arch specialized override** (sm50_7x / sm75 / sm100 / sm80_8x / sm86_89) |
| *(correction)* `off_22AD230`, `off_23B3A80` | `0x22AD230`, `0x23B3A80` | C++ vtables (virtual slot) | virtual slot | 3,707 | **ISel node vtables — NOT encoder dispatch** (the prior catalog mislabel) |

Overlap (handlers shared between layers, from the reconciliation matrix):

| | encoding_tree | sass_dispatch_1 | sass_dispatch_2 | per_sm_dispatch | isel_vtable |
|---|---|---|---|---|---|
| **encoding_tree** | 4,618 | 2,828 | 3,367 | 2,194 | 0 |
| **sass_dispatch_1** | 2,828 | 6,915 | 2,832 | 1,711 | 0 |
| **sass_dispatch_2** | 3,367 | 2,832 | 3,478 | 1,899 | 0 |
| **per_sm_dispatch** | 2,194 | 1,711 | 1,899 | 2,421 | 0 |
| **isel_vtable** | 0 | 0 | 0 | 0 | 3,707 |

The decisive evidence for the ISel-vtable correction is the bottom row/column: **the two C++ vtables share zero handlers with any of the four encoder layers.** A genuine alternate encoder dispatch would necessarily re-use the same `0xC6xxxx`–`0xE2xxxx` encoder-stub pool that the four real layers all index into; `off_22AD230` and `off_23B3A80` point exclusively into the ISel matcher/node method bodies (`0xBA9CF0`–`0xBDBA60` and the surrounding range), with no intersection. The union of the four encoder layers is **8,874** distinct handler addresses; adding the two vtables would add 3,707 ISel methods that never participate in encoding.

> ⚡ **QUIRK — the dispatch key is `(format_id<<8) | minor_opcode`, not the opcode**
> Both encoding trees and the per-SM override array key on a composite 16-bit value formed from the 7-bit format ID in the high byte and the 8-bit minor opcode in the low byte. The opcode integer alone does **not** index these tables — it indexes only `opcode_to_encoding` and the `sass_handler_dispatch_*` sub-tables. A reimplementer who keys the trees by raw opcode will land on the wrong leaf for every multi-format instruction (every ALU op that has both a 64-bit and 128-bit encoding).

### Encoding decision trees — geometry

The two trees are 16-byte-slot arrays spanning a fixed address window. Each tree's window holds more slots than are populated; the empty tail slots are reserved:

| Tree | Window | 16-byte slots in window | Populated internal | Populated leaf | Reserved tail |
|---|---|---|---|---|---|
| `encoding_tree_1` | `0x233BE00`–`0x2353E00` | 6,144 | 274 | 5,169 | 701 |
| `encoding_tree_2` | `0x235CE00`–`0x2379E00` | 7,424 | 427 | 6,251 | 746 |
| **total** | — | **13,568** | 701 | 11,420 | 1,447 |

Slot shapes:

- **Internal node** — `{child_ptr, child_count}`: a radix fan-out. `child_ptr` points at a contiguous run of `child_count` child slots; the walk descends by matching successive bytes of the composite key.
- **Leaf node** — three flavors observed across the 11,420 leaves: `key_handler` (4,117 leaves) carries `{dispatch_key = (format_id<<8)|minor, handler_va}`; `handler_value` (3,635 leaves) carries `{handler_va, extra_value}`; `null_or_data` (3,668 leaves) carries an inline data word or a null terminator. Of the `key_handler` leaves, **396** distinct dispatch keys and **2,542** distinct handler addresses are observed (multiple keys can share a handler; one key can appear in both trees at different specificity).

See [reference/data/sass-encoding-dispatch.md](../reference/data/sass-encoding-dispatch.md) for the full per-layer summary and reconciliation matrix as browsable tables.

### Dispatch-row semantics — category and variant

Each row of the `sass_handler_dispatch_*` and `per_sm_handler_dispatch` arrays is 24 bytes: `{u64 handler, u64 pad=0, u32 variant, u32 category}`, looked up by the composite key `(category << 8) | variant`. The two fields are independent axes:

- **`category` = the encoding-format / opcode-CLASS family** (one per functional-unit / op-class), **not** an SM-generation bucket. The SM split is carried entirely by *which* `per_sm` array is indexed (sm50_7x / sm75 / sm80_8x / sm86_89 / sm100) — orthogonal to `category`. The encoding width (64- vs 128-bit) selects the format-descriptor width; the per-SM arrays hold byte-identical handler clusters that differ only in their `setBits` triples.
- **`variant` = a sequential, modifier-driven sub-opcode** within a category (dense enumeration `0x02`..`0x1b`, running sequentially per category group). The *alternate-encoding* choice (the second physical encoding some instructions accept) is a **separate runtime flag** on the operand struct (`*(u8*)(op+0x28) != 0`, which reads an extra source from `op+0x34`) — **not** the table `variant`.

> ⚡ **QUIRK — `category` is an op-class, `variant` is a sub-opcode, and "which SM" is a third, orthogonal axis**
> Three independent dimensions select one handler: the op-class (`category`), the modifier-driven sub-opcode (`variant`), and the architecture (which `per_sm` array). Earlier work that read `category` as an SM-generation bucket conflated the first and third axes. Concretely: the same `(category, variant)` pair resolves to byte-identical handler bodies across every `per_sm` array — only the embedded `setBits(start, width, value)` triples differ, because the field positions move between SM encodings.

### `encoding_bitfield_lookup` — `field_a` / `field_b`

The bitfield-lookup table at `0x23F2E00`–`0x23FAE00` is 4,096 entries of `{u32 field_a, u32 field_b}` (4,014 active; sentinel `0xFFFFFFFF` ends a field-list run). The two fields are role-distinct:

- **`field_a`** (a `.text` VA pointer in 3,924 of the active entries; 2,067 unique values) is a **per-(opcode, operand-layout) operand-field encoder function**. It marshals the parsed operand fields for one field group and tail-calls the multi-bit-field insert primitive `setBits` at `0x7B9B80`, which writes the SASS code bitstring into the encoder buffer at `obj+0x220`. Example: entry 116's `field_a = 0x1BA4C00` reads `op+0x2C`/`op+0x30` and writes `dst+0x1C`/`+0x20`; `0x1BA4C50`/`0x1BA4C80`/`0x1BA4BB0` are N-field variants of the same marshaller; `0x1BB2A48` is a trampoline.
- **`field_b`** is an **operand-field-KIND classifier code**, not a raw bit width: `0` = none/default (3,961 entries); small codes `1`..`17` name specific operand-field classes; `0xFFFFFFFF` marks end-of-field-list. A handful of "half-sentinel" rows carry only `field_b ∈ {4, 24, 28, 30}` to mark special field categories. A descriptor *run* is a sequence of `{field_a = sub-encoder, field_b = kind}` pairs bounded by the `0xFFFFFFFF` sentinel.

This is the executable counterpart to the static `slot_sizes`/`slot_types` arrays in the [format descriptors](./encoding.md#instruction-format-group-catalog): the format descriptor says *how wide* each slot is; `field_a` is the code that actually *packs* it.

## Targeting Behavior — `nullsub` Sentinels

64 of the 409 tables contain at least one `nullsub_*` slot. The IDA convention is that `nullsub` is a function whose entire body is `ret` — used as a placeholder for "this entry is reachable but does nothing." In ptxas's dispatch tables, nullsubs mean three different things:

| Pattern | Interpretation | Example |
|---|---|---|
| Single nullsub at slot 0 only | Sentinel — index 0 reserved (e.g. "OP_INVALID") | `0x22a5aa0` slot 0 |
| Many nullsubs scattered through table | SM-tier unimplemented opcodes | `0x22a5aa0` (93 holes) |
| Block of consecutive nullsubs | Reserved opcode range for future SMs | `0x22b64d8` (clustered) |
| Nullsub at every slot | Table not yet emitted for this build | (none observed in v13.0.88) |

The distinction matters when reimplementing: an "SM-tier unimplemented" nullsub means **silent return** — the encoder produces a 64- or 128-bit instruction word of zeros. The default `0x10c0b40` block in the megafunctions, by contrast, **returns an error code**. So index-0 sentinels and SM-tier holes have different fault semantics even though both are nullsubs.

> ⚡ **QUIRK — silent-zero versus error-default**
> An opcode that hits a nullsub slot in `0x22a5aa0` and an opcode whose category isn't in the megafunction's case list both "do nothing", but the first writes an all-zeros instruction word and the second returns -1 to the caller. PTX programs that hit the nullsub path on an older SM will compile cleanly to a bogus zero-instruction; PTX programs that hit the megafunction default will trigger the "Instruction '%s' cannot be compiled for architecture '%s'" diagnostic (string `0x...` per [ptxas_strings.json](../methodology.md#string-driven-analysis)). Same observable cause, completely different failure modes.

## Table Catalog — All Tables With ≥100 Slots

A compact catalog for navigation. The "Pattern" column captures what the table looks like at a glance:

- **mega** = single-block of mega-dispatcher labels (jumps within megafunc)
- **percase** = one unique function per slot (real per-opcode dispatch)
- **sparse** = mostly populated but with nullsub holes (SM-tier sparse)
- **gotomap** = computed-goto table (1–7 unique targets, many slots)
- **mixed** = blend of unique funcs and shared fallback

| Address | Slots | Uniq | Nullsubs | Pattern | Owner / role |
|---|---|---|---|---|---|
| `0x22f1f60` | 5,560 | 6 | 0 | mega | The six mega-dispatchers, shared block |
| `0x237a1b0` | 2,455 | 2 | 0 | gotomap | `sub_15F5510` + `sub_169B190` thread |
| `0x22ad230` | 2,129 | 246 | 65 | mixed | **ISel matcher vtable** (`off_22AD230`) — not an encoder table |
| `0x23b3a80` | 2,109 | 150 | 1 | mixed | **ISel node vtable** (`off_23B3A80`) — not an encoder table |
| `0x23f6d00` | 2,064 | 1 | 0 | gotomap | `sub_B12920` thread (tensor/MMA?) |
| `0x2358c38` | 1,971 | 1 | 0 | gotomap | `sub_143C440` thread |
| `0x23d2a30` | 1,966 | 1 | 0 | gotomap | `sub_198BCD0` thread |
| `0x21e7118` | 1,836 | 60 | 2 | mixed | Per-opcode multi-stage |
| `0x21f0590` | 1,677 | 10 | 0 | gotomap | Mid-density goto thread — 291/283/240/240/240 distribution |
| `0x22a1d80` | 1,649 | 57 | — | mixed | Per-opcode mid-density |
| `0x2355b80` | 1,548 | 5 | 0 | gotomap | 5-target thread in `sub_13ACxxx`/`sub_ACBxxx` |
| `0x21debc0` | 1,262 | 15 | 0 | gotomap | `sub_917A60`+ family thread |
| `0x21d3ac8` | 1,148 | 7 | 0 | gotomap | 7-target thread |
| `0x1d4b778` | 1,080 | 1 | 0 | gotomap | `sub_5FF700` thread |
| `0x202e108` | 948 | 4 | 0 | gotomap | `sub_704D30` thread (764/948 slots) + 3 minor branches |
| `0x21e35b8` | 827 | 25 | 0 | mixed | — |
| `0x229e9f8` | 734 | 8 | 0 | gotomap | `sub_AED3C0`/`sub_AEA420` dual thread (342+329 of 734) |
| `0x1d10a68` | 703 | 1 | 0 | gotomap | `sub_4CE6B0` thread |
| `0x20254b0` | 701 | 5 | 0 | gotomap | `sub_657xxx`/`sub_65Dxxx`/`sub_65Axxx` triple thread |
| `0x21f3a20` | 667 | 2 | 0 | gotomap | — |
| `0x23f4430` | 633 | 633 | 0 | percase | One unique func per slot |
| `0x202b840` | 604 | 3 | 0 | gotomap | Mercury master-encoder thread |
| `0x21cc6e0` | 591 | 17 | 0 | mixed | — |
| `0x203a5a8` | 552 | 1 | 0 | gotomap | `sub_720F00` thread |
| `0x2021820` | 550 | 8 | 0 | gotomap | `sub_61F700` lead (156) + 4 secondary @ 69 each — symmetric 4-way split |
| `0x21ec3d0` | 537 | 15 | 0 | mixed | — |
| `0x21b3418` | 535 | 3 | 0 | gotomap | Mercury triple-thread |
| `0x22b3960` | 504 | — | — | — | — |
| `0x22b64d8` | 503 | 208 | 46 | mixed | Per-opcode validate/encode |
| `0x21dd3a0` | 502 | 2 | 0 | gotomap | — |
| `0x21f9158` | 470 | 469 | 8 | percase | Per-opcode printer/dispatch — nullsubs at slots 4, 55, 302, 321, 331, 457, 460, 461 (`nullsub_148`, `_190`, `_227`, `_231`, `_232`, `_246`, `_247`, `_248`) |
| `0x21d6860` | 470 | 469 | 4 | percase | Sibling of `0x21f9158` |
| `0x229d418` | 469 | 468 | — | percase | — |
| `0x21d82b0` | 466 | 466 | 33 | percase | — |
| `0x22b2a58` | 455 | 455 | 40 | percase | SM-tier sibling of `0x22a*` family |
| `0x22a9bb0` | 455 | 454 | 2 | sparse | SM-tier 4 (latest) — fully populated |
| `0x22a8248` | 455 | 454 | 25 | sparse | SM-tier 3 |
| `0x22a6e70` | 455 | 454 | 25 | sparse | SM-tier 2 |
| `0x22a5aa0` | 455 | 454 | 93 | sparse | SM-tier 1 (earliest) |
| `0x21f5b70` | 443 | 443 | 0 | percase | — |
| `0x22bb738` | 399 | 241 | — | mixed | — |
| `0x21d2e88` | 388 | 3 | 0 | gotomap | — |
| `0x22b5910` | 359 | 22 | — | mixed | — |
| `0x21e4fb0` | 363 | 2 | 0 | gotomap | — |
| `0x21d7798` | 353 | 2 | 0 | gotomap | — |
| `0x21c4b58` | 353 | 1 | 0 | gotomap | `sub_7D3A20` thread |
| `0x21b5218` | 350 | 3 | 0 | gotomap | Mercury triple-thread |
| `0x21d9ef8` | 343 | 1 | 0 | gotomap | `sub_89FBA0` thread |
| `0x22b7ba8` | 341 | 31 | — | mixed | — |
| `0x23b2e00` | 331 | 2 | 0 | gotomap | — |
| `0x23f0768` | 329 | 24 | 3 | mixed | — |
| `0x21c2bc8` | 327 | 2 | 0 | gotomap | — |
| `0x23d6ed0` | 315 | 237 | — | mixed | — |
| `0x23f11f0` | 309 | 208 | 42 | mixed | — |
| `0x23f31a0` | 298 | 2 | 0 | gotomap | — |
| `0x23f3b00` | 292 | 237 | 79 | sparse | — |
| `0x29fca68` | 269 | 269 | 0 | percase | — |
| `0x23f5808` | 236 | 236 | 79 | sparse | — |
| `0x23d1a88` | 236 | — | — | — | sibling of `0x23f5808` |
| `0x23d21f8` | 236 | — | — | — | sibling of `0x23f5808` |
| `0x21edbc8` | 204 | 203 | 184 | sparse | — |

Tables below 100 slots — 349 of the 409 — are mostly per-instruction-family encoders (5–80 slots each) sitting adjacent to the corresponding rodata format constants. They are catalogued in `ptxas_data_tables.json` for completeness but rarely warrant individual analysis.

### Catalog Addendum — Tables Audited After Initial Wave

A targeted re-audit of `ptxas_data_tables.json` surfaced several tables in the 158–244-slot range that the initial catalog skipped over. They sort into two structural groups.

**Group A — fourth four-tier SM-tier family (240/243/244 slots, ~73–74 nullsubs each).** Same shape as the `0x22a5aa0` family (page §Per-SM-Tier Encoder Index Tables) but a different per-opcode aspect — distinct slot-1 fingerprints and a target range that overlaps the opcode-printer corpus rather than the opcode-encoder corpus. Strong evidence this is a *second* parallel 4-tier vtable, likely the per-opcode *validate-or-cost* probe parallel to the encoder vtable.

All four tables share slot 0 = `nullsub_469` (the OP_INVALID sentinel — uniform across the cohort, so it cannot fingerprint individual tables). The **distinguishing slot is slot 1**: it varies across all four members and is the only practical disambiguator when reading a dispatch trace.

| Address | Slots | Uniq | Nullsubs | Tier reading | Notes |
|---|---|---|---|---|---|
| `0x21f6a90` | 244 | 244 | 74 | latest tier (most populated) | Target range `0xA393D0..0x1BB0FA0`; slot 0 = `nullsub_469`, slot 1 = `sub_C1EF80`, slot 2 = `sub_A393D0` (slot-2 is a fixed prologue handler shared with siblings) |
| `0x23d8178` | 243 | 243 | 73 | mid tier | Slot 0 = `nullsub_469`, slot 1 = `sub_19FF740` (only sibling with this slot-1 target), slot 2 = `sub_A393D0` |
| `0x22b5150` | 243 | 243 | 73 | mid tier (alt) | Slot 0 = `nullsub_469`, slot 1 = `sub_C1EF80` (matches `0x21f6a90` at slot 1; collision means these two tables agree on the first non-sentinel entry — diff the remaining slots to separate them), slot 2 = `sub_A393D0` |
| `0x21fa5e0` | 240 | 240 | 73 | earliest tier | Smallest population, distinct slot 1 = `sub_C5DBB0` — predates four opcodes added in later tiers |

The 240→243→243→244 progression matches the "opcodes added per SM generation, never removed" rule documented for the `0x22a5aa0` family. Diffing slot lists across the four would yield the exact opcode set added per SM generation for whichever aspect this vtable controls. **Confidence: HIGH for structural classification, MED for "validate/cost" role attribution — the constant `sub_A393D0` at slot 2 of every entry suggests a fixed prologue handler, which is more consistent with a probe/cost role than with a printer or encoder role.**

**Group B — uncited single-tier per-opcode dispatch tables.** No 4-tier family detected; each likely a one-off per-subsystem dispatch.

| Address | Slots | Uniq | Nullsubs | Best-guess role |
|---|---|---|---|---|
| `0x21f7df8` | 234 | 234 | 208 | Per-opcode handler for an *early* SM tier — 208/234 holes is the highest sparsity in the catalog, consistent with "feature-flag dispatch that only a handful of opcodes participate in" (e.g. tensor-core fragment ops circa SM80) |
| `0x23549c0` | 204 | 204 | 120 | Per-opcode dispatch in the `0x1398000` code range (distinct from the main encoder corpus at `0xA39xxx..0xC5Dxxx`) — likely a secondary lowering pipeline, possibly the cuLINK/relocation path |
| `0x21d2598` | 164 | 154 | 31 | Per-opcode dispatch in `0x6E0000..0x7FD000` (early code section, pre-mercury) — 10 slots share handlers (uniq 154/164), the only mid-size table with non-trivial slot reuse outside the SM-tier families. Likely Mercury *preprocess* dispatch |

**Group C — uniform-target tables (uniq ≤ 2).** Catalog skipped these because they don't carry per-opcode handler information, but listing them is useful for completeness — they signal "computed-goto threads that the catalog already cataloged elsewhere but never named":

| Address | Slots | Uniq | Pattern reading |
|---|---|---|---|
| `0x1d0e128` | 244 | 2 | Two-target goto thread — early-section pair |
| `0x1d0b880` | 238 | 1 | Single-function thread, `0x1d0xxxx` early code |
| `0x22b74a0` | 211 | 1 | Single-function thread in encoder region |
| `0x2020a90` | 211 | 2 | Two-target thread |
| `0x21f73f0` | 209 | 154 | Mid-density per-opcode dispatch — *not* a goto thread despite low uniqueness; the 154 unique funcs across 209 slots imply ~55 slots collapse to a shared fallback |
| `0x1d07848` | 203 | 2 | Two-target thread, early section |

`0x21f73f0` is the only Group C entry worth a deep-dive — its 209/154 ratio matches the `0x21d2598` pattern (per-opcode dispatch with a small shared fallback bucket) and it sits one bucket below the SM-tier-family size. Possible fifth sibling of the `0x22a5aa0` cohort with a smaller opcode universe; worth diffing against the four siblings to test.

## What the Tables Don't Cover

The 409 tables in `ptxas_data_tables.json` do **not** include:

1. **Per-opcode SASS encoder functions** (the ~1,086 SM100 handlers documented in [encoding.md § Encoder Template](./encoding.md#encoder-template)). These are reached through the mega-switch case-targets, not through a separate function-pointer table — the megafunctions call them via direct `call sub_XXXXX` instructions emitted at each case label. **(MED confidence — confirmed by absence of those addresses in any of the 409 tables.)**
2. **PhaseManager vtable** at `off_22BD5C8`. This is a 159-entry C++ vtable, not a switch jump table — its layout follows the Itanium C++ ABI and is documented in [methodology.md § Type Recovery](../methodology.md#type-recovery).
3. **Knob lookup tables** (~2,000 ROT13-encoded names). These are paired key/value records, not function-pointer arrays.
4. **Bugspec kind-string table** at `0x21F0500`. Also key/value, not dispatch.
5. **Format descriptor xmmwords** at `0x23F1CE8..0x23F2EF8`. These are 128-bit constant blobs, not function pointers — they're consumed by `_mm_loadu_si128` in the encoder template.

Item 1 is the most surprising omission. Per-opcode encoders are *not* dispatched through rodata — they're hardcoded as direct `call` targets inside each mega-switch case body. This means swapping out an opcode's encoder at runtime requires patching the case body itself, not just rewriting a table slot. The implication for binary diffing is significant: an SM tier added in ptxas v14 will probably appear as a new switch case (and a new slot in the `0x22f1f60` mega-table) rather than as a slot reassignment.

## Sidebar — The OCG Slab Allocator (vtable +24) Idiom

> ⚡ **QUIRK — register allocator routes through a vtable-dispatched slab, not malloc**
> ptxas does not allocate working-set buffers from `malloc`. Every per-compilation buffer is obtained from an arena object reachable as `*(ctx + 16)` (the OCG context's allocator handle). The handle is a polymorphic C++ object whose **vtable slot +24** is the byte-sized "raw allocate" entry point. Call shape:
>
> ```c
> // Generic form seen at dozens of call sites:
> _QWORD *alloc_obj = *(_QWORD **)(ctx + 16);  // OCG allocator handle
> void *mem = (*(__int64 (**)(_QWORD *, __int64, ...))(*alloc_obj + 24))(alloc_obj, size, ...);
> ```
>
> The first argument is `this`, the second is the byte size, and Hex-Rays renders any remaining stack slots as `double`/`__m128i` carry-over from the caller's frame — those are noise from the SysV AMD64 ABI, not real parameters. The callee uses `(this, size)` only.

This idiom is **not** confined to one subsystem. Verified call sites in `ptxas_full.c` (v13.0.88):

| `ptxas_full.c` line | Owner | Allocation size | Subsystem | Buffer role |
|---|---|---|---|---|
| 622528 | `sub_704D30` callees | 24 | early IR construction | Per-record IR node (24-byte payload) |
| 748581 | helper near `sub_823020` | 80 | mid-pipeline | 80-byte working record |
| 501648 | caller of `sub_7AB*` | 192 | mid-pipeline | 192-byte container record |
| 995796 | `sub_957160` | 2056 | **regalloc** | Pressure histogram (512 DWORDs + 2-DWORD sentinel) — see [regalloc/algorithm.md § Pressure Array Construction](../regalloc/algorithm.md#pressure-array-construction) |
| 1478513 | caller in `sub_BCEF*` | 4096 | scratch/staging | One-page scratch buffer |

Five distinct sizes (24, 80, 192, 2056, 4096) appear across the binary; the slot +24 entry point services all of them. There is no separate "small/medium/large" dispatch — the allocator decides bucket internally. The implication for reimplementers: a faithful clone cannot model regalloc's pressure arrays as stack-frame locals or as `new int[512]`. They live in the same arena as IR nodes, the same arena as the 4096-byte scratch pages, and the same arena as the 80-byte working records — and that arena is destroyed wholesale at end-of-compilation, not per-pass. Code that holds raw pointers into this arena across compilation boundaries is undefined.

**Sentinel pattern at the call site.** Every observed call site follows an identical pre-sequence: the caller writes a 3-word sentinel header (`{handle, 0, 0xFFFFFFFF}`) onto its own stack *before* the allocator call (visible at `ptxas_full.c:995791–995795` for the regalloc case). This is the arena's free-list bookkeeping — the allocator threads the returned block onto the caller's local list so that the caller's destructor can mass-release on scope exit. The `0xFFFFFFFF` is a "no successor" marker. A reimplementer who misses this sentinel will leak everything the function allocates because the arena's reclamation pass walks those headers.

**Confidence: HIGH for the vtable+24 idiom and the verified call sites; MED for the "destroyed at end-of-compilation" lifetime claim (inferred from absence of explicit free calls — the allocator's destructor is in the OCG context teardown, which has not been disassembled in detail).**

## Open Follow-Ups

Tables that warrant individual deep-dives in future work:

- **`0x23f4430` (633 unique per-slot funcs)** — appears one-to-one with no sharing. The target range (`0x1BB38B0..0x1BBD2C0`, ~38 KB of code) is too compact to be the main encoder corpus but too broad to be a single function. Likely the SASS *printer* dispatch (one printer per opcode) feeding `sass-printing.md` infrastructure. **Confidence: MED. Action: cross-ref with `sass-printing.md` opcode mnemonic list.**
- **`0x23b3a80` (2,109 slots, 150 unique, only 1 nullsub)** — resolved: this is the second ISel node C++ vtable (`off_23B3A80`), the polymorphic dispatch surface for the ISel pattern-matcher node hierarchy. The broad target range (0x9DA000–0x181D000) is the spread of virtual method bodies across the matcher and lowering ranges, not an encoder fan-out. See [Dispatch Reconciliation](#dispatch-reconciliation--four-encoder-layers-plus-an-isel-vtable) below and [Instruction Selection § Vtable Registration](./isel.md#vtable-registration-and-dispatch-table-layout). **Confidence: HIGH.**
- **The 159-entry family at `0x22a7cb8`, `0x22a9090`, `0x22a9620`, `0x22aa9f8`, `0x2399f58`** — five tables of 159 slots each (matching the [PhaseManager](../passes/phase-manager.md) phase count). These are *not* encoder tables but phase-vtable variants. They deserve a separate section in `passes/phase-manager.md`. **Confidence: HIGH. Action: hand off to passes-wiki authors.**
- **`0x21cb0f8..0x21eaa88` (eight 126-slot tables)** — a Mercury-region family. The slot-0 fingerprint differs across all eight, suggesting eight Mercury sub-pipelines (more than the six stages documented in [mercury.md](./mercury.md#architecture)). Possible that two are for `capmerc`-mode-only stages. **Confidence: MED. Action: diff slot-0s against the Mercury phase enum.**

## Cross-References

- [SASS Instruction Encoding](./encoding.md) — abstract framework, format descriptors, bitfield packer, encoder template
- [Mercury Encoder Pipeline](./mercury.md) — the 6-stage sub-pipeline whose master encoder calls into these tables
- [Capsule Mercury & Finalization](./capmerc.md) — capmerc-mode variations
- [Code Generation Overview](./overview.md) — where the encoder sits in the global pipeline
- [Methodology](../methodology.md) — confidence-level conventions and the IDA extraction protocol that produced `ptxas_data_tables.json`
