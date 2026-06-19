# ISA Batch 10 — wvec Pack (wide→narrow readout)

This is the per-instruction reference for the **`wvec`→`vec` pack** family of the Vision-Q7 *Cairo*
(`ncore2gp`) ISA — the **wide-accumulator readout**. After a [B04](b04-mac-integer.md)/[B05](b05-mac-mixed.md)
MAC chain has reduced a long dot-product into the 1536-bit
[`wvec` wide accumulator](../core/register-files.md#per-file-detail) (24/48/96-bit lanes), the only way
to *consume* that result is a **pack**: read a `wvec` lane, **arithmetic-right-shift** it by an
`AR`-supplied (or per-lane-vector-supplied) amount to de-scale the Q-format product, **round**, **saturate**
to the narrow signed or unsigned range, and write the narrowed lanes back into a 512-bit
[`vec` register](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster). The 42
`ivp_pack*` mnemonics here are that readout. It is the dual of the MAC: B04/B05 own the *accumulate*; B10
owns the *narrowing read-out*; [B22](b22-unpack-wvec-mov.md) owns the *non-narrowing* unpack/move. Every
mnemonic's FLIX slot, opcode-selector template, operand model and value semantics are read directly out of
the shipped binaries (`libisa-core.so` encode thunks + iclass arg tables, `libfiss-base.so` `xdref` value
leaves **driven live by ctypes**, `libcas-core.so` pipeline tags) and tagged.

This page inherits the certified-perfect denominator from
[the coverage tally](../core/coverage-tally.md): the `1534 / 12569` shipped mnemonic/placement cover and the
`864/864` value-leaf cover. Counts are grounded with `nm | rg -c` against the binary `.symtab`, never a
decompile grep; the `extracted/` tree is gitignored (reach it with `fd --no-ignore` or an absolute path).
Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` =
a byte / immediate / symbol / **executed** value read from the shipped binary; `INFERRED` = reasoned over
OBSERVED; `CARRIED` = re-used at a cited page's confidence; crossed with `HIGH`/`MED`/`LOW`. All prose is
binary / static-analysis derived only.

> **Scope in one line.** B10 = `ivp_pack*` (the `packv*` saturating round-shift readout, the `packl*`/
> `packm*`/`packp*`/`packq*`/`packh*` word-select extracts, and their signed/unsigned · round/no-round ·
> AR-shift/vector-shift · low/high-half variants), reading **24 / 48 / 96-bit** `wvec` lanes and writing
> **8 / 16 / 32-bit** `vec` lanes with **arithmetic right-shift + round-half-up + signed/unsigned
> saturate**. **42 mnemonics, 217 placements, 56 value leaves.** The op rides the **Ld slot `s1`**, *not*
> the Mul slot — it is a readout, not a multiply. `[HIGH/OBSERVED]`

---

## 1. Key facts

| Fact | Value | Binary source |
|---|---|---|
| B10 mnemonics (`ivp_pack*`) | **42** | `nm libisa-core.so \| rg -o 'Opcode_(ivp_pack[a-z0-9_]*)_Slot_' \| sort -u \| wc -l` |
| B10 placements (`mnemonic × slot`) | **217** | `nm \| rg -c 'Opcode_ivp_pack[a-z0-9_]*_Slot_.*_encode'` (= `25×8 + 17×1`) |
| B10 value leaves (`xdref_*pack*`, excl. `mul*packl`) | **56** | `nm libfiss-base.so \| rg 'module__xdref_pack' \| rg -vc 'mul'` |
| Issue slot | **`s1 = Ld`** of the wide formats + `N2` | `Opcode_ivp_pack*_Slot_<f>_s1_ld_encode` — **never** `s2_mul` |
| Source (read) | **`wvec`** (idx 5, 1536-bit, 4 entries) — viewed as `Nx24`/`Nx48`/`Nx96` | `opnd_ivp_sem_wvec_pack_wvr` (IN) |
| Destination (write) | **`vec`** (idx 2, 512-bit, 32 entries) — `2Nx8`/`Nx16`/`N_2x32` lanes | `opnd_ivp_sem_wvec_pack_vt` (OUT) |
| Shift / round amount | **`AR` scalar** (`r`-forms) **or** per-lane **`vec`** (no-`r` forms) | iclass `_args` operand 2 = `art` (i) **or** `..._vr` (i) ([§3.3](#33-the-operand-model--art-shift-vs-vector-shift)) |
| `wvec`→`vec` lane narrowing | 24→8, 48→16, 96→32 (the inverse of the B04 8→24/16→48/32→96 widen) | `xdref` width-sig `_<outbits>_<accbits>_<shiftbits>` |
| Rounding mode | **round-half-up** (`+ (1<<(shift-1))` bias, ties toward +∞) | executed: `packv_8_24_8`, `packvr_16_48_32` ([§5.2](#52-rounding--round-half-up-proven-on-half-way-inputs)) |
| Saturation | signed-`N` (`packv*`/`packvr*`), unsigned-`N` (`packvu*`/`packvru*`), **none** (`packvnr*`/`packl*`) | executed: clamp to `±max` vs wrap ([§5.1](#51-saturation--signed-unsigned-and-the-no-sat-truncate), [§5.3](#53-roundvstruncate-and-no-saturate-the-nr-forms)) |
| Shift clamp | `AR` shift clamped to **≤23** (8-bit out) or **≤32** (16/32-bit out) before use | `module__xdref_shift_amt_satu_{23,32}` helper ([§4.2](#42-the-shift-amount-is-clamped-before-use)) |
| Pipeline | `s1_ld` op, **stage0…stage12**, result written into `vec` `@stage12` | `F0_F0_S1_Ld_16_IVP_PACK*_inst_stage{0..12}` (`libcas-core.so`) |
| User opcode (16-bit form) | `IVP_PACKVRNX48` + the two-half `_0`/`_1` 32-bit-readout variants | [register-files §6.2](../core/register-files.md#62-vec--wvec--pack-write-into-the-accumulator-and-unpack-read-it-back-out) |

The family is large because **four orthogonal axes multiply out**: **lane width** (8/24, 16/48, 32/96) ×
**saturation** (signed / unsigned / none) × **rounding** (round / no-round) × **shift source** (AR scalar /
per-lane vector / none), plus the **word-half select** (low / mid / high / both-halves `_0`/`_1`) on the
extract forms. [§2](#2-roster--the-42-ivp_pack-mnemonics) organises the roster on those axes; the token
grammar is decoded in [§2.1](#21-the-pack-token-grammar-decoded).

---

## 2. Roster — the 42 `ivp_pack*` mnemonics

Columns: `mnemonic` · `FLIX format·slot` (the slots hosting it — all sit in the **Ld** slot `s1`) ·
`opcode-sel imm` (the `F6_S1_Ld` encode-thunk template `WORD0`, the
[universal `C7 07 imm32 C3` ABI](../core/flix-encoding.md#61-the-universal-encode-thunk-abi); per-format
packing differs) · `wvec→vec` (acc-bits → out-bits) · `shift src` (`AR` / `vec` / none) · `sat` (signed /
unsigned / none) · `round` · `xdref leaf` · `[conf]`. Templates are byte-exact from `objdump -d` this pass;
semantics from the joined [`xdref` leaf](#6-the-mnemonic--value-leaf-join-by-width-and-token) executed live.

### 2.1 The pack token grammar, decoded

Every mnemonic is `ivp_pack` + a token string + a lane-shape suffix. Decoded from the executed leaf bodies
and the iclass operand models ([§3.3](#33-the-operand-model--art-shift-vs-vector-shift),
[§5](#5-value-semantics--driven-live)):

| token | meaning | shift source | saturation | rounding |
|---|---|---|---|---|
| `l` (`packl*`) | **low** word extract (truncate to low `N` bits) | none | none | none |
| `m` (`packm*`) | **mid** word extract (`>>16` then narrow) | none | none | none |
| `h` (`packh*`) | **high** word extract | none | none | none |
| `p` / `q` (`packp*`/`packq*`) | partial / quad-packed extract variants | none | varies | none |
| `v` (`packv*`) | **vec** narrow-with-saturate; shift from a per-lane **`vec`** | `vec` (per-lane) | **signed-`N`** | none |
| `vr` (`packvr*`) | vec + **round**; shift from **`AR`** scalar | `AR` | **signed-`N`** | round-half-up |
| `vnr` (`packvnr*`) | vec + **no-round, no-saturate** (raw arithmetic-shift) | `vec` (per-lane) | **none** (wraps) | none |
| `vrnr` (`packvrnr*`) | vec + round + **no-saturate** | `AR` | none (wraps) | round-half-up |
| `vu` (`packvu*`) | vec + **unsigned**-saturate | `vec` (per-lane) | **unsigned-`N`** | round-half-up |
| `vru` (`packvru*`) | vec + round + **unsigned**-saturate; `AR` shift | `AR` | **unsigned-`N`** | round-half-up |
| `_0` / `_1` | low-half / high-half of a **two-step full-width** (32-bit) readout | — | — | — |
| `r` (mid-token) | the operand carries an **`AR`** shift instead of a per-lane `vec` shift | — | — | — |

The single most useful disambiguator: the presence of an internal **`r`** flips the shift source from a
*per-lane `vec`* operand to a *scalar `AR`* operand (`art`). That is the structural reason the no-`r` forms
place in only **one** slot ([§3.2](#32-the-bimodal-placement--8-slots-r-vs-1-slot-f6-only)).

### 2.2 `packvr*` — AR-shift, round, signed-saturate (the canonical readout, 9 leaves / many mnemonics)

This is the production readout: a quantized GEMM accumulates `int32` partial sums into `wvec`, then a
`packvrnx48` de-scales by the per-tensor right-shift (an `AR` scalar), rounds, and saturates to `int16`.

| mnemonic | fmt·slot | opcode-sel imm (F6·s1) | wvec→vec | shift | sat | round | leaf | conf |
|---|---|---|---|---|---|---|---|---|
| `ivp_packvrnx48` | F0/F1/F2/F3/F4/F6/F7/N2 · s1_ld | `0x00320000` | 48 → 16 | AR | signed-16 | half-up | `packvr_16_48_32` | `[HIGH/OBSERVED]` |
| `ivp_packvrnx48_0` | (same 8 slots) | `0x00320001` | 48 → 16 (lo half) | AR | signed-16 | half-up | `packvr_16_48_32__0` | `[HIGH/OBSERVED]` |
| `ivp_packvrnx48_1` | (same 8 slots) | — | 48 → 16 (hi half) | AR | signed-16 | half-up | `packvr_16_48_32__1` | `[HIGH/OBSERVED]` |
| `ivp_packvr2nx24` | (same 8 slots) | `0x000c1000` | 24 → 8 | AR | signed-8 | half-up | `packvr_8_24_32` | `[HIGH/OBSERVED]` |
| `ivp_packvr2nx24_0` / `_1` | (same 8 slots) | — | 24 → 8 (lo/hi) | AR | signed-8 | half-up | `packvr_8_24_32` | `[HIGH/OBSERVED]` |
| `ivp_packvrn_2x64w` | (same 8 slots) | `0x00321001` | 96 → 32 (wide) | AR | signed-32 | half-up | `packvr_32_96_32` | `[HIGH/OBSERVED]` |
| `ivp_packvrnrnx48` | (same 8 slots) | `0x00300000` | 48 → 16 | AR | **none** | half-up | `packvrnr_16_48_32` | `[HIGH/OBSERVED]` |
| `ivp_packvrnrnx48_0` / `_1` | (same 8 slots) | `0x00300001` / — | 48 → 16 (lo/hi) | AR | none | half-up | `packvrnr_16_48_32__{0,1}` | `[HIGH/OBSERVED]` |
| `ivp_packvrnr2nx24` / `_0` / `_1` | (same 8 slots) | `0x000e0001` | 24 → 8 | AR | none | half-up | `packvrnr_8_24_32` | `[HIGH/OBSERVED]` |
| `ivp_packvrnrn_2x64w` | (same 8 slots) | `0x00301001` | 96 → 32 (wide) | AR | none | half-up | `packvrnr_32_96_32` | `[HIGH/OBSERVED]` |
| `ivp_packvru2nx24` / `_0` / `_1` | (same 8 slots) | `0x00340000` | 24 → 8 | AR | **unsigned-8** | half-up | `packvru_8_24_32` | `[HIGH/OBSERVED]` |

### 2.3 `packv*` (no-`r`) — per-lane VECTOR shift, F6-only (17 mnemonics, 1 placement each)

The no-`r` forms take the shift amount from a **second `vec` operand** (a per-lane shift), so each lane can
de-scale by a different amount. That extra `vec` read makes them encodable **only in F6** (the one wide
format with a spare Ld port — [§3.2](#32-the-bimodal-placement--8-slots-r-vs-1-slot-f6-only)).

| mnemonic | fmt·slot | opcode-sel imm (F6·s1) | wvec→vec | shift | sat | round | leaf | conf |
|---|---|---|---|---|---|---|---|---|
| `ivp_packvnx48` | **F6·s1_ld only** | `0x003dd000` | 48 → 16 | vec | signed-16 | none | `packv_16_48_16` | `[HIGH/OBSERVED]` |
| `ivp_packvnx48_0` / `_1` | **F6 only** | — | 48 → 16 (lo/hi) | vec | signed-16 | none | `packv_16_48_16__{0,1}` | `[HIGH/OBSERVED]` |
| `ivp_packv2nx24` / `_0` / `_1` | **F6 only** | `0x003c9000` | 24 → 8 | vec | signed-8 | none | `packv_8_24_8` | `[HIGH/OBSERVED]` |
| `ivp_packvn_2x64w` | **F6 only** | `0x00303000` | 96 → 32 | vec | signed-32 | none | `packv_32_96_32_w` | `[HIGH/OBSERVED]` |
| `ivp_packvnrnx48` / `_0` / `_1` | **F6 only** | `0x003d5000` | 48 → 16 | vec | **none** | none | `packvnr_16_48_16{,__0,__1}` | `[HIGH/OBSERVED]` |
| `ivp_packvnr2nx24` / `_0` / `_1` | **F6 only** | `0x003cf000` | 24 → 8 | vec | none | none | `packvnr_8_24_8` | `[HIGH/OBSERVED]` |
| `ivp_packvnrn_2x64w` | **F6 only** | `0x003db000` | 96 → 32 | vec | none | none | `packvnr_32_96_32_w` | `[HIGH/OBSERVED]` |
| `ivp_packvu2nx24` / `_0` / `_1` | **F6 only** | `0x00322000` | 24 → 8 | vec | **unsigned-8** | none | `packvu_8_24_8` | `[HIGH/OBSERVED]` |

> **GOTCHA — the no-`r` `packv*` family is F6-exclusive; do not schedule it anywhere else.** `nm | rg
> 'Opcode_ivp_packvnx48_Slot_.*_encode'` returns **exactly one** symbol — `Slot_f6_s1_ld_encode` — for all
> 17 no-`r` forms, vs **8** for the `r`-forms. The reason is operand arity: the no-`r` form's iclass
> `_args` lists a *second `vec` IN* (`opnd_ivp_sem_wvec_pack_vr`) where the `r`-form lists an `AR` scalar
> (`art`); F6 is the only wide format carrying the extra `Ld`/vec read port that second `vec` source needs
> ([flix §2](../core/flix-encoding.md#2-the-14-formats--selector-length-slot-composition):
> `F6 = LdSt · Ld · Mul · ALU`). A reimplementer's bundler **must** place a vector-shift pack into F6 or
> the bundle is unencodable. `[HIGH/OBSERVED]`

### 2.4 `packl*` / `packm*` / `packp* / packq* / packh*` — word-select extracts (no shift, no sat)

These are the **raw word-select** readouts: no de-scale, no saturate — just pull a chosen 8/16/32-bit slice
out of the wide acc lane. They are the "I already scaled; give me the bits" path, and the building block of
the two-step full-width (`_0`/`_1`) readouts.

| mnemonic | fmt·slot | opcode-sel imm (F6·s1) | wvec→vec | extract | leaf | conf |
|---|---|---|---|---|---|---|
| `ivp_packl2nx24` | 8 slots · s1_ld | `0x0033b050` | 24 → 8 | low byte | `packl_8_24` | `[HIGH/OBSERVED]` |
| `ivp_packl2nx24_1` | 8 slots · s1_ld | `0x0033a051` | 24 → 8 | high-half byte | `packl_8_24` | `[HIGH/OBSERVED]` |
| `ivp_packlnx48` | 8 slots · s1_ld | `0x0033b051` | 48 → 16 | low 16 | `packl_16_48` | `[HIGH/OBSERVED]` |
| `ivp_packmnx48` | 8 slots · s1_ld | `0x0033b060` | 48 → 16 | **mid** word (`>>16`) | `packm_16_48` | `[HIGH/OBSERVED]` |
| `ivp_packpnx48` | 8 slots · s1_ld | `0x0033a061` | 48 → 16 | partial-packed | `packp_16_48` | `[HIGH/OBSERVED]` |
| `ivp_packqnx48` | 8 slots · s1_ld | `0x0033b061` | 48 → 16 | quad-packed | `packq_16_48` | `[HIGH/OBSERVED]` |
| `ivp_packln_2x96` | 8 slots · s1_ld | `0x0033a060` | 96 → 32 | low 32 | `packl_32_96` | `[HIGH/OBSERVED]` |
| `ivp_packhn_2x64w` | 8 slots · s1_ld | `0x0033a050` | 96 → 32 | **high** word | `packm_32_96`/`packh` | `[MED/OBSERVED]` |

> **NOTE — `l`/`m`/`h` select *word position* in the wide lane, not sign.** For the 96-bit (`2x96`/`2x64w`)
> acc lane, `packl_32_96` returns word0 (`mov (%rsi)`), `packm_32_96` returns word1 (`mov 0x4(%rsi)`), and
> the `h`/`packhn` form returns the top word — the 96-bit lane is `{w0, w1, w2}` and these three select
> among them. For 48-bit lanes, `packl_16_48` is the low 16, `packm_16_48` is `>>16`. The `__1`/`_1`
> variants do a cross-word bit-shuffle (`packl_16_48__1`: `shl $0x8` the high half | `shr $0x18` the low
> half) to assemble the *upper* lanes of a two-output readout. `[HIGH/OBSERVED]`

> **QUIRK — `ivp_mulnx16packl` and friends are NOT in B10.** Four `mul*packl` mnemonics
> (`ivp_mulnx16packl`, `ivp_mulanx16packl`, `ivp_mulsnx16packl`, `ivp_mulanx16packlt`) *also* carry the
> `packl` token, but they start with `mul`, not `pack`. They are a **fused multiply-then-low-pack** that
> writes `vec` directly without ever touching `wvec` — squarely [B04](b04-mac-integer.md) (its
> [§2.4 "pack-low"](b04-mac-integer.md#24-immediate-scalar-sign-multiply-and-pack-low)). The B10
> classifier is `name root starts "pack"`, so it excludes them; `nm | rg 'module__xdref_mul.*packl'` =
> 3 leaves (`mul/mula/muls_16_16_16_packl`) that belong to B04's value tally, not B10's 56. `[HIGH/OBSERVED]`

---

## 3. Encoding — where pack lives in the FLIX grid

### 3.1 Pack is a Ld-slot (`s1`) opcode, not a Mul-slot opcode

Every `ivp_pack*` placement resolves to an `s1_ld` slot. Querying the symtab:

```
nm libisa-core.so | rg 'Opcode_ivp_packvrnx48_Slot_.*_encode' | rg -o 'Slot_[a-z0-9_]+'
  →  Slot_f0_s1_ld  Slot_f1_s1_ld  Slot_f2_s1_ld  Slot_f3_s1_ld
     Slot_f4_s1_ld  Slot_f6_s1_ld  Slot_f7_s1_ld  Slot_n2_s1_ld     (8)
```

This is the architectural counterpart of the MAC: B04/B05 *write* into `wvec` from the **Mul slot `s2`** at
stage 12; B10 *reads* `wvec` back out from the **Ld slot `s1`**, also landing the narrowed result in `vec`
at stage 12. The two never collide — a pack can co-issue with an independent MAC in the same wide bundle
(pack in `s1`, MAC in `s2`), so a software pipeline can drain accumulator *k* while still filling
accumulator *k+1*. The `libcas-core.so` schedule confirms the slot/stage: `F0_F0_S1_Ld_16_IVP_PACKVRNX48_inst_stage{0..12}`
(13 stage functions; result `@stage12`). `[HIGH/OBSERVED]`

> **GOTCHA — pack rides `s1_ld`; the *unpack* (raw move) rides `s2_mul`.** It is tempting to assume the
> readout shares the MAC's Mul slot. It does not: the *narrowing* pack is a Ld-slot op
> (`Opcode_ivp_pack*_Slot_*_s1_ld_encode`), whereas the *non-narrowing*
> [unpack/wvec-move](b22-unpack-wvec-mov.md) (`ivp_sem_unpack_wvec_mov_*`) rides `s2_mul`
> ([register-files §6.2](../core/register-files.md#62-vec--wvec--pack-write-into-the-accumulator-and-unpack-read-it-back-out)).
> They are *different* ops in *different* slots; B10 owns only the Ld-slot narrowing pack. `[HIGH/OBSERVED]`

### 3.2 The bimodal placement — 8 slots (`r`) vs 1 slot (F6-only)

The 217 placements split cleanly:

```
8-placement mnemonics: 25  (the AR-shift "r" forms + the shiftless packl/m/p/q/h extracts)
1-placement mnemonics: 17  (the per-lane-vector-shift no-"r" packv* forms — F6 only)
                          25×8 + 17×1 = 200 + 17 = 217  ✓  (= num_encode_fns contribution)
```

The 8-slot forms occupy `{F0,F1,F2,F3,F4,F6,F7,N2}·s1_ld` — every wide format's Ld slot plus the narrow
`N2`. The 1-slot forms occupy **only F6** for the operand-arity reason of
[§2.3](#23-packv-no-r--per-lane-vector-shift-f6-only-17-mnemonics-1-placement-each). `[HIGH/OBSERVED]`

### 3.3 The operand model — `art` shift vs vector shift

The iclass `_args` table (a `(operand-name-ptr, direction)` array in `.data.rel.ro`, file = VMA − `0x200000`)
is the authoritative operand model. Read byte-exact this pass for three representative iclasses:

```
Iclass_IVP_PACKVRNX48_args   @0x845da0:                  # the AR-shift round+sat readout
   [0] opnd_ivp_sem_wvec_pack_vt    dir='o'   # OUT — vec destination (Nx16)
   [1] opnd_ivp_sem_wvec_pack_wvr   dir='i'   # IN  — wvec accumulator source (Nx48)
   [2] art                          dir='i'   # IN  — AR scalar = right-shift / round amount
   [3] CPENABLE                     dir='i'   # IN  — coprocessor-enable state

Iclass_IVP_PACKVNX48_args    @0x841900:                  # the vector-shift no-round readout (F6-only)
   [0] opnd_ivp_sem_wvec_pack_vt    dir='o'   # OUT — vec destination
   [1] opnd_ivp_sem_wvec_pack_wvr   dir='i'   # IN  — wvec accumulator source
   [2] opnd_ivp_sem_wvec_pack_vr    dir='i'   # IN  — *vec* per-lane shift (the extra read → F6 only)
   [3] CPENABLE                     dir='i'

Iclass_IVP_PACKLNX48_args    @0x845fe0:                  # the shiftless low-word extract
   [0] opnd_ivp_sem_wvec_pack_vt    dir='o'   # OUT — vec destination
   [1] opnd_ivp_sem_wvec_pack_wvr   dir='i'   # IN  — wvec accumulator source
   [2] CPENABLE                     dir='i'                # — no shift operand at all
```

So the three structural classes are exactly: **AR-shift** (`art` operand → 8 placements), **vector-shift**
(`..._vr` operand → F6-only), and **no-shift** (extract → 8 placements). The `vt`/`wvr` direction bytes are
literally the ASCII `'o'` (`0x6f`, out) and `'i'` (`0x69`, in) — the same per-operand direction encoding the
[register-files page](../core/register-files.md#6-cross-file-bridge--move-instructions) documents for cross-file
bridges. `[HIGH/OBSERVED]`

### 3.4 The opcode-selector templates (byte-exact `F6_S1_Ld` `WORD0`)

Read from the encode thunks this pass (`.text`, VMA == file; the universal `C7 07 imm32 C3` ABI). The
templates cluster by sub-family — the `0x33xxxxxx` band is the extract family, `0x003xxxxx` the saturating-vec
family:

```
ivp_packvrnx48        @0x35e670  WORD0 = 0x00320000      # AR round+sat, 48→16
ivp_packvrnx48_0      @0x360620  WORD0 = 0x00320001      # +1 = the _0 half-select bit
ivp_packvrnrnx48      @0x360820  WORD0 = 0x00300000      # AR round, no-sat
ivp_packvr2nx24       @0x35e0f0  WORD0 = 0x000c1000      # AR round+sat, 24→8
ivp_packvru2nx24      @0x35e170  WORD0 = 0x00340000      # AR round, unsigned-sat, 24→8
ivp_packvnx48         @0x3634e0  WORD0 = 0x003dd000      # VEC shift, signed-sat (F6 only)
ivp_packvnrnx48       @0x363510  WORD0 = 0x003d5000      # VEC shift, no-sat   (F6 only)
ivp_packvu2nx24       @0x3635a0  WORD0 = 0x00322000      # VEC shift, unsigned-sat (F6 only)
ivp_packl2nx24        @0x35e070  WORD0 = 0x0033b050      # low-byte extract
ivp_packmnx48         @0x35e5f0  WORD0 = 0x0033b060      # mid-word extract
```

Note the `_0`/`_1` two-half variants differ from the base form by exactly the low selector bit
(`packvrnx48 = 0x00320000` → `packvrnx48_0 = 0x00320001`); the `WORD1` upper lane is `0x00000000` as for
every placement ([template §3.1 NOTE](template-and-partition.md#3-the-canonical-per-instruction-page-template-the-b01b30-schema)).
The full `(mnemonic, slot) → WORD0` table is per-format (the same mnemonic packs differently in F0 vs F6),
so a reimplementer's assembler carries the table; it cannot synthesize one format from another. `[HIGH/OBSERVED]`

---

## 4. The pack pipeline — annotated C against the binary

The per-lane pack is a four-stage pipeline, reconstructed from the executed `xdref` leaf bodies and the
`libcas-core` schedule: **(1) clamp the shift, (2) add the round bias, (3) arithmetic-right-shift the wide
lane, (4) saturate to the narrow range and write the narrow lane.** This is the textbook fixed-point
requantize.

```c
// One lane of a wvec pack: ivp_pack{v,vr,vu,vru}<...>  (W_in = acc lane 24/48/96; W_out = vec lane 8/16/32)
// Source read from wvec @stage12; narrowed result written to vec @stage12 (the s1_ld readout).
// shift = AR scalar  (r-forms)  or  per-lane vec value (no-r forms).
typedef  int_t<W_in>   acc_t;    // 24 / 48 / 96-bit wvec lane (two's complement)
typedef  int_t<W_out>  out_t;    // 8 / 16 / 32-bit vec lane

out_t pack_lane(acc_t acc, int shift, pack_mode mode)
{
    // (1) CLAMP the shift amount.  An out-of-range shift is clamped, not wrapped:
    //     8-bit out  -> shift <= 23 (module__xdref_shift_amt_satu_23, cmova $0x17)
    //     16/32 out  -> shift <= 32 (module__xdref_shift_amt_satu_32)
    shift = (shift > SHIFT_MAX) ? SHIFT_MAX : shift;          // §4.2

    // (2) ROUND bias — round-half-up.  Add 0.5 ULP at the shift boundary BEFORE the shift.
    //     bias = (shift == 0) ? 0 : (1 << (shift - 1)).  The r-forms add it; the nr-forms skip it.
    acc_t biased = acc;
    if (mode.round && shift != 0)
        biased = acc + ((acc_t)1 << (shift - 1));             // §5.2 — ties resolve toward +inf

    // (3) ARITHMETIC right-shift (sign-preserving) by the de-scale amount.
    acc_t shifted = biased >> shift;     // ASR — replicates the sign bit, not a logical shift

    // (4) SATURATE to the narrow range and narrow.  Three modes, proven by execution (§5.1):
    switch (mode.sat) {
      case SIGNED:    // packv* / packvr*   -> clamp to [ -(2^(W_out-1)) , 2^(W_out-1)-1 ]
        if (shifted >  SMAX) return SMAX;                     // 0x7f / 0x7fff / 0x7fffffff
        if (shifted <  SMIN) return SMIN;                     // 0x80 / 0x8000 / 0x80000000
        return (out_t)shifted;
      case UNSIGNED:  // packvu* / packvru* -> clamp to [ 0 , 2^W_out - 1 ]
        if (shifted < 0)     return 0;                        // negatives clamp to 0
        if (shifted > UMAX)  return UMAX;                     // 0xff / 0xffff / 0xffffffff
        return (out_t)shifted;
      case NONE:      // packvnr* / packvrnr* / packl* -> NO clamp; the result WRAPS mod 2^W_out
        return (out_t)shifted;                                // low W_out bits kept, may overflow
    }
}
```

The `packl*`/`packm*`/`packh*` extracts are step (4)`NONE` with `shift = 0` and a fixed word offset (low /
`>>16` / high) — pure slice, no arithmetic. `[HIGH/OBSERVED]` on the four-stage structure; the rounding
mode, saturation mode, and shift clamp are each **proven by execution** in [§5](#5-value-semantics--driven-live).

### 4.1 The wide lane is assembled from 32-bit half-words

`wvec` lanes are wider than a host register, so the leaf reads them as a little array of `uint32` words
(`(%rsi)` = word0, `0x4(%rsi)` = word1, …) and re-assembles the 48-/96-bit value across two/three words
before the shift. `packvr_16_48_32` builds the 48-bit lane as `lo32 | (sext16(hi16) << 32)` (the
`movswl 0x4(%rsi)` sign-extends the top half); `packl_16_48__1` instead does the cross-word
`(hi16 << 8) | (lo32 >> 24)` shuffle to extract the *upper* output lane. A reimplementer that models the
wvec lane as a flat `int_t<3L>` and shifts it gets the same answer; the half-word assembly is a host-ABI
detail of the simulator leaf, not architectural. `[HIGH/OBSERVED]`

### 4.2 The shift amount is clamped before use

The shift is **clamped, not masked**, before it is applied — an over-large shift saturates the shift itself.
Two helper leaves do this, selected by output width:

```asm
00000000080edd0 <module__xdref_shift_amt_satu_23>:   ; 8-bit out  -> max shift 23
  80edd0: 83 fe 17        cmp    $0x17,%esi            ;  if (shift > 23)
  80edd3: b8 17 00 00 00  mov    $0x17,%eax
  80edd8: 0f 47 f0        cmova  %eax,%esi             ;     shift = 23
  80eddb: 89 32           mov    %esi,(%rdx)           ;  *out = shift
  80eddd: c3              ret
```

`module__xdref_shift_amt_satu_32` is the same with `cmp $0x20` (max 32) for 16/32-bit outputs. The bound is
exactly the acc-lane width minus one output-lane-byte (`24−1 = 23`), so a clamp never reads past the lane.
`[HIGH/OBSERVED]`

---

## 5. Value semantics — driven LIVE

`libfiss-base.so` is **callable in-process via ctypes with no license**
([coverage-tally §5](../core/coverage-tally.md#5-value-semantics-coverage--the-864864-execution-validated-cover)),
so every claim below is **proven-by-execution**, not decoded-and-guessed. The leaf SysV ABI, recovered from
`objdump -d`: `rdi` = unused context; then the wide-acc value (by register for the 24-bit lane, by pointer
for 48/96-bit lanes); then the shift (`edx`); the last pointer (`rcx`) is the narrow output. The three pack
modes were driven on the *same* inputs to prove they differ exactly as the disassembly predicts.

### 5.1 Saturation — signed, unsigned, and the no-sat truncate

Driving `packv_8_24_8` (signed-8) and `packvru_8_24_32` (unsigned-8) live, ctypes, raw 24-bit acc inputs:

```
packv_8_24_8   (SIGNED-8 saturate):
   acc=+100   shift=0  -> 0x64  (+100)    in range, kept
   acc=+200   shift=0  -> 0x7f  (+127)    > +127 -> clamp to SMAX            ← saturates high
   acc=max24  shift=0  -> 0x7f  (+127)    huge   -> clamp to SMAX
   acc=-129   shift=0  -> 0x80  (-128)    < -128 -> clamp to SMIN            ← saturates low
   acc=-200   shift=0  -> 0x80  (-128)

packvru_8_24_32 (UNSIGNED-8 saturate):
   acc=+100   shift=0  -> 100              in range, kept
   acc=+300   shift=0  -> 255  (0xff)      > 255 -> clamp to UMAX            ← saturates high
   acc=-1     shift=0  -> 0                negative -> clamp to 0            ← unsigned floor
   acc=-1000  shift=0  -> 0
```

Signed clamps to `[-128, +127]`; unsigned clamps to `[0, 255]` (negatives become 0). Both are **proven**,
including the edges. `[HIGH/OBSERVED by execution]`

### 5.2 Rounding — round-half-up, proven on half-way inputs

The decisive rounding test is a value whose post-shift fraction is **exactly 0.5**. Driving `packv_8_24_8`
(signed) at `shift=4`:

```
packv_8_24_8, shift=4:
   acc=8   ->  8 >> 4 = 0.5  -> out=1     (0.5 rounds UP to 1)        ← not 0 (round-to-even would give 0)
   acc=24  -> 24 >> 4 = 1.5  -> out=2     (1.5 rounds UP to 2)        ← not 2-to-even-2 coincidence...
   acc=16  -> 16 >> 4 = 1.0  -> out=1     (exact, no rounding)
```

and `packvr_16_48_32` (signed-16) at `shift=1` over consecutive odds (each is `n/2` with fraction 0.5):

```
packvr_16_48_32, shift=1:   acc=1->1   acc=3->2   acc=5->3   acc=7->4   acc=9->5    (= ceil(n/2))
```

Every 0.5-fraction rounds **toward +∞** (up), never to-even — this is **round-half-up**, implemented by the
`+(1<<(shift-1))` bias of [§4](#4-the-pack-pipeline--annotated-c-against-the-binary) (the `movl $0x1,…; shl
%cl; shr $1` idiom in `packvr`). A reimplementer that models round-to-nearest-even would mismatch on every
tie. `[HIGH/OBSERVED by execution]`

### 5.3 Round-vs-truncate, and no-saturate — the `nr` forms

Driving the round (`packvr_16_48_32`) and no-round (`packvnr_16_48_16`) forms on **identical** inputs proves
both axes at once — `nr` drops *both* the round bias *and* the saturate:

```
shift=1, half-way (0.5-fraction) inputs:
   acc   packvr (round+sat)   packvnr (no-round, no-sat)
    1          1                    0          ← round: 0.5->1   ;  trunc: 0.5->0
    3          2                    1          ← round: 1.5->2   ;  trunc: 1.5->1
    5          3                    2
    7          4                    3
    9          5                    4          (packvr = ceil(n/2), packvnr = floor(n/2))

no-saturate proof (shift=0, out-of-range acc):
   acc=40000     packvnr16 = 0x9c40 (-25536)   vs   packvr16 = 0x7fff (+32767)
   acc=74565     packvnr16 = 0x2345 (+9029)    vs   packvr16 = 0x7fff
   acc=-50000    packvnr16 = 0x3cb0 (+15536)   vs   packvr16 = 0x8000 (-32768)
```

`packvnr` **wraps mod 2^16** (the low 16 bits survive, the value overflows silently), while `packvr`
**clamps**. So the `nr` token = *no-round AND no-saturate* = the **raw arithmetic-shift-and-narrow**; the
`r`/default = *round + signed-saturate*. This is the single most important reimplementation distinction in
the batch: choosing `packvnr` where you meant `packvr` produces silent overflow garbage instead of a clamped
result. `[HIGH/OBSERVED by execution]`

### 5.4 The de-scale (Q-format right-shift), proven

The shift is the Q-format de-scale: a MAC that accumulated `Q_a × Q_b` products carries `frac(a)+frac(b)`
fractional bits, and the pack shift strips them back to the output Q-format. Proven on `packvr_16_48_32`:

```
packvr_16_48_32:  acc=100  shift=1 -> 50      (100/2, rounded)
                  acc=768  shift=9 -> 2        (768/512 = 1.5 -> round-half-up -> 2)
                  acc=256  shift=9 -> 1        (256/512 = 0.5 -> round-half-up -> 1)
```

The right-shift is **arithmetic** (sign-preserving): a negative acc shifted right stays negative
(`-50000 >> 0` with sat → `-32768`, and the unsaturated `packvnr` keeps the sign through the shift). A
reimplementer models `(acc + bias) >> shift` as a C arithmetic shift on a signed type. `[HIGH/OBSERVED by
execution]`

---

## 6. The mnemonic ↔ value-leaf join (by width and token)

As with the MAC batches ([B04 §6](b04-mac-integer.md#6-the-mnemonic--value-leaf-join)), **the encode
mnemonic and the value leaf are joined by `(pack-mode-token, acc-width, out-width, shift-width)`, NOT by the
literal name.** `ivp_packvrnx48` has no `xdref_packvrnx48` leaf — its semantics live in
`module__xdref_packvr_16_48_32` (the width-signed name: `packvr` mode, `16` out-bits, `48` acc-bits, `32`
shift-bits). The join, verified by matching widths and executing:

| encode mnemonic (`libisa-core`) | value leaf (`libfiss-base`) | join key |
|---|---|---|
| `ivp_packvrnx48` | `module__xdref_packvr_16_48_32` | round+signed-sat, 48→16, AR shift |
| `ivp_packvr2nx24` | `module__xdref_packvr_8_24_32` | round+signed-sat, 24→8, AR shift |
| `ivp_packvrn_2x64w` | `module__xdref_packvr_32_96_32` | round+signed-sat, 96→32 |
| `ivp_packvru2nx24` | `module__xdref_packvru_8_24_32` | round+**unsigned**-sat, 24→8 |
| `ivp_packvnx48` | `module__xdref_packv_16_48_16` | signed-sat, 48→16, **vec** shift |
| `ivp_packvu2nx24` | `module__xdref_packvu_8_24_8` | **unsigned**-sat, 24→8, vec shift |
| `ivp_packvnrnx48` | `module__xdref_packvnr_16_48_16` | **no-round, no-sat**, 48→16 |
| `ivp_packvrnrnx48` | `module__xdref_packvrnr_16_48_32` | round, **no-sat**, 48→16, AR shift |
| `ivp_packlnx48` | `module__xdref_packl_16_48` | low-word extract, 48→16 |
| `ivp_packmnx48` | `module__xdref_packm_16_48` | mid-word extract, 48→16 |
| `ivp_packvrnx48_0` / `_1` | `module__xdref_packvr_16_48_32__0` / `__1` | two-half full-width readout |

> **GOTCHA — the `_<out>_<acc>_<shift>` suffix is three numbers, and the third is the shift-operand width,
> not a third value.** `packvr_16_48_32` is `out=16, acc=48, shift=32` — the trailing `32` is the *AR-shift
> operand bit-width* (a 32-bit `art` scalar), NOT a 32-bit lane. Contrast `packv_16_48_16` (the vector-shift
> form) whose third field `16` echoes the output width because its shift rides a 16-bit `vec` lane. A join
> that reads the third number as a lane width mis-types the op. The `_w` suffix (`packv_32_96_32_w`) marks
> the wide 96→32 form; the `__0`/`__1` suffix marks the two-half readout. `[HIGH/OBSERVED]`

---

## 7. Per-batch coverage tally

The batch's contribution to the [roll-up](template-and-partition.md#6-the-roll-up-coverage-model), each
number with a binary witness:

| quantity | value | witness |
|---|--:|---|
| B10 mnemonics `m` | **42** | `nm libisa-core.so \| rg -o 'Opcode_(ivp_pack[a-z0-9_]*)_Slot_' \| sort -u \| wc -l` |
| B10 placements `p` | **217** | `nm \| rg -c 'Opcode_ivp_pack[a-z0-9_]*_Slot_.*_encode'` = `25×8 + 17×1` |
| B10 value leaves `v` | **56** | `nm libfiss-base.so \| rg 'module__xdref_pack' \| rg -vc 'mul'` (`packl`5+`packm`2+`packp`2+`packq`2+`packv`9+`packvnr`9+`packvr`9+`packvrnr`9+`packvru`4+`packvu`4 + bare `pack`1) |

`m = 42` lands inside the template's `≈42` target for B10 *exactly*, and rolls into the **1065** `ivp_`
vector axis ([template §6.1](template-and-partition.md#61-the-two-axes-sum-independently)); `p = 217` rolls
into the **12569** placement cover, never the pre-fold 12642 (pack touches no fold-source package). The 56
value leaves roll into the **864** cover; they are *fewer* than the 42 mnemonics' placements because one
leaf serves a whole format-family (e.g. `packvr_16_48_32` underlies `packvrnx48`, `_0`, `_1` across all 8
slots). `[HIGH/OBSERVED]`

---

## 8. Boundaries — what B10 does NOT own

| neighbour | what it owns | how to tell apart |
|---|---|---|
| [B04](b04-mac-integer.md) / [B05](b05-mac-mixed.md) | the MAC that **fills** `wvec` (`mul*`/`mula*`/`muls*`/`mulus*`/…) | writes `wvec` from `s2_mul`; root verb is `mul`, not `pack` |
| [B04 §2.4](b04-mac-integer.md#24-immediate-scalar-sign-multiply-and-pack-low) | the fused `mul*packl` low-pack (writes `vec` directly) | root starts `mul`; classifier excludes from B10 ([§2.4 QUIRK](#24-packl--packm--packp-packq--packh--word-select-extracts-no-shift-no-sat)) |
| [B22](b22-unpack-wvec-mov.md) | the **non-narrowing** unpack / `wvec`-move (`ivp_sem_unpack_wvec_mov_*`, `ivp_movww`) | rides `s2_mul`, copies the raw accumulator without shift/round/sat |
| [B13](b13-sp-cvt.md) / [B20](b20-hp-cvt.md) | plain `vec`→`vec` **dtype conversion** (`cvt*`) | source is `vec`, not `wvec`; verb is `cvt`, not `pack` |

The defining property a reimplementer keys on: **a B10 op reads `wvec` (the `opnd_ivp_sem_wvec_pack_wvr`
IN operand) and writes `vec` with narrowing.** If it reads `vec` it is a convert (B13/B20); if it reads
`wvec` without narrowing it is an unpack (B22); if it writes `wvec` it is a MAC (B04/B05). `[HIGH/OBSERVED]`

---

## 9. Adversarial self-verification — 5 strongest claims, re-challenged

Each claim re-derived against the binary this pass; nothing taken on a report's word.

1. **"42 `ivp_pack*` mnemonics, 217 placements, split 25×8 + 17×1."** *Challenge:* did the `mul*packl`
   forms leak in, or did the count include the `_0`/`_1` halves twice? Re-derived: `rg -o
   'Opcode_(ivp_pack[a-z0-9_]*)_Slot_'` over `nm` (root starts `pack`, so the 4 `mul*packl` are excluded) =
   **42** distinct; per-mnemonic `rg -c` sums to **217**; the bimodal histogram is exactly **25** mnemonics
   with 8 placements and **17** with 1 (`25×8 + 17 = 217`). The `_0`/`_1` halves are *distinct* mnemonics
   with their own thunks, correctly counted once each. **Confirmed.** `[HIGH/OBSERVED]`

2. **"Pack rides `s1_ld`, not `s2_mul`."** *Challenge:* maybe some pack forms ride the Mul slot like the
   MAC. `nm | rg 'Opcode_ivp_pack.*_Slot_.*_encode' | rg -o 'Slot_[a-z0-9_]+' | sort -u` returns **only**
   `s1_ld` slots (`f0..f7_s1_ld`, `n2_s1_ld`) — **zero** `s2_mul`. The `s2_mul` symbols are the *unpack*
   (`ivp_sem_unpack_wvec_mov_*`, B22). Pack is Ld-slot exclusively. **Confirmed.** `[HIGH/OBSERVED]`

3. **"Rounding is round-half-up, not round-to-even."** *Challenge:* the executed ties might be coincidence.
   Re-run on a *clean* 0.5 fraction with no even-coincidence: `packv_8_24_8(acc=8, shift=4)` → `8>>4 = 0.5`
   → **1** (round-to-even would give **0**, the nearest even). And `packvr_16_48_32(acc=1, shift=1)` →
   `0.5` → **1** (to-even = 0). Both round **up**. The bias term `1<<(shift-1)` in the disassembly confirms
   the mechanism. Round-to-even is **refuted**. **Confirmed.** `[HIGH/OBSERVED by execution]`

4. **"`packvnr` is no-round AND no-saturate (wraps); `packvr` is round + signed-saturate."** *Challenge:*
   maybe `nr` only drops the round but still saturates. Re-run on out-of-range acc: `packvnr_16_48_16(40000,
   0)` → `0x9c40 (-25536)` — the **modular wrap** of 40000 mod 2^16, **not** `0x7fff` (which a saturator
   returns, and which `packvr` *does* return on the same input). So `nr` drops both. Saturation in `nr` is
   **refuted**. **Confirmed.** `[HIGH/OBSERVED by execution]`

5. **"The no-`r` `packv*` forms are F6-exclusive because they take a per-lane `vec` shift."** *Challenge:*
   maybe they place elsewhere too, or the operand is really an AR. `nm | rg
   'Opcode_ivp_packvnx48_Slot_.*_encode'` returns **exactly one** symbol (`Slot_f6_s1_ld_encode`), and the
   iclass `Iclass_IVP_PACKVNX48_args` operand 2 is `opnd_ivp_sem_wvec_pack_vr` (a `vec` IN), whereas the
   `r`-form `Iclass_IVP_PACKVRNX48_args` operand 2 is `art` (an AR IN). The extra `vec` read needs F6's
   spare Ld port. Both the single-placement count and the operand-model difference confirm it. **Confirmed.**
   `[HIGH/OBSERVED]`

**What I could not ground to OBSERVED.** The exact word-position of `ivp_packhn_2x64w`'s "high" extract is
`[MED/OBSERVED]` — the leaf reads `module__xdref_packm_32_96`/a high-word slice, but the `2x64w` lane-tiling
(whether "high" is word1 or word2 of the 96-bit lane) is inferred from the `m`/`h` token semantics, not
separately executed end-to-end. The `p`/`q` extract variants' exact bit-shuffle is `[HIGH/OBSERVED]` as
leaves but their *use* (which GEMM tiling emits them) is `[MED/INFERRED]`.

---

## 10. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| 42 `ivp_pack*` mnemonics; 217 placements (25×8 + 17×1); 56 value leaves | `[HIGH/OBSERVED]` | `nm libisa-core.so`/`libfiss-base.so` rosters + per-mnemonic `rg -c` |
| Pack is a `s1_ld`-slot op (never `s2_mul`); unpack is `s2_mul` (B22) | `[HIGH/OBSERVED]` | `Opcode_ivp_pack*_Slot_*_s1_ld_encode` symtab; `ivp_sem_unpack_wvec_mov_*` on `s2_mul` |
| Operand model `vt`(out)/`wvr`(in)/`art`-or-`vr`(in)/`CPENABLE`(in) | `[HIGH/OBSERVED]` | `Iclass_IVP_PACK*_args` tables (`.data.rel.ro`, delta `0x200000`) |
| AR-shift (`r`) forms = 8 placements; vector-shift (no-`r`) forms = F6-only | `[HIGH/OBSERVED]` | one placement each = `Slot_f6_s1_ld_encode`; operand-arity difference |
| Opcode-sel `WORD0` templates (byte-exact `F6_S1_Ld`) | `[HIGH/OBSERVED]` | `objdump -d` of the encode thunks this pass |
| Pipeline: clamp → round-bias → ASR → saturate; result `@stage12` | `[HIGH/OBSERVED]` | executed `xdref` leaves + `libcas` `F0_F0_S1_Ld_*_PACK*_inst_stage{0..12}` |
| Round-half-up (ties toward +∞), proven on 0.5-fraction inputs | `[HIGH/OBSERVED by execution]` | ctypes `packv_8_24_8`/`packvr_16_48_32` on half-way values |
| Signed-sat (`packv*`/`packvr*`), unsigned-sat (`packvu*`/`packvru*`), no-sat wrap (`packvnr*`/`packl*`) | `[HIGH/OBSERVED by execution]` | ctypes clamp-vs-wrap on out-of-range acc |
| `nr` = no-round AND no-saturate (raw arithmetic-shift extract) | `[HIGH/OBSERVED by execution]` | `packvnr` truncates + wraps where `packvr` rounds + clamps, same inputs |
| Shift clamped (≤23 for 8-bit out, ≤32 for 16/32-bit) before use | `[HIGH/OBSERVED]` | `module__xdref_shift_amt_satu_{23,32}` (`cmova $0x17`/`$0x20`) |
| `mul*packl` belong to B04, not B10 (write `vec`, root `mul`) | `[HIGH/OBSERVED]` | classifier root-starts-`pack`; `mul*packl` excluded |
| Mnemonic↔leaf join by `(mode,out,acc,shift)` signature, not name | `[HIGH/OBSERVED]` | no `xdref_packvrnx48` leaf; all resolve by width-token |
| `packhn` high-word position within the 96-bit lane | `[MED/OBSERVED]` | leaf is a high-word slice; lane-tiling inferred from `m`/`h` token |

---

## 11. Cross-references

- [The Eight Register Files](../core/register-files.md) — the `wvec` (1536-bit accumulator, source) /
  `vec` (512-bit, destination) geometry, the `(12,12)` accumulate, and the
  [§6.2 pack/unpack bridge](../core/register-files.md#62-vec--wvec--pack-write-into-the-accumulator-and-unpack-read-it-back-out)
  this page expands.
- [ISA Batch 04 — Integer MAC Matrix (signed)](b04-mac-integer.md) — the signed MAC that **fills** the
  `wvec` lanes B10 reads out; its [§8](b04-mac-integer.md#8-readout-the-wvecvec-pack-cross-link-to-b10)
  points forward here. Lane geometry 8→24/16→48/32→96 is the inverse of B10's 24→8/48→16/96→32.
- [ISA Batch 05 — MAC (mixed-sign/complex/wide-acc)](b05-mac-mixed.md) — the unsigned/mixed/complex MAC
  that fills `wvec`; the `mulus*` (`uint8` activation × `int8` weight) accumulations are the dominant
  source a `packvru*` (unsigned-sat) or `packvr*` (signed-sat) readout drains.
- [ISA Batch 22 — Unpack / wvec move](b22-unpack-wvec-mov.md) — the **non-narrowing** counterpart
  (`ivp_sem_unpack_wvec_mov_*` on `s2_mul`, `ivp_movww` spill); the B10/B22 boundary is
  [§8](#8-boundaries--what-b10-does-not-own).
- [The FLIX VLIW Encoding (14 format / 46 slot)](../core/flix-encoding.md) — the Ld slot (`s1`) these ops
  occupy, the F6 wide format (`LdSt · Ld · Mul · ALU`) the vector-shift forms require, and the encode-thunk
  `WORD0` ABI.
- [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the schema this page follows,
  the `≈42` B10 partition target, and the `1534 ↔ 12569` roll-up the 42/217 tally closes onto.
- [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the certified `12569` placement /
  `864` value-leaf denominators this batch's `217` / `56` contribute to.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED`/`INFERRED`/`CARRIED`
  tags and the proven-by-execution value lane used throughout [§5](#5-value-semantics--driven-live).

---

*Provenance: the encode templates, slot placements, and iclass operand-arg tables are `[HIGH/OBSERVED]` —
re-disassembled / `readelf`-dumped in-checkout from `libisa-core.so` (`ncore2gp/config/`, `.data.rel.ro`
delta `0x200000`). The value semantics in [§5](#5-value-semantics--driven-live) are `[HIGH/OBSERVED by
execution]` — the `libfiss-base.so` `xdref` pack leaves were loaded via ctypes and run on the inputs shown
(saturation edges, 0.5-fraction rounding ties, no-sat wrap). The pipeline stages are `[HIGH/OBSERVED]` from
`libcas-core.so` `F0_F0_S1_Ld_*_IVP_PACK*_inst_stage*` symbol tags. The `extracted/` carving is gitignored;
counts are `nm | rg -c` against the binary `.symtab`. All prose reads as derived from shipped-artifact static
analysis (lawful interoperability RE).*
