# SASS Encoding Dispatch & Format Tables

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

Browsable data dump for the [SASS encoder](../../codegen/encoding.md) and its [function-pointer dispatch tables](../../codegen/encoding-tables.md). Every value here is read directly from the binary's `.rodata`; the consuming code is documented on the encoder pages.

## Encoder Dispatch Layers

The encoder routes one logical instruction through four function-pointer layers plus a scalar opcode→slot table, all drawing handlers from one shared pool. The per-SM array is the authoritative per-architecture override.

| Layer | Region VA | Indexing scheme | Keyed by | Distinct handlers | Note |
|---|---|---|---|---|---|
| `opcode_to_encoding` | `0x22B4B60` | flat `u16[222]` | opcode index | — | opcode → encoding slot (sentinel 355) |
| `encoding_tree_1` | `0x233BE00`–`0x2353E00` | 16-byte slotted tree (274 internal / 5,169 leaf) | `(format_id<<8)\|minor` | 4,618 | primary format + handler selection |
| `encoding_tree_2` | `0x235CE00`–`0x2379E00` | 16-byte slotted tree (427 internal / 6,251 leaf) | `(format_id<<8)\|minor` | — | secondary/extended tree (shares pool) |
| `sass_handler_dispatch_1` | `0x22C0E00`–`0x22F1E00` | `(opcode, category, variant)` sub-tables | opcode id | 6,915 | superset emit-handler list |
| `sass_handler_dispatch_2` | `0x2379E00`–`0x2399E00` | `(opcode, category, variant)` sub-tables | opcode id | 3,478 | strict subset of `_1` |
| `per_sm_handler_dispatch` | `0x22E7AD0`–`0x23B99D0` | 5 × `{u64 op, u64 ptr, u64 pad}` arrays | `(format_id<<8)\|minor` | 2,421 | per-arch override (sm50_7x / sm75 / sm100 / sm80_8x / sm86_89) |
| `isel_vtable` (`off_22AD230`, `off_23B3A80`) | `0x22AD230`, `0x23B3A80` | C++ vtables | virtual slot | 3,707 | **ISel node vtables — NOT encoder dispatch** |
| `encoder_handler_union` | — | — | — | 8,874 | union of tree + sass1 + sass2 + per_sm |

### Reconciliation matrix (handlers shared between layers)

| Layer | encoding_tree | sass_dispatch_1 | sass_dispatch_2 | per_sm_dispatch | isel_vtable |
|---|---|---|---|---|---|
| encoding_tree | 4,618 | 2,828 | 3,367 | 2,194 | 0 |
| sass_dispatch_1 | 2,828 | 6,915 | 2,832 | 1,711 | 0 |
| sass_dispatch_2 | 3,367 | 2,832 | 3,478 | 1,899 | 0 |
| per_sm_dispatch | 2,194 | 1,711 | 1,899 | 2,421 | 0 |
| isel_vtable | 0 | 0 | 0 | 0 | 3,707 |

The `isel_vtable` row/column is zero against every encoder layer: the two C++ vtables share no handler with any genuine encoder dispatch, which is the proof that they are ISel node vtables, not an alternate encoder path.

### Encoding-tree geometry (16-byte slots)

| Tree | Window | slots in window | internal | leaf | reserved tail |
|---|---|---|---|---|---|
| encoding_tree_1 | `0x233BE00`–`0x2353E00` | 6,144 | 274 | 5,169 | 701 |
| encoding_tree_2 | `0x235CE00`–`0x2379E00` | 7,424 | 427 | 6,251 | 746 |
| total | — | 13,568 | 701 | 11,420 | 1,447 |

Leaf flavors (across 11,420 leaves): `key_handler` 4,117 (`{dispatch_key, handler}`; 396 distinct keys, 2,542 distinct handlers), `handler_value` 3,635 (`{handler, extra}`), `null_or_data` 3,668 (inline data or terminator).

### Opcode → encoding slot — sentinel-355 opcodes

`opcode_to_encoding` (`u16[222]`) carries 117 non-zero non-sentinel slots, 95 zeros, and **10 sentinel-355** entries. Sentinel 355 means "extended / macro-lowered — no default encoding, requires the ISel resolver's SM-specific vtable override":

| Opcode | Mnemonic | Opcode | Mnemonic |
|---|---|---|---|
| 40 | FCHK | 213 | CREATEPOLICY |
| 49 | FRND | 214 | CVTA |
| 169 | S2UR | 215 | DMMA |
| 212 | CGAERRBAR | 216 | ELECT |
| — | — | 219 | FENCE_S |
| — | — | 220 | FMNMX |

## Format Descriptors (38 entries)

Each format descriptor is a 136-byte `.rodata` record at `0x23F1D70`+. The `xmmword` header is loaded into the encoding context at `a1+8`; the trailing slot arrays define operand geometry. `format_id` is the value emitted at bits[25:31]; `width_bits` is the instruction word width; `slot_sizes`/`slot_types`/`slot_flags` are the per-slot geometry.

| # | VA | Label | format_id | width | slots | slot_sizes | slot_types | slot_flags | encoders |
|---|---|---|---|---|---|---|---|---|---|
| 0 | `0x23F1D70` | 64b_B | 4 | 64 | 1 | 8 | — | 0 | 70 |
| 1 | `0x23F1DF8` | 128b_0x03 | 8 | 128 | 2 | 10,17 | 28 | 0,3 | 202 |
| 2 | `0x23F1E80` | 128b_0x09_idx2 | 9 | 128 | 2 | 8,17 | 28 | 0,4 | 0 |
| 3 | `0x23F1F08` | 64b_A | 3 | 64 | 1 | 10 | — | 0 | 215 |
| 4 | `0x23F1F90` | 64b_C | 2 | 64 | 1 | 8 | 12 | 0 | 20 |
| 5 | `0x23F2018` | 128b_0x07 | 6 | 128 | 2 | 10,17 | 24 | 0,3 | 26 |
| 6 | `0x23F20A0` | 128b_0x04_idx6 | 4 | 128 | 2 | 10,17 | 20 | 0,3 | 0 |
| 7 | `0x23F2128` | 128b_0x09 | 7 | 128 | 2 | 8,17 | 24 | 0,4 | 2 |
| 8 | `0x23F21B0` | 128b_0x0A | 10 | 128 | 2 | 10,17 | — | 0,3 | 135 |
| 9 | `0x23F2238` | 64b_D | 2 | 64 | 1 | 10 | 14 | 0 | 17 |
| 10 | `0x23F22C0` | 128b_0x09_idx10 | 9 | 128 | 2 | 8,17 | 28 | 0,4 | 0 |
| 11 | `0x23F2348` | 128b_0x0D | 8 | 128 | 2 | 10,17 | 28 | 0,3 | 11 |
| 12 | `0x23F23D0` | 128b_0x0B_idx12 | 11 | 128 | 2 | 8,17 | — | 0,4 | 0 |
| 13 | `0x23F2458` | 128b_0x08_idx13 | 8 | 128 | 2 | 8,17 | 26 | 0,4 | 0 |
| 14 | `0x23F24E0` | 64b_0x03_idx14 | 3 | 64 | 1 | 6 | 12 | 0 | 0 |
| 15 | `0x23F2568` | 128b_0x08_idx15 | 8 | 128 | 2 | 12,17 | 30 | 0,2 | 0 |
| 16 | `0x23F25F0` | 128b_0x12 | 9 | 128 | 2 | 10,17 | 30 | 0,3 | 21 |
| 17 | `0x23F2678` | 128b_0x13 | 9 | 128 | 2 | 12,17 | — | 0,2 | 143 |
| 18 | `0x23F2700` | 128b_0x0B_idx18 | 11 | 128 | 2 | 6,17 | 30 | 0,5 | 0 |
| 19 | `0x23F2788` | 64b_0x05_idx19 | 5 | 64 | 1 | 6 | — | 0 | 0 |
| 20 | `0x23F2810` | 128b_0x16 | 7 | 128 | 2 | 10,17 | 26 | 0,3 | 6 |
| 21 | `0x23F2898` | 128b_0x05_idx21 | 5 | 128 | 2 | 12,17 | 24 | 0,2 | 0 |
| 22 | `0x23F2920` | 128b_0x05_idx22 | 5 | 128 | 2 | 8,17 | 20 | 0,4 | 0 |
| 23 | `0x23F29A8` | 128b_0x19 | 7 | 128 | 2 | 12,17 | 28 | 0,2 | 152 |
| 24 | `0x23F2A30` | 128b_0x06_idx24 | 6 | 128 | 2 | 12,17 | 26 | 0,2 | 0 |
| 25 | `0x23F2AB8` | 128b_0x0D_idx25 | 13 | 128 | 3 | 12,17,33 | 42 | 0,2,9 | 0 |
| 26 | `0x23F2B40` | 128b_0x0E_idx26 | 14 | 128 | 3 | 8,17,33 | 40 | 0,4,11 | 0 |
| 27 | `0x23F2BC8` | 128b_0x07_idx27 | 7 | 128 | 2 | 8,17 | 24 | 0,4 | 0 |
| 28 | `0x23F2C50` | 64b_E | 1 | 64 | 1 | 10 | 12 | 0 | 1 |
| 29 | `0x23F2CD8` | 128b_0x10_idx29 | 16 | 128 | 3 | 8,17,33 | 44,52,68,84,100,116 | 0,4,11 | 0 |
| 30 | `0x23F2D60` | 64b_0x04_idx30 | 4 | 64 | 1 | 6 | 14 | 0 | 0 |
| 31 | `0x23F2DE8` | 128b_0x21 | 6 | 128 | 2 | 10,17 | 24 | 0,3 | 2 |
| 32 | `0x23F2E70` | 64b_0x02_idx32 | 2 | 64 | 1 | 12 | — | 0 | 0 |
| 33 | `0x23F2EF8` | 128b_0x23 | 7 | 128 | 2 | 12,17 | 28 | 0,2 | 9 |
| 34 | `0x23F2F80` | 128b_0x07_idx34 | 7 | 128 | 2 | 14,17 | 30 | 0,1 | 0 |
| 35 | `0x23F3008` | 128b_0x08_idx35 | 8 | 128 | 2 | 14,17 | — | 0,1 | 0 |
| 36 | `0x23F3090` | 128b_0x0C_idx36 | 12 | 128 | 3 | 14,17,33 | 42 | 0,1,8 | 0 |
| 37 | `0x23F3118` | 128b_0x0D_idx37 | 13 | 128 | 3 | 10,17,33 | 40 | 0,3,10 | 0 |

**Universal slot template** (`0x23F1C60`, refcount 7,302 — the most-shared slot-size template): sizes `[3, 2, 4, 6, 8]`. Slot-size values `10`/`12` = register, `17` = immediate/cbuf, `33` = wide third slot, `-1` = unused. Slot-type `28` = register-type, `12`/`14` = alt, `-1` = unused. Slot-flag `0` = default, `2`/`3`/`4` = secondary/uniform-extended class, `-1` = unused.

## Modifier Value Tables (40 arrays)

Forty `.rodata` arrays at `0x22FCD20`–`0x22FD580` (2,144 bytes total) that map an Ori-modifier enum value to a SASS field value. The lookup is `TABLE[ir_val - BASE]`; a `-1` entry is an illegal/reserved slot (the encoder propagates `-1` as an encoding error). 11 are identity maps, 4 are byte-width (wide enum spaces up to 256), 3 contain `-1` gap entries.

| VA | Label | type | count | identity | gaps | values |
|---|---|---|---|---|---|---|
| `0x22FCD20` | rounding_mode_023 | dword | 3 | no | no | 0,2,3 |
| `0x22FCD30` | gap_table_neg1 | dword | 5 | no | **yes** | 0,-1,2,3,4 |
| `0x22FCD50` | gap_table_dual_neg1 | dword | 6 | no | **yes** | 0,-1,1,-1,2,3 |
| `0x22FCD70` | swap_01 | dword | 4 | no | no | 1,0,2,3 |
| `0x22FCD80` | gap_no_3 | dword | 4 | no | no | 0,1,2,4 |
| `0x22FCD90` | fold_last_two | dword | 4 | no | no | 0,1,2,2 |
| `0x22FCDA0` | large_jump_9_to_12 | dword | 19 | no | no | 0,1,2,3,4,5,6,7,8,12,13,14,15,17,18,19,20,21,22 |
| `0x22FCDF4` | fold_3_3 | dword | 4 | no | no | 1,2,3,3 |
| `0x22FCE1C` | complex_remapping_15 | dword | 15 | no | **yes** | 0,10,11,17,19,20,-1,25,26,28,42,35,12,33,34 |
| `0x22FCE60` | alt_remapping_11 | dword | 11 | no | no | 10,11,17,19,20,21,25,26,28,34,35 |
| `0x22FCEA4` | pair_fold_group | dword | 18 | no | no | 0,1,1,2,3,4,5,0,1,2,3,4,5,7,8,10,11,12 |
| `0x22FCEF4` | identity_4a | dword | 4 | **yes** | no | 0,1,2,3 |
| `0x22FCF10` | compound_remap_13 | dword | 13 | no | no | 0,1,0,1,6,7,8,17,18,19,20,21,22 |
| `0x22FCF50` | offset_2_to_6 | dword | 6 | no | no | 0,2,3,4,5,6 |
| `0x22FCF70` | rotation | dword | 5 | no | no | 1,2,3,0,4 |
| `0x22FCF90` | fold_0_1_1_2_2 | dword | 5 | no | no | 0,1,1,2,2 |
| `0x22FCFAC` | pair_1_3 | dword | 2 | no | no | 1,3 |
| `0x22FCFC4` | fold_2_dup | dword | 9 | no | no | 0,1,2,2,3,4,5,6,7 |
| `0x22FCFF0` | split_block | dword | 9 | no | no | 0,1,2,4,5,0,3,4,5 |
| `0x22FD020` | identity_byte_64 | byte | 64 | **yes** | — | 0..63 |
| `0x22FD060` | stride_skip_byte_36 | byte | 36 | no | — | 0,1,3,4,6,7,9,10,…,52 (skip every 3rd) |
| `0x22FD0A0` | identity_byte_96 | byte | 96 | **yes** | — | 0..95 |
| `0x22FD100` | small_identity_8 | dword | 8 | **yes** | no | 0..7 |
| `0x22FD150` | skip_4 | dword | 6 | no | no | 0,1,2,3,5,6 |
| `0x22FD170` | skip_1_3 | dword | 5 | no | no | 0,2,4,5,6 |
| `0x22FD18C` | stride_9_then_1_4 | dword | 9 | no | no | 0,9,18,27,36,1,2,3,4 |
| `0x22FD1B8` | fold_1_2_2_3_4 | dword | 5 | no | no | 1,2,2,3,4 |
| `0x22FD1E0` | jump_5_to_9 | dword | 13 | no | no | 0,1,2,3,4,5,9,10,11,12,13,14,15 |
| `0x22FD220` | identity_byte_256 | byte | 256 | **yes** | — | 0..255 |
| `0x22FD320` | power_encoding_1_3_15 | dword | 3 | no | no | 1,3,15 |
| `0x22FD354` | identity_7_plus_3 | dword | 10 | no | no | 1,2,3,4,5,6,0,1,2,3 |
| `0x22FD384` | identity_22 | dword | 22 | no | no | 1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,0,1,2,3,4,5,7 |
| `0x22FD3E4` | identity_17_rotated | dword | 17 | no | no | 1..11,0,1,2,3,4,5 |
| `0x22FD440` | identity_8_dup_tail | dword | 13 | no | no | 0,1,2,3,4,5,6,7,2,3,4,5,6 |
| `0x22FD480` | identity_5 | dword | 5 | **yes** | no | 0..4 |
| `0x22FD498` | tristate | dword | 3 | **yes** | no | 0,1,2 |
| `0x22FD4C0` | identity_11 | dword | 11 | **yes** | no | 0..10 |
| `0x22FD500` | identity_9 | dword | 9 | **yes** | no | 0..8 |
| `0x22FD540` | identity_10 | dword | 10 | **yes** | no | 0..9 |
| `0x22FD570` | quaternary | dword | 4 | **yes** | no | 0,1,2,3 |

## Tier-2 Per-Architecture Modifier Defaults

The Tier-2 modifier table (loaded into the encoding context at `a1+404`) selects per-SM encoding variations for the same Tier-1 format layout. The groups cluster by SM generation; each 16-byte slot is two `{lo, hi}` packed words.

| Group | VA | SM range | dwords (per slot, abbreviated) |
|---|---|---|---|
| group_A_maxwell_turing | `0x202A280` | sm_50–sm_75 | 12 slots; `{0,1,1,1}`,`{2,1,3,1}`,`{0,1,2,1}`,`{3,1,4,1}`,`{0,2,2,2}`,`{4,2,6,2}`, then `-1`-terminated pairs |
| group_B_ampere_ada | `0x22F1B30` | sm_80–sm_89 | `{0,2,1,1}`,`{0,1,1,2}`,`{2,1,3,2}` |
| group_D_lovelace_hopper | `0x22F1BA0` | sm_89–sm_90 | `{1,1,3,1}`,`{0,2,2,1}` |
| group_E_blackwell_dc | `0x22F1AA0` | sm_100–sm_103 | `{0,2,1,2}`,`{4,1,5,1}`,`{0,2,3,2}`,`{4,2,5,2}` |
| group_F_blackwell_consumer | `0x22F1C20` | sm_120–sm_121 | `{0,4,1,4}`,`{2,4,3,4}` |
| group_G_cross_arch | `0x23B2DE0` | cross-architecture | `{0,1,1,4}` |

The `4` appearing in the high word of every `group_F` slot is the consumer-Blackwell encoding-class tag; `group_A` uses `1`/`2`, matching the Maxwell–Turing two-class split. These constants are the per-SM `setBits` triples that make the per-SM handler arrays byte-distinct while sharing the same handler bodies.

## Cross-References

- [SASS Instruction Encoding](../../codegen/encoding.md) — encoder template, bitfield packer, format descriptor architecture
- [SASS Encoding Dispatch Tables](../../codegen/encoding-tables.md) — the function-pointer tables and dispatch reconciliation
- [Instruction Selection](../../codegen/isel.md) — the ISel resolver `sub_BFEBF0` and sentinel-355 handling
