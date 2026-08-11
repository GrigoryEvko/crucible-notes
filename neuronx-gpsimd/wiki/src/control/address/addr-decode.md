# SoC Physical-Address Bitfield Layout (64-die mesh)

This page reconstructs the **Cayman SoC physical-address bitfield codec** — the
RTL-generated header that splits a 64-bit physical address into a per-die *local*
byte offset plus the *inter-die mesh routing* fields that steer a transaction to one
of up to **64 Cayman chips** and one of **2 dies per package**. Two views of the same
64-bit envelope ship: the **LOCAL** decoder (`cayman_addr_decode.h`, a die's own view
— "which chip, which die, which byte") and the **NEIGHBOR** decoder
(`cayman_addr_decode_neighbor.h`, the post-route egress view — "cross seng?, cross
die?, take neighbor route?, hit PEB?"). This is the field codec that the cross-die
[`remote_routing_id` fold](../../dma/rdma-cross-die.md) composes and that the
[D2D / PCIe fabric](pkl-pcie-d2d-fabric.md) consumes.

Everything below is **macro-proven** from the shipped RTL-generated headers — the C
GET/SET accessor macros, the Verilog `` `define `` GET macros, and the Verilog
`struct packed` field list. Every bit range and width is derivable by independent
shift/mask arithmetic (popcount of each mask, struct width-sum unpack MSB-first),
and the three forms — C macro, Verilog macro, Verilog struct — **agree exactly**
for both decoders.

**Provenance** (gitignored extracted tree; absolute paths):

| view | C header (`.h`) | Verilog (`.vh`) | second copy (customop-lib) |
|---|---|---|---|
| LOCAL | `…/cayman-arch-regs_tgz/output/address_map/cayman_addr_decode.h` | `…/cayman_addr_decode.vh` | `…/customop-lib_0.21.2.0_amd64/…/arch-headers/cayman/cayman_addr_decode.h` |
| NEIGHBOR | `…/cayman_addr_decode_neighbor.h` | `…/cayman_addr_decode_neighbor.vh` | `…/arch-headers/cayman/cayman_addr_decode_neighbor.h` |

> **NOTE — confidence tags.** Per the [confidence model](../reference/confidence-model.md):
> `OBSERVED` = byte/macro/struct read from a shipped artifact;
> `INFERRED` = reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited
> cross-page anchor; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK**
> (counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION**
> (overturns a naive reading), **NOTE** (orientation). All bit positions and widths
> here are **HIGH·OBSERVED** (proven by the macro math); per-field *semantic* readings
> of the boolean routing flags are **MED·INFERRED** from the field names — the headers
> carry no prose. Cayman is **NC-v3**, byte-grounded; any v5/Maverick reading is
> **INFERRED** (these headers are the Cayman pair).

---

## 1. The 64-bit envelope — both decoders at a glance

Both decoders define exactly the same 64-bit envelope: only bits **[57:0]** are
addressed by any macro (58 defined bits); **[63:58]** are untouched/undefined. The two
decoders are **bit-for-bit identical in [47:0]** (LOCAL + DIE) and in the top two
attribute bits **[56], [57]**; they differ only in how the middle window **[55:48]** is
allocated.

```
 bit  63 ............. 58 | 57 | 56 | 55 | 54 | 53 .. 48 | 47 | 46 ................... 0
      └── undefined (6) ──┘  OK   RO  RSV  VLD └ CAYMAN_ID┘ DIE └────── LOCAL (47) ──────┘   <- LOCAL view
                            OK   RO  PU  VLD  PEB NR ED ES R2 DIE └────── LOCAL (47) ──────┘   <- NEIGHBOR view
```

(NEIGHBOR `[53..48]` = `PEB`, `NEIGHBOR_ROUTE`, `EXIT_DIE`, `EXIT_SENG`, then the 2-bit
`NEIGHBOR_RSVD` shown `R2`.)

> **CORRECTION — "47-bit local vs 57-bit cross-die" framing.** The widths recovered
> from the structs are **LOCAL = 47 bits `[46:0]`** and a **defined envelope of 58 bits
> `[57:0]`** (not 57). There is no single 57-bit field. The *cross-die routing key* —
> the part the inter-die fabric uses to pick a chip+die, below the `[57:55]` transport
> attributes — is `{LOCAL[46:0], DIE[47], CAYMAN_ID[53:48], CAYMAN_ID_VALID[54]}`,
> i.e. geometry occupies **`[54:0]` = 55 bits**; with the three attribute bits `[57:55]`
> the full envelope is **58 bits**. The "47 vs 57" shorthand is approximate: 47 is the
> exact per-die LOCAL width; the "cross-die" number should be read as the 55-bit routed
> geometry (or 58-bit full envelope), **not** a 57-bit field.

---

## 2. LOCAL decoder — `cayman_addr_decode`

A die's own / local view: *which chip (0..63), which die (0/1), which byte (47-bit)*,
plus three transport-attribute bits at the top.

### 2.1 Bit-field layout (MSB → LSB)

| Bit range | Field | Width | Meaning | Conf |
|---|---|---|---|---|
| `[57]` | `OK_TO_FAIL` | 1 | Transaction may fail silently (poison / ok-to-fail) | MED·INFERRED |
| `[56]` | `PCIE_ATTR_RELAXED_ORDERING` | 1 | PCIe TLP relaxed-ordering attribute hint | MED·INFERRED |
| `[55]` | `RESERVED` | 1 | Reserved | HIGH·OBSERVED |
| `[54]` | `CAYMAN_ID_VALID` | 1 | `CAYMAN_ID` is meaningful / route by chip id | HIGH·OBSERVED |
| `[53:48]` | `CAYMAN_ID` | 6 | Chip id in the 64-die mesh (2^6 = 64 chips) | HIGH·OBSERVED |
| `[47]` | `DIE` | 1 | Die selector within a Cayman package (2 dies) | HIGH·OBSERVED |
| `[46:0]` | `LOCAL` | 47 | Per-die intra-die byte address (128 TiB / die) | HIGH·OBSERVED |
| `[63:58]` | *(undefined)* | 6 | Not addressed by any macro | HIGH·OBSERVED |

### 2.2 C accessor macros (`cayman_addr_decode.h`)

Every field has a **GET** (shift-and-mask) and a **SET** (clear-and-OR) macro; the GET
mask equals the SET mask per field, so encode/decode is **symmetric and lossless**
(`GET(SET(a,v)) == v` for any in-range `v`, and `SET` preserves all other fields).

| Field | GET (verbatim) | shift / mask | derived range |
|---|---|---|---|
| `LOCAL` | `(addr>>0ULL)&0x7fffffffffffULL` | 0 / `0x7fffffffffff` (47 set bits) | `[46:0]` |
| `DIE` | `(addr>>47ULL)&0x1ULL` | 47 / `0x1` | `[47]` |
| `CAYMAN_ID` | `(addr>>48ULL)&0x3fULL` | 48 / `0x3f` (6 set bits) | `[53:48]` |
| `CAYMAN_ID_VALID` | `(addr>>54ULL)&0x1ULL` | 54 / `0x1` | `[54]` |
| `RESERVED` | `(addr>>55ULL)&0x1ULL` | 55 / `0x1` | `[55]` |
| `PCIE_ATTR_RELAXED_ORDERING` | `(addr>>56ULL)&0x1ULL` | 56 / `0x1` | `[56]` |
| `OK_TO_FAIL` | `(addr>>57ULL)&0x1ULL` | 57 / `0x1` | `[57]` |

The SET macros are the canonical clear-then-insert idiom, e.g. for `CAYMAN_ID`:

```c
#define CAYMAN_ADDR_DECODE_SET_CAYMAN_ID(addr,value) \
    ((addr & ~(0x3fULL<<48ULL)) | ((value & 0x3fULL)<<48ULL))
```

### 2.3 Verilog packed struct (`cayman_addr_decode.vh`)

SystemVerilog `struct packed` lays members **MSB-first** (first member = most
significant). Unpacking the widths top-to-bottom reproduces the macro positions
exactly; widths sum to **58**, bottom field at **bit 0**.

```systemverilog
typedef struct packed {
  logic        ok_to_fail                 // [57]
  logic        pcie_attr_relaxed_ordering // [56]
  logic        reserved                   // [55]
  logic        cayman_id_valid            // [54]
  logic  [5:0] cayman_id                  // [53:48]
  logic        die                        // [47]
  logic [46:0] local                      // [46:0]
} cayman_addr_decoder_t;
```

> **QUIRK — generator emits no member semicolons.** The shipped `.vh` omits the trailing
> `;` on every `logic` member line (verbatim above). This is a cosmetic defect of the
> emitter; it does not affect the bit layout, but a strict SystemVerilog parser will
> reject the file as-is — add the semicolons before compiling. The `.vh` also ships only
> **GET** macros (`` `CAYMAN_ADDR_DECODE_GET_LOCAL(addr) `` → `addr[46:0]` etc.), **no
> SET** form; the C header carries both directions.

---

## 3. NEIGHBOR decoder — `cayman_addr_decode_neighbor`

The post-route / egress view. Same `#include`/`` `ifndef `` guard name
(`CAYMAN_ADDR_DECODE_H`) and same struct typedef name (`cayman_addr_decoder_t`) as the
LOCAL decoder — it is a **drop-in alternate view selected at include time, not a
co-resident second type**. The two headers are **mutually exclusive in one translation
unit** (include exactly one).

### 3.1 Bit-field layout (MSB → LSB)

| Bit range | Field | Width | Meaning | Conf |
|---|---|---|---|---|
| `[57]` | `OK_TO_FAIL` | 1 | Ok-to-fail attribute *(unchanged vs LOCAL)* | MED·INFERRED |
| `[56]` | `PCIE_ATTR_RELAXED_ORDERING` | 1 | PCIe relaxed-ordering *(unchanged)* | MED·INFERRED |
| `[55]` | `PCIE_U_RSVD` | 1 | PCIe user-reserved *(where LOCAL `RESERVED` sat)* | HIGH·OBSERVED |
| `[54]` | `ID_VALID` | 1 | Routing-id valid *(parallel to `CAYMAN_ID_VALID`)* | HIGH·OBSERVED |
| `[53]` | `PEB` | 1 | Target / route to the PEB subsystem | MED·INFERRED |
| `[52]` | `NEIGHBOR_ROUTE` | 1 | Engage the neighbor-routing path | MED·INFERRED |
| `[51]` | `EXIT_DIE` | 1 | Cross the originating-die boundary | MED·INFERRED |
| `[50]` | `EXIT_SENG` | 1 | Leave the SEngine (Sequencer Engine) domain | MED·INFERRED |
| `[49:48]` | `NEIGHBOR_RSVD` | 2 | Reserved *(where `CAYMAN_ID[1:0]` sat locally)* | HIGH·OBSERVED |
| `[47]` | `DIE` | 1 | Die selector *(unchanged)* | HIGH·OBSERVED |
| `[46:0]` | `LOCAL` | 47 | Per-die local address *(unchanged)* | HIGH·OBSERVED |
| `[63:58]` | *(undefined)* | 6 | Not addressed | HIGH·OBSERVED |

### 3.2 C accessor macros (`cayman_addr_decode_neighbor.h`)

`LOCAL`, `DIE`, `PCIE_ATTR_RELAXED_ORDERING`, `OK_TO_FAIL` are **byte-identical** to the
LOCAL decoder. The remaining fields replace the `CAYMAN_ID` window:

| Field | GET (verbatim) | shift / mask | derived range |
|---|---|---|---|
| `LOCAL` | `(addr>>0ULL)&0x7fffffffffffULL` | 0 / `0x7fffffffffff` | `[46:0]` |
| `DIE` | `(addr>>47ULL)&0x1ULL` | 47 / `0x1` | `[47]` |
| `NEIGHBOR_RSVD` | `(addr>>48ULL)&0x3ULL` | 48 / `0x3` (2 bits) | `[49:48]` |
| `EXIT_SENG` | `(addr>>50ULL)&0x1ULL` | 50 / `0x1` | `[50]` |
| `EXIT_DIE` | `(addr>>51ULL)&0x1ULL` | 51 / `0x1` | `[51]` |
| `NEIGHBOR_ROUTE` | `(addr>>52ULL)&0x1ULL` | 52 / `0x1` | `[52]` |
| `PEB` | `(addr>>53ULL)&0x1ULL` | 53 / `0x1` | `[53]` |
| `ID_VALID` | `(addr>>54ULL)&0x1ULL` | 54 / `0x1` | `[54]` |
| `PCIE_U_RSVD` | `(addr>>55ULL)&0x1ULL` | 55 / `0x1` | `[55]` |
| `PCIE_ATTR_RELAXED_ORDERING` | `(addr>>56ULL)&0x1ULL` | 56 / `0x1` | `[56]` |
| `OK_TO_FAIL` | `(addr>>57ULL)&0x1ULL` | 57 / `0x1` | `[57]` |

Each field also has its matching clear-and-OR SET macro with the same mask.

### 3.3 Verilog packed struct (`cayman_addr_decode_neighbor.vh`)

```systemverilog
typedef struct packed {
  logic        ok_to_fail                 // [57]
  logic        pcie_attr_relaxed_ordering // [56]
  logic        pcie_u_rsvd                // [55]
  logic        id_valid                   // [54]
  logic        peb                        // [53]
  logic        neighbor_route             // [52]
  logic        exit_die                   // [51]
  logic        exit_seng                  // [50]
  logic  [1:0] neighbor_rsvd              // [49:48]
  logic        die                        // [47]
  logic [46:0] local                      // [46:0]
} cayman_addr_decoder_t;
```

MSB-first width-sum unpack reproduces the macro positions exactly (sum = 58, bottom at
bit 0). The `.vh` GET macros match the C macros field-for-field. Same no-semicolon
generator quirk as §2.3.

### 3.4 LOCAL → NEIGHBOR diff (what `[55:48]` becomes)

| span | LOCAL | NEIGHBOR | note |
|---|---|---|---|
| `[46:0]` | `LOCAL` (47) | `LOCAL` (47) | **unchanged** |
| `[47]` | `DIE` | `DIE` | **unchanged** |
| `[53:48]` | `CAYMAN_ID` (6) | split → `NEIGHBOR_RSVD[49:48]`, `EXIT_SENG[50]`, `EXIT_DIE[51]`, `NEIGHBOR_ROUTE[52]`, `PEB[53]` | **repurposed** |
| `[54]` | `CAYMAN_ID_VALID` | `ID_VALID` | **renamed** (same bit) |
| `[55]` | `RESERVED` | `PCIE_U_RSVD` | **renamed** (same bit) |
| `[56]` | `PCIE_ATTR_RELAXED_ORDERING` | same | **unchanged** |
| `[57]` | `OK_TO_FAIL` | same | **unchanged** |

The LOCAL view says **"which chip (0..63)"** via the 6-bit `CAYMAN_ID`; the NEIGHBOR
view replaces that single field with a small set of boolean **"which boundary to cross /
which fabric exit to take"** routing flags. Same 64-bit envelope, same attribute bits at
`[54..57]`. `[HIGH for the bit diff; MED·INFERRED for the per-flag semantics]`

---

## 4. C pseudocode for both decoders (reproduced from the macros)

The codec is the standard shift/mask bitfield pattern; there is **no monolithic encode
macro** — encode = chain the per-field `SET_*`, decode = chain the per-field `GET_*`.
Field/macro names below are the **real** ones from the headers.

```c
/* ===== LOCAL decoder (cayman_addr_decode.h) ===== */
typedef struct { uint64_t local; uint8_t die, cayman_id, cayman_id_valid,
                          reserved, pcie_attr_ro, ok_to_fail; } cayman_local_t;

static inline cayman_local_t cayman_local_decode(uint64_t a) {
    return (cayman_local_t){
        .local           = (a >> 0)  & 0x7fffffffffffULL, /* [46:0] 47b */
        .die             = (a >> 47) & 0x1,               /* [47]        */
        .cayman_id       = (a >> 48) & 0x3f,              /* [53:48] 6b  */
        .cayman_id_valid = (a >> 54) & 0x1,               /* [54]        */
        .reserved        = (a >> 55) & 0x1,               /* [55]        */
        .pcie_attr_ro    = (a >> 56) & 0x1,               /* [56]        */
        .ok_to_fail      = (a >> 57) & 0x1,               /* [57]        */
    };
}

/* ENCODE (compose) — chain the SET macros; SET preserves all other fields */
static inline uint64_t cayman_local_encode(const cayman_local_t *f) {
    uint64_t a = 0;
    a = CAYMAN_ADDR_DECODE_SET_LOCAL(a,           f->local);            /* [46:0] */
    a = CAYMAN_ADDR_DECODE_SET_DIE(a,             f->die);              /* [47]   */
    a = CAYMAN_ADDR_DECODE_SET_CAYMAN_ID(a,       f->cayman_id);        /* [53:48]*/
    a = CAYMAN_ADDR_DECODE_SET_CAYMAN_ID_VALID(a, f->cayman_id_valid);  /* [54] = 1 when remote-chip */
    a = CAYMAN_ADDR_DECODE_SET_RESERVED(a,        f->reserved);         /* [55]   */
    a = CAYMAN_ADDR_DECODE_SET_PCIE_ATTR_RELAXED_ORDERING(a, f->pcie_attr_ro); /* [56] */
    a = CAYMAN_ADDR_DECODE_SET_OK_TO_FAIL(a,      f->ok_to_fail);       /* [57]   */
    return a;
}

/* ===== NEIGHBOR decoder (cayman_addr_decode_neighbor.h) ===== */
typedef struct { uint64_t local; uint8_t die, neighbor_rsvd, exit_seng, exit_die,
                          neighbor_route, peb, id_valid, pcie_u_rsvd,
                          pcie_attr_ro, ok_to_fail; } cayman_neighbor_t;

static inline cayman_neighbor_t cayman_neighbor_decode(uint64_t a) {
    return (cayman_neighbor_t){
        .local          = (a >> 0)  & 0x7fffffffffffULL, /* [46:0] */
        .die            = (a >> 47) & 0x1,               /* [47]   */
        .neighbor_rsvd  = (a >> 48) & 0x3,               /* [49:48] 2b */
        .exit_seng      = (a >> 50) & 0x1,               /* [50]   */
        .exit_die       = (a >> 51) & 0x1,               /* [51]   */
        .neighbor_route = (a >> 52) & 0x1,               /* [52]   */
        .peb            = (a >> 53) & 0x1,               /* [53]   */
        .id_valid       = (a >> 54) & 0x1,               /* [54]   */
        .pcie_u_rsvd    = (a >> 55) & 0x1,               /* [55]   */
        .pcie_attr_ro   = (a >> 56) & 0x1,               /* [56]   */
        .ok_to_fail     = (a >> 57) & 0x1,               /* [57]   */
    };
}
/* neighbor encode = symmetric SET_* chain; identical idiom, omitted for brevity */
```

> **GOTCHA — same guard, same typedef.** Both headers use the include guard
> `CAYMAN_ADDR_DECODE_H` and the struct name `cayman_addr_decoder_t`. Including *both*
> in one TU silently keeps **only the first** (the second is `#ifndef`-elided), so the
> `cayman_addr_decoder_t` you get is whichever header you included first — there is no
> compile error to warn you. A reimplementation must pick exactly one view per TU (or
> rename the guard/typedef before merging both).

---

## 5. The 64-die mesh routing model

The bit allocation directly implies the addressing hierarchy:

- **`LOCAL[46:0]`** — selects a byte inside one die's **47-bit** address space (**128 TiB
  / die**). This is the per-die intra-die map served by the rest of the address map
  (`seng`/`peb`/`io_d2d`/…) — see [unified SoC memory map](unified-soc-memory-map.md) and
  the [SoC master map](soc-master-map.md).
- **`DIE[47]`** — selects one of the **2 dies** of a Cayman package.
- **`{CAYMAN_ID[53:48], CAYMAN_ID_VALID[54]}`** — selects which of up to **64 Cayman
  chips** the transaction targets. `2^6 = 64` chip ids; this *is* the "64-die mesh". With
  `CAYMAN_ID_VALID = 0` the access stays on the originating chip; with `= 1` it is routed
  by chip id across the inter-die D2D fabric. `[HIGH geometry; MED·INFERRED "valid =
  route remotely" semantics]`
- **`[55..57]`** — transport attributes (reserved / PCIe relaxed-ordering / ok-to-fail);
  **not address geometry**.

In the **NEIGHBOR** view the same transaction, once it has been keyed by the remapper,
carries its egress encoding as the per-direction flags: **`EXIT_SENG`** (leave the local
Sequencer-Engine domain) → **`EXIT_DIE`** (cross the die boundary) → **`NEIGHBOR_ROUTE`**
(take the neighbor route) → **`PEB`** (toward the PEB subsystem). These name the **staged
fabric exits** that step a packet out of the local SEngine, off the die, onto the
neighbor route, toward the destination block. The `CAYMAN_ID[53:48]` + `CAYMAN_ID_VALID`
pair is the **lookup key**; the NEIGHBOR flags are the **post-remap egress encoding**.

> **NOTE — the address-remapper CAM.** The decode headers define only the address
> *envelope*; they do **not** contain the route table. The sibling address map exposes a
> programmable `user_remapper` register block (2 KiB, under the SPROT fabric) that a
> 64-die mesh uses to translate `{CAYMAN_ID, DIE, LOCAL}` into an egress route / physical
> target — the CAM/LUT that *consumes* the `CAYMAN_ID` field. The
> `EXIT_SENG`/`EXIT_DIE`/`NEIGHBOR_ROUTE`/`PEB` flags map onto the named addressable
> subsystems on the die (`io_d2d` = the die-to-die fabric; `peb`; the `seng`/SEngine
> domain). `[MED·INFERRED — co-location + naming, not a wiring trace]` The transport
> itself (DWC-PCIe + iATU on Cayman) is detailed in
> [PKL PCIe / D2D fabric](pkl-pcie-d2d-fabric.md).

### Worked cross-die decode (one concrete address)

Compose an address targeting **chip 42, die 1**, with the chip id valid and PCIe
relaxed-ordering set, at local byte offset `0x12345_6789ABC`:

```
SET_LOCAL(0,        0x123456789ABC)  -> 0x000000123456789ABC
SET_DIE(_,          1)               -> bit[47]    set
SET_CAYMAN_ID(_,    42 = 0b101010)   -> bits[53:48] = 0x2A
SET_CAYMAN_ID_VALID(_,1)             -> bit[54]    set
SET_PCIE_ATTR_RELAXED_ORDERING(_,1)  -> bit[56]    set
                          -----------------------------------
            composed addr = 0x016A923456789ABC
```

Round-trip decode:

| GET | value |
|---|---|
| `GET_LOCAL` | `0x123456789ABC` |
| `GET_DIE` | `1` |
| `GET_CAYMAN_ID` | `42` (correct) |
| `GET_CAYMAN_ID_VALID` | `1` |
| `GET_PCIE_ATTR_RELAXED_ORDERING` | `1` |
| `GET_OK_TO_FAIL` | `0` |

The same `0x016A923456789ABC`, read through the **NEIGHBOR** decoder, would surface
`ID_VALID=1` (`[54]`), `PCIE_ATTR_RELAXED_ORDERING=1` (`[56]`), and decode the
`[53:48]` bits as `{PEB=1, NEIGHBOR_ROUTE=0, EXIT_DIE=1, EXIT_SENG=0,
NEIGHBOR_RSVD=0b10}` — i.e. the *same bits* reinterpreted as egress flags rather than a
chip id. That dual interpretation of `[53:48]` is the whole point of shipping two
decoders.

---

## 6. The byte-identical second copy (host vs device build)

> **NOTE — two byte-identical copies ship.** Both `.h` files exist a **second time**,
> byte-for-byte identical, in the customop-lib arch-headers tree:
>
> | file | arch-regs copy SHA-256 | customop-lib copy | result |
> |---|---|---|---|
> | `cayman_addr_decode.h` | `f0b214b7…147bde9` | `f0b214b7…147bde9` | **IDENTICAL** |
> | `cayman_addr_decode_neighbor.h` | `2a0f3fed…f1589a0` | `2a0f3fed…f1589a0` | **IDENTICAL** |
>
> (`diff` reports identical; SHA-256 confirms.) The `.vh` (Verilog) forms exist **only**
> in the `cayman-arch-regs_tgz` copy — the customop-lib tree ships the C headers only.
> Two copies exist because the codec is needed at **two build sites**: the host-side
> custom-op toolchain (`custom_op/c10/include/arch-headers/cayman/`, C only) and the
> RTL/arch-regs generation output (`address_map/`, C + Verilog). Both copies being
> byte-identical corroborates this is the **shipped production layout**, not a one-off
> intermediate.

---

## 7. Reconciliation & confidence ledger

**.h / .vh / struct agreement.** For *both* decoders, the C macros (GET+SET), the
Verilog GET macros, and the Verilog packed struct **agree exactly** on every field's
position and width — 7 fields (LOCAL) and 11 fields (NEIGHBOR), derived by independent
shift/mask popcount and MSB-first struct width-sum unpack. Both occupy exactly
`[57:0]` (58 defined bits).

| Confidence | Claims |
|---|---|
| **HIGH·OBSERVED** | every bit range/width for both decoders; .h/.vh/struct agreement; byte-identical second-copy (SHA-256); 64-die sizing (6-bit `CAYMAN_ID`); 47-bit per-die `LOCAL`; 2-die `DIE` bit; lossless symmetric GET/SET codec; worked decode round-trip |
| **MED·INFERRED** | per-field *semantic* readings (`OK_TO_FAIL`, `EXIT_SENG`, `EXIT_DIE`, `NEIGHBOR_ROUTE`, `PEB`, the `*_VALID` "route remotely" meaning); mapping of the egress flags to the SEngine/PEB/`io_d2d` subsystems; `user_remapper`-as-CAM linkage |
| **LOW** | none asserted |

> **WALL — Cayman (NC-v3) only.** These two header pairs are the **Cayman** decoders;
> the layout above is byte-grounded for NC-v3. The Maverick (v5) SoC uses native UCIe
> die-to-die and its own address-map DB — any v5 field layout would be a *different*
> decoder and is **not** covered here. `[INFERRED for any v5 read]`

---

**See also:** [SoC master map](soc-master-map.md) ·
[unified SoC memory map](unified-soc-memory-map.md) ·
[PKL PCIe / D2D fabric](pkl-pcie-d2d-fabric.md) ·
[RDMA cross-die `routing_id` fold](../../dma/rdma-cross-die.md)
