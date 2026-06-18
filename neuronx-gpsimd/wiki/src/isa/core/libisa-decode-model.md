# The Canonical ISA Decode Model (libisa-core)

This page is the authoritative **object/table model** of how `libisa-core.so` decodes a
Vision-Q7 instruction: the object hierarchy a reimplementer mirrors
(`ISA → format → slot → opcode → field → operand`), the decode entry points and their call
graph, and the full enumeration of the decode *algorithms* the library contains — each
reproduced as annotated C pseudocode that names the real `libisa-core.so` symbols it ports.
Everything here is read **directly from the shipped binary** (the non-stripped `.symtab`, the
accessor machine code, and the raw `.data.rel.ro` / `.rodata` / `.data` table bytes); every
count, address, struct offset, and predicate quoted below was re-disassembled or re-read
in-checkout this pass.

This page owns the **decode pipeline as a model**. The two sibling pages own the adjacent
material and are cross-linked rather than duplicated:

* The byte-level *bundle-decode methodology* — the `op0`/`b3lo` anatomy, the `format_decoder`
  predicate ladder reproduced bit-by-bit, the 256-cell `length_table`, the `Slot_*_get`
  scatter bodies, and a fully worked oracle-validated decode — is
  [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) (**Part 0**). This page
  is the libisa-core *object model* behind that methodology; where Part 0 reproduces a body
  byte-for-byte, this page names the table slot it sits in and the call that reaches it.
* The raw *table schema & codec ABI* — every table's C struct layout, the stride/offset
  recipe, the `encode_fn` template ABI, and the assemble (encode) direction — is
  [The libisa Table Schema & Codec ABI](libisa-table-schema.md) (**sibling #612**). This page
  uses those structs as the model's classes; it does not re-derive their byte offsets.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/immediate/string read from the shipped binary (or executed this pass);
`INFERRED` = reasoned over OBSERVED facts; `CARRIED` = re-used at a cited report's confidence;
crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA**
(a reimplementation trap), **CORRECTION** (overturns a naive or earlier reading), **NOTE**
(orienting context).

> **NOTE — the binary, and its address arithmetic.** All addresses are in `libisa-core.so`
> (`ncore2gp/config/`, sha256
> `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`, 9,690,712 B, ET_DYN
> x86-64, **not stripped**, 45,198-entry `.symtab`, **no DWARF**). `.text`/`.rodata` are
> VMA == file-offset; the `.data.rel.ro` and `.data` tables have a **per-binary** delta of
> `0x200000` (`readelf -SW`: `.data` VMA `0x764040` ↔ file `0x564040`), so a table like `slots`
> @ VMA `0x6cdb00` is read at file offset `0x4cdb00`. The string pool is `.rodata` @ `0x3b6e40`
> (VMA == file). Pointer fields in the tables are filled at load time by `R_X86_64_RELATIVE`
> relocations (the on-disk image stores them as the target VMA).

---

## 1. What this model is — and what it is NOT

`libisa-core.so` is a standard Cadence/Tensilica **libisa** configuration library: a pure
data-plus-thunks description of one configured Xtensa ISA (here the Vision-Q7 *Cairo* config,
`Xm_ncore2gp`, `Xtensa24`/XEA3, `TargetHWVersion=NX1.1.4`). It exports exactly the
`xtensa-isa-internal` accessor surface (159 defined exports), and a host disassembler
(`xtensa-elf-objdump`, the device ISS, the lift tooling) `dlopen`s it and calls those accessors
to turn raw bytes into a decoded instruction. The "decode model" on this page is that accessor
surface plus the tables it reads and the thunks it dispatches to.

> **GOTCHA — this is the *ISA* decoder, not the host-runtime resolvers, not the firmware engine
> front-end, and not the device handler dispatch.** Four other "decode-shaped" mechanisms live in
> *different* binaries and must not be conflated with this model:
>
> * **`libnrtucode` host resolvers** (`get_ext_isa`, `get_hwdecode_table`,
>   `nrtucode_ll_get_libraries_from_opcodes`) pick *which ext-ISA library blob* to load for a
>   given `coretype`/`flavor`. They key purely on `coretype` (a per-generation jump table at
>   `0x556c` + the bitmask `0x2020202000` over coretypes `{13,21,29,37}`), **never** on opcode
>   *content*, and contain no `format_decoder` / `length_decoder` / `slot_get` machinery.
> * **The firmware SEQ-engine instruction front-end** (HW-decode vs Sunda software-fetch) is the
>   device-side instruction fetch FSM — a 178-entry `opcode − 0x41` dispatch table, with a real
>   silicon decode FIFO selectable by the `disable_hw_decode` CSR chicken bit. It is *not*
>   libisa.
> * **The device-side `kernel_info_table`** (embedded in each Q7 ext-ISA `.so`) dispatches a
>   *handler function* per `(opcode, specialization)` via a big-endian key
>   `(opcode<<24 | spec<<16) → func_addr` linear scan. That is the *post*-decode handler
>   dispatch, not the bit-field decode model.
> * **`get_hwdecode_table` → PROF_CAM/PROF_TABLE** are *profiler* tables (a 64-slot CAM keyed on
>   `opcode_id` with `mask`/`enable`), not the ISA format→slot→opcode hierarchy.
>
> See §10 for the relation. This page is *only* the `libisa-core.so` ISA decoder: format
> selection, length, per-slot bit-gather, per-slot opcode classification, and operand-field
> extraction. `[HIGH/OBSERVED]`

There is also a tiny companion library, `libisa-core-hw.so` (35.7 KB), loaded **first** under
`isa-base-dlls = [ libisa-core-hw.so, libisa-core.so ]`; it carries the bare Xtensa
hardware-core register/state partition (6 states + 15 sysregs, canonical SR numbers) and an
*empty* opcode/format/slot/field model (`num_formats = num_slots = num_opcodes = 0`). Its
`format_decoder`/`length_decoder` are `mov $0xffffffff; ret` stubs. The full TIE-extended decode
model is entirely in `libisa-core.so`; §9 covers the two-library composition. `[HIGH/OBSERVED]`

---

## 2. The object hierarchy a reimplementer mirrors

The model is a strict containment hierarchy. Each node is a row in a flat `.data.rel.ro` array;
each array is reached by a `num_<x>()` count accessor and a per-field scalar accessor, and the
codec functions hang off the leaf rows as function pointers. The accessor pattern is uniform:
sign-extend the index, `lea TABLE(%rip)`, scale by the table's stride, load the field at its
fixed offset. (The struct layouts, strides, and field offsets are the sibling schema page's
subject; this page treats each struct as a *class* and names only the fields the decode path
reads.)

```
ISA  (libisa-core.so, one configured Vision-Q7 ISA, interface_version = 118)
│   get_config_table() ─→ config_table  (IsaMemoryOrder=LittleEndian, IsaMaxInstructionSize=32, …)
│
├─ formats[14]                      "how a fetched word frames into slots"
│    each: { name, length(2/3/8/16), format_encode_fn }
│    decode_format_fn() ─→ format_decoder   (the ONE format selector, hardcoded)
│    decode_length_fn() ─→ length_decoder   (the ONE length selector, hardcoded → length_table)
│    Format_<fmt>_slots[] : the ordered (issue-order) list of global slot ids this format issues
│
├─ slots[46]                        "one issue position inside one format"
│    each: { name, format(name), nop(name), position(seq idx), get_fn, set_fn }
│    get_fn  : Slot_<fmt>_Format_<fmt>_s<N>_<unit>_<bitoff>_get   (bundle → 32-bit slotbuf, scatter)
│    set_fn  : …_set                                              (slotbuf → bundle, assemble)
│
├─ decodes[46]                      "the per-slot opcode classifier"   (one per slot)
│    each: { slot(name), decode_fn }
│    decode_fn : Slot_<slot>_decode(slotbuf) → const char* mnemonic   (masked-compare ladder)
│
├─ opcodes[1534]                    "one mnemonic"
│    each: { name, package, iclass(name), flags, stage tables, ldst descriptor }
│
├─ opcodedefs[12569]                "one legal (mnemonic × slot) placement"  (the encode matrix)
│    each: { opcode(name), slot(name), encode_fn }
│    encode_fn : Opcode_<mnem>_Slot_<slot>_encode(slotbuf)   (lays opcode-selector bits)
│
├─ iclasses[1447]                   "the operand signature shared by a family of opcodes"
│    each: { name, num_operands, operands[](name,inout), num_stateOperands, … }
│
├─ operands[232]                    "one operand of an iclass"
│    each: { name, field(name), regfile(name|""), num_regs, flags, encode/decode/do_reloc/undo_reloc }
│
├─ fields[3237]                     "one operand bitfield, per (field × slot) placement"
│    each: { name, slot(name), get_fn, set_fn }
│    get_fn  : Field_<field>_Slot_<slot>_get(slotbuf) → uint32   (raw bitfield isolate)
│
└─ regfiles[8] + regfile_views[4]   "where a register operand lives"
     AR, BR (+BR2/4/8/16 views), vec, vbool, valign, wvec, b32_pr, gvr
```

Two parallel "register/state" partitions — `states[81]`, `sysregs[34]`, `ctypes[64]`,
`ctype_protos[651]`, `protos[3484]`, `coprocs[1]`, `funcUnits[1]` — hang off the same ISA root
but are consumed by the assembler/scheduler, not the byte-decode path; they are documented on
the schema page. The decode path proper is the chain `formats → slots → decodes → opcodes →
iclasses → operands → fields`. `[HIGH/OBSERVED]`

### 2.1 The count manifest (the model's dimensions), binary-grounded

Every dimension is a `mov $imm,%eax; ret` count accessor. Re-read from the immediates this pass
(`objdump -d` on each accessor) and cross-checked against the `nm` symbol-family populations —
**not** a decompile grep, which inflates 2–12×.

| Count accessor | Addr | Immediate | Value | Backing table (VMA · stride) | `nm` symbol family (= count) |
|---|---|---|---|---|---|
| `interface_version` | `0x3b5b20` | `0x76` | **118** | (libisa ABI rev) | — |
| `num_formats` | `0x3b65e0` | `0x0e` | **14** | `formats` `0x6cd980` · 24 | `Format_*_encode` = 14 |
| `num_slots` | `0x3b6510` | `0x2e` | **46** | `slots` `0x6cdb00` · 48 | `Slot_*Format_*_get` = 46 |
| `num_decode_fns` | `0x3b64c0` | `0x2e` | **46** | `decodes` `0x6ce3c0` · 16 | `Slot_<slot>_decode` = 46 |
| `num_opcodes` | `0x3b61d0` | `0x5fe` | **1534** | `opcodes` `0x6ce6c0` · 72 | — |
| `num_encode_fns` | `0x3b6130` | `0x3119` | **12569** | `opcodedefs` `0x6e9640` · 24 | `Opcode_*_Slot_*_encode` = 12569 |
| `num_iclasses` | `0x3b5fb0` | `0x5a7` | **1447** | `iclasses` `0x7330c0` · 56 | — |
| `num_operands` | `0x3b5e80` | `0xe8` | **232** | `operands` `0x746d80` · 64 | — |
| `num_fields` | `0x3b5b40` | `0xca5` | **3237** | `fields` `0x74aa00` · 32 | `Field_*_get` = 3237 |
| `num_regfiles` | `0x3b5c20` | `0x08` | **8** | `regfiles` `0x74a800` · **56** | — |
| `num_regfile_views` | `0x3b5d50` | `0x04` | **4** | `regfile_views` `0x74a780` · 32 | — |
| `num_states` | `0x3b6670` | `0x51` | **81** | `states` `0x6ccf40` · 32 | — |
| `num_sysregs` | `0x3b6720` | `0x22` | **34** | `sysregs` `0x6ccb00` · 32 | — |
| `num_ctypes` | `0x3b67d0` | `0x40` | **64** | `ctypes` `0x6cbb00` · 64 | — |
| `num_ctype_protos` | `0x3b6d30` | `0x28b` | **651** | `ctype_protos` `0x67bb40` · 32 | — |
| `num_protos` | `0x3b6920` | `0xd9c` | **3484** | `protos` `0x680d40` · 88 | — |
| `num_xfer_protos` | `0x3b6c20` | `0x02` | **2** | `xfer_protos` `0x680cc0` · 56 | — |
| `num_coprocs` | `0x3b6dc0` | `0x01` | **1** | `coprocs` `0x67bb00` · 16 | — |
| `num_funcUnits` | `0x3b5bd0` | `0x01` | **1** | `funcUnits` `0x74a9c0` · 16 | — |

`num_interfaces = num_interface_classes = num_bypass_groups = 0` (their element accessors are
dead-index stubs — safe only because the counts are 0; do not parse). `[HIGH/OBSERVED]`

> **CORRECTION — `regfiles` stride is 56, not 64.** `regfile_name` (`0x3b5c30`) computes the
> row address as `lea (,rdi,8),rax ; shl $6,rdi ; sub rax,rdi`, i.e. `rdi*64 − rdi*8 = rdi*56`.
> An earlier schema draft listed the `regfiles` stride as 64; the binary says **56**
> (`8 × 56 = 448 = 0x1C0` bytes, zero-padded after entry 7). The `regfile_views` stride *is* 32
> (`shl $0x5`). This matters to anyone walking `regfiles[]` to resolve a register operand's
> file: at stride 64 you read garbage from entry 1 onward. `[HIGH/OBSERVED]`

### 2.2 The 1534/1607 ↔ 12569/12642 placement relationship

The model ships **1534 opcodes** and **12569 OPCODEDEF placements** (the legal
`(mnemonic × slot)` cross-product). These pair as **1534 ↔ 12569**: `num_opcodes() = 0x5fe`,
`num_encode_fns() = 0x3119`, and `nm` counts exactly 12569 `Opcode_*_Slot_*_encode` symbols.
The placement matrix is byte-complete — summing per-slot occupancy over all 46 slots totals
12569, and summing per-opcode placements over the 1534-mnemonic roster also totals 12569.

The standalone TIE database (the cleartext authoring source, decoded once) reports **1607
mnemonics** and **12642 placements** — the **pre-fold authoring superset**, `+73` mnemonics and
`+73` placements over the shipped runtime model. These pair as **1607 ↔ 12642**. The `+73`
delta is the authoring-vs-runtime *fold*: 73 author-time mnemonics collapse (alias/elide) before
the runtime tables are generated, each carrying exactly one placement, so both totals move by 73
in lockstep.

> **GOTCHA — never cross-pair the totals.** The correct pairs are **1607 ↔ 12642**
> (pre-fold / authoring) and **1534 ↔ 12569** (shipped / runtime). Pairing `1534` with `12642`
> or `1607` with `12569` mixes the authoring superset with the runtime fold and produces a
> `+73`/`−73` phantom discrepancy. The shipped `libisa-core.so` tables are authoritative for the
> decode model and are exactly **1534 / 12569**. `[HIGH/OBSERVED]` on the runtime pair (binary
> immediates + `nm`); `[HIGH/CARRIED]` on the pre-fold pair (TIE DB) and the `+73` fold count.

---

## 3. The decode entry points and the call graph

The whole decode model has exactly **two** top-level entry accessors, and both return a
*hardcoded* function address — the model is **not** table-driven at its root:

```c
// decode_format_fn (0x3b6650):   lea -0xce7(%rip),%rax  # 3b5970 <format_decoder> ; ret
// decode_length_fn (0x3b6660):   lea -0xc17(%rip),%rax  # 3b5a50 <length_decoder> ; ret
xtensa_format (*decode_format_fn(void))(const uint8_t*) { return &format_decoder; } // 0x3b5970
int           (*decode_length_fn(void))(const uint8_t*) { return &length_decoder; } // 0x3b5a50
```

A reimplementation hardcodes them too: there is **one** format decoder and **one** length decoder
for the entire ISA. From there the path fans out through per-slot and per-field function-pointer
tables. The full call graph for decoding one bundle:

```
decode_bundle(mem, pc)
│
├─ format_decoder(mem)            0x3b5970   → format index 0..13 / -1     (§4)
│     reads w = *(uint32_le*)mem ; mask-and-match predicate ladder
│
├─ length_decoder(mem)            0x3b5a50   → byte length 2/3/8/16 / -1   (§4)
│     idx = ((mem[3]&0xf)<<4) | (mem[0]&0xf) ; return length_table[idx]    (0x3d4100)
│
└─ for each slot sid in Format_<fmt>_slots[]   (issue order):
      ├─ slot_get_fn(sid)         0x3b65a0   → slots[sid].get  (+0x20)     (§5)
      │     Slot_<…>_get(insn, slotbuf)      bundle bits → 32-bit slotbuf  (scatter)
      │
      ├─ decode_fn(sid)           0x3b64f0   → decodes[sid].decode_fn (+0x8) (§6)
      │     Slot_<slot>_decode(slotbuf)      slotbuf → const char* mnemonic (masked-compare)
      │       └─ (internally calls Field_<sel>_Slot_<slot>_get to read selector bits)
      │
      └─ for each operand of the opcode's iclass:                          (§7)
            ├─ field_get_fn(field_idx)  0x3b5b90 → fields[field_idx].get (+0x10)
            │     Field_<field>_Slot_<slot>_get(slotbuf) → raw bitfield value
            ├─ operand_undo_reloc_fn(...) 0x3b5f90 → operands[op].undo_reloc  (PC-relative ops)
            └─ operand_decode_fn(...)     0x3b5f50 → operands[op].decode       (sign-ext/scale/bias)
                  (the OperandSem_<x>_decode leaf does the value transform)
```

The dispatch accessors are themselves one-line table indexers, re-disassembled byte-exact this
pass:

| Dispatch accessor | Addr | Indexes | Stride · field | Yields |
|---|---|---|---|---|
| `slot_get_fn` | `0x3b65a0` | `slots[i]` @ `0x6cdb00` | 48 · `+0x20` | `Slot_<…>_get` |
| `slot_set_fn` | `0x3b65c0` | `slots[i]` @ `0x6cdb00` | 48 · `+0x28` | `Slot_<…>_set` |
| `decode_fn` | `0x3b64f0` | `decodes[i]` @ `0x6ce3c0` | 16 · `+0x08` | `Slot_<slot>_decode` |
| `encode_fn` | `0x3b6180` | `opcodedefs[i]` @ `0x6e9640` | 24 · `+0x10` | `Opcode_*_Slot_*_encode` |
| `field_get_fn` | `0x3b5b90` | `fields[i]` @ `0x74aa00` | 32 · `+0x10` | `Field_<f>_Slot_<s>_get` |
| `field_set_fn` | `0x3b5bb0` | `fields[i]` @ `0x74aa00` | 32 · `+0x18` | `Field_<f>_Slot_<s>_set` |
| `operand_decode_fn` | `0x3b5f50` | `operands[i]` @ `0x746d80` | 64 · `+0x28` | `OperandSem_<x>_decode` |
| `operand_undo_reloc_fn` | `0x3b5f90` | `operands[i]` @ `0x746d80` | 64 · `+0x38` | `Operand_<x>_rtoa` |

`[HIGH/OBSERVED]` — `decode_format_fn`/`decode_length_fn` bodies and every dispatch accessor's
`lea`/`shl`/`mov OFF(...)` were re-disassembled this pass (`decode_fn`: `lea decodes ; shl $4 ;
mov 0x8(%rax,%rdi,1)`; `slot_get_fn`: `lea slots ; lea(rdi,rdi,2) ; shl $4 ; mov 0x20(...)`;
`encode_fn`: `lea opcodedefs ; lea(rdi,rdi,2) ; mov 0x10(%rax,%rdx,8)`).

> **QUIRK — decode is *function-pointer* dispatch, encode is *index* dispatch.** The decode side
> selects a thunk by *slot* (one `Slot_<slot>_decode` per slot, 46 total — the
> `num_decode_fns = num_slots = 46` identity). The encode side selects a thunk by *placement*
> (one `Opcode_<mnem>_Slot_<slot>_encode` per legal `(mnemonic, slot)`, 12569 total). There is
> **one decoder per slot but 12569 encoders**, because a slot's decoder is a single
> masked-compare ladder that recognises *every* opcode legal in that slot, whereas each encoder
> lays down the fixed bits for *one* specific opcode in that slot. Decode is many-opcodes→one-fn;
> encode is one-opcode→one-fn-per-slot. `[HIGH/OBSERVED]`

---

## 4. Decode algorithm #1 — format selection, and #2 — length

These two algorithms are reproduced bit-by-bit, with the full predicate ladder and the 256-cell
`length_table`, on [Part 0 §3–§4](../../reference/flix-decoding.md#3-format-selection--format_decoder-0x3b5970).
Here they are the model's two root entry points; the essentials, as the object model presents
them.

**`format_decoder` (`0x3b5970`)** reads `w = *(uint32_le*)inst` (`mov (%rdi),%edx`) and runs a
fixed mask-and-match ladder — the first hit wins and returns the format index `0..13`, else `-1`.
The `op0 = w & 0xF` nibble is the primary selector; for the FLIX cases (`op0 ∈ {0xE, 0xF}`) the
`b3lo = (w>>24) & 0xF` nibble and a few byte-3 high bits (the `0x0B00000F` / `0x0800000F` /
`0x3700000F` / `0x1900000F` / `0x0900000F` masks) further separate the eight wide and three
narrow formats. Verified this pass: the body opens `test $0x8,%dl ; je → ret 0` (x24),
`and $0xc / cmp $0x8 → 1` (x16a), `and $0xe / cmp $0xc → 2` (x16b), `and $0xb00000f /
cmp $0x100000f → 3` (F0), `and $0x800000f / cmp $0x800000e → 4` (F11), `and $0x3700000f /
cmp $0x300000f → 5` (F1) … and closes with `and $0x900000f / cmp $0xf → 13` (N0) and a
`mov $0xffffffff ; cmovne` default of `-1`. The mask/compare immediates are byte-exact against
Part 0's table.

**`length_decoder` (`0x3b5a50`)** takes **raw bytes** (not the word), forms an 8-bit index, and
loads the 256-entry `int32` `length_table` at `0x3d4100`. The body, re-disassembled this pass:

```c
int length_decoder(const uint8_t *inst) {           // 0x3b5a50
    uint8_t hi = inst[3] << 4;                       // movzbl 3(%rdi); shl $4; movzbl %al → (b3<<4)&0xff
    uint8_t lo = inst[0] & 0x0F;                     // movzbl (%rdi); and $0xf
    return length_table[hi | lo];                    // lea length_table(0x3d4100); mov (%rdx,%rax,4),%eax
}
```

Because `inst[3] << 4` is masked to 8 bits (`movzbl %al`), the row index is `(inst[3] & 0xF) << 4`
— `b3lo` in the high nibble, `op0` in the low. The table yields `{3,2,16,8}` (and `-1` for the
two illegal `op0==0xF, b3lo∈{7,f}` cells); the seven distinct length *outcomes* the corpus refers
to are the four byte-sizes plus the `op0==0xF` 8-vs-16 split keyed on `b3lo` parity plus the
illegal `-1`.

> **CORRECTION — `op0==0xF` is *not* a constant 8 bytes.** The Tensilica static macro
> `XCHAL_BYTE0_FORMAT_LENGTHS` keys length on byte 0 alone and flattens `op0==0xF` to 8. The
> shipped runtime `length_decoder` reads `byte3` too, so an `op0==0xF` word decodes to 8, 16,
> **or** illegal depending on `b3lo`. The binary `length_table` is authoritative; assuming a flat
> 8 desyncs every `op0==0xF` *wide* bundle. (Full treatment: Part 0 §2/§4.) `[HIGH/OBSERVED]`

The 14 formats and their slot rosters (the `formats[]` rows and `Format_<fmt>_slots[]` lists)
are tabulated on [Part 0 §3](../../reference/flix-decoding.md#the-14-formats) and
[the FLIX encoding page](flix-encoding.md). For the object model the salient facts are: format
indices `{0:x24, 1:x16a, 2:x16b, 3:F0, 4:F11, 5:F1, 6:F2, 7:F3, 8:F4, 9:F6, 10:F7, 11:N1,
12:N2, 13:N0}`; lengths `{x24:3, x16*:2, wide:16, narrow:8}`; slot-count census
`1+1+1+4+5+4+4+5+4+4+4+3+2+4 = 46 = num_slots`; the two 5-slot formats are `F3` and `F11`; and
the `Format_<fmt>_slots[]` order is **issue** order, physically out-of-bit-order for
`F3`/`F11`/`F6`/`F7`. `formats[0]` is `{name="x24", length=3, encode=0x3b57c0}` (read byte-exact
this pass), confirming the table layout. `[HIGH/OBSERVED]`

---

## 5. Decode algorithm #3 — per-slot bit-gather (`Slot_*_get`)

For each slot id the chosen format issues, `slot_get_fn(sid)` returns `slots[sid].get` (+0x20) —
a `Slot_<fmt>_Format_<fmt>_s<N>_<unit>_<bitoff>_get` thunk that **scatters** the slot's
non-contiguous bits out of the (up to four) 32-bit lanes of the bundle into a fresh 32-bit
normalized `slotbuf` word. There are exactly **46** such thunks (one per slot;
`nm | rg -c 'Slot_.*Format_.*_get'` = 46), and the get address embedded in each symbol equals the
`slots[i].get` pointer at table offset `+0x20`, so the symbol name and the table cross-check by
address.

The thunk bodies are arbitrary per-slot `and`/`shr`/`shl`/`or` chains — the `_<bitoff>_` token
(`4`, `16`, `28`, `41`, `54`, `58`, …) marks where the slot's *low byte* starts but the rest
scatters across high bits, so a slot's decoded width is **not** `next_offset − offset`; the body
is authoritative. Every `s0` slot starts at bit 4 because bits `[3:0]` are always the `op0`/format
selector. A representative gather body (the N0 LdSt slot, `0x3b5400`, which zero-fills
`slotbuf[1..7]` then assembles `slotbuf[0]` from seven scattered ranges) and the per-format
offset/width roster are reproduced on
[Part 0 §5](../../reference/flix-decoding.md#5-per-format-slot-partition--the-slot_get-bit-gather);
the model treats them as `slots[sid].get` function pointers.

```c
// per-slot bit-gather, as the model invokes it
uint32_t slotbuf[8] = {0};
slot_get_fn(sid)(insn, slotbuf);   // slots[sid].get : Slot_<…>_get @ slots[sid] +0x20
// slotbuf[0] now holds the slot's opcode-selector + operand bits, normalized to bit 0
```

The inverse `slot_set_fn(sid)` (`slots[sid].set`, +0x28; also 46 thunks,
`nm | rg -c 'Slot_.*Format_.*_set'` = 46) scatters a normalized `slotbuf` *back* into the bundle
for the assemble direction (schema page §3). `[HIGH/OBSERVED]`

---

## 6. Decode algorithm #4 — per-slot opcode classification (`Slot_*_decode`)

Once a slot is gathered into `slotbuf`, `decode_fn(sid)` returns `decodes[sid].decode_fn` (+0x8)
— a `Slot_<slot>_decode` classifier (exactly **46**, one per slot; this is the
`num_decode_fns = num_slots` identity). The classifier runs a **masked-compare ladder** over the
opcode-selector bits of `slotbuf` and returns a pointer into the opcode-name string pool
(`.rodata` @ `0x3b6e40`). It is the exact inverse of the `Opcode_<mnem>_Slot_<slot>_encode`
templates (§7.1).

The `decodes[]` table is stride 16, `{ slot_name(+0), decode_fn(+8) }`. Verified this pass:
`decodes[0] = { 0x3c9b79 → "Inst", 0x3697a0 = Slot_inst_decode }`,
`decodes[1] = { 0x3c9b86, 0x36b130 }`. The 46 `Slot_<slot>_decode` symbols span every slot,
including the three `None` filler slots (`Slot_n0_s1_none_decode`, `…_n0_s2_none_decode`,
`…_n1_s1_none_decode`, each recognising only `nop`) and the three base/density slots
(`Slot_inst_decode`, `Slot_inst16a_decode`, `Slot_inst16b_decode`).

A real classifier body, re-disassembled this pass — `Slot_inst_decode` (`0x3697a0`), the x24
base-ISA decoder:

```c
const char* Slot_inst_decode(const uint32_t *slotbuf) {        // 0x3697a0
    const char *pool = (const char*)0x3b6e40;                  // lea  string pool
    uint32_t w = slotbuf[0];                                   // mov  (%rdi),%edx
    if ((w & 0xFFFFFF) == 0x60000) return pool + ...;          // and $0xffffff; cmp $0x60000
    uint32_t op = (w << 8) >> 0x18;                            // shl $8; shr $0x18  → bits[23:16]
    switch (op) {                                              // cmp $0x51 / $0x52 / $0x70 / $0x92 / $0xf9 / $0xff …
        case 0x51: ...   case 0x52: ...   case 0x70: ...
        case 0x92: ...   case 0xf9: ...   case 0xff: ...
    }
    uint32_t op0lo = (uint8_t)w;                               // movzbl %dl
    switch (op0lo) { case 0x35: return pool + 0x37; ... }      // peel low byte for the dense groups
    // … (a multi-level masked-compare tree, one leaf per opcode legal in this slot)
}
```

This is the literal inverse of the encode templates: e.g. `Opcode_excw_Slot_inst_encode`
(`0x338610`) is `movl $0x2080,(%rdi); ret` and `Slot_inst_decode` recognises that selector
pattern as `excw`; `Opcode_rsr_sar_Slot_inst_encode` lays `0x00030300` (`RSR | SAR<<8`) and the
same ladder peels bits `[15:8]` for the SR number after matching `op1=3, op2=0`. The narrow/wide
slots' classifiers have the same shape over their own selector windows. `[HIGH/OBSERVED]`
(Slot_inst_decode head and the `excw` encode-template inverse spot-checked byte-exact this pass.)

> **NOTE — the classifier returns a *name pointer*, not an index, at this layer.** The slot
> classifier hands back a `const char*` into the string pool. To reach the opcode's *iclass*
> (and thus its operands) the model resolves the name through `opcodes[]` — the pool entry the
> classifier returns is the same `opcodes[i].name` pointer, so a downstream consumer either
> stores opcode names or builds a name→index map once. (The Cadence-generated `xtensa-modules.c`
> form of the same classifier returns the `OPCODE_<X>` enum value, which *is* the `opcodes[]`
> index; the binary thunk returns the equivalent name pointer. Same classification, different
> return convention.) `[HIGH/OBSERVED]`

---

## 7. Decode algorithm #5 — operand extraction (`Field_*_get` ∘ `OperandSem_*_decode`)

With the opcode known, operands are read per the opcode's **iclass**. The iclass is the operand
*signature* shared by a family of opcodes: `opcodes[i].iclass` names an `iclasses[]` row whose
`operands[]` sub-array lists `(operand_name, inout)` pairs. Each `operands[]` row names a **field**
(the bitfield), a **regfile** (or empty string for immediates/offsets), and four codec function
pointers `{encode, decode, do_reloc, undo_reloc}`. (Verified this pass:
`opcodedefs[0] = {opcode="excw", slot="Inst", …}` → `opcodes[0]` → `iclasses[0].name =
"xt_iclass_excw"`, a fully consistent chain.)

The crucial structural fact is that the **same logical field sits at a *different* bit position in
each slot it appears in**, so the field bitfield-extractor is keyed per `(field × slot)`: there
are **3237** `Field_<field>_Slot_<slot>_get` thunks (`num_fields = 0xca5 = 3237`,
`nm | rg -c 'Field_.*_get'` = 3237; 3230 carry a matching `_set`). The composition the model runs
per operand is `slot.get → field_get` then the operand value transform:

```c
// extract one operand of opcode `mnem` from the gathered slotbuf
const iclass_t *ic = opcode_iclass(mnem);             // opcodes[].iclass → iclasses[]
for (int a = 0; a < ic->num_operands; a++) {
    int op_idx = ic->operand[a];                       // → operands[op_idx]
    int fld    = operands[op_idx].field;               // the (field × this slot) placement
    uint32_t raw = field_get_fn(fld)(slotbuf);         // Field_<field>_Slot_<slot>_get : raw bitfield
    if (operands[op_idx].undo_reloc)                   // PC-relative operands only
        operands[op_idx].undo_reloc(&raw, pc);         // Operand_<x>_rtoa : raw += (pc & ~3)
    operands[op_idx].decode(&raw);                     // OperandSem_<x>_decode : sign-ext / scale / bias
    out.ops[a] = (operand_t){ .value = raw,
                              .regfile = operands[op_idx].regfile };
}
```

A real `Field_*_get` body — `Field_t_Slot_inst_get` (`0x312c10`), the x24 AR `t`-register nibble,
re-disassembled this pass:

```c
uint32_t Field_t_Slot_inst_get(const uint32_t *slotbuf) {   // 0x312c10
    uint32_t w = slotbuf[0];                                // mov (%rdi),%eax
    return (w << 0x18) >> 0x1c;                             // shl $0x18; shr $0x1c → bits[7:4]
}
```

— a left-justify-then-right-justify idiom that isolates a 4-bit field. The same field `t` in a
FLIX slot lands at a different bit position and gets its own thunk; immediates use a direct
`movzbl` of the right byte. (Full ABI on the schema page §4 and Part 0 §6.)

The **`OperandSem_<x>_decode`** leaves (95 in the binary; `nm | rg -c 'OperandSem_.*_decode'` = 95)
apply the value transform — sign-extension, scaling, bias, and PC-relative rebasing. The canonical
example, `soffsetx4` (the `operands[0]` row, verified this pass to be
`{ name="soffsetx4", field="offset", regfile="", flags=2, encode=0x337230, decode=0x337220,
do_reloc=0x3384d0, undo_reloc=0x3384e0 }`):

```c
// disassemble: slot.get → Field_offset_Slot_<s>_get → undo_reloc → decode
int OperandSem_opnd_sem_soffsetx4_decode(uint32_t *v) {   // sign-extend 18-bit, scale ×4, +4
    *v = ((int)(*v << 0xe) >> 0xc) + 4;  return 0;
}
int Operand_soffsetx4_rtoa(uint32_t *v, uint32_t pc) {    // operand→address (undo_reloc)
    *v += (pc & ~3u);  return 0;
}
```

so a branch target prints as `soffsetx4 = (PC & ~3) + (sext18 << 2)`. Register operands carry
their `regfile` name (one of the 8 files in §8) instead of a value transform.

> **QUIRK — `operands[0].regfile` is the *empty string*, not NULL.** `soffsetx4` is a
> PC-relative branch offset with no register file, so its `regfile` pointer (`0x3c4ec4`) targets
> the empty string `""`, not `0`. AR/BR-less operands point at `""`; only a genuinely absent
> field is `NULL`. A consumer that tests `regfile != NULL` to mean "is a register" is wrong —
> test the *string contents*. `[HIGH/OBSERVED]`

> **NOTE — operand-count reconciliation.** `libisa-core.so`'s `operands[]` table is **232** rows
> (`num_operands = 0xe8`) — the *codec* operands (one row per distinct field/regfile/codec
> tuple). The Cadence `xtensa-modules.c` `OPERAND_*` enum is larger (1517) because it also
> enumerates implicit/state operands and per-iclass argument slots that share a codec. Both are
> correct for their layer; the **232** figure is the binary `operands[]` table this model reads.
> `[HIGH/OBSERVED]` on 232; `[HIGH/CARRIED]` on the 1517 enum count.

---

## 8. The register-operand model (where a decoded register lives)

A register operand's `operands[].regfile` names one of **8** `regfiles[]` rows. Read byte-exact
from the table (stride 56, §2.1 CORRECTION), the files are:

| idx | name | short | width | count | ctype | coproc |
|---|---|---|---|---|---|---|
| 0 | `AR` | `a` | 32 | 64 | `_TIE_uint32` | core |
| 1 | `BR` | `b` | 1 | 16 | `_TIE_xtbool` | core |
| 2 | `vec` | `v` | 512 | 32 | `_TIE_xt_ivp32_xb_vec2Nx8` | Vision |
| 3 | `vbool` | `vb` | 64 | 16 | `_TIE_xt_ivp32_vbool2N` | Vision |
| 4 | `valign` | `u` | 512 | 4 | `_TIE_xt_ivp32_valign` | Vision |
| 5 | `wvec` | `wv` | 1536 | 4 | `_TIE_xt_ivp32_xb_wvecspill` | Vision |
| 6 | `b32_pr` | `pr` | 64 | 16 | `_TIE_xt_ivp32_xb_int64pr` | Vision |
| 7 | `gvr` | `gr` | 512 | 8 | `_TIE_xt_ivp32_xb_gsr` | Vision |

`BR` carries four narrowed views `BR2`/`BR4`/`BR8`/`BR16` (the 4 `regfile_views[]` rows) for
2/4/8/16-bit boolean tuples. The decode model uses the regfile only to *name* a decoded register
index (`v0..v31`, `a0..a63`, …); the index itself comes from the operand's `Field_*_get`. All
eight files are caller-saved in this config (`num_callee_saved = 0`). The full register
architecture — including the `gvr` "global state register" file that the binary exposes but the
shipped TIE header omits — is on [the register-files page](register-files.md). `[HIGH/OBSERVED]`

---

## 9. How the model is parameterized — config, and the two-library composition

The model has a single configuration root: `get_config_table()` (`0x3b5b30`, body
`lea 0x4a8f09(%rip),%rax # 85ea40 ; ret`) returns `config_table` @ `0x85ea40`, a NULL-terminated
array of `{const char* key, const char* value}` 16-byte pairs. Decoded byte-exact this pass:

| key | value | bearing on decode |
|---|---|---|
| `IsaMemoryOrder` | `LittleEndian` | byte→lane packing: `insn[w] |= byte[i] << 8*(i&3)` |
| `IsaMaxInstructionSize` | `32` | the fetch-window width (bundles use 2/3/8/16) |
| `PipeWStage` | `6` | pipeline write stage (scheduling, not byte-decode) |
| `DataMemWidth` | `512` | data memory width |
| `IsaCoprocessorCount` | `7` | coprocessor count |
| `IsaUseBooleans` | `1` | the `BR` boolean file is present |

`IsaMemoryOrder = LittleEndian` and `IsaMaxInstructionSize = 32` are the only two keys the
byte-decode path consumes (lane packing + fetch width); the rest parameterize scheduling and the
register model. `interface_version() = 118` pins the libisa ABI revision a host must speak.
`[HIGH/OBSERVED]`

**The two-library composition.** A host loads two libisa libraries in order
`isa-base-dlls = [ libisa-core-hw.so, libisa-core.so ]` and **merges** their tables (neither lib
has table-registration constructors — the host reads each lib's exported tables after `dlopen`).
The two partition the special-register space:

* `libisa-core-hw.so` (35.7 KB) — the **bare Xtensa hardware-core** layer: 6 states + 15 sysregs
  with canonical SR numbers (`LBEG=0, LEND=1, LCOUNT=2, BR=4, MMID=89, DDR=104, CCOUNT=234, …`)
  from the stock `xt_core`/`xt_debug`/`xt_timer` packages. **Every** opcode/format/slot/field
  count is **0**; its `format_decoder`/`length_decoder` are `mov $0xffffffff; ret`. It also carries
  two extra `config_table` keys `ConfigKey0 = 0x5fa7c9e6` / `ConfigKey1 = 0xb2aebb83` (the
  Tensilica per-configuration signature words).
* `libisa-core.so` — the full **TIE-extended** ISA: all 14 formats, 46 slots, 1534 opcodes,
  12569 placements, 3237 fields, the eight regfiles, and the extended (`VECBASE`/`EPC`/`WB`/`MPU`/
  …) state+SR space.

Their register sets are **disjoint** (0 of the hw lib's 6 states and 0 of its 15 sysregs appear in
the core lib's 81 states / 34 sysregs): the hw lib owns the architectural core registers, the core
lib owns the config-extended ones. For the *decode model* this means the byte→instruction pipeline
is entirely in `libisa-core.so`; the hw lib contributes only the bare-core SR names a fully merged
model resolves. `[HIGH/OBSERVED]` on the counts/tables/load order; `[MED/INFERRED]` on "merged at
load" (the load order and disjoint sets imply it; the host merge code is in `libnrtucode`, not
these libs).

> **NOTE — config is not gen-gated *inside* libisa-core.** There is no `arch_id`/`coretype`/
> codename gate anywhere in `libisa-core.so`'s decode path — it models the single configured Cairo
> Vision-Q7 core, gen-invariant. Per-generation selection (SUNDA/CAYMAN/MARIANA/MARIANA+/MAVERICK)
> happens one layer up, in the `libnrtucode` resolvers (§10), which pick a different ext-ISA *blob*
> per `coretype` — not a different `libisa-core` decode model. `[HIGH/OBSERVED]`

---

## 10. Parallel decode mechanisms in adjacent layers (contrast, not part of this model)

For orientation — and to forestall the conflation the GOTCHA in §1 warns about — four other
decode-shaped mechanisms sit *around* this model in the `libnrtucode` host runtime, the firmware
SEQ engine, and the device ext-ISA libraries. None is the libisa-core ISA decoder; all are read
from `libnrtucode_internal.so` / the carved device images, a different corpus. They reference
`libisa-core.so` only as an **encoding oracle** (to confirm an IVP mnemonic is a real encoding),
never as a decode call path.

| Mechanism | Where | Keys on | What it produces | Relation to this model |
|---|---|---|---|---|
| `get_ext_isa` / `get_num_ext_isa_libs` | `libnrtucode` resolver (`0x9b2b30`) | `coretype` (jump table @ `0x556c`, 5 live slots of 32) + `flavor` validity | a `(so_ptr, so_len, json_ptr, json_len)` blob | picks **which** ext-ISA library to load; does not decode instructions |
| `nrtucode_ll_get_libraries_from_opcodes` | `libnrtucode` resolver (`0x9b1880`) | `coretype` bitmask `0x2020202000` + `getenv(CPTC_DECODE)` | a scalar library UID (`0` base, `3` cptc) | **opcode-content-blind** — never reads the opcodes; not a decode |
| SEQ instruction fetch (HW-decode / Sunda dual-fetch) | NX firmware SEQ engine (FSMs `0x31ac` / `0x2d81`) | `opcode − 0x41` → 178-entry dispatch table; mode = CSR `0x4000[0]` `disable_hw_decode` | a handler trampoline `jx` | the firmware **instruction-fetch** front-end; not libisa, no `format_decoder` |
| `get_hwdecode_table` → PROF_CAM/PROF_TABLE | `libnrtucode` (`0x9b2cd0`) → `hwdecode_table_list` (38×16) | flat `(gen,engine) idx` + `kind∈{CAM,TABLE}` | a `(ptr,size)` profiler blob (CAM 0x400, TABLE 0x2000) | a **profiler** CAM (`opcode_id`/`mask`/`enable`), not the ISA hierarchy |
| `kernel_info_table` | device Q7 ext-ISA `.so` | big-endian `(opcode<<24 | spec<<16)` linear scan | a handler `func_addr` to jump to | the **post-decode handler dispatch** — runs *after* libisa decode |

Two contrasts are worth stating in full, because they look most like a "decode" but aren't this
one:

* **The SEQ dual-fetch** is the only true "HW decode vs software model" split in the firmware, but
  at the *instruction-fetch* level: two co-resident FSMs — a hardware-assisted decode FIFO
  (`0x31ac`, with an `RTL_PC_check` coherence point against the firmware cache) and a software
  fetch (`0x2d81`, `l32i` cursor) — selected once at boot from an args flag and the `0x4000[0]`
  chicken bit. Both index the *same* 178-entry per-engine dispatch table by `opcode − 0x41` and
  call the *same* handler bodies; they differ only in the fetch FSM. This is a per-engine
  firmware dispatcher, entirely independent of libisa-core's `format_decoder`/`length_decoder`.
  `[HIGH/CARRIED]`
* **The device `kernel_info_table`** is the closest analogue to a decode, and the most
  instructive contrast: its 8-byte entry `{u8 _, u8 _, u8 spec, u8 opcode, u32 func_addr}` is
  keyed by a big-endian `(opcode, spec)` word and resolved by linear scan to a function address.
  That is a *handler* table — one implementation function per `(opcode, specialization)` — not a
  field extractor. Where this page's model answers "what mnemonic and operands does this 16-byte
  bundle hold?", the `kernel_info_table` answers "which firmware routine implements this
  already-decoded GPSIMD compute opcode?". They are sequential stages, not competitors. The CPTC
  data codec (`cptc_decode_impl<1..6>`, reached via `pool_conv_lut_load`, opcode `0xe4`) is a
  *data*-decompression kernel dispatched this way — it decodes a compressed-tensor format, not a
  machine-instruction stream, and it calls libisa only as an encoding oracle. `[HIGH/CARRIED]`

`[HIGH/OBSERVED]` on the host-resolver symbols/keys (read from `libnrtucode`); the firmware
SEQ-fetch and device handler addresses are `[HIGH/CARRIED]` from the firmware-decode reports.

---

## 11. The full decode loop, as the model assembles it

The whole pipeline, naming every real `libisa-core.so` symbol each step ports. `insn` is the
32-byte (`IsaMaxInstructionSize`) fetch window holding the bundle as eight little-endian `uint32`
lanes.

```c
typedef uint32_t insnbuf[8];                 // 32-byte fetch window, 8 LE uint32 lanes
typedef struct { const char *mnem; operand_t ops[8]; int n_ops; } decoded_slot;
typedef struct { int format, length, nslots; decoded_slot slot[5]; } decoded_bundle;

int decode_bundle(const uint8_t *mem, uint32_t pc, decoded_bundle *out) {
    // (a) byte -> lanes (config_table: IsaMemoryOrder == LittleEndian).
    insnbuf insn = {0};
    for (int i = 0; i < 32; i++) insn[i>>2] |= (uint32_t)mem[i] << (8 * (i & 3));

    // (b) format.  decode_format_fn() -> format_decoder @ 0x3b5970.            (§4)
    int fmt = format_decoder(mem);            // reads w = insn[0]; mask-match ladder
    if (fmt < 0) return -1;                    // illegal selector (op0=F, b3lo 7/f)
    out->format = fmt;

    // (c) length.  decode_length_fn() -> length_decoder @ 0x3b5a50 -> length_table 0x3d4100. (§4)
    int len = length_decoder(mem);
    if (len < 0) return -1;
    out->length = len;

    // (d..f) each slot the format issues, in Format_<fmt>_slots[] (issue) order.
    const slot_id *slots = format_slots[fmt];  // formats[fmt] -> ordered slot-id list
    out->nslots = format_nslots[fmt];
    for (int s = 0; s < out->nslots; s++) {
        slot_id sid = slots[s];
        uint32_t slotbuf[8] = {0};

        // (d) gather scattered slot bits.  slot_get_fn(sid) -> slots[sid].get.  (§5)
        slot_get_fn(sid)(insn, slotbuf);       // e.g. Slot_n0_..._s0_ldst_4_get @ 0x3b5400

        // (e) classify into a mnemonic.  decode_fn(sid) -> decodes[sid].decode_fn. (§6)
        const char *mnem = decode_fn(sid)(slotbuf);   // e.g. Slot_inst_decode @ 0x3697a0

        // (f) operands: opcode -> iclass -> args -> field_get -> operand_decode. (§7)
        out->slot[s].mnem = mnem;
        const iclass_t *ic = opcode_iclass(mnem);     // opcodes[].iclass -> iclasses[]
        for (int a = 0; a < ic->num_operands; a++) {
            int op = ic->operand[a];
            uint32_t raw = field_get_fn(operands[op].field)(slotbuf); // Field_*_Slot_*_get
            if (operands[op].undo_reloc) operands[op].undo_reloc(&raw, pc); // PC-relative
            operands[op].decode(&raw);                // OperandSem_<x>_decode (sign/scale/bias)
            out->slot[s].ops[a] = (operand_t){ raw, operands[op].regfile };
        }
        out->slot[s].n_ops = ic->num_operands;
    }
    return len;                                // advance the sweep pointer by `len`
}
```

A faithful port of this pipeline against the 14 formats, 46 gather thunks, 46 classifiers, 3237
field thunks, and 95 OperandSem leaves reproduces the device-native `xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`) per-slot mnemonics with **zero** disagreements across hundreds of
thousands of real slot decodes (the lift-tool cross-validation recorded **81,442 bundles /
549,375 per-slot mnemonics / 0 length errors** at 100% agreement, 12 of 14 formats
oracle-confirmed; `F4`/`F6` interiors are structurally covered but `[MED/INFERRED]` because no
shipped object emits them). `[HIGH/OBSERVED]` on the pipeline and the 12-format agreement;
`[MED/INFERRED]` on `F4`/`F6` interiors.

> **GOTCHA — the FLIX-desync wall.** A linear sweep over dense FLIX `.text` does not stay
> synchronized end to end even with a byte-exact length decoder, because device `.text`
> interleaves literal pools and boot stubs between bundles and the decoder cannot tell a literal
> word from a bundle word in-band. Detection (a `length_decoder` `-1`, an out-of-image branch
> target, a nonsense field) and recovery (re-anchor on a prologue / `retw.n` / the `.xt.prop.*`
> property records) are Part 0 §10's subject; the residual literal/boot spans are recoverable only
> with the per-image property records, not a smarter byte heuristic. And the **NCFW management
> core is scalar Xtensa-LX, not FLIX** — never route NCFW IRAM through this model (Part 0 §11).
> `[HIGH/OBSERVED]`

---

## 12. Adversarial self-verification (5 strongest claims, re-checked against the binary)

Each of the model's primary claims was re-grounded against `libisa-core.so` this pass.

1. **The two decode entry points return hardcoded addresses (not table lookups).**
   `objdump -d 0x3b6650`: `lea -0xce7(%rip),%rax # 3b5970 <format_decoder> ; ret`; `0x3b6660`:
   `lea -0xc17(%rip),%rax # 3b5a50 <length_decoder> ; ret`. **Confirmed** — one format decoder
   and one length decoder for the whole ISA. `[HIGH/OBSERVED]`
2. **`length_decoder` indexes `length_table` by `((byte3&0xf)<<4)|(byte0&0xf)`.** Body at
   `0x3b5a50`: `movzbl 0x3(%rdi); shl $0x4; movzbl %al; movzbl (%rdi); and $0xf; or; lea
   0x1e697(%rip) # 3d4100 <length_table>; mov (%rdx,%rax,4),%eax`. **Confirmed** byte-exact —
   and the `movzbl %al` after the shift is what masks `byte3` to its low nibble. `[HIGH/OBSERVED]`
3. **`num_decode_fns = num_slots = 46`, one classifier per slot.** `num_slots()` @ `0x3b6510` =
   `mov $0x2e`; `num_decode_fns()` @ `0x3b64c0` = `mov $0x2e`; `nm | rg -c 'Slot_.*Format_.*_get'`
   = 46 (gathers) and the `Slot_<slot>_decode` family (excluding `Field`/`Opcode`/`Format`) = 46
   (classifiers). `decodes[0] = {"Inst", 0x3697a0=Slot_inst_decode}`. **Confirmed.**
   `[HIGH/OBSERVED]`
4. **The placement matrix is 1534 ↔ 12569 (shipped), not 12642.** `num_opcodes()` @ `0x3b61d0` =
   `mov $0x5fe` (1534); `num_encode_fns()` @ `0x3b6130` = `mov $0x3119` (12569);
   `nm | rg -c 'Opcode_.*_Slot_.*_encode'` = **12569** exactly. The 1607/12642 pair is the TIE-DB
   pre-fold superset, not the binary. **Confirmed** the runtime pair from the binary.
   `[HIGH/OBSERVED]`
5. **The decode/codec chain is internally consistent across tables** (`opcodedefs[0] → decodes[0]
   → iclasses[0] → operands[0]`). `opcodedefs[0] = {opcode="excw", slot="Inst",
   encode_fn=0x338610}`; `0x338610` = `movl $0x2080,(%rdi); ret` (template `0x00002080`);
   `decodes[0].slot = "Inst"` (same `0x3c9b79` pointer); `opcodes[0]`→`iclasses[0].name =
   "xt_iclass_excw"`; `operands[0] = {name="soffsetx4", field="offset", regfile="",
   decode=0x337220, undo_reloc=0x3384e0}`. The encode template, the slot classifier, the iclass
   name, and the operand codec all reference one consistent `excw`/`Inst`/`soffsetx4` chain.
   **Confirmed** byte-exact. `[HIGH/OBSERVED]`

The two earlier-reading **CORRECTIONS** this pass overturns: the `regfiles` stride is **56**
(`rdi*64 − rdi*8`, from `regfile_name` @ `0x3b5c30`), not 64 (§2.1); and `operands[0].regfile`
resolves to the **empty string** (`0x3c4ec4`), not a TIE vector regfile name (§7 QUIRK).

---

## 13. Function & symbol map

All in `libisa-core.so` (`ncore2gp/config/`). `.text`/`.rodata`: VMA == file. `.data.rel.ro` /
`.data`: file = VMA − `0x200000`.

| Symbol | Addr | Role |
|---|---|---|
| `decode_format_fn` | `0x3b6650` | → `&format_decoder` (the one format selector) |
| `decode_length_fn` | `0x3b6660` | → `&length_decoder` (the one length selector) |
| `format_decoder` | `0x3b5970` | `w → format index 0..13 / -1` (§4) |
| `length_decoder` | `0x3b5a50` | raw bytes → length via `length_table` (§4) |
| `length_table` | `0x3d4100` (.rodata) | 256 × `int32`, `(b3lo,op0) → length` |
| `slot_get_fn` / `slot_set_fn` | `0x3b65a0` / `0x3b65c0` | → `slots[i].get` / `.set` (gather/scatter) |
| `decode_fn` | `0x3b64f0` | → `decodes[i].decode_fn` (per-slot classifier) |
| `encode_fn` | `0x3b6180` | → `opcodedefs[i].encode_fn` (per-placement template) |
| `field_get_fn` / `field_set_fn` | `0x3b5b90` / `0x3b5bb0` | → `fields[i].get` / `.set` (operand bitfield) |
| `operand_decode_fn` | `0x3b5f50` | → `operands[i].decode` (`OperandSem_*_decode`) |
| `operand_undo_reloc_fn` | `0x3b5f90` | → `operands[i].undo_reloc` (`Operand_*_rtoa`, PC-rel) |
| `get_config_table` | `0x3b5b30` | → `config_table` @ `0x85ea40` |
| `interface_version` | `0x3b5b20` | libisa ABI rev = `118` |
| `formats` | `0x6cd980` (.data.rel.ro) | 14 × `{name,length,encode}` stride 24 |
| `slots` | `0x6cdb00` (.data.rel.ro) | 46 × `{name,format,nop,position,get,set}` stride 48 |
| `decodes` | `0x6ce3c0` (.data.rel.ro) | 46 × `{slot,decode_fn}` stride 16 |
| `opcodes` | `0x6ce6c0` (.data.rel.ro) | 1534 × opcode rows, stride 72 |
| `opcodedefs` | `0x6e9640` (.data.rel.ro) | 12569 × `{opcode,slot,encode_fn}` stride 24 |
| `iclasses` | `0x7330c0` (.data.rel.ro) | 1447 × iclass rows, stride 56 |
| `operands` | `0x746d80` (.data.rel.ro) | 232 × operand rows, stride 64 |
| `fields` | `0x74aa00` (.data.rel.ro) | 3237 × `{name,slot,get,set}` stride 32 |
| `regfiles` | `0x74a800` (.data.rel.ro) | 8 × regfile rows, stride **56** |
| `config_table` | `0x85ea40` (.data) | 6 key/value ptr pairs + `{0,0}` terminator |
| `Slot_inst_decode` | `0x3697a0` | x24 base classifier (§6) |
| `Slot_n0_Format_n0_s0_ldst_4_get` | `0x3b5400` | worked N0 LdSt gather (§5 / Part 0) |
| `Field_t_Slot_inst_get` | `0x312c10` | x24 AR `t` extractor (§7) |
| `Opcode_excw_Slot_inst_encode` | `0x338610` | `excw` template `0x00002080` (decode inverse) |
| count accessors | see §2.1 table | `num_*` immediates |

---

## 14. Cross-references

* [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) (**Part 0**) — the
  byte-level decode reproduced bit-by-bit: `format_decoder` ladder, 256-cell `length_table`,
  `Slot_*_get` scatter bodies, a worked oracle-validated decode, the desync wall, and the
  scalar-LX NCFW exception.
* [The libisa Table Schema & Codec ABI](libisa-table-schema.md) (**sibling #612**) — every
  table's C struct layout, the stride/offset recipe, the encode-template ABI, and the assemble
  (encode) direction.
* [The FLIX VLIW Encoding](flix-encoding.md) — the 14-format / 46-slot / 7-length issue grid and
  the byte-3 sub-format map in full.
* [Register-File Model](register-files.md) — the 8 regfiles + 4 views, the `gvr` discrepancy,
  per-register dbnums.
* [ISA Coverage & the 1534/1607/12642 Tally](coverage-tally.md) — the placement-count census and
  the pre-fold/runtime fold accounting.
* [Core Identity & Configuration](identity-config.md) — the `config_table` keys, ConfigID/ConfigKey
  words, and the `interface_version`/`num_*` identity card.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — what the tags and the
  "walls" mean.
