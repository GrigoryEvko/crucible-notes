# The Microcode Loader (RELA Relocator + Xtensa Encoder + UCPL)

> *All host-binary addresses on this page apply to `libnrtucode_extisa.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, 9,656,488 B; `.rodata` VMA == file offset, so every `0x9…` host offset is also an analysis VMA). The host-side staging counterpart in `libnrt.so` (build-id `8bb57aba…`, `tdrv/ucode_lib.c`, GCC 14.2.1, DWARF present) is cited as `symbol @0x…`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every reloc-type band, mask, slot signature, and header field is re-derived byte-exact from the objdump/xxd of the cited functions. · Part XI — GPSIMD / Q7 Microcode & ISA · [back to index](../index.md)*

## Abstract

`libnrtucode_extisa.so` does not ship the GPSIMD pool kernels as ready-to-run device images. It ships **13 position-dependent Xtensa ELF32 objects** (`e_machine=0x5e`, see [Vision-Q7 Identification](xtensa-vision-q7.md)) that still carry `.rela.*` relocations, and it carries — *on the host x86-64 side* — a complete **ELF32 microcode loader + Xtensa relocator/encoder** that prelinks a selected blob into device IRAM/DRAM scratch and emits a packed `"UCPL "` device-image header for the firmware to install. Think of it the way a dynamic linker resolves a shared object before `mmap`-ing it into a process: the engine is fixed silicon, the blobs are relocatable code, and this loader is `ld.so` for a foreign (Xtensa) architecture, running entirely on the host.

The loader is a four-function pipeline — **stage → relocate → encode → emit** — driven by `nrtucode_ll_create`. `sub_8150` validates the ELF32 magic/class, extracts the `.dynamic` reloc descriptor, and `memcpy`s the IRAM (R+X) and DRAM (R/W) segments (plus BSS zero-fill) into two `0xA5`-poisoned scratch buffers. `sub_74E0` is the **RELA relocation engine**: it walks the `Elf32_Rela[]` table, maps each `r_offset` into an IRAM or DRAM device window, and classifies the reloc type into five bands — a no-op (0), a byte-granular unaligned 32-bit word splice (5, R_XTENSA_32-style), and two instruction-immediate bands (20–34 low-half, 35–49 high-half) that it hands to `sub_79E0`. `sub_79E0` is the **Xtensa immediate-field encoder**: for a narrow 24-bit instruction it clears mask `0xFF0000F7` and ORs the 16-bit immediate in at bit 8; for a FLIX/VLIW bundle (8 or 16 bytes) it dispatches on one of **eight slot-format signatures** and splices the immediate into a TIE slot bitfield. `sub_8380` writes the `0x20`-byte `"UCPL "` header and returns it to the firmware installer.

This page documents what a reimplementer must reproduce byte-for-byte: (1) the **reloc descriptor** and its dynamic-tag jump table; (2) the **RELA type taxonomy** and the IRAM/DRAM window-select math; (3) the **type-5 byte-granular splice** and its two mask tables; (4) the **narrow-24b + FLIX immediate encoder** with all eight slot signatures and five bundle-clear masks; and (5) the **`"UCPL "` header + geometry descriptor** that frames the staged image. It also flags a confirmed **success-on-error bug** in the `libnrt.so` staging counterpart (`ucode_alloc_new_staged_libs`).

For reimplementation, the contract is:

- **The reloc descriptor** (`0x40` B) extracted by `sub_6750` from `.dynamic` via the `d_tag−4` jump table — and the corrected field offsets: RELA table ptr `@+0x20`, reloc count `@+0x28`.
- **The RELA type taxonomy** — `0` no-op, `5` word splice, `20–34` low-immediate, `35–49` high-immediate (`value >> 16`), `>0xFF` err 5, else "Unknown relocation type" err 8 — and the `0x400`-biased window-select that routes each `r_offset` to IRAM or DRAM.
- **The type-5 splice** — `value & 3` selects a shift and a `{mask_lo, mask_hi}` pair from `dword_920D70` / `dword_920D7C`; the relocated word straddles two device words at a 4-byte-unaligned offset.
- **The Xtensa encoder** — `(word & 8)==0` ⇒ narrow `(word & 0xFF0000F7) | ((imm&0xffff)<<8)`; else a FLIX 8/16-byte bundle dispatched on eight slot signatures (`0x41000000`…`0x42000000`), each ANDing a per-slot clear mask from `xmmword_91FF60..A0` before OR-ing the shuffled immediate.
- **The `"UCPL "` header** — magic `0x204C504355`, `align4`/`align32` code+data sizes, start-symbol qword; `library_size = align32(code)+align4(data)`, capped at `0x10000` on device.

| | |
|---|---|
| **Orchestrator / UCPL writer** | `sub_8380` @`0x8380` (269 B) = `nrtucode_ll_prelink` |
| **Stager (validate + extract + copy)** | `sub_8150` @`0x8150` (549 B) |
| **RELA relocation engine** | `sub_74E0` @`0x74E0` (1269 B) |
| **Xtensa immediate encoder** | `sub_79E0` @`0x79E0` (1746 B) |
| **Reloc-descriptor extractor** | `sub_6750` @`0x6750` (789 B), jump table @`0x91FF30` |
| **Endian normalizer** | `sub_7470` @`0x7470` (15 B, ~40 callers in `sub_6750`); flag `dword_9360A0` |
| **Geometry descriptor** | `@0x920DC8` (8×u64: IRAM/DRAM bases, sizes, scratch) |
| **`"UCPL "` magic** | `0x204C504355` (`movabs` @`0x843e`); header `0x20` B |
| **Type-5 mask tables** | `dword_920D70` = `ff000000 ffff0000 ffffff00`; `dword_920D7C` = `00ffffff 0000ffff 000000ff` |
| **Narrow-24b clear mask** | `0xFF0000F7` (`and` @`0x7a4b`); imm at bits `[23:8]` |
| **Device per-library cap** | `0x10000` (64 KiB) |

---

## 1. The Pipeline — stage → relocate → encode → emit

### Purpose

The loader exists because the embedded blobs are **relocatable**, not absolute: their `.rela.*` sections still reference symbols and code/data addresses by section-link, and their FLIX large-immediate fields are left blank for the loader to splice. A device cannot run such an image. The pipeline resolves every relocation against the *device's own* IRAM/DRAM target addresses (from the geometry descriptor), encodes the immediates into the Xtensa instruction words in place, and packages the result behind a `"UCPL "` header that tells the firmware where the code and data segments land. The path is the foreign-architecture analogue of `ld.so`'s relocate-then-map step.

### Entry Point

```text
nrtucode_ll_create  (exported; selects blob by arch_id/lib_idx/flavor → sub_8660)
  └─ sub_8380 (0x8380)                         ── ORCHESTRATOR / UCPL writer
       ├─ memset(iram_dest, 0xA5, geom+0x18)   ── poison IRAM scratch (32 KiB)
       ├─ memset(dram_dest, 0xA5, geom+0x38)   ── poison DRAM scratch ( 8 KiB)
       ├─ sub_8150 (0x8150)                     ── STAGE
       │    ├─ sub_6A70                          ── parse + validate ELF32 (magic/class)
       │    ├─ sub_66D0 → sub_7470/sub_7490      ── section-header walk (endian-normalized)
       │    ├─ sub_6750 (0x6750)                 ── extract .dynamic → reloc descriptor
       │    ├─ memcpy(iram_seg) + memset(BSS)    ── stage R+X segment
       │    └─ memcpy(dram_seg) + memset(BSS)    ── stage R/W segment
       ├─ sub_74E0 (0x74E0)                      ── RELOCATE (per Elf32_Rela)
       │    └─ sub_79E0 (0x79E0)                 ── ENCODE Xtensa immediate fields
       └─ write 0x20-byte "UCPL " header @arg7   ── EMIT
```

The error formatter `sub_80C0` (@`0x80C0`, `(*(ctx+48))(*(ctx+40), fmt, …)`) is called on every failure path of `sub_74E0`/`sub_79E0`/`sub_8150`.

### Algorithm

```c
// sub_8380 — nrtucode_ll_prelink(core, blob_body, blob_size, geom_desc, …, hdr_dst)
function ll_prelink(geom, blob, blob_size, hdr_dst):
    // 5-slot staging context built at rsp+0x08 (see §5b)
    staging[0] = geom                                   // geometry/region descriptor @0x920dc8
    staging[1] = iram_dest                              // host mirror of device IRAM split region
    staging[3] = dram_dest                              // host mirror of device DRAM split region
    memset(iram_dest, 0xA5, geom[+0x18])                // poison IRAM scratch (32 KiB)
    memset(dram_dest, 0xA5, geom[+0x38])                // poison DRAM scratch ( 8 KiB)

    rc = stage_image(staging, &reloc_desc, blob)        // sub_8150 → fills staging[+0x10],[+0x20]
    if rc != 0: return rc                               // validate / extract / bounds errors

    rc = relocate(staging, &reloc_desc)                 // sub_74E0
    if rc != 0: return rc

    // EMIT the 0x20-byte "UCPL " device-image header (see §5a)
    code = staging[+0x10]                                // iram_filled (dword, set by sub_8150)
    data = staging[+0x20]                                // dram_filled (dword)
    hdr_dst[0x00] = 0x000000204C504355                  // "UCPL " magic | header size 0x20 @+0x04
    hdr_dst[0x08] = align4(code)                         // add $3; and $0xfffffffc
    hdr_dst[0x0c] = align32(code)                        // add $0x3f; and $0xffffffe0
    hdr_dst[0x10] = align4(data)
    hdr_dst[0x14] = reloc_desc.start_symbol_qword        // out-desc slot @rsp+0x58
    hdr_dst[0x1c] = reloc_desc.entry_extra_dword         // out-desc slot @rsp+0x50
    return 0
```

> **NOTE —** `sub_8380` poisons both scratch buffers with `0xA5` *before* staging, so any byte the copy/BSS step does not touch is a recognisable `0xA5` pattern rather than stale data. `library_size` (computed by the caller, `nrtucode_ll_create`) is `align32(code)+align4(data)`; if it would exceed `0x10000` the device cannot hold the library and the create fails with status `7 ENOSPC`. CONFIDENCE: **HIGH** (header field map re-derived from the `sub_8380` disasm; `movabs $0x204c504355` @`0x843e`, `and $0xfffffffc`/`$0xffffffe0` @`0x8452`/`0x845b`).

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `sub_8380` @`0x8380` | 269 | Orchestrator; poison, stage, relocate, write UCPL header | HIGH |
| `sub_8150` @`0x8150` | 549 | Stage: validate + extract + memcpy IRAM/DRAM + BSS | HIGH |
| `sub_74E0` @`0x74E0` | 1269 | RELA relocation engine (§3) | HIGH |
| `sub_79E0` @`0x79E0` | 1746 | Xtensa immediate-field encoder (§4) | HIGH |
| `sub_6750` @`0x6750` | 789 | `.dynamic` → reloc-descriptor extractor (§2) | HIGH |
| `sub_6A70` @`0x6A70` | 293 | ELF32 program/section validator | HIGH |
| `sub_66D0` @`0x66D0` | 124 | Section-header walker (`e_shoff@+0x20`, `e_shnum@+0x30`) | HIGH |
| `sub_73B0` @`0x73B0` | 67 | ELF magic/class validator; sets endian flag `dword_9360A0` | HIGH |
| `sub_7470` @`0x7470` | 15 | Endian-read primitive (`bswap`+`cmove` on the flag) | HIGH |
| `sub_80C0` @`0x80C0` | 144 | Error-log formatter (varargs, ctx log vtable) | HIGH |

### Considerations

The stager bounds-checks the staged IRAM segment against `geom[+0x18]` and the DRAM segment against `geom[+0x38]`; on overflow it logs `"Segment exceeds the size of the split region on device"` and returns `13`. This is the only place a blob too large for the 32 KiB IRAM / 8 KiB DRAM scratch is rejected; a reimplementer must size the scratch buffers to the geometry descriptor, not to the blob.

---

## 2. The Reloc Descriptor (`sub_6750`)

### Purpose

Before any relocation can run, the loader must find the `Elf32_Rela[]` table and its entry count inside the blob's `.dynamic` section, plus the symbol/string tables and the init/fini addresses. `sub_6750` is that extractor: it scans the section headers for `SHT_DYNAMIC` (section `type==2`), then walks the `d_tag/d_val` pairs through a 10-case jump table indexed by `d_tag−4`, filling a `0x40`-byte out-descriptor that every later stage reads.

### Algorithm

```c
// sub_6750 — extract_reloc_descriptor(blob, …, out /*0x40-byte desc*/)
function extract_reloc_descriptor(blob, out):
    e_shoff = sub_7470(blob[+0x20])                      // endian-normalized e_shoff
    e_shnum = sub_7490(blob[+0x30])                      // 16-bit e_shnum
    find shdr with sh_type == 2 (SHT_DYNAMIC)            // section-type scan
    for each Elf32_Dyn (d_tag, d_val) in .dynamic:       // 8-byte entries
        idx = d_tag - 4
        if d_tag == 0x70000002:                          // DT_LOPROC+2 (Neuron/Xtensa proc tag)
            out[+0x14] = d_val + base                     // handler @0x6984
            continue
        if idx < 0 or idx > 9: continue                  // out of jump-table range
        switch via JT @0x91FF30 [idx]:                   // (d_tag-4) → s32 displacement
          DT_HASH   (4):  out[+0x2c] = sub_7470(d_val) + DRAM_base   // @0x6967
          DT_STRTAB (5):  out[+0x34] = d_val + DRAM_base             // @0x69e2
          DT_SYMTAB (6):  out[+0x30] = d_val + DRAM_base             // @0x699d
          DT_RELA   (7):  out[+0x20] = RELA_table_base               // @0x69b3 ← see CORRECTION
          DT_RELASZ (8):  out[+0x28] = (RELASZ * 0xAAAAAAAB) >> 35   // = RELASZ/12 = #Elf32_Rela  @0x69fb
          9/10/11      :  /* RELAENT/STRSZ/SYMENT — no write */      // @0x6940 (×3)
          DT_INIT   (12): out[+0x18] = d_val + base                  // @0x6a1a
          DT_FINI   (13): out[+0x1c] = d_val + base                  // @0x6921
    out[+0x38] = max_section_alignment
    out[+0x3c] = window_select_flag                       // *(a7+8): nonzero ⇒ +0x400 bias (§3)
```

The jump table at `0x91FF30` is 10 signed-32 displacements relative to its own base; the verbatim on-disk bytes are `37 6a 6e ff | b2 6a 6e ff | 6d 6a 6e ff | 83 6a 6e ff | cb 6a 6e ff | 10 6a 6e ff | 10 6a 6e ff | 10 6a 6e ff | ea 6a 6e ff | f1 69 6e ff`, resolving to handler addresses `0x6967 / 0x69e2 / 0x699d / 0x69b3 / 0x69fb / 0x6940 / 0x6940 / 0x6940 / 0x6a1a / 0x6921`. The three identical `…6a10` (`0x6940`) entries are the no-write `DT_RELAENT/STRSZ/SYMENT` cases. The `RELASZ/12` divide is the textbook `imul 0xAAAAAAAB; shr 35` reciprocal for `÷12 = ÷sizeof(Elf32_Rela)`.

> **CORRECTION (UC-NX) —** an earlier scaffold (`P1-UC-INTERNAL §3b`) placed the **RELA table pointer at descriptor `+0x32`** and the **reloc count at `+0x40`**. Both are wrong. The binary truth is **RELA table ptr `@+0x20`** and **reloc count `@+0x28`**. This is double-proven in `sub_74E0`'s prologue: the loop bound is `cmpl $0x0,0x28(%rsi)` (@`0x74e0`) and `movslq 0x28(%r14),%rax` (@`0x7534`); the table base is `mov 0x20(%rsi),%rbp` (@`0x74fe`). The seed's `+0x32`/`+0x40` would index past the count field into the init-alignment slot. CONFIDENCE: **HIGH** (offsets read from disasm).

### Considerations

Every field read goes through `sub_7470` (the endian normalizer), which `bswap`es the value iff the global flag `dword_9360A0` is set. That flag is set by `sub_73B0` from `EI_DATA` (byte 5 of the ELF header): `1=LE→0`, `2=BE→1`. The 13 shipped blobs are all little-endian (`EI_DATA==1`), so on a little-endian host the normalizer is a pass-through — but a reimplementer must keep the byte-swap path, because the loader is written to relocate a big-endian Xtensa image too.

---

## 3. The RELA Relocation Engine (`sub_74E0`)

### Purpose

`sub_74E0` is the core: it iterates the `Elf32_Rela[]` table extracted in §2, resolves each entry's target into a device IRAM or DRAM window, and applies the relocation. It is the foreign-architecture relocator — its taxonomy mirrors the Xtensa `R_XTENSA_*` reloc families, with type 5 being the unaligned 32-bit word reloc (R_XTENSA_32) and the two instruction-immediate bands being the low/high halves of a split immediate.

### Entry Point

```text
sub_8380 (orchestrator)
  └─ sub_74E0 (0x74E0)            ── per Elf32_Rela: classify + window-select + apply
       └─ sub_79E0 (0x79E0)      ── (types 20-34 / 35-49) instruction-immediate splice
```

### The reloc-type taxonomy

`r_info` here is the bare relocation **type** (the symbol index is resolved separately via the reloc descriptor's base deltas). The engine classifies it into five bands:

| Type band | Class | Action | Anchor |
|---|---|---|---|
| `0` | `R_XTENSA_NONE` | no-op, `continue` | @`0x759f` |
| `5` | `R_XTENSA_32` (word) | byte-granular unaligned 32-bit word splice (§3c) | @`0x75a5` |
| `20–34` (`0x14–0x22`) | instruction immediate, **low half** | → `sub_79E0(addr, value & 0xffff)` | @`0x7583` |
| `35–49` (`0x23–0x31`) | instruction immediate, **high half** | → `sub_79E0(addr, value >> 16)` | @`0x758f`, shift @`0x767b` |
| `> 0xFF` | malformed | `return 5` | @`0x754e` |
| else | unknown | log `"Unknown relocation type %d"`; `return 8` | @`0x79bd` |

> **NOTE —** the split into a `20–34` low-immediate band and a `35–49` high-immediate band is the structural signature of an `l32r`-style high/low immediate **pair**: the same resolved value feeds two relocations, the low band taking `value & 0xffff` and the high band taking `value >> 16` (the `shr $0x10` at `0x767b`). The exact numeric `R_XTENSA_*` IDs behind each band are not bound to the Xtensa ABI here (needs the Vision-IVP32 `bfd/elf32-xtensa` reloc table — see [Identification](xtensa-vision-q7.md)); the *behaviour* is fully decoded. CONFIDENCE: **HIGH** (bands), **MED** (named ABI constants).

### Algorithm — the per-Rela loop and window-select

```c
// sub_74E0 — relocate(staging, reloc_desc)
function relocate(staging, desc):
    if desc[+0x28] == 0: return 0                        // reloc_count; nothing to do (@0x74e0)
    rela = desc[+0x20] + 8                               // &Rela[0].r_addend; reads at rela-8/-4/+0
    for i in 0 .. desc[+0x28]-1:                          // loop bound = reloc_count (@0x7534)
        r_info   = rela[-4]                               // bare reloc TYPE
        if r_info > 0xFF: return 5                        // malformed (@0x754e)
        r_offset = rela[-8]
        r_addend = rela[ 0]

        // --- window-select: route r_offset to IRAM or DRAM ---
        iram_limit = desc[+0x0c]                          // IRAM size/limit
        flag       = desc[+0x3c]                          // window-select flag (from §2)
        bias       = (flag != 0) ? 0x400 : 0              // 1024-byte window bias
        eff        = bias + r_offset
        if eff < iram_limit:
            base_delta = desc[+0x00] - desc[+0x04]        // IRAM base delta
        else:
            base_delta = desc[+0x08] - iram_limit         // DRAM base delta
        addr_in_image = base_delta + r_offset             // host-mirror address of the word

        switch (r_info):
          case 0: continue                                // R_XTENSA_NONE
          case 20..34:                                     // low-immediate instruction reloc
              value = base_delta + r_addend                // resolved value (window-relative)
              return_if(sub_79E0(staging, addr_in_image, value & 0xffff))
          case 35..49:                                     // high-immediate instruction reloc
              value = base_delta + r_addend
              return_if(sub_79E0(staging, addr_in_image, (value >> 16) & 0xffff))   // @0x767b
          case 5: splice_word(staging, addr_in_image, base_delta + r_addend)        // §3c
          default: log("Unknown relocation type %d", r_info); return 8
        rela += 0xC                                        // sizeof(Elf32_Rela)
    return 0
```

> **QUIRK —** the `0x400` (1024-byte) bias is added **only to the window-select comparison**, not to the final address. A reloc whose `r_offset` falls in the low 1 KiB of the image can be forced into the DRAM window by setting the `+0x3c` flag, even though its actual store still lands at `base_delta + r_offset`. This is how the loader handles a blob whose IRAM and DRAM split-regions are adjacent: the bias disambiguates offsets that would otherwise alias. CONFIDENCE: **HIGH** (`(flag!=0)<<0xA` at `0x7558`–`0x756x`).

### Algorithm — the type-5 byte-granular word splice

Type 5 is `R_XTENSA_32`: a 32-bit word reloc that, unlike a normal ELF `R_*_32`, may target a **4-byte-unaligned** offset. When the target is unaligned the relocated value straddles two adjacent device words, and the engine splices it in with two mask tables.

```c
// sub_74E0 type-5 path (0x75a5..0x7984)
function splice_word(staging, addr, value):
    rem = value & 3                                       // r15d = low 2 bits of the VALUE
    if rem == 0:                                          // aligned fast path (@0x775b)
        store32(staging, addr, value)                     // window-checked simple store
        return
    // --- unaligned: split across two adjacent words ---
    shift   = rem * 8                                     // shl $0x3, r15d
    mask_lo = dword_920D70[rem - 1]                       // -4-base indexing (table starts at 0x920d70)
    mask_hi = dword_920D7C[rem - 1]                       // overlaps dword_920D70 at +0xC
    w0      = load32(staging, floor4(addr))               // window-checked
    w1      = load32(staging, floor4(addr) + 4)
    w0      = (value << shift)        | (w0 & mask_lo)     // patch low word, retain its high byte(s)
    w1      = (value >> (32 - shift)) | (w1 & mask_hi)     // patch high word, retain its low byte(s)
    store32(staging, floor4(addr),     w0)                // window-checked
    store32(staging, floor4(addr) + 4, w1)
```

The two mask tables are contiguous and overlapping (verbatim `xxd` @`0x920D70`, 16 bytes: `ff00 0000 ffff 0000 ffff ff00 00ff ffff`):

| `value & 3` | shift | `mask_lo` (`dword_920D70`) | `mask_hi` (`dword_920D7C`) | effect |
|---|---|---|---|---|
| 1 | 8 | `0xFF000000` | `0x00FFFFFF` | low word keeps its top byte; value's bytes 0–2 fill bytes 1–3; value byte 3 → next word byte 0 |
| 2 | 16 | `0xFFFF0000` | `0x0000FFFF` | low word keeps its top half; value's low half → bytes 2–3; value's high half → next word bytes 0–1 |
| 3 | 24 | `0xFFFFFF00` | `0x000000FF` | low word keeps top 3 bytes; value byte 0 → byte 3; value bytes 1–3 → next word bytes 0–2 |

Each load and store is window-checked against the same IRAM/DRAM geometry; an out-of-range load logs `"Invalid load @ 0x%x"` (`@0x920062`) and an out-of-range store logs `"Invalid store (0x%x) @ 0x%x"` (`@0x920046`), both via `sub_80C0`.

> **GOTCHA —** the mask tables share storage: `dword_920D7C` is `dword_920D70 + 0xC`, so the 4-entry `mask_lo` table (`ff000000 ffff0000 ffffff00 00ffffff`) and the 4-entry `mask_hi` table (`00ffffff 0000ffff 000000ff 00000000`) overlap at the `00ffffff` word. A reimplementer who declares two independent 4-word arrays will allocate 8 words; the binary uses 7. The indexing is `−4`-based (`rem` from 1..3, not 0..3) precisely because `rem==0` takes the aligned fast path and never indexes the table. CONFIDENCE: **HIGH** (`xxd` verbatim).

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `sub_74E0` @`0x74E0` | 1269 | RELA loop, window-select, type-5 splice | HIGH |
| `sub_79E0` @`0x79E0` | 1746 | Instruction-immediate encoder (called for 20–34 / 35–49) | HIGH |
| `sub_80C0` @`0x80C0` | 144 | OOB load/store + "Unknown relocation type" formatter | HIGH |

### Considerations

The engine re-derives the IRAM-vs-DRAM window on **every** load and store from `staging[0] → geom desc` (`+0x10`/`+0x18` IRAM target/size, `+0x30`/`+0x38` DRAM target/size) — it does not cache a window per reloc. This is deliberate: a single instruction-immediate reloc can read its instruction word from IRAM and resolve a value pointing into DRAM, so the read window and the value window are independent. A reimplementation that picks one window per reloc entry will mis-route the split high/low pairs.

---

## 4. The Xtensa Immediate-Field Encoder (`sub_79E0`)

### Purpose

`sub_79E0` patches a 16-bit immediate into an Xtensa instruction at a relocated address. Xtensa has two encodings the loader must handle: the **narrow 24-bit** instruction (a single 3-byte instruction with a contiguous immediate field) and the **FLIX/VLIW bundle** (an 8- or 16-byte bundle of independent slots, where the immediate lives in a TIE-defined bitfield of one slot). The narrow case is a single mask-and-OR; the FLIX case requires identifying which slot format the bundle uses and scattering the immediate bits per that format's TIE layout.

### Algorithm — address resolve + narrow 24-bit form

```c
// sub_79E0 — encode_immediate(staging, addr, imm16)
function encode_immediate(staging, addr, imm16):
    // resolve addr into the IRAM (geom+0x10/+0x18) or DRAM (geom+0x30/+0x38) window
    word_ptr = window_resolve(staging, addr)             // identical math to sub_74E0
    if word_ptr == OOB:
        log("Invalid relocation address %u", addr); return 8     // @0x91ffcb
    word = *word_ptr

    // --- NARROW 24-bit form ---  (@0x7a41)
    if (word & 8) == 0:
        *word_ptr = (word & 0xFF0000F7) | ((imm16 & 0xffff) << 8) // imm at bits [23:8]
        return 0
    // --- else: FLIX/VLIW wide bundle ---  (§4 cont.)
```

The narrow path is the textbook Xtensa 24-bit immediate insertion: clear the imm16 field with mask `0xFF0000F7` (verbatim `and $0xff0000f7,%eax` @`0x7a4b`, preceded by `shl $0x8,%ecx` @`0x7a48`), then OR the immediate in at bit 8. The mask keeps the top byte (`0xFF000000`) and the low 3 control bits (`0xF7` = `0b11110111`, i.e. everything but bit 3 — the very bit `(word & 8)` tested to select this path).

### Algorithm — FLIX/VLIW bundle dispatch

```c
// sub_79E0 FLIX path (0x7a7c..0x8081)
    // bundle length from the instruction word
    if (word & 0x100000F) == 0xF  or  (word & 0xF) == 0xE:  len = 16   // 16-byte bundle
    elif (word & 0x300000F) == 0x100000F:                  len = 8     //  8-byte bundle
    else: log("Unknown instruction format [0x%x]", word); return 12    // @0x91ffe9

    memcpy(stack_bundle, word_ptr, len)                  // copy bundle to scratch
    slot = (len == 16) ? stack_bundle[+0xC] : stack_bundle[+0x4]       // slot word to patch

    // dispatch on slot-format SIGNATURE (mask & compare); each splices imm differently:
    handler = match_slot_signature(slot)                 // one of 8 (table below)
    if handler == NONE:
        log("Failed to set large immediate field in instruction slot %i"); return 8   // @0x92000b

    slot &= handler.bundle_clear_mask                    // AND per-slot xmmword clear mask
    slot |= scatter(imm16, handler.shuffle)              // OR shuffled immediate bits
    write slot back into stack_bundle
    memcpy(word_ptr, stack_bundle, len)                  // publish patched bundle  (@0x8081)
    return 0
```

### The eight slot-format signatures

Each FLIX slot is identified by a `(mask, compare)` signature test on the slot word, dispatched into a handler that scatters the immediate per that slot's TIE field layout and ANDs a per-slot bundle-clear mask. All eight are captured verbatim; the *named* TIE operands need the Vision-IVP32 `.tie` config (see [Identification](xtensa-vision-q7.md)).

| Bundle | Signature `(mask)==(compare)` | Handler | Clear mask | Confidence |
|---|---|---|---|---|
| 16 B | `(slot & 0x410C0000) == 0x41000000` | @`0x7b13` | `xmmword_91FFA0` | HIGH |
| 16 B | `(slot & 0xF) == 0xE` (low nibble) | @`0x7bc4` | `xmmword_91FF60` | HIGH |
| 16 B | `(slot & 0x410E0000) == 0x41040000` | @`0x7d09` | `xmmword_91FF90` | HIGH |
| 16 B | `(slot & 0x410E8000) == 0x41080000` | @`0x7d88` | `xmmword_91FF80` | HIGH |
| 16 B | `slot == 0x41088000` | @`0x7e8a` | `xmmword_91FF70` | HIGH |
| 16 B | `(slot & 0x410E0000) == 0x41060000` | @`0x7eee` | (in-word masks) | HIGH |
| 8 B | `(slot & 0x42000000) == 0x42000000` | @`0x7fe5` | (in-word masks) | HIGH |
| 8 B | `(slot & 0x42000000) == 0x40000000` | @`0x7fa0`/`0x8045` | (in-word masks) | HIGH |

The five per-slot bundle-clear masks live at `xmmword_91FF60..A0`; only the **low 8 bytes** are used (loaded via `movq`, then `pand`), and the high 8 bytes are zero. Verbatim on-disk bytes (`xxd`):

```text
xmmword_91FF60 = ff ff ff df  ff bf f9 3f   (slot @0x7bc4)
xmmword_91FF70 = ff ff ff 9f  7f 7f e8 ff   (slot @0x7e8a)
xmmword_91FF80 = ff ff ff 9f  ff 7f e0 ff   (slot @0x7d88)
xmmword_91FF90 = f7 ff ff df  ff 7f f0 ff   (slot @0x7d09)
xmmword_91FFA0 = ff ff ff df  ff ff a0 ff   (slot @0x7b13)
```

Each mask clears the destination TIE immediate bitfield(s) before the handler ORs in the shuffled immediate halves. The shuffle constants are exact in the disasm — e.g. handler `@0x7b13` builds the slot with `imm<<8`, `(imm<<0x12) & 0x4000000`, `(imm<<0x13) & 0x20000000` — i.e. the 16 immediate bits are *scattered*, not contiguous, across the slot per the TIE field layout.

> **QUIRK —** the FLIX bundle is patched out-of-place: the loader `memcpy`s the whole 8- or 16-byte bundle to a stack VLA, splices the slot there, and `memcpy`s it back. It never writes the slot word directly into device-mirror memory. This matters because the slot patch reads-modifies-writes the slot word (`slot &= clear; slot |= scatter`), and the bundle-clear masks are 8-byte `pand`s — a piecewise field write into the live bundle would corrupt neighbouring slots whose bits share the cleared mask bytes. CONFIDENCE: **HIGH** (every signature, mask, and `memcpy` captured verbatim; named TIE operands **MED**).

### Function Map

| Function | Size | Role | Confidence |
|---|---|---|---|
| `sub_79E0` @`0x79E0` | 1746 | Narrow-24b + FLIX bundle immediate splice | HIGH |
| `sub_80C0` @`0x80C0` | 144 | Encoder error formatter (3 strings) | HIGH |

### Considerations

The bundle-length selectors are themselves Xtensa FLIX format-length nibbles, not loader inventions: `(word & 0x100000F)==0xF` and `(word & 0xF)==0xE` mark 16-byte formats; `(word & 0x300000F)==0x100000F` marks an 8-byte format. A reimplementer decoding an unknown blob must use these exact selectors — an instruction whose format byte matches none of them is rejected with err 12, which is the correct failure for a blob built against a different TIE config than the one this loader expects.

---

## 5. The `"UCPL "` Header and Geometry Descriptor

### Purpose

The loader's one observable output (besides the relocated scratch buffers) is a `0x20`-byte device-image header the firmware reads to install the library. It frames the staged image as `{code segment, data segment, start symbol}` with the alignment the device expects, behind a `"UCPL "` magic. The geometry descriptor is the static table that supplies the device IRAM/DRAM target addresses and scratch sizes the whole pipeline relocates against.

### (5a) The `"UCPL "` device-image header

Written by `sub_8380` (`@0x843e..0x847b`); destination is the orchestrator's `arg7`. `0x20` (32) bytes:

| Offset | Type | Field | Value / source |
|---|---|---|---|
| `+0x00` | u64 | `magic` | `0x000000204C504355` (bytes `55 43 50 4C 20` = `"UCPL "`); the `+0x04` dword reads back `0x00000020` = header size = code dst_offset (magic/offset overlap) |
| `+0x08` | u32 | `code_size_a4` | `align4(staging[+0x10])` = `align4(iram_filled)` |
| `+0x0c` | u32 | `code_size_a32` | `align32(iram_filled)` = data-segment dst_offset |
| `+0x10` | u32 | `data_size_a4` | `align4(staging[+0x20])` = `align4(dram_filled)` |
| `+0x14` | u64 | `start_symbol` | device pointer (out-desc slot `@rsp+0x58`) |
| `+0x1c` | u32 | `entry_extra` | dword (out-desc slot `@rsp+0x50`) |

`library_size` (computed by `nrtucode_ll_create`) = `align32(code) + align4(data)`; the on-device per-library cap is `0x10000` (64 KiB). The `align4`/`align32` are the verbatim `add $3; and $0xfffffffc` / `add $0x3f; and $0xffffffe0` sequences. CONFIDENCE: **HIGH** (matches the [overview](overview.md) 64-byte-record framing field-for-field, re-derived here from the orchestrator disasm).

### (5b) The staging context (5 slots)

Built by `sub_8380` at `rsp+0x08`; passed as the first arg to `sub_8150` and `sub_74E0`:

```text
[+0x00] desc_ptr     = geometry/region descriptor (@0x920dc8)
[+0x08] iram_dest    = host mirror of device IRAM split region (memset 0xA5, size geom+0x18)
[+0x10] iram_filled  = code bytes copied incl. BSS span (DWORD, set by sub_8150)
[+0x18] dram_dest    = host mirror of device DRAM split region (memset 0xA5, size geom+0x38)
[+0x20] dram_filled  = data bytes copied (DWORD, set by sub_8150)
```

> **NOTE —** the filled-size slots `[+0x10]`/`[+0x20]` are read by `sub_8380` as **dwords** when packing the UCPL `align4`/`align32` fields — they are not 64-bit sizes. A reimplementer that stores them as `u64` and reads `align4(u64)` will pick up the adjacent pointer's low bits. CONFIDENCE: **HIGH** (the UCPL writer reads `staging+0x10` / `staging+0x20` as 32-bit).

### (5c) The geometry / split-region descriptor (`@0x920DC8`)

Eight `u64`s, verbatim `xxd`, supplying every device base/size the pipeline uses:

| Offset | Value | Meaning |
|---|---|---|
| `+0x00` | `0x00000000` | IRAM device base low / reserved |
| `+0x08` | `0x00020000` | IRAM region size (128 KiB) |
| `+0x10` | `0x00018000` | IRAM device **target** addr (reloc base; the `sub_74E0`/`sub_79E0` window) |
| `+0x18` | `0x00008000` | IRAM prelink scratch size (32 KiB) ← `malloc`'d + `0xA5` poison |
| `+0x20` | `0x00080000` | DRAM region size (512 KiB) |
| `+0x28` | `0x00040000` | DRAM split / reserved (256 KiB) |
| `+0x30` | `0x000BE000` | DRAM device **target** addr (778240) |
| `+0x38` | `0x00002000` | DRAM prelink scratch size (8 KiB) ← `malloc`'d + `0xA5` poison |

`sub_8150` bounds-checks the staged IRAM segment against `+0x18` and the DRAM segment against `+0x38` (→ ret 13). The reloc engine uses `+0x10`/`+0x30` as the relocation bases and `+0x18`/`+0x38` (with the `+0x08`/`+0x20` region sizes) as window limits. CONFIDENCE: **HIGH** (matches the [overview](overview.md) geometry verbatim).

---

## 6. The `ucode_lib.c` Staging Counterpart and Its Bug

### Purpose

`libnrtucode_extisa.so` *produces* the relocated image and UCPL header; `libnrt.so`'s `tdrv/ucode_lib.c` is the runtime *consumer* that allocates device DRAM, DMAs the library blobs and an EXTRAM image in, and wires the on-device dispatch table (the full staging path is the [`ucode.c` facade](ucode-facade.md)). One function on that path, `ucode_alloc_new_staged_libs`, has a confirmed error-handling defect that a reimplementer must not copy.

### The success-on-error bug

> **GOTCHA —** `ucode_alloc_new_staged_libs` (`@0x310660` in `libnrt.so`, `tdrv/ucode_lib.c:224`) **returns `NRT_SUCCESS` (0) on every post-`calloc` allocation failure**, and on that path it never writes `*ulib_set_info`. The control flow is byte-confirmed:
>
> - The struct `calloc` failure path is **correct**: `je 0x31089e` → `mov $0x4,%eax` (NRT_RESOURCE) → `jmp 0x3107f6` → `ret` with status 4.
> - But every later allocation failure — `lib_dmem` (`jne 0x310810` @`0x3106e8`), `ucode_table` (@`0x310868`), `ucode_get_q7_extram` (@`0x31075f`), EXTRAM `dmem_alloc_aligned` (`jne 0x310808` @`0x3107ca`), and `dmem_buf_copyin` (@`0x3107e9`) — funnels into the cleanup block at `0x310810`: `nlog_write` + `dmem_free`×3 + `free(struct)`, then **`jmp 0x3107f4`** (@`0x310863`).
> - `0x3107f4` is `xor %eax,%eax; ...; ret` → **returns 0 (SUCCESS)**. Critically, the jump lands at `0x3107f4`, *past* the `mov %rbx,0x0(%r13)` at `0x3107f0` that writes `*ulib_set_info = struct` — so on this path the out-param is **left unwritten while the freshly-freed struct pointer (`%rbx`) is dangling**.
>
> Net effect: a transient device-DRAM exhaustion during ucode staging returns SUCCESS to the caller with `*ulib_set_info` either NULL/stale (and pointing at freed memory in the cached extisa-only branch). The caller (`ucode_stage_libs`) treats SUCCESS as "`ulib_set_info` populated" and proceeds to `ucode_stage_libs_impl`, which dereferences it → use-after-free / NULL-deref / silent corruption instead of a clean `NRT_RESOURCE`. The `num_libs != 1` branch has an extra `==SUCCESS ? impl : NRT_RESOURCE` guard that *partially* masks it; the locked `num_libs == 1` branch checks `==SUCCESS` then dereferences `tpb->sunda.ulib_set_info_extisa_only` (just freed). The intended code was almost certainly `return NRT_RESOURCE;` on the cleanup path. **VERIFIED — the bug is real.** CONFIDENCE: **HIGH** (DWARF-attributed function; `xor %eax,%eax; ret` at `0x3107f4` reached from the `0x310863` cleanup `jmp`, confirmed by disasm).

### Considerations

This is a host-side runtime defect, not a wire-format issue — the relocated image and UCPL header produced by `libnrtucode_extisa.so` are unaffected. It is recorded here because the loader pipeline's correctness depends on its *consumer* propagating allocation failures; a reimplementer building the `ucode_lib.c` side must return a non-zero status (and leave `*ulib_set_info` NULL) on every cleanup path. See the [Known-Bugs Catalog](../appendix/known-bugs.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `nrtucode_ll_create` (exported) | Selects the blob (`sub_8660`) and drives `sub_8380`; computes `library_size` and the `0x10000` cap |
| `sub_6A70` / `sub_66D0` (validators) | Parse + validate the ELF32 header and section table before relocation |
| `sub_7470` / `sub_73B0` (endian) | The byte-order normalizer and the `EI_DATA`-driven flag every field read goes through |
| `ucode_stage_libs` (`libnrt.so`) | The runtime consumer that allocates device DRAM and installs the staged library |

## Cross-References

- [The 13 Q7 Microcode Blobs](q7-blobs.md) — the ELF32 images this loader relocates; the `.rela.*` inputs live inside them
- [The ucode.c dlopen Facade](ucode-facade.md) — `libnrt`'s consumer side, where the staged image and UCPL header are installed on device
- [Overview: the Q7 GPSIMD Engine](overview.md) — the provider→loader→device flow, the geometry descriptor, the 64-byte load/unload record
- [The IVP Vector ISA Catalog (1065 Ops)](ivp-isa-catalog.md) — the FLIX format/length scheme the encoder's bundle selectors decode
- [Tensilica Xtensa and Vision-Q7 Identification](xtensa-vision-q7.md) — the ISA proof; the named `R_XTENSA_*` reloc IDs and TIE slot operands left MED here
- [Known-Bugs and Anomalies Catalog](../appendix/known-bugs.md) — the `ucode_alloc_new_staged_libs` success-on-error defect
- [back to index](../index.md)
