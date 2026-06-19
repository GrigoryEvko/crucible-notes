# ISA Batch 21 — Select / Shuffle / Compress (the lane-routing crossbar)

This batch is the **arbitrary per-lane permutation network** of the Vision-Q7 *Cairo* (`ncore2gp`)
512-bit FLIX vector ISA: the family where every output lane is sourced from an **arbitrary input
lane** chosen by a per-lane control. It is the full lane crossbar that sits one level above the
*single-scalar-indexed* shape-changers of [B16 (vec_rep)](b16-vec-rep.md): where `rep`/`extr` route
**one** lane, the ops here route **all** lanes independently, in one issue. The batch owns the two
semantic groups `ivp_sem_vec_select` (**18** mnemonics) and `ivp_sem_vec_specialized_seli` (**6**
slot-pinned mnemonics) — **24 ops** total, a perfect non-overlapping cover (no member also appears in
the B19 scatter/gather or B20 hp-convert groups). It hosts five compute shapes:

* **SEL** — two-source `2N`-to-`N` crossbar: `vt[k] = {vr ++ vs}[ctrl[k]]`, control reaches *both*
  source vectors (the index is one bit wider than a shuffle's).
* **SHFL** — single-source `N`-to-`N` permute: `vt[k] = vr[ctrl[k]]`, reach is one register.
* **DSEL** — *dual-output* deal/zip: writes **two** result vectors (`vu`, `vt`) from the same source
  pair in one issue — a butterfly/de-interleave stage.
* **DCMPRS** — predicate-driven byte **EXPAND**: `out[k] = vbr[k] ? src[prefix-popcount of vbr] :
  src[63]` — the stream-compaction primitive.
* **SELi/SHFLi (`_S0/_S2/_S4`)** — the *slot-pinned* immediate forms: the same byte-permute compute,
  but locked into the LdSt / Mul / ALU2 FLIX slot so a `sel` in S3 and a byte-permute in another slot
  can co-issue in one bundle.

Everything below is re-grounded against the shipped binaries **this pass**: the **encoding** from
`libisa-core.so` (`Opcode_<mnem>_Slot_<slot>_encode` thunks read byte-for-byte; the `Iclass_IVP_<MNEM>_args`
operand-descriptor arrays and the `regfiles[]` table walked directly), the **value semantics** by
*executing* the matching `module__xdref_*` leaves in `libfiss-base.so` live in-process, the **issue
timing** from the per-op `*_issue` scoreboard bodies in `libcas-core.so`, and a byte-exact
**encode/decode oracle** from the device-native `xtensa-elf-as`/`xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`). Every representative was round-tripped through that device oracle this pass.
Confidence tags per [the Confidence & Walls model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / proven-by-execution, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic re-confirmed this pass.** `libisa-core.so` (9 690 712 B, ET_DYN x86-64,
> not stripped; 45 198 symbols). `readelf -SW` this pass: `.text` (VMA `0x312c10`) and `.rodata` (VMA
> `0x3b6e40`) are **VMA == file-offset**; `.data` (VMA `0x764040` ↔ file `0x564040`) and `.data.rel.ro`
> (VMA `0x67bb00` ↔ file `0x47bb00`) carry the per-binary delta **`0x200000`** — **not** libtpu's
> `0x400000`, so `objdump -s -j .data` on the `Iclass_*_args` operand tables and the `regfiles[]`
> array must subtract `0x200000`. Encode thunks live in `.text` (VMA == file). All three config DLLs
> are under `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/` (gitignored; reach with
> `fd --no-ignore` or an absolute path). `[HIGH/OBSERVED]`

---

## 0. Scope boundary — the crossbar wall

The unifying property this batch owns is the **per-output-lane index vector**: a *different* source
lane may be chosen for *every* output lane. That is the structural test separating B21 from its
neighbours:

* **Single-scalar-indexed lane↔scalar transforms (`ivp_rep*`, `ivp_extr*`, `ivp_inj*`) → [B16](b16-vec-rep.md), not here.**
  A `rep` broadcasts *one* lane to all outputs; an `extr` pulls *one* lane to a scalar. There is no
  per-output index. The scalar-broadcast selects (`ivp_sels2nx8`/`selsnx16`/`selsn_2x32`) spell like
  this batch but ride the **`ivp_sem_vec_rep`** datapath (a single scalar index broadcasts one selected
  lane) and are owned by B16; they are cited here only as adjacency. `[HIGH/OBSERVED — membership]`
* **Memory-indexed access (`ivp_gather*`, `ivp_scatter*`) → B19, not here.** Those compute a per-lane
  *address* (`base + offset[lane]`) and hand it to the SuperGather host port; B21 ops route lanes
  *inside* the register file with no memory touch. The two rosters are disjoint (0 cross-membership,
  verified this pass). `[HIGH/OBSERVED]`
* **The cross-lane *fold* (`ivp_radd`/`rmax`/…) and the unaligned-access *funnel shift*
  (`ivp_lalign`/`malign`/…) → the reduce / valign families.** Those collapse or stream the vector;
  B21 *permutes* it. The shared S3-ALU lane-permute datapath this page documents is the crossbar leg;
  the reduce fold and valign funnel-shift are the
  [VALIGN / Shuffle-Select / Reduce ISS slice](../../iss/cas-valign-shuffle-reduce.md)'s other two
  legs. `[HIGH/CARRIED]`

The [partition classifier](template-and-partition.md) routes by the `sel|shfl|dsel|compr|zip` verb
glob; the `extr*` glob it also lists is **reclassified to B16** (the structural inverse of `rep`), as
that page's §0 states. The deeper RTL-level reference compute for these ops — the
`xdsem_tiesel_5_32` 5-bit 32-way lane mux and the `xdsem_bitkill` per-lane predicate kill — is the
[group-semantics-II](../semantics/group-semantics-ii.md) page's domain; this page is the per-opcode
encoding/operand/timing reference.

---

## 1. Roster and count verification `[HIGH/OBSERVED]`

The two groups are enumerated directly from the `libisa-core.so` symbol table (the encrypted TIE-XML
that carries the formal `<SEMANTIC>` membership is *not* consulted for counts — every mnemonic below
has at least one `Opcode_ivp_<mnem>_Slot_*_encode` thunk and one `Iclass_IVP_<MNEM>_args` descriptor;
the `_Slot_` anchor pins the mnemonic boundary so `sel2nx8` does not bleed into `sel2nx8t`/`sel2nx8i`):

**`ivp_sem_vec_select` (18):**
`SEL2NX8 SELNX16 SELN_2X32` · `SEL2NX8T SELNX16T SELN_2X32T` · `SEL2NX8I` ·
`DSEL2NX8I DSEL2NX8I_H DSELNX16 DSELN_2X32 DSELNX16T DSELN_2X32T` ·
`SHFL2NX8 SHFLNX16 SHFLN_2X32 SHFL2NX8I` · `DCMPRS2NX8`

**`ivp_sem_vec_specialized_seli` (6):**
`SEL2NX8I_S0 SEL2NX8I_S2 SEL2NX8I_S4` · `SHFL2NX8I_S0 SHFL2NX8I_S2 SHFL2NX8I_S4`

`18 + 6 = 24`. All 24 carry a thunk and an `Iclass_*_args`; 0 cross-membership with any other batch.

### 1.1 The lane-grid token decides element width

The mnemonic suffix names the lane grid (`512 / elemW = lane count`); permutation acts at **lane
granularity** — a result lane copies a whole input element:

| token | lanes | element |
|-------|-------|---------|
| `2NX8` | 64 | `i8` (byte-lane) |
| `NX16` | 32 | `i16` (the canonical 16-bit lane) |
| `N_2X32` | 16 | `i32` |

---

## 2. Common encoding model `[HIGH/OBSERVED]`

### 2.1 State / exception gate

Every op of this batch shares the IVP vector-coprocessor gate: `STATE_IN = CPENABLE` (sampled at the
early pipeline stage), `EXC = Coprocessor1Exception` (raised — and the op squashed before any datapath
effect — iff the cp1 enable bit is clear). This is the same gate the whole `ivp_` axis carries; it is
*not* re-listed per op below. `[HIGH/OBSERVED — ICLASS state args]`

### 2.2 Operand classes — read from the descriptor arrays

Each `Iclass_IVP_<MNEM>_args` symbol in `.data` is an array of **16-byte operand descriptors**,
`{const char* name; uint64 dir}` (the `dir` low byte is an ASCII code: `0x6f`=`'o'`=output,
`0x69`=`'i'`=input, `0x6d`=`'m'`=inout). Walked this pass (subtracting the `0x200000` `.data` delta),
`DSELNX16` resolves to exactly **five** descriptors, the first **two** marked `'o'`:

```
DSELNX16_args  @0x8469c0 (file 0x6469c0):
  op0  opnd_ivp_sem_vec_select_vu   dir=0x6f 'o'   <- 2nd output
  op1  opnd_ivp_sem_vec_select_vt   dir=0x6f 'o'   <- 1st output
  op2  opnd_ivp_sem_vec_select_vs   dir=0x69 'i'
  op3  opnd_ivp_sem_vec_select_vr   dir=0x69 'i'
  op4  opnd_ivp_sem_vec_select_sr   dir=0x69 'i'   <- control vector
```

`SELNX16_args` (one output) is four descriptors (`vt vs vr sr`). The dual-output of `DSEL` is therefore
not an interpretation — it is **two `'o'`-marked operand descriptors in the encoding table**. The
operand-name strings (`vt vu vs vr sr vbr isel ishfl slct slct_h`) all live in `.rodata` as
`opnd_ivp_sem_vec_select_*`. The descriptor counts (`region-size / 16`) and directions per op:

| op | operands | shape |
|----|---------:|-------|
| `SHFLNX16` / `SHFL2NX8` / `SHFLN_2X32` | 3 | `vt`(o) `vr`(i) `sr`(i) |
| `SHFL2NX8I` | 3 | `vt`(o) `vr`(i) `ishfl`(imm) |
| `SELNX16` / `SEL2NX8` / `SELN_2X32` | 4 | `vt`(o) `vs`(i) `vr`(i) `sr`(i) |
| `SEL2NX8I` | 4 | `vt`(o) `vs`(i) `vr`(i) `isel`(imm) |
| `SELNX16T` (.T) | 5 | `vt`(**m** RMW) `vs`(i) `vr`(i) `sr`(i) `vbr`(i) |
| `DSELNX16` / `DSELN_2X32` | 5 | `vu`(o) `vt`(o) `vs`(i) `vr`(i) `sr`(i) |
| `DSEL2NX8I` | 5 | `vu`(o) `vt`(o) `vs`(i) `vr`(i) `slct`(imm) |
| `DSEL2NX8I_H` | 5 | `vu`(**m**) `vt`(o) `vs`(i) `vr`(i) `slct_h`(imm) |
| `DSELNX16T` (.T) | 6 | `vu`(**m**) `vt`(**m**) `vs`(i) `vr`(i) `sr`(i) `vbr`(i) |
| `DCMPRS2NX8` | 3 | `vt`(o) `vr`(i) `vbr`(i) |

The `.T` predicated forms mark the destination(s) `'m'` (inout) — a read-modify-write, because the
killed lanes keep the destination's prior value (§4.3). `[HIGH/OBSERVED — every count is
region-size/16 from the symbol table; the vu/vt dual-out, the inout `.T` destinations, and the vbr/imm
controls are the literal descriptor directions.]`

> **CORRECTION — the immediate operands are named `isel` / `ishfl` / `slct` / `slct_h`, not
> `saimm7`/`selimm`/`shflimm`.** A conceptual reading labelled the 7-bit `SEL2NX8I`/`SHFL2NX8I`
> immediate `saimm7` and the `_Sn` coded immediate `selimm`/`shflimm`. The actual operand-name strings
> in `.rodata` are `opnd_ivp_sem_vec_select_isel`, `…_ishfl`, `…_slct`, `…_slct_h` — the tokens
> `saimm7`/`selimm`/`shflimm` appear in **no** shipped binary. This page uses the binary names; the
> "sa" mnemonic note (`isel` is a *select-address* index, not signed arithmetic) still holds. The
> `_Sn` coded-pattern immediate is the same `isel`/`ishfl` field of the slot-pinned encoding.
> `[HIGH/OBSERVED — exhaustive string sweep, all three DLLs]`

### 2.3 Register files and operand widths

Two independent reads of the operand classes, both byte-exact this pass:

The `libcas-core.so` address-resolver thunks mask the operand index to the file size:

| class | resolver | mask | regs |
|-------|----------|------|------|
| `vec` (`vt vu vs vr sr`) | `opnd_sem_vec_addr @0x17aa270` | `and $0x1f` | 32 |
| `vbool` (`vbr`) | `opnd_sem_vbool_addr @0x17aa280` | `and $0xf` | 16 |
| `valign` | `opnd_sem_valign_addr @0x17aa2c0` | `and $0x3` | 4 |

and the `regfiles[]` descriptor table in `libisa-core.so` (VMA `0x74a800`, file `0x54a800`, in
`.data.rel.ro`, **8 entries × 56 bytes**, between `regfile_views @0x74a780` and `funcUnits @0x74a9c0`)
gives the bit-width and count per file (`name @+0x0`, `width @+0x18`, `count @+0x1c`):

| idx | file | width | count | used by this batch |
|----:|------|------:|------:|--------------------|
| 0 | `AR` | 32 | 64 | (immediate-rotate scalar path) |
| 1 | `BR` | 1 | 16 | — |
| **2** | **`vec`** | **512** | **32** | `vt vu vs vr sr` |
| **3** | **`vbool`** | **64** | **16** | `vbr` |
| **4** | **`valign`** | **512** | **4** | — (the valign sibling) |
| 5 | `wvec` | 1536 | 4 | — |
| 6 | `b32_pr` | 64 | 16 | `DCMPRS` drain port |
| 7 | `gvr` | 512 | 8 | — |

The control vector `sr`, the two data sources `vr`/`vs`, and both outputs `vt`/`vu` are all in the
512-bit `vec` file (idx 2); the `.T`-form predicate `vbr` is a 64-bit `vbool` (idx 3). The
`&0x1f / &0xf / &0x3` masks match the idx-2/3/4 counts exactly. `[HIGH/OBSERVED — masks + descriptor
table both read this pass]`

### 2.4 The selector constants — encode-thunk bodies

Each placement's `Opcode_ivp_<mnem>_Slot_<slot>_encode` is a two-instruction thunk `movl
$imm32,(%rdi); ret` (the high opcode word at `0x4(%rdi)` is always 0); the `imm32` is the format-local
opcode-selector template the assembler writes into the bundle. Read byte-for-byte at `F0/S3` (the
canonical ALU slot) this pass:

| op | `F0/S3` encode `imm32` | op | `F0/S3` encode `imm32` |
|----|----------------------:|----|----------------------:|
| `SEL2NX8` | `0x66c00000` | `DSELNX16` | `0x60000000` |
| `SELNX16` | `0x66d00000` | `DSELN_2X32` | `0x62000000` |
| `SELN_2X32` | `0x66e00000` | `DSEL2NX8I` | `0x64000000` |
| `SEL2NX8I` | `0x66800000` | `DSEL2NX8I_H` | `0x64004000` |
| `SEL2NX8T` | `0x80000000` | `DSELNX16T` | `0x00000000` |
| `SELNX16T` | `0x80100000` | `DSELN_2X32T` | `0x20000000` |
| `SELN_2X32T` | `0x80200000` | `DCMPRS2NX8` | `0x81000002` |
| `SHFL2NX8` | `0x82900208` | `SHFLN_2X32` | `0x8290020c` |
| `SHFLNX16` | `0x8290020a` | `SHFL2NX8I` | `0x82800208` |

Two structural facts fall straight out of these bytes:

* **`SHFL` shares one base, width in the low byte.** `SHFL{2NX8,NX16,N_2X32}` are
  `0x82900200 + {0x08,0x0a,0x0c}` — the element width is a per-format enumerated low-byte field, *not*
  three independent opcodes. The immediate form `SHFL2NX8I` swaps the base to `0x82800208`. The
  `DSEL2NX8I_H` "high half" variant is exactly `DSEL2NX8I + 0x4000` (bit 14). `[HIGH/OBSERVED]`
* **`DSELNX16T` encodes to `0x0`.** Its opcode-selector bits land entirely in the operand-field region
  of the wide bundle (the legal "no nonzero upper lane" case); the discriminator is carried by the
  operand binding, not a nonzero selector word. `[HIGH/OBSERVED]`

> **QUIRK — the `&0x3f` (SEL) vs `&0x1f` (SHFL) index-mask split.** For the *same* `NX16` 32-lane
> grid, the two ops mask the per-lane control index to *different* widths — and that one-bit
> difference is the whole SEL-vs-SHFL distinction. Read straight from the `libfiss-base.so` value
> bodies this pass:
>
> | op | index mask | reach |
> |----|-----------|-------|
> | `shfl_nx16  @0x86af70` | `and $0x1f` (5-bit, 0..31) | one source, `N=32` lanes |
> | `sel_nx16   @0x85ef90` | `and $0x3f` (6-bit, 0..63) | two-source pool, `2N=64` lanes |
>
> The extra index bit `SEL` carries selects *which source vector*. The full per-width mask table
> (SEL mask = SHFL mask × 2 + 1, every one read as the `and $0xN` immediate in the matching
> `module__xdref_{shfl,sel}_*` body): `shfl` `{2nx8:0x3f, nx16:0x1f, n_2x32:0x0f}` (= `log2(N)`),
> `sel` `{2nx8:0x7f, nx16:0x3f, n_2x32:0x1f}` (= `log2(2N)`). `[HIGH/OBSERVED — six masks read]`

### 2.5 The slot-pinned `_Sn` forms

The six `ivp_sem_vec_specialized_seli` ops exist so the bundle scheduler can place a byte-permute in a
*non-S3* slot when S3 is occupied. The `_Sn` suffix is **literally the FLIX slot the thunk is locked
into** — every placement of an `_S0` op is an S0 (LdSt) slot, every `_S2` an S2 (Mul) slot, every `_S4`
an S4 (ALU2) slot, with **no** cross-contamination:

| op | slot class(es) of every placement | placements |
|----|-----------------------------------|-----------:|
| `SEL2NX8I_S0` | `s0_ldst`, `s0_ldstalu` | 2 |
| `SEL2NX8I_S2` | `s2_mul` | 3 |
| `SEL2NX8I_S4` | `s4_alu` | 2 |
| `SHFL2NX8I_S0` | `s0_ldst`, `s0_ldstalu` | 8 |
| `SHFL2NX8I_S2` | `s2_mul` | 3 |
| `SHFL2NX8I_S4` | `s4_alu` | 2 |

> **QUIRK — `_Sn` is the host slot, and the immediate is a *coded* lane pattern, not a free index.**
> The `_Sn` thunks carry a 5-bit `isel`/`ishfl` field that is a **one-hot / power-of-two grouped
> pattern**, not a raw 0..31 select. Probed exhaustively against the device assembler this pass, the
> legal set for `SEL2NX8I_S0` is `{0,1,2,3,8,16,32,64}` — the assembler *rejects* `4` and `127`. The
> encode `imm32` is format/slot-specific (it is the packed selector for that one FLIX format): e.g.
> `SEL2NX8I_S0` in `f1_s0_ldstalu` = `0x11000000`, in `n2_s0_ldst` = `0x10100000`; `SEL2NX8I_S4` in
> both `f3_s4_alu` and `f11_s4_alu` = `0x00000000` (the selector lands wholly in the slot-local operand
> region); `SHFL2NX8I_S2 (f1_s2_mul)` = `0x02e43000`, `SHFL2NX8I_S4 (f3_s4_alu)` = `0x00b38000`.
> `[HIGH/OBSERVED — slot lists + encode bytes + assembler probe]`

### 2.6 The slot-local field map (generic SEL/SHFL/DSEL family, `F0/S3`)

The `F0/S3` slot-local bit-field assignment (distinct from the `vec_alu` map), reconstructed from the
operand-field decode in the encode thunks (the `isel`/`ishfl` split-field masks confirmed in
libisa-core) and the round-trips of §5:

```
  vt  (1st OUT)        : bits[19:15]                 (5-bit, contiguous)
  vu  (2nd OUT, DSEL)  : bits[24:20]                 (5-bit, contiguous)
  sr  (CTRL vec IN)    : bits[14:10]                 (5-bit)
  vs  (IN2)            : bits[9:8] ++ bits[3:1]      (5-bit, MSB-first split)
  vr  (IN1)            : bits[7:4] ++ bit[0]         (5-bit, MSB-first split)
  vbr (.T predicate)   : bits[28:25]                 (4-bit vbool select)
  slct  (DSEL2NX8I)    : bits[13:10]                 (4-bit immediate, and $0xf,  0..15)
  slct_h(DSEL2NX8I_H)  : bits[11:10]                 (2-bit immediate, and $0x3,  0..3)
  isel  (SEL2NX8I)     : (esi&0x1f)<<10 | (esi<<15)&0x300000   (5+2 = 7-bit split)
  ishfl (SHFL2NX8I)    : (esi&0x3)<<1   | (esi<<8)&0x7c00       (2+5 = 7-bit split)
```

The `isel`/`ishfl`/`slct`/`slct_h` field-extraction masks are read directly from the libisa-core field
`_set` routines; the `vt`/`vu`/`sr` contiguous fields are confirmed by the §5 round-trips.
`[HIGH/OBSERVED for the OUT/CTRL contiguous fields and the four immediate split masks; MED/INFERRED for
the exact split-field bit positions of vs/vr.]`

---

## 3. Immediate ranges — device-assembler probe `[HIGH/OBSERVED]`

Every immediate range was probed by attempting an `xtensa-elf-as` assembly of the boundary values
(`XTENSA_CORE=ncore2gp`); legality is the assembler's accept/reject:

| immediate | op | legal range | rejected | meaning |
|-----------|----|-------------|----------|---------|
| `isel` / `ishfl` | `SEL2NX8I` / `SHFL2NX8I` | `0..127` | `128`, `-1` | 7-bit **unsigned** select-address |
| `slct` | `DSEL2NX8I` | `0..15` | `16` | 4-bit deal pattern |
| `slct_h` | `DSEL2NX8I_H` | `0..3` | `4` | 2-bit half-pattern |
| `isel`/`ishfl` (`_S0/_S2/_S4`) | the specialized forms | `{0,1,2,3,8,16,32,64}` | `4`, `127` | coded one-hot/grouped pattern |

> **CORRECTION — the `_S0` coded-immediate legal set includes `3`.** An earlier reading listed the
> accepted `_S0` patterns as `{0,1,2,8,16,32,64}` (rejecting `4`). Re-probing the device assembler
> exhaustively this pass, **`3` is also accepted** while `4` and `127` remain rejected, so the legal
> set is `{0,1,2,3,8,16,32,64}`. The shape of the encoding (sparse, power-of-two-grouped, *not* a free
> index) is unchanged. `[HIGH/OBSERVED — exhaustive as probe]`

---

## 4. Semantics — per-lane value functions

The lane-routing core is a 5-bit one-hot 32-way multiplexer instantiated per output half; the value
functions below were **executed live** in `libfiss-base.so` (`ctypes.CDLL`, NULL context) this pass on
directed edge inputs, and the index masks are the `and $0xN` reads of §2.4. The deeper RTL model
(`xdsem_tiesel_5_32`, `xdsem_bitkill`) is the [group-semantics-II](../semantics/group-semantics-ii.md)
reference; the per-lane functions are reproduced here.

### 4.1 SEL — two-source `2N`-to-`N` crossbar

```c
// module__xdref_sel_nx16_512_512_512_512 @0x85ef90
// pool = {srcB ++ srcA}: srcB occupies the LOW pool half, srcA the HIGH half.
void sel_nx16(const i16 vr[32] /*srcA*/, const i16 vs[32] /*srcB*/,
              const i16 sr[32] /*ctrl*/, i16 vt[32] /*out*/) {
    for (int k = 0; k < 32; k++) {
        unsigned idx = sr[k] & 0x3f;          // 6-bit: spans the 2N=64-lane pool
        vt[k] = (idx < 32) ? vs[idx] : vr[idx - 32];  // B low half, A high half
    }
}
```

Width variants differ only in the mask: `SEL2NX8` `&0x7f` (128-lane pool), `SELN_2X32` `&0x1f`
(32-lane pool). **Live-driven** (`srcA[k]=0xA00+k`, `srcB[k]=0xB00+k`): `idx 0 → 0xB00` (B[0]),
`idx 32 → 0xA00` (A[0]), `idx 63 → 0xA1f` (A[31]), `idx 127&0x3f=63 → 0xA1f` — B is the low pool half.
`[HIGH/OBSERVED — proven by execution]`

### 4.2 SHFL — single-source `N`-to-`N` permute

```c
// module__xdref_shfl_nx16_512_512_512 @0x86af70
void shfl_nx16(const i16 vr[32] /*src*/, const i16 sr[32] /*ctrl*/, i16 vt[32]) {
    for (int k = 0; k < 32; k++)
        vt[k] = vr[ sr[k] & 0x1f ];           // 5-bit: one source, no source-select bit
}
```

**Live-driven**: `idx 5 → 0x105`, `idx 0x25&0x1f=5 → 0x105` (mask wraps), `idx 31 → 0x11f`. Shuffle has
**no `.T` predicated form** — a pure permute needs no per-lane guard (the roster confirms the absence).
`[HIGH/OBSERVED]`

### 4.3 SEL.T — predicated merge

```c
// op_pred_t: each output lane gated by xdsem_bitkill (the vbool kill); vt is INOUT (RMW)
void sel_nx16_t(const i16 vr[32], const i16 vs[32], const i16 sr[32],
                const u2 vbr[32] /*per-lane vbool*/, i16 vt[32] /*read-modify-write*/) {
    for (int k = 0; k < 32; k++)
        vt[k] = (vbr[k] & 1) ? sel_lane(vr, vs, sr, k)   // §4.1 pool[ctrl]
                             : vt[k];                     // killed lane keeps dst's prior value
}
```

The kill is the same per-lane `xdsem_bitkill` predicate guard the `_t` arithmetic ops use; the
destination `vt` is the `'m'`-marked (inout) descriptor of §2.2 — a read-modify-write whose killed
lanes retain their prior value. `[HIGH/OBSERVED — the vbr operand + the inout direction; CARRIED merge
idiom]`

### 4.4 SEL.I / SHFL.I — immediate-pattern permute

`vt[k] = pool[ imm_pattern(k) ]`: a *single* 7-bit immediate (`isel`/`ishfl`, 0..127) names a fixed
permutation pattern that drives **all** lanes — a packed per-lane index, not one scalar index. The
immediate indexes a baked 7-bit lane-pattern table internal to the permute network (`tab_selimm_7b` /
`tab_shflimm_7b`); the exact `imm → pattern` map is the network LUT (the `<SEMANTIC>` body, not exposed
in this config). `[HIGH structure / MED exact-LUT]`

### 4.5 DSEL — dual-output deal/zip

```c
// module__xdref_dsel_nx16_..._32 @0x5ccee0  — writes TWO 512-bit results
void dsel_nx16(const i16 vr[32], const i16 vs[32], const i16 sr[32],
               i16 vt[32] /*out1*/, i16 vu[32] /*out2*/) {
    // two independent permutations of the same {vr++vs} pool, both written this issue.
    // canonical use: de-interleave (even-lane gather -> vt, odd-lane gather -> vu),
    // i.e. a butterfly/deal stage. The dual ARG_OUT (vu,vt) both retire together.
}
```

Operand order from the oracle: `IVP_DSEL* vu, vt, vs, vr [, sr | imm]`. `DSEL2NX8I` drives the deal by
`slct` (imm4, 16 patterns); `DSEL2NX8I_H` by `slct_h` (imm2, 4 half-patterns). The dual output is the
two output descriptors of §2.2 — both `vu` and `vt` are written in one issue. `[HIGH/OBSERVED —
dual-out signature]`

### 4.6 DCMPRS2NX8 — predicate-driven byte EXPAND (the prefix-popcount primitive)

```c
// module__xdref_dcmprs_2nx8_512_512_64 @0x8341d0
//   calls module__xdref_popc64_7_64  @0x8236c0  (the prefix popcount helper)
//     and module__xdref_dcmprs_clamp @0x8341c0  (the fill-index clamp)
void dcmprs_2nx8(const u8 vr[64] /*src*/, u64 vbr /*64-bit vbool*/, u8 vt[64]) {
    for (int k = 0; k < 64; k++) {
        unsigned idx = popc64(vbr & ((1u64 << k) - 1)) & 0x3f;  // # of TRUE lanes BELOW k
        // dcmprs_clamp: test predicate bit k; if 0 -> cmove index to 0x3f (= src[63] fill)
        vt[k] = (vbr >> k & 1) ? vr[idx] : vr[63];
    }
}
```

This is **EXPAND**: each output lane that the predicate keeps pulls the next element from the *compacted*
source stream (its position in that stream = the prefix-popcount of the predicate below it); each
*dropped* lane is filled with `src[63]`. The body calls `module__xdref_popc64_7_64 @0x8236c0` (the
prefix-popcount helper — there is no `popcnt` *instruction* in the unrolled body; the bit-counting is
the called helper) at each lane, then `module__xdref_dcmprs_clamp @0x8341c0`, whose body
`test %edx,%edx; mov $0x3f,%eax; cmove %eax,%esi` forces the index to `0x3f` (lane 63) when the lane's
predicate bit is zero. `[HIGH/OBSERVED — popc64 call + clamp body + proven by execution]`

> **GOTCHA — DCMPRS is an EXPAND, not a left-pack COMPRESS.** A naive reading of "compress" expects the
> kept lanes to land *contiguously at the bottom* (`out[0..n-1]`). The shipped value function instead
> places each kept lane **at its own predicate position**, pulling from the compacted source — a
> *scatter into the predicate-true slots*. Left-pack is only the special case where the predicate is
> contiguous at the bottom. **Live-driven worked example** (`src[j] = 0xA0 + j`, so `src[63] = 0xDF`):
>
> | predicate `vbr` (kept lanes) | per-kept-lane prefix popcount → source | output (positions shown, all others `=0xDF` fill) |
> |---|---|---|
> | `keep {3, 7, 9}` | `out[3]←src[0]`, `out[7]←src[1]`, `out[9]←src[2]` | `out[3]=0xA0, out[7]=0xA1, out[9]=0xA2` |
> | `keep {0, 1, 2}` (contiguous → looks like left-pack) | `out[0..2]←src[0..2]` | `0xA0 0xA1 0xA2 0xDF…` |
> | `keep {0, 2, 4, 6}` | stride-2 expand | `0xA0 0xDF 0xA1 0xDF 0xA2 0xDF 0xA3 0xDF…` |
> | `keep {}` (none) | — | all `0xDF` |
>
> Walking `keep {3,7,9}` explicitly: `out[3] = src[popcount(bits below 3) = 0] = src[0] = 0xA0`;
> `out[7] = src[popcount({3}) = 1] = src[1] = 0xA1`; `out[9] = src[popcount({3,7}) = 2] = src[2] =
> 0xA2`. The "compress" name describes the *source* read order (kept elements read densely 0,1,2,…);
> the *output* is the EXPAND placement. `[HIGH/OBSERVED — four predicates executed live]`

### 4.7 Specialized SELi/SHFLi (`_S0/_S2/_S4`)

Identical SEL.I / SHFL.I compute to §4.4/§4.2, but slot-pinned (§2.5) with the coded `isel`/`ishfl`
pattern (§3). They let the bundler co-issue a byte-permute in the LdSt/Mul/ALU2 lane while an unrelated
op holds S3. `[HIGH/OBSERVED — slot-pin + compute equivalence]`

---

## 5. Worked bit-patterns — device-oracle round-trips `[HIGH/OBSERVED]`

Every bundle below was assembled by `xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) and disassembled back to
the exact mnemonic + operand spelling by `xtensa-elf-objdump` this pass. The bytes are the bundle's
little-endian word as emitted (companion slots `nop`); the 8-byte forms are the S3 ALU slot inside an
N0 bundle, the `DCMPRS2NX8` form is a 16-byte wide bundle:

| bundle | bytes (LE) |
|--------|-----------|
| `{ nop; nop; nop; ivp_selnx16  v3, v1, v2, v4 }` | `2250d2302044452f` |
| `{ nop; nop; nop; ivp_shflnx16  v5, v1, v2 }` | `3250d0581242452f` |
| `{ nop; nop; nop; ivp_dselnx16  v6, v7, v1, v2, v4 }` | `22500c702044452f` |
| `{ nop; nop; nop; ivp_sel2nx8i  v3, v1, v2, 5 }` | `2250c8302045452f` |
| `{ nop; nop; nop; ivp_selnx16t  v3, v1, v2, v4, vb2 }` | `325082302044452f` |
| `{ nop; nop; nop; ivp_dsel2nx8i_h  v6, v7, v1, v2, 1 }` | `22508c702051452f` |
| `{ nop; nop; nop; ivp_dcmprs2nx8  v3, v1, vb2 }` | `0009191448c08300950022108504452f` |
| `{ ivp_sel2nx8i_s0  v3, v1, v2, 8; nop }` | `029c04409c04c22f` |
| `{ ivp_shfl2nx8i_s0  v5, v1, 8; nop }` | `029c62409e04410f` |

**Reconstruction of `ivp_selnx16 v3,v1,v2,v4` (`2250d2302044452f`)** from the §2.6 map: selector
`0x66d` in the high field, `vt=3` in `[19:15]`, `sr=v4` in `[14:10]`, `vs=v1`/`vr=v2` in the split
fields. **Executed semantics**: `pool = v1 ++ v2` (64 `i16` lanes); for `k` in `0..31`,
`v3[k] = pool[v4[k] & 0x3f]` — each result lane copies the pool lane its `v4` index names, reaching
across both `v1` and `v2`.

The two `_S0` bundles (`029c…c22f`, `029c…410f`) occupy the **S0 (LdSt)** slot — note they are the
*first* slot of the bundle (not the S3-ALU slot the generic forms use), freeing S3 for a co-issued ALU
op exactly as §2.5 predicts.

---

## 6. Issue timing `[HIGH/OBSERVED]`

Read from the `*_issue` scoreboard bodies in `libcas-core.so` (the `mov $LAT,%esi` immediate before
each operand-scoreboard `call`). All generic ops issue in the **S3 ALU** lane-permute slot
(`F0_F0_S3_ALU_36_…`); the `_Sn` forms in their pinned LdSt/Mul/ALU2 slot.

| op | issue fn | data/result reads | control read | host classes |
|----|----------|-------------------|--------------|--------------|
| `SELNX16` | `@0x14b2fb0` | `vt`,`vs`,`vr` @ LAT `0xa` (10) | `sr` @ LAT `0xc` (12) | `vec` |
| `SHFLNX16` | `@0x14b3080` | `vt`,`vr` @ LAT 10 | `sr` @ 12 (+ `AR` @ 12 immediate path) | `vec`/`AR` |
| `DSELNX16` | `@0x14b4940` | `vu`,`vt`,`vs` @ LAT 10 | two reads @ 12 | `vec` (×5) |
| `DCMPRS2NX8` | `@0x14b7bb0` | `vbr`,`vr` @ LAT 10 | `vec` @ 12, `b32_pr` @ 12 | `vbool`+`vec`+`b32_pr` |

The data operands and the result(s) resolve at **LAT 10** (the S3-ALU vector-execute stage); the
**control selector vector `sr`** resolves one stage deeper at **LAT 12** — the extra read depth is the
lane crossbar. `DSELNX16` shows its dual output as a third LAT-10 `vec` read; `DCMPRS2NX8` pulls the
`vbool` predicate at LAT 10 and drains the result through the pack network at LAT 12. `[HIGH/OBSERVED —
every LAT byte read this pass]`

> **NOTE — result latency vs the deeper selector read (a cross-page reconciliation).** Two readings of
> the same bytes coexist in sibling reports. Viewed as a control-to-result span, the crossbar is a
> **2-cycle** op (`USE = 10`, `DEF = 12`). Viewed as the operand scoreboard, the result/use latency is
> the uniform **LAT-10** vector-execute stage (forwardable like any S3-ALU op) and the **LAT-12** read
> is the *control selector* operand, not the result writeback. The binary encodes the per-operand
> scoreboard latencies directly: data/result at `0xa`, the selector vector at `0xc`. This page reports
> those operand latencies; both readings agree on the bytes. The
> [VALIGN / Shuffle-Select / Reduce ISS slice](../../iss/cas-valign-shuffle-reduce.md) carries the full
> scoreboard model. `[HIGH/OBSERVED]`

---

## 7. FLIX placement counts `[HIGH/OBSERVED]`

Per-op placement counts (the number of `Opcode_ivp_<mnem>_Slot_*_encode` thunks), counted directly
from the `libisa-core.so` symbol table this pass (**127 placements total**):

| op | placements | op | placements |
|----|-----------:|----|-----------:|
| `SEL2NX8` / `SELNX16` / `SELN_2X32` | 7 each | `SHFL2NX8` / `SHFLNX16` / `SHFLN_2X32` | 7 each |
| `SEL2NX8T` / `SELNX16T` / `SELN_2X32T` | 7 each | `SHFL2NX8I` | 9 |
| `SEL2NX8I` | 9 | `DCMPRS2NX8` | 2 |
| `DSEL2NX8I` / `DSEL2NX8I_H` | 4 each | `SEL2NX8I_S0` / `_S4` | 2 each |
| `DSELNX16` / `DSELN_2X32` | 4 each | `SEL2NX8I_S2` | 3 |
| `DSELNX16T` / `DSELN_2X32T` | 4 each | `SHFL2NX8I_S0` | 8 |
| | | `SHFL2NX8I_S2` / `_S4` | 3 / 2 |

The generic dynamic forms spread across the seven `S3`-bearing formats (`F0,F1,F2,F4,F6,F7,N0`); the
immediate forms gain extra format placements (9); the DSEL family is restricted to four formats;
`DCMPRS2NX8` to two (`F0/S3`, `F1/S3`). The full per-slot bit scatter for every placement is the FLIX
decoder's domain, not transcribed here. `[HIGH/OBSERVED — symbol counts; MED for the exact format list
per op.]`

---

## 8. Cross-references and divergences

* [B16 — Vector Replicate / Extract](b16-vec-rep.md) — the single-scalar-indexed neighbours; the
  scalar-broadcast selects (`sels*`) live there, on the `vec_rep` datapath.
* [Group Semantics II](../semantics/group-semantics-ii.md) — the RTL reference compute
  (`xdsem_tiesel_5_32` 5-bit 32-way lane mux; `xdsem_bitkill`); the immediate-permute LUTs
  (`tab_selimm_7b` / `tab_shflimm_7b`).
* [cas/fiss SuperGather](../../iss/cas-supergather.md) — the memory-indexed sibling (B19); the shuffle/
  select ops are live-driven there to blend gathered results.
* [cas/fiss VALIGN / Shuffle-Select / Reduce](../../iss/cas-valign-shuffle-reduce.md) — the full ISS
  decode/value/timing for the lane-manipulation families (the reduce fold and valign funnel-shift legs).
* [VAL — Reduce / Shift / Shuffle-Select](../../validation/reduce-shift-shuffle.md) — the four-oracle
  bit-exact validation that drove these value functions live (the source of the DCMPRS EXPAND worked
  example).
* [Template & Partition](template-and-partition.md) — the 30-batch classifier; the `extr*` glob
  reclassification.

> **CORRECTION / DIVERGENCE LEDGER.**
> 1. **Immediate operand names** — the binary strings are `isel`/`ishfl`/`slct`/`slct_h`, not
>    `saimm7`/`selimm`/`shflimm` (§2.2). `[HIGH/OBSERVED]`
> 2. **`_S0` coded immediate** — the legal set is `{0,1,2,3,8,16,32,64}` (`3` accepted, `4` rejected),
>    refining an earlier `{0,1,2,8,16,32,64}` reading (§3). `[HIGH/OBSERVED]`
> 3. **`SEL.T`/`DSEL.T`/`DCMPRS` selector** — a slot-local field map cites these as short 6-bit
>    `[34:29]` prefixes, but the **full encode words** read this pass are `SEL2NX8T=0x80000000`,
>    `SELNX16T=0x80100000`, `DSELNX16T=0x0`, `DSELN_2X32T=0x20000000`, `DCMPRS2NX8=0x81000002` — the
>    slot-local prefix and the full word are consistent (the prefix is the high field of the word);
>    this page reports the full encode bytes. `[HIGH/OBSERVED]`
> 4. **Result latency reading** — the operand scoreboard encodes data/result at LAT 10 and the control
>    selector at LAT 12; a "2-cycle use-10/def-12" reading describes the same bytes as a
>    control-to-result span (§6). No contradiction; this page reports the operand latencies.
>    `[HIGH/OBSERVED]`
> 5. **`DCMPRS` popcount** — the value body has no `popcnt` *instruction*; the prefix popcount is the
>    called `module__xdref_popc64_7_64 @0x8236c0` helper. The EXPAND semantic is unchanged (§4.6).
>    `[HIGH/OBSERVED]`
