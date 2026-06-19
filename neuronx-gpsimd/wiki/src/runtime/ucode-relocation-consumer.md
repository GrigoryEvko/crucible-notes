# The UCODE Relocation / Prelink Engine (runtime consumer)

> **Binary:** `libnrtucode_internal.so` (host x86-64 ELF, *not* stripped,
> BuildID `9cbf78c6f59c…`) from the `aws-neuronx-gpsimd-customop-lib` package —
> **not** `libnrt.so`. Every symbol, offset, struct byte, reloc type and
> instruction-format mask on this page was re-verified against that binary and
> against an **embedded Xtensa PI device blob** at file offset `0x2ef7e0`.
> Section deltas for this binary (`readelf -SW`): `.rodata` VMA==fileoffset
> (Δ=0), `.text` Δ=0x1000, `.data.rel.ro` Δ=0x2000, `.data` Δ=0x3000. All
> `.rodata` structs below (`*_memory_bounds`, the keep-masks) are at
> VMA==fileoffset and were `xxd`'d directly.

This is the **host-side relocator** for the GPSIMD Q7 ext-ISA loadable kernel.
The kernel that runs on the 8 Cadence Vision-Q7 ("Cairo") pool cores is shipped
as a 32-bit Xtensa **position-independent ELF**; this engine walks its
`Elf32_Rela` table, rewrites Xtensa instruction immediates and 32-bit data words
so the code/data run from their final on-device `{IRAM,DRAM}` addresses, and
then stages the two relocated buffers to the device. It is a *true* dynamic
relocator (relocation is correctness-critical; a failed reloc aborts the install)
— the **device loader sees no residual relocations** (see §10).

> **NOTE — two unrelated "relocators".** Do not confuse this with the NEFF
> `kbin_patch` INSERT/MODIFY/DELETE slot-patch table in `libnrt.so` (a warn-only
> trace aid that maps a device sequencer PC back to a NEFF-IP). That is a
> different binary, a different package, a different table format, and *warn-only*
> failure semantics. This engine is the GPSIMD customop-lib side and shares
> nothing with it but the word "relocation".

Cross-links: the host segment-loader / R\_XTENSA processor and UCPL prelink header
([`runtime/prelinker-ucpl.md`](./prelinker-ucpl.md), the SX-RT prelink chain); the
device-side external-library loader that consumes the staged image
([`../firmware/pool/external-lib-loader.md`](../firmware/pool/external-lib-loader.md)).
The NEFF↔ELF container relationship is a forward reference to the not-yet-authored
`neff/neff-elf-relationship.md`.

**Confidence tags:** `HIGH/MED/LOW` × `OBSERVED` (read from the binary / embedded
blob) / `INFERRED` (reasoned across control-data flow) / `CARRIED` (taken from a
backing report, re-verified here).

---

## 1. One-paragraph model `[HIGH·OBSERVED]`

`nrtucode_ll_create @0x9b1a90` (the loadable-library constructor) picks the
per-arch + per-flavor Q7-POOL ext-ISA PI image, allocates two **host** scratch
buffers (an IRAM-region image and a DRAM-region image), and runs `prelink
@0x9b5d60`. `prelink` (1) poison-fills both scratch buffers with `0xA5`, (2)
`prelink_load_lib @0x9b5e70` validates the ELF, copies the **code** segment into
the IRAM buffer and the **data** segment into the DRAM buffer (zero-padding each
to its `p_memsz`), and parses the dynamic-relocation descriptor via `get_dyn_info
@0x9b6ed0`, (3) `prelink_relocate_lib @0x9b6160` walks the `Elf32_Rela` table,
applying each relocation in place — patching Xtensa instruction-slot immediates
(`R_XTENSA_SLOT*_OP` / `_ALT`, types 20.. / 35..) through `relocate_op @0x9b6660`
and 32-bit data words (`R_XTENSA_32`, type 5) directly — and (4) emits a 32-byte
`"UCPL "` header describing the staged `{code,data}` sizes. The two **fully
relocated** buffers are then bulk-written to the device through the nrtucode
context's platform vtable. This is the device analog of a dynamic loader
relocating `.text`/`.data` into a process image, except the relocation happens on
the host and only the finished bytes cross to the device.

The reloc/prelink subsystem is the six recovered `.c.o` translation units whose
names survive in the string pool: `prelink.c`, `prelink_load.c`,
`prelink_relocate.c`, `prelink_memory_bounds.c`, `prelink_log.c`, and the
Tensilica `pi_library_load.c` (`xtlib_*`) loader.

---

## 2. The input — the Xtensa PI library (`validate_dynamic_load @0x9b71f0`) `[HIGH·OBSERVED]`

### 2.1 Magic / endianness gate (`xtlib_verify_magic @0x9b6d40`)

```c
// xtlib_verify_magic(elf): returns 0 on accept, -1 (0xFFFFFFFF) on reject.
i64 xtlib_verify_magic(const u8 *e) {
    if (!(e[0]==0x7F && e[1]=='E' && e[2]=='L' && e[3]=='F' && e[4]==1))  // \x7fELF + ELFCLASS32
        return -1;
    switch (e[5]) {                 // EI_DATA
        case 1: dword_9BB56C = 0; break;   // LSB  -> byte-swap OFF
        case 2: dword_9BB56C = 1; break;   // MSB  -> byte-swap ON
        default: return -1;
    }
    return 0;
}
```

`dword_9BB56C @0x9bb56c` (a `.bss` neighbor of `xtlib_globals @0x9bb568`) is the
**process-global byte-swap flag**. Every multi-byte ELF field is thereafter read
through `xtlib_host_word @0x9b6e00` (`_byteswap_ulong` when the flag is set) and
`xtlib_host_half @0x9b6e20` (`__ROL2__(x,8)` when set), so the loader is
endian-agnostic: a big-endian Q7 image on a little-endian host is fully
supported. `[OBSERVED]` `[LOW]` that any shipped image is actually big-endian —
all embedded blobs observed are `EI_DATA==1`.

### 2.2 Program-header shape contract

`validate_dynamic_load(out96, elf)` zeroes a 96-byte stack scratch, runs the
magic gate, then walks the `Elf32_Phdr` table (`e_phoff @elf+0x1C`, 32-byte
phdrs, `e_phnum @elf+0x2C`) expecting this **exact** segment shape `[OBSERVED]`:

| phdr | `p_type` | `p_flags` test | role |
|------|----------|----------------|------|
| LOAD #0 | 1 | `(flags & 7) == 5` | code, R+X → **IRAM** |
| LOAD #1 | 1 | `(flags & 6) == 2` | data, R+W → **DRAM** (optional) |
| DYNAMIC | 2 | `(flags & 7) == 6` | `.dynamic` |

When the optional data LOAD is present the validator records a **3-phdr "split"
marker** `*(out+8) = 0x200000001` (`v6` advances 2→3; the matched phdr pointers
are stored at `out+0x10` / `out+0x20` / `out+8*v6+0x10`). Any deviation stores
status **7**.

```c
// 0x9B71F0 — the shape that must hold:
//   *(u32*)out          = (verify_magic != 0);            // 0 means OK
//   LOAD0: host_word(*(elf+e_phoff))==1 && (host_word(phdr0.p_flags)&7)==5
//   LOAD1: host_word(phdr1.p_type)==1   && (host_word(phdr1.p_flags)&6)==2  -> *(out+8)=0x200000001
//   DYN  : host_word(phdr.p_type)==2    && (host_word(phdr.p_flags)&7)==6
//   on mismatch: *(u32*)out = 7;
```

> **GROUND TRUTH — embedded blob `@0x2ef7e0`.** Decoding it directly:
> `e_machine=94` (XTENSA), `ET_EXEC`, `e_entry=0x1005610`, 4 phdrs:
> `PHDR[0] LOAD vaddr=0x1000000 filesz=memsz=0x6f1e flags=5 align=0x80` (code),
> `PHDR[1] LOAD vaddr=0x2000000 filesz=0x450 memsz=0x48c flags=7` (data),
> `PHDR[2] LOAD vaddr=0x3000000 flags=3`,
> `PHDR[3] DYNAMIC vaddr=0x3000000 filesz=0xa4 flags=6`. The data seg's
> `memsz>filesz` (0x48c vs 0x450) is the `.bss` tail that `prelink_load_lib`
> zero-fills. This matches the contract exactly. `[OBSERVED]`

### 2.3 Sizing helpers (no relocation, but the loader substrate) `[HIGH·OBSERVED]`

- `align_ptr(x,a) @0x9b6e40` = `(-a) & (x+a-1)` — round `x` up to power-of-two `a`.
- `find_align @0x9b6e50` — the max `p_align` over LOAD sections with non-zero
  `p_memsz` (the embedded blob → `0x80`; this becomes the staging alignment).
- `xtlib_pi_library_size @0x9b7320` / `xtlib_split_pi_library_size @0x9b7450` /
  `xtlib_pi_scratch_size @0x9b75a0` — compute the staged code/data region sizes
  from `max(p_vaddr+p_memsz)` so a caller can size device buffers. On a bad
  magic they set `xtlib_globals=1` and return `-1`.

---

## 3. The dyn_info descriptor — `get_dyn_info @0x9b6ed0` `[HIGH·OBSERVED]`

`get_dyn_info` is the **producer** of the 64-byte per-load relocation descriptor
("dyn_info", the `a7` out-param) that `reloc_addr` (§4) and `prelink_relocate_lib`
(§5) consume. It locates the `PT_DYNAMIC` segment (`while host_word(p_type)!=2`,
status **4** if absent), seeds the four segment bases from its arguments, computes
the staging alignment, and walks the `Elf32_Dyn` `{d_tag, d_un}` 8-byte array
rebasing each pointer tag.

Byte-exact layout, decoded from the writes in `0x9b6ed0`:

| off | field | producer (`get_dyn_info` write) | consumer |
|-----|-------|---------------------------------|----------|
| `+0x00` | `seg0_link_base` | `host_word(a3)` (aligned code load VA) | `reloc_addr` `di[0]` |
| `+0x04` | `seg0_stage_base` | `host_word(a4)` (IRAM region base) | `reloc_addr` `di[1]` |
| `+0x08` | `seg1_link_base` | `host_word(a5)` (aligned data load VA) | `reloc_addr` `di[2]` |
| `+0x0c` | `seg_boundary` | `host_word(a6)` (DRAM base / link↔load split) | `reloc_addr` `di[3]`; `*(di+12)` |
| `+0x10` | `seg0_end_va` | `host_word(e_entry@+0x18 + v45)` | the IRAM/DRAM split compare |
| `+0x14` | `dt_xt_pi` | `0`, then `case 0x70000002` rebased (`+v45`) | — |
| `+0x18` | `dt_init_va` | `case 12` (`DT_INIT + v45`) | — (INIT) |
| `+0x1c` | `dt_fini_va` | `case 13` (`DT_FINI + v45`) | — (FINI) |
| `+0x20` | `rela_table*` | `case 7` (`DT_RELA` rebased) | `*(di+0x20)` (§5) |
| `+0x28` | `rela_count` | `case 8` (`host_word(DT_RELASZ / 0xC)`) | `*(di+0x28)` (§5) |
| `+0x2c` | `dt_hash` | `case 4` (`DT_HASH + v31`) | — |
| `+0x30` | `dt_symtab` | `case 6` (`DT_SYMTAB + v31`) | — |
| `+0x34` | `dt_strtab` | `case 5` (`DT_STRTAB + v31`) | — |
| `+0x38` | `max_p_align` | the staging alignment (`a7+56`) | §2 (callers) |
| `+0x3c` | `split_flag` | `*(validate_struct+8)` (3-phdr split) | `reloc_addr` `di[15]`; `*(di+0x3c)` (§5) |

> **CORRECTION.** The backing inventory (DX-IDA-03 §7) placed `dt_init` at
> dyn_info `+0x24`; the writes in `0x9b6ed0` put **`case 12` (DT_INIT) at
> `a7+24` (`+0x18`)** and **`case 13` (DT_FINI) at `a7+28` (`+0x1c`)**, while the
> `rela_table` (`case 7`) lands at `a7+32` (`+0x20`) and `rela_count`
> (`case 8`) at `a7+40` (`+0x28`). The exact `+0x18..+0x28` grouping is the one
> verified here; the INIT/FINI words are *populated* but not read by the reloc
> walk, so their precise role is `[MED·INFERRED]`.

```c
// 0x9B6ED0 — the d_tag dispatch (DT values OBSERVED in the embedded blob's .dynamic):
//   v31 = a5 - (a6 + (a6 & (align-1)));   // data-segment rebase delta
//   v45 = a3 - (a4 + (a4 & (align-1)));   // code-segment rebase delta
switch (host_word(dyn->d_tag)) {
  case 4:  a7[44] = host_word(host_word(dyn->d_val) + v31);  break;  // DT_HASH
  case 5:  a7[52] = host_word(host_word(dyn->d_val) + v31);  break;  // DT_STRTAB
  case 6:  a7[48] = host_word(host_word(dyn->d_val) + v31);  break;  // DT_SYMTAB
  case 7:  // DT_RELA — split-flag picks shdr-relative vs simple rebase:
           if (split_flag) {
               shdr = *(u64*)(vstruct + 8*vstruct.shidx + 16);
               *(u64*)(a7+32) = *(u32*)(shdr+4) + elf + dyn->d_val - *(u32*)(shdr+8);
           } else {
               *(u64*)(a7+32) = host_word(host_word(dyn->d_val) + v31);
           }
           break;
  case 8:  a7[40] = host_word(host_word(dyn->d_val) / 0x0C);  break; // DT_RELASZ -> entry count
  case 9: case 10: case 11: break;                                   // RELAENT/STRSZ/SYMENT ignored
  case 12: a7[24] = host_word(host_word(dyn->d_val) + v45);  break;  // DT_INIT
  case 13: a7[28] = host_word(host_word(dyn->d_val) + v45);  break;  // DT_FINI
  default: if (dyn->d_tag == 1879048194)  // 0x70000002 = DT_LOPROC+2, the Xtensa-PI reloc ext
               a7[20] = host_word(host_word(dyn->d_val) + v45);
           break;
}
```

> **GOTCHA — `DT_RELASZ` is divided, not stored.** `case 8` writes
> `host_word(DT_RELASZ / 0x0C)`, i.e. it pre-divides the byte size by the
> 12-byte `Elf32_Rela` stride to yield an **entry count**. `prelink_relocate_lib`
> then loops `count` times. Each relocation is therefore a 12-byte
> `Elf32_Rela {u32 r_offset; u32 r_info; u32 r_addend}` and `DT_RELAENT` must
> be `0x0C`.

> **GROUND TRUTH — the blob's `.dynamic`.** Decoding `PHDR[3]` (`p_offset=0x7500`
> in-blob): `DT_INIT=0x1000000`, `DT_FINI=0x1006f10`, `DT_HASH=0x30000a4`,
> `DT_STRTAB=0x30000c4`, `DT_SYMTAB=0x30000b4`, `DT_SYMENT=0x10`,
> `DT_RELA=0x30000c8`, `DT_RELASZ=0xB40`, `DT_RELAENT=0xC`. `0xB40 / 0xC = 240`
> relocations. `[OBSERVED]`

---

## 4. `reloc_addr @0x9b6130` — the PI address-remap primitive `[HIGH·OBSERVED]`

`reloc_addr(dyn_info *di, int link_va)` maps a **link-time PI virtual address** to
its final **staged address**, choosing the code-segment vs data-segment delta by
the boundary. Decompiled byte-exact (`di` indexed as `_DWORD`):

```c
// 0x9B6130
u32 reloc_addr(const u32 *di, int link_va) {
    u32 boundary = di[3];                                   // +0x0c seg_boundary
    int delta;
    if (link_va + ((di[15] != 0) << 10) >= boundary)        // di[15] = +0x3c split_flag
        delta = di[2] - boundary;                           // data/DRAM seg: seg1_link_base - boundary
    else
        delta = di[0] - di[1];                              // code/IRAM seg: seg0_link_base - seg0_stage_base
    return link_va + delta;
}
```

So an address **at or above** the boundary is rebased by the DATA-segment delta;
below it, by the CODE-segment delta. The `+ (split ? 1024 : 0)` nudge accounts for
the extra guard page that exists only when a separate data LOAD segment is
present. This is the "device-IP → staged-IP" primitive for the PI library — the
GPSIMD analog of the NEFF-side `get_neff_ip`, but **forward / load-time /
address-exact** rather than reverse / trace-time / slot-index.

---

## 5. `prelink_relocate_lib @0x9b6160` — the relocation walk `[HIGH·OBSERVED]`

```c
// 0x9B6160 — outer loop over rela_count entries.
i64 prelink_relocate_lib(void **handle, dyn_info *di) {
    if (di->rela_count <= 0) return 0;                      // *(int*)(di+40)
    u32 *e = (u32*)(di->rela_table + 8);                    // entry+2 (skip to r_addend)
    for (i64 k = 0; ; ++k, e += 3 /* +0x0C */) {
        u32 r_off  = e[-2];   // r_offset : link-time patch SITE
        u32 type   = e[-1];   // r_info   : low byte = ELF reloc type
        u32 addend = e[ 0];   // r_addend : target-value seed (link VA)
        if (type > 0xFF) return 5;                          // corrupt table guard
        ...
    }
}
```

> **GOTCHA — `r_info` is dispatched whole, then `& 0xFF` is the type.** The guard
> `if (type > 0xFF) return 5` rejects any entry whose **full** `r_info` dword
> exceeds a byte, but the branch arithmetic below works on `type - 20` / `type -
> 35` using the full dword, and the final `if ((u8)type)` test reads only the low
> byte. The high 24 bits of `r_info` are the symbol index / Xtensa negate-offset
> flags `[MED·INFERRED]`; they are not exercised by this walk.

### 5.1 The relocation TYPE set — ground-truthed `[HIGH·OBSERVED]`

The branch logic computes `v15 = type - 20`; if `v15 ∈ [0,14]` it is a
`SLOT{0..14}_OP` immediate (the **low** half); else `v15 = type - 35`; if
`∈ [0,14]` it is a `SLOT{0..14}_ALT` immediate (the **high** half, value `>>16`).
`type == 0` falls through to the `if ((u8)type) break;` test → `continue`
(skipped padding). `type == 5` is the special data-word path. Anything else →
`"Unknown relocation type %d"` and **status 8**.

| type | name | count in blob | apply path |
|------|------|---------------|------------|
| `0` | `R_XTENSA_NONE` | 8 | skipped (table padding) |
| `5` | `R_XTENSA_32` | 30 | §5.3 data/word split-store |
| `20`(`0x14`)..`34` | `R_XTENSA_SLOT{0..14}_OP` | 101 | §5.2 instr-immediate, **low** half |
| `35`(`0x23`)..`49` | `R_XTENSA_SLOT{0..14}_ALT` | 101 | §5.2 instr-immediate, **high** half (`>>16`) |

> **GROUND TRUTH — the blob's 240 relocations.** Histogram of `r_info & 0xFF`
> over all 240 `Elf32_Rela` entries: **`{35: 101, 20: 101, 0: 8, 5: 30}`** —
> exactly the four kinds above. The entries appear in `{35-high, 20-low}` pairs,
> each pair followed by **two** `NONE(0)` entries (the slack from the two-word
> `L32R`/`MOVI`/`CONST16` high/low split). First 6 entries (off, type, addend):
> `(0x100001b, 35, 0x200025c)`, `(0x100001e, 20, 0x200025c)`, `(0, 0, 0)`,
> `(0, 0, 0)`, `(0x1000043, 35, 0x20001f8)`, `(0x1000048, 20, 0x20001f8)`. The
> addends `0x200025c` / `0x20001f8` are **data-segment** link VAs (base
> `0x2000000`) that `reloc_addr` rebases to DRAM. `[OBSERVED]` — this is the page's
> strongest claim and it is byte-exact against the device image.

> **NOTE — "ALT" is the high-half variant.** The two SLOT classes are the
> classic Xtensa two-instruction split that materializes a 32-bit constant: the
> `_OP` reloc writes the low 16 bits, the `_ALT` reloc writes `value >> 16`. The
> per-FLIX-slot index (0..14) is `type - 20` / `type - 35` and is passed to
> `relocate_op` as `slot_index`.

### 5.2 The instruction-immediate path (types 20/35) → `relocate_op`

```c
// inside the loop, for a SLOT*_OP / _ALT:
int seg_delta = (... segment-delta chosen exactly like reloc_addr ...);
u32 target    = addend + (segment-delta on addend);         // resolve target via §4 deltas
u32 site      = r_off + seg_delta;                          // where to patch (staged VA)
u32 value     = (type < 35) ? target : (target >> 16);      // low vs high half
i64 rc = relocate_op(handle, site, /*slot*/ (type<35?type-20:type-35), value);
if (rc) return rc;                                          // FATAL — abort install
```

### 5.3 The data-word path (type 5 = `R_XTENSA_32`) `[HIGH·OBSERVED]`

A 4-byte data pointer is read, rebased through `reloc_addr`, and written back.
Because Xtensa PI data words can be **unaligned**, the code splits the access into
aligned vs straddling cases:

```c
// type == 5:
u32 site = r_off + seg_delta;
u32 mis  = site & 3;
if (mis == 0) {                                  // aligned fast path
    u32 w = load32(site);                        // bounds-checked (§7); on miss -> 0xFFFFFFFF + log
    w     = reloc_addr(di, addend + w);
    store32(site, w);                            // bounds-checked store
} else {                                         // unaligned: 2 loads / 2 stores
    u32 lo = load32(site & ~3);
    u32 hi = load32((site & ~3) + 4);
    u32 v  = (u32)(((u64)hi << 32 | lo) >> (8 * mis));        // extract straddling word
    v      = reloc_addr(di, addend + v);                      // rebase
    u32 lo2 = (load_lo & dword_9AAF70[mis-1]) | (v << (8*mis));   // keep bytes outside the field
    u32 hi2 = (load_hi & dword_9AAF7C[mis-1]) | (v >> (32-8*mis));
    store32(site & ~3, lo2);
    store32((site & ~3)+4, hi2);
}
```

> **The merge keep-masks `@0x9aaf70` / `@0x9aaf7c` (`.rodata`, `xxd`'d directly):**
> ```
> dword_9AAF70 = { 0x000000FF, 0x0000FFFF, 0x00FFFFFF }   // low-word keep masks  (misalign 1/2/3)
> dword_9AAF7C = { 0xFFFFFF00, 0xFFFF0000, 0xFF000000 }   // high-word keep masks (misalign 1/2/3)
> ```
> For `mis ∈ {1,2,3}` these preserve exactly the bytes **outside** the 32-bit
> relocated field while the shifted rebased value is OR'd in. The decompiler's
> `__PAIR64__(hi,lo) >> 24 / >> 16 / >> 8` confirms the `8*mis` extract shift.

> **GOTCHA — bad data loads are non-fatal.** Every load/store is gated against the
> §7 windows. An out-of-range **load** logs `"Invalid load @ 0x%x"` and
> substitutes `0xFFFFFFFF` but the walk **continues**; an out-of-range **store**
> logs `"Invalid store (0x%x) @ 0x%x"` and continues. Only the
> *instruction-slot* gate in `relocate_op` (§6.1) and the table guards return a
> fatal status.

---

## 6. `relocate_op @0x9b6660` — the Xtensa instruction-slot immediate writer `[HIGH·OBSERVED]`

`relocate_op(handle, site, slot_index, value)` patches the immediate operand of
one Xtensa instruction in place. It is the most intricate function in the
subsystem: Xtensa has variable-width instructions (16/24-bit) **and** FLIX wide
bundles (the NX Vision-Q7 packs multiple ops per fetch), so the immediate bits are
scattered across the instruction word differently per format.

### 6.1 Locate + bounds-check (the same gate as §7) `[OBSERVED]`

```c
// 0x9B6660
i64 relocate_op(void **h, u32 site, int slot, u32 value) {
    bounds *b = (bounds*)h[0];
    u8 *host_buf; u32 win_base;
    // window A: DRAM-reserved [b+0x30 .. +0x30+b+0x38) -> host_buf = h[3] (DRAM), win_base=b+0x30
    // window B: IRAM-reserved [b+0x10 .. +0x10+b+0x18) -> host_buf = h[1] (IRAM), win_base=b+0x10
    if (site not in window A and site not in window B || host_buf == NULL) {
        log_error(h, "Invalid relocation address %u", site);
        return 8;                                            // FATAL
    }
    u32 *word = (u32*)(site + host_buf - win_base);          // host pointer to the instruction word
    ...
}
```

### 6.2 Classify the instruction format `[OBSERVED]`

```c
    u32 w = *word;
    if ((w & 8) == 0) {                                      // 24-bit NARROW immediate: FAST path
        *word = ((u16)value << 8) | (w & 0xFF0000F7);        // splice imm into bits 8..23
        return 0;
    }
    // else a WIDE / FLIX bundle. Determine byte length:
    size_t n = 16;
    if ((w & 0x100000F) != 0xF && (w & 0xF) != 0xE) {
        n = 8;
        if ((w & 0x300000F) != 0x100000F) {
            log_error(h, "Unknown instruction format [0x%x]", w);
            return 12;                                        // FATAL
        }
    }
    char scratch[16];
    memcpy(scratch, word, n);                                // pull bundle into stack scratch
    ... splice immediate into the FLIX slot ...
    memcpy(word, scratch, n);                                // write the bundle back
    return 0;
```

### 6.3 The per-format immediate splice (the SSE keep-mask switch) `[OBSERVED bits / MED·INFERRED layout]`

The function dispatches on sub-format bits of the bundle's second/third word
(masks `0x41000000` / `0x41040000` / `0x41060000` / `0x41080000` / `0x41088000` /
`0x410C0000` / `0x40000000`) and writes `value` bit-by-bit into the slot's
immediate field, clearing the destination bits first with **five 128-bit keep-masks
carved from `.rodata`** (`xxd`'d directly; only the low 8 bytes of each are
non-trivial):

```
xmmword_9AAF20 = ff ff ff df ff ff a0 ff   (00 ...)     // used by (sub & 0x410C0000)==0x41000000
xmmword_9AAF30 = ff ff ff df ff bf f9 3f   (00 ...)     // used by ((w & 0xF)==0xE) narrow-wide form
xmmword_9AAF40 = ff ff ff 9f ff 7f e0 ff   (00 ...)     // used by (sub & 0x410E8000)==0x41080000
xmmword_9AAF50 = f7 ff ff df ff 7f f0 ff   (00 ...)     // used by (sub & 0x410E0000)==0x41040000
xmmword_9AAF60 = ff ff ff 9f 7f 7f e8 ff   (00 ...)     // used by (sub & 0x410E8000)==0x41088000
```

Each branch does `dst64 = _mm_or_si128(scattered(value),
_mm_and_si128(load(dst64), MASK))` — the `_mm_and_si128` with the keep-mask
clears the slot's immediate bits, and the OR of the scattered `(value << N) &
literal` terms writes the new immediate. The `0x100000F` / `0x300000F` arms also
handle the **large immediate** (L32R-class) where the constant spans two slot
words; a format it can't encode logs `"Failed to set large immediate field in
instruction slot %i"` and returns 8.

> **NOTE.** `[HIGH]` that these masks are the Xtensa per-slot immediate encoder
> and that they correspond to the eight observed sub-format branches; `[MED·INFERRED]`
> on the **exact** bit map of each scattered term — the shift/mask constants are
> directly observed, but each maps to a specific ncore2gp TIE slot immediate
> layout not re-derived field-by-field here. This is the *only* place the
> relocator writes CODE; types 20/35 funnel here, type 5 writes DATA directly in
> `prelink_relocate_lib` (§5.3).

---

## 7. `memory_bounds` — the per-arch device-region geometry `[HIGH·OBSERVED]`

The address gate in `relocate_op` (§6.1) and the load/store checks in
`prelink_relocate_lib` (§5.3) both dereference a per-arch `memory_bounds` const
struct in `.rodata`. **There are only TWO such structs in the binary** — `nm`
confirms exactly `sunda_memory_bounds @0x9aaea0` and `cayman_memory_bounds
@0x9aaee0` (`nm | rg -c memory_bounds` → **2**). 64-byte struct (8 qwords),
decoded directly (`.rodata` VMA==fileoffset):

| off | field | sunda | cayman | meaning |
|-----|-------|-------|--------|---------|
| `+0x00` | `iram_link_base` | `0` | `0` | code-segment device base VA |
| `+0x08` | `iram_size` | `0x10000` | `0x20000` | total IRAM aperture |
| `+0x10` | `iram_resvd_off` | `0x5500` | `0x18000` | IRAM relocatable window START |
| `+0x18` | `iram_resvd_size` | `0xab00` | `0x8000` | IRAM relocatable window SIZE |
| `+0x20` | `dram_link_base` | `0x80000` | `0x80000` | data-segment device base VA |
| `+0x28` | `dram_size` | `0x10000` | `0x40000` | total DRAM aperture |
| `+0x30` | `dram_resvd_off` | `0x8e000` | `0xbe000` | DRAM relocatable window START |
| `+0x38` | `dram_resvd_size` | `0x2000` | `0x2000` | DRAM relocatable window SIZE |

### 7.1 The two-window gate `[OBSERVED]`

A patch SITE is valid iff it falls in window **A** `[b+0x30 .. b+0x30+b+0x38)`
(DRAM-reserved, uses the DRAM host buffer `handle[3]`) **or** window **B**
`[b+0x10 .. b+0x10+b+0x18)` (IRAM-reserved, uses the IRAM host buffer
`handle[1]`). The host patch pointer = `site + host_buf - window_base`. Anything
else → `"Invalid relocation address %u"`. So relocations are only ever applied
inside the per-arch **reserved (loadable-library) sub-regions** of IRAM/DRAM — the
rest of the aperture is the resident base ucode and is never touched.

### 7.2 Mariana / Mariana+ / Maverick reuse cayman's bounds

> **CLAIM (byte-grounded for the path; INFERRED for v5/Maverick interior).** The
> **single** `prelink` call site in the binary — at `nrtucode_ll_create`,
> decompiled line 2506 — passes `&cayman_memory_bounds` *unconditionally*:
> ```c
> v26 = prelink(lib_elf, &cayman_memory_bounds, ctx,
>               prelink_error_log_callback, iram_buf, dram_buf, &ucpl_hdr);
> ```
> That call services **all four** Q7-POOL core kinds (the bittest mask
> `0x2020202000` over kinds `{13,21,29,37}` = Sunda/Cayman/Mariana/Mariana+ —
> see §10). Since there is no `mariana_memory_bounds` / `maverick_memory_bounds`
> symbol (`nm` confirms NONE), and the call hardcodes cayman's struct,
> **Mariana / Mariana+ / Maverick reuse cayman's `memory_bounds`**. `[OBSERVED]`
> that the prelink path uses cayman bounds and that only two bounds structs
> exist; `[INFERRED]` `[MED]` that this is the *intended* geometry for the v5
> Maverick interior specifically — Maverick ships its own `MAVERICK_Q7_POOL_*`
> kernel-load getters, but its device-region geometry is **not byte-grounded** to
> a dedicated bounds struct, so the cayman-reuse for Maverick is flagged INFERRED.
> `sunda_memory_bounds` exists but the resident Sunda core (kind 6) registers
> **without** prelink (§10), so `sunda_memory_bounds` is referenced by the
> sizing/`xtlib_*` reference paths rather than this install path. `[OBSERVED]`

---

## 8. `prelink @0x9b5d60` + `prelink_load_lib @0x9b5e70` — the orchestrator `[HIGH·OBSERVED]`

```c
// 0x9B5D60  prelink(elf, bounds, ctx, log_cb, iram_buf, dram_buf, &out_hdr)
i64 prelink(...) {
    void *handle[5] = { bounds, iram_buf, 0/*iram_used*/, dram_buf, 0/*dram_used*/ };
    memset(iram_buf, 0xA5, bounds->iram_size);    // poison-fill: catches un-relocated holes
    memset(dram_buf, 0xA5, bounds->dram_size);
    dyn_info di = {0};
    i64 rc = prelink_load_lib(handle, elf, bounds->iram_link_base, bounds->dram_link_base, &di);  // §8.1
    if (rc) return rc;
    rc = prelink_relocate_lib(handle, &di);       // §5 — apply ALL relocations in place
    if (rc) return rc;
    // emit the 32-byte UCPL output header:
    *(u64*)(out+0x00) = 0x204C504355;             // "UCPL " magic (libnrtUCode PreLink)
    u32 code_sz = (handle[2 /*iram_used*/] + 3) & ~3u;
    *(u32*)(out+0x08) = code_sz;
    *(u32*)(out+0x0c) = (code_sz + 63) & ~31u;     // 32B-rounded aligned code size
    *(u32*)(out+0x10) = (handle[4 /*dram_used*/] + 3) & ~3u;   // data_sz
    *(u64*)(out+0x14) = di.data_load_va;           // staged DRAM base (from dyn_info)
    *(u32*)(out+0x1c) = di.data_link_off;
    return 0;
}
```

> **The UCPL header (0x20 bytes).** `0x204C504355` little-endian decodes to the
> ASCII bytes **`"UCPL "`** (`b'UCPL \x00\x00\x00'`) — verified. This descriptor is
> what the device-install step (§10) consumes to size and place the two prelinked
> blobs. Layout: `+0x00 u64 magic`, `+0x08 u32 code_sz` (4B-rounded), `+0x0c u32
> code_sz_aligned` (32B-rounded), `+0x10 u32 data_sz`, `+0x14 u64 data_load_va`,
> `+0x1c u32 data_link_off`.

### 8.1 `prelink_load_lib @0x9b5e70` — the segment copy `[HIGH·OBSERVED]`

```c
// validate -> "Failed to validate ELF shared object file" (status) on bad shape.
// align = find_align(elf); load addrs = align_ptr(iram_link_base/dram_link_base, align).
// get_dyn_info -> "Failed to extract dynamic relocation info from ELF shared object file" on fail.
// CODE LOAD seg:
dst = iram_buf + (load_va_code - bounds->iram_link_base);
if (dst_off + p_memsz > iram_size) -> "Segment exceeds the size of the split region on device", ret 13;
memcpy(dst, elf + p_offset, p_filesz);
memset(dst + p_filesz, 0, p_memsz - p_filesz);   // zero the .bss tail
handle[2] = p_memsz;                             // iram_used
// DATA LOAD seg: same into dram_buf, checked against bounds->dram_size; overflow -> ret 13.
```

So `prelink_load_lib` stages the *raw* bytes; `prelink_relocate_lib` then patches
them in place. The poison `0xA5` fill before the copy means any byte the relocator
never touches in the reserved window is visibly `0xA5A5A5A5` on the device — a
diagnostic for un-relocated holes.

---

## 9. The `xtlib_*` public reference loader (the device-direct variant) `[HIGH·OBSERVED]`

Alongside the prelink path (stage-into-host-buffers-then-DMA), the same object
exports the canonical Tensilica `xtlib` host-loader API that relocates via
**caller callbacks** (a `memcpy_fn` + a `memset_fn`, e.g. straight to device
memory). These have **no internal callers** in this `.so` — they are the exported
`xtlib` ABI used by a device-resident loader twin — and they implement the same
algorithm:

- `xtlib_host_load_pi_library @0x9b7650` / `xtlib_host_load_split_pi_library
  @0x9b7900` — validate, `get_dyn_info`, then for each `PT_LOAD` with
  `p_type==1` compute `dst = base + p_vaddr` and call `xtlib_load_seg`.
- `xtlib_load_pi_scratch @0x9b7830` — load the `PT_LOAD` seg flagged
  `(p_flags & 6)==2` (the `.lit` / scratch literal pool) via the load callback.
- `xtlib_load_seg @0x9b6d90` — the per-segment mover: copy `p_filesz` bytes via
  the `memcpy` callback, then zero-fill `p_memsz - p_filesz` via the `memset`
  callback. `p_filesz`/`p_memsz` are endian-swapped through `dword_9BB56C`.

> **Difference vs prelink.** `xtlib_host_load_*` relocate through
> `get_dyn_info` + the segment deltas but write via the **caller's** callbacks
> (device-direct), whereas `prelink` writes into local poison-filled host buffers
> and lets the UCPL header drive a subsequent bulk device write. The **prelink
> path is the one actually used** by `libnrtucode_internal` (§10); the `xtlib_*`
> set is the portable reference loader.

---

## 10. Who calls it — the install seam (`nrtucode_ll_create @0x9b1a90`) `[HIGH·OBSERVED]`

The **single** caller of `prelink` is the loadable-library constructor. Flow
(decompiled C around line 2506):

1. `nrtucode_get_ext_isa_internal(arch, opcode, &lib_descr, flavor)` picks the
   per-arch + per-flavor Q7-POOL ext-ISA PI image (the embedded ELF32 blobs;
   flavor `∈ {debug, test, perf-default}`, plus the
   `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` env override).
2. Allocate the `nrtucode_ll_t` handle (`malloc(0x48)`). Gate on the core-KIND
   `a2`:
   - `a2 > 0x25` → status **8** (unsupported);
   - `_bittest64(0x2020202000, a2)` true for kinds `{13,21,29,37}` (the Q7-POOL
     cores Sunda / Cayman / Mariana / Mariana+) → **allocate** iram/dram scratch
     and **prelink**;
   - the resident Sunda NX core (kind 6) registers **without** prelink (resident
     ucode).
3. `iram_buf = malloc(unk_9AAEF8)`, `dram_buf = malloc(unk_9AAF18)`, then
   ```c
   rc = prelink(lib_elf, &cayman_memory_bounds, ctx,
                prelink_error_log_callback, iram_buf, dram_buf, &ucpl_hdr);
   if (rc) { nrtucode_context_log(ctx,1,"Failed prelink with status = %d", rc); return 9; }
   ```
   (`malloc` failure on either scratch buffer → status 5; note the bounds is
   **always** `&cayman_memory_bounds` — §7.2.)
4. Push the two prelinked images to the device through the nrtucode context
   **platform vtable** (`ctx+8`): the `alloc device region` slot sizes
   `code+data`; the `mem_write_buf` slot (vtable `+24`) does three writes — the
   32-byte UCPL header region, then `iram_buf`, then `dram_buf`. The library size
   is gated `> 0x10000` → `"Prelinked library would be larger than the available
   buffer on device"` and status 7.
5. Name the handle `nrtucode_ll_t@%p`, log `"Initialized …"`, bump the object
   count, return the handle.

> **The reloc-then-stage model — NO residual relocations.** Relocation happens
> **entirely on the host** (`prelink_relocate_lib` patches the local IRAM/DRAM
> buffers in place); only after step (3) returns 0 does step (4) bulk-write the
> *already-relocated* bytes to the device. The
> [device external-library loader](../firmware/pool/external-lib-loader.md) therefore
> receives a fully resolved image and applies **no** relocations of its own — it
> consumes the UCPL header and DMAs the staged `{code,data}` into the running
> core. This is the key contrast with an in-process dynamic loader that leaves a
> `.rela.dyn` for the target's `ld.so`. `[OBSERVED]`

### 10.1 The device-install micro-sequence `[HIGH·OBSERVED]`

`nrtucode_ll_get_load_sequence @0x9b1fb0` → `_common @0x9b1fc0` builds the
TPB-sequencer micro-program that makes the device **copy** the staged library into
the running core. It emits N 64-byte (`<<6`) sequencer slots with header half-word
`4245` (`0x1095`, a DGE/library-load opcode), carrying the ll's staged device addr
(`ll+8` via core vtable slot `+32`) and size (`ll+24`). Up to 8 such blocks (one
per Q7 pool core). `nrtucode_ll_get_load_sequence_num_instrs @0x9b1e30` returns the
slot count (1 per core). This is the device-side consumer of the prelink output —
the bridge from the host-relocated UCPL image to the on-device dispatch.

---

## 11. Error / status codes (the relocator's return channel) `[HIGH·OBSERVED]`

| code | trigger |
|------|---------|
| `0` | success |
| `5` | relocation type byte `> 0xFF` (corrupt table); or host `malloc` failure upstream |
| `7` | `validate_dynamic_load` malformed phdr shape; or prelinked lib too big |
| `8` | `"Invalid relocation address %u"` (outside both reserved windows); `"Unknown relocation type %d"`; `"Failed to set large immediate field …"` |
| `9` | `nrtucode_ll_create`: prelink returned non-zero (`"Failed prelink with status"`) |
| `12` | `"Unknown instruction format [0x%x]"` (`relocate_op` can't classify the bundle) |
| `13` | `"Segment exceeds the size of the split region on device"` (load overflow) |
| (non-fatal) | `"Invalid load @ 0x%x"` / `"Invalid store (0x%x) @ 0x%x"` — bad data load substitutes `0xFFFFFFFF`, walk continues |

All routed through `prelink_error_log_callback @0x9b24b0` →
`nrtucode_context_vlog` (level 1). **Unlike** the NEFF-side patch table (warn-only,
trace aid), a relocation **failure here ABORTS the library install** — these
relocations are correctness-critical.

---

## 12. Adversarial self-verify — the five strongest claims, re-challenged `[meta]`

1. **The R\_XTENSA type set is exactly `{0, 5, 20.., 35..}`.** Challenge: maybe
   the binary handles more types that simply don't appear in this one blob.
   Re-check: `prelink_relocate_lib`'s branch arithmetic only recognizes `type-20
   ∈ [0,14]` (`SLOT*_OP`), `type-35 ∈ [0,14]` (`SLOT*_ALT`), `type==0` (NONE,
   skipped), and `type==5` (`R_XTENSA_32`); **every other** value falls through to
   `"Unknown relocation type %d"` → status 8. So the *handled* set is exactly
   these four kinds. The embedded blob's 240 entries histogram to
   `{35:101, 20:101, 0:8, 5:30}` — no other type present. **CONFIRMED.**
2. **Only TWO `memory_bounds` structs exist; v3/v4/v5 reuse cayman's.** Challenge:
   a per-arch struct might be anonymous (no symbol). Re-check: `nm | rg -c
   memory_bounds` → **2** (`sunda @0x9aaea0`, `cayman @0x9aaee0`); the **single**
   `prelink` call site (line 2506) hardcodes `&cayman_memory_bounds` for all four
   Q7-POOL kinds; no `mariana_*`/`maverick_*` bounds symbol exists. **CONFIRMED**
   (Maverick interior flagged INFERRED — §7.2).
3. **Reloc-then-stage; no residual relocs for the device.** Challenge: maybe the
   device re-applies relocations. Re-check: `prelink` returns only after
   `prelink_relocate_lib` has patched the **host** buffers in place; the device
   write (vtable `+24`) bulk-copies those finished bytes, and the UCPL header
   carries only `{magic, code_sz, code_sz_aligned, data_sz, data_load_va,
   data_link_off}` — no relocation table is forwarded. **CONFIRMED.**
4. **The UCPL magic is `"UCPL "` = `0x204C504355`.** Re-check: the literal store
   `*(u64*)a7 = 0x204C504355` decodes little-endian to `b'UCPL \x00\x00\x00'`.
   **CONFIRMED.**
5. **The type-5 unaligned path uses the `0x9AAF70`/`0x9AAF7C` keep-masks.**
   Re-check: `xxd @0x9aaf70` →
   `{0x000000FF,0x0000FFFF,0x00FFFFFF}` then `{0xFFFFFF00,0xFFFF0000,0xFF000000}`;
   the decompiler indexes `dword_9AAF70[mis-1]` / `dword_9AAF7C[mis-1]` and shifts
   the extracted word by `8*mis` (`__PAIR64__ >> 8/16/24`). **CONFIRMED.**

---

## 13. What a reimplementer needs `[meta]`

To relocate and install a GPSIMD custom kernel: §2 (the 3-segment PI ELF shape +
endian gate), §3 (the `dyn_info` 64-byte descriptor + DT-tag producer), §5/§6 (the
four-type table + the Xtensa per-slot immediate encoder), §7 (the per-arch bounds
+ the two-window gate), §8 (the UCPL header). §10 is where it plugs into the
runtime: one `prelink` per Q7-POOL loadable-library install, at library-create
time, producing the exact bytes the 8 Q7 cores execute.

> **CARRIED / open items.** `[MED·INFERRED]`: the exact per-FLIX-slot bit map
> inside `relocate_op`'s ~8 sub-format branches (the shift/mask constants are
> OBSERVED, but each maps to a specific ncore2gp TIE slot immediate layout not
> re-derived field-by-field); the `dyn_info +0x18..+0x28` `DT_INIT`/`DT_FINI`
> grouping (populated, not exercised by the reloc walk); the precise meaning of
> the high 24 bits of `r_info` beyond the type byte. `[LOW]`: whether any shipped
> Q7 image is ever big-endian (the swap path exists; all observed blobs are LE);
> the SRAM/EXTRAM blob variants' role (only IRAM/DRAM windows are gated here).
