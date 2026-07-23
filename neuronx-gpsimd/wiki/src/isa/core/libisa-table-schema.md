# The libisa Table Schema & Codec ABI

This is the **data-structure** reference for `libisa-core.so` — the Rosetta Stone for parsing
its on-disk ISA tables and driving its encode/decode codec. Where the
[FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) walks a byte stream into a
bundle and the [Canonical ISA Decode Model](libisa-decode-model.md) gives the decode *algorithm*
(the format/slot/field traversal and the 1534↔12569 placement model), this page owns the layer
underneath both: the **byte-level layout of every libisa table** — its base address, element
stride, and per-field offsets — and the **codec function ABI** (the `get`/`set`/`field_get`/
`field_set` thunks and the `encode`/`decode`/`do_reloc`/`undo_reloc` operand codecs) that
compose to insert or extract one operand. After reading it you can `mmap` the shipped `.so`,
apply a stride, and read any table cell directly — no accessor calls, no decompiler — and you
can read or write the opcode-selector and operand bits of any slot from the literal thunk bytes.

Everything below is read **directly out of the shipped Tensilica libisa config library**
`libisa-core.so` (`ncore2gp/config/`, sha256 `8fe68bf4…f143e451`, 9 690 712 bytes, ELF64
x86-64, **not stripped** — full `.symtab`, 45 058 symbols). Every address, stride and field
offset was read from the accessor machine code (`objdump -d`), the table is then read raw
(`xxd`/`readelf -x`), and the two cross-check. `[HIGH/OBSERVED]` throughout except where a
field is flagged.

> **Scope split.** This page is the *layout + codec ABI*. The **content** of the format/slot
> tables — which 14 formats exist, their selector triggers, the 46 slots' bit offsets/units —
> lives in [The FLIX VLIW Encoding](flix-encoding.md). The **decode traversal** and the
> shipped-vs-authoring placement fold live in [the Decode Model](libisa-decode-model.md). The
> per-operand metadata semantics (the 64 ctypes, the single coproc, the funcUnit) live in
> [ctype / coproc / funcUnit Tables](ctype-coproc-funcunit.md). This page does not re-narrate
> any of those; it gives the struct each of them parses.

---

## 1. Key facts

| Fact | Value | Source |
|---|---|---|
| Binary | `libisa-core.so`, ELF64 x86-64, not stripped, 45 058 syms | `readelf -h` |
| sha256 | `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451` | `sha256sum` |
| `interface_version` | `0x76` = **118** (Xtensa libisa ABI rev) | `interface_version` @ `0x3b5b20` → `mov $0x76,%eax` |
| Table residency | all 22 tables in `.data.rel.ro` (VMA `0x67bb00`) **except** `config_table` in `.data` (VMA `0x764040`) | `readelf -SW` |
| **`.data.rel.ro` VMA↔file delta** | **`0x200000`** (VMA `0x67bb00` → file `0x47bb00`) | `readelf -SW` |
| **`.data` VMA↔file delta** | **`0x200000`** (VMA `0x764040` → file `0x564040`) | `readelf -SW` |
| `.text` / `.rodata` delta | **0** (VMA == file offset — codec thunks & string pool resolve direct) | `readelf -SW` |
| String pool | `.rodata` @ `0x3b6e40` (every table's `char*` points here) | `readelf -SW` |
| Codec thunk pool | `.text` @ `0x312c10` (first byte = `Field_t_Slot_inst_get`) | `readelf -SW` |
| Pointer fields | filled at load by `R_X86_64_RELATIVE` relocs (r_offset = field VMA, addend = target VMA) | `readelf -rW` |

The headline table counts, each read from the count accessor's literal immediate:

| Table | Count | Stride | VMA | Accessor (→ immediate) |
|---|---:|---:|---|---|
| `formats[]` | 14 | 24 | `0x6cd980` | `num_formats` @ `0x3b65e0` → `0x0e` |
| `slots[]` | 46 | 48 | `0x6cdb00` | `num_slots` @ `0x3b6510` → `0x2e` |
| `fields[]` | 3237 | 32 | `0x74aa00` | `num_fields` @ `0x3b5b40` → `0xca5` |
| `operands[]` | 232 | 64 | `0x746d80` | `num_operands` @ `0x3b5e80` → `0xe8` |
| `iclasses[]` | 1447 | 56 | `0x7330c0` | `num_iclasses` @ `0x3b5fb0` → `0x5a7` |
| `opcodes[]` | **1534** | 72 | `0x6ce6c0` | `num_opcodes` @ `0x3b61d0` → `0x5fe` |
| `opcodedefs[]` | **12569** | 24 | `0x6e9640` | `num_encode_fns` @ `0x3b6130` → `0x3119` |
| `decodes[]` | 46 | 16 | `0x6ce3c0` | `num_decode_fns` @ `0x3b64c0` → `0x2e` |
| `regfiles[]` | 8 | **56** | `0x74a800` | `num_regfiles` @ `0x3b5c20` → `0x08` |
| `regfile_views[]` | 4 | 32 | `0x74a780` | `num_regfile_views` @ `0x3b5d50` → `0x04` |
| `states[]` | 81 | 32 | `0x6ccf40` | `num_states` @ `0x3b6670` → `0x51` |
| `sysregs[]` | 34 | 32 | `0x6ccb00` | `num_sysregs` @ `0x3b6720` → `0x22` |
| `ctypes[]` | 64 | 64 | `0x6cbb00` | `num_ctypes` @ `0x3b67d0` → `0x40` |
| `ctype_protos[]` | 651 | 32 | `0x67bb40` | `num_ctype_protos` @ `0x3b6d30` → `0x28b` |
| `protos[]` | 3484 | 88 | `0x680d40` | `num_protos` @ `0x3b6920` → `0xd9c` |
| `xfer_protos[]` | 2 | 56 | `0x680cc0` | `num_xfer_protos` @ `0x3b6c20` → `0x02` |
| `coprocs[]` | 1 | 16 | `0x67bb00` | `num_coprocs` @ `0x3b6dc0` → `0x01` |
| `funcUnits[]` | 1 | 16 | `0x74a9c0` | `num_funcUnits` @ `0x3b5bd0` → `0x01` |
| `config_table` | NUL-term | 16 (`{k,v}`) | `0x85ea40` (.data) | `get_config_table` @ `0x3b5b30` |

> **NOTE — pin the placement pairs; never cross them.** The shipped runtime table holds **1534
> opcodes ↔ 12569 `opcodedefs` placements**; the *authoring* TIE-DB superset is **1607 ↔ 12642**
> (`+73` folded-out). This page documents the **runtime** table (1534/12569) — the bytes in this
> `.so`. The `+73` fold and the `1607/12642` superset belong to
> [the Decode Model](libisa-decode-model.md) and [the Coverage Tally](coverage-tally.md); cite
> the matched pair, never "1534 shipped / 12642 placements."

> **CORRECTION — `regfiles[]` stride is 56, not 64.** Some early count dumps listed the
> `regfiles[]` stride as 64 (the `shl $6` half of the index math). The full arithmetic in
> `regfile_name` is `lea (,%rdi,8),%rax` (×8) then `shl $6,%rdi` (×64) then `sub %rax,%rdi`
> (×64 − ×8) = **×56**, and the raw bytes confirm `BR` begins exactly at `regfiles+0x38`
> ([§4.9](#49-regfiles-stride-56--0x74a800), byte-verified). Model 56. This matches
> [The Eight Register Files](register-files.md), the reconciled authority.

---

## 2. The universal accessor pattern — read any field offset yourself

Every scalar accessor in `libisa.def` is a one-line table indexer of one fixed shape. Reading
the shape off `objdump -d` is how every stride and offset on this page was recovered, and a
reimplementer never needs the accessors — only this pattern:

```asm
foo(int i):                       ; the canonical libisa accessor
    movslq %edi,%rdi              ; sign-extend index i
    lea    TABLE(%rip),%rax       ; base VMA of the table
    <scale rdi to i*STRIDE>       ; the stride is encoded here (table below)
    mov    OFF(%rax,%rdi,1),%rax  ; load the field at byte offset OFF
    ret
```

The load **mnemonic** also encodes the field *width*, so the C type is recoverable from the
disassembly alone:

| load mnemonic | C type read |
|---|---|
| `mov OFF(...),%rax` | `ptr` / `int64` (8 B) |
| `mov OFF(...),%eax` | `int32` (4 B) |
| `movzwl OFF(...),%eax` | `uint16` (2 B) |
| `movzbl OFF(...),%eax` | `uint8` (1 B) |
| `movsbl OFF(...),%eax` | `int8` (1 B) |

The **stride** is encoded in the index-scaling instruction(s). These are the encodings observed
across the table set — recognise them in any Tensilica libisa `.so`:

| asm scaling | stride | tables |
|---|---:|---|
| `shl $0x4,%rdi` | ×16 | `coprocs`, `funcUnits`, `decodes` |
| `shl $0x5,%rdi` | ×32 | `fields`, `regfile_views`, `states`, `sysregs`, `ctype_protos` |
| `shl $0x6,%rdi` | ×64 | `operands`, `ctypes` |
| `lea (%rdi,%rdi,2),%rdx` then `,8` scale | ×24 | `formats`, `opcodedefs` |
| `lea (%rdi,%rdi,8),%rdx` then `,8` scale | ×72 | `opcodes` |
| `lea (,%rdi,8); shl $6; sub` (64−8) | ×56 | `iclasses`, `regfiles`, `xfer_protos` |
| `lea (%rdi,%rdi,4); lea (rdi,rax,2)` then `,8` | ×88 | `protos` (×5 → ×11 → ×8) |

Two-index accessors (`table[i].subarray[j]`) load the `+OFF` pointer, then index *it* with its
own scale; byte/halfword sub-fields use `movzbl`/`movzwl`/`movsbl` so element widths are
recoverable the same way.

---

## 3. The table inventory at a glance

Twenty-two live tables plus a config root. Each is a flat array of fixed-stride records whose
pointer fields land in `.rodata` (resolved by `R_X86_64_RELATIVE`). The first eight are the
**encoding spine** a disassembler/assembler touches per instruction; the rest are TIE metadata.

| # | table | what one record is | role |
|---|---|---|---|
| 1 | `formats[]` | `{name, length, encode_fn}` | the 14 FLIX/scalar formats (length + blank-template emitter) |
| 2 | `slots[]` | `{name, format, nop, position, get, set}` | the 46 issue slots; `get`/`set` scatter between the 32-bit slot word and the bundle |
| 3 | `fields[]` | `{name, slot, get, set}` | 3237 per-`(field,slot)` bit-field codecs |
| 4 | `operands[]` | `{name, field, regfile, num_regs, flags, enc, dec, ator, rtoa}` | 232 value↔field codecs + PC reloc |
| 5 | `iclasses[]` | `{name, operands[], stateOperands[], interfaceOperands[]}` | 1447 instruction classes (the operand list an opcode draws) |
| 6 | `opcodes[]` | `{name, package, iclass, flags, stages…, ldst…}` | 1534 mnemonics (the roster) |
| 7 | `opcodedefs[]` | `{opcode, slot, encode_fn}` | **12569** `(opcode × slot)` encode placements |
| 8 | `decodes[]` | `{slot, decode_fn}` | 46 per-slot opcode classifiers (one per slot) |
| 9 | `regfiles[]` | `{name, shortname, package, num_bits, num_entries, num_callee_saved, flags, ctype, coproc}` | 8 register files |
| 10 | `regfile_views[]` | `{name, parent, num_bits, ctype}` | 4 narrowed `BR` views |
| 11 | `states[]` | `{name, package, num_bits, flags, coproc}` | 81 TIE states |
| 12 | `sysregs[]` | `{name, number, is_user, num_contents, contents[]}` | 34 special registers |
| 13 | `ctypes[]` | `{name, package, num_bits, alignment, regfile, view, num_regs, flags, field_types[], field_names[]}` | 64 TIE C-types |
| 14 | `ctype_protos[]` | `{name, ctype, kind, other_type}` | 651 ctype constructor/op protos |
| 15 | `protos[]` | `{name, package, operator, operands[], tmps[], insns[]}` | 3484 TIE protos (the lowered op bodies) |
| 16 | `xfer_protos[]` | `{kind, endpoint1, endpoint2, proto1, proto2, …}` | 2 transfer protos |
| 17 | `coprocs[]` | `{name, number}` | 1 (`Vision`) |
| 18 | `funcUnits[]` | `{name, num_copies}` | 1 |
| — | `config_table` | `{key, value}` pairs, NUL-terminated | the config root (`get_config_table`) |

> **GOTCHA — `interface_*` / `interface_class_*` are dead stubs.** `num_interfaces` and
> `num_interface_classes` both return `0`, and their element accessors index *with no table
> base* (e.g. `interface_class_num_members` does `shl $0x4,%rdi; mov (%rdi),%eax` — it would
> dereference the raw index). They are safe only because the counts are zero. There is **no**
> `interfaces[]` array; do not parse one. Likewise `num_bypass_groups == 0` — the bypass tables
> are absent in this config.

---

## 4. Per-table C struct schemas

Each offset below was read from the `mov OFF(%rax,…)` in that field's accessor; the load width
gives the C type. Every pointer offset cross-checks against an `R_X86_64_RELATIVE` reloc landing
exactly there. The byte-witness lines are raw `xxd` of `(VMA − 0x200000)`.

### 4.1 `formats[]` — stride 24 @ `0x6cd980`

```c
struct xtensa_format_internal {       // 24 bytes
    const char *name;          // +0x00  format_name        ("x24","F0","N0",…)
    int32_t     length;        // +0x08  format_length      bytes: 2 / 3 / 8 / 16
    void      (*encode)(uint32_t*); // +0x10 format_encode_fn  writes a blank bundle template
};
```
`formats[0]` (file `0x4cd980`): `0abb 3b00…` `03 00 00 00` `c057 3b00…` → `{name="x24",
length=3, encode=0x3b57c0}`. The format-selection and length-decode entry points are **not** in
this table — `decode_format_fn`/`decode_length_fn` return the hardcoded `format_decoder`
(`0x3b5970`) and `length_decoder` (`0x3b5a50`); see [the FLIX encoding](flix-encoding.md).

### 4.2 `slots[]` — stride 48 @ `0x6cdb00`

```c
struct xtensa_slot_internal {          // 48 bytes
    const char *name;          // +0x00  slot_name          ("Inst","F0_S0_LdSt",…)
    const char *format;        // +0x08  slot_format        (the format NAME this slot is in)
    const char *nop;           // +0x10  slot_nop_name
    int32_t     position;      // +0x18  slot_position      SEQUENTIAL idx 0..N-1, NOT a bit offset
    void      (*get)(const uint32_t *bundle, uint32_t *slotbuf); // +0x20 slot_get_fn
    void      (*set)(uint32_t *bundle, const uint32_t *slotbuf); // +0x28 slot_set_fn
};
```
`slots[0]` (file `0x4cdb00`): name=`"Inst"`, format=`"x24"`, position `+0x18` = `0`,
`get=0x3b0450` (= `Slot_x24_Format_inst_0_get`), `set=0x3b0490`. The `get`/`set` thunks
*scatter* a slot's bits in/out of the 128-bit bundle ([§5.4](#54-slot-getset--scatter-into-the-bundle)).

> **GOTCHA — `slots[].position` is a sequential index, not a bit offset.** It returns `0,1,2,…`
> in *issue* order; the real bundle bit offset lives in the `get` thunk body (and is echoed in
> its symbol name, e.g. `…_s2_mul_41_…`). Indexing the bundle by `position` corrupts every slot.

### 4.3 `fields[]` — stride 32 @ `0x74aa00`

```c
struct xtensa_field_internal {         // 32 bytes
    const char *name;          // +0x00  field_name   ("t","s","imm8","fld_inst_23_16",…)
    const char *slot;          // +0x08  field_slot   (the slot NAME the field lives in)
    uint32_t  (*get)(const uint32_t *slotword);         // +0x10  field_get_fn
    void      (*set)(uint32_t *slotword, uint32_t val); // +0x18  field_set_fn
};
```
`fields[0]` (file `0x54aa00`, byte-exact): name@`0x3b83df`=`"t"`, slot@`0x3c9b79`=`"Inst"`,
`get=0x312c10`, `set=0x312c20`. The **same** logical field recurs once per slot it appears in,
each placement at a different bit position with its own thunk pair — which is why the table is
3237 deep ([§5.3](#53-field-getset--the-per-fieldslot-bit-codec)).

> **GOTCHA — 3237 `fields[]` entries but only 3230 named `Field_*_get`/`_set` thunk pairs.**
> `nm | rg -c 'Field_.*_Slot_.*_get'` = 3230 (and `_set` = 3230); seven `fields[]` rows do not
> have a uniquely-named per-`(field,slot)` thunk (their `get`/`set` pointers alias a shared or
> compiler-merged body). Parse all 3237 rows for `(name,slot)`, but expect 3230 distinct named
> codec addresses.

### 4.4 `operands[]` — stride 64 @ `0x746d80`

```c
struct xtensa_operand_internal {       // 64 bytes
    const char *name;          // +0x00  operand_name       ("soffsetx4",…)
    const char *field;         // +0x08  operand_field      (FIELD name)
    const char *regfile;       // +0x10  operand_regfile    (regfile name, or "" for immediates)
    int32_t     num_regs;      // +0x18  operand_num_regs
    int32_t     flags;         // +0x1c  operand_flags
    int       (*encode)(uint32_t*);            // +0x20  operand_encode_fn    value→field
    int       (*decode)(uint32_t*);            // +0x28  operand_decode_fn    field→value
    int       (*do_reloc)(uint32_t*, uint32_t pc);   // +0x30  operand_do_reloc_fn   "ator"
    int       (*undo_reloc)(uint32_t*, uint32_t pc); // +0x38  operand_undo_reloc_fn "rtoa"
};
```
`operands[0]` (file `0x546d80`, byte-exact): name@`0x3cdd87`=`"soffsetx4"`,
field@`0x3cddd3`=`"offset"`, regfile=`""`, num_regs=0, flags=`2`, enc=`0x337230`,
dec=`0x337220`, ator=`0x3384d0`, rtoa=`0x3384e0`. The four function pointers are the operand
value codec ([§5.5](#55-operand-codec--value--field--pc-reloc)).

### 4.5 `iclasses[]` — stride 56 @ `0x7330c0`

```c
struct xtensa_iclass_internal {        // 56 bytes
    const char *name;          // +0x00  iclass_name
    int32_t     num_operands;  // +0x08
    struct { char *name; uint8_t inout; } *operands; // +0x10  elem stride 16
    int32_t     num_stateOperands;    // +0x18
    struct { char *state; uint8_t inout; } *stateOperands; // +0x20  elem stride 16
    int32_t     num_interfaceOperands;// +0x28
    char      **interfaceOperands;    // +0x30  ptr[8] per element
};
```
The `operands` sub-array (`+0x10`) is what `opcode → iclass → arg list` walks: each element's
`name` (`@+0`) is the operand name and `inout` (`movzbl @+8`) is its in/out direction.

### 4.6 `opcodes[]` — stride 72 @ `0x6ce6c0`

```c
struct xtensa_opcode_internal {        // 72 bytes
    const char *name;          // +0x00  opcode_name
    const char *package;       // +0x08  opcode_package
    const char *iclass;        // +0x10  opcode_iclass   (iclass NAME)
    int32_t     flags;         // +0x18  opcode_flags    (semantic-bit catalog — see below)
    uint8_t    *operand_stages;// +0x20  per-operand [use,def] + bypass groups, 6 B/elem:
                               //          use_stage [0], def_stage [1] (u8),
                               //          bypass_use_group_idx u16 @+2, def_group_idx u16 @+4
    uint8_t    *stateOp_stages;// +0x28  per-stateOperand triple, 3 B/elem [use,def,allow_reorder]
    int32_t     num_funcUnit_uses; // +0x30
    struct { char *unit; int32_t stage; } *funcUnit_uses; // +0x38  elem stride 16
    uint16_t    ldst_bytes;    // +0x40  opcode_ldst_bytes (movzwl)
    uint8_t     ldst_value_opnd;   // +0x42
    uint8_t     ldst_base_opnd;    // +0x43
    uint8_t     ldst_offset_opnd;  // +0x44
    uint8_t     ldst_postincr_opnd;// +0x45
    uint8_t     aligning_base_opnd;   // +0x46
    uint8_t     aligning_offset_opnd; // +0x47
};                             // padded to 72
```
`flags` (`+0x18`) is the global semantic-bit catalog the assembler keys on (load `0x10`, store
`0x20`, post-update `0x800`, t-suffix `0x80`, scatter `0x20000`, branch/relative `0x1`,
core/serializing `0x100`, a vector sub-class marker `0x80000` on a 193-op subset). These are
the **format-independent** half of the two-tier selector model; the per-slot opcode-selector
bits are the *other* tier and live in the encode templates ([§5.1](#51-encode-thunk--the-opcode-selector-emit)).
The `operand_stages` element is **6 bytes** (use,def,u16 use-grp,u16 def-grp); the bypass-group
accessors read the u16s `@+2/+4` of the same sub-array — internally consistent, but with
`num_bypass_groups==0` here the bypass packing is `[MED]`.

### 4.7 `opcodedefs[]` — the ENCODE matrix, stride 24 @ `0x6e9640`

```c
struct xtensa_opcode_encode_internal { // 24 bytes  — 12569 entries
    const char *opcode;        // +0x00  encode_fn_opcode  (opcode NAME)
    const char *slot;          // +0x08  encode_fn_slot    (slot NAME)
    void      (*encode_fn)(uint32_t *slotword); // +0x10  encode_fn
};
```
This is the `(opcode × slot)` cross-product of every **legal** slot placement: **12569**
`Opcode_<mnem>_Slot_<slot>_encode` thunks, the table the 32 mnemonic-slice sweeps iterate.
`opcodedefs[0]` (file `0x4e9640`, byte-exact): opcode@`0x3c9d50`=`"excw"`, slot@`0x3c9b79`=
`"Inst"`, `encode_fn=0x338610` (= `Opcode_excw_Slot_inst_encode`). The thunk ABI is
[§5.1](#51-encode-thunk--the-opcode-selector-emit).

### 4.8 `decodes[]` — the per-slot DECODE table, stride 16 @ `0x6ce3c0`

```c
struct xtensa_slot_decode_internal {   // 16 bytes  — 46 entries == num_slots
    const char *slot;          // +0x00  decode_fn_slot  (slot NAME)
    const char*(*decode_fn)(const uint32_t *slotword); // +0x08  decode_fn → opcode name
};
```
`decodes[0]` (file `0x4ce3c0`, byte-exact): slot@`0x3c9b79`=`"Inst"`, `decode_fn=0x3697a0` (=
`Slot_inst_decode`). There is **one decoder per slot, not per opcode** — each is a masked-compare
switch over the opcode-selector bits returning the mnemonic ([§5.2](#52-slot-decode--the-inverse-classifier)).
`nm | rg -c '… Slot_[a-z0-9_]+_decode'` = **46** = `num_slots` = `num_decode_fns`.

### 4.9 `regfiles[]` — stride 56 @ `0x74a800`

```c
struct xtensa_regfile_internal {       // 56 bytes  (stride byte-verified: BR begins at +0x38)
    const char *name;          // +0x00  ("AR","BR","vec","wvec","gvr",…)
    const char *shortname;     // +0x08  ("a","b","v","wv","gr",…)  — the operand base token
    const char *package;       // +0x10
    int32_t     num_bits;      // +0x18  register WIDTH
    int32_t     num_entries;   // +0x1c  register COUNT (depth)
    int32_t     num_callee_saved; // +0x20  (0 for all 8)
    int32_t     flags;         // +0x24  (0x05; gvr=0x0d)
    const char *ctype;         // +0x28
    const char *coproc;        // +0x30  ("" for AR/BR; "Vision" for the 6 SIMD files)
};
```
`regfiles[0]` raw (file `0x54a800`): name@`0x3c9a43`=`"AR"`, shortname@`0x3b6e59`=`"a"`,
package@`0x3c99f1`=`"xt_xtensa"`, `+0x18`=`20 00 00 00`(=32), `+0x1c`=`40 00 00 00`(=64),
`+0x20`=`0`, `+0x24`=`05`, ctype@`0x3bb82c`=`"_TIE_uint32"`, coproc@`0x3c4ec4`=`""` (the
**empty string** — a non-NULL pointer to `\0`, not NULL). `regfiles[1]` (`BR`) starts exactly at
`+0x38` — the byte-proof of stride **56**. The full roster, ports, and bridges are
[The Eight Register Files](register-files.md); this page owns only the struct.

### 4.10 `regfile_views[]` — stride 32 @ `0x74a780`

```c
struct xtensa_regfile_view_internal {  // 32 bytes
    const char *name;          // +0x00  ("BR2","BR4","BR8","BR16")
    const char *parent;        // +0x08  ("BR" — all four)
    int32_t     num_bits;      // +0x10  (2 / 4 / 8 / 16)
    const char *ctype;         // +0x18  (_TIE_xtbool2/4/8/16)
};
```
Four narrowed boolean views over the single `BR` file — typed windows, no storage of their own.
They are the `8 files + 4 views = 12 TIE regfile-class entries` reconciliation
([register-files.md §7](register-files.md#7-the-8-vs-12-reconciliation--files-vs-views)).

### 4.11 The TIE metadata tables (one line each)

```c
// states[]  (32) @0x6ccf40 — 81 entries
struct { const char *name; const char *package; int32_t num_bits; int32_t flags; const char *coproc; };
// sysregs[] (32) @0x6ccb00 — 34 entries
struct { const char *name; int32_t number; int32_t is_user; int32_t num_contents; char **contents; };
// ctypes[]  (64) @0x6cbb00 — 64 entries
struct { const char *name; const char *package; int32_t num_bits; int32_t alignment;
         const char *regfile; const char *regfile_view; int32_t num_regs; int32_t flags;
         char **field_types; char **field_names; };
// ctype_protos[] (32) @0x67bb40 — 651 entries
struct { const char *name; const char *ctype; const char *kind; const char *other_type; };
// protos[] (88) @0x680d40 — 3484 entries (the lowered TIE op bodies)
struct { const char *name; const char *package; const char *operator; int32_t num_operands;
         /* operands[] @+0x20 stride 24; tmps[] @+0x30 stride 24; insns[] @+0x40 stride 16 */
         /* num_tmps @+0x28; num_insns @+0x38; insn arg quad via +0x48/+0x50 */ };
// xfer_protos[] (56) @0x680cc0 — 2 entries (kind + 2 endpoints + 2 proto names)  [MED interiors]
// coprocs[] (16) @0x67bb00 — 1: {name="Vision", number=1}
// funcUnits[] (16) @0x74a9c0 — 1: {name, num_copies}
```
The `ctypes`/`coprocs`/`funcUnits` semantics (the reverse `ctype → regfile` map, the single
`Vision` coproc) are [ctype / coproc / funcUnit Tables](ctype-coproc-funcunit.md); `states`/
`sysregs` belong to the state/sysreg interface page. The `protos[]` and `xfer_protos[]`
second-level element offsets are `[MED]` (read from the accessor, not each live-cross-checked).

### 4.12 `config_table` — the config root @ `0x85ea40` (.data)

`get_config_table` (`0x3b5b30`) = `lea config_table(%rip)` → `0x85ea40`. It is a NUL-terminated
array of 16-byte `{const char *key; const char *value;}` pairs (file `0x65ea40` = VMA −
`0x200000`, the **`.data`** delta). Byte-read keys: `IsaMemoryOrder=LittleEndian`,
`IsaMaxInstructionSize=32`, `PipeWStage`, `DataMemWidth=512`, `IsaCoprocessorCount`,
`IsaUseBooleans`; terminated by a `{0,0}` pair at `+0x60`.

> **GOTCHA — `config_table` is in `.data`, not `.data.rel.ro`.** It is the one root *outside*
> the `.data.rel.ro` table cluster. For this binary the `.data` delta also happens to be
> `0x200000`, but it is a **distinct section** with its own header — read its delta from
> `readelf -SW` per binary, do not assume the `.data.rel.ro` delta carries.

---

## 5. The codec ABI — four layers that compose one operand

Encoding or decoding one operand of one slot walks four codec layers in sequence. Each layer is
a tiny `.text` thunk read directly from its bytes (`.text` is VMA==file offset). The four layers,
outermost to innermost:

```
opcode-selector  : Opcode_<mnem>_Slot_<slot>_encode   (lays the fixed opcode bits)
operand value    : OperandSem_…_encode / _decode + _ator / _rtoa  (value ↔ field, ± PC reloc)
bit-field        : Field_<f>_Slot_<s>_get / _set       (the field's bits in the 32-bit slot word)
slot scatter     : Slot_<…>_get / _set                 (the slot word ↔ the 128-bit bundle)
```

### 5.1 Encode thunk — the opcode-selector emit

`opcodedefs[i].encode_fn` is a movl-template thunk that writes only the **fixed opcode-selector
bits** of mnemonic `i` into the slot's normalized word(s); operand fields are deposited
separately by the field/operand layers. The byte form is invariant across all 12569 thunks:

```
C7 07 <imm32>            [ C7 47 04 <imm32> ]            C3
movl $TEMPLATE,(%rdi)    [ movl $WORD1,0x4(%rdi) ]       ret
└─ word0 = opcode-selector template   └─ word1 (wide slots only)   └─ return
```

```c
void Opcode_<mnem>_Slot_<slot>_encode(uint32_t *slotword /*rdi*/) {
    slotword[0] = TEMPLATE;          // C7 07 imm32   — the fixed opcode bits for THIS (mnem,slot)
    /* wide FLIX slots only: */ slotword[1] = WORD1;  // C7 47 04 imm32 — clears the 2nd lane
    return;                          // C3
}
```

Byte-verified samples (literal `xxd`):

| thunk | bytes | template |
|---|---|---|
| `Opcode_excw_Slot_inst_encode` (`0x338610`) | `c7 07 80 20 00 00 c3` | `0x00002080` |
| `Opcode_xor_Slot_inst_encode` (`0x33a000`) | `c7 07 00 00 30 00 c3` | `0x00300000` |
| `Opcode_rsr_sar_Slot_inst_encode` (`0x33cca0`) | `c7 07 00 03 03 00 c3` | `0x00030300` |
| `Opcode_ivp_oeqn_2xf32_Slot_f1_s3_alu_encode` (`0x34b490`) | `c7 07 00 c8 05 27 c3` | `0x2705c800` |
| `Opcode_mov_n_Slot_f0_s3_alu_encode` (`0x3386b0`) | `c7 07 00 d8 98 64` `c7 47 04 00 00 00 00` `c3` | word0=`0x6498d800`, **word1=0** |

> **QUIRK — the upper lane (`word1`) is always `0x00000000`.** Every 2-lane (wide-FLIX) thunk
> across all 12569 placements clears the second 32-bit lane and writes **zero** opcode-selector
> bits there: the selector never spills past bit 32. `word1 == 0` with zero exceptions. A
> reimplemented encoder that emits a wide slot must therefore zero lane 1 before depositing
> operand bits — the template contributes nothing there.

> **NOTE — the same mnemonic has a different template in every slot it can occupy.** `addi`'s
> template is `0x10b60000` in `F0_S0_LdSt`, `0x00398000` in `F0_S1_Ld`, `0x00c03000` in
> `F0_S2_Mul`, `0x86880000` in `F0_S3_ALU` — because each slot owns a different bit window of the
> bundle. The 12569 thunks are the full legal `(mnemonic × slot)` matrix; the per-format packing
> is the *format-local* tier of the [two-tier selector model](flix-encoding.md#62-the-two-tier-selector-model).

**Symbol-mangling rule** (the `.symtab` ↔ `opcodedefs` name map):
`Opcode_<mnem>_Slot_<slot>_encode`, where (1) every `.` in the mnemonic → `_` (`add.n`→`add_n`,
`wur.fsr`→`wur_fsr`, `rsr.sar`→`rsr_sar`) **and** (2) the slot token is lowercased
(`Inst`→`inst`, `F2_S0_LdSt`→`f2_s0_ldst`). Both transforms are required; the mangling is
injective (no stem collisions).

### 5.2 Slot decode — the inverse classifier

`decodes[slot].decode_fn` is the read-side inverse: one `Slot_<slot>_decode` per slot runs a
masked-compare ladder over the gathered slot word and returns a pointer into the opcode-name
string pool — exactly inverting the encode templates of §5.1.

```c
const char *Slot_<slot>_decode(const uint32_t *slotword /*rdi*/);
// Slot_inst_decode (0x3697a0): masks & 0xffffff, compares the opcode group, peels bits[23:16]
//   (shl 8; shr 0x18) and switches — the inverse of every Opcode_*_Slot_inst_encode template.
```
This is the per-slot classifier the [decode model](libisa-decode-model.md) drives; this page
owns only its ABI and its place in the table (`decodes[]`, §4.8).

### 5.3 Field get/set — the per-`(field,slot)` bit codec

`fields[i].get`/`.set` read/write one operand bitfield inside the 32-bit slot word. The **same**
field name at a **different** bit position per slot gets its own thunk pair — the source of 3230
named pairs over 3237 rows.

```c
uint32_t Field_<f>_Slot_<s>_get(const uint32_t *slotword /*rdi*/);
//   left-justify then right-justify to isolate the field, or a direct movzbl of the byte.
void     Field_<f>_Slot_<s>_set(uint32_t *slotword /*rdi*/, uint32_t val /*esi*/);
//   mask val to width, shift to position, clear the field in the word, OR it back.
```

Byte-verified codec bodies:

```asm
Field_t_Slot_inst_get   (0x312c10):  mov (%rdi),%eax; shl $0x18; shr $0x1c        -> bits[7:4]
Field_t_Slot_inst_set   (0x312c20):  mov (%rdi),%eax; and $0xf,%esi; shl $4,%esi;
                                      and $0xf,%al; or %eax,%esi; mov %esi,(%rdi)  -> deposit nibble@4
Field_s_Slot_inst_get   (0x312c30):  shl $0x14; shr $0x1c                          -> bits[11:8]
Field_imm8_Slot_inst_get       (0x31ebe0): movzbl 0x2(%rdi)                        -> bits[23:16]
Field_imm8_Slot_f0_s0_ldst_get (0x31ec10): mov (%rdi); movzbl %ah                  -> bits[15:8]
```

The last two are the **same `imm8` field** read from a different bit position in two different
slots — the structural reason there are ~3230 thunk pairs rather than one per logical field.

> **QUIRK — `set` is the exact inverse of `get` over the field width.** `Field_t_Slot_inst_set`
> clears bits[7:4] (`and $0xf,%al` keeps the low nibble, the field nibble is the one cleared)
> and ORs in `(val & 0xf) << 4`; `Field_t_Slot_inst_get` extracts `(word >> 4) & 0xf`. So
> `field_get(slotword_after_set(0, v)) == v` for every `v` in range — proven over the full 5-bit
> vector register fields (`vr`/`vs`/`vt`) and 4-bit scalar AR fields (`r`/`s`/`t`) by the ISS
> slotfill cross-validation. The codec pair is a bijection on the field's bit window.

### 5.4 Slot get/set — scatter into the bundle

`slots[i].get`/`.set` move bits between a slot's 32-bit normalized word and the 128-bit (wide)
or 64-bit (narrow) bundle. A slot's bits do **not** form a contiguous window — they scatter
across the bundle's lanes — so the thunk is an `and`/`shr`/`shl`/`or` chain, not a single shift.

```c
void Slot_<…>_get(const uint32_t *bundle /*rdi*/, uint32_t *slotbuf);  // bundle → normalized word
void Slot_<…>_set(uint32_t *bundle /*rdi*/, const uint32_t *slotbuf);  // normalized word → bundle
```
`slots[0].get = 0x3b0450 = Slot_x24_Format_inst_0_get`; `slots[0].set = 0x3b0490`. The exact
scatter geometry (which bundle bits map where, per slot) is the
[FLIX encoding §5](flix-encoding.md#5-the-46-slot-roster--bit-offsets-widths-units-formats)
table; this page owns the ABI and the `slots[]` pointer fields that name them.

### 5.5 Operand codec — value ↔ field, ± PC reloc

`operands[i]` carries four codecs that translate between the user-visible *immediate value* and
the raw *field bits*, plus PC-relative relocation. All return `int` (`0` = success, nonzero =
out-of-range):

```c
int operand_encode_fn(uint32_t *value);            // value → field  (scale/bias/clamp)
int operand_decode_fn(uint32_t *value);            // field → value  (inverse: sign-ext/+bias)
int operand_do_reloc_fn(uint32_t *v, uint32_t pc); // "ator": absolute target → PC-relative field
int operand_undo_reloc_fn(uint32_t *v, uint32_t pc);// "rtoa": PC-relative field → absolute target
```

Byte-verified on `operands[0] = soffsetx4` (the branch displacement):

```asm
OperandSem_opnd_sem_soffsetx4_encode (0x337230): mov(%rdi); sub $4; shr $2; and $0x3ffff; mov  -> *v=((*v-4)>>2)&0x3ffff
OperandSem_opnd_sem_soffsetx4_decode (0x337220): sign-ext the 18-bit field, ×4 via shift, +4    -> inverse
Operand_soffsetx4_ator               (0x3384d0): and $0xfffffffc,%esi; sub %esi,(%rdi); ret      -> *v -= (pc & ~3)
Operand_soffsetx4_rtoa               (0x3384e0): and $0xfffffffc,%esi; add %esi,(%rdi); ret      -> *v += (pc & ~3)
```

`ator` and `rtoa` are exact inverses (`sub %esi` vs `add %esi` on the same word-aligned `pc`).
Immediate operands with no reloc carry only `encode`/`decode` (e.g.
`OperandSem_opnd_sem_immr_encode: *v &= 0xf` — a 4-bit immediate).

### 5.6 The full codec data-flow

One operand through one slot, both directions. This is the composition a reimplementer mirrors:

```
   ASSEMBLE  (mnemonic, value, pc)  ──►              ◄── DISASSEMBLE  (bytes, pc)

   user immediate                                    user immediate
        │ operand_encode_fn (scale/bias/clamp)            ▲ operand_decode_fn (sign-ext/+bias)
        ▼                                                 │
   field value ── operand_do_reloc(pc) ──► relocated   relocated ── operand_undo_reloc(pc) ──► field value
        │ Field_<f>_Slot_<s>_set(slotword,val)             ▲ Field_<f>_Slot_<s>_get(slotword)
        ▼                                                 │
   32-bit slot word  (+ opcode bits from Opcode_*_encode)  32-bit slot word
        │ Slot_<…>_set(bundle128, slotword)                ▲ Slot_<…>_get(bundle128, slotword)
        ▼                                                 │
   ┌──────────────── 128-bit FLIX bundle ────────────────┐
        ▲ Format_<F>_encode lays the blank template;      ▲ format_decoder / length_decoder
          op0 nibble selects the format (FLIX encoding)     pick format/length first (FLIX decoding)
```

To **assemble** a bundle: for each issued op, call its `Opcode_*_Slot_*_encode` to lay the
opcode bits, then `operand_encode`→`do_reloc`→`Field_*_set` each operand, then `Slot_*_set` to
scatter the slot word into the bundle. To **disassemble**: `format_decoder`/`length_decoder`,
then per slot `Slot_*_get`→`Slot_*_decode` for the mnemonic and `Field_*_get`→`undo_reloc`→
`operand_decode` for each operand. The selector bits (set by `encode`) and the operand bits (set
by `Field_*_set`) are **disjoint** within the slot word, so the two halves never collide — which
is why encode and decode are exact mutual inverses.

---

## 6. Schema conventions a reimplementer mirrors

The libisa ABI is internally uniform; a reimplementation honors five conventions and parses any
table from raw bytes:

1. **Counts are exposed as `num_*` accessors with a literal immediate.** `num_<table>()` is
   always `mov $0xN,%eax; ret`; read the immediate (`mov` operand) for the entry count. Never
   trust a symbol grep for a count — the table immediate is authoritative (e.g. `num_encode_fns`
   = `0x3119` = 12569, matched by `nm | rg -c 'Opcode_*_encode'` = 12569).

2. **Every table is a flat fixed-stride array.** `entry_i = base + i*STRIDE`; read scalars at the
   §4 offsets, with width from the load mnemonic (§2). No linked lists, no per-entry size word.

3. **Pointer fields point into `.rodata`; resolve them with the section delta, not a constant.**
   For `.data.rel.ro` tables, `file_off = VMA − Δ_relro`; for `config_table` in `.data`,
   `file_off = VMA − Δ_data`. **Compute each Δ per binary** from `readelf -SW` (here both are
   `0x200000`, but they are independent section headers). `.rodata` and `.text` are
   VMA == file offset, so string and thunk targets resolve with **no** adjustment — apply the
   delta only to the `.data*` table bodies you `xxd`/`objdump`, never to the pointers they hold.

4. **Codec is per-`(thing, slot)`, addressed by name.** Field/operand bit positions are
   slot-local, so a field recurs once per slot with its own thunk (3230 named `Field_*` pairs),
   and an opcode recurs once per legal slot with its own encode template (12569 `Opcode_*`
   thunks). Resolve a codec by the mangled symbol (`.`→`_`, slot lowercased) **or** by following
   the `get`/`set`/`encode_fn` pointer in the table row — both reach the same address.

5. **The runtime table is the shipped fold (1534/12569), not the authoring superset
   (1607/12642).** Parse `opcodes[]`/`opcodedefs[]` as the 1534/12569 pair; the `+73` pre-fold
   superset is a TIE-DB-only artifact ([decode model](libisa-decode-model.md)).

---

## 7. Adversarial self-verification

The five strongest table-layout/codec claims, each checked byte-exact against the shipped
`.so`:

1. **`opcodedefs[]` stride 24, `encode_fn` @ +0x10.** `encode_fn` accessor (`0x3b6180`):
   `lea (%rdi,%rdi,2),%rdx` (×3) then `mov 0x10(%rax,%rdx,8),%rax` (×8, +0x10) → stride
   3×8 = **24**, field at **+0x10**. ✓
2. **`regfiles[]` stride 56, not 64.** `regfile_name` (`0x3b5c30`): `lea (,%rdi,8),%rax` (×8);
   `shl $0x6,%rdi` (×64); `sub %rax,%rdi` → 64 − 8 = **56**. Raw bytes: `BR` record begins at
   `regfiles + 0x38` (= 56). ✓
3. **`fields[0] = {name="t", slot="Inst", get=0x312c10, set=0x312c20}`.** Raw `xxd` at file
   `0x54aa00`: `df83 3b00`→`0x3b83df`="t", `799b 3c00`→`0x3c9b79`="Inst", `102c 3100`→
   `0x312c10`, `202c 3100`→`0x312c20`. ✓
4. **`excw` encode thunk template `0x2080`.** `xxd` at `0x338610`: `c7 07 80 20 00 00 c3` =
   `movl $0x2080,(%rdi); ret`. Matches `opcodedefs[0].encode_fn` pointer (`0x338610`) and the
   `excw`/`Inst` strings it pairs. ✓
5. **2-lane encode thunk clears `word1` to zero.** `xxd` at `0x3386b0`
   (`Opcode_mov_n_Slot_f0_s3_alu_encode`): `c7 07 00 d8 98 64` (`word0=0x6498d800`)
   `c7 47 04 00 00 00 00` (`word1=0`) `c3`. ✓

All five reproduce exactly. The count `nm | rg -c 'Opcode_*_Slot_*_encode'` = 12569 equals the
`num_encode_fns` immediate `0x3119` = 12569.

---

## 8. Function & symbol map

| Symbol | Address | Role |
|---|---|---|
| `interface_version` | `0x3b5b20` | libisa ABI rev `0x76` = 118 |
| `get_config_table` | `0x3b5b30` | → `config_table` @ `0x85ea40` (.data) |
| `num_*` (per table) | §1 column | each `mov $count,%eax; ret` — the authoritative count |
| `formats[]` / `slots[]` / `fields[]` | `0x6cd980` / `0x6cdb00` / `0x74aa00` | format / slot / field tables (§4.1–4.3) |
| `operands[]` / `iclasses[]` | `0x746d80` / `0x7330c0` | operand codec / iclass tables (§4.4–4.5) |
| `opcodes[]` / `opcodedefs[]` / `decodes[]` | `0x6ce6c0` / `0x6e9640` / `0x6ce3c0` | roster / encode matrix / per-slot decoders (§4.6–4.8) |
| `regfiles[]` / `regfile_views[]` | `0x74a800` / `0x74a780` | 8 files / 4 views (§4.9–4.10) |
| `Opcode_excw_Slot_inst_encode` | `0x338610` | encode thunk template `0x2080` (§5.1) |
| `Slot_inst_decode` | `0x3697a0` | per-slot inverse classifier (§5.2) |
| `Field_t_Slot_inst_get` / `_set` | `0x312c10` / `0x312c20` | per-field bit codec (§5.3) |
| `Slot_x24_Format_inst_0_get` / `_set` | `0x3b0450` / `0x3b0490` | slot scatter codec (§5.4) |
| `OperandSem_…_soffsetx4_encode` / `_decode` | `0x337230` / `0x337220` | operand value codec (§5.5) |
| `Operand_soffsetx4_ator` / `_rtoa` | `0x3384d0` / `0x3384e0` | operand PC-reloc codec (§5.5) |

---

## 9. Cross-references

- [The FLIX VLIW Encoding](flix-encoding.md) — the **content** of `formats[]`/`slots[]`: the 14
  formats' selectors and lengths, the 46 slots' bit offsets/widths/units, and the encode-thunk
  pattern applied to *laying out a bundle*. This page is the struct it parses.
- [The Canonical ISA Decode Model (libisa-core)](libisa-decode-model.md) — the decode traversal
  and the shipped (1534/12569) vs authoring (1607/12642) placement fold.
- [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) — the byte-stream decode
  worked end to end; it links here for the assemble direction and codec ABI.
- [The Eight Register Files](register-files.md) — the `regfiles[]`/`regfile_views[]` roster,
  ports, and bridges; the byte-witness for the stride-56 layout in §4.9.
- [ctype / coproc / funcUnit Tables](ctype-coproc-funcunit.md) — the semantics of `ctypes[]`,
  `coprocs[]`, `funcUnits[]` whose structs are listed in §4.11.
- [The TIE Database & Four Independent ISA Sources](tie-database.md) — where the authoring
  superset (the source of the `+73` fold) lives.
- [ISA Coverage & the 1534/1607/12642 Tally](coverage-tally.md) — the placement-count census.
- [Master Glossary → Register files & ISA metadata](../../glossary.md#register-files--isa-metadata)
  — one-line anchors for the metadata fields named here.

---

*Provenance: every table base VMA, stride, and field offset is `[HIGH/OBSERVED]` — read from the
accessor machine code (`objdump -d`) and cross-checked against the raw table bytes (`xxd` /
`readelf -x`) and the `R_X86_64_RELATIVE` relocations in the shipped `libisa-core.so` (sha256
`8fe68bf4…f143e451`). The codec thunk bodies (encode/decode/field/operand/reloc) are
`[HIGH/OBSERVED]` — disassembled and read as literal bytes. `protos[]`/`xfer_protos[]`
second-level element offsets are `[MED]`. All facts read as derived from shipped-artifact static
analysis (lawful interoperability RE).*
