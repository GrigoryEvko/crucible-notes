# ISA Batch 23 — Vector Integer Divide (the iterative seed → 4-step → remainder family)

This batch is the **vector integer iterative-divide** slice of the Vision-Q7 *Cairo* (`ncore2gp`)
512-bit FLIX vector ISA: the family that builds an integer `quotient`/`remainder` four bits at a time
out of a chain of issued instructions. There is **no single-instruction vector integer divide** on
this engine — division is an *unrolled, multi-instruction* radix-2 non-restoring (shift / compare /
conditional-subtract) sequence, and each issued op advances the quotient by exactly **four** bits,
which is what the `_4STEP` suffix names. Operation state is carried instruction-to-instruction in a
`(quotient, remainder)` accumulator **pair** held in two 512-bit `vec` registers (`vt` = partial
quotient, `vu` = running remainder/dividend residue).

The batch owns the single semantic group **`ivp_sem_divide`** — **14 mnemonics** — a perfect
non-overlapping cover (0 cross-membership with the adjacent
[B21 select/shuffle](b21-select-shuffle.md) crossbar group or any other batch). It hosts three
*temporal* forms across two *width* families and two *signednesses*, plus two remainder-emitting `Q`
seed forms:

* **`_4STEP0` (seed / first)** — reads the **source** operands (`vs` = divisor, `vr` = dividend),
  initialises the `(quotient, remainder)` pair, and runs the first block of four quotient bits.
* **`_4STEP` (middle step)** — read-modify-writes the running `(vt, vu)` pair plus a fresh dividend
  chunk `vr`, advancing four more quotient bits.
* **`_4STEPN` (step-N / last)** — the same datapath as `_4STEP` but the *terminal* block; it applies
  the final remainder fix-up / quotient correction. It differs from `_4STEP` **only** in the bundle's
  slot-tail byte (`b3` → `bb`, a single bit) — see §5.
* **`Q` / `SQ` seed forms** (`DIVNX16Q_4STEP0`, `DIVNX16SQ_4STEP0`) — the **remainder-producing**
  seed variants: the same compute, but the selector picks the remainder-emit mux so the residue is a
  first-class output rather than only a carry into the next step.

Everything below is grounded in the shipped binaries: the **encoding** from the
non-stripped `libisa-core.so` (`Opcode_ivp_<mnem>_Slot_f2_s2_mul_encode` thunks read byte-for-byte;
the `Iclass_IVP_<MNEM>_args` operand descriptors and the `regfiles[]` table walked directly), the
**value semantics** from the `opcode__ivp_div…__stage_5` reference bodies in `libfiss-base.so`
(disassembled in place), the **issue timing** from the per-stage `F2_F2_S2_Mul_27_IVP_DIV…_inst_stageN`
scoreboard bodies in `libcas-core.so`, and a byte-exact **encode/decode oracle** from the
device-native `xtensa-elf-as`/`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). All 14 round-trip
through that device oracle. Confidence tags per
[the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-round-trip, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic.** All five config DLLs are under
> `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/` (gitignored; reach with `fd --no-ignore`
> or an absolute path). `libfiss-base.so` (12 330 016 B, ET_DYN x86-64, **not stripped**; the 864
> `module__xdref_*` value leaves + the `opcode__*__stage_5` reference bodies): `readelf -SW`
> gives `.text` (VMA `0x190430`) and `.rodata` (VMA `0x88ff00`) **VMA == file-offset**, while
> `.data.rel.ro` (VMA `0xc17e80` ↔ file `0xa17e80`) carries the per-binary delta **`0x200000`** — **not**
> libtpu's `0x400000`. `libisa-core.so` (9 690 712 B, not stripped) carries the same `0x200000` `.data`
> delta, so `objdump -s -j .data` on the `Iclass_*_args` tables and the `regfiles[]` array must subtract
> `0x200000`; the encode thunks live in `.text` (VMA == file).

---

## 0. Scope boundary — integer iterative divide vs the fp reciprocal-seed family

The unifying property this batch owns is the **inline, table-free, multi-issue integer divide**: a
straight-line `shift / compare / conditional-subtract` chain that threads a `(vt, vu)` accumulator pair
across issued instructions. That is the structural test separating B23 from its nearest namesake:

* **The floating-point reciprocal/Newton seed family (`ivp_div0nxf16`, `ivp_divnnxf16`,
  `ivp_div0n_2xf32`, `ivp_divnn_2xf32`) is NOT here.** Those produce an fp16/fp32 **reciprocal seed**
  by indexing a lookup table (`module__xdref_div0_16f_16f @0x51fff0`, `…_div0_32f_32f @0x878340`,
  `…_recip0_1_1_16f_16f @0x520110`), and the Newton/QLI refine step
  (`module__xdref_divn_1_1_1_16f_16f_16f_16f_2 @0x520540`, `…_recipqli_… @0x87df20`) consumes a
  `recip_tab` LUT (`table__recip_tab @0x9553c0`). The B23 integer group has **no LUT and no `(a,b)`
  value leaf at all** — its math lives entirely inside the `opcode__…__stage_5` context body (§4). The
  fp seed slice is the domain of [B15 fp32 seeds (sp_lookup)](b15-sp-lookup.md) /
  [B14 fp16 seeds](b14-hp-lookup.md); do not conflate the two.
* **The integer multiply group (`ivp_sem_vec_mul`) is NOT here** even though B23 *rides the multiply
  slot* (F2/S2/Mul). The divide reuses the multiplier lane's datapath at issue time, but it is its own
  semantic group with its own selector class; the two rosters are disjoint.
* **The shared vector ALU (`ivp_sem_vec_alu`) is NOT here.** ADD/SUB/MIN/MAX/ABS/AVG and the
  saturating/wrapping/signedness machinery are the
  [Formal Semantics I](../semantics/group-semantics-i.md) domain; B23 is the only family that *iterates*
  to build a result across multiple issues. `[HIGH/CARRIED]`

The [partition classifier](template-and-partition.md) routes B23 by the `ivp_sem_divide` membership;
the integer-divide group is *exactly* the `_4STEP*`-suffixed mnemonics (the `div0`/`divn` fp roots are
glob-adjacent but reclassified to the fp seed batches).

> **GOTCHA — "divn" is two different things on this engine.** The token `divn` appears in both *this*
> batch (`ivp_divn_2x32x16{s,u}_4step*` = a 32-bit-dividend ÷ 16-bit-divisor **integer** divide) **and**
> in the fp family (`ivp_divnnxf16`, `ivp_divnn_2xf32` = the fp **Newton refine** step). They share no
> selector, no slot scatter, and no value path. The integer `divn_2x32x16` is the `N/2`-lane wide
> integer quotient; the fp `divn` is the iterative reciprocal correction. This page is **only** the
> integer one.

---

## 1. Roster and count verification

Enumerated directly from the `libisa-core.so` symbol table — every mnemonic below has exactly one
`Opcode_ivp_<mnem>_Slot_f2_s2_mul_encode` thunk and a matching `Iclass_IVP_<MNEM>_args` descriptor; the
`_4step0` / `_4step` / `_4stepn` anchors pin the form boundary so `divnx16s_4step` does not bleed into
`divnx16s_4stepn`:

**`ivp_sem_divide` (14):**

`DIVNX16Q_4STEP0 DIVNX16SQ_4STEP0` · `DIVNX16S_4STEP0 DIVNX16S_4STEP DIVNX16S_4STEPN` ·
`DIVNX16U_4STEP0 DIVNX16U_4STEP DIVNX16U_4STEPN` ·
`DIVN_2X32X16S_4STEP0 DIVN_2X32X16S_4STEP DIVN_2X32X16S_4STEPN` ·
`DIVN_2X32X16U_4STEP0 DIVN_2X32X16U_4STEP DIVN_2X32X16U_4STEPN`

`2 + 3 + 3 + 3 + 3 = 14`. All 14 carry exactly **one** encode thunk (at `f2_s2_mul`) and one iclass; 0
cross-membership with any other batch. The complementary `Opcode_ivp_div0nxf16_*` / `…_divnnxf16_*` /
`…_div0n_2xf32_*` / `…_divnn_2xf32_*` thunks belong to the fp seed family (§0) and are excluded.
`[HIGH/OBSERVED — nm roster]`

### 1.1 The lane-grid token decides width family

| token | lanes | element | dividend | divisor | quotient |
|-------|------:|---------|----------|---------|----------|
| `NX16` | 32 | `i16` | `i16` | `i16` | `i16` (+ `i16` remainder) |
| `N_2X32X16` | 16 | mixed | `i32` | `i16` | `i32` (the wide `N/2`-lane quotient) |

`DIVNX16*` is the canonical 32-lane 16-bit divide; `DIVN_2X32X16*` is the `N/2 = 16`-lane form whose
**dividend is 32-bit** and **divisor 16-bit**, producing a 32-bit quotient. The `2X32X16` token reads
"two-times-32" = the doubled (32-bit) lane against a 16-bit divisor. `[HIGH/OBSERVED — mnemonic +
regfile width]`

---

## 2. Shared encoding — FLIX format F2, slot S2 (Mul)

All 14 ops have **exactly one** placement: format **F2**, slot **S2**, slot-class **Mul** (the
multiply lane of the wide F2 4-slot bundle). Counted directly from the symbol table, every op
has placement count **1**, the single slot `f2_s2_mul`:

```
Opcode_ivp_divnx16s_4step0_Slot_f2_s2_mul_encode      @0x35efd0   (1 placement)
Opcode_ivp_divn_2x32x16u_4stepn_Slot_f2_s2_mul_encode @0x35efc0   (1 placement)
… (all 14 identical: f2_s2_mul, count 1)
```

The reference soft-model's slot-fill symbols confirm the placement verbatim:
`slotfill__F2__F2_S2_Mul_slot2__IVP_DIVNX16S_4STEP0 @0x5ab440` (libfiss-base). The bundle is 16 bytes = a
4-slot F2 word; the other three slots are NOP-padded by the assembler when a divide is issued alone, so
`objdump` prints `{ nop; nop; ivp_div…; nop }` (§5). This reuse of the **multiplier slot** is why the
iterative divide can co-issue with LdSt/ALU ops in the same bundle but never with a multiply.
`[HIGH/OBSERVED — placement count + slotfill symbol]`

### 2.1 State / exception gate

Every op of this batch shares the IVP vector-coprocessor gate: `STATE_IN = CPENABLE` (sampled early in
the pipeline), `EXC = Coprocessor1Exception` (raised — and the op squashed before any datapath effect —
iff the cp1 enable bit is clear). The `Iclass_IVP_DIVNX16S_4STEP0_stateArgs` symbol (`@0x845340`,
libisa-core) carries the CPENABLE state argument; it is *not* re-listed per op. There is **no**
division-by-zero exception argument (§4). `[HIGH/OBSERVED — stateArgs symbol]`

### 2.2 Operand classes — read from the iclass descriptor array

`Iclass_IVP_DIVNX16S_4STEP0_args` (`.data` VMA `0x845380` ↔ file `0x645380`, subtracting the `0x200000`
delta) is an array of **16-byte operand descriptors** `{const char* name; uint64 dir}` (the `dir` low
byte is an ASCII code: `0x6f` = `'o'` = output, `0x69` = `'i'` = input). Walked byte-for-byte,
the seed form resolves to exactly **five** descriptors, the first **two** marked `'o'`:

```
Iclass_IVP_DIVNX16S_4STEP0_args  @0x845380 (file 0x645380):
  op0  opnd_ivp_sem_divide_vu        dir=0x6f 'o'   <- remainder destination (2nd output)
  op1  opnd_ivp_sem_divide_vt        dir=0x6f 'o'   <- quotient destination  (1st output)
  op2  opnd_ivp_sem_divide_vs        dir=0x69 'i'   <- divisor   (seed only)
  op3  opnd_ivp_sem_divide_vr        dir=0x69 'i'   <- dividend chunk
  op4  opnd_ivp_sem_divide_lane_ctrl dir=0x69 'i'   <- 1-bit phase selector
```

The name pointers resolve in `.rodata` to `opnd_ivp_sem_divide_{vu,vt,vs,vr,lane_ctrl}`
(`@0x3cd680..0x3cd6dc`). The dual output (`vu` + `vt`) is therefore **not an interpretation** — it is
two `'o'`-marked descriptors in the encoding table. For the **step / step-N** forms the running
accumulator `vt`/`vu` are the implicit read-modify-write pair (the static iclass lists only the fresh
`vr` + `lane_ctrl` as explicit inputs, but the device toolchain requires all four tokens, §5).
`[HIGH/OBSERVED — descriptor directions + name strings]`

| op form | operands (asm order) | shape |
|---------|---------------------|-------|
| `_4STEP0` (seed, incl. `Q`/`SQ`) | `vt vu vr vs imm` | `vt`(o) `vu`(o) `vr`(i) `vs`(i) `lane_ctrl`(imm1) |
| `_4STEP` / `_4STEPN` (step) | `vt vu vr imm` | `vt`(**m** RMW) `vu`(**m** RMW) `vr`(i) `lane_ctrl`(imm1) |

> **NOTE — descriptor order vs assembler order differ.** The iclass descriptor array lists
> `vu, vt, vs, vr` (second-output-first, the same convention the
> [B21 DSEL dual-output](b21-select-shuffle.md) uses), but the device assembler accepts/prints the seed
> form as `vt, vu, vr, vs, imm` and the step form as `vt, vu, vr, imm`. This page uses the **assembler**
> order in all worked examples (§5), as that is what `xtensa-elf-objdump` round-trips.

### 2.3 Register files — the operand classes

The `regfiles[]` descriptor table in `libisa-core.so` (`.data.rel.ro` VMA `0x74a800` ↔ file `0x54a800`,
**8 entries × 56 bytes**, between `regfile_views @0x74a780` and `funcUnits @0x74a9c0`) gives the
bit-width and count per file (`width @+0x18`, `count @+0x1c`), decoded:

| idx | file | width | count | used by this batch |
|----:|------|------:|------:|--------------------|
| 0 | `AR` | 32 | 64 | — |
| 1 | `BR` | 1 | 16 | — |
| **2** | **`vec`** | **512** | **32** | **`vt vu vs vr`** |
| 3 | `vbool` | 64 | 16 | — (divide has no per-lane predicate) |
| 4 | `valign` | 512 | 4 | — |
| 5 | `wvec` | 1536 | 4 | — (the wide accumulator file; not the divide path) |
| 6 | `b32_pr` | 64 | 16 | — |
| 7 | `gvr` | 512 | 8 | — |

All four register operands (`vt`, `vu`, `vs`, `vr`) live in the 512-bit **`vec` file (idx 2)**, count 32
— hence the 5-bit register selector fields below. The divide writes **two** `vec` results (`vt`, `vu`)
per issue; it reads no `vbool` (idx 3) because — unlike the `.T` predicated ALU/select ops — it has no
per-lane merge guard. `[HIGH/OBSERVED — width/count dwords decoded from the table]`

### 2.4 The selector constants — encode-thunk bodies

Each placement's `Opcode_ivp_<mnem>_Slot_f2_s2_mul_encode` is a two-instruction thunk `movl
$imm32,(%rdi); ret` (the high opcode word at `0x4(%rdi)` is always 0); the `imm32` is the format-local
opcode-selector template the assembler writes into the bundle. Read byte-for-byte and decoded
into the 9-bit primary selector `[29:21]` plus the step sub-selector field:

| mnemonic | encode `imm32` | sel `[29:21]` | sub `[20:15]` |
|----------|---------------:|:-------------:|:-------------:|
| `DIVNX16Q_4STEP0` | `0x00000000` | `0x000` | — |
| `DIVNX16SQ_4STEP0` | `0x00200000` | `0x001` | — |
| `DIVNX16S_4STEP0` | `0x00400000` | `0x002` | — |
| `DIVNX16U_4STEP0` | `0x00600000` | `0x003` | — |
| `DIVN_2X32X16S_4STEP0` | `0x00800000` | `0x004` | — |
| `DIVN_2X32X16U_4STEP0` | `0x00A00000` | `0x005` | — |
| `DIVNX16S_4STEP` | `0x03C20000` | `0x01e` | `0x04` |
| `DIVNX16S_4STEPN` | `0x03CA0000` | `0x01e` | `0x14` |
| `DIVNX16U_4STEP` | `0x03C28000` | `0x01e` | `0x05` |
| `DIVNX16U_4STEPN` | `0x03CA8000` | `0x01e` | `0x15` |
| `DIVN_2X32X16S_4STEP` | `0x03C30000` | `0x01e` | `0x06` |
| `DIVN_2X32X16S_4STEPN` | `0x03CB0000` | `0x01e` | `0x16` |
| `DIVN_2X32X16U_4STEP` | `0x03C38000` | `0x01e` | `0x07` |
| `DIVN_2X32X16U_4STEPN` | `0x03CB8000` | `0x01e` | `0x17` |

Three structural facts fall straight out of these bytes:

* **The six seed forms are six unique 9-bit selector classes `0x000..0x005`** — adjacent integers. The
  `Q` (`0x000`) and `SQ` (`0x001`) remainder seeds, the plain signed/unsigned 16-bit seeds (`0x002` /
  `0x003`), and the signed/unsigned 32/16 seeds (`0x004` / `0x005`) differ **only** in this selector;
  the operand nibbles are identical across all six on the same registers (§5(b)).
* **All eight step / step-N forms share the escape selector `[29:21] = 0x1e`** and are disambiguated by
  the **6-bit** sub-selector below it: `{0x04,0x05,0x06,0x07}` = `_4STEP` for `{NX16S, NX16U,
  2X32X16S, 2X32X16U}`, and `_4STEPN` = `_4STEP | 0x10` (`{0x14,0x15,0x16,0x17}`).
* The selector const indexes the shared divide mux; the same datapath decodes it into the internal
  control fan-out signals the schedule exposes (`op_q`, `op_signed`, `op_32x16`, `op_first_step`,
  `op_last_step`) — i.e. `{is-Q, is-signed, is-32×16, is-seed, is-last}` are exactly the bits the
  selector carries. `[HIGH/OBSERVED — selector decode]`

> **CORRECTION — the step sub-selector is `[20:15]` (6-bit), not `[19:15]` (5-bit).** A conceptual
> reading placed the step sub-selector in a 5-bit field `[19:15]`. The encode words above settle it: the
> `_4STEPN` sub-values `0x14/0x15/0x16/0x17` each require **bit 20** set (`0x14 = 10100b`), so the field
> spans `[20:15]`. The `_4STEPN = _4STEP | 0x10` relation (the "last step" bit is bit 20 relative to the
> field base, i.e. bit-4 of the sub-selector) is the literal cause of the `0x10` offset. `[HIGH/OBSERVED
> — encode-word decode of all eight step forms]`

### 2.5 The slot-local field map (`F2/S2/Mul`)

The slot-local bit-field assignment, reconstructed from the operand-field decode in the encode thunks
and confirmed by the §5 round-trips (all register fields are SPLIT and reassembled MSB-first, the IVP
convention):

```
  vt  (1st OUT, quotient dst)   : bits[8]   ++ bits[3:0]      (5-bit, MSB-first split)
  vu  (2nd OUT, remainder dst)  : bits[11:9] ++ bits[5:4]     (5-bit, MSB-first split)
  vs  (IN, divisor; _4STEP0)    : bits[19:15]                 (5-bit, contiguous)
  vr  (IN, dividend chunk)      : bits[14:12] ++ bits[7:6]    (5-bit, MSB-first split)
  lane_ctrl (phase selector)    : bit[20]                     (1-bit immediate)
  opcode selector               : bits[29:21]                 (9-bit primary class)
  step sub-selector             : bits[20:15]                 (6-bit; only when [29:21]=0x1e,
                                                               reusing the vs/lane_ctrl bit region)
```

For the **step / step-N** forms the `[29:21]=0x1e` escape repurposes the `vs`+`lane_ctrl` bit region as
the 6-bit sub-selector — consistent with those forms having no `vs` source operand (the divisor is
already folded into the threaded residue). `[HIGH/OBSERVED for the OUT/IN contiguous and split fields and
the 1-bit lane_ctrl position; MED/INFERRED for the exact MSB-first split of vt/vu/vr, pinned by §5
per-register differentials.]`

### 2.6 lane_ctrl is a true 1-bit field

The operand-semantic encode/decode helpers in `libisa-core.so` settle the width unambiguously:

```
OperandSem_opnd_sem_opnd_ivp_sem_divide_lane_ctrl_decode @0x337d10:  andl $0x1,(%rdi) ; ret
OperandSem_opnd_sem_opnd_ivp_sem_divide_lane_ctrl_encode @0x337d20:  andl $0x1,(%rdi) ; ret
```

The field is masked `& 0x1` on both decode and encode — a 1-bit field — versus the sibling
`opnd_ivp_sem_ld_st_i_imm2_decode @0x337d30` which masks `& 0x3` (2-bit). The device assembler agrees:
`imm ∈ {0,1}` assemble; `2`, `3`, `-1` are **rejected** (§5). In the reference body the bit is read from
the per-op context at offset `0x138`, masked `& 1`, and gates the initial sign-init / step phase
(`mov 0x138(%rdi),%edx ; and $0x1,%edx ; je …` @0x7d3510). It selects which sub-lane phase / scheduling
half the 4-step block operates on. `[HIGH/OBSERVED for the 1-bit width and the je gate; MED/INFERRED for
the precise "which half" meaning]`

---

## 3. Operand signatures (ICLASS)

Each opcode has its own ICLASS (1 opcode / iclass). Two signature shapes:

**SEED (`_4STEP0`, incl. `Q`/`SQ`):**

```
OUT : opnd_ivp_sem_divide_vu : vec(512b, W)   remainder destination
      opnd_ivp_sem_divide_vt : vec(512b, W)   quotient  destination
IN  : opnd_ivp_sem_divide_vs : vec(512b, R)   divisor
      opnd_ivp_sem_divide_vr : vec(512b, R)   dividend
      opnd_ivp_sem_divide_lane_ctrl : imm(1b) phase selector
STATE_IN : CPENABLE     EXC : Coprocessor1Exception
asm order: vt, vu, vr, vs, imm
```

**STEP / STEP-N (`_4STEP` / `_4STEPN`):**

```
IN/OUT : opnd_ivp_sem_divide_vt : vec(512b, R@USE10 / W@DEF12)   running quotient
         opnd_ivp_sem_divide_vu : vec(512b, R@USE10 / W@DEF12)   running remainder
IN     : opnd_ivp_sem_divide_vr : vec(512b, R)   next dividend chunk
         opnd_ivp_sem_divide_lane_ctrl : imm(1b)
STATE_IN : CPENABLE     EXC : Coprocessor1Exception
asm order: vt, vu, vr, imm   (4 tokens; the toolchain requires all four — §5)
```

Lane grid: `DIVNX16*` → 16-bit × 32 lanes; `DIVN_2X32X16*` → 32-bit dividend/quotient × 16 lanes with a
16-bit divisor. All operands are in the 512-bit `vec` file (idx 2). `[HIGH/OBSERVED — iclass descriptors
+ stateArgs]`

---

## 4. Semantics — the iterative non-restoring divide `[HIGH/OBSERVED structure]`

Unlike the small `(a, b, *out)` element leaves the ALU/select ops call, the integer divide has **no
`module__xdref_div…` value leaf** — there is no integer divide primitive in the 864-leaf oracle (only
the fp `div0_16f`/`div0_32f` reciprocal seeds and the `divn_…f` Newton steps exist, §0). The integer
math lives entirely inside the per-op **reference body** `opcode__ivp_div…__stage_5`, which consumes a
per-op context struct (the ISS register image) and writes the per-lane result words back into it.

Reference symbols (host-x86, `libfiss-base.so`):

```
opcode__ivp_divnx16s_4step0__stage_5   @ 0x7d3500   (seed compute body)
stateload__ivp_divnx16s_4step0         @ 0x7d5210   (CPENABLE sample)
regload__ivp_divnx16s_4step0           @ 0x7d5230   (vec-lane gather)
writeback__ivp_divnx16s_4step0         @ 0x7d53d0   (vt + vu lane scatter)
opcode__ivp_divn_2x32x16u_4stepn__stage_5 @ 0x7d1650 ; writeback__… @ 0x7d3310
```

### 4.1 What the seed body does, byte-anchored

Disassembled at `0x7d3500..0x7d5210` (the full seed body, ~7440 B), the instruction histogram is
`80 cmp / 80 sub / 47 setb / 32 neg / 63 test` — a long straight-line `compare / set-bit /
conditional-subtract` chain, the radix-2 non-restoring divide fully unrolled. The residue is threaded as
a **17-bit** value (the masks `or $0x10000`, `and $0x1ffff`, `and $0x1fffe`, `shl $0x11` read out of the
body confirm a 17-bit-wide running remainder, one guard bit above the 16-bit lane). Signed operands take
the sign-magnitude branch (`test $0x8000 ; je ; neg`) on both divisor (ctx `0xb6`) and dividend (ctx
`0xfa`); unsigned operands skip it. The opening of the body, byte-anchored:

```
7d3510:  mov 0x138(%rdi),%edx ; and $0x1,%edx ; je …      ; lane_ctrl & 1 gates the phase
7d3532:  movzwl 0xb6(%rdi),%r8d                            ; divisor  (zero-loaded, sign tested next)
7d3541:  test $0x8000,%r8d ; je 7d354d ; neg %r10d          ; signed: take divisor magnitude
7d3555:  movzwl 0xfa(%rdi),%r9d
7d355d:  test $0x8000,%r9d ; je ; neg %r9d                  ; signed: take dividend magnitude
7d3573:  cmp %eax,%r9d ; … setb %cl ; jae … ; sub %r9d,%eax ; per-bit compare / conditional subtract
7d358e:  or  $0x10000,%eax                                  ; set the 17th (guard) residue bit
7d3599:  shl $0x11,%ecx ; or %ecx,%eax                      ; shift the quotient bit into the residue word
7d35a9:  and $0x1ffff,%r10d                                 ; keep 17 bits of the running residue
```

### 4.2 The full seed → 4×step → quotient/remainder sequence (annotated C)

The complete division of a 16-bit dividend `Q` by a 16-bit divisor `D` is *one* `_4STEP0` plus three
`_4STEP` plus a `_4STEPN` (4 × 5 = 20 inner bits ≥ the 16 quotient bits + sign/correction headroom). The
per-lane algorithm reproduced from the `opcode__ivp_divnx16s_4step0__stage_5` body
(`module` = the real reference symbol named in each comment):

```c
/* B23 vector integer divide — per-lane reference, NX16 signed (DIVNX16S).
 * The hardware runs all 32 lanes of a 512-bit vec in parallel; this is ONE lane.
 * State carried across issued ops in the (vt, vu) vec pair:
 *   vt = partial quotient (bits accumulated so far, LSB-aligned per step)
 *   vu = running 17-bit residue (16-bit remainder + 1 guard bit) packed with carry
 * No module__xdref_div integer leaf exists — this body IS the oracle. */

typedef struct { uint32_t q;        /* partial quotient   (-> vt lane) */
                 uint32_t residue;  /* 17-bit running rem  (-> vu lane) */ } divpair_t;

/* opcode__ivp_divnx16s_4step0__stage_5 @0x7d3500  — SEED / first 4 bits */
divpair_t ivp_divnx16s_4step0(int16_t vs_divisor, int16_t vr_dividend, int lane_ctrl)
{
    /* sign-magnitude conversion (test $0x8000 ; neg) — UNSIGNED form skips this */
    uint32_t d = (vs_divisor < 0) ? (uint32_t)(-vs_divisor) : (uint16_t)vs_divisor;
    uint32_t r = (vr_dividend < 0) ? (uint32_t)(-vr_dividend) : (uint16_t)vr_dividend;

    (void)lane_ctrl;            /* &1 phase gate (ctx 0x138); selects scheduling half */
    uint32_t residue = 0;       /* 17-bit accumulator, guard bit = 0x10000 */
    uint32_t q = 0;

    /* radix-2 non-restoring: 4 inner bits per issued op (the "_4STEP") */
    for (int i = 0; i < 4; i++) {
        residue = (residue << 1) | ((r >> 15) & 1);   /* shift dividend MSB in (shl/or) */
        r <<= 1;
        residue &= 0x1ffff;                            /* and $0x1ffff — keep 17 bits   */
        int bit = (residue >= d);                      /* cmp ; setb/setae               */
        if (bit) residue -= d;                         /* conditional-subtract (sub)     */
        q = (q << 1) | bit;                            /* accumulate quotient bit        */
    }
    return (divpair_t){ .q = q, .residue = residue | 0x10000 };  /* or $0x10000 guard    */
}

/* opcode__ivp_divnx16s_4step__stage_5 @0x7d5590  — MIDDLE step, RMW (vt,vu) */
divpair_t ivp_divnx16s_4step(divpair_t acc, int16_t vr_dividend_chunk, int lane_ctrl)
{
    uint32_t d_from_residue = /* divisor folded into acc by the seed step */ 0;
    uint32_t r = (uint16_t)vr_dividend_chunk;
    (void)lane_ctrl;
    for (int i = 0; i < 4; i++) {                      /* 4 more quotient bits           */
        acc.residue = ((acc.residue << 1) | ((r >> 15) & 1)) & 0x1ffff;
        r <<= 1;
        int bit = (acc.residue >= d_from_residue);
        if (bit) acc.residue -= d_from_residue;
        acc.q = (acc.q << 1) | bit;
    }
    return acc;                                        /* writeback both vt and vu       */
}

/* opcode__ivp_divnx16s_4stepn__stage_5 @0x7d78e0  — LAST step + terminal fix-up */
divpair_t ivp_divnx16s_4stepn(divpair_t acc, int16_t vr_dividend_chunk, int lane_ctrl)
{
    acc = ivp_divnx16s_4step(acc, vr_dividend_chunk, lane_ctrl);  /* same inner 4 bits   */
    /* terminal correction: restore sign of quotient/remainder for the signed form
     * (sign-of-quotient = sign(dividend) ^ sign(divisor); sign-of-remainder = sign(dividend),
     * truncation toward zero — the family default; not bit-pinned). */
    acc.q       = apply_quotient_sign(acc.q);          /* neg if signs differed          */
    acc.residue = apply_remainder_sign(acc.residue);   /* neg if dividend was negative   */
    return acc;                                        /* final quotient -> vt, rem -> vu */
}

/* Whole 16-bit divide: seed + 2 middle + last  (4 issued ops × 4 inner bits = 16 + headroom) */
divpair_t divide16(int16_t Q, int16_t D, int lane_ctrl) {
    divpair_t a = ivp_divnx16s_4step0(D, Q, lane_ctrl);      /* bits 15..12 */
    a = ivp_divnx16s_4step (a, Q << 4,  lane_ctrl);          /* bits 11.. 8 */
    a = ivp_divnx16s_4step (a, Q << 8,  lane_ctrl);          /* bits  7.. 4 */
    a = ivp_divnx16s_4stepn(a, Q << 12, lane_ctrl);          /* bits  3.. 0 + sign fix-up */
    return a;  /* a.q = Q/D (trunc toward zero), a.residue = Q - (a.q * D) */
}
```

The `Q` / `SQ` seed forms (`DIVNX16Q_4STEP0` `0x000`, `DIVNX16SQ_4STEP0` `0x001`) keep the residue as a
first-class `vu` output from the *seed* rather than only carrying it into the next step (the "Q" mux
selects the remainder-emit path). The `S`/`U` split is the sign-magnitude branch above (signed extracts
the sign via `test $0x8000 ; neg`, works on magnitudes, restores sign at `_4STEPN`; unsigned
zero-extends and skips both). `[HIGH/OBSERVED for the shift/compare/subtract structure, the 17-bit
residue, the sign-magnitude branch, the lane_ctrl gate; MED/INFERRED for the exact remainder
sign/rounding convention and the Q-vs-non-Q output split.]`

> **QUIRK — fixed 4 issued ops, *not* one divide instruction; the step count is software's job.** A
> full 16-bit quotient needs **four** issued ops (`_4STEP0` + 2× `_4STEP` + `_4STEPN`), each emitting 4
> quotient bits, threaded through the `(vt, vu)` pair. The 32-bit `DIVN_2X32X16*` quotient needs **eight**
> (`_4STEP0` + 6× `_4STEP` + `_4STEPN`). The hardware does **not** loop internally to full precision —
> the issuing code is responsible for emitting exactly the right number of `_4STEP`s before the
> `_4STEPN`. Emit too few and the low quotient bits are simply absent; the only enforced terminal is the
> `_4STEPN` fix-up. `[HIGH/OBSERVED — the _4STEP* form set + the 4-bit-per-op body; MED/INFERRED for the
> exact 8-op count of the 32-bit form]`

> **QUIRK — division-by-zero produces a degenerate result, no trap.** Only `Coprocessor1Exception` is
> declared (the cp1 enable gate); there is **no** division-by-zero exception argument in the iclass. With
> `D = 0` the inner `residue >= d` compare is always true and every conditional-subtract fires (or never
> fires), yielding an all-ones / max-residue degenerate quotient; software must guard `divisor == 0`
> itself. Signed `INT16_MIN / -1` takes the magnitude path `|INT16_MIN|`, which wraps to `INT16_MIN`
> (no clamp), so the quotient is the wrapped value. `[MED/INFERRED — no DBZ exc arg observed; the
> degenerate value is reasoned from the compare/subtract structure, not bit-pinned]`

---

## 5. Worked bit-patterns — device-oracle round-trips

Every bundle below was assembled by `xtensa-elf-as` (`XTENSA_CORE=ncore2gp`,
`XTENSA_SYSTEM=…/XtensaTools/config`) and disassembled back to the exact mnemonic + operand spelling by
`xtensa-elf-objdump`. The hex is `objdump`'s 16-byte (128-bit) memory-order word; each divide
op alone → an F2 word with three NOP-filled sibling slots.

**(a) Seed, signed 16-bit:**

```
asm : ivp_divnx16s_4step0 v3, v2, v1, v0, 0
hex : 0006014220a0850618200410b334252f
dec : { nop; nop; ivp_divnx16s_4step0 v3, v2, v1, v0, 0; nop }
      vt=v3 vu=v2 vr=v1 vs=v0 lane_ctrl=0, selector[29:21]=0x002.
```

**(b) Seed variants on the SAME registers — the selector const is the ONLY change** (proves the 9-bit
class field placement; operand nibbles fixed):

```
ivp_divnx16sq_4step0 v3,v2,v1,v0,1 -> 0006014218a0850618200410b334252f  (sel 0x001, lane_ctrl=1)
ivp_divnx16q_4step0  v3,v2,v1,v0,0 -> 0006014200a0850618200410b334252f  (sel 0x000, lane_ctrl=0)
```

The three differ only in the slot-local selector + the lane_ctrl bit.

**(c) Seed, unsigned 16-bit, different regs + lane_ctrl=1:**

```
asm : ivp_divnx16u_4step0 v5, v4, v6, v7, 1
hex : 000601423b2885ca18200810b334252f
dec : vt=v5 vu=v4 vr=v6 vs=v7 lane_ctrl=1, selector=0x003.
```

**(d) Seed, unsigned 32/16:**

```
asm : ivp_divn_2x32x16u_4step0 v8, v9, v10, v11, 0
hex : 00060942152087d018201210b334252f   selector=0x005.
```

**(e) Middle STEP vs STEP-N — the `_4STEP`/`_4STEPN` bit is a single bundle byte:**

```
ivp_divnx16s_4step  v3, v2, v1, 0 -> 000639422220854618200410b334252f   (sel 0x1e, sub 0x04)
ivp_divnx16s_4stepn v3, v2, v1, 0 -> 000639422220854618200410bb34252f   (sel 0x1e, sub 0x14)
```

XOR of the two: **byte[12] `0xb3 → 0xbb` (one bit, `^0x08`)** — the slot-tail byte that carries the
"last step" bit. Every step / step-N pair differs in exactly this byte. `[HIGH/OBSERVED — byte-diff]`

**(f) lane_ctrl differential (1-bit field at slot-local `[20]`):**

```
ivp_divnx16s_4step v3,v2,v1,0 -> 000639422220854618200410b334252f
ivp_divnx16s_4step v3,v2,v1,1 -> 000639422a20854618200410b334252f
```

A single nibble flips (`2 → a`) = bit 20 of the slot. The device assembler **rejects** `imm ∈ {2,3,-1}`
for both the step (4-token) and seed (5-token) forms, confirming the 1-bit width.

**(g) Operand-position differentials (each isolates one field's bundle byte; seed form, asm order
`vt,vu,vr,vs`).** Baseline `ivp_divnx16s_4step0 v3,v2,v1,v0,0` = `0006014220a0850618200410b334252f`:

| change | byte affected | bytes |
|--------|---------------|-------|
| `vt v3→v4` | byte[7] `06→08` | `00060142 20a08508 …` |
| `vu v2→v5` | byte[10] `04→0a` | `… 18200a10 …` |
| `vr v1→v8` | byte[4] `20→24` | `00060142 24a08506 …` |
| `vs v0→v8` | byte[6] `85→87` | `00060142 20a08706 …` |

These match the MSB-first split field map (§2.5). `[HIGH/OBSERVED — per-register round-trip]`

> **CORRECTION — the vr/vs operand-differential labels.** A conceptual reading labelled the byte-4
> change `vs` and the byte-3 change `vr`. Re-probing per-register, in the assembler order
> `vt,vu,vr,vs` the **`vr`** (3rd token, `v1`) drives **byte[4]** (`20→24`) and the **`vs`** (4th token,
> `v0`) drives **byte[6]** (`85→87`). The labels were swapped; the §2.5 split map is the corrected
> ground truth.

### 5.1 Round-trip ledger

All 14 `ivp_sem_divide` opcodes assembled and disassembled cleanly under `ncore2gp` (`xtensa-elf-as` →
`xtensa-elf-objdump -d`), **14/14** mnemonics recovered, each a 16-byte F2 bundle with the divide in
slot S2 (Mul) and the three siblings = NOP. Encoding (selector consts, field map) cross-checked against
the encode-thunk bytes: 0 discrepancies. Operand arity/spelling pinned by the toolchain (seed = 5
tokens with the trailing 1-bit imm; step = 4 tokens).

---

## 6. Issue timing

The divide ops live in the **deep vector pipe**. `libcas-core.so` models each as a 13-stage function set
`F2_F2_S2_Mul_27_IVP_DIVNX16S_4STEP0_inst_stage0 … _inst_stage12` (the `stage_functions @0x21a3300`
table holds the 13 function pointers; the `_27` is the F2/S2/Mul slot-class index). The
`<INSTR_SCHEDULE>`/USEDEF shape is identical across all 14 (the selector const drives the control
fan-out, not the timing):

| event | stage |
|-------|------:|
| USE instr decode | 2 |
| USE `CPENABLE` (coproc enable gate) | 3 |
| USE `lane_ctrl` | 8 |
| USE `vr` (dividend chunk) | 10 |
| USE `vs` (divisor; SEED only) | 10 |
| USE `vt`,`vu` (accumulator; STEP forms) | 10 |
| internal control fan-out DEF (`op_q`/`op_signed`/`op_32x16`/`op_first_step`/`op_last_step`) | 9 |
| DEF `vt_out2`,`vu_out2` (staged bypass tap) | 11 |
| DEF `vt` (quotient result) | 12 |
| DEF `vu` (remainder result) | 12 |

**Result latency: inputs USE @10, results DEF @12 → a 2-cycle producer→consumer latency** with a staged
bypass at stage 11 (`vt_out2`/`vu_out2`). A dependent divide step that consumes the prior step's
`(vt, vu)` therefore needs the 2-cycle gap (or eats a bypass stall) — which is the structural cost of the
seed → step → step → step-N chain: each link is a 2-cycle dependency on the one before. `CPENABLE` is
sampled at stage 3, well ahead, so it never stalls. The schedule is identical for SEED vs STEP vs STEPN
vs `Q` vs 16- vs 32-width. `[HIGH/OBSERVED — per-stage symbols + the stage_functions table]`

> **QUIRK — the divide chain is latency-bound, not throughput-bound.** Because every `_4STEP` reads the
> previous op's `(vt, vu)` at USE 10 and produces them at DEF 12, a full 16-bit divide (4 chained ops)
> costs ~4 × 2 = 8 cycles of dependent latency on the critical path *per lane group*, independent of how
> many lanes the op processes in parallel. The 32-bit `DIVN_2X32X16*` (8 chained ops) doubles that. There
> is no internal unroll to hide it — the chain depth is the latency. `[HIGH/OBSERVED — the 2-cycle
> per-link latency; MED/INFERRED for the exact 8-op chain of the 32-bit form]`

---

## 7. Cross-references and divergences

* [B15 — fp32 Transcendental Seeds (sp_lookup)](b15-sp-lookup.md) — the fp32 reciprocal/Newton **seed**
  family. **Relationship:** B15's `div0`/`recip0`/`recipqli`/`divn` produce an fp32 reciprocal
  *approximation* by indexing a 128-entry `.rodata` LUT (`table__recip_tab @0x9553c0`,
  `module__xdref_div0_32f_32f @0x878340`), to be refined by Newton–Raphson / QLI. B23 is the **integer**
  analogue but takes the *opposite* implementation: no LUT, no `(a,b)` value leaf — an inline radix-2
  non-restoring `shift/compare/subtract` chain (§4). The two "div" families share a name root and the
  multiplier slot but **nothing** of the datapath.
* [Formal Semantics I — arith / MAC / load-store / gather](../semantics/group-semantics-i.md) — the
  shared `ivp_sem_vec_alu` reference compute (SUB = `a + ~b + 1`, the signed/unsigned extension, the
  saturation clamp constants) that B23's per-bit subtract structurally resembles but is *not* part of.
* [fiss Datapath — the 864-Leaf Value Oracle](../../iss/fiss-datapath-oracle.md) — the
  `module__xdref_*` element-semantics oracle; note B23 is the family that has **no** integer divide leaf,
  computing instead inside the `opcode__…__stage_5` context body.
* [Template & Partition](template-and-partition.md) — the 30-batch classifier; the `ivp_sem_divide`
  membership and the `div0`/`divn` fp-glob reclassification.
* [B21 — Select / Shuffle / Compress](b21-select-shuffle.md) — the preceding committed batch boundary;
  the source of the dual-output (`vu, vt`) descriptor-order convention reused here.

> **CORRECTION / DIVERGENCE LEDGER** (every entry `[HIGH/OBSERVED]`).
> 1. **Step sub-selector is `[20:15]` (6-bit), not `[19:15]` (5-bit)** — the `_4STEPN` sub-values
>    `0x14..0x17` require bit 20; the `_4STEPN = _4STEP | 0x10` relation is that bit (§2.4).
> 2. **Operand-differential vr/vs labels swapped** — in assembler order `vt,vu,vr,vs`, `vr` drives
>    byte[4] and `vs` drives byte[6] (§5(g)); an earlier reading had them reversed.
> 3. **Cross-page: B15 attributes the "`divn` Newton-step macro" to "B23 divide".** B23 is **integer
>    only** (`ivp_divn_2x32x16{s,u}_4step*`); the fp `divn`/`recipqli` Newton refine step is the
>    *separate* fp DIVN.NXF16 family (`module__xdref_divn_…f`, `recip_tab` LUT), which the fp seed batches
>    own. The `divn` token is overloaded (§0 GOTCHA); B15's reference to B23 should be read as "the
>    *integer* `divn_2x32x16` lives in B23", not the fp Newton step. To reconcile on the B15 page.
> 4. **No integer divide value leaf** — unlike the ALU/select ops, B23 has no `module__xdref_div…`
>    `(a,b,*out)` primitive; the only `xdref_div*` leaves are the fp reciprocal seeds. The integer math is
>    inside `opcode__…__stage_5` (§4).
> 5. **Result-latency reading** — the schedule encodes data/accumulator USE at stage 10 and the
>    `vt`/`vu` results DEF at stage 12 (a 2-cycle producer→consumer span), with the `vt_out2`/`vu_out2`
>    bypass tap at stage 11 (§6).
