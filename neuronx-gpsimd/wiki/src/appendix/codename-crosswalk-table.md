# The Codename ↔ NC-ver ↔ coretype ↔ arch_id Cross-Walk

This is the **exhaustive pinned lookup appendix** for every GPSIMD generation
identifier. Where the Part-0
[Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) is the
*narrative* (it explains the relations) and the Part-6
[Codename ↔ Generation Map](../generations/codename-generation-map.md) is the
*proof* (it disassembles the dispatch), **this page is the reference card**: one
row per generation, every column pinned, **every cell tagged with the binary
symbol / disassembly site / header line that establishes it**. A reimplementer
who needs "what is MARIANA_PLUS's NCFW selector byte and which lib establishes
it" reads exactly one row here.

Nothing here is asserted from a naming pattern. Every value was re-grounded
against the shipped binaries this pass (`nm` / `objdump` / `readelf` /
`strings` + the clean C arch-ISA headers); the four primary artifacts, with the
exact symbol or address that pins each column, are named in §0. The single most
important discipline on the page is the **MAVERICK identity wall** (§3): the
device `coretype 37` is OBSERVED, the NCFW `arch_id 36` is *doubly* INFERRED and
carries a `*` on every appearance.

**Confidence tags.** Every row and every per-cell footnote carries **HIGH / MED /
LOW** crossed with **OBSERVED** (read from bytes / disassembly / a shipped header
this session), **INFERRED** (deduced from a structural relation with no
contradicting evidence), or **CARRIED** (taken from a cross-referenced sibling
surface at its stated confidence). Escaped `\|` inside a table cell is a literal
pipe. `36*` is never to be presented as binary-observed.

---

## 0 · The four artifacts that pin this table

Every column below is grounded in one of four shipped artifacts. The paths are
absolute (the `extracted/` tree is gitignored, so `fd --no-ignore` / absolute
paths are mandatory); each artifact is named by the **one symbol or address** the
cross-walk reads from it. `[HIGH/OBSERVED]`

| tag | artifact | what it pins | the anchor read this pass |
|---|---|---|---|
| **INT** | `…/custom_op/c10/lib/libnrtucode_internal.so` (5-gen twin, not stripped) | `coretype` (the `*_libs` jump-table targets); the ct-37 resolver masks; the MAVERICK string split | `nm INT \| rg '_libs$'` → 5 symbols; `objdump` `cmp $0x25` + `movabs $0x2020202000` |
| **NCFW** | `…/aws/neuron/lib/libncfw.so` (management-core firmware host lib) | `arch_id` (the `get_image` selector immediates); the NCFW `v#` blob symbols; shipped ceiling | `objdump … <libncfw_get_image>` ladder `{0x1c,0x14,0x05,0x0c}`; 8 `v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` |
| **SO** | `…/custom_op/c10/lib/libnrtucode.so` (4-gen shipped front, stripped) | the shipped-vs-internal split (MAVERICK = 0); the runtime getter addresses | `strings SO \| rg -ci maverick` → 0; `nm -D SO` getter VAs |
| **INC** | `…/custom_op/c10/include` (arch-isa + arch-headers trees) | the enum ordinals (`nrtucode.h`); `NeuronCoreVersion`; the EXTISA-blob/ISA-dir identity; TONGA Product-ID | `nrtucode.h:14..56`; `neuron_*_arch_isa/`; `arch-headers/`; `spis_model.json:107` |

> **NOTE — section deltas are per-binary; do not over-generalize.** On **INT**,
> `readelf -SW` gives `.rodata` VMA `0x46b0` / file `0x46b0` (Δ 0 — the jump
> table @ `0x556c` and the `lea`-named getter blobs slice verbatim) but
> `.data.rel.ro` VMA `0x9b8cf0` / file `0x9b6cf0` (**Δ `0x2000`** — the five
> `*_libs` tables live here and need the `0x2000` subtraction for a raw-file
> slice). The **ncore2gp device-config DLLs** (`libisa-core` etc., a *different*
> binary family) carry **Δ `0x200000`** on `.data`/`.data.rel.ro` — confirm with
> `readelf -SW` per binary before any `.data` `xxd`. The `_ZTV + 0x10` vtable rule
> also does **not** apply to **INT**: it has **zero** C++ vtables; the `*_libs`
> entries are plain C `{SO_get, JSON_get}` 16-byte function-pointer pairs. `[HIGH/OBSERVED]`

---

## 1 · The full pinned cross-walk

There are **five unified-NeuronCore GPSIMD generations** (SUNDA…MAVERICK), a
single arithmetic family keyed on `coretype` / `arch_id`, plus **one pre-unified
outlier (TONGA)** that sits *outside* the family (§4). One row per generation;
every cell pinned to its artifact (§0).

| Codename | NC-ver | coretype `[INT]` | arch_id `[NCFW/INC]` | NCFW-selector \| v# `[NCFW]` | product `[CARRIED]` | PCI-ID `[CARRIED]` | DKMS-arch `[CARRIED]` | EXTISA-blob identity `[INT/INC]` | shipped-vs-internal `[SO/INT]` |
|---|---|---|---|---|---|---|---|---|---|
| **SUNDA** | NC-v2 | **6** `H/OBS` (`sunda_libs@0x9b8f80`) | **5** = `0x05` `H/OBS` (`nrtucode.h:19`; `get_image` `0x05`) | `0x05` \| v2 `H/OBS` | Trn1 / Trn1N + Inf2 / Inf2E `H/CARRIED` | `0x7164`, `0x7264` `H/CARRIED` | `NEURON_ARCH_V2` `H/CARRIED` | **1 lib** (`SUNDA_Q7_POOL_RELEASE_EXTISA_0`, weak-undef); **only gen with a real 17-entry JSON opcode manifest**; body from container | **SHIPPED** (all 4 libs; `.a` via weak-undef) `H/OBS` |
| **CAYMAN** | NC-v3 | **13** `H/OBS` (`cayman_libs@0x9b8f90`) | **12** = `0x0c` `H/OBS` (`nrtucode.h:27`; `get_image` `0x0c`) | `0x0c` \| v3 `H/OBS` | Trn2 (+AC/E/N/P/U/UAC) `H/CARRIED` | `0x7364` `H/CARRIED` | `NEURON_ARCH_V3` `H/CARRIED` | **4 libs** `EXTISA_0..3`; own compile (idx0 `0xa260`); 16 stub JSONs; XT-14.09/clang-10 | **SHIPPED** (all 4 libs) `H/OBS` |
| **MARIANA** | NC-v4 | **21** `H/OBS` (`mariana_libs@0x9b8fd0`) | **20** = `0x14` `H/OBS` (`nrtucode.h:36`; `get_image` `0x14`) | `0x14` \| v4 `H/OBS` | Trn3 / TRN3PDS98 (+SwitchV1) `H/CARRIED` | `0x7564` (GA) `M/CARRIED` | `NEURON_ARCH_V4` `H/CARRIED` | **4 libs**; distinct compile, same opcode contract; idx0 `9f2ce049…c751` | **SHIPPED** (all 4 libs) `H/OBS` |
| **MARIANA_PLUS** | NC-v4+ | **29** `H/OBS` (`mariana_plus_libs@0x9b9010`) | **28** = `0x1c` `H/OBS` (`nrtucode.h:45`; `get_image` `0x1c`) | `0x1c` \| v4+ `H/OBS` | Trn3-pre / TRN3PDS98 (SwitchV1 ultra-server) `H/CARRIED` | `0x7565` (pre) `M/CARRIED` | `NEURON_ARCH_V4` (shared) `H/CARRIED` | **4 libs**, **byte-identical to MARIANA** (idx0 `9f2ce049…c751`; adds **0** new EXTISA bytes) | **SHIPPED** (all 4 libs); a flag-delta on MARIANA's ISA `H/OBS` |
| **MAVERICK** | NC-v5 | **37** `H/OBS` (`maverick_libs@0x9b9050`) | **36\*** = `0x24` `M/INFERRED` (`coretype−1`; **no NCFW byte**) | **(none)** \| **(no v#)** `H/OBS-neg` | *(unshipped — no product binding in corpus)* `LOW/OPEN` | — | — | **4 libs** (idx0 `a92c8ba0…87f4`, sz `0x7fb0`; ET_DYN/PIC, stripped); exist **only** in INT `.rodata`; XT-15.05/clang-15 | **INTERNAL-ONLY** (INT only; **0** in SO) `H/OBS` |
| *TONGA* | *NC-v1 (outlier)* | *— (none)* `H/OBS-neg` | *— (Product-ID `0x01`)* `H/OBS` | *— \| (none)* `H/OBS-neg` | *Inf1* `H/CARRIED` | *—* | *(legacy V1)* `H/CARRIED` | *— (no EXTISA blob, no NCFW image)* `H/OBS-neg` | *legacy ISA headers only (`arch-isa/` family)* `H/OBS` |

**Column grounding, in one line each** (all re-run this pass):

- **coretype `{6,13,21,29,37}`** — the five `*_libs` symbol addresses in **INT**
  (`nm INT | rg '_libs$'`), keyed by the `coretype − 6` jump table at `.rodata`
  `0x556c`; independently re-confirmed by the two resolver bitmasks (§2.3). `[HIGH/OBSERVED]`
- **arch_id `{5,12,20,28}`** — *two* independent **structures agree**: the
  `<GEN>_NX_TOPSP` enum ordinals in `INC/nrtucode.h` (L19/27/36/45) **and** the
  `libncfw_get_image` selector immediates `{0x05,0x0c,0x14,0x1c}` in **NCFW**. `[HIGH/OBSERVED]`
- **arch_id `36*` (MAVERICK)** — **no firmware byte states 36**; it is
  `coretype − 1`, and the enum slot at 36 is a `__REMOVED__` placeholder (§3). `[MED/INFERRED]`
- **NCFW-selector / v#** — the `get_image` ladder `{0x1c,0x14,0x05,0x0c}` + the
  eight `v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` blob symbols in **NCFW**; no
  v5/maverick image. `[HIGH/OBSERVED]`
- **product / PCI-ID / DKMS-arch** — **CARRIED** from the *host platform* surfaces
  (the DKMS `NEURON_ARCH_V2/3/4` PCI-ID table, the platform compiler banners), not
  from any GPSIMD binary in this corpus; every host surface caps at MARIANA / Trn3 /
  `NEURON_ARCH_V4`. The MAVERICK product binding is genuinely **OPEN** — nothing
  here names a "Trn4"; do not fabricate one (§6). `[HIGH/OBSERVED on the host surface, CARRIED here]`
- **EXTISA-blob identity** — the per-gen `{SO_get, JSON_get}` thunk pairs in each
  `*_libs` table (**INT**) + the per-gen `neuron_<gen>_arch_isa` ISA dirs (**INC**); §7. `[HIGH/OBSERVED]`
- **shipped-vs-internal** — `strings SO | rg -ci maverick` → **0** vs **INT** →
  189 strings / 125 `nm` symbols (§5). `[HIGH/OBSERVED]`

> **NOTE — `0xH` vs decimal in this table.** `arch_id` is given both decimal and
> hex because both forms appear in the binaries: `nrtucode.h` carries the decimal
> enum ordinal; `libncfw_get_image` carries the hex `cmpl` immediate. They are the
> same byte: `5=0x05`, `12=0x0c`, `20=0x14`, `28=0x1c`, `36=0x24`. The MAVERICK
> `0x24` is the *only* one that no `cmpl` actually compares against.

---

## 2 · The `coretype = arch_id + 1` relation — verified per row

The defining invariant of the family. It is **not** an arithmetic coincidence and
**not** a stride read off either axis — it is a **join between two independently
observed firmware structures**: the `arch_id` is the `<GEN>_NX_TOPSP` enum ordinal
(and the matching NCFW `get_image` `cmpl` immediate), the `coretype` is the
`<GEN>_Q7_POOL` ordinal, and `Q7_POOL` is exactly one enum slot past `NX_TOPSP` in
every shipped block. The relation falls straight out of the enum layout.

### 2.1 · Row-by-row (the `−1` holds; the MAVERICK row is the exception to *watch*)

```
codename       arch_id (NX_TOPSP ord. ; NCFW sel)     coretype (Q7_POOL ord.)     ct − arch_id
-----------    ------------------------------------   ------------------------    ------------
SUNDA          5   (nrtucode.h:19 ; NCFW 0x05)         6   (nrtucode.h:20)          1     ✓
CAYMAN         12  (nrtucode.h:27 ; NCFW 0x0c)         13  (nrtucode.h:28)          1     ✓
MARIANA        20  (nrtucode.h:36 ; NCFW 0x14)         21  (nrtucode.h:37)          1     ✓
MARIANA_PLUS   28  (nrtucode.h:45 ; NCFW 0x1c)         29  (nrtucode.h:46)          1     ✓
MAVERICK       36* (INFERRED — NO byte, NO NCFW)       37  (nrtucode.h:56)          1*    ✓ (INFERRED)
```

For the four shipped rows the `arch_id` is **double-anchored** — once as the
`NX_TOPSP` ordinal (`INC/nrtucode.h`), once as the `cmpl` immediate
(`NCFW libncfw_get_image`) — and the two anchors are equal. `coretype − arch_id =
1` for all four. **`[HIGH/OBSERVED]`** The MAVERICK row satisfies the arithmetic
(`37 − 36 = 1`) but only because `36*` is *defined* as `coretype − 1`; it has no
independent anchor (§3).

> **GOTCHA — the stride is `+7, +8, +8, +8`, NOT a uniform `+8`.** The `coretype`
> set `{6,13,21,29,37}` steps **+7 (6→13)** then **+8, +8, +8**; `arch_id`
> `{5,12,20,28,36*}` likewise. The +7 first step is because the SUNDA enum block
> has a different member count than the later blocks (SUNDA starts at `NX_ACT = 0`;
> see §3). Do **not** read a "+8 stride" off either axis to derive anything — the
> *only* uniform relation is `coretype = arch_id + 1`, and it is that `−1` (never a
> stride) that extends the unshipped MAVERICK `arch_id` to 36. `[HIGH/OBSERVED]`

### 2.2 · The NCFW `get_image` ladder, byte-exact (the `arch_id` source)

`libncfw_get_image` (dynsym `T` @ `0x1179` in **NCFW**) maps an `arch_id` to a
`{iram, dram}` NCFW firmware image. The ladder, re-disassembled this pass, compares
**exactly** `{0x1c, 0x14, 0x05, 0x0c}` with a `ja` default:

```
1199:  83 7d fc 1c   cmpl $0x1c,-0x4(%rbp)   ; arch_id 0x1c (28) -> v4_plus  MARIANA_PLUS
11a7:  0f 87 …       ja   12ec               ; arch_id > 0x1c -> default (return 2) — covers 0x24/MAVERICK
11ad:  83 7d fc 14   cmpl $0x14,-0x4(%rbp)   ; arch_id 0x14 (20) -> v4        MARIANA
11c1:  83 7d fc 05   cmpl $0x5, -0x4(%rbp)   ; arch_id 0x05 (5)  -> v2        SUNDA
11c7:  83 7d fc 0c   cmpl $0xc, -0x4(%rbp)   ; arch_id 0x0c (12) -> v3        CAYMAN
```

There is **no** `cmpl $0x24` (36) and **no** `cmpl $0x25` (37) anywhere in the
ladder — a hypothetical MAVERICK `arch_id 0x24` falls through the `ja` arm to the
`return 2` default. The NCFW image → generation map is a **total function on the
four shipped generations and undefined for MAVERICK**. The eight blob symbols sit
at strictly increasing `.rodata` offsets in `v2 < v3 < v4 < v4_plus` order
(`v2_iram@0x6a140 … v4_plus_iram@0x8ccc0`); `nm NCFW | rg -i 'v5|maverick'` → **0
hits**. `[HIGH/OBSERVED — the absent `0x24` is OBSERVED-negative.]`

### 2.3 · The coretype-37 resolver masks (the `coretype` corroborators)

The `*_libs` jump table is not the only structure that fixes `{6,13,21,29,37}`. In
**INT**, `nrtucode_get_num_ext_isa_libs` (`0x9b2c90`) does
`cmp $0x25,%edi` (reject `coretype > 37`) then `movabs $0x2020202000` — a bitmask
with set bits at **exactly `{13,21,29,37}`** (SUNDA `ct6` handled by a separate
`cmp $0x6` leg). The sibling `opset_get_library_index` mask is `0x2020202040` →
`{6,13,21,29,37}` (bit 6 set directly). Both accept `coretype 37`; the shipped
front **SO** rejects it (24-entry table, `cmp $0x17` → `coretype ≤ 29`). Bit-decode
this pass: `0x2020202000 → {13,21,29,37}`, `0x2020202040 → {6,13,21,29,37}`. `[HIGH/OBSERVED]`

---

## 3 · The MAVERICK split — `ct37` OBSERVED, `arch36` *doubly* INFERRED (the WALL)

MAVERICK is the one row whose two key columns disagree on their grounding. Carry
this distinction exactly; it is the single most important caveat on the page.

**`coretype 37` is OBSERVED — three independent firmware reads:**

1. **The `*_libs` jump-table target.** `get_ext_isa` case `idx 31` (`coretype 37`)
   → `lea … 0x9b9050 <maverick_libs>` — read from the `.rodata` `0x556c` table
   bytes. **[HIGH/OBSERVED]**
2. **The enum ordinal.** `NRTUCODE_CORE_MAVERICK_Q7_POOL` is enum index **37**
   (`nrtucode.h:56`). **[HIGH/OBSERVED]**
3. **The two resolver bitmasks.** `0x2020202000` / `0x2020202040` both set bit 37,
   and `cmp $0x25` (37) is the accept-bound in the resolvers (§2.3). **[HIGH/OBSERVED]**

**`arch_id 36*` is INFERRED — and *doubly* so:**

- **No `arch_id` byte for MAVERICK exists.** The only `arch_id`-keyed firmware
  structures are the NCFW selectors (`get_image`, `ctx_log`); both compare *exactly*
  `{0x05,0x0c,0x14,0x1c}` with a `ja` default. There is **no `0x24` (36)** and **no
  `0x25` (37)** NCFW selector byte. **[HIGH/OBSERVED-negative]**
- **The `coretype − 1` slot is a `__REMOVED__` placeholder, not a core.** For
  SUNDA…MARIANA_PLUS, `arch_id` *is* the `<GEN>_NX_TOPSP` ordinal (5/12/20/28). For
  MAVERICK that pattern **breaks**: `NRTUCODE_CORE_MAVERICK_NX_TOPSP` is enum index
  **54** (`nrtucode.h:54`), *not* 36; the slot at index 36 is
  `NRTUCODE_CORE_MAVERICK_NX__REMOVED__` (`nrtucode.h:55`) — a removed/reserved
  placeholder. The MAVERICK block also **starts at `NX_DVE`** (`nrtucode.h:50`, no
  `NX_ACT` — the ACT→DVE fold), which is why its `NX_TOPSP` lands at 54 instead of
  the +8-stride 36. So `arch_id 36 = coretype(37) − 1` lands on a `__REMOVED__`
  name, **not** on a real `NX_TOPSP` core. It is inferred from the `−1` relation
  *and* it contradicts the very `NX_TOPSP = arch_id` structure that grounds the
  other four rows. **[MED/INFERRED]**

> **CORRECTION — `arch_id 36` is doubly inferred, never "the +1 of the coretype"
> alone.** A reading that treats MAVERICK `arch_id 36` as merely "INFERRED from
> `coretype = arch_id + 1`" understates it. The `NX_TOPSP = arch_id` correspondence
> that holds for SUNDA…MARIANA_PLUS *fails* for MAVERICK — the index-36 enum slot is
> a `__REMOVED__` placeholder (`nrtucode.h:55`), and the real `MAVERICK_NX_TOPSP` is
> at 54. Mark it **`36*`**, tag it **MED/INFERRED**, and never present it as
> binary-observed. (Confirmed this pass: `nrtucode.h:50,54,55,56`.) The Part-6
> [Codename ↔ Generation Map](../generations/codename-generation-map.md) §4 records
> the same wall.

**Why there is no NCFW image at all.** **NCFW** ships exactly eight firmware-blob
symbols `{v2,v3,v4,v4_plus} × {iram,dram}`; the `.rodata` blob region closes right
after `v4_plus`; zero `maverick`/`v5` string; four `.c` source strings only
(`sunda.c … mariana_plus.c`). MAVERICK is realised only in the newer (clang-15 /
XtensaTools-15.05, ET_DYN/PIC) **INT** twin's dispatch tables + its four
internal-only Q7 ELFs. The NCFW absence is a missing *orchestration layer*, not a
missing feature — MAVERICK still has on-engine collective capability (the Q7-POOL
`SB2SB_COLLECTIVE 0xBF` + the SP/sync engine) the same way CAYMAN…MARIANA_PLUS do.
See [The MAVERICK Profile](../generations/maverick-profile.md). `[HIGH/OBSERVED]`

> **GOTCHA — flag every MAVERICK / v5 *interior* as INFERRED.** Beyond the three
> OBSERVED `coretype 37` anchors and the OBSERVED ISA-header opcode/dtype values,
> **every v5 interior is HEADER/getter/dispatch-OBSERVED at most**: `arch_id 36*`;
> the v5 Q7 geometry / CSR programmer / run-stall / DKL; the v5 D2D transport IP;
> the per-opcode→handler-body bindings (FLIX-VLIW desync frontier). The
> [MAVERICK Profile](../generations/maverick-profile.md) §7 enumerates the six v5
> walls; do not state a v5 interior as fact on this card.

---

## 4 · TONGA — the pre-unified outlier (out of the family)

TONGA is a real, older codename, but it is **not** a sixth GPSIMD generation and is
**not** part of the `coretype = arch_id + 1` family. It is the legacy "L"-family
ISA-header predecessor — the historical **NC-v1 / Inf1-era** part from which the
modern `neuron_*_arch_isa` headers descend — with **zero** GPSIMD runtime identity.
It occupies the *chronology* row of §1 for completeness and nothing more.

- **Byte-grounded "older than SUNDA":** the SPIS Product-ID register description
  reads `"Product ID. (Tonga - 0x01, Sunda - 0x02, Cayman - 0x03, Mariana - 0x4)"`
  (`INC/arch-headers/mariana/csrs/spis/spis_model.json:107`, read this pass). TONGA
  = Product-ID `0x01`, one generation *before* SUNDA. **[HIGH/OBSERVED]**
- **No runtime identity:** no `coretype`, no `arch_id`, no `NRTUCODE_CORE_TONGA`, no
  `tonga_libs`, no NCFW image, no EXTISA blob; absent from every `get_image` /
  `get_ext_isa` / `get_num_ext_isa_libs` selector. `strings INT | rg -i tonga` → 0.
  **[HIGH/OBSERVED]**
- **Distinct, older dtype family:** `TONGA_ISA_TPB_DTYPE_*` (8 codes) — a strict
  subset of the 16-code `NEURON_ISA_TPB_DTYPE_*` the five GPSIMD gens use. A
  different enum *name family* means a different, older ISA. **[HIGH/OBSERVED]**
- **On-disk shape:** `INC/arch-headers/` has **six** dirs (the five codenames +
  `tonga`, register maps only); `INC/neuron_*_arch_isa/` has only **four** ISA dirs
  `{sunda, cayman, mariana, maverick}` (confirmed this pass — no `mariana_plus`, no
  `tonga`). TONGA appears only in the legacy `arch-isa/` tree, never as a
  `neuron_tonga_arch_isa` GPSIMD ISA. **[HIGH/OBSERVED]**

Anyone who finds `tonga` in an ABI header must treat it as the legacy "L" / TONGA
ISA, never as a sixth coretype. The full ordering, oldest → newest:

```
TONGA(NC-v1, Inf1, ProdID 0x01)  ⊳  SUNDA(v2)  ⊳  CAYMAN(v3)  ⊳  MARIANA(v4)  ⊳  MARIANA_PLUS(v4+)  ⊳  MAVERICK(v5)
└──── outside the unified family ────┘    └──────── coretype = arch_id + 1 (no NCFW v5 for MAVERICK) ────────┘
```

See [Cross-Generation Opcode-Table Diff + TONGA](../generations/cross-gen-opcode-diff.md)
for the V1 ISA deep-dive (the engine-scoped `TONGA_ISA_TPB_OPCODE`, the 44-entry
roster, the V1→V2 byte inheritance).

---

## 5 · Shipped-vs-internal — the 4/5 split

"Is GPSIMD four generations or five?" is a binary-version question, not an
inconsistency. The shipped runtime path tops out at MARIANA_PLUS; MAVERICK lives
only in the non-shipped symbol twin. One row per artifact:

| artifact | SUNDA | CAYMAN | MARIANA | M_PLUS | MAVERICK | bound that establishes it |
|---|---|---|---|---|---|---|
| **NCFW** `libncfw.so` (images) | v2 | v3 | v4 | v4+ | — | `arch_id ≤ 0x1c` (28); `ja` default; 8 blob symbols, no v5 `H/OBS` |
| **SO** `libnrtucode.so` (front getter) | ✓ | ✓ | ✓ | ✓ | — (stub) | 24-entry table `cmp $0x17` → `coretype ≤ 29`; `strings \| rg -ci maverick` = 0 `H/OBS` |
| `libnrtucode_extisa.so` (blob container)³ | ✓ | ✓ | ✓ | ✓ | — | container; `strings \| rg -ci maverick` = 0 `H/CARRIED` |
| `libnrtucode.a` (static) | —¹ | ✓ | ✓ | ✓ | — | 435 members = 48+124+124+124+0+15; `ar t \| rg -ic maverick` = 0 `H/OBS` |
| **INT** `libnrtucode_internal.so` (twin) | ✓² | ✓ | ✓ | ✓ | **✓** | `cmp $0x25` → `coretype ≤ 37`; 5 `*_libs` symbols incl `maverick_libs@0x9b9050` `H/OBS` |

¹ SUNDA `.a`/getter contents resolved from the container as a weak-undef
(`R_X86_64_64` to `SUNDA_…_EXTISA_0_{SO,JSON}_get`). ² **INT** also carries the
SUNDA weak-undef plus the four MAVERICK-only Q7 ELFs. ³ The **flat
`libnrtucode_extisa.so`** is **NOT a standalone file in the gpsimd checkout** — under
`neuronx-gpsimd/` the EXTISA content is the embedded blob set *inside* **INT** (66
`*_EXTISA_*_SO_get` accessors); the flat container ships in the sibling
`neuronx-runtime` corpus, so its row is `CARRIED` across the package boundary (see
[bibliography §1 CORRECTION](bibliography-source-binaries.md)).

**The split is sharp in the tallies** (re-counted this pass): a `maverick` literal
appears **189 times** via `strings INT | rg -oi maverick | wc -l` and **125 times**
as symtab symbols via `nm INT | rg -ci maverick`, vs **0** in the shipped front
**SO** (`strings SO | rg -oi maverick | wc -l` → 0). Whether a field deployment
links the 4-gen front or a 5-gen internal build is out of scope of these binaries.
`[HIGH/OBSERVED; the "internal twin = newer 5-gen build, front = older 4-gen
shipped" reading is MED/INFERRED.]`

> **CORRECTION — `189` strings / `125` symbols, not "187".** An earlier survey
> recorded the MAVERICK string tally as "internal = 187". The session re-count is
> **189** strings-occurrences (`strings INT | rg -oi maverick | wc -l`) and **125**
> symtab-symbols (`nm INT | rg -ci maverick`); the `187` was a `rg -ac` raw-byte
> count that under-counts vs `strings`-tokenised occurrences. Use **189
> strings-occurrences / 125 symtab-symbols / 0 in SO**.

---

## 6 · The "v5" label is overloaded — disambiguate

Two distinct "v5"-shaped tokens live in this corpus, on **different axes at
different layers**. Conflating them is the most common cross-reference error.

| token | axis | what it actually is | coretype / arch_id |
|---|---|---|---|
| **`CoreV5` / `core_v5`** | compiler ArchLevel (host platform / `libwalrus`) | the **Trn3-PRE / MARIANA_PLUS** variant slot — a Trn3-family *pre-release*, shipped-tooling-visible | `coretype 29` / `arch_id 0x1c` (an **NCFW-present** gen) |
| **`NC-v5` / GPSIMD `coretype 37`** | GPSIMD coretype / NC-banner axis | the genuine 5th **silicon** — **MAVERICK** | `coretype 37` / `arch_id 36*` (**no** NCFW image) |

The platform's index crosswalk places `CoreV5` *under* Mariana/Trn3 as the
"trn3pre variant" — **not** a fifth silicon row. So the host compiler reaching
"CoreV5" does **not** mean it reaches MAVERICK; it means it enumerates a Trn3-pre
ArchLevel that maps to MARIANA_PLUS (`coretype 29 / arch_id 0x1c`, NCFW-present).
The GPSIMD `coretype-37` anchors (§3: the `maverick_libs` jump-table target + the
`nrtucode.h:56` ordinal + the resolver bytes) are on the GPSIMD axis and are
independent of the compiler ArchLevel axis. `[HIGH/OBSERVED for both axes; the
"`CoreV5` = MARIANA_PLUS-region, not MAVERICK" reading is MED/INFERRED-STRONG,
anchored on the explicit "trn3pre variant" label.]`

> **NOTE — a third "v5"-ish token: `NEURON_CORE_VERSION_V5 = 5`.** The arch-ISA
> header carries `NEURON_ISA_TPB_NEURON_CORE_VERSION_V5 = 5`
> (`INC/neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h:136`, confirmed this
> pass; ABSENT from the mariana header, which caps at `V4 = 4` `:134`). This
> `NeuronCoreVersion` token is the **codegen-target ISA version** axis — distinct
> again from both `CoreV5` (the compiler ArchLevel, = MARIANA_PLUS-region) and
> `coretype 37` (the device dispatch key). Three parallel enumerations; none derives
> the other. `NeuronCoreVersion::V5` and `coretype 37` are both OBSERVED for
> MAVERICK; only the NCFW `arch_id` byte is not. `[HIGH/OBSERVED]`

The product / PCI-ID / DKMS-arch columns in §1 are likewise on a *host* surface,
not in the GPSIMD firmware: the DKMS `NEURON_ARCH_V2/3/4` PCI-ID table
(`0x7164 … 0x7565`), the platform compiler banners (`Sunda → "Trainium1"`,
`Cayman → "Trainium2"`, `Mariana → "Gen4"/Trn3`), and the NKI `nc_version`
gen-config (`gen2/gen3/gen4`) — **every one of those host surfaces caps at MARIANA
/ Trn3 / `NEURON_ARCH_V4`**. The numeric `coretype → silicon-part` binding is **not
inside any GPSIMD binary in this corpus**; the product column is carried, not
firmware-grounded, and the MAVERICK product family is genuinely **OPEN** (no "Trn4"
is named anywhere — do not fabricate one).

---

## 7 · Per-generation EXTISA-blob identity + shipped-vs-internal

The substantive per-generation device descriptor is the compiled Xtensa Q7 EXTISA
`*_SO` image (its `kernel_info_table` *is* the opcode→function table); the
accompanying `*_JSON` blobs are 32-byte `{"dummy_message": "hello world"}` stubs in
every generation except SUNDA. The five per-gen `*_libs` tables are `nm`-confirmed
in **INT** at strictly increasing addresses, indexed by the `coretype − 6` jump
table (§2.3).

| gen | EXTISA libs | idx0 blob identity (vs siblings) | toolchain | status |
|---|---|---|---|---|
| **SUNDA** | 1 (`Q7_POOL_RELEASE_EXTISA_0`, weak-undef) | the **only** gen with a real 17-entry JSON opcode manifest; body resolved from the container | XT-14.09 / clang-10 | SHIPPED |
| **CAYMAN** | 4 (`EXTISA_0..3`) | own compile; idx0 sz `0xa260` (`@0x2ef7e0`); 16 stub JSONs | XT-14.09 / clang-10 | SHIPPED |
| **MARIANA** | 4 | distinct compile of the CAYMAN opcode contract; idx0 `9f2ce049…c751` | XT-14.09 / clang-10 | SHIPPED |
| **MARIANA_PLUS** | 4 | **byte-identical to MARIANA** for all four libs (idx0 `9f2ce049…c751`); adds **0** new EXTISA bytes; **no** `neuron_mariana_plus_arch_isa` dir (shares MARIANA's ISA) | XT-14.09 / clang-10 | SHIPPED |
| **MAVERICK** | 4 (**INT** only) | entirely separate build — idx0 `a92c8ba0…87f4` sz `0x7fb0` (`@0x994de0`); DYN/PIC, fully stripped | **XT-15.05 / clang-15** | **INTERNAL-ONLY** |

The `*_libs` tables: `sunda_libs@0x9b8f80 < cayman_libs@0x9b8f90 <
mariana_libs@0x9b8fd0 < mariana_plus_libs@0x9b9010 < maverick_libs@0x9b9050`
(`nm INT`, confirmed this pass). The opcode *contract* (`lib0 = 17 / lib1 = 1 /
lib2 = 2 / lib3 = 9` entries) is identical across all four shipped generations —
only the compiled machine code differs. `[HIGH/OBSERVED]`

> **QUIRK — MARIANA ≡ MARIANA_PLUS at the device-image level.** Their EXTISA blobs
> are byte-identical (idx0 `9f2ce049…c751` both), their NCFW DRAM is byte-identical,
> and MARIANA_PLUS has **no** `neuron_mariana_plus_arch_isa` dir — it shares
> MARIANA's ISA. MARIANA_PLUS is a distinct codename at the NCFW / getter / coretype
> level (its own `arch_id 0x1c`, its own `ctx_log`, its own NCFW IRAM, its own
> `mariana_plus_libs@0x9b9010`, its own `arch-headers` dir) but a *code /
> feature-flag delta* on the MARIANA silicon ISA — selected at runtime via
> `NEURON_RT_DBG_V4_PLUS=0/1` (the env that replaced the removed
> `NRTUCODE_MPLUS_ON_MARIANA` flag, whose "flag has been removed" tripwire string is
> still present in the shipped front **SO**). `[HIGH/OBSERVED]`

---

## 8 · Confidence rollup

**HIGH / OBSERVED** (binary/header-exact, re-verified this pass): the five
codenames; `coretype {6,13,21,29,37}` (the five `*_libs` symbols + two resolver
bitmasks); `arch_id {5,12,20,28}` (`NX_TOPSP` ordinals + NCFW `get_image`
immediates); `coretype = arch_id + 1` (the enum-layout consequence, four rows); the
NCFW selector set `{0x05,0x0c,0x14,0x1c}` with no `0x24`/`0x25`; the eight NCFW
blob symbols (no v5); the per-gen EXTISA-blob identities with MARIANA ≡
MARIANA_PLUS byte-identity; the 4-shipped/5-internal split (189 strings / 125
symbols in **INT** vs 0 in **SO**; 435 `.a` members with 0 MAVERICK);
`NeuronCoreVersion::V5 = 5` (maverick header) vs `V4` cap (mariana); TONGA =
Product-ID `0x01` with no runtime identity.

**MED / INFERRED:** MAVERICK `arch_id 36*` (the `coretype − 1` extrapolation
landing on a `__REMOVED__` slot — §3); the `CoreV5 = MARIANA_PLUS-region, not
MAVERICK` reading; "internal twin = newer 5-gen build, front = older 4-gen
shipped"; the MARIANA `0x7564` and MARIANA_PLUS `0x7565` PCI-IDs (host-table
carried).

**LOW / OPEN (not claimed):** the definitive `coretype → silicon-part`
(Trn2/Trn3/Inf2/"Trn4"…) binding is **not in this corpus**; the MAVERICK
product / PCI-ID / DKMS-arch cells are deliberately blank, and the MAVERICK product
family is genuinely open — **no "Trn4" is named anywhere; do not fabricate one.**

---

## See also

- [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) — the
  Part-0 narrative companion to this card (the relation explained, the `CoreV5` vs
  `NC-v5` overload, the carried product/PCI-ID columns).
- [Codename ↔ Generation Map](../generations/codename-generation-map.md) — the
  Part-6 proof: the 32-case `get_ext_isa` dispatch disassembled, the enum-ordinal
  anchors, the contradiction ledger.
- [Master Per-Generation Capability Matrix](../generations/master-capability-matrix.md)
  — the full 15-subsystem × 5-generation grid (what each row of this card *does*).
- [The MAVERICK (v5) Profile](../generations/maverick-profile.md) — the v5 row's
  deep-dive (the `ct37`/`arch36` wall, the ACT→DVE fold, the six v5 walls).
- [Version + Ext-ISA Getters](../runtime/version-extisa-getters.md) — the runtime
  getters that establish `coretype`/`arch_id` at dispatch (the `*_libs` jump table,
  the resolver bitmasks, the front-lib `cmp $0x17` 4-gen bound).
- [Cross-Generation Opcode-Table Diff + TONGA](../generations/cross-gen-opcode-diff.md)
  — the TONGA V1 ISA deep-dive and the v2..v5 opcode-namespace evolution.
- [The Full Do-Not-Repeat / Correction Ledger](do-not-repeat-full-ledger.md) — the
  consolidated ledger of crossed-label / mis-read traps (the NCFW v3/v4 label
  crossing, the `arch_id 36` understatement, the 187→189 re-count) this card is
  pinned against.
