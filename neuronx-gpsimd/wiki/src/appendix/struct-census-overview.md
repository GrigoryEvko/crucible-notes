# Struct / Vtable / Class Recovery Census — Overview & Index

> **Scope — the index for the struct-layout appendix.** This page is the **entry
> point** for the per-struct appendix: it documents *how* every struct, vtable
> and polymorphic class in the GPSIMD host+device corpus was recovered, *which*
> binaries the census ranges over, and *the ranking* that orders the field-exact
> deep-dive pages. It does **not** lay out fields itself — the byte tables live in
> the three sibling pages it indexes:
> [Host-Runtime Struct Layouts](struct-host-runtime-layouts.md) (the `libnrt.so`
> C++ IR + ABI structs), [Device-Firmware Global Structs](struct-device-firmware-globals.md)
> (the on-chip Vision-Q7 globals), and
> [Host Execution-State Structs + Census Close](struct-exec-state-census.md) (the
> execute-time/handle state). The completeness accounting lives in
> *The Coverage Ledger*.
>
> Tags per claim — `[CONF × PROV]`: `HIGH/MED/LOW` × `OBSERVED` (read directly
> from `nm`/`objdump`/`readelf`/`c++filt`/`strings` on a shipped ELF, from its
> IDA `*_structures.json`/`*_enums.json`/`*_rtti.json`/`*_xrefs.json` sidecar, or
> from a shipped `.h`), `INFERRED` (an ABI/CFG rule applied to an observed fact),
> `CARRIED` (taken from a backing pass and re-confirmed here against the binary).

> **NOTE — artifact provenance.** Every figure on this page is grounded **solely
> in static analysis** of the shipped binaries and their shipped headers: the
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64` package
> (`…/custom_op/c10/lib/{libnrtucode.so,libnrtucode_internal.so}` +
> `…/custom_op/c10/include/**`), the `ncore2gp` toolchain DLLs
> (`…/config/{libisa-core.so,libisa-core-hw.so}`), and — for the full host
> runtime — the separately-packaged `libnrt.so.2.31.24.0` from
> `aws-neuronx-runtime-lib_2.31.24.0` (the binary anchored in
> [The libnrt Surface Map](../runtime/libnrt-surface.md)). Recovered symbols,
> embedded `__FILE__`/assert strings, shipped headers, and the IDA `.json`
> sidecars are all **binary-derived and citeable**. Re-ground commands appear
> inline; no count is taken from the decompile listing.

---

## 0. One-screen orientation

The census answers one question for the reimplementer: *for each data structure
the GPSIMD path touches, do we know its exact byte layout, and how confident are
we?* The corpus splits cleanly by where the struct lives and how its layout is
recoverable:

* **Host C++ runtime (`libnrt.so`)** — *richest recovery.* Compiled with
  DWARF `.debug_info` and full RTTI, so the C++ IR classes (`kbin`'s
  `mem_ref_*`/`dma_desc_*`, the `enc_*` collectives ops, the `ntff::*` trace
  schema) yield both **vtable slot→method** maps and field types via the
  `dump()`/`parse()` **Rosetta-stone** technique (§1).
* **Host C ABI shims (`libnrtucode.so`, `libnrtucode_internal.so`)** — *stripped,
  zero RTTI.* No C++ vtables at all; layout truth comes entirely from the
  **shipped headers** (`nrtucode.h`, `nxlib*.h`) and their `static_assert`
  size/offset pins (§1.3). The IDA sidecars here recover **only the 5 standard
  `Elf64_*` ELF structs** — they are not a struct source (§2, CORRECTION).
* **ncore2gp config DLLs (`libisa-core.so`)** — *not stripped, but C, not C++.*
  Tensilica **TIE Ctype** register/field metadata tables emitted as plain C
  arrays (`ctype_regfile`, `Ctype__TIE_*_fieldNames`); recovered by **native
  byte-decode** against the `.data`/`.data.rel.ro` images (note the **0x200000**
  VMA→fileoffset delta, §1.4).
* **Device Vision-Q7 firmware globals** — *INFERRED interiors.* No host-readable
  symbols; layouts are reconstructed by native ncore2gp Xtensa byte-decode and
  cross-checked against the per-arch ISA headers. v5/Maverick interiors are
  **INFERRED** throughout (§3, [device globals](struct-device-firmware-globals.md)).

---

## 1. The recovery method

Four independent evidence channels are cross-checked against each other. A struct
earns **HIGH** confidence only when ≥2 channels agree on a size/offset; a single
channel is **MED**; an ABI rule with no direct read is **LOW/INFERRED**.

### 1.1 The `dump()` / `parse()` Rosetta-stone — field typing from override bodies `[HIGH × OBSERVED]`

The host C++ runtime gives every polymorphic IR class a `dump()` (sometimes
`print()`/`serialize()`) virtual in **slot 0** of its vtable, and many parse
counterparts. An override's body is a *type-annotated walk of the struct*: each
member is read at a fixed `this`-relative offset with a width and signedness
dictated by the print/format call. Disassembling the override therefore recovers
the field map without DWARF. The skeleton:

```c
// vptr = _ZTV<Class> + 0x10   (past offset-to-top + typeinfo header words)
// slot[0] = Class::dump(...)  — disassemble this body:
void Class::dump(Stream &s) {
    s.fmt("%s",   *(char**)  (this + 0x08));  // ⇒ field@0x08 : char*        (a name ptr)
    s.fmt("%u",   *(u32*)    (this + 0x10));  // ⇒ field@0x10 : uint32_t
    s.fmt("%lld", *(i64*)    (this + 0x18));  // ⇒ field@0x18 : int64_t      (signed: %lld)
    for (auto &e : *(vector*) (this + 0x20))  // ⇒ field@0x20 : std::vector<…> (begin/end/cap = 0x20/0x28/0x30)
        e.dump(s);
}
```

Each `mov`/format pair fixes one *(offset, width, signedness, pointer-ness)*
tuple; an inlined `std::vector`/`std::string` member is recognized by its
three-pointer (begin/end/cap) or `{ptr,len,cap}` shape. The canonical worked
example is `mem_ref_sp::dump(kbin_mem_ref&)` at slot 0 of `_ZTV` @`0xbf8c88`
(the **State-Buffer / on-chip SRAM reference** the kbin IR emits for a custom-op
descriptor); its 3-slot interface `[0]dump [1]dtor [2]deleting-dtor` and the
sibling `mem_ref_*`/`dma_desc_*` families are mapped in
[Host-Runtime Struct Layouts](struct-host-runtime-layouts.md) and consumed by the
[NEFF→device IR lowering](../neff/assembly-pipeline.md) and the
[DMA descriptor model](../dma/descriptor-model.md). `[HIGH × OBSERVED]`

> **GOTCHA — vptr base, not the symbol.** Slot N is read from
> `*(_ZTV + 0x10 + 8*N)`, **not** `_ZTV + 8*N`. The two leading qwords are
> offset-to-top and the typeinfo pointer; treating the `_ZTV` symbol address as
> slot 0 overcounts every slot by 0x10 (= two slots). Always cross-check the
> recovered slot-0 target against a real call site. `[HIGH × OBSERVED]`

### 1.2 IDA structures/enums/RTTI JSON — the cross-check `[HIGH × OBSERVED]`

Each binary's IDA v3 run emits `*_structures.json`, `*_enums.json`,
`*_rtti.json`, and `*_xrefs.json` sidecars. For the host runtime these carry
IDA's recovered aggregate types and the `__si_class_type_info` inheritance edges;
the Rosetta-stone offsets are validated against IDA's field table, and the
vtable→method map against `*_xrefs.json`. For the *stripped* ucode shims, the
sidecars contribute **only** the auto-derived ELF structs — they are a negative
control, not a struct source (§2). Inheritance is read from the
`__si_class_type_info` record triple `[vptr, _ZTS-name-ptr, base-_ZTI-ptr]` and
resolved against the `_ZTI` addr→name map.

### 1.3 Shipped-header `static_assert` size/offset pins `[HIGH × OBSERVED]`

The customop-lib ships its public + ISA headers, and the **arch-isa** set carries
its *own* compile-time layout guards — the strongest header channel. Of the **62**
`.h` files under `c10/include/arch-isa/`, **38 contain a `static_assert`**, all
routed through one macro in `arch-isa/common/aws_tonga_isa_common.h`:

```c
// aws_tonga_isa_common.h
#define TONGA_ISA_STATIC_ASSERT(COND, ...)  static_assert((COND), __VA_ARGS__)   // (else: no-op)
#define TONGA_ISA_OFFSET_OF(st, m)          offsetof(st, m)
```

The `arch-isa/tpb/*` headers pin **every TPB instruction struct** to the fixed
ISA instruction width, and pin sub-field offsets/enum widths exactly — these are
byte-exact ground truth that survived compilation:

```c
// aws_tonga_isa_tpb_matmul.h
TONGA_ISA_STATIC_ASSERT(sizeof(TONGA_ISA_TPB_MATMUL_INST) == TONGA_ISA_TPB_INST_NBYTES,
                        "MatMul instruction-size != TONGA_ISA_TPB_INST_NBYTES");
// aws_tonga_isa_sp_common.h        — opcode enum is exactly 1 byte
TONGA_ISA_STATIC_ASSERT(sizeof(TONGA_ISA_SP_OPCODE) == 1, "…should be 1-byte");
// aws_tonga_isa_notification.h     — struct size AND field offset pinned
TONGA_ISA_STATIC_ASSERT(sizeof(TONGA_ISA_NOTIFICATION_ERROR_METADATA) == 3, "unexpected size");
TONGA_ISA_STATIC_ASSERT(TONGA_ISA_OFFSET_OF(TONGA_ISA_NOTIFICATION_GENERIC_ENTRY, header) == 3,
                        "header at an unexpected offset");
```

A struct whose size *and* a field offset are both `static_assert`-pinned is HIGH
even with no symbol. The host C-ABI headers use the parallel `NXLIB_STATIC_ASSERT`
macro (`nxlib_defines.h` → `static_assert` in C++, `_Static_assert` in C); e.g.
`nxlib_window.h` pins `sizeof(nxlib_window_size_t) == sizeof(uint32_t)` (enum is
exactly 4 B) plus a per-arch window-budget invariant. Where a header instead
*documents* a byte layout in prose, the layout is compile-checked the same way —
the cleanest worked example is `nrtucode_dge_mailbox_t` from `nrtucode.h`, whose
doc-comment spells the 4 bytes and whose C definition compiles to exactly that:

```c
typedef struct nrtucode_dge_mailbox {     // sizeof == 4 (compile-confirmed)
    uint8_t  reserved;   // +0  must be 0
    uint8_t  priority;   // +1  [0..255], default 0xFF (higher = lower priority)
    uint16_t bitmask;    // +2  16-bit LE DMA-enable mask, default 0xFFFF
} nrtucode_dge_mailbox_t;
```

(Verify: `gcc -xc - <<<'…' && sizeof==4`.) This struct, the
`nrtucode_extkernel_t` ext-ISA descriptor, and the `nrtucode_*_t` enum family are
consumed by the [DGE host API](../runtime/dge-host-api.md), the
[nrtucode context](../runtime/nrtucode-context.md), and the
[opcode→lib resolver](../runtime/opcode-to-lib-resolver.md). `[HIGH × OBSERVED]`

### 1.4 Native ncore2gp byte-decode — device + ISA-table structs `[MED × OBSERVED]`

For device-side structs and the ISA register/field metadata, the recovery is a
raw image byte-decode rather than a symbol read. `libisa-core.so` is **not
stripped** and exposes its TIE metadata as plain **C arrays** — `ctype_regfile`,
`ctype_regfile_view`, `ctype_num_regs`, `ctype_field_name`, `ctype_field_type`,
`decode_fn`, `decode_fn_slot`, and the per-type `Ctype__TIE_<t>_fieldNames` /
`_fieldTypes` tables (e.g. `…TIE_xtbool16…`, `…TIE_int64…`). Reading these
requires the section delta:

> **GOTCHA — the 0x200000 `.data` delta on ncore2gp DLLs.** For `libisa-core.so`,
> `readelf -SW` gives `.data` Addr `0x764040` / Off `0x564040` and `.data.rel.ro`
> Addr `0x67bb00` / Off `0x47bb00` — a **VMA − fileoffset = 0x200000** delta on
> both writable sections. `.text`/`.rodata` are VMA==fileoffset. Any `xxd`/
> `objdump -s` on a `.data`/`.data.rel.ro`-resident table **must subtract
> 0x200000** from the symbol VMA to hit the right file bytes. This is *not* the
> libtpu `0x400000` nor libnrt's zero delta — confirm per-section with
> `readelf -SW <file>` before decoding. `[HIGH × OBSERVED]`

The device-firmware global structs themselves carry no host-readable symbols;
their interiors are reconstructed by the native Vision-Q7 Xtensa byte-decode and
cross-checked against the per-arch ISA headers
(`arch-headers/{sunda,cayman,mariana,mariana_plus,maverick}/…`,
`arch-isa/common/aws_tonga_isa_common.h`). See
[Device-Firmware Global Structs](struct-device-firmware-globals.md).

---

## 2. The corpus census

Counts re-grounded against each shipped binary. RTTI columns are
`nm <file> | rg -c '_ZTI'` / `'_ZTV'` / `'_ZTS'`; struct/enum columns are the IDA
`*_structures.json`/`*_enums.json` array lengths (`jq length`). `[HIGH × OBSERVED]`

| binary | structs (IDA) | enums (IDA) | RTTI `_ZTI`/`_ZTV`/`_ZTS` | recovery confidence |
|---|---|---|---|---|
| `libnrt.so.2.31.24.0` (host runtime) | 17 372 funcs; ~184 named vtables | (RTTI-class) | **266 / 225 / 245** | **HIGH** — DWARF + RTTI + Rosetta `dump()` |
| `libnrtucode.so` (host C ABI) | **5** (`Elf64_*` only) | **0** | **0 / 0 / 0** (stripped) | **HIGH via headers** — `nrtucode.h`/`nxlib*.h` + `static_assert` |
| `libnrtucode_internal.so` (host C ABI, internal names) | **5** (`Elf64_*` only) | **0** | **0 / 0 / 0** (stripped) | **HIGH via headers** — same headers, `NRTUCODE_INTERNAL_NAMES` on |
| `libisa-core.so` (ncore2gp TIE config) | C-array tables | C-array tables | **0 / 0 / 0** (C, not C++) | **MED** — TIE Ctype arrays, native byte-decode (0x200000 delta) |
| `libisa-core-hw.so` (ncore2gp HW config) | C-array tables | — | **0 / 0 / 0** | **MED** — 204 syms, HW register variant |
| customop-lib headers (`c10/include/**`) | shipped C structs/enums | shipped enums | n/a | **HIGH** — source-of-truth definitions, `static_assert`-pinned |

> **CORRECTION — the "13,578 structs / 522 enums / 770 RTTI for libnrt.so" tally
> does not reproduce.** A backing pass attributed those figures to `libnrt.so`.
> Re-grounding on the binaries present here and on the host-runtime anchor:
> **(a)** no file named `libnrt.so` exists in *this* checkout at all
> (`fd --no-ignore 'libnrt' … | rg '\.so'` returns only `libnrtucode.so` and
> `libnrtucode_internal.so`); the full host runtime lives in a *separate*
> `aws-neuronx-runtime-lib` package and is the `122 956 336`-byte
> `libnrt.so.2.31.24.0` anchored in [The libnrt Surface Map](../runtime/libnrt-surface.md).
> **(b)** Its **RTTI** count is **266 `_ZTI` / 225 `_ZTV` / 245 `_ZTS`**
> (`nm libnrt.so | rg -c`), *not* 770 — the "770" is roughly the *sum*
> `266+225+245 = 736`, inflated, and it is the wrong axis (RTTI ≠ "structs").
> **(c)** The recovered *function* count is **17 372**, not "13,578 structs" —
> "13,578" appears to be a function- or symbol-grep figure mislabeled as a struct
> count. **(d)** The two ucode shims present here recover **5 structs / 0 enums /
> 0 RTTI each** (only the auto-derived `Elf64_Sym/Rela/Dyn/Verneed/Vernaux`), so
> the four-figure totals cannot come from them either. Treat the
> "13,578 / 522 / 770" triple as **unverifiable on this corpus** and use the
> per-binary figures in the table above. `[HIGH × OBSERVED]`

> **QUIRK — the ucode shims are a deliberate negative control.** `libnrtucode.so`
> and `libnrtucode_internal.so` are *stripped* (`nm` → "no symbols";
> `nm -D` shows only the 75 dynamic imports) and contain **zero `_ZTV`/`_ZTI`/
> `_ZTS`**. Their function-pointer dispatch is **plain C arrays**, not C++
> vtables — slot N is `symbol + 8*N`, first entry at `+0x00` (do **not** apply
> the `_ZTV + 0x10` rule here). All their layout truth therefore comes from the
> shipped headers, never from RTTI. The internal shim's `*_strings.json` (66 564
> strings) *does* expose its loader behavior — `"Magic value \`%x\` mismatch"`,
> `"Unknown relocation type %d"`, `"%s added opcode 0x%02x"`,
> `"Failed prelink with status = %d"` — confirming a magic-checked, relocated,
> opcode-parsed image format whose internal structs IDA did **not** recover.
> `[HIGH × OBSERVED]`

> **NOTE — the two header trees split sharply on `static_assert`.** Of the **3685**
> `.h` files under `c10/include/arch-headers/` (the per-arch register/address-map
> headers — cayman/mariana/mariana_plus/maverick/sunda **plus a 6th `tonga`
> dir**), **zero** carry a `static_assert`: they are `#define`/bitfield register
> layouts, not struct size pins, and contribute address maps rather than struct
> sizes. The byte-exact pins all live in the **62** `arch-isa/` headers (38 with a
> `TONGA_ISA_STATIC_ASSERT`, §1.3). The maverick subtree dominates the file count
> (3147 of 3685) but, being register-map `#define`s, adds no v5 *struct* ground
> truth — consistent with the v5/Maverick-interiors-INFERRED wall (§4). `[HIGH × OBSERVED]`

---

## 3. The census ranking — what the per-struct pages cover, in order

The deep-dive pages are ordered by reimplementation criticality, which combines
three observable signals: **(R1) consumed-by-many** — the struct appears as an
argument/field across many GPSIMD-path functions (high `*_xrefs.json` fan-in);
**(R2) byte-exact-confirmed** — size/offsets agree across ≥2 channels (Rosetta +
header `static_assert`, or Rosetta + IDA); **(R3) field-typed-via-Rosetta** — a
`dump()`/`parse()` override fully types the interior. `[MED × INFERRED ranking;
OBSERVED signals]`

| # | struct / class family | lives in | R1 | R2 | R3 | page · primary consumer(s) |
|---|---|---|:--:|:--:|:--:|---|
| 1 | `nrt_ucode_info` / `nrt_ucode_img` (32 B / 16 B) | host `libnrt.so` (DWARF) | ● | ● | — | [host layouts](struct-host-runtime-layouts.md) · [libnrt surface §5.1](../runtime/libnrt-surface.md), [nrtucode bring-up](../runtime/nrtucode-bringup.md) |
| 2 | `kbin` `mem_ref_*` family (8 concrete + list/ptr_table) | host C++ IR | ● | ● | ● | [host layouts](struct-host-runtime-layouts.md) · [NEFF assembly pipeline](../neff/assembly-pipeline.md), [descriptor model](../dma/descriptor-model.md) |
| 3 | `dma_desc_*` family (`data`/`event`/`inc_semaphore`) | host C++ IR | ● | ● | ● | [host layouts](struct-host-runtime-layouts.md) · [DMA descriptor model](../dma/descriptor-model.md), [ring field tables](../dma/descriptor-ring-field-tables.md) |
| 4 | `nrtucode_*` enums + `nrtucode_extkernel_t` / `nrtucode_dge_mailbox_t` | host C ABI | ● | ● | — | [host layouts](struct-host-runtime-layouts.md) · [DGE host API](../runtime/dge-host-api.md), [opcode→lib resolver](../runtime/opcode-to-lib-resolver.md) |
| 5 | `nrtucode_context` / `nrtucode_core` / platform-impl structs | host C ABI | ● | — | — | [exec-state census](struct-exec-state-census.md) · [nrtucode context](../runtime/nrtucode-context.md), [nrtucode core](../runtime/nrtucode-core.md) |
| 6 | `nrtucode_opset` / `nrtucode_loadable_library` (handle state) | host C ABI | ● | — | — | [exec-state census](struct-exec-state-census.md) · [nrtucode opset](../runtime/nrtucode-opset.md), [ll-create](../runtime/nrtucode-ll-create.md) |
| 7 | `enc_*` collectives op + proxy-task classes | host C++ | ◐ | ● | ● | [host layouts](struct-host-runtime-layouts.md) · [CCE in-transfer](../dma/cce-in-transfer.md) |
| 8 | `ntff::*` trace schema (50+ protobuf classes) | host C++ | ◐ | ◐ | ● | [exec-state census](struct-exec-state-census.md) · [NTFF trace parse state](../neff/ntff-trace-parse-state.md) |
| 9 | Vision-Q7 device firmware globals (per-arch) | device | ◐ | — | — | [device globals](struct-device-firmware-globals.md) · [aws_hal_q7](../runtime/aws-hal-q7.md), [HW-decode CAM](../runtime/hw-decode-cam-programming.md) |
| 10 | TIE `Ctype` register/field metadata tables | ncore2gp | ◐ | ◐ | — | [device globals](struct-device-firmware-globals.md) · [ISA encoding appendix](isa-encoding-appendix.md) |

Legend: ● strong · ◐ partial · — n/a. The execute-time/handle structs (#5, #6, #8)
close out the census in [Host Execution-State Structs](struct-exec-state-census.md);
the completeness accounting (which structs remain INFERRED-only) is tallied in
*The Coverage Ledger*.

> **QUIRK — `nrt_ucode_info` ranks #1 despite being trivially small (32 B).** Its
> rank is consumed-by-many, not complexity: it is the single registration struct
> the host stores into the four `pool_eng_*_bin` globals at
> `nrt_set_pool_eng_ucode`, and that the override seam in `tpb_eng_init_hals_v2`
> reads back to swap in a custom Q7 kernel — the one struct on the
> reimplementation critical path (see [libnrt surface §5](../runtime/libnrt-surface.md)).
> `[HIGH × OBSERVED]`

> **NOTE — `nrt_ucode_info` is a host-`libnrt.so` DWARF struct, *not* a ucode-shim
> header type.** It is recovered from the `tdrv_set_pool_eng_ucode` store
> disassembly + DWARF in `libnrt.so` (32 B = two `{void*,size_t}` `nrt_ucode_img`
> pairs). The customop-lib headers (`nrtucode.h`) define **no** `nrt_ucode_info`/
> `nrt_ucode_img` and **no** `custom_op_header`/`custom_op_payload` struct;
> their ext-ISA descriptor is `nrtucode_extkernel_t = {const uint8_t *image;
> size_t image_size; const char *json; size_t json_size;}` and instruction-set
> registration is **opset-based** (a fixed **64-byte** instruction validated by
> `nrtucode_opset_add_instruction`), not descriptor-struct-based. `[HIGH × OBSERVED]`

---

## 4. Confidence & gaps

* **HIGH × OBSERVED:** every per-binary count in §2; the
  `mem_ref_*`/`dma_desc_*`/`enc_*` vtable maps and their slot-0 `dump()` typing;
  the header-pinned enums/structs (`nrtucode_*_t`, `nrtucode_dge_mailbox_t` = 4 B,
  `nxlib_window_size_t` = 4 B); the 0x200000 `.data`/`.data.rel.ro` delta on
  `libisa-core.so`; the stripped-zero-RTTI status of both ucode shims.
* **MED × OBSERVED:** the TIE `Ctype` table interiors (recovered by native
  byte-decode, single channel); the `ntff::*` protobuf schema (generated, large,
  low GPSIMD relevance).
* **INFERRED (flagged inline):** the §3 *ranking ordering* (signals are observed;
  the precedence between them is an editorial criterion); device-firmware global
  *interiors* where only one decode channel exists.
* **WALL — v5/Maverick interiors are INFERRED.** The `NRTUCODE_CORE_MAVERICK_*`
  coretypes exist only behind `#if defined(NRTUCODE_INTERNAL_NAMES)` in
  `nrtucode.h`, and `nxlib_window.h` aliases Maverick's window budget to
  Mariana's (`CHIP == MARIANA || … || MAVERICK` → `NXLIB_MAX_WINDOWS = 40`). No
  byte-grounded Maverick struct interior is observable in this corpus; every v5
  struct claim downstream is **INFERRED** and tagged as such. `[LOW × INFERRED]`
* **Not covered here:** the field byte-tables themselves (in the three sibling
  pages); the completeness/coverage tally (in *The Coverage Ledger*); the device
  Q7 ISA opcode encodings (in [ISA encoding appendix](isa-encoding-appendix.md)).
