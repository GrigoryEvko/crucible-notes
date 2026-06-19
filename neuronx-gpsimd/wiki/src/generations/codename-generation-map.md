# Codename ↔ Generation Map

This is the Part-6 deep companion to the Part-0
[Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md). The
cross-walk is the *quick-reference join table* — the one place that says "SUNDA
means coretype 6, arch_id 5, NC-v2". This page is the *proof and the
machinery*: it disassembles the one host function that turns a `coretype`
integer into a per-generation device image, enumerates its entire 32-entry jump
table, anchors every `(arch_id, coretype)` pair to the exact enum line that
emits it, carves the per-generation EXTISA blob `sha256`s, and resolves the two
identity walls — the MAVERICK `arch_id 36` fiction and the TONGA outlier — with
a contradiction ledger that supersedes the backing survey where the bytes
disagree with it.

Everything here is read from the shipped binaries and headers of the
`aws-neuronx-gpsimd-customop-lib_0.21.2.0` and
`aws-neuronx-runtime-lib_2.31.24.0` packages. No claim rests on a stride read
off a naming pattern unless it is tagged **INFERRED**; the spine of the page is
a jump table whose bounding `cmp` and whose five live arms are quoted
byte-exact.

**Confidence tags.** Every cell carries **HIGH / MED / LOW** crossed with
**OBSERVED** (read from bytes / disassembly / a shipped header this session),
**INFERRED** (deduced from structure with no contradicting evidence), or
**CARRIED** (taken from a cross-referenced sibling surface at its stated
confidence). The single most important caveat on the page is the starred `36*`
on the MAVERICK `arch_id` — it is never to be presented as binary-observed, and
§4 shows it is *doubly* inferred.

The binaries this page quotes (paths abbreviated; all under the two packages
above):

```
INT  = …/custom_op/c10/lib/libnrtucode_internal.so   (5-gen symbol twin, NOT stripped)
       sha256 b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b
SO   = …/custom_op/c10/lib/libnrtucode.so            (4-gen shipped front, stripped)
       sha256 06d3f0b1630e38828ace79d3c9f3123ac14b3ea4c5cde4aa906a31b3e82ccce5
A    = …/custom_op/c10/lib/libnrtucode.a             (static archive, 435 members)
EXT  = …/aws/neuron/lib/libnrtucode_extisa.so        (runtime EXTISA blob container)
       sha256 dc00763dbdb27cb49d90cad55da676b9e01c5c0dfa3151919bc4e8ea1c39159f
NCFW = …/aws/neuron/lib/libncfw.so                   (NCFW management-core firmware host lib)
       sha256 598920d743762c03b3007c089829c02d0095408bf431fa3533e508c5f0aa3e49
       BuildID a98f8e1ca2294582835310c3a1092e0a5e500db5
INC  = …/custom_op/c10/include                       (arch-isa + arch-headers trees)
```

> **NOTE — VMA == file offset only in `.text`/`.rodata`.** All the addresses
> below are in `INT`'s `.text` (the dispatch) and `.rodata` (the jump table
> @ `0x556c`, the `*_libs` tables @ `0x9b8f80+`), where the ELF maps VMA 1:1 to
> file offset, so `objdump`/`xxd` offsets are the addresses quoted. The
> per-generation `*_libs` *tables* live in `.data.rel.ro`; their carve targets
> (the EXTISA blob `.data` payloads) were resolved through the `*_get` thunks,
> not by raw `.data` xxd, precisely because the `.data` VMA↔file delta is
> per-binary. `extracted/` and `ida/` are gitignored — every path above is
> absolute / `--no-ignore`.

---

## 1 · The master generation table

Five unified-NeuronCore GPSIMD generations form a single arithmetic family keyed
on a `coretype` and an `arch_id`. A sixth name, **TONGA**, sits *outside* that
family (§7). One row per generation, every cell tagged:

| Codename | NC-ver | coretype | arch_id | NCFW v# / sel-byte | NCFW selector | silicon (INFERRED) | shipped? | EXTISA blob `sha256` (idx0) |
|---|---|---|---|---|---|---|---|---|
| **SUNDA** | NC-v2 | **6** | **5** (0x05) | v2 / `0x05` | `get_image` case `0x05` → `v2_ncfw_*` | gen-2 (trn1/inf2-era) | YES (all 4 libs) | `444497066f5e1738…84c0` |
| **CAYMAN** | NC-v3 | **13** | **12** (0x0c) | v3 / `0x0c` | `get_image` case `0x0c` → `v3_ncfw_*` | Trainium2-class (gen-3) | YES (all 4 libs) | `910d41c3ededce67…5527` |
| **MARIANA** | NC-v4 | **21** | **20** (0x14) | v4 / `0x14` | `get_image` case `0x14` → `v4_ncfw_*` | gen-4 | YES (all 4 libs) | `9f2ce049608c0a88…c751` |
| **MARIANA_PLUS** | NC-v4+ | **29** | **28** (0x1c) | v4+ / `0x1c` | `get_image` case `0x1c` → `v4_plus_ncfw_*` | gen-4+ (MARIANA refresh) | YES (all 4 libs) | `9f2ce049608c0a88…c751` *(≡ MARIANA)* |
| **MAVERICK** | NC-v5 | **37** | **36\*** `[INFERRED]` | — (no NCFW image) | *(none — `arch_id 0x24` hits `ja` default)* | gen-5 (distinct SoC) | **NO — internal twin only** | `a92c8ba0e9dfb2d8…87f4` |
| *TONGA* | *NC-v1 (outlier)* | *— (none)* | *— (Product ID `0x01`)* | *— (none)* | *— (absent from every selector)* | *Inf1 (legacy V1)* | *legacy ISA headers only* | *— (no EXTISA blob)* |

**Per-cell / per-column grounding** (all re-run this session):

- **coretype `{6,13,21,29,37}`** — the live set of the 32-case `get_ext_isa`
  jump table (§3), independently re-confirmed by two resolver bitmasks. The
  five values are also the `<GEN>_Q7_POOL` enum ordinals in `nrtucode.h` (§2).
  **[HIGH / OBSERVED]**
- **arch_id `{5,12,20,28}`** — the `<GEN>_NX_TOPSP` enum ordinals
  (`nrtucode.h` L19/27/36/45) *and* the `libncfw_get_image` selector immediates
  `{0x05,0x0c,0x14,0x1c}` (§5). Two independent firmware structures agree.
  **[HIGH / OBSERVED]**
- **arch_id `36*` (MAVERICK)** — *no firmware byte states 36.* It is
  `coretype − 1 = 37 − 1`, and the enum slot it would occupy
  (`nrtucode.h` L55) is `NRTUCODE_CORE_MAVERICK_NX__REMOVED__`, a placeholder —
  **not** a `NX_TOPSP` core. Doubly inferred; see §4. **[MED / INFERRED]**
- **NCFW v# / sel-byte / selector** — `libncfw_get_image` @ `0x1179` in `NCFW`
  (§5); the four `cmpl` immediates and the eight `v{2,3,4,4_plus}_ncfw_{iram,
  dram}_bin` blob symbols. No v5/maverick image exists. **[HIGH / OBSERVED]**
- **silicon column** — **INFERRED**, *not* a proven part-name map. The
  numeric `coretype → silicon-part` binding is not inside any GPSIMD binary in
  this corpus; only "Trainium2-class" for CAYMAN is an OBSERVED part-class hint
  (report banners). Do not fabricate "Trn4". See the cross-walk §6 for the
  carried product/PCI-ID columns. **[LOW / INFERRED]**
- **shipped?** — the 4-shipped / 5-internal split (§6): `libnrtucode.a` has
  **0** MAVERICK members; `NRTUCODE_CORE_*` in `SO` names four generations, in
  `INT` names five. **[HIGH / OBSERVED]**
- **EXTISA blob `sha256`** — idx0 representative, carved this session via the
  `*_SO_get` thunks (§5 of the cross-walk gives the per-index table). MARIANA
  and MARIANA_PLUS idx0 are **byte-identical** (`9f2ce049…`), confirmed three
  ways (§6). Cross-reference
  [EXTISA Image Inventory](../images/extisa-inventory.md) and
  [Image Catalog Index](../images/image-catalog-index.md). **[HIGH / OBSERVED]**

---

## 2 · The `(arch_id, coretype)` pairs are enum ordinals — proved row by row

The most common mistake is to imagine `arch_id` and `coretype` are two unrelated
fields whose `+1` relation is a coincidence. They are not. **Both are positions
in one C enum** — `nrtucode_coretype_t` in
`INC/nrtucode.h` — and the relation falls straight out of the declaration order.

The enum is dense and positional: only the first enumerator carries `= 0`
(`nrtucode.h:14`), so every subsequent value is its line's ordinal. Each
generation declares a fixed block of NeuronCore members; the two that matter are
`<GEN>_NX_TOPSP` (the `arch_id`) and `<GEN>_Q7_POOL` (the `coretype`):

```c
/* INC/nrtucode.h — nrtucode_coretype_t, dense/positional from = 0 at line 14 */
NRTUCODE_CORE_SUNDA_NX_ACT          = 0,   /* L14 */
NRTUCODE_CORE_SUNDA_NX_DVE,                /* L15 */
/* … NX_POOL, NX_PE, NX_SP … */
NRTUCODE_CORE_SUNDA_NX_TOPSP,              /* L19  -> 5   = SUNDA arch_id  */
NRTUCODE_CORE_SUNDA_Q7_POOL,               /* L20  -> 6   = SUNDA coretype */

NRTUCODE_CORE_CAYMAN_NX_ACT,               /* L22 */
/* … */
NRTUCODE_CORE_CAYMAN_NX_TOPSP,             /* L27  -> 12  = CAYMAN arch_id  */
NRTUCODE_CORE_CAYMAN_Q7_POOL,              /* L28  -> 13  = CAYMAN coretype */

NRTUCODE_CORE_MARIANA_NX_TOPSP,            /* L36  -> 20  = MARIANA arch_id  */
NRTUCODE_CORE_MARIANA_Q7_POOL,             /* L37  -> 21  = MARIANA coretype */

NRTUCODE_CORE_MARIANA_PLUS_NX_TOPSP,       /* L45  -> 28  = MARIANA_PLUS arch_id  */
NRTUCODE_CORE_MARIANA_PLUS_Q7_POOL,        /* L46  -> 29  = MARIANA_PLUS coretype */

#if defined(NRTUCODE_INTERNAL_NAMES)       /* L49 — the whole MAVERICK block is gated */
NRTUCODE_CORE_MAVERICK_NX_DVE,             /* L50  — note: starts at NX_DVE, no NX_ACT  */
/* … */
NRTUCODE_CORE_MAVERICK_NX_TOPSP,           /* L54  -> 54  (NOT 36!) */
NRTUCODE_CORE_MAVERICK_NX__REMOVED__,      /* L55  -> 36? NO — see §4; this is index 36's name */
NRTUCODE_CORE_MAVERICK_Q7_POOL,            /* L56  -> 37  = MAVERICK coretype (OBSERVED) */
#endif
```

The arithmetic is a *join between two independently observed firmware
structures* — never an assumption:

```
codename       arch_id (NX_TOPSP ord. + NCFW sel-byte)   coretype (Q7_POOL ord.)   ct − arch_id
-----------    ----------------------------------------  -----------------------   ------------
SUNDA          5   (nrtucode.h:19 ; NCFW 0x05)           6   (nrtucode.h:20)        1
CAYMAN         12  (nrtucode.h:27 ; NCFW 0x0c)           13  (nrtucode.h:28)        1
MARIANA        20  (nrtucode.h:36 ; NCFW 0x14)           21  (nrtucode.h:37)        1
MARIANA_PLUS   28  (nrtucode.h:45 ; NCFW 0x1c)           29  (nrtucode.h:46)        1
MAVERICK       36* (INFERRED — NO byte, NO NCFW)         37  (nrtucode.h:56)        1*  (INFERRED)
```

> **The `coretype = arch_id + 1` invariant, proved.** For all four shipped
> generations the `arch_id` is anchored *twice* — once as the `NX_TOPSP` enum
> ordinal in `nrtucode.h`, once as the `cmpl` immediate in
> `libncfw_get_image` — and the two anchors are equal. The `coretype` is the
> `Q7_POOL` ordinal, which is exactly one slot past `NX_TOPSP` in every block
> (`NX_TOPSP` then `Q7_POOL`, adjacent lines L19/L20, L27/L28, L36/L37,
> L45/L46). Therefore `coretype = arch_id + 1` is a *structural consequence of
> the enum layout*, not a numeric accident. **[HIGH / OBSERVED]**

> **GOTCHA — the stride is +7 then +8, not a uniform +8.** The `coretype` set
> `{6, 13, 21, 29, 37}` steps **+7 (6→13)** then **+8, +8, +8**; `arch_id`
> `{5, 12, 20, 28, 36}` likewise. The +7 first step is because the SUNDA block
> has a different member count than the later blocks. Do **not** read a "+8
> stride" off the `coretype` axis to derive anything — the *only* uniform
> relation is `arch_id = coretype − 1`, and it is that `−1` (not a stride) that
> extends the unshipped MAVERICK `arch_id` to 36. The cross-walk's CORRECTION
> banner says the same.

---

## 3 · The `get_ext_isa` 32-case dispatch — the spine of the page

The one host function that converts a runtime `coretype` into a per-generation
EXTISA image is `nrtucode_get_ext_isa`. It is the authoritative gen-set proof:
its jump table has exactly 32 entries and exactly five of them are live, and
those five indices are exactly the five generations.

### 3.1 · The symbols

| symbol | type | addr (`INT`) | role |
|---|---|---|---|
| `nrtucode_get_ext_isa` | `T` (exported) | `0x9b2c80` | public wrapper: `xor %ecx,%ecx` ; `jmp 0x9b2b30` — calls the internal switch with `isa_index = 0` |
| `nrtucode_get_ext_isa_internal` | `t` (local) | `0x9b2b30` | the actual 32-case switch |
| `nrtucode_get_num_ext_isa_libs` | `T` (exported) | `0x9b2c90` | independent corroborator — encodes the live-set as a bitmask |
| `nrtucode_opset_get_library_index` | `T` (exported) | `0x9b1950` | second corroborator — `0x2020202040` bitmask (bit 6 set) |
| `nrtucode_ll_get_libraries_from_opcodes` | `T` (exported) | `0x9b1880` | third corroborator — `0x2020202000` bitmask |
| `nrtucode_core_get_coretype` | `T` (exported) | `0x9b0a10` | produces the `coretype` argument consumed here |

There is no symbol literally named `get_ext_isa`; the function is
`nrtucode_get_ext_isa`, plain C (no mangling), and the switch lives in its local
`_internal` variant.

### 3.2 · The switch-bounding instructions (byte-exact)

The switch variable is the `coretype`, in `edi`, saved to `r15d` at `0x9b2b3e`.
After an orthogonal `NEURON_UCODE_FLAVOR` env block (see the QUIRK below), the
bound is computed and applied:

```
0x9b2bdb:  41 83 c7 fa          add  $0xfffffffa,%r15d     ; r15d = coretype - 6
0x9b2bdf:  41 83 ff 1f          cmp  $0x1f,%r15d           ; compare against 31
0x9b2be3:  0f 87 81 00 00 00    ja   0x9b2c6a              ; if (coretype-6) > 31u -> default
0x9b2be9:  48 8d 0d 7c 29 65 ff lea  -0x9ad684(%rip),%rcx  ; %rcx = jump-table base VMA 0x556c
0x9b2bf0:  4a 63 14 b9          movslq (%rcx,%r15,4),%rdx  ; load int32 rel-offset, index = ct-6, scale 4
0x9b2bf4:  48 01 ca             add  %rcx,%rdx             ; base-relative
0x9b2bf7:  ff e2                jmp  *%rdx                 ; indirect dispatch
```

`cmp $0x1f` (31) on `coretype − 6` bounds the switch to `coretype ∈ [6 .. 37]`
— **exactly 32 cases**. It is a true base-relative jump table (`.rodata` @
`0x556c`, `int32` offsets, scale 4, index `coretype − 6`), not a compare chain.

### 3.3 · The 32-case enumeration (index = `coretype − 6`)

| idx | coretype | target VMA | resolves to | live? |
|---|---|---|---|---|
| 0 | **6** | `0x9b2bf9` → `lea … 0x9b8f80 <sunda_libs>` | **SUNDA** | ● |
| 1–6 | 7–12 | `0x9b2c6a` | default (ret `eax=1`) | |
| 7 | **13** | `0x9b2c13` → `lea … 0x9b8f90 <cayman_libs>` | **CAYMAN** | ● |
| 8–14 | 14–20 | `0x9b2c6a` | default | |
| 15 | **21** | `0x9b2c06` → `lea … 0x9b8fd0 <mariana_libs>` | **MARIANA** | ● |
| 16–22 | 22–28 | `0x9b2c6a` | default | |
| 23 | **29** | `0x9b2c20` → `lea … 0x9b9010 <mariana_plus_libs>` | **MARIANA_PLUS** | ● |
| 24–30 | 30–36 | `0x9b2c6a` | default | |
| 31 | **37** | `0x9b2c2d` → `lea … 0x9b9050 <maverick_libs>` | **MAVERICK** | ● |

The live-set is **exactly `{6, 13, 21, 29, 37}`** — the 27 other cases all jump
to the shared default/epilogue `0x9b2c6a`, which leaves `eax = 1` (error) and
returns. **[HIGH / OBSERVED — jump table read this session.]**

> **NOTE — the live arms are not in codename order in the table, but resolve
> correctly.** The five `lea <gen>_libs` targets are interleaved
> (`idx7`→cayman jumps *forward* to `0x9b2c13`, `idx15`→mariana jumps *back* to
> `0x9b2c06`). The compiler laid the case bodies out for branch density, not in
> ordinal order — but each `lea` names its generation's `*_libs` symbol
> explicitly, so the `coretype → codename` map is unambiguous. Do not infer the
> codename from the *address order* of the case bodies; read the `lea` operand.

### 3.4 · The dispatch as annotated C pseudocode

```c
/* nrtucode_get_ext_isa(coretype, isa_index=0, out_struct, scratch)
 *   -> 0 = ok, 1 = unknown coretype, 3 = NULL getter (gen present but image absent)
 *
 * The five live coretypes each index a 4-entry <gen>_libs table of
 * { SO_get(), JSON_get() } 16-byte thunk pairs. isa_index (0..3) selects which
 * of the four EXTISA variants; the public wrapper passes 0, and an orthogonal
 * NEURON_UCODE_FLAVOR env block (§3.5) may bump it to a debug/test variant
 * BEFORE this switch — it does not change the coretype bound. */
int nrtucode_get_ext_isa_internal(uint32_t coretype, int isa_index,
                                  ext_isa_out_t *out, void *scratch)
{
    /* … NEURON_UCODE_FLAVOR strcmp block runs here, only when isa_index == 0 … */

    uint32_t i = coretype - 6;            /* 0x9b2bdb */
    if (i > 31u)                          /* 0x9b2bdf cmp $0x1f ; 0x9b2be3 ja  */
        return 1;                         /* default: unknown coretype         */

    const lib_pair_t *libs;               /* base-relative jump *table[0x556c] */
    switch (coretype) {
        case  6: libs = sunda_libs;        break;  /* idx 0  @0x9b8f80  SUNDA        */
        case 13: libs = cayman_libs;       break;  /* idx 7  @0x9b8f90  CAYMAN       */
        case 21: libs = mariana_libs;      break;  /* idx 15 @0x9b8fd0  MARIANA      */
        case 29: libs = mariana_plus_libs; break;  /* idx 23 @0x9b9010  MARIANA_PLUS */
        case 37: libs = maverick_libs;     break;  /* idx 31 @0x9b9050  MAVERICK     */
        default: return 1;                         /* the other 27 cases → ret 1     */
    }

    const lib_pair_t *e = &libs[isa_index];        /* 0x9b2c38: rcx = &table[isa_index] */
    if (e->so_get == NULL) return 3;               /* gen present but image absent      */
    if (e->json_get == NULL) return 3;
    e->so_get(out, &out[1]);                       /* call *%rdx — fill SO image   */
    e->json_get(&out[2], &out[3]);                 /* call *%r14 — fill JSON image */
    return 0;
}
```

### 3.5 · Two independent corroborations of the live-set

The jump table is not the only firmware structure that fixes
`{6,13,21,29,37}`. Two resolver bitmasks, decoded this session, agree:

```
nrtucode_get_num_ext_isa_libs   @0x9b2c90:
   0x9b2c9c  cmp    $0x25,%edi              ; reject coretype > 37
   0x9b2ca3  movabs $0x2020202000,%rcx      ; bits {13,21,29,37}
   0x9b2cad  bt     %rdx,%rcx
   0x9b2cc3  cmp    $0x6,%rdx ; je …        ; coretype 6 (SUNDA) handled separately
   => 4 EXTISA libs each for {6,13,21,29,37}

nrtucode_opset_get_library_index @0x9b1950:
   0x9b1a18  cmp    $0x25,%esi
   0x9b1a1f  movabs $0x2020202040,%rsi      ; bits {6,13,21,29,37}  (bit 6 set directly)
   0x9b1a29  bt     %rcx,%rsi

nrtucode_ll_get_libraries_from_opcodes @0x9b1880:
   0x9b18a8  cmp    $0x25,%edi
   0x9b18b2  movabs $0x2020202000,%rdx      ; bits {13,21,29,37}
   0x9b18bc  bt     %rcx,%rdx
```

Bit-decode (this session): `0x2020202000 → {13,21,29,37}`,
`0x2020202040 → {6,13,21,29,37}`. Three structures (the jump table + two
bitmasks) independently fix the same gen-set. **[HIGH / OBSERVED]**

> **QUIRK — the `strcmp` block at the top of `_internal` is the
> `NEURON_UCODE_FLAVOR` selector, not part of the coretype switch.** At
> `0x9b2b5b`–`0x9b2bd0` the function `getenv("NEURON_UCODE_FLAVOR")` and
> `strcmp`s against `"debug"/"DEBUG"/"test"/"TEST"`; when `isa_index == 0` it
> may bump the selected `isa_index` (e.g. to 1 for a test flavor) — i.e. it
> picks *which of the four per-gen EXTISA variants* to fetch. The coretype
> switch and its 32-case bound at `0x9b2bdb+` run afterward and are unaffected.
> Do not mistake these four string compares for a fifth/sixth generation.

---

## 4 · The MAVERICK row — `ct37` OBSERVED, `arch36` doubly INFERRED (the WALL)

MAVERICK is the one row whose two key columns disagree on their grounding.
Carry this distinction exactly; it is the most important caveat on the page.

**`coretype 37` is OBSERVED — three independent firmware reads:**

1. **The enum ordinal.** `NRTUCODE_CORE_MAVERICK_Q7_POOL` is enum index 37
   (`nrtucode.h:56`). **[HIGH / OBSERVED]**
2. **The jump table.** `get_ext_isa` case idx 31 (`coretype 37`) →
   `lea … 0x9b9050 <maverick_libs>` (§3.3). **[HIGH / OBSERVED]**
3. **The resolver bitmasks.** `0x2020202000`/`0x2020202040` both set bit 37, and
   `cmp $0x25` (37) is the accept-bound in all three resolvers (§3.5). **[HIGH /
   OBSERVED]**

**`arch_id 36*` is INFERRED — and *doubly* so:**

- There is **no `arch_id` byte for MAVERICK.** The only `arch_id`-keyed firmware
  structures are the two NCFW selectors (`get_image`, `ctx_log`); both compare
  *exactly* `{0x05,0x0c,0x14,0x1c}` with a `ja` default. There is **no `0x24`
  (36)** and **no `0x25` (37)** NCFW selector byte anywhere (§5). A hypothetical
  MAVERICK `arch_id 0x24` falls through `ja 0x12ec` to `return 2`.
- **The `coretype − 1` slot is a placeholder, not a core.** For SUNDA…
  MARIANA_PLUS, `arch_id` *is* the `<GEN>_NX_TOPSP` ordinal (5/12/20/28). For
  MAVERICK that pattern **breaks**: `NRTUCODE_CORE_MAVERICK_NX_TOPSP` is enum
  index **54** (`nrtucode.h:54`), *not* 36. The enum slot at index 36 is
  `NRTUCODE_CORE_MAVERICK_NX__REMOVED__` (`nrtucode.h:55`) — a removed/reserved
  placeholder. So `arch_id 36` is `coretype(37) − 1` landing on a `__REMOVED__`
  name, **not** on a real `NX_TOPSP` core. It is inferred from the `−1` relation
  *and* it contradicts the very structure (`NX_TOPSP = arch_id`) that grounds
  the other four rows.

So `arch_id 36 = 0x24` is consistent with the four shipped rows arithmetically
but has **no direct anchor and a structural counter-signal**. Mark it `36*`,
tag it **MED / INFERRED**, and never present it as binary-observed.

> **CORRECTION to SX-GEN-01.** The backing survey treats MAVERICK `arch_id 36`
> as "INFERRED from coretype = arch_id + 1" but does not record that the enum
> slot at 36 is a `__REMOVED__` placeholder while the real
> `MAVERICK_NX_TOPSP` is at 54. The `NX_TOPSP = arch_id` correspondence that
> holds for SUNDA…MARIANA_PLUS *does not hold* for MAVERICK. This page records
> it: `arch_id 36` is doubly inferred, not merely "the +1 of the coretype". The
> cross-walk §3 likewise says MAVERICK "drops `NX_ACT` … adds an
> `NX__REMOVED__` placeholder" — confirmed: the MAVERICK block starts at
> `MAVERICK_NX_DVE` (`nrtucode.h:50`, no `NX_ACT` — the ACT→DVE fold) and the
> whole block is gated behind `#if defined(NRTUCODE_INTERNAL_NAMES)` (L49).

**Why there is no NCFW image at all.** `NCFW` ships exactly eight firmware-blob
symbols `{v2,v3,v4,v4_plus} × {iram,dram}` (§5); the rodata blob region closes
right after `v4_plus`, with no room for a fifth image; zero `maverick`/`v5`
string; four `.c` source strings only. MAVERICK is realized only in the newer
(clang-15 / XtensaTools-15.05, ET_DYN/PIC) internal twin's dispatch tables and
its four internal-only Q7 ELFs. The NCFW absence is a missing *orchestration
layer*, not a missing feature — MAVERICK still has on-engine collective
capability the same way CAYMAN…MARIANA_PLUS do. See
[The MAVERICK Profile](maverick-profile.md). **[HIGH / OBSERVED]**

---

## 5 · The NCFW selector — how the host picks the management-core image

`libncfw_get_image` (dynsym `T` @ `0x1179` in `NCFW`) is the host function that
maps an `arch_id` to a `{iram, dram}` NCFW firmware image. It is a **balanced
binary-search tree** over `arch_id` (in `edi`, spilled to `-0x4(%rbp)`), not a
flat linear ladder; the `(out)` struct it fills is
`[0]=iram_ptr, [8]=iram_size, [0x10]=dram_ptr, [0x18]=dram_size`. It returns 0
on hit, 2 on miss, `0x16` (22) if the out-pointer is NULL.

```
0x1188  cmpq $0x0,-0x10(%rbp) ; mov $0x16        ; (out == NULL) -> EINVAL guard
0x1199  cmpl $0x1c,-0x4(%rbp) ; je 0x12a7        ; arch_id 0x1c (28) -> v4_plus_ncfw_*  MARIANA_PLUS
0x11a3  cmpl $0x1c            ; ja 0x12ec        ; arch_id > 28 -> default (covers 0x24/maverick)
0x11ad  cmpl $0x14,-0x4(%rbp) ; je 0x1262        ; arch_id 0x14 (20) -> v4_ncfw_*       MARIANA
0x11b7  cmpl $0x14            ; ja 0x12ec        ; default
0x11c1  cmpl $0x5, -0x4(%rbp) ; je 0x11d2        ; arch_id 0x05 (5)  -> v2_ncfw_*        SUNDA
0x11c7  cmpl $0xc, -0x4(%rbp) ; je 0x121a        ; arch_id 0x0c (12) -> v3_ncfw_*        CAYMAN
0x11cd  jmp  0x12ec                              ; default -> mov $0x2,%eax ; ret
```

The selector set is **exactly `{0x1c, 0x14, 0x05, 0x0c}`** = arch_id
{28, 20, 5, 12} for {MARIANA_PLUS, MARIANA, SUNDA, CAYMAN}. There is **no**
branch on `0x24` (36) and **no** branch on `0x25` (37) anywhere in
`0x1179..0x1309` — the NCFW image → generation map is *total on the four shipped
generations and undefined for MAVERICK*. **[HIGH / OBSERVED — the absent `0x24`
is OBSERVED-negative.]**

The eight NCFW blob symbols (`nm NCFW`), strictly increasing in `.rodata`
offset in the same `v2 < v3 < v4 < v4_plus` order that anchors the codename↔v#
pairing:

| gen | iram blob `r` | dram blob `r` |
|---|---|---|
| SUNDA (v2) | `v2_ncfw_iram_bin` @ `0x6a140` | `v2_ncfw_dram_bin` @ `0x66a60` |
| CAYMAN (v3) | `v3_ncfw_iram_bin` @ `0x79860` | `v3_ncfw_dram_bin` @ `0x74a40` |
| MARIANA (v4) | `v4_ncfw_iram_bin` @ `0x83260` | `v4_ncfw_dram_bin` @ `0x7e440` |
| MARIANA_PLUS (v4+) | `v4_plus_ncfw_iram_bin` @ `0x8ccc0` | `v4_plus_ncfw_dram_bin` @ `0x87ea0` |

`nm NCFW | rg -i 'v5|maverick'` → **0 hits**. **NCFW tops out at MARIANA_PLUS.**
**[HIGH / OBSERVED]**

> **GOTCHA — the `get_image` BST is unordered; the codename↔v# pairing is
> triple-anchored against the mis-read trap.** The ladder compares `0x1c`
> first, then a `ja` guard, then `0x14`, `0x05`, `0x0c` — out of numeric order,
> which is exactly how a prior cross-reference wave crossed the CAYMAN/MARIANA
> labels on the v3/v4 rows (ledger §8, LEDGER-1). The canonical pairing is
> re-anchored three ways here: the `je` targets resolve in firmware-blob address
> order (`v2 < v3 < v4 < v4+`), the four `.c` source strings sort
> `sunda.c < cayman.c < mariana.c < mariana_plus.c`, and the eight
> `v{…}_ncfw_{iram,dram}_bin` symbols sit at strictly increasing `.rodata`
> offsets in the same order. All three agree: `0x05=v2=SUNDA`, `0x0c=v3=CAYMAN`,
> `0x14=v4=MARIANA`, `0x1c=v4+=MARIANA_PLUS`.

---

## 6 · Shipped vs internal — the 4/5 split and the EXTISA blob identities

"Is GPSIMD four generations or five?" is a binary-version question, not an
inconsistency. The shipped runtime path tops out at MARIANA_PLUS; MAVERICK lives
only in the non-shipped symbol twin.

| lib | SUNDA | CAYMAN | MARIANA | M_PLUS | MAVERICK | bound |
|---|---|---|---|---|---|---|
| `NCFW` (`libncfw.so`, images) | v2 | v3 | v4 | v4+ | — | `arch_id ≤ 0x1c` (28) |
| `SO` (`libnrtucode.so`, front) | ✓ | ✓ | ✓ | ✓ | — (stub) | `ct ≤ 0x25`; resolver bound |
| `EXT` (`libnrtucode_extisa.so`) | ✓ | ✓ | ✓ | ✓ | — | n/a (container) |
| `A` (`libnrtucode.a`, static) | —¹ | ✓ | ✓ | ✓ | — | n/a |
| `INT` (`libnrtucode_internal.so`, twin) | ✓² | ✓ | ✓ | ✓ | **✓** | `ct ≤ 0x25` (37) |

¹ SUNDA `.a` contents resolved from the container as a weak-undef. ² The
internal twin also carries the SUNDA weak-undef plus the four MAVERICK-only Q7
ELFs.

**The static archive, member-counted (`ar t A | wc -l`):**

```
libnrtucode.a  total members ............... 435
   SUNDA      img_sunda_*_contents.c.o ......  48
   CAYMAN     img_cayman_*_contents.c.o ..... 124
   MARIANA    img_mariana_*_contents.c.o .... 124   (excl. mariana_plus)
   MARIANA_PLUS img_mariana_plus_*.c.o ...... 124
   MAVERICK   ............................... 0      <-- absent
   gen-agnostic core (nrtucode*/prelink*/…) . 15
   --------------------------------------------------
   48 + 124 + 124 + 124 (= 420 codename blobs) + 15 core = 435  (exact, no slack)
```

`ar t A | rg -i maverick | wc -l` → **0**. MAVERICK does not ship in the
archive. **[HIGH / OBSERVED]**

**The split is sharp in three symbol/string tallies** (re-counted this session):

- `NRTUCODE_CORE_*` enum literals: `INT` names **five** generations
  (incl. `NRTUCODE_CORE_MAVERICK_NX_POOL`); `SO` names **four** (no MAVERICK).
- `nm INT | rg -ci maverick` → **125** MAVERICK symtab symbols (the
  `maverick_libs` table + `MAVERICK_*_get` thunks); `nm SO` is stripped → **0**.
- `strings INT | rg -oi maverick | wc -l` → **189** occurrences; `strings SO` →
  **0**. (`nm -D` dynamic on `INT` → 0 maverick: these are static/local
  symbols, consistent with internal-only microcode.) **[HIGH / OBSERVED]**

> **CORRECTION to SX-GEN-01.** SX-GEN-01 §4 records the MAVERICK string tally as
> "internal=187, front=0". The session re-count is **189** strings occurrences
> (`strings INT | rg -oi maverick | wc -l`) and **125** symtab symbols
> (`nm INT | rg -ci maverick`). The `187` figure was a `rg -ac` raw-byte-pattern
> count over the binary, which under-counts vs `strings`-tokenized occurrences.
> Use **189 strings-occurrences / 125 symtab-symbols / 0 in `SO`**. The cross-
> walk §4 already says 189; this page ratifies 189 over 187.

**Per-generation EXTISA blob `sha256` (idx0 representative; carved this session
via the `*_SO_get` thunks).** The five `*_libs` tables are `nm`-confirmed at
`sunda_libs@0x9b8f80 < cayman_libs@0x9b8f90 < mariana_libs@0x9b8fd0 <
mariana_plus_libs@0x9b9010 < maverick_libs@0x9b9050`, indexed by the
`coretype − 6` jump table of §3:

| gen | coretype | idx0 EXTISA blob `sha256` | carve source |
|---|---|---|---|
| SUNDA | 6 | `444497066f5e1738e24d2db6a373f64e13da5625180a1bfcdf97f82f58ab84c0` | container `EXT` (single image; `sunda_libs` is a weak-undef in `INT`) |
| CAYMAN | 13 | `910d41c3ededce67cd00ec7041a5e66c3c39536d2e9b16fe21ea019db4b55527` | `INT` `@0x2ef7e0 sz 0xa260` |
| MARIANA | 21 | `9f2ce049608c0a88e3947137e68bf2bccd070f3a67658b18d847f4fda7b0c751` | `INT` `@0x5893c0 sz 0xa260` |
| MARIANA_PLUS | 29 | `9f2ce049608c0a88e3947137e68bf2bccd070f3a67658b18d847f4fda7b0c751` | `INT` `@0x855240 sz 0xa260` — **byte-identical to MARIANA** |
| MAVERICK | 37 | `a92c8ba0e9dfb2d85aa3926e147ae2fdc550346e034afa49b70a75ed4df587f4` | `INT` `@0x994de0 sz 0x7fb0` (ET_DYN/PIC, internal-only) |

**MARIANA ≡ MARIANA_PLUS at the device-image level — confirmed three ways:** (1)
`INT` MARIANA_0 (`0x5893c0`) sha == `INT` MARIANA_PLUS_0 (`0x855240`) sha =
`9f2ce049…c751`; idx3 likewise both `8477ff26…a4df`; (2) cross-packaging — the
container `EXT` MARIANA_0 carves to the *same* `9f2ce049…c751`; (3) MARIANA_PLUS
adds **zero** new EXTISA images, so the 29-carve corpus reduces to **13
distinct** ELFs. MARIANA_PLUS has *no* `neuron_mariana_plus_arch_isa` dir — it
shares MARIANA's ISA — and is a *code / feature-flag delta* selected at runtime
via `NEURON_RT_DBG_V4_PLUS=0/1` (the env that replaced the removed
`NRTUCODE_MPLUS_ON_MARIANA` flag, whose "flag has been removed" tripwire string
is still present in `SO`). Detail in
[MARIANA_PLUS (v4+) Generation Delta](mariana-plus-delta.md). **[HIGH /
OBSERVED]** Full blob inventory:
[EXTISA Image Inventory](../images/extisa-inventory.md),
[Image Catalog Index](../images/image-catalog-index.md).

`strings EXT | rg -ci maverick` → **0** — MAVERICK EXTISA is absent from the
runtime container too, present *only* in `INT`. **[HIGH / OBSERVED]**

---

## 7 · TONGA — the pre-unified outlier (resolved)

TONGA is a real, older codename, but it is **not** a sixth GPSIMD generation and
it is **not** part of the `coretype = arch_id + 1` family. It is the legacy "L"-
family ISA-header predecessor — the historical **NC-v1 / Inf1-era** part from
which the modern `neuron_*_arch_isa` headers descend — with **zero** GPSIMD
runtime identity.

- **Byte-grounded "older than SUNDA":** the SPIS Product-ID register
  description reads `"Product ID. (Tonga - 0x01, Sunda - 0x02, Cayman - 0x03)"`
  — `extracted/nested/cayman-arch-regs_tgz/csrs/spis/spis_model.json:107`. TONGA
  = Product ID `0x01`, one generation *before* SUNDA (`0x02`). This is the
  concrete byte that fixes its position. **[HIGH / OBSERVED]**
- **No runtime identity:** no `coretype`, no `arch_id`, no
  `NRTUCODE_CORE_TONGA` enum entry, no `tonga_libs`, no NCFW image, no EXTISA
  blob; absent from every `get_image` / `get_ext_isa` / `get_num_ext_isa_libs` /
  resolver selector. `strings INT | rg -i tonga` → 0. **[HIGH / OBSERVED]**
- **Distinct, older dtype enum family:** its dtypes are
  `TONGA_ISA_TPB_DTYPE_*` — **8 codes** `{INVALID=0, UINT8=3, UINT16=5,
  BFLOAT16=6, FP16=7, INT32=8, FP32=0xA, INT64=0xC}` in
  `INC/arch-isa/tpb/aws_tonga_isa_tpb_common.h` — a strict subset of the
  16-code `NEURON_ISA_TPB_DTYPE_*` set the five GPSIMD gens use. A different
  enum *name family* (`TONGA_` vs `NEURON_`) means a different, older ISA, not a
  GPSIMD generation. **[HIGH / OBSERVED]**
- **On-disk shape:** `INC/arch-headers/` has **six** dirs (the five codenames +
  `tonga`, register maps only), but `INC/neuron_*_arch_isa/` has only **four**
  ISA dirs `{sunda, cayman, mariana, maverick}`. TONGA appears only in the
  legacy `arch-isa/` tree, never as a `neuron_tonga_arch_isa` GPSIMD ISA.
  Its deprecated opcodes survive as comments in the live headers
  (`… = 0x04 // tonga stuff, deprecated`), and the CMake umbrella package
  retains the historical name (`TongaArchIsa::TongaArchIsa`,
  `TongaArchHeaders::TongaArchHeaders`). **[HIGH / OBSERVED]**

TONGA is kept in the *chronology* row of the master table for completeness — and
bound to the Inf1 / legacy `V1` arch by the platform compiler surface — but it
is **explicitly excluded from the five-generation runtime line**. Anyone who
finds `tonga` in an ABI header must treat it as the legacy "L" / TONGA ISA,
never as a sixth coretype.

The full ordering, oldest → newest:

```
TONGA(NC-v1, Inf1, ProdID 0x01)  ⊳  SUNDA(v2)  ⊳  CAYMAN(v3)  ⊳  MARIANA(v4)  ⊳  MARIANA_PLUS(v4+)  ⊳  MAVERICK(v5)
└──── outside the unified family ────┘    └──────── coretype = arch_id + 1 (no NCFW v5 for MAVERICK) ────────┘
```

The ordering is *fixed by bytes*, not asserted: (a) the NCFW selector immediates
`{0x05,0x0c,0x14,0x1c}` are strictly increasing in lockstep with the `v#` tags;
(b) the `coretype`/`arch_id` enum ordinals are monotone (`{6,13,21,29,37}` /
`{5,12,20,28,36*}`); (c) the dtype space is a strict superset chain
(TONGA ⊂ SUNDA/CAYMAN ⊂ MARIANA ⊂ MAVERICK); (d) the toolchain bumps at MAVERICK
(clang-10/XT-14.09 → clang-15/XT-15.05, EXEC → ET_DYN). All four anchors point
the same direction. See
[Cross-Generation Arch-ISA Header Diff](arch-isa-header-diff.md) and
[Cross-Generation Opcode-Table Diff + TONGA](cross-gen-opcode-diff.md) for the
dtype/opcode expansion detail.

---

## 8 · Contradiction ledger

Every place where a report, a header, or a binary has disagreed on a generation
identity, with the resolution. The first three are inherited from prior waves
(do not re-introduce them); the last two are CORRECTIONs this page makes to
SX-GEN-01.

| # | claim in conflict | wrong reading | resolved reading | anchor |
|---|---|---|---|---|
| **L1** | NCFW codename↔v# on the v3/v4 rows | "0x0c = mariana, 0x14 = cayman" (NCFW-04/05/06) — labels crossed | `0x05=v2=SUNDA, 0x0c=v3=CAYMAN, 0x14=v4=MARIANA, 0x1c=v4+=MARIANA_PLUS` | triple-anchored: `je` blob-address order + `.c` string order + `*_ncfw_*` symbol-offset order (§5 GOTCHA) |
| **L2** | `get_memory_image` arg0 | mis-labeled "coretype" (RT-16/RT-03) | a **flat (gen × engine) image index 0..37**, *not* the coretype `{6,13,21,29,37}` that `get_ext_isa` keys on. Value 37 collides (MAVERICK coretype AND its Q7_POOL flat index) but they are different fields | RT-17 §5/§8; keep the three key-spaces (coretype / arch_id / flat index) apart |
| **L3** | `NRTUCODE_MPLUS_ON_MARIANA` env | a live "MPLUS-on-MARIANA" selector (RT-03) | a **removed-flag tripwire**: if set, `get_memory_image` fwrites "…flag has been removed… transition to NEURON_RT_DBG_V4_PLUS=0/1" and returns 8. The live v4/v4+ selector is `NEURON_RT_DBG_V4_PLUS` | the tripwire string is still in `SO` (§6) |
| **L4** | MAVERICK `arch_id 36` grounding | "INFERRED from coretype = arch_id + 1" (SX-GEN-01) — *understated* | **doubly INFERRED**: the enum slot at 36 is `NRTUCODE_CORE_MAVERICK_NX__REMOVED__` (a placeholder), while the real `MAVERICK_NX_TOPSP` is at index **54**. The `NX_TOPSP = arch_id` correspondence that holds for the other four rows *fails* for MAVERICK | `nrtucode.h:54,55` (§4) — **this page's CORRECTION** |
| **L5** | MAVERICK string tally | "internal=187" (SX-GEN-01 §4) | **189** strings-occurrences / **125** symtab-symbols / **0** in `SO`. The `187` was a `rg -ac` raw-byte count; use the `strings`-tokenized 189 | `strings INT \| rg -oi maverick \| wc -l` (§6) — **this page's CORRECTION** |

**Non-contradictions (consistency confirmations, for the record):** the live
coretype set `{6,13,21,29,37}` agrees across the jump table and both resolver
bitmasks; MARIANA ≡ MARIANA_PLUS (EXTISA + NCFW DRAM byte-identical) agrees
across `INT`, `EXT`, and the catalog index; `coretype = arch_id + 1` agrees
between the `NX_TOPSP`/`Q7_POOL` enum ordinals and the NCFW selector immediates.
No firmware structure contradicts any of these.

---

## 9 · Confidence rollup / honest gaps

**HIGH / OBSERVED** (binary/header-exact, re-verified this session): the five
codenames; `coretype {6,13,21,29,37}` (jump table + two bitmasks); `arch_id
{5,12,20,28}` (`NX_TOPSP` ordinals + NCFW selector); `coretype = arch_id + 1`
(enum-layout consequence); the 32-case `get_ext_isa` dispatch with live-set
`{6,13,21,29,37}`; the NCFW selector immediates `{0x05,0x0c,0x14,0x1c}` with no
`0x24`/`0x25`; the eight NCFW blob symbols (no v5); the per-gen EXTISA `sha256`s
with MARIANA ≡ MARIANA_PLUS byte-identity; the 4-shipped/5-internal split
(`.a` = 0 MAVERICK members / 435 total; `INT` 189 strings & 125 symbols vs `SO`
0); TONGA = legacy NC-v1 (Product ID `0x01`) with no runtime identity.

**MED / INFERRED:** MAVERICK `arch_id 36*` (the `coretype − 1` extrapolation
landing on a `__REMOVED__` slot — §4); "internal twin = newer 5-gen build, front
= older 4-gen shipped" (toolchain + bound divergence).

**LOW / OPEN (not claimed — future work):** the definitive `coretype → silicon-
part` (Trn2/Trn3/Inf2/Trn4…) binding is **not in this corpus**; the silicon
column in §1 is INFERRED and the MAVERICK product family is genuinely open (no
"Trn4" is named anywhere — do not fabricate one). Trace
`nrtucode_core_get_coretype` consumers / the `libnrt.so` device probe to close
it.

---

## See also

- [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) — the
  Part-0 quick-reference join table this page proves and deepens (the `CoreV5`
  vs `NC-v5` overload, the carried product/PCI-ID columns live there).
- [SUNDA v2 Baseline Topology](sunda-v2-baseline.md),
  [MARIANA_PLUS (v4+) Generation Delta](mariana-plus-delta.md),
  [The MAVERICK Profile](maverick-profile.md) — the per-row deep-dives.
- [Master Per-Generation Capability Matrix](master-capability-matrix.md) — the
  full subsystem × generation grid.
- [Cross-Generation Arch-ISA Header Diff](arch-isa-header-diff.md),
  [Cross-Generation Opcode-Table Diff + TONGA](cross-gen-opcode-diff.md) — the
  dtype/opcode expansion chain.
- [EXTISA Image Inventory](../images/extisa-inventory.md),
  [Image Catalog Index](../images/image-catalog-index.md) — the full per-blob
  `sha256` / size / BuildID catalog.
