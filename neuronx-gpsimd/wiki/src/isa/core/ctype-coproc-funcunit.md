# ctype / coproc / funcUnit / bypass Tables

A FLIX bundle decoded by the [libisa-core decode model](libisa-decode-model.md) hands the rest
of the toolchain `(opcode, slot, operand)` triples; the [register-files page](register-files.md)
binds each operand's `(regfile, index)`. This page closes the loop on the **last four metadata
tables** a reimplementer must model to *type*, *enable*, *schedule*, and *time* those operands:

* **`ctypes[]`** — the **64 custom register-value types** (TIE C-types). Every operand slot, and
  every register file, carries a `ctype` that fixes the operand's bit width, its byte alignment,
  the regfile it lives in, and how a value of that type is loaded, stored, moved, and converted.
* **`ctype_protos[]`** — the **651 value-access protocols**: for each `(ctype, access-kind)` pair
  the named instruction-sequence binding the C/TIE compiler expands to load/store/move/convert a
  value of that type. This is the per-*value-type* counterpart of the per-*state* transfer protos.
* **`coprocs[]`** — the **single coprocessor**, `{name="Vision", number=1}` (CP1). The one IVP32
  SIMD coprocessor that owns the six SIMD register files and the floating-point control state.
* **`funcUnits[]`** — the **single pipeline functional-unit hazard object**,
  `{name="XT_LOADSTORE_UNIT", num_copies=2}`. The dual-copy load/store unit is the only structural
  port hazard the ISA declares; it is the silicon basis for the `1 + 1` co-issue bound.
* **`bypass` groups / chunks / entry** — **structurally absent** in this config (all three count
  accessors return `0`). The forwarding network is *not* a table here; the authoritative timing
  model is the **per-operand `use_stage` / `def_stage`** fields embedded in `opcodes[]`.

Every count, base VMA, stride, field offset, and decoded string below is read **from the
shipped `libisa-core.so`** — the accessor bodies (count immediates, RIP-relative `lea` table
bases, struct field offsets and load widths), `nm -S` symbol geometry, and a `struct.unpack`
parse of the raw `.data.rel.ro` table bytes with the name pointers resolved into `.rodata`. The
host-side C-stub *implementation* of the ctype get/set/move/convert accessors lives in a
separate binary, `libctype.so`; its deep treatment is the cross-linked
[libctype CSTUB page](../../iss/libctype-cstub.md). No external or vendor source tree was
consulted — this is binary-derived prose only, lawful-interoperability RE.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string/immediate read from a shipped artifact; `INFERRED` = reasoned over
OBSERVED facts; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orienting context). Where folklore disagrees with the binary, **the binary wins**.

> **GOTCHA — the `.data.rel.ro` VMA↔file-offset delta is `0x200000` in `libisa-core.so`.** All
> four tables on this page live in `.data.rel.ro` (`readelf -SW`: VMA `0x67bb00` ↔ file
> `0x47bb00`). Their *name/regfile/ctype pointer fields* are link-time-absolute VMAs into
> `.rodata`, which is **VMA == file offset** (delta `0`). Before `xxd`/`objdump -s`-ing a table
> struct, subtract `0x200000`; resolving a name pointer out of it needs **no** adjustment. The
> delta is **per-binary** — `libctype.so` happens to share `0x200000` for its `.data`/`.bss`, but
> never generalize a delta across binaries.

This page deliberately does **not** restate the table-ABI export list or the `(opcode × slot)`
encode matrix — those are [the libisa Table Schema](libisa-table-schema.md) — nor the headline
`funcUnit/coproc` one-liners of the [Config Reference Sheet §5](config-reference-sheet.md#5-functional-unit--coprocessor-inventory).
It owns the **interior** of these four tables.

---

## 1. Key facts

The five count accessors and the four table geometries: every count is
a literal `mov $imm,%eax; ret` (or `xor %eax,%eax; ret` for the three empty bypass accessors), and
the table symbol **sizes** (`nm -S`) confirm `count × stride` byte-exact. Every row is
`[HIGH/OBSERVED]`.

| Table / accessor | Value | Source |
|---|---|---|
| `num_ctypes` | **64** (`0x40`) | `@ 0x3b67d0` → `b8 40 00 00 00` (`mov $0x40,%eax`) |
| `ctypes[]` table | base VMA `0x6cbb00`, **stride 64**, 64 entries | `ctype_name @ 0x3b67e0` `lea …<ctypes>; shl $6` |
| `ctypes` symbol size | `0x1000` = `64 × 0x40` | `nm -S libisa-core.so` |
| `num_ctype_protos` | **651** (`0x28b`) | `@ 0x3b6d30` → `mov $0x28b,%eax` |
| `ctype_protos[]` table | base VMA `0x67bb40`, **stride 32**, 651 entries | `ctype_proto_name @ 0x3b6d40` `lea …; shl $5` |
| `ctype_protos` symbol size | `0x5160` = `651 × 0x20` | `nm -S` |
| `num_coprocs` | **1** | `@ 0x3b6dc0` → `mov $0x1,%eax` |
| `coprocs[]` table | base VMA `0x67bb00`, **stride 16**, 1 entry | `coproc_name @ 0x3b6dd0` `lea …<coprocs>; shl $4` |
| `coprocs` symbol size | `0x10` = `1 × 0x10` | `nm -S` |
| `num_funcUnits` | **1** | `@ 0x3b5bd0` → `mov $0x1,%eax` |
| `funcUnits[]` table | base VMA `0x74a9c0`, **stride 16**, 1 entry | `funcUnit_name @ 0x3b5be0` `lea …<funcUnits>; shl $4` |
| `funcUnits` symbol size | `0x10` = `1 × 0x10` | `nm -S` |
| `num_bypass_groups` | **0** | `@ 0x3b61a0` → `31 c0` (`xor %eax,%eax`) |
| `num_bypass_group_chunks` | **0** | `@ 0x3b61b0` → `xor %eax,%eax` |
| `bypass_entry` | **0** (const-0 stub) | `@ 0x3b61c0` → `xor %eax,%eax` |
| `num_opcodes` (timing carrier) | **1534** (`0x5fe`) | `@ 0x3b61d0` → `mov $0x5fe,%eax` |

The `coprocs[]` base (`0x67bb00`) and `ctype_protos[]` base
(`0x67bb40`) are adjacent — `coprocs` is the single 16-byte record that opens `.data.rel.ro`,
`ctype_protos` follows immediately at `+0x40`.

---

## 2. The `ctypes[]` table — 64 custom register-value types

A **ctype** (TIE *C-type*) is the codec's value-type descriptor: it is what *types* an operand
slot. When the decoder produces an operand, the operand's ctype fixes (a) the storage/value
**width** in bits, (b) the byte **alignment**, (c) the **regfile** the value lives in, (d) the
regfile **view** (for the boolean packs), (e) how many underlying regfile entries the value
**spans**, and (f) whether it is a core scalar C-type or a SIMD/boolean value. The register-files
table's `ctype` field names the *default* ctype of each file; this table holds the full roster of
**64** ctypes, of which a single regfile carries many (e.g. `vec` is the regfile for 27 different
ctypes — `vec2Nx8`, `vecNx16`, `vecN_2x32`, `int8`, … — all different lane interpretations of the
same 512-bit storage).

### 2.1 Accessor-proven struct layout

Each ctype field has a one-line accessor; the offset and load width come straight out of the
`mov OFF(%rax,%rdi,1)` in each. The `ctype_name` accessor proves the geometry:

```
ctype_name @ 0x3b67e0:
  movslq %edi,%rdi
  lea    0x315316(%rip),%rax        # 6cbb00 <ctypes>     base VMA 0x6cbb00
  shl    $0x6,%rdi                  # idx * 0x40          stride 64
  mov    (%rax,%rdi,1),%rax         # +0x00  name ptr
```

```c
struct xtensa_ctype_internal {     // 64 bytes (stride 0x40), 64 entries
    const char *name;              // +0x00  e.g. "_TIE_xt_ivp32_xb_vec2Nx8"
    const char *package;           // +0x08  "xt_core" | "xt_booleans" | "xt_ivp32" | "VectraFP"
    int32_t     num_bits;          // +0x10  storage/value width in bits
    int32_t     alignment;         // +0x14  byte/bit alignment (== num_bits for the SIMD types)
    const char *regfile;           // +0x18  regfile this value type lives in (one of the 8, §register-files)
    const char *regfile_view;      // +0x20  regfile VIEW (BR2/BR4/BR8/BR16 for the sub-bool packs)
    int32_t     num_regs;          // +0x28  # of underlying regfile entries the value spans
    uint32_t    flags;             // +0x2c  0x0001 = core scalar C type ; 0x0000 = SIMD/bool
    char      **field_types;       // +0x30  field_types[j] (struct decomposition) or NULL
    char      **field_names;       // +0x38  field_names[j] or NULL
};  // symbol size 0x1000 = 64*0x40 confirms exactly 64 entries, no 65th.
```

The field accessors (addresses in `libisa-core.so`):

| Accessor | VMA | Field | Offset / width |
|---|---|---|---|
| `ctype_name` | `0x3b67e0` | `name` | +0x00 ptr |
| `ctype_package` | `0x3b6800` | `package` | +0x08 ptr |
| `ctype_num_bits` | `0x3b6820` | `num_bits` | +0x10 int32 |
| `ctype_alignment` | `0x3b6840` | `alignment` | +0x14 int32 |
| `ctype_regfile` | `0x3b6860` | `regfile` | +0x18 ptr |
| `ctype_regfile_view` | `0x3b6880` | `regfile_view` | +0x20 ptr |
| `ctype_num_regs` | `0x3b68a0` | `num_regs` | +0x28 int32 |
| `ctype_flags` | `0x3b68c0` | `flags` | +0x2c uint32 |
| `ctype_field_type` | `0x3b68e0` | `field_types[j]` | +0x30 ptr → `ptr[]` |
| `ctype_field_name` | `0x3b6900` | `field_names[j]` | +0x38 ptr → `ptr[]` |

### 2.2 The full 64-ctype roster

Byte-exact `mmap` parse of file `0x4cbb00`, name/package/regfile pointers resolved into
`.rodata`. The four packages partition cleanly: **13** core scalar C-types (`flags=0x0001`,
regfile `AR`), **5** core booleans (regfile `BR`), and **46** Vision IVP32 SIMD value types
(`flags=0x0000`, the six SIMD regfiles).

| idx | name | pkg | bits | align | regfile | view | num_regs | flags | fields |
|---:|---|---|---:|---:|---|---|---:|---|:--:|
| 0 | `_TIE_uint8` | `xt_core` | 8 | 8 | `AR` | `AR` | 1 | `0x0001` | |
| 1 | `_TIE_int8` | `xt_core` | 8 | 8 | `AR` | `AR` | 1 | `0x0001` | |
| 2 | `_TIE_uint16` | `xt_core` | 16 | 16 | `AR` | `AR` | 1 | `0x0001` | |
| 3 | `_TIE_int16` | `xt_core` | 16 | 16 | `AR` | `AR` | 1 | `0x0001` | |
| 4 | `_TIE_uint32` | `xt_core` | 32 | 32 | `AR` | `AR` | 1 | `0x0001` | |
| 5 | `_TIE_int32` | `xt_core` | 32 | 32 | `AR` | `AR` | 1 | `0x0001` | |
| 6 | `_TIE_uint64` | `xt_core` | 64 | 64 | `AR` | `AR` | **2** | `0x0001` | `{hi,lo}` |
| 7 | `_TIE_int64` | `xt_core` | 64 | 64 | `AR` | `AR` | **2** | `0x0001` | `{hi,lo}` |
| 8 | `_TIE_char` | `xt_core` | 8 | 8 | `AR` | `AR` | 1 | `0x0001` | |
| 9 | `_TIE_short` | `xt_core` | 16 | 16 | `AR` | `AR` | 1 | `0x0001` | |
| 10 | `_TIE_unsigned` | `xt_core` | 32 | 32 | `AR` | `AR` | 1 | `0x0001` | |
| 11 | `_TIE_int` | `xt_core` | 32 | 32 | `AR` | `AR` | 1 | `0x0001` | |
| 12 | `_TIE_void` | `xt_core` | 32 | 32 | `AR` | `AR` | 1 | `0x0001` | |
| 13 | `_TIE_xtbool` | `xt_booleans` | 8 | 8 | `BR` | `BR` | 1 | `0x0000` | |
| 14 | `_TIE_xtbool2` | `xt_booleans` | 8 | 8 | `BR` | `BR2` | 2 | `0x0000` | `{b1,b0}` |
| 15 | `_TIE_xtbool4` | `xt_booleans` | 8 | 8 | `BR` | `BR4` | 4 | `0x0000` | `{b3..b0}` |
| 16 | `_TIE_xtbool8` | `xt_booleans` | 8 | 8 | `BR` | `BR8` | 8 | `0x0000` | `{b7..b0}` |
| 17 | `_TIE_xtbool16` | `xt_booleans` | 16 | 16 | `BR` | `BR16` | 16 | `0x0000` | `{b15..b0}` |
| 18 | `_TIE_xt_ivp32_valign` | `xt_ivp32` | 512 | 512 | `valign` | `valign` | 1 | `0x0000` | |
| 19 | `_TIE_xt_ivp32_vboolN` | `xt_ivp32` | 32 | 32 | `vbool` | `vbool` | 1 | `0x0000` | |
| 20 | `_TIE_xt_ivp32_vbool2N` | `xt_ivp32` | 64 | 64 | `vbool` | `vbool` | 1 | `0x0000` | |
| 21 | `_TIE_xt_ivp32_xb_vec2Nx8` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | ← `vec` default |
| 22 | `_TIE_xt_ivp32_xb_vec2Nx8U` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 23 | `_TIE_xt_ivp32_xb_vecN_2x16` | `xt_ivp32` | 256 | 256 | `vec` | `vec` | 1 | `0x0000` | |
| 24 | `_TIE_xt_ivp32_xb_vecN_2x16U` | `xt_ivp32` | 256 | 256 | `vec` | `vec` | 1 | `0x0000` | |
| 25 | `_TIE_xt_ivp32_vboolN_2` | `xt_ivp32` | 16 | 16 | `vbool` | `vbool` | 1 | `0x0000` | |
| 26 | `_TIE_xt_ivp32_xb_vecN_2x64w` | `xt_ivp32` | 1024 | 512 | `wvec` | `wvec` | 1 | `0x0000` | |
| 27 | `_TIE_xt_ivp32_xb_int64w` | `xt_ivp32` | 64 | 64 | `wvec` | `wvec` | 1 | `0x0000` | |
| 28 | `_TIE_xt_ivp32_xb_int16` | `xt_ivp32` | 16 | 16 | `vec` | `vec` | 1 | `0x0000` | |
| 29 | `_TIE_xt_ivp32_xb_int16U` | `xt_ivp32` | 16 | 16 | `vec` | `vec` | 1 | `0x0000` | |
| 30 | `_TIE_xt_ivp32_xb_vecNx16` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 31 | `_TIE_xt_ivp32_xb_vecNx16U` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 32 | `_TIE_xt_ivp32_xb_int24` | `xt_ivp32` | 24 | 32 | `wvec` | `wvec` | 1 | `0x0000` | |
| 33 | `_TIE_xt_ivp32_xb_vec2Nx16w` | `xt_ivp32` | 1024 | 512 | `wvec` | `wvec` | 1 | `0x0000` | |
| 34 | `_TIE_xt_ivp32_xb_vec2Nx16Uw` | `xt_ivp32` | 1024 | 512 | `wvec` | `wvec` | 1 | `0x0000` | |
| 35 | `_TIE_xt_ivp32_xb_vec2Nx24` | `xt_ivp32` | 2048 | 512 | `wvec` | `wvec` | 1 | `0x0000` | |
| 36 | `_TIE_xt_ivp32_xb_int256` | `xt_ivp32` | 256 | 256 | `vec` | `vec` | 1 | `0x0000` | |
| 37 | `_TIE_xt_ivp32_xb_vecN_16x256` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 38 | `_TIE_xt_ivp32_xb_vecNx32` | `xt_ivp32` | 1024 | 512 | `wvec` | `wvec` | 1 | `0x0000` | |
| 39 | `_TIE_xt_ivp32_xb_vecNx32U` | `xt_ivp32` | 1024 | 512 | `wvec` | `wvec` | 1 | `0x0000` | |
| 40 | `_TIE_xt_ivp32_xb_vecNx48` | `xt_ivp32` | 2048 | 512 | `wvec` | `wvec` | 1 | `0x0000` | |
| 41 | `_TIE_xt_ivp32_xb_int48` | `xt_ivp32` | 48 | 64 | `wvec` | `wvec` | 1 | `0x0000` | |
| 42 | `_TIE_xt_ivp32_xb_int128` | `xt_ivp32` | 128 | 128 | `vec` | `vec` | 1 | `0x0000` | |
| 43 | `_TIE_xt_ivp32_xb_vecN_8x128` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 44 | `_TIE_VectraFP_xtfloat` | `VectraFP` | 32 | 32 | `vec` | `vec` | 1 | `0x0000` | scalar fp in vec lane |
| 45 | `_TIE_xt_ivp32_xb_vecN_2xf32` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 46 | `_TIE_xt_ivp32_xb_vecN_2xf16` | `xt_ivp32` | 256 | 256 | `vec` | `vec` | 1 | `0x0000` | |
| 47 | `_TIE_VectraFP_xb_f16` | `VectraFP` | 16 | 16 | `vec` | `vec` | 1 | `0x0000` | |
| 48 | `_TIE_xt_ivp32_xb_vecNxf16` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 49 | `_TIE_xt_ivp32_vbool1` | `xt_ivp32` | 1 | 8 | `vbool` | `vbool` | 1 | `0x0000` | |
| 50 | `_TIE_xt_ivp32_xb_int8` | `xt_ivp32` | 8 | 8 | `vec` | `vec` | 1 | `0x0000` | |
| 51 | `_TIE_xt_ivp32_xb_int8U` | `xt_ivp32` | 8 | 8 | `vec` | `vec` | 1 | `0x0000` | |
| 52 | `_TIE_xt_ivp32_xb_vecNx8` | `xt_ivp32` | 256 | 256 | `vec` | `vec` | 1 | `0x0000` | |
| 53 | `_TIE_xt_ivp32_xb_vecNx8U` | `xt_ivp32` | 256 | 256 | `vec` | `vec` | 1 | `0x0000` | |
| 54 | `_TIE_xt_ivp32_xb_int32v` | `xt_ivp32` | 32 | 32 | `vec` | `vec` | 1 | `0x0000` | |
| 55 | `_TIE_xt_ivp32_xb_int32Uv` | `xt_ivp32` | 32 | 32 | `vec` | `vec` | 1 | `0x0000` | |
| 56 | `_TIE_xt_ivp32_xb_vecN_2x32v` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 57 | `_TIE_xt_ivp32_xb_vecN_2x32Uv` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 58 | `_TIE_xt_ivp32_xb_int64` | `xt_ivp32` | 64 | 64 | `vec` | `vec` | 1 | `0x0000` | lane int64 |
| 59 | `_TIE_xt_ivp32_xb_vecN_4x64` | `xt_ivp32` | 512 | 512 | `vec` | `vec` | 1 | `0x0000` | |
| 60 | `_TIE_xt_ivp32_xb_int32pr` | `xt_ivp32` | 32 | 32 | `b32_pr` | `b32_pr` | 1 | `0x0000` | |
| 61 | `_TIE_xt_ivp32_xb_int64pr` | `xt_ivp32` | 64 | 64 | `b32_pr` | `b32_pr` | 1 | `0x0000` | ← `b32_pr` default |
| 62 | `_TIE_xt_ivp32_xb_gsr` | `xt_ivp32` | 512 | 512 | `gvr` | `gvr` | 1 | `0x0000` | ← `gvr` default |
| 63 | `_TIE_xt_ivp32_xb_wvecspill` | `xt_ivp32` | 1536 | 512 | `wvec` | `wvec` | 1 | `0x0000` | ← `wvec` default/spill |

**Regfile-binding census** (the `ctype.regfile` column, OBSERVED): `vec` 27, `AR` 13, `wvec` 11,
`BR` 5, `vbool` 4, `b32_pr` 2, `valign` 1, `gvr` 1 — total **64**. The eight regfile names are
*exactly* [the eight register files](register-files.md): `{AR, BR, vec, vbool, valign, wvec,
b32_pr, gvr}`. The `flags` census is **13 × `0x0001`** (the core scalar C-types) + **51 ×
`0x0000`** (SIMD/bool). The per-regfile **default** ctype that the register-files table cites is
present at the expected index (`vec` → idx 21, `wvec` → idx 63, `gvr` → idx 62, `b32_pr` → idx 61,
`valign` → idx 18, `vbool` → idx 20, `AR` → idx 4 `_TIE_uint32`, `BR` → idx 13 `_TIE_xtbool`).
This is the **reverse `ctype → regfile` map** the register-files page cross-links to here.

> **NOTE — `alignment` is in *bits* for the wide SIMD types and equals `num_bits` for the others;
> the wide `wvec` ctypes pin `alignment = 512`.** For the scalar/boolean types `alignment ==
> num_bits` (8, 16, 32, 64). For the SIMD types `alignment == num_bits` *except* the `wvec`
> ctypes (idx 26, 33, 34, 35, 38, 39, 40, 63) and `int24`/`int48`, where `alignment` is a coarser
> grain than the value width: `wvecspill` is `num_bits=1536` but `alignment=512` (the 512-bit
> machine register the 1536-bit accumulator spills through). A reimplementer aligns wide
> accumulator spills to **512 bits**, not 1536.

### 2.3 Composite ctypes — the `field_types` / `field_names` decomposition

Only **6** of the 64 ctypes carry non-NULL field arrays — the *decomposable* aggregate types. The
array lengths are confirmed by the `Ctype__TIE_*_fieldNames` / `_fieldTypes` symbol **sizes**
(`nm -S`): each pointer is 8 bytes, so size `÷ 8` is the field count.

| ctype | sym size | field count | `field_names` | `field_types` |
|---|---:|---:|---|---|
| `_TIE_uint64` (idx 6) | `0x10` | 2 | `{hi, lo}` | `{uint32, uint32}` |
| `_TIE_int64` (idx 7) | `0x10` | 2 | `{hi, lo}` | `{int32, uint32}` |
| `_TIE_xtbool2` (idx 14) | `0x10` | 2 | `{b1, b0}` | `{xtbool ×2}` |
| `_TIE_xtbool4` (idx 15) | `0x20` | 4 | `{b3..b0}` | `{xtbool ×4}` |
| `_TIE_xtbool8` (idx 16) | `0x40` | 8 | `{b7..b0}` | `{xtbool ×8}` |
| `_TIE_xtbool16` (idx 17) | `0x80` | 16 | `{b15..b0}` | `{xtbool ×16}` |

The 64-bit `AR` types decompose into `{hi, lo}` 32-bit halves — matching `num_regs = 2`, the
two-`AR` pair — and the boolean packs decompose into their N `xtbool` bits. The other **58** ctypes
are atomic (both arrays NULL). A reimplemented C/TIE front-end uses this aggregate descriptor to
spill/reload a `uint64` as two `AR` words and to address an individual boolean inside a pack.

---

## 3. The `ctype_protos[]` table — 651 value-access protocols

`ctype_protos[]` is the codec's **value-access binding**: for each `(ctype, access-kind)` pair it
names the protocol whose `proto_TIE_*` operand/tmp/insn-arg symbol set the C/TIE compiler expands
when it must load, store, move, or convert a value of that ctype. It is the per-*value-type*
analogue of the per-*state* transfer protos that handle the two wide save/restore user-states
([states / sysregs page] — the two proto families are orthogonal: state-transfer vs value-codec).

### 3.1 Struct layout

```
ctype_proto_name @ 0x3b6d40:
  lea    …<ctype_protos 0x67bb40>; shl $5  (idx*0x20); mov (%rax,%rdi,1)    +0x00 ptr
```

```c
struct xtensa_ctype_proto_internal {   // 32 bytes (stride 0x20), 651 entries
    const char *name;        // +0x00  "_TIE_<pkg>_<ctype>_<kind>[_<other>]"
    const char *ctype;       // +0x08  the source/owning ctype name
    const char *kind;        // +0x10  protocol-kind STRING (see §3.2)  — a char*, NOT an int!
    const char *other_type;  // +0x18  the target ctype (convert protos), else ""
};  // symbol size 0x5160 = 651*0x20 confirms 651 entries.
```

> **CORRECTION — `ctype_proto.kind` is a `char*` string, not an `enum int`.** The
> `ctype_proto_kind` accessor (`0x3b6d80`) does `mov 0x10(...),%rax` — a **full 8-byte pointer**
> load. Reading the field as `int32` yields garbage (the low 32 bits of a `.rodata` pointer). The
> kind is one of the twelve strings `{rtor, mtor, rtom, loadi, storei, move, loadip, storeip,
> loadx, storex, loadxp, storexp}`.

### 3.2 The kind census

`mmap` parse of all 651 records (`kind` and `other_type` resolved into `.rodata`):

| count | kind | meaning |
|---:|---|---|
| 205 | `rtor` | register→register move/convert (cross-ctype when `other_type` is set) |
| 71 | `mtor` | memory→register convert (widen/pack on load) |
| 64 | `rtom` | register→memory convert (narrow/unpack on store) |
| 47 | `loadi` | immediate-offset load |
| 47 | `storei` | immediate-offset store |
| 47 | `move` | same-ctype move |
| 31 | `loadip` | load + post-increment-immediate |
| 31 | `storeip` | store + post-increment-immediate |
| 27 | `loadx` | indexed (register-offset) load |
| 27 | `storex` | indexed store |
| 27 | `loadxp` | indexed load + post-increment |
| 27 | `storexp` | indexed store + post-increment |
| **651** | | (sum, OBSERVED) |

**340** of the 651 protos carry a non-empty `other_type` (the convert protos — `rtor`/`mtor`/`rtom`
between two ctypes, e.g. `vbool1_rtor_uint32`, `vecNx32U_mtor_xb_vecNx48`, `vec2Nx24_rtom_xb_wvecspill`);
the remaining **311** are the same-type access protos (load/store/move).
`[HIGH/OBSERVED counts; MED/INFERRED convert direction]`

The `rtor 205 / loadi 47 / storei 47` triple is the **authoritative link** to the host C-stub
library: `libctype.so` implements *exactly* these three kinds as 299 `Function_TIE_*` stubs (the
other 9 kinds are table-only, synthesized caller-side). See §6 and the
[libctype CSTUB page](../../iss/libctype-cstub.md).

---

## 4. The `coprocs[]` table — the single `Vision` coprocessor

The config declares **exactly one** coprocessor. Its 16-byte record opens `.data.rel.ro`.

### 4.1 Struct + decoded entry

```c
struct xtensa_coproc_internal {    // 16 bytes (stride 0x10), 1 entry
    const char *name;    // +0x00
    int32_t     number;  // +0x08
};  // symbol size 0x10 = 1*0x10 confirms exactly ONE coproc.
```

Raw bytes `coprocs @ VMA 0x67bb00` (file `0x47bb00`):

```
0047bb00:  0b b8 3b 00 00 00 00 00   01 00 00 00 00 00 00 00
           ^^^^^^^^^^^ name ptr      ^^^^^^^^^^^ number = 1
           = 0x003bb80b -> ".rodata" -> "Vision\0"
```

```
coprocs[0] = { name = "Vision", number = 1 }    (CP1)
```

`Vision` is the IVP32 SIMD coprocessor. It is the `coproc` tag carried on **all
six** SIMD register files (`vec`, `vbool`, `valign`, `wvec`, `b32_pr`, `gvr` — see
[register-files §3](register-files.md#3-the-eight-register-files--the-authoritative-roster)) and on
the 19 IVP32 floating-point control/status states (`RoundMode`, the sticky flags + enables, the
`IVP_FS*` saved-state). `AR` and `BR` are the *core* files (`coproc = ""`, the empty string — not
NULL); they belong to no coprocessor.

### 4.2 `coproc` ↔ `CPENABLE` ↔ state — the 1-vs-7 reconciliation

The config has two different "coprocessor" counts that measure two different things; they do not
conflict:

| Count | Value | What it measures |
|---|---:|---|
| `num_coprocs` (this table) | **1** | named coprocessors that own state / a regfile in *this* config |
| `CPENABLE` width / `XCHAL_CP_MAXCFG` | **7** | the architectural coprocessor-enable register width (CP0..CP6) |

The Xtensa core option fixes **7** coprocessor-ID slots — the `CPENABLE` enable bitmask has seven
bits (CP0..CP6). But in the Vision-Q7 `ncore2gp` config exactly **one** of those slots is
*populated* with a named coprocessor that defines state and owns register files: `Vision`,
`number = 1`, i.e. it occupies **CP slot 1** of the 7-bit `CPENABLE`. The other six bits are
unallocated/reserved (no named coproc, no state, no regfile).

So a reimplemented `CPENABLE` model needs exactly **one meaningful enable bit (CP1)**: a vector op
faults or stalls if `CPENABLE` bit 1 (Vision) is clear. (`coproc 0` is conventionally the
core/no-coproc slot; `Vision` is `number = 1`.)
`[HIGH/OBSERVED name/number; MED/INFERRED bit index 1 ↔ Vision]`

> **GOTCHA — do not infer seven coprocessors from `XCHAL_CP_MAXCFG = 7`.** Six of the seven
> `CPENABLE` slots are empty in this config. The shipped `coprocs[]` table has one entry, and
> `num_coprocs` is the arbiter: model **one** coprocessor. The seven is the *enable-register
> width*, not the coprocessor *count*.

---

## 5. The `funcUnits[]` table — the dual-copy load/store unit

The config declares **exactly one** pipeline functional-unit hazard object.

### 5.1 Struct + decoded entry

```c
struct xtensa_funcUnit_internal {  // 16 bytes (stride 0x10), 1 entry
    const char *name;       // +0x00
    int32_t     num_copies; // +0x08
};  // symbol size 0x10 = 1*0x10 confirms exactly ONE funcUnit.
```

Raw bytes `funcUnits @ VMA 0x74a9c0` (file `0x54a9c0`):

```
0054a9c0:  d3 d0 3c 00 00 00 00 00   02 00 00 00 00 00 00 00
           ^^^^^^^^^^^ name ptr      ^^^^^^^^^^^ num_copies = 2
           = 0x003cd0d3 -> ".rodata" -> "XT_LOADSTORE_UNIT\0"
```

```
funcUnits[0] = { name = "XT_LOADSTORE_UNIT", num_copies = 2 }
```

The **only** structural functional-unit hazard the ISA models is the load/store
unit, with **two copies** — the two parallel memory pipes (the `_0`/`_1` dual LSU). There is **no**
separate ALU / Mul / Ld funcUnit object: ALU, Mul, Ld, LdSt are FLIX *issue slots*
([flix-encoding](flix-encoding.md)), not funcUnit hazard records. The `num_copies = 2` is the
silicon basis for the `1 + 1` co-issue bound (§7).

### 5.2 Per-opcode funcUnit binding — who uses the LSU

Each opcode in `opcodes[]` (base `0x6ce6c0`, stride 72) carries a `num_funcUnit_uses` count
(`+0x30`) and a `funcUnit_use[]` array (`+0x38`), each use a `{const char* unit; int32 stage}`
record of stride 16. Sweeping all **1534** opcodes:

| `num_funcUnit_uses` | opcode count |
|---:|---:|
| 0 | **1291** |
| 1 | **238** |
| 2 | **5** |

Every one of the **248** `funcUnit_use` records names the *same* unit, `XT_LOADSTORE_UNIT`, every
one at **stage 0**. So:

* **1291 opcodes use no functional unit** — the ALU / Mul / predicate ops. Their hazard is the
  FLIX issue grid + the per-operand stage scoreboard (§6), *not* a funcUnit object.
* **238 opcodes occupy one LSU copy** — the scalar + vector loads/stores (`l32i.n`, `s32i.n`,
  `l16ui`, `l32r`, `l32ex`, `s32ex`, the `ivp_l*` / `ivp_s*` vector loads/stores, …).
* **5 opcodes occupy *both* LSU copies** — the dual-pumped 2-vector (`l2*`) loads that consume
  both memory pipes in one issue:

  ```
  ivp_l2a4nx8_ip   ivp_l2au2nx8_ip   ivp_l2au2nx8_ipi   ivp_l2u2nx8_xp   ivp_l2au2nx8_xp
  ```

A reimplemented hazard model treats the funcUnit as **pure load/store-port occupancy on the 2-copy
LSU**: a single load/store takes one copy; the five dual loads need *both* copies free.

---

## 6. The `bypass` table is structurally absent — timing lives per-operand

### 6.1 The bypass-group table is empty

```
num_bypass_groups       @ 0x3b61a0 :  xor %eax,%eax ; ret   -> 0
num_bypass_group_chunks @ 0x3b61b0 :  xor %eax,%eax ; ret   -> 0
bypass_entry            @ 0x3b61c0 :  xor %eax,%eax ; ret   -> 0
```

`libisa-core` declares **no** bypass groups and **no** group chunks; `bypass_entry()` is a
constant-0 stub. The forwarding/bypass network as an explicit group **table** is structurally
absent in this config — the same dead-stub pattern as the (empty) interface table.

> **GOTCHA — there is no forwarding-network *table* to parse; do not invent per-port reservation
> data.** Because the bypass-group table is empty, the per-port functional-unit reservation matrix
> a scheduler would read (the `MODULE_SCHEDULE` data) is **not populated** in this corpus. With
> the reservation data absent, the per-cycle co-issue ceiling is **not recoverable** beyond the
> structural bound the format roster + the 2 LSU copies give (§7). The `1 + 1` co-issue is the
> **sound, non-speculative** bound. This is the empty-`MODULE_SCHEDULE` wall.
> `[HIGH/OBSERVED empty table; OBSERVED-negative per-port model]`

### 6.2 The authoritative timing is the per-operand `use_stage` / `def_stage`

The real latency model lives in the `opcodes[]` operand-use sub-array (`opc+0x20`, stride 6):

```c
struct operand_use {     // 6 bytes per operand
    uint8_t  use_stage;     // +0x00  stage the source operand is READ   (0xFF = not a source)
    uint8_t  def_stage;     // +0x01  stage the dest operand is WRITTEN  (0xFF = not a def)
    uint16_t byp_use_grp;   // +0x02  bypass-group index — 0xFFFF (no group; table is empty)
    uint16_t byp_def_grp;   // +0x04  bypass-group index — 0xFFFF
};
```

Accessors: `opcode_operand_use_stage @ 0x3b6260` (`movzbl (.)`),
`opcode_operand_def_stage @ 0x3b6290` (`movzbl 0x1(.)`), `opcode_bypass_use_group_idx @ 0x3b6490`
(`movzwl 0x2(.)`), `opcode_bypass_def_group_idx @ 0x3b6460` (`movzwl 0x4(.)`). State-operand uses
(`opc+0x28`, stride 3) carry `use_stage / def_stage / allow_reorder`.

**Sentinels** (OBSERVED): `use_stage == 0xFF` means "not a source", `def_stage == 0xFF` means "not
a def"; `byp_use_grp == byp_def_grp == 0xFFFF` means "no bypass group" — every operand carries the
null `0xFFFF` index because there are **zero** groups to index into.

**Worked example — `add.n` (opcode idx 4), the three operand-use records:**

```
op0 (dst):  use_stage=0xFF  def_stage=4     byp=0xFFFF/0xFFFF   -> AR result ready @ STAGE 4
op1 (src):  use_stage=4     def_stage=0xFF  byp=0xFFFF          -> reads its source @ stage 4
op2 (src):  use_stage=4     def_stage=0xFF  byp=0xFFFF
```

The **`def_stage` of the destination operand is the producer result latency**, and the
**`use_stage` of a source operand is the consumer read latency**. The forwarding rule a reimplemented
scoreboard applies is purely stage arithmetic: *a value is forwardable from its `def_stage`
onward; a consumer at `use_stage` stalls iff `(def_stage − elapsed) > use_stage`.* No group/chunk
indirection is used.

### 6.3 The per-unit latency table (operand-0 `def_stage` distribution)

Sweeping the destination-operand `def_stage` over all opcodes yields the authoritative
per-class result latency:

| `def_stage` | opcode count | class |
|---:|---:|---|
| 1 | 15 | def-post / post-increment address update |
| 3 | 2 | (CPENABLE-check class) |
| **4** | 80 | **scalar `AR` result** (`add.n`, `addi.n`) |
| 5 | 95 | scalar (load-use / 2nd-stage scalar) |
| 6 | 8 | |
| **9** | 6 | **`valign` / align result** (`ivp_la_pp`, `lalign_i`) |
| **10** | 106 | **vector result @ execute stage** (vec / vbool / gather) |
| **11** | 346 | **vector add (int) / many SIMD** |
| **12** | 485 | **MUL / MAC (`wvec`) / DSEL** — the deepest common vector retire |
| **13** | 115 | **FP-vector add / MAC** — one stage deeper than the integer pipe |

Representative ops, `(use, def)` of the destination operand:

| op | dst `(use, def)` | latency class |
|---|---|---|
| `add.n` / `addi.n` | `(0xFF, 4)` | AR scalar, LAT 4 |
| `ivp_addnx16` | `(0xFF, 11)` | int vector add |
| `ivp_dselnx16` | `(0xFF, 12)` | DSEL / 2-cycle vec |
| `ivp_mul2nx8` | `(0xFF, 12)` | int MUL → `wvec`, LAT 12 |
| `ivp_mulan_2xf32` | `(10, 13)` | FP MAC, LAT 13 (accumulator read @10) |
| `ivp_addn_2xf32` | `(0xFF, 13)` | FP vector add, LAT 13 |
| `ivp_la_pp` | `(0xFF, 9)` | valign result, LAT 9 |
| `ivp_packvrnx48` | `(0xFF, 12)` | wvec → vec pack readout, LAT 12 |

The recurring source `use_stage = 10` across the SIMD ops is the execute stage where the vector
value is read; the recurring `def_stage = 4` for scalar `AR` is the early scalar result. These
per-operand stages are the **authoritative** source the cycle-accurate ISS turns into interlocks —
see the [cas timing-model page](../../iss/cas-timing-model.md) for the reconstructed scoreboard;
this page is the table it reconstructs *from*.
`[HIGH/OBSERVED stages; MED/INFERRED 1/3 role-labels]`

---

## 7. The `1 + 1` co-issue bound (why the LSU copies matter)

A wide FLIX format declares up to 5 *slots*, but the scheduler's real question is *how many ops
co-issue per functional unit per cycle*. The per-port reservation that would answer this — the
`MODULE_SCHEDULE` matrices — is **empty** (§6.1). With the reservation data absent, the **sound**
bound is the structural one: the single `XT_LOADSTORE_UNIT` with **`num_copies = 2`** caps the
bundle at **two memory ops** (the LdSt + Ld slots), plus the format's Mul + ALU slots —
**1 load/store-class op + 1 of each other class**, capped by the 2 LSU copies. The five dual `l2*`
loads consume *both* copies, so they cannot co-issue with another memory op. Do **not** infer a
tighter per-cycle throughput than the format roster + the 2 LSU copies allow; the empty
`MODULE_SCHEDULE` makes a per-port hazard model unrecoverable from this corpus. This bound is
shared with the [Config Reference Sheet §4.1](config-reference-sheet.md#41-the-co-issue-ceiling--1--1-is-the-sound-bound).
`[HIGH/OBSERVED rosters; OBSERVED-negative per-port model]`

---

## 8. The host-side codec — `libctype.so` get/set/move/convert (cross-reference)

The four tables above are the *declarations*. The **host-callable implementation** of the ctype
value protocols — the C-stub `get` (loadi), `set` (storei), `move`, and `convert` (rtor) functions
the native TIE simulator and `xt-gdb` invoke — lives in a **separate binary**, `libctype.so`
(388,648 bytes, not stripped, VERS_1.1). It is an **independent** host value-marshalling layer:
its only dynamic imports are libc (`malloc`, `fprintf`, `exit`, `stderr`); it does **not** link
`libisa-core`, `libcas-core`, or `libfiss-base`. The full treatment is the
[libctype CSTUB page](../../iss/libctype-cstub.md); the binding facts that anchor it to *this*
page's tables:

* **The proto-kind match is byte-exact.** `libctype` ships **299** `Function_TIE_*` stubs:
  **205 `_rtor`** + **47 `_loadi`** + **47 `_storei`** — *exactly* the `rtor 205 / loadi 47 /
  storei 47` counts of §3.2. The other 9 ISA-06 kinds (`mtor`, `rtom`, `move`, `loadip`/`storeip`,
  `loadx`/`storex`/`loadxp`/`storexp`) have **zero** host stubs — they are table-only, synthesized
  caller-side from `base_addr` arithmetic.
* **The dispatch tables are ctype-id-indexed.** Three `.bss` ftables, each `0x200 = 64 × 8`
  (`cstub_loadi_ftable @ 0x2562c0`, `cstub_storei_ftable @ 0x2564c0`, `cstub_rtor_ftable @
  0x256700`), indexed by **ctype id 0..63** — the same 64-row index space as `ctypes[]`. The
  populated `loadi` entries decode byte-exact to the §2.2 indices (`idx4 = uint32`, `idx18 =
  valign`, `idx21 = vec2Nx8`, `idx49 = vbool1`, `idx58 = int64`, `idx60/61 = int32pr/int64pr`,
  `idx62 = gsr`, `idx63 = wvecspill`).
* **The single-vs-multi-word flag.** `cstub_ctype_multi_word[64]` (`.data @ 0x256080`, 64 bytes)
  marks `idx {6, 7} ∪ {18..63}` (48 entries) as multi-word — exactly the `AR uint64/int64` pair
  (`num_regs = 2`, §2.3) and every Vision SIMD type (all `> 32` bits): a
  byte-exact `01` at those indices, `00` elsewhere.

The pseudocode for the host get/set/convert dispatch, naming the real symbols:

```c
// ── libctype.so : the 6-export VERS_1.1 dispatch surface (host value marshalling) ──
//
// One-time host pointer to the simulated target-memory window:
void cstub_set_base_address(void *p);              // @0x43850 : base_addr = p

// GET / SET : pure ctype-id table lookups returning the marshalling stub.
typedef void (*loadi_fn)(reg_struct *dst, uint32_t base, uint32_t off);   // reads *dst
typedef void (*storei_fn)(const reg_struct *src, uint32_t base, uint32_t off); // writes mem
loadi_fn  cstub_loadi_function (int ctype_id);     // @0x43860 : cstub_loadi_ftable[ctype_id]
storei_fn cstub_storei_function(int ctype_id);     // @0x43870 : cstub_storei_ftable[ctype_id]

// CONVERT (rtor) : per-source sorted singly-linked list keyed on the TARGET ctype id.
//   24-byte node { int32 other_ctype_id @0x00; void *fn @0x08; node *next @0x10 }.
typedef void (*rtor_fn)(reg_struct *dst, /* scalar value OR reg_struct* */ uintptr_t src);
rtor_fn cstub_rtor_function(int src_ctype_id, int other_ctype_id) {        // @0x43880
    cstub_rtor_node *n = cstub_rtor_ftable[src_ctype_id];   // list head
    for (; n; n = n->next)                                  // kept sorted on other_ctype_id
        if (n->other_ctype_id == other_ctype_id)            // (cmp/je/ja early-out)
            return (rtor_fn)n->fn;     // Function_TIE_<src>_rtor_<other>
    return NULL;                       // no such convert
}

// SIZE (single/multi-word) : the per-ctype byte flag.
uint8_t cstub_ctype_size_function(int ctype_id) {  // @0x438c0
    return cstub_ctype_multi_word[ctype_id];       // 1 = spans >1 host word
}

// Build all 3 ftables at sim startup (4715 instrs); 265 (src,other) convert edges, 205 fns.
void cstub_ctypes_init(...);                        // @0x438d0

// ── representative stub bodies (the get/set/move/convert bit-layout) ──
//
// GET scalar AR (ctype idx 4):  Function_TIE_xt_core_uint32_loadi @0x3d4a0
//   vaddr = base + off;  if (vaddr & 3) cstub_vaddr_not_aligned(vaddr); // exit(1) on misalign
//   *(uint32*)dst = *(uint32*)(base_addr + (vaddr & ~3));               // 4-byte, align-checked
//
// GET 512-bit vec (ctype idx 21): Function_TIE_xt_ivp32_xb_vec2Nx8_loadi @0x3e7f0
//   memcpy *dst <- base_addr+vaddr, 64 bytes via 4x movdqu  (NO alignment check; unaligned moves)
//
// CONVERT scalar->vec widen (idx 0 -> 21): Function_TIE_xt_ivp32_uint8_rtor_xb_vec2Nx8 @0x3070
//   broadcast scalar src across SIMD lanes (movd; pshufd $0; byte-lane LUT + por), write 64 bytes
//
// CONVERT scalar->predicate (idx 5 -> 60): int32_rtor_xb_int32pr @0x113b0
//   *(uint32*)dst = (int32)src;  *(uint32*)(dst+4) = (int32)src >> 31;  // {value, sign} 64-bit pr
```

> **NOTE — `move` is realized as a trivial copy, not a stub.** `libctype` has **no** `Function_TIE_*_move`
> symbol even though `ctype_protos[]` lists 47 `move` protos. A same-type move is the identity
> `rtor` or a `loadi`/`storei` round-trip / flat struct copy — the native sim needs no dedicated
> function. The 9 table-only kinds (move, mtor, rtom, and the indexed/post-inc load/store modes)
> are computed by the caller from `base_addr` arithmetic before calling `loadi`/`storei`.
> `[HIGH/OBSERVED counts; MED/INFERRED caller synthesis]`

---

## 9. Provenance & confidence ledger

| Claim | Tag | Witness |
|---|---|---|
| `num_ctypes = 64`; `ctypes` sym size `0x1000`; stride 64 | HIGH/OBS | `0x3b67d0` `mov $0x40`; `nm -S`; `ctype_name` `shl $6` |
| The 64-ctype roster + regfile census `vec27/AR13/wvec11/BR5/vbool4/b32_pr2/valign1/gvr1` | HIGH/OBS | `mmap` parse of `0x4cbb00`, names into `.rodata` |
| 6 composite ctypes + field-array sizes `{2,2,2,4,8,16}` | HIGH/OBS | `nm -S Ctype__TIE_*_fieldNames/Types` |
| `num_ctype_protos = 651`; kind census; 340 converts | HIGH/OBS | `0x3b6d30` `mov $0x28b`; `mmap` parse `0x47bb40` |
| `kind` is a `char*` (not int) | HIGH/OBS | `ctype_proto_kind @ 0x3b6d80` `mov 0x10(.),%rax` |
| `coprocs[0] = {"Vision", 1}`; sym size `0x10` | HIGH/OBS | `0x47bb00` bytes `0b b8 3b 00 … 01`; `→ "Vision\0"` |
| `funcUnits[0] = {"XT_LOADSTORE_UNIT", 2}`; sym size `0x10` | HIGH/OBS | `0x54a9c0` bytes `d3 d0 3c 00 … 02`; `→ "XT_LOADSTORE_UNIT\0"` |
| funcUnit-use sweep `{0:1291,1:238,2:5}`; 248 LSU@stage0; 5 dual `l2*` | HIGH/OBS | `opcodes[]` sweep `0x6ce6c0` |
| bypass groups/chunks/entry all `0` (const-0 stubs) | HIGH/OBS | `0x3b61a0/b0/c0` `xor %eax,%eax` |
| `add.n` operand `(use,def)` = dst`(FF,4)` / src`(4,FF)`; byp `0xFFFF` | HIGH/OBS | operand-use array of opc idx 4 |
| dst `def_stage` distribution `4:80 … 11:346 12:485 13:115` | HIGH/OBS | operand-0 sweep over 1534 opcodes |
| `Vision = CP slot/bit 1` of the 7-bit `CPENABLE` | MED/INF | `number = 1` + 7-bit width + single entry |
| convert-direction semantics of `rtor`/`mtor`/`rtom` | MED/INF | name + `other_type`, corroborated by §8 |
| 1/3 auxiliary operand role-labels (post-incr / CPENABLE) | MED/INF | stage values OBSERVED; roles inferred |

**Structurally absent (do not parse as tables):** bypass groups / chunks / entry (count 0); the
timing is per-operand (§6). **Empty-`MODULE_SCHEDULE` wall:** the `1 + 1` co-issue is the sound
bound; a tighter per-port reservation model is not recoverable from this corpus.

---

## 10. Cross-references

* [Config-Grounded Microarch Reference Sheet §4.1 / §5](config-reference-sheet.md) — the headline
  `funcUnit`/`coproc` one-liners and the `1 + 1` co-issue ceiling this page details.
* [The Eight Register Files](register-files.md) — the eight files each ctype binds to; this page is
  the reverse `ctype → regfile` map.
* [The libisa Table Schema & Codec ABI](libisa-table-schema.md) — the table-ABI export list and the
  `(opcode × slot)` encode matrix (this page is the four *metadata* tables, not the encode side).
* [The Canonical ISA Decode Model (libisa-core)](libisa-decode-model.md) — the decode pipeline that
  produces the operands these tables type.
* [The FLIX VLIW Encoding](flix-encoding.md) — the 14-format / 46-slot issue grid the funcUnit and
  the `1 + 1` bound sit on; ALU/Mul/Ld are *slots*, not funcUnits.
* [libctype — CSTUB Custom-Type Functions](../../iss/libctype-cstub.md) — the host C-stub
  *implementation* of the ctype get/set/move/convert protocols (the ISS-lane deep treatment).
* [libcas-core — Cycle/Pipeline Timing Model](../../iss/cas-timing-model.md) — the scoreboard the
  per-operand stages (§6) reconstruct into cycle-accurate interlocks.
* [cas/fiss Load/Store (LSU) Semantics](../../iss/cas-load-store.md) — the value+timing semantics of
  the load/store ops bound to `XT_LOADSTORE_UNIT`.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — what `[HIGH/OBSERVED]`,
  `[MED/INFERRED]`, and "wall" mean.
* [Master Glossary → ctype / coproc / funcUnit](../../glossary.md#register-files--isa-metadata) —
  the one-line definitions and canonical anchors.

---

*Provenance: the `ctypes[]` (`0x6cbb00`), `ctype_protos[]` (`0x67bb40`), `coprocs[]` (`0x67bb00`),
`funcUnits[]` (`0x74a9c0`) tables, the count accessors, the `opcodes[]` operand-stage and
funcUnit-use sub-arrays, and the empty bypass accessors were disassembled, `nm -S`-sized, and
`mmap`-parsed from the shipped `libisa-core.so` (`.data.rel.ro` delta `0x200000`;
`.rodata` delta `0`). The `libctype.so` cross-reference (§8) comes from the same kind of read.
All facts are derived from shipped-artifact static analysis (lawful interoperability RE); no
external or vendor source tree was consulted.*
