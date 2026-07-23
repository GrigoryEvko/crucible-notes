# ISA Batch 07 — Vector Stores

This is the per-instruction reference for the **vector store** family of the Vision-Q7 *Cairo*
(`ncore2gp`) ISA: the 92 shipped `ivp_` mnemonics that move a 512-bit
[`vec`](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster) register (or a
[`vbool`](../core/register-files.md#3-the-eight-register-files--the-authoritative-roster) mask) **out**
to memory through the single store-capable issue slot `s0` (the `ldst` slot), plus the store-side
alignment idiom — the [`valign`](../core/register-files.md#per-file-detail)-assisted unaligned store
that primes a carry register, streams rotate-merged vectors, and flushes the residual tail bytes at the
end of a span. It is the exact mirror of [**Batch 06**](b06-loads.md) (loads): same `s0` slot, same AGU
addressing modes, same `valign` priming machinery, opposite data direction. It is *not* the
scatter/gather path — indexed stores live in [**Batch 19**](b19-scatter-gather.md); B07 is
**contiguous and strided** stores only. The boundary is stated and `nm`-checked in
[§9](#9-the-b06--b07--b19-boundary).

Every mnemonic's FLIX slot, opcode-selector template, operand bit-window and addressing-mode encoding
is read **directly out of the shipped binaries** — `libisa-core.so` encode thunks (`objdump`), the
`fld_*_get` field getters, the on-device `xtensa-elf-as`/`xtensa-elf-objdump` round-trip, and the
`libfiss-base.so` `module__xdref_sav` merge leaf **driven live by ctypes** — and tagged. This page
inherits the certified denominator from [the coverage tally](../core/coverage-tally.md): the
`1534 / 12569` shipped mnemonic/placement cover and the `864/864` value-leaf cover. Counts are grounded
with `nm | rg -c` against the binary `.symtab`, never a decompile grep; the `extracted/` tree is
gitignored (reach it with `fd --no-ignore` or an absolute path). The page default is
`[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag, following
[the Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a byte / immediate /
symbol / **executed** value read from the shipped binary; `INFERRED` = reasoned over OBSERVED; `CARRIED`
= re-used at a cited page's confidence; crossed with `HIGH`/`MED`/`LOW`. All prose is binary /
static-analysis derived only.

> **Scope in one line.** B07 = `sv*` (aligned vector store, 40) + `ss*` (stream / deinterleave-on-store,
> 32) + `sb*` (`vbool` bit-mask store, 6) + `sa*`/`sav*`/`san*` + `salign`/`sapos` (valign-assisted
> unaligned store, prime, and tail-flush, 14), each in four AGU addressing modes
> (`_i`/`_ip`/`_x`/`_xp`), with a 20-strong `*t*` predicated subset. **92 mnemonics, 746 placements, 1
> value leaf (`sav`).**

---

## 1. Key facts

| Fact | Value | Binary source |
|---|---|---|
| B07 mnemonics (vector store) | **92** | classifier `^ivp_(sv\|ss\|sb\|sa)` over `nm libisa-core.so` `Opcode_*` roster ([§9](#9-the-b06--b07--b19-boundary)) |
| B07 placements (`mnemonic × slot`) | **746** | summed `nm \| rg -c 'Opcode_<m>_Slot_*_encode'` over the 92 |
| Issue slot | **`s0 = ldst`** of every wide + narrow format | `Opcode_*_Slot_<f>_s0_ldst_encode` (8–10 placements typical) |
| Data source — bulk stores | **`vec`** (idx 2, 512-bit, 32 entries) | live: `IVP_SV2NX8_IP v0, a3, …` (operand 1 = `v`) |
| Data source — `sb*` bit-stores | **`vbool`** (idx 3, 64-bit, 16 entries) | live: `IVP_SBN_I vb0, a3, 0` (rejects `v0`/`pr0`/`u0`) |
| Alignment-carry register | **`valign`** (idx 4, `u`, 512-bit, 4 entries) | live: `IVP_SAVNX8S_XP v0, u0, a3, a4` (operand 2 = `u`) |
| Address base / stride | **`AR`** (idx 0, `a`, 32-bit, 64 entries) | live: operand 3 = `a3` base, operand 4 = `a4` stride |
| AGU addressing modes | `_i` (imm), `_ip` (imm post-inc), `_x` (reg stride), `_xp` (reg post-inc) | suffix → word0 selector bits ([§3](#3-encoding--addressing-mode-bits)) |
| Post-increment flag | **bit `0x100`** of word0 (`_xp` − `_x` = `+0x100`) | `sv2nx8`: `_x`=`0x10f00000`, `_xp`=`0x10f00100` |
| Predicated stores (`*t*`) | **20** — `vbool`-masked partial / byte-enable store | `nm` `^ivp_(sv2nx8t\|svn_2x16st\|svn_2x16ut\|…)` |
| Pipeline | unified **ld/st** `stage0..stage11`; `vec` read @10, mem write @11 | `ivp_sem_ld_st_{opcode,semantic}_stageN` (`libcas-core.so`) |
| Value leaf | **1** (`module__xdref_sav`); all other stores are identity data-movement | `nm libfiss-base.so \| rg module__xdref_sav` |
| word1 invariant | **`word1 == 0x00000000`** (184/746 thunks write it as `movl $0x0`) | `objdump` scan, all 746 store thunks ([§3.4](#34-the-word1--0-invariant)) |

The store family is wide (92 mnemonics) because four orthogonal axes multiply out: **data shape**
(`2nx8` byte / `nx8s`-`nx8u` signed-unsigned byte / `n_2x16` halfword / `nx16` halfword / `n_2x32`
word / stream-`NxM`) × **addressing mode** (`_i`/`_ip`/`_x`/`_xp`) × **alignment discipline** (aligned
`sv*` vs valign-assisted `sa*`/`sav*`/`san*`) × **predication** (`*t*` masked or unmasked). The roster
below is organized on the data-shape / verb axis.

---

## 2. Roster — the 92 vector store mnemonics

Columns: `mnemonic` · `FLIX format·slot` (the `s0_ldst` slots hosting it) · `opcode-sel imm` (the
`F0_S0_ldst` encode-thunk `WORD0`, the
[universal `C7 07 imm32 C3` ABI](../core/flix-encoding.md#61-the-universal-encode-thunk-abi); narrow
N-formats carry a smaller selector) · `src · AR fields` (data file + AR base/stride) · `bytes` (16
wide / 8 narrow) · semantics. Templates are byte-exact from `objdump -d`. Every row of §2.1–§2.4 is
`[HIGH/OBSERVED]`.

### 2.1 Aligned vector stores (`sv*` — the contiguous-store spine, 40)

The `sv` verb stores a full 512-bit `vec` register to a **naturally-aligned** address. The dtype suffix
is cosmetic for the store itself (the bits are written verbatim — a store is type-agnostic data
movement); the `s`/`u` token records the *intended* element signedness for the disassembler and the
matching load. All four AGU modes exist for each shape; the `t` forms are `vbool`-predicated.

| mnemonic | fmt·slot | opcode-sel imm (F0·s0) | src · AR | bytes | semantics |
|---|---|---|---|---|---|
| `ivp_sv2nx8_i` | F0/F1/F2/F3/F6/F7/N0/N1/N2 · s0_ldst | `0x10a20000` | `vec` · `a`base | 16/8 | store 64×`int8` lanes to `[base + imm]` |
| `ivp_sv2nx8_ip` | (same 9 slots) | `0x10f44100` | `vec` · `a`base±imm | 16/8 | store, then **`base += imm`** (imm×64-scaled) |
| `ivp_sv2nx8_x` | (same 9 slots) | `0x10f00000` | `vec` · `a`base,`a`stride | 16/8 | store to `[base + stride]` (reg stride) |
| `ivp_sv2nx8_xp` | (same 9 slots) | `0x10f00100` | `vec` · `a`base+=stride | 16/8 | store, then **`base += stride`** (reg post-inc) |
| `ivp_sv2nx8t_{i,ip,x,xp}` | F0/F1/F2/F3/F6/F7/N0/N1/N2 | `0x10200000` (`t_i`) | `vec`+`vbool` · `a` | 16/8 | **predicated** byte store — write only mask-on lanes |
| `ivp_svnx8s_{i,ip,x,xp}` | (9 slots) | `0x10a60000` (`s_i`) | `vec` · `a` | 16/8 | store 64 **signed** `int8` lanes (`nx8`, half-vec view) |
| `ivp_svnx8u_{i,ip,x,xp}` | (9 slots) | `0x10aa0000` (`u_i`) | `vec` · `a` | 16/8 | store 64 **unsigned** `int8` lanes |
| `ivp_svnx8st_{i,ip,x,xp}` | (9 slots) | — | `vec`+`vbool` · `a` | 16/8 | predicated signed-byte store |
| `ivp_svnx8ut_{i,ip,x,xp}` | (9 slots) | — | `vec`+`vbool` · `a` | 16/8 | predicated unsigned-byte store |
| `ivp_svn_2x16s_{i,ip,x,xp}` | (9 slots) | `0x10ae0000` (`s_i`) | `vec` · `a` | 16/8 | store 32×**signed** `int16` lanes (`2x16` pair view) |
| `ivp_svn_2x16u_{i,ip,x,xp}` | (9 slots) | `0x10b20000` (`u_i`) | `vec` · `a` | 16/8 | store 32×**unsigned** `int16` lanes |
| `ivp_svn_2x16st_{i,ip,x,xp}` | (9 slots) | — | `vec`+`vbool` · `a` | 16/8 | predicated signed-halfword store |
| `ivp_svn_2x16ut_{i,ip,x,xp}` | (9 slots) | — | `vec`+`vbool` · `a` | 16/8 | predicated unsigned-halfword store |

The selector deltas across `s`/`u`/`2nx8`/`nx16` are **distinct iclasses**, not a single dtype bit:
`sv2nx8_i`=`0x10a20000`, `svnx8s_i`=`0x10a60000` (+0x40000), `svnx8u_i`=`0x10aa0000` (+0x40000 again),
`svn_2x16s_i`=`0x10ae0000`, `svn_2x16u_i`=`0x10b20000`. The +0x40000 ladder runs through the data-shape
field at bits[19:18] of word0 (see [§3.2](#32-the-data-shape-selector-ladder)).

### 2.2 Stream / deinterleave-on-store (`ss*`, 32)

The `ss` verb is the **streaming / transpose store**: it scatters the lanes of one `vec` register across
a *strided* address pattern so that an `NxM`-element group lands at the stride boundary — the
store-side of a transpose, used to write column-major from a row-major register. The `_NxM` suffix names
the deinterleave granularity (N consecutive elements of M bits). The narrow shapes (`ssnx16`, `ssn_2x32`,
`ss2nx8`) fit an 8-byte bundle; the coarse shapes (`ssn_4x64`, `ssn_8x128`, `ssn_16x256`) need a
**16-byte wide format** (live decode: 4-slot bundle with 3 `nop` fills).

| mnemonic | shape | bytes | opcode-sel imm (F0·s0) | semantics |
|---|---|---|---|---|
| `ivp_ssnx16_{i,ip,x,xp}` | 16-bit deinterleave | 8 | `0x10e38000` (`_i`) | stream-store, 16-bit element granularity |
| `ivp_ss2nx8_{i,ip,x,xp}` | 8-bit deinterleave | 8 | `0x10e30000` (`_i`) | stream-store, 8-bit element granularity |
| `ivp_ssnx8s_{i,ip,x,xp}` | signed 8-bit | 8 | `0x10e40000` (`_i`) | stream-store, signed byte |
| `ivp_ssn_2x16s_{i,ip,x,xp}` | signed 2×16 | 8 | — | stream-store, signed-halfword pair |
| `ivp_ssn_2x32_{i,ip,x,xp}` | 2×32-bit | 8 | `0x109a0000` (`_i`) | stream-store, 32-bit word pair |
| `ivp_ssn_4x64_{i,ip,x,xp}` | 4×64-bit | **16** | `0x109c0000` (`_i`) | stream-store, 64-bit group (wide format) |
| `ivp_ssn_8x128_{i,ip,x,xp}` | 8×128-bit | **16** | `0x109e0000` (`_i`) | stream-store, 128-bit group (wide format) |
| `ivp_ssn_16x256_{i,ip,x,xp}` | 16×256-bit | **16** | `0x10980000` (`_i`) | stream-store, 256-bit group (wide format) |

> **NOTE — `ss*` drops the `N1` placement; `sv*` keeps it.** The aligned `sv*` ops place in **9** slots
> (F0/F1/F2/F3/F6/F7/N0/N1/N2); the `ss*` stream stores place in **8** (no `N1`). The narrow `N1` slot
> cannot host the stream-store's extra stride-pattern field. A bundler that targets `N1` for a stream
> store has no encoding.

### 2.3 Predicate / bit-mask store (`sb*`, 6)

The `sb` verb stores a **`vbool`** mask register — not a `vec` data register — to memory: the
predicate-spill path. `sbn` packs the 64-bit `vbool` to a byte string; `sb2n` is the double-width
(2N) form. Confirmed live: `IVP_SBN_I vb0, a3, 0` assembles, `v0`/`pr0`/`u0` are rejected as the source.

| mnemonic | src | bytes | opcode-sel imm (F0·s0) | semantics |
|---|---|---|---|---|
| `ivp_sbn_{i,ip}` | `vbool` | 16/8 | `0x10c600c0` (`_i`) | store an N-lane `vbool` mask to `[base(+imm)]` |
| `ivp_sb2n_{i,ip}` | `vbool` | 16/8 | `0x10c60080` (`_i`) | store a 2N-lane `vbool` mask |
| `ivp_sbn_2_{i,ip}` | `vbool` | 16/8 | — | store the 2-bit-grouped `vbool` view |

> **GOTCHA — `sb*` source is `vbool`, and `sb*` drops the `N0` placement, keeps `N1`.** Unlike `sv*`
> (drops nothing) and `ss*` (drops `N1`), `sb*` places in 8 slots **without** `N0` but **with** `N1`
> (`f0/f1/f2/f3/f6/f7/n1/n2`). The mask-store's operand layout (a `vb` source instead of a `v`) fits the
> `N1` narrow format but not `N0`. Type the operand as `vbool`, not `vec`, or the disassembler rejects
> the bundle.

### 2.4 valign-assisted unaligned store (`sa*`/`sav*`/`san*` + `salign`/`sapos`, 14)

The alignment family is the heart of the unaligned store. `salign`/`sapos` **prime** a `valign` register
`u` from an `AR` base (setting the byte-position state, no `vec` source); `san*`/`sav*`/`sa*` perform the
**rotate-merge streaming store** (read `vec`, rotate by the carried byte offset, merge with the residual
held in `u`, store the aligned portion, update `u` with the new carry); a final flush writes the
trailing residual. `sa*`/`san*` are immediate-post-inc; `sav*` is variable-stride (`a`-reg stride).

| mnemonic | operands (live) | opcode-sel imm (F0·s0) | role |
|---|---|---|---|
| `ivp_salign_i` | `u, a, imm` | `0x11000200` | **prime** `valign` from base+imm (no store) |
| `ivp_salign_ip` | `u, a, imm` | `0x11020600` | prime + base post-inc |
| `ivp_sapos_fp` | `u, a` | `0x11060600` | set valign byte-**position** from base ("fp") |
| `ivp_sapos_fpxp` | `u, a, a` | `0x11000600` | set position + reg post-inc |
| `ivp_sa2nx8_ip` | `v, u, a` | `0x10fa00d0` | rotate-merge byte store, imm post-inc |
| `ivp_sanx8s_ip` | `v, u, a` | `0x10fa00e0` | rotate-merge **signed**-byte store |
| `ivp_sanx8u_ip` | `v, u, a` | `0x10fa00f0` | rotate-merge **unsigned**-byte store |
| `ivp_san_2x16s_ip` | `v, u, a` | `0x10fa8080` | rotate-merge signed-halfword store |
| `ivp_san_2x16u_ip` | `v, u, a` | `0x10fa8090` | rotate-merge unsigned-halfword store |
| `ivp_sav2nx8_xp` | `v, u, a, a` | `0x10cc0000` | **variable-stride** rotate-merge byte store |
| `ivp_savnx8s_xp` | `v, u, a, a` | `0x10cc8000` | variable-stride signed-byte store |
| `ivp_savnx8u_xp` | `v, u, a, a` | `0x10cd0000` | variable-stride unsigned-byte store |
| `ivp_savn_2x16s_xp` | `v, u, a, a` | `0x10cd8000` | variable-stride signed-halfword store |
| `ivp_savn_2x16u_xp` | `v, u, a, a` | `0x10ce0000` | variable-stride unsigned-halfword store |

> **QUIRK — `salign`/`sapos` add the `F4·s0_ld` placement; the bulk stores never do.** `salign_i`,
> `salign_ip`, `sapos_fp`, `sapos_fpxp` each carry **10** placements — the 9 bulk-store slots **plus**
> `f4_s0_ld` (the dual-`Ld` format's load slot). The prime ops have a *load-shaped* encoding (they read
> an `AR` base to set up the carry register, no `vec` write to memory), so they slot into the load lane
> as well. Templates differ per slot: `salign_i` is `0x11000200` in `f0_s0_ldst` but `0x10619400` in
> `f4_s0_ld`. A reimplementer's assembler must carry both.

---

## 3. Encoding — addressing-mode bits

### 3.1 Every store is an `s0` (ldst-slot) opcode

Querying the encode-thunk symtab, the aligned `sv*` stores resolve to **9 placements** each — one per
`s0_ldst` slot of the seven wide formats and the three narrow N-formats that carry a store-capable slot:

```
nm libisa-core.so | rg 'Opcode_ivp_sv2nx8_i_Slot_.*_encode' | rg -o 'Slot_[a-z0-9_]+'
  →  Slot_f0_s0_ldst  Slot_f1_s0_ldstalu  Slot_f2_s0_ldst  Slot_f3_s0_ldst
     Slot_f6_s0_ldst  Slot_f7_s0_ldst  Slot_n0_s0_ldst  Slot_n1_s0_ldst  Slot_n2_s0_ldst   (9)
```

`s0` is the **only** store-capable slot in the FLIX grid: a store therefore co-issues with a load (`s1`),
a MAC (`s2`), and 1–3 ALU ops (`s3+`) in one wide bundle — the structural basis of a software-pipelined
copy/transform loop that reads, computes, and writes back every cycle. The F1 variant is named
`f1_s0_ldstalu` (a fused ld/st+ALU slot). The placement total over the 92 B07 mnemonics is **746**
(summed `nm | rg -c` per mnemonic) — B07's contribution to the certified `12569` placement cover
([coverage-tally §1](../core/coverage-tally.md#1-the-five-headline-numbers--re-derived-from-the-binary)).
`[HIGH/OBSERVED]`

### 3.2 The data-shape selector ladder

Reading the `F0_S0_ldst` `WORD0` templates byte-exact, the data-shape lives in a field high in word0
and steps by `0x40000` per shape:

```
ivp_sv2nx8_i      WORD0 = 0x10a20000     (8-bit, "2nx8")
ivp_svnx8s_i      WORD0 = 0x10a60000     (+0x40000  -> signed byte "nx8s")
ivp_svnx8u_i      WORD0 = 0x10aa0000     (+0x40000  -> unsigned byte "nx8u")
ivp_svn_2x16s_i   WORD0 = 0x10ae0000     (+0x40000  -> signed halfword)
ivp_svn_2x16u_i   WORD0 = 0x10b20000     (+0x40000  -> unsigned halfword)
```

These are **distinct iclasses** (the disassembler maps each `WORD0` back to its own mnemonic via the
`Slot_f0_s0_ldst_decode` classifier), not a single global dtype bit OR-ed onto one base — the same
[two-tier selector model](../core/flix-encoding.md#62-the-two-tier-selector-model) the MAC batch
documents. A reimplementer's assembler carries the full `(mnemonic, slot) → template` table; it cannot
synthesize `svnx8u` from `sv2nx8` by adding a constant across all formats (the per-format packing
diverges in the narrow N-slots).

### 3.3 The four addressing modes and the post-increment bit

The four AGU modes are the suffix `_i`/`_ip`/`_x`/`_xp`. The cleanest read is the `sv2nx8` family in the
`F0_S0_ldst` slot:

```
ivp_sv2nx8_i   WORD0 = 0x10a20000   immediate offset,            no base update
ivp_sv2nx8_ip  WORD0 = 0x10f44100   immediate offset, post-INC   (base += imm)
ivp_sv2nx8_x   WORD0 = 0x10f00000   register stride,             no base update
ivp_sv2nx8_xp  WORD0 = 0x10f00100   register stride, post-INC    (base += stride)
```

The **post-increment flag is bit `0x100`** of word0: `_xp − _x = 0x10f00100 − 0x10f00000 = 0x100`. The
immediate `_i`/`_ip` forms use a *different* base template (`0x10a2…`/`0x10f4…`) because the immediate
mode steals encoding space for the `bimm4` offset field. A reimplementer models the AGU as:

```c
// AGU effective-address + base-update for the four store addressing modes.
//   ar_base, ar_stride : AR scalars (32-bit, regfile idx 0)
//   imm4               : 4-bit signed immediate, SCALED by the vector byte-size (64 for a 2nx8)
//   *mem               : destination
uint32_t store_agu(uint32_t *ar_base, uint32_t ar_stride, int imm4, mode_t mode,
                    const vec512 data, void *membank)
{
    uint32_t ea;                                   // effective address for THIS store
    switch (mode) {
      case MODE_I:   ea = *ar_base + (imm4 << 6);              break;  // _i  : EA = base + imm*64
      case MODE_IP:  ea = *ar_base; *ar_base += (imm4 << 6);   break;  // _ip : store@base, base += imm*64
      case MODE_X:   ea = *ar_base + ar_stride;                break;  // _x  : EA = base + stride
      case MODE_XP:  ea = *ar_base; *ar_base += ar_stride;     break;  // _xp : store@base, base += stride
    }
    mem_write_512(membank, ea, data);              // unified ld/st datapath, write @stage 11
    return ea;
}
```

The `imm4 << 6` scaling is **OBSERVED live**: `IVP_SV2NX8_IP v0, a3, N` accepts `N ∈ {…,−64,0,64,128,
…,448}` and **rejects** `16`/`32` and `±512` — i.e. the immediate must be a multiple of 64 (one
512-bit vector) and fits a signed 4-bit field × 64 = `[−512, +448]` (the `−512` end is excluded, the
field is `[−8,+7]`). The encoded byte: `imm=0`→`…031f`-style byte0 bits[7:4]=`0`, `imm=64`→`1`,
`imm=128`→`2`, `imm=−64`→`0xf`, `imm=448`→`7` (live decode below). `[HIGH/OBSERVED by execution]`

### 3.4 The operand bit-windows (live)

The operand fields are deposited by the `field_set`/`operand_encode` path, **not** by the opcode-sel
thunk. Their windows are read from the `fld_*_Slot_f0_s0_ldst_get` thunk bodies (the `and`/`shr`/`shl`
chains):

| field getter | body | bit-window | role in a store |
|---|---|---|---|
| `fld_f0_s0_ldst_3_0` | `and $0xf` | bits[3:0] | low nibble of slot/format tag |
| `fld_f0_s0_ldst_7_4` | `shl $0x18; shr $0x1c` | bits[7:4] | the **AGU offset** (`bimm4`) field |
| `fld_f0_s0_ldst_31_10` | `shr $0xa` | bits[31:10] | the 22-bit opcode-selector region |
| `fld_ivp_sem_ld_st_i_bimm4` | `shl $0x18; shr $0x1c` | bits[7:4] | the immediate post-inc offset (= `_7_4`) |

Driving the on-device assembler to vary one operand at a time **proves** the windows live (the bundle is
the little-endian 8 bytes; the disassembler prints them grouped):

```
IVP_SV2NX8_IP v0,  a3, 0     →  029c5e94 9c04030f      (baseline)
IVP_SV2NX8_IP v1,  a3, 0     →  029c5e94 9c04130f      vt: byte1 hi-nibble  0x0 -> 0x1  (vec sel)
IVP_SV2NX8_IP v31, a3, 0     →  029c5e94 9e04f30f      vt=31: byte1 hi=0xf AND byte3 0x9c->0x9e (high bit)
IVP_SV2NX8_IP v0,  a4, 0     →  029c5e94 9c04040f      AR base: byte1 lo-nibble 0x3 -> 0x4 (a3->a4)
IVP_SV2NX8_IP v0,  a3, 64    →  029c5e94 9c04031f      offset: byte0 hi-nibble 0x0 -> 0x1  (+64 = +1*64)
IVP_SV2NX8_IP v0,  a3, 128   →  029c5e94 9c04032f      offset = 0x2  (+128 = +2*64)
IVP_SV2NX8_IP v0,  a3, -64   →  029c5e94 9c0403ff      offset = 0xf  (signed -1)
IVP_SV2NX8_IP v0,  a3, 448   →  029c5e94 9c04037f      offset = 0x7  (+448 = +7*64, max +ve)
```

So in this narrow store format, word0 = `…9c04031f`: byte0[7:4]=`bimm4` AGU offset, byte1[3:0]=AR base
reg, byte1[7:4]=`vt` source low nibble, byte3 bit0=`vt` high bit; the rest is the opcode selector. The
field positions are **single-bit isolable and monotone** — the AGU offset toggles exactly byte0[7:4],
the base reg exactly byte1[3:0], with no cross-talk.

### 3.5 The `word1 == 0` invariant

Stores in the 16-byte wide formats span two 32-bit words. Scanning **all 746** store encode thunks, the
upper-word write — when present — is *exactly* `movl $0x0,0x4(%rdi)` (`C7 47 04 00 00 00 00`): **184 of
746** thunks write word1, and every one writes `0x00000000`. The other 562 thunks (the formats whose
slot leaves word1 pre-zeroed, plus the 8-byte narrow formats) write only word0. So the template's
[`word1 == 0` invariant](template-and-partition.md) holds with zero exceptions: the upper lane carries
no store opcode-selector bits.

```
Opcode_ivp_sv2nx8_i_Slot_f2_s0_ldst_encode:
  movl $0x10740000,(%rdi)      ; word0 = shape/mode selector
  movl $0x0,0x4(%rdi)          ; word1 = 0  (explicit clear, wide format)
  ret
Opcode_ivp_sv2nx8_i_Slot_f0_s0_ldst_encode:
  movl $0x10a20000,(%rdi)      ; word0 only — word1 pre-zeroed by the bundle assembler
  ret
```

A reimplementer quotes only word0; if it shows a wide-slot template it states the `/0x00000000`
explicitly.

---

## 4. The unaligned-store idiom — prime → rotate-merge → flush

A store of a `vec` register to an **address that is not 64-byte aligned** cannot be a single aligned
write: the 512-bit payload straddles two aligned 64-byte memory rows. The Vision-Q7 solution is the same
rotate-merge machinery the loads use ([B06](b06-loads.md)), run in reverse: a `valign` carry register
`u` holds the bytes that belong to the *next* row, and each streaming store writes the aligned head of
the current row while stashing the unaligned tail in `u` for the following iteration. The span ends with
an explicit **flush** that writes whatever residual `u` still holds.

The three phases, with the real mnemonics:

```c
// Store a contiguous run of K vectors `src[0..K-1]` to an UNALIGNED byte address `dst`.
// Phase 1 (prime):  set up the valign carry register from the destination base.
// Phase 2 (stream): rotate-merge each vector, store the aligned head, carry the tail in u.
// Phase 3 (flush):  write the residual tail bytes that u still holds at the span end.

void unaligned_vec_store(vec512 *src, int K, uint8_t *dst) {
    valign u;
    uint32_t base = (uint32_t)dst;

    // ---- Phase 1: PRIME  (ivp_salign_ip / ivp_sapos_fp) ----
    //   compute byte_off = base & 63, seed u with the leading-row carry, advance base to the
    //   aligned row.  No vec source, no memory write — pure carry-register setup.
    ivp_salign_ip(&u, &base, /*imm*/0);          // u.byte_off = base & 63;  u.carry = {}

    // ---- Phase 2: STREAM  (ivp_sav*_xp / ivp_san*_ip), one per source vector ----
    for (int k = 0; k < K; k++) {
        // ivp_savnx8s_xp v=src[k], u, a=base, a=stride :
        //   merged   = (src[k] << byte_off) | u.carry     // rotate src left by byte_off, OR carry
        //   store the LOW 512 bits of `merged` to [base]   // the aligned head of this row
        //   u.carry  = (src[k] >> (512 - byte_off))        // bytes pushed past the row -> next carry
        //   base    += stride                              // post-increment
        ivp_savnx8s_xp(src[k], &u, &base, /*stride*/64);
    }

    // ---- Phase 3: FLUSH  (a final ivp_sa*/ivp_sav* with a zero/identity source) ----
    //   write the residual u.carry to the trailing partial row.  Without this the last
    //   byte_off bytes of the run are never committed to memory.
    ivp_savnx8s_xp(VEC_ZERO, &u, &base, /*stride*/0);   // commit the tail
}
```

> **GOTCHA — the flush is mandatory; omitting it loses the trailing `byte_off` bytes.** The streaming
> phase only ever writes the *aligned head* of each row; the unaligned tail of the **final** vector lives
> in `u.carry` and is committed only by the flush. A reimplementer that drops the flush silently
> truncates the last partial row by up to 63 bytes — a classic unaligned-store bug. This mirrors the
> load side's priming `lavn*` exactly, inverted (the merge itself is executed in §5).

> **NOTE — `salign`/`sapos` carry no `vec` operand and place into the load slot too.** The prime ops are
> the only B07 mnemonics with a `f4_s0_ld` placement ([§2.4](#24-valign-assisted-unaligned-store-savsavsan--salignsapos-14)).
> `sapos` ("store-align position", suffix `fp`) sets the byte-position state directly; `salign` seeds it
> from a base+offset. Both write only the `valign` register — no memory traffic — so they are
> *addressing-state* instructions, not stores in the data-movement sense.

---

## 5. The valign flush-tail merge — driven LIVE

`libfiss-base.so` is **callable in-process via ctypes with no license**
([coverage-tally §5](../core/coverage-tally.md#5-value-semantics-coverage--the-864864-execution-validated-cover)).
The single B07 value leaf is `module__xdref_sav` (`@0x85d060`) — the store-align-variable merge
function. Its SysV ABI, recovered from `objdump -d` of the body: `rdi`=context; `rsi`,`rdx`=input vector
structs (16×`uint32` lanes each — the new data `A` and the carry source `B`); `ecx`=byte offset (masked
`& 0x3f` then `& 0x7f` for the 128-byte two-row span); `r8d`=a first/flush flag; `r9d`=a secondary
count; three stack pointers (`0x148`/`0x150`/`0x158`) = the output structs (`out0`=byte-enable mask,
`out1`=the residual carry to write back into `u`, `out2`=the merged store payload). The body is the
textbook rotate-merge: `shl %cl` rotates by the alignment, `& 0x3f`/`& 0x7f` masks the byte position,
and `0xffffffff` populates the byte-enable lanes.

Driven live with two **distinguishable** input vectors — all-`0xAAAAAAAA` for the new data `A`,
all-`0xBBBBBBBB` for the carry `B` — sweeping the byte offset and counting how many 32-bit words of the
merged payload `out2` come from each source:

```
byte-merge transition  (out2 = merged store payload; A=new data, B=residual carry held in valign):
  off= 0   out2: A-words=16  B-words= 0   out2[0]=aaaaaaaa  out2[15]=aaaaaaaa   mask=ffffffff
  off= 4   out2: A-words=15  B-words= 1   out2[0]=bbbbbbbb  out2[15]=aaaaaaaa
  off= 8   out2: A-words=14  B-words= 2   out2[0]=bbbbbbbb  out2[15]=aaaaaaaa
  off=16   out2: A-words=12  B-words= 4   out2[0]=bbbbbbbb  out2[15]=aaaaaaaa
  off=32   out2: A-words= 8  B-words= 8   out2[0]=bbbbbbbb  out2[15]=aaaaaaaa
  off=48   out2: A-words= 4  B-words=12   out2[0]=bbbbbbbb  out2[15]=aaaaaaaa
  off=60   out2: A-words= 1  B-words=15   out2[0]=bbbbbbbb  out2[15]=aaaaaaaa
```

This is the **rotate-merge flush byte selection, proven by execution.** At byte-offset `off`, the merged
payload holds the low `off/4` words from the residual carry `B` and the high `16 − off/4` words from the
new data `A` — exactly a left-rotate of the new vector by `off` bytes with the head back-filled from the
carry register. The transition is monotone and proportional (off=0→16A/0B, off=32→8A/8B, off=60→1A/15B),
and the byte-enable mask `out0` is `0xffffffff` (all bytes of the head row are written). The companion
`out1` returns the bytes that rotate *off the top* of `A` — the new carry stashed back into `u` for the
next streaming store. A reimplementer models the streaming store as:

```c
// One streaming-store step (the executed sav semantics):
//   off  = base & 63                                   // current byte misalignment
//   merged = rotate_left_bytes(A, off) with head[0..off) <- u.carry     // = out2
//   store merged to [base & ~63]                        // aligned head row
//   u.carry = high `off` bytes of A (rotated off the top)  // = out1, fed to next step
```

`[HIGH/OBSERVED by execution]` — `module__xdref_sav` was loaded via ctypes and run on the inputs shown;
the monotone A/B split across the offset sweep is the certificate.

> **QUIRK — a store has no arithmetic value leaf; only `sav` (the merge) does.** The bulk stores
> (`sv*`/`ss*`/`sb*`) are **identity data-movement** — they write the source bits verbatim, so there is
> no per-element value function to validate and no `module__xdref_sv*`/`_ss*`/`_sb*` leaf exists
> (`nm libfiss-base.so | rg 'module__xdref_(sv|ss|sb)' = 0`). The single value leaf in B07 is `sav`,
> because the rotate-merge actually *computes* the stored bytes from two sources. This is why B07's
> value-leaf contribution to the `864` denominator is **1**, not 92.

---

## 6. The store pipeline — unified ld/st datapath

`libcas-core.so` (DWARF) tags the store and load semantics under a **single unified pipeline**:
`ivp_sem_ld_st_opcode_stage0 … stage11` and `ivp_sem_ld_st_semantic_stage{0,1,3,8,9,10,11}`. Stores and
loads share the AGU, the address generation, and the memory-bank datapath; they differ only in data
direction. The `_4_`/`_16_` symbol prefixes (`_4_ivp_sem_ld_st_semantic_stage*`,
`_16_ivp_sem_ld_st_semantic_stage*`) are the **format byte-length** keys (4-byte narrow vs 16-byte
wide), i.e. one schedule instance per bundle width.

The store reads its `vec` source at the unified vector read **stage 10**
([register-files §5](../core/register-files.md#5-readwrite-semantics-per-file): every architectural
`vec` source read lands @10) and the memory write completes by **stage 11** — the deepest stage in the
ld/st schedule. This gives the store a long pipeline depth but a write-only sink, so a store never
back-pressures the `vec` read ports. The valign-assisted stores additionally read/write the `valign`
register (file idx 4); `valign` is a `@10`-read / `@12`-write file
([register-files §5](../core/register-files.md#5-readwrite-semantics-per-file)), so the carry update has
a 2-cycle latency that the software pipeline hides by issuing independent rows back to back.

```
stage:   0      1      3        8        9        10              11
ld/st:   addr   addr   off-gen  bank     bank     vec READ @10    MEM WRITE @11
                 calc            select   route    (store source)  (store commit)
```

`[HIGH/OBSERVED]` on the `ld_st` stage span and the `vec` read@10; `[MED/INFERRED]` on the exact
stage→micro-op assignment (the tag names the stage; the per-stage RTL is not separately exposed).

---

## 7. Predicated / partial stores (`*t*`)

The 20 `*t*` mnemonics (`sv2nx8t_*`, `svnx8st_*`, `svnx8ut_*`, `svn_2x16st_*`, `svn_2x16ut_*`) take an
additional **`vbool`** mask operand and store **only the lanes whose mask bit is set** — a per-lane
byte-enable. This is the masked/partial store used at loop boundaries (tail iterations that must not
write past the valid element count) and for conditional scatter-free writes.

```c
// Predicated store: ivp_sv2nx8t_xp  v=data, vbool=mask, a=base, a=stride
void pred_store_2nx8t(vec512 data, vbool mask, uint32_t *base, uint32_t stride, void *mem) {
    uint32_t ea = *base;
    for (int lane = 0; lane < 64; lane++)             // 64 byte-lanes of a 2nx8 vec
        if (mask.bit[lane])                            // write only mask-on lanes
            ((uint8_t*)mem)[ea + lane] = data.byte[lane];
    *base += stride;                                   // _xp post-increment
}
```

> **NOTE — the `t` selector is a distinct opcode, not a flag on the base store.** `sv2nx8_i` =
> `0x10a20000`, `sv2nx8t_i` = `0x10200000` — a different selector word (different iclass), because the
> predicated form must encode the extra `vbool` operand field. A reimplementer assembles `*t*` from its
> own template, not by OR-ing a "predicated bit" onto the unmasked store.

---

## 8. Per-batch coverage tally

The three numbers, each with a binary witness:

| number | value | witness |
|---|---|---|
| B07 **mnemonics** `m` | **92** | `nm libisa-core.so \| rg -o 'Opcode_(ivp_(sv\|ss\|sb\|sa)…)_…_encode' \| sort -u \| wc -l` = 92 |
| B07 **placements** `p` | **746** | `Σ` over the 92 of `nm \| rg -c 'Opcode_<m>_Slot_*_encode'` |
| B07 **value leaves** `v` | **1** | `nm libfiss-base.so \| rg -c module__xdref_sav` = 1 (bulk stores have none) |

Verb breakdown (`nm` over the store-mnemonic roster): `sv*`=40 (aligned vec), `ss*`=32
(stream/transpose), `sb*`=6 (`vbool` bit-store), `sa*`/`sav*`/`san*`+`salign`/`sapos`=14 (valign flush +
alignment-state). `40 + 32 + 6 + 14 = 92`. ✓

`m = 92` rolls into the **1065** `ivp_` vector axis; `p = 746` rolls into the **12569** shipped placement
total ([coverage-tally §1](../core/coverage-tally.md#1-the-five-headline-numbers--re-derived-from-the-binary));
`v = 1` (the `sav` merge) into the **864** value-leaf denominator — the only B07 op with a non-identity
value function. The valid pairing stays `1534 ↔ 12569`; B07 touches no fold-source package, so its `p`
carries no `+73` authoring forms. `[HIGH/OBSERVED]`

---

## 9. The B06 / B07 / B19 boundary

B07 owns the **store** verbs; the symmetric **load** verbs are B06, and **indexed** stores are B19. The
split is mechanical and `nm`-checked — **no double count**:

```
ivp_ store-verb roster  (sv|ss|sb|sa)                                = 92   -> B07 (this page)
ivp_ load-verb roster   (lv|ls|la|lsr…)                              = 90   -> B06 (loads)
ivp_ scatter/gather roster (scatter*|gather*)                        = 23   -> B19 (indexed)
  overlap(B07, B06)  = comm -12  =  0   ✓
  overlap(B07, B19)  = comm -12  =  0   ✓
```

| token | meaning | batch |
|---|---|---|
| `sv*` / `ss*` / `sb*` / `sa*`/`sav*`/`san*` / `salign`/`sapos` | vector store / stream-store / mask-store / valign-assisted store + prime | **B07** |
| `lv*` / `ls*` / `la*` / `lavn*` / `lalign` | vector load / stream-load / valign-load + load-prime | B06 |
| `scatter*` / `gather*` (indexed, `gvr`/`b32_pr` index) | indexed write / read | B19 |

> **GOTCHA — `ss*` is a *strided/streaming* store, not a scatter.** The `ss` (stream) verb deinterleaves
> lanes across a **regular stride** (`_NxM` granularity) — it is contiguous-with-stride, fully described
> by `(base, stride, granularity)` and owned by B07. A **scatter** (`ivp_scatter*`, B19) takes a
> per-lane **index vector** from `gvr`/`b32_pr` and writes to arbitrary addresses — a different operand
> model entirely. Do not bin `ss*` into B19: the disambiguator is "regular stride field" (B07) vs
> "index-vector operand" (B19). The `nm` overlap is **0**.

---

## 10. Adversarial self-verification — 5 strongest claims, re-challenged

1. **"92 store mnemonics, 746 placements, 1 value leaf."** Derived: `nm | rg -o
   'Opcode_(ivp_(sv|ss|sb|sa)…)…' | sort -u | wc -l` = **92**; summing `rg -c` per mnemonic = **746**;
   `nm libfiss-base.so | rg -c module__xdref_sav` = **1**, and `rg -c 'module__xdref_(sv|ss|sb)'` = **0**
   (bulk stores are identity). *Challenge:* could a non-store `sa*`/`ss*` op (e.g. `ssn_…` vs `sub*` or
   `sel*`) have leaked in? The classifier matched only `sv|ss|sb|sa` *store* roots; `sub*`/`sel*`/`sll*`/
   `sqz*`/`sqrt*` start with `s` but are distinct verbs binned to B01/B21/B12/B24/B15 — verified by
   listing every `ivp_s*` mnemonic (190) and excluding the 98 non-store verbs. **Confirmed.**

2. **"Post-increment is bit `0x100`; the immediate is ×64-scaled, field `[−8,+7]`."** *Challenge:* maybe
   the post-inc is a different bit, or the scale is wrong. The templates: `sv2nx8_x`=`0x10f00000`,
   `sv2nx8_xp`=`0x10f00100`, delta exactly `0x100`. Live: `IVP_SV2NX8_IP v0,a3,N` accepts
   `N∈{−64,0,64,128,448}` and rejects `16`,`32`,`−448`,`±512` — proving the immediate is a signed-4-bit
   field scaled by 64 (`64×[−8,+7]=[−512,+448]`, with `−512` excluded), and the encoded byte0[7:4] runs
   `0→1→2→…7,0xf` exactly as `imm/64` mod 16. **Confirmed.**

3. **"The valign flush merge is a rotate-by-`off` with carry back-fill."** *Challenge:* maybe `sav`
   doesn't rotate, or the byte split is not proportional. Drove `module__xdref_sav` live with
   distinguishable `A`=`0xAA…`/`B`=`0xBB…` vectors over a byte-offset sweep: `out2` holds exactly `off/4`
   low words from `B` (carry) and `16−off/4` high words from `A` (data), monotone across
   `off∈{0,4,8,16,32,48,60}` (16A/0B → 8A/8B → 1A/15B). A non-rotating merge would not produce a
   proportional split. **Confirmed by execution.**

4. **"`sb*` stores `vbool`, not `vec`; `salign`/`sapos` carry no `vec`."** *Challenge:* maybe `sbn`
   takes a `vec` and the file is mis-typed. Live: `IVP_SBN_I vb0, a3, 0` assembles; `IVP_SBN_I v0,…`,
   `pr0,…`, `u0,…` are all **rejected** ("bad register name" / "invalid symbolic operand") — the source
   is unambiguously `vbool`. And `IVP_SALIGN_I u0, a3, 0` / `IVP_SAPOS_FP u0, a3` assemble with a
   `valign`+`AR` operand pair and **no** `vec` operand. **Confirmed.**

5. **"Stores are `s0`-exclusive; `sv*`=9 placements, `ss*`=8 (no N1), `sb*`=8 (no N0),
   `salign`/`sapos`=10 (+F4 load slot)."** *Challenge:* maybe a store places outside `s0`. `nm |
   rg -o 'Slot_[a-z0-9_]+'` over each family returns **only** `s0_ldst`/`s0_ldstalu`/`s0_ld`
   placements — no `s1`/`s2`/`s3`. The per-family slot sets are exactly as stated (sv: 9 incl N0+N1+N2;
   ss: 8 dropping N1; sb: 8 dropping N0; salign/sapos: 10 adding f4_s0_ld). The `s0`-exclusivity holds —
   stores can only issue in the one ldst slot. **Confirmed.**

---

## 11. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| 92 store mnemonics; 746 placements; 1 value leaf (`sav`) | `[HIGH/OBSERVED]` | `nm libisa-core.so` `Opcode_*` roster + classifier + per-mnemonic `rg -c`; `nm libfiss-base.so` |
| Every store is an `s0` (ldst-slot) opcode; per-family slot sets | `[HIGH/OBSERVED]` | `Opcode_*_Slot_<f>_s0_*_encode` symtab |
| Opcode-sel templates (byte-exact `F0_S0_ldst` `WORD0`) | `[HIGH/OBSERVED]` | `objdump -d` of the encode thunks |
| Post-inc bit `0x100`; immediate ×64-scaled, signed-4-bit field | `[HIGH/OBSERVED by execution]` | template deltas + `xtensa-elf-as` accept/reject sweep |
| Operand bit-windows (vt / AR base / bimm4) isolable & monotone | `[HIGH/OBSERVED by execution]` | `fld_*_get` bodies + live operand-variation decode |
| valign rotate-merge flush = rotate-by-`off` + carry back-fill | `[HIGH/OBSERVED by execution]` | `module__xdref_sav` driven live, monotone A/B byte split |
| `sb*` source = `vbool`; `salign`/`sapos` no `vec` (load-slot too) | `[HIGH/OBSERVED by execution]` | `xtensa-elf-as` operand-type acceptance |
| Bulk stores are identity (no `xdref` leaf); only `sav` has a value leaf | `[HIGH/OBSERVED]` | `nm libfiss-base.so \| rg module__xdref_(sv\|ss\|sb)` = 0 |
| `word1 == 0` across all 746 store thunks (184 write `movl $0x0`) | `[HIGH/OBSERVED]` | `objdump` scan of every store thunk |
| Unified ld/st pipeline stage0..11; `vec` read@10, mem write@11 | `[HIGH/OBSERVED]` structure; `[MED/INFERRED]` stage→µ-op | `libcas-core` `ivp_sem_ld_st_*_stageN` tags + register-files §5 |
| B06/B07/B19 disjoint (0 overlap each) | `[HIGH/OBSERVED]` | `comm -12` over the three `nm` rosters |

---

## 12. Cross-references

- [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the §3 per-instruction
  schema this page follows, the §4 partition slice (B07, task #622), and the `word1 == 0` /
  encode-thunk ABI invariants.
- [The FLIX VLIW Encoding (14 format / 46 slot)](../core/flix-encoding.md) — the `s0` ldst slot these
  ops occupy, the narrow N-formats, the F4 dual-load format the prime ops also slot into, and the
  encode-thunk `WORD0` ABI.
- [The Eight Register Files](../core/register-files.md) — the `vec` (store source), `vbool` (mask /
  `sb*` source), `valign` (the `u` carry register), and `AR` (base/stride) files, their geometry, and
  the `@10`-read / `@11`-write store stages.
- [ISA Batch 06 — Vector Loads + valign priming](b06-loads.md) — the mirror batch: same `s0` slot, same
  AGU modes, same valign machinery, opposite data direction; the load-side `lavn*` prime / `lalign`.
- [ISA Batch 19 — SuperGather scatter / gather](b19-scatter-gather.md) — the *indexed* store path
  (per-lane address vector from `gvr`/`b32_pr`), distinct from B07's contiguous/strided stores; the
  boundary is [§9](#9-the-b06--b07--b19-boundary).
- [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the certified `12569`
  placement / `864` value-leaf denominators this batch's `746` placements / `1` leaf contribute to.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED`/`INFERRED`/
  `CARRIED` tags and the proven-by-execution value lane used in [§5](#5-the-valign-flush-tail-merge--driven-live).

---

*Provenance: the encode templates, slot placements, addressing-mode bits and operand windows are
`[HIGH/OBSERVED]` — disassembled in-checkout from `libisa-core.so` (`ncore2gp/config/`) and confirmed
live with the on-device `xtensa-elf-as`/`xtensa-elf-objdump` under `XTENSA_CORE=ncore2gp`; the valign
flush-tail merge in [§5](#5-the-valign-flush-tail-merge--driven-live) is `[HIGH/OBSERVED by execution]`
— the `libfiss-base.so` `module__xdref_sav` leaf was loaded via ctypes and run on the inputs shown; the
ld/st pipeline stages are `[HIGH/OBSERVED]` from `libcas-core.so` DWARF symbol tags (stage→µ-op
`[MED/INFERRED]`). The `extracted/` carving is gitignored; counts are `nm | rg -c` against the binary
`.symtab`. All prose reads as derived from shipped-artifact static analysis (lawful interoperability RE).*
