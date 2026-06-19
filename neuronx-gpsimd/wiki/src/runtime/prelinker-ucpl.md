# The Host Prelinker — UCPL / Segment Loader / R_XTENSA / Staging

> **Binary:** `libnrtucode_internal.so` (host x86-64 ELF, **not** stripped,
> BuildID `9cbf78c6…`, 10,276,288 B) from the `aws-neuronx-gpsimd-customop-lib`
> `0.21.2.0` package — **not** `libnrt.so`. Every symbol, address, struct byte,
> reloc type, instruction mask and reloc-store below was re-verified against that
> binary this session (`nm`/`objdump`/`readelf`/`xxd`), cross-checked against the
> stripped shipped twin and the static archive `libnrtucode.a` where noted.
>
> **Section deltas** (`readelf -SW`, confirmed): `.rodata` VMA==fileoffset
> (Δ=0), `.text` Δ=0x1000, `.data.rel.ro` Δ=0x2000, `.data` Δ=0x3000. Every
> `.rodata` table below (`cayman_memory_bounds`, the FLIX field-clear masks, the
> type-5 keep-masks) is at VMA==fileoffset and was `xxd`'d directly.
>
> **GOTCHA — zero C++ vtables.** `nm libnrtucode_internal.so | rg -c '_ZTV'` → **0**.
> Every function-pointer table in this library (the memhandle vtable §4, the
> `get_dyn_info` `d_tag` jump table §2.4) is a plain C array: **slot N =
> `table_symbol + 8*N`**. The `_ZTV + 0x10` rule used elsewhere in this wiki
> **does not apply here**. Every count on this page is grounded via
> `nm <lib> | rg -c` or a direct disasm read, never grepped from a decompile.

This page is the definitive end-to-end reference for the GPSIMD **host
prelinker** — the x86-64 routine in `libnrtucode` that turns a packaged 32-bit
Xtensa custom-op library into a fully position-resolved, device-stageable image
and emits the compact **UCPL** header that the on-device loader consumes. It is
the *producer* half of the loader; the *consumer* half (the on-device init pass)
is documented in
[runtime/ucode-relocation-consumer.md](ucode-relocation-consumer.md) and the
device external-library loader in
[firmware/pool/external-lib-loader.md](../firmware/pool/external-lib-loader.md).

The chain is exactly four stages, executed by one function `prelink @0x9b5d60`:

```
prelink @0x9b5d60                                            [§1 entry, §3.6 UCPL emit]
  ├─ (1) validate + parse  → xtlib_verify_magic, validate_dynamic_load   [§2]
  ├─ (2) load segments     → prelink_load_lib @0x9b5e70                   [§2]
  │        builds the small "desc" + the xtlib_pil_info "pil"
  ├─ (3) relocate          → prelink_relocate_lib @0x9b6160              [§3]
  │        → relocate_op @0x9b6660 (per-slot FLIX operand fixup)
  └─ (4) emit UCPL header  → 0x20 bytes, magic "UCPL "                    [§3.6]
              then ll_create stages it to device via the memhandle vtable [§4]
```

> **HOST-vs-DEVICE [HIGH·OBSERVED].** The prelinker is **host-resident x86-64**.
> Every function in the chain (`prelink`, `prelink_load_lib`,
> `prelink_relocate_lib`, `relocate_op`, `reloc_addr`, `xtlib_*`) is an x86-64
> SysV-ABI `.text` symbol — `nm -n` shows them at `0x9b5d60`-band, and `objdump`
> decodes them as `push %r1x` / AT&T x86, **not** Xtensa. The names
> `xtlib_host_load_*` are the Tensilica *host-load* API: the host pre-loads +
> relocates the image into the target's address space and hands off a flattened
> blob; the device only runs `xtlib_target_init_pi_library` (init + entry expose,
> **no** relocation). Only the per-NX region table (`cayman` vs `sunda`) is
> generation-specific.

---

## Chain symbol map `[HIGH·OBSERVED — nm -n + objdump]`

| symbol | addr | role | stage |
|--------|------|------|-------|
| `prelink` | `0x9b5d60` | entry; poison scratch → load → relocate → emit UCPL | 1,4 |
| `prelink_load_lib` | `0x9b5e70` | Xtensa-ELF segment loader + descriptor builder | 2 |
| `validate_dynamic_load` | `0x9b71f0` | Ehdr/Phdr split-load validation | 2 |
| `xtlib_verify_magic` | `0x9b6d40` | `\x7fELF` + ELFCLASS32 + EI_DATA endian flag | 2 |
| `xtlib_load_seg` | `0x9b6d90` | per-seg `memcpy(filesz)` + `memset` bss (inlined ×2) | 2 |
| `find_align` / `align_ptr` | `0x9b6e50` / `0x9b6e40` | max `sh_addralign` / `(p+a-1)&~(a-1)` | 2 |
| `get_dyn_info` | `0x9b6ed0` | PT_DYNAMIC parse → the `pil` descriptor | 2 |
| `prelink_relocate_lib` | `0x9b6160` | `Elf32_Rela` iterator + type dispatch | 3 |
| `relocate_op` | `0x9b6660` | per-slot FLIX/narrow operand-field patcher | 3 |
| `reloc_addr` | `0x9b6130` | 2-way segment rebase + `0x400` scratch bias | 3 |
| `log_error` | `0x9b60a0` | error callback shim | all |
| `cayman_memory_bounds` | `0x9aaee0` | wired region table (8 qwords) | 2,3 |
| `sunda_memory_bounds` | `0x9aaea0` | dormant alt region table (zero xrefs) | — |
| `plat_memhandle_dummy` | `0x9b8cf0` | default (stub) 5-slot memhandle vtable | 4 |

The `.a` archive member names (`ar t libnrtucode.a`) confirm the source modules:
`prelink.c`, `prelink_load.c`, `prelink_relocate.c`, `prelink_memory_bounds.c`,
`prelink_log.c`. `prelink` + the UCPL emit are **byte-identical** across the
stripped `.so`, the internal twin, and `prelink.c.o`. `[HIGH·OBSERVED]`

---

## 1. `prelink @0x9b5d60` — the entry + the UCPL chain `[HIGH·OBSERVED]`

Signature (recovered from the prelink frame + the `load_lib`/`relocate_lib`
call sites), with the stack arg the UCPL header is emitted into:

```c
// prelink @0x9b5d60.  sub $0x90 after 3 pushes → stack arg0 @ rsp+0xb0.
int prelink(const void       *pi_library,   /* rdi : packaged Xtensa ELF32 ET_DYN  */
            const mbounds_t  *bounds,       /* rsi : &cayman_memory_bounds         */
            void             *ctx,          /* rdx : nrtucode_context_t* (logging)  */
            void             *log_cb,       /* rcx : prelink_error_log_callback     */
            void             *scratch_code, /* r8  : host buf, cayman[0x18]=0x8000  */
            void             *scratch_data, /* r9  : host buf, cayman[0x38]=0x2000  */
            ucpl_header_t    *out_header)   /* @rsp+0xb0 : the 0x20-byte UCPL out    */
{
    memset(scratch_code, 0xA5, bounds[0x18]); // @9b5da0  poison code buf (32 KiB)
    memset(scratch_data, 0xA5, bounds[0x38]); // @9b5db1  poison data buf ( 8 KiB)
    xorps_zero(&pil, 0x40);                    // @9b5dc2  zero the pil_info (0x40 B)

    int rc = prelink_load_lib(&desc, pi_library, bounds[0x10], bounds[0x30], &pil);
    if (rc) return rc;                         // @9b5dff  segment load failed     [§2]
    rc = prelink_relocate_lib(&desc, &pil);
    if (rc) return rc;                         // @9b5e12  relocation failed       [§3]

    emit_ucpl_header(out_header, &desc, &pil); // @9b5e16..9b5e5b                   [§3.6]
    return 0;                                  // @9b5e5e
}
```

The stack frame carries **two** working structs that the load and relocate
stages produce and consume (this is the key structural fact — there is *not* one
"desc"):

* the small **`desc`** (`rsp+0x08`, `load_lib` arg0) — `{bounds, scratch_code,
  code_len, scratch_data, data_len, ctx, log_cb}`, a host control block (§2.1);
* the **`pil`** = `xtlib_pil_info` host-8B-ptr variant (`rsp+0x40`, `load_lib`
  arg4) — `{dst/src bases, start_sym, text_addr, init, fini, rela(8B ptr),
  rela_count, hash, symtab, strtab, align, scratch_section_found}` (§2.4).

> **The two segment lengths** `desc.code_seg_len` (init 0, written by `load_lib`
> with the **code `p_memsz`**) and `desc.data_seg_len` are the only `load_lib`
> outputs that flow into the UCPL header; everything else `load_lib` writes goes
> into the `pil` for the relocator. `[HIGH·OBSERVED]`

### 1.1 The small `desc` control block `[HIGH·OBSERVED]`

`prelink` builds this on its stack (`rsp+0x08`) and passes it as `load_lib` arg0.
Offsets are cross-checked against the prelink stores and the `load_lib`
reads/writebacks:

| off | sz | field | role |
|-----|----|-------|------|
| `0x00` | 8 | `bounds` | → `cayman_memory_bounds` |
| `0x08` | 8 | `scratch_code` | host buf (cayman[0x18]=32 K); code `memcpy` dst |
| `0x10` | 8 | `code_seg_len` | **WRITEBACK** = code `p_memsz` → `UCPL[0x08]=align4` |
| `0x18` | 8 | `scratch_data` | host buf (cayman[0x38]=8 K); data `memcpy` dst |
| `0x20` | 8 | `data_seg_len` | **WRITEBACK** = data `p_memsz` → `UCPL[0x10]=align4` |
| `0x28` | 8 | `ctx` | `nrtucode_context_t*` (used by `log_error`) |
| `0x30` | 8 | `log_cb` | error callback fn ptr |

The two `_seg_len` fields are the only outputs `load_lib` writes here; the rest are
inputs.

---

## 2. `prelink_load_lib @0x9b5e70` — segment loader + descriptor builder

`prelink_load_lib` is a direct compilation of the Tensilica split-load host-load
path. It validates the ELF, finds the max section alignment, computes each
segment's host-scratch destination, parses `PT_DYNAMIC` into the `pil`, then
copies the two `PT_LOAD` segments into the scratch buffers with `.bss` zero-fill.

```c
// prelink_load_lib @0x9b5e70 (internal call sequence, instruction-anchored)
int prelink_load_lib(desc_t *desc, const Ehdr *e, xt_ptr code_base,
                     xt_ptr data_base, pil_t *pil)
{
    xtlib_validate_t v;
    if (validate_dynamic_load(&v, e)) {        // @9b5e92  §2.2 → log + return status
        log_error(ctx, "...validate ELF shared object file");  // .rodata 0x5309
        return v.status;                       //          1=NOT_ELF / 7=NOT_SPLITLOAD
    }
    int al = find_align(e);                     // @9b5eb6  max sh_addralign      §2.3
    xt_ptr cdst = align_ptr(code_base, al);     // @9b5ec2
    xt_ptr ddst = align_ptr(data_base, al);     // @9b5ece
    // dst = align_ptr(region_base, al) + (p_paddr & (al-1))   ← note p_PADDR, not p_vaddr
    cdst += host_word(phdr[0].p_paddr) & (al-1);// @9b5eed
    ddst += host_word(phdr[1].p_paddr) & (al-1);// @9b5f02

    int rc = get_dyn_info(e, &v, cdst, phdr[0].p_paddr,
                          ddst, phdr[1].p_paddr, pil);   // @9b5f44             §2.4
    if (rc) { log_error(ctx, "...extract dynamic relocation info"); return rc; } // 0x5134

    load_seg(desc->scratch_code, cdst, code_base, &phdr[0], &desc->code_seg_len); // §2.5
    load_seg(desc->scratch_data, ddst, data_base, &phdr[1], &desc->data_seg_len);
    return 0;
}
```

### 2.0 ELF parse + the offsets the loader reads `[HIGH·OBSERVED]`

The `pi_library` is a 32-bit Xtensa ELF `ET_DYN` **split-load** shared object.
Every multi-byte field is read through `xtlib_host_word`/`xtlib_host_half`, which
conditionally byte-swap on `xtlib_globals+4` (`@0x9bb56c`, set by
`xtlib_verify_magic`):

```
Ehdr:  e_entry@0x18  e_phoff@0x1c  e_shoff@0x20  e_phnum@0x2c(half)  e_shnum@0x30(half)
Phdr (stride 0x20): p_type@0  p_offset@4  p_vaddr@8  p_paddr@0xc  p_filesz@0x10
                    p_memsz@0x14  p_flags@0x18  p_align@0x1c
Shdr (stride 0x28): sh_type@4  sh_size@0x14  sh_addralign@0x20   (find_align)
Dyn  (stride 0x10): d_tag@0  d_un@4                              (get_dyn_info)
```

> **NOTE — placement uses `p_paddr`.** The destination is
> `align_ptr(region_base, align) + (p_paddr & (align-1))`. The loader reads
> **`p_paddr`** (offset 0xc), not `p_vaddr`, as the segment placement offset.
> `[HIGH·OBSERVED]`

### 2.1 `xtlib_verify_magic @0x9b6d40` — ELF magic + class + endian `[HIGH·OBSERVED]`

Disassembled this session, byte-exact:

```c
// xtlib_verify_magic @0x9b6d40
int xtlib_verify_magic(const Ehdr *e) {
    if (e->e_ident[0] != 0x7f) return -1;   // @9b6d45  cmpb $0x7f,(%rdi)
    if (e->e_ident[1] != 'E')  return -1;   // @9b6d4a  $0x45
    if (e->e_ident[2] != 'L')  return -1;   // @9b6d50  $0x4c
    if (e->e_ident[3] != 'F')  return -1;   // @9b6d56  $0x46
    if (e->e_ident[4] != 1)    return -1;   // @9b6d5c  EI_CLASS == ELFCLASS32 (REQUIRED)
    switch (e->e_ident[5]) {                 // @9b6d62  EI_DATA
        case 1:  xtlib_globals[1] = 0; break;// ELFDATA2LSB → no byteswap (@9b6d6e je)
        case 2:  xtlib_globals[1] = 1; break;// ELFDATA2MSB → byteswap   (@9b6d75)
        default: return -1;                  // @9b6d73
    }
    return 0;                                // @9b6d7a  mov %edx,xtlib_globals+0x4 (0x9bb56c)
}
```

`xtlib_host_word @0x9b6e00`: `bswap %eax ; cmove` — swap iff `globals+4 != 0`.
`xtlib_host_half @0x9b6e20`: `rol $8,%ax ; cmove`. The EI_DATA byte is the **only**
"version-like" discriminator anywhere in the pipeline; the UCPL header has no
version field (§3.6).

### 2.2 `validate_dynamic_load @0x9b71f0` — split-load Phdr validation `[HIGH·OBSERVED]`

Produces a 0x30-byte `xtlib_validate_t` (zeroed via 3× `movups`):

| off | sz | field | value |
|-----|----|-------|-------|
| `0x00` | 4 | `status` | `0` ok / `1` NOT_ELF / `7` NOT_SPLITLOAD |
| `0x04` | 4 | `number_segments` | 2, or 3 if a scratch seg is present |
| `0x08` | 8 | `{scratch_found:lo, scratch_index:hi}` | `0x200000001` iff scratch present (else 0) |
| `0x10`,`0x18`,`0x20`,`0x10+ns*8` | 8 | `Phdr*` | text / data / [scratch] / dynamic |

The accepted layout (all flags via `xtlib_host_word`):

* `phdr[0]`: `p_type==1` (PT_LOAD), `p_flags & 7 == 5` (PF_R\|PF_X) — **text**.
* `phdr[1]`: `p_type==1`, `~p_flags & 6 == 0` (PF_W\|PF_R set, X don't-care) — **data**.
* optional `phdr[2]`: `p_type==1`, `p_flags & 6 == 2` (PF_W set, PF_R clear, the
  `-WX` **scratch** seg) → `scratch_found=1`, `scratch_index=2`, `ns=3`.
* then a Phdr with `p_type==2` (PT_DYNAMIC), `p_flags & 7 == 6` (PF_R\|PF_W);
  absent/wrong → `status = 7` NOT_SPLITLOAD.

> **NOTE — looser-than-canonical flag masks.** The binary's data check is
> `~flags & 6 == 0` and the scratch check is `flags & 6 == 2` — both ignore the
> X bit, slightly looser than the 3-bit canonical equalities (data RWX=7,
> scratch -WX=3). On the documented split-load layout they are equivalent.
> `[masks HIGH·OBSERVED; equivalence MED·INFERRED]`

### 2.3 `find_align` / `align_ptr` `[HIGH·OBSERVED]`

`align_ptr(p,a)` = `lea (rdi,rsi),eax ; dec ; neg esi ; and` = `(p+a-1)&~(a-1)`
(4 instructions, exact). `find_align(e)` walks the Shdr table (stride 0x28),
skipping `SHT_NULL` and zero-size sections, and `cmovg`-maxes `sh_addralign`. The
result is `pil.align @+0x38` and drives the destination base alignment.

### 2.4 `get_dyn_info @0x9b6ed0` — the `pil` descriptor build `[HIGH·OBSERVED]`

`get_dyn_info` locates PT_DYNAMIC (inlined `p_type==2` scan; none → rc `4`
NO_DYNAMIC_SEGMENT), then walks `Elf32_Dyn` entries (stride 0x10) through a
**10-entry jump table at `0x9aaf88` (= `cayman_memory_bounds + 0xa8`)**, dispatch
`ecx = host_word(d_tag) - 4 ; cmp $9 ; ja default`:

| idx | `d_tag` | target | writes |
|-----|---------|--------|--------|
| 0 | `DT_HASH=4` | `0x9b70e7` | `pil[0x2c]=hash` (data-rebased) |
| 1 | `DT_STRTAB=5` | `0x9b7162` | `pil[0x34]=strtab` (data-rebased) |
| 2 | `DT_SYMTAB=6` | `0x9b711d` | `pil[0x30]=symtab` (data-rebased) |
| 3 | `DT_RELA=7` | `0x9b7133` | `pil[0x20]=rela` (scratch-aware, §2.4.1) |
| 4 | `DT_RELASZ=8` | `0x9b717b` | `pil[0x28]=d_val/12` (rela_count) |
| 5 | `DT_RELAENT=9` | `0x9b70c0` | no-op |
| 6 | `DT_STRSZ=10` | `0x9b70c0` | no-op |
| 7 | `DT_SYMENT=11` | `0x9b70c0` | no-op |
| 8 | `DT_INIT=12` | `0x9b719a` | `pil[0x18]=init` (text-rebased) |
| 9 | `DT_FINI=13` | `0x9b70a1` | `pil[0x1c]=fini` (text-rebased) |
| default | `0x70000002` (DT_LOPROC+2) | `0x9b7104` | `pil[0x14]=text_addr` (data-rebased) |

`rela_count = DT_RELASZ / sizeof(Elf32_Rela)` is the `imul $0xaaaaaaab ; shr $0x23`
divide-by-12 magic (verified: 12→1, 24→2, 132→11, 1200→100). Of the 16 pil
fields, **only `pil[0x20]` (rela) is an 8-byte store** (`mov %rdx,0x20(%rbx)`) —
a host pointer into the in-image rela table; every other field is a 4-byte store.
The host `pil` is `xtlib_pil_info` with `rel` widened to 8 B, `sizeof = 0x40` (the
4-byte public twin is `0x3c`). `pil[0x10] start_sym` and `pil[0x18]/[0x1c]
init/fini` are the three fields the UCPL header reads (§3.6).

#### 2.4.1 The `DT_RELA` scratch-aware branch (`0x9b7133`) `[HIGH·OBSERVED]`

```c
if (pil->scratch_section_found) {                       // cmpl $0,0x3c(%rbx)
    Phdr *s = val->segment[val->scratch_segment_index];  // scratch Phdr*
    pil->rela = (host)e + s->p_offset + d_ptr - s->p_vaddr;  // rela table INSIDE the image
} else {
    pil->rela = host_word(d_ptr + (dst_data_addr - src_data_offs)); // data-rebased
}
```

When a `-WX` scratch segment carries the rela table, `pil[0x20]` points at the
table **inside the original `pi_library` image** (a host address), not at the
loaded data buffer. The relocator (§3) advances `pil[0x20] + 8` to
`&rela[0].r_addend` and iterates `rela_count` entries.

### 2.5 `xtlib_load_seg` — the per-segment copy `[HIGH·OBSERVED]`

`xtlib_load_seg @0x9b6d90` is inlined twice. Per `PT_LOAD`:

```c
// dst = scratch_base + (dst_seg - region_base)
memcpy(dst, pi_library + p_offset, p_filesz);          // @9b5fd4 / @9b606e
memset(dst + p_filesz, 0, p_memsz - p_filesz);         // @9b5fec / @9b6087  .bss zero-fill
*seg_len_writeback = p_memsz;                          // @9b5ff6 / @9b608c  ← p_MEMSZ, not filesz
```

> **NOTE — the writeback is `p_memsz`, not `p_filesz`.** `memcpy` moves
> `p_filesz`; `memset` zero-fills to `p_memsz`; the desc length the UCPL header
> advertises is the **in-memory** size (so the device reserves the full bss).
> `UCPL.code_seg_len = align4(code p_memsz)`, `UCPL.data_seg_len =
> align4(data p_memsz)`. `[HIGH·OBSERVED]`

The `-WX` scratch segment (if present) is **not** body-loaded — only `phdr[0]`
(code) and `phdr[1]` (data) are copied; the scratch seg carries the in-image rela
table (§2.4.1).

### 2.6 Region-bounds — the "Segment exceeds region" rc 13 `[HIGH·OBSERVED]`

Before each `memcpy`, the loader checks `(dst_seg - region_base) + p_memsz` against
the cayman region size:

```c
// code @9b5f9f:  r12 = dst_code - bounds[0x10] ; cmp (r12 + p_memsz), bounds[0x18] ; ja overflow
// data @9b6022:  r15 = dst_data - bounds[0x30] ; cmp (r15 + p_memsz), bounds[0x38] ; ja overflow
// overflow @9b6033:  log_error(ctx, "Segment exceeds the size of the split region on device")  // .rodata 0x49bd
//                    return 13;  // XTLIB_INTERNAL_ERR
```

`cayman_memory_bounds @0x9aaee0` (`xxd`'d this session, 8 qwords):

| off | value | role |
|-----|-------|------|
| `+0x00` | `0` | — |
| `+0x08` | `0x20000` (128 K) | code total |
| `+0x10` | `0x18000` (96 K) | **code region base** |
| `+0x18` | `0x8000` (32 K) | **code region size** (= `scratch_code` buf) |
| `+0x20` | `0x80000` (512 K) | — |
| `+0x28` | `0x40000` (256 K) | data total |
| `+0x30` | `0xbe000` (760 K) | **data region base** |
| `+0x38` | `0x2000` (8 K) | **data region size** (= `scratch_data` buf) |

`sunda_memory_bounds @0x9aaea0` is present but **dormant** (zero code xrefs in
this build) — its code region (`[0x10]=0x5500`, `[0x18]=0xab00`) and data region
(`[0x30]=0x8e000`) differ; the prelink *algorithm* and the UCPL *format* are
mode-invariant. `[cayman HIGH·OBSERVED; sunda-selection mechanism not present, MED]`

> **GOTCHA — two independent ceilings.** (a) the per-segment cayman region size
> (32 K / 8 K) checked here in `load_lib`; (b) the **64 KiB** cap on the *total*
> staged image, enforced by the caller `ll_create` *after* `prelink` returns
> (`cmpq $0x10001`, §4.2). `load_lib` never tests 64 KiB. Do not conflate them.

---

## 3. `prelink_relocate_lib @0x9b6160` + `relocate_op @0x9b6660` — R_XTENSA

The relocator walks the `Elf32_Rela` table the loader pinned, rejects any
symbol-bearing reloc, rebases addresses through `reloc_addr`, and patches the
Xtensa instruction/data words **in place in the host scratch buffers**. The
staged image therefore contains *fully relocated* code with **zero** residual
relocations — the UCPL header carries no reloc/symbol table by design.

> **RECONCILED with the committed consumer page (DX-RT-12,
> [runtime/ucode-relocation-consumer.md](ucode-relocation-consumer.md)).** Same
> binary, same anchors: `prelink_relocate_lib @0x9b6160`, `relocate_op @0x9b6660`,
> `reloc_addr @0x9b6130`. The handled R_XTENSA set is identical (§3.1). The
> consumer page additionally provides the **ground-truth blob histogram** from a
> real device PI image (240 `Elf32_Rela` entries:
> `{35:101, 20:101, 0:8, 5:30}`) — confirming exactly the four kinds below.

### 3.1 The handled R_XTENSA type set `[HIGH·OBSERVED]`

The dispatch in `prelink_relocate_lib @0x9b61c5..` (disassembled this session):

```
9b61c8  cmp $0xff,%edx ; ja 0x9b6625    ; r_info > 0xff  → rc 5 UNKNOWN_SYMBOL (STN_UNDEF gate)
9b61f0  lea -0x14(%rdx),%eax            ; -20
9b6203  cmp $0xf,%eax ; jb 0x9b62ac     ; type ∈ [20,35)  → SLOT*_OP
9b620c  lea -0x23(%rdx),%eax            ; -35  (then cmp $0xf,jb → SLOT*_ALT)
        test %dl,%dl ; je               ; type 0          → NONE (skip)
        cmp $0x5     ; jne → "Unknown"  ; type 5          → §3.3 data-word fixup
```

| type | name | apply path |
|------|------|------------|
| `0` | `R_XTENSA_NONE` | skip (no-op) |
| `5` | `R_XTENSA_32` *(see CORRECTION)* | §3.3 in-place additive 32-bit word fixup via `reloc_addr` |
| `20`(`0x14`)..`34` | `R_XTENSA_SLOT{0..14}_OP` | §3.4 instruction operand-field, **low** half |
| `35`(`0x23`)..`49` | `R_XTENSA_SLOT{0..14}_ALT` | §3.4 instruction operand-field, **high** half (`value>>16`) |

Anything else (and any `r_info > 0xff`) → error. The constants `0x14=20`,
`0x23=35`, `0xf=15`, `0x5`, `0xff` are byte-exact in `objdump`. The slot index
`0..14` is `type-20` (OP) / `type-35` (ALT) and selects which FLIX operand to
patch; ALT patches the high 16 bits of a CONST16 / `L32R` / `MOVI` two-word
constant materialization.

> **CORRECTION — type 5 naming: `R_XTENSA_32`, not `R_XTENSA_RELATIVE`.** An
> earlier survey pass named type 5 `R_XTENSA_RELATIVE`. The committed consumer
> page names it **`R_XTENSA_32`** and grounds it in the device blob histogram
> (`5:30` of 240 real entries). Both describe the **same** code path — an
> in-place *additive* 32-bit word relocation rebased through `reloc_addr`
> (`*w = reloc_addr(*w + r_addend)`); the disagreement is purely the ELF enum
> label. In a position-independent split-load `pi_library` the linker emits
> self-relative data-word relocs; whichever label the toolchain stamps, the
> binary's path is identical. **This page standardizes on `R_XTENSA_32` (type 5)**
> to match the committed consumer page and the device-blob ground truth.
> `[CORRECTION·HIGH — histogram OBSERVED on the device blob]`

> **GOTCHA — symbol resolution is degenerate by contract.** `r_info > 0xff`
> (`ELF32_R_SYM(r_info) != STN_UNDEF`) → return `5` `XTLIB_UNKNOWN_SYMBOL`. There
> is **no** dynamic-symbol lookup; a packaged `pi_library` must be fully
> self-relative (every reloc symbol-less). "Resolution" is purely the segment-base
> arithmetic in `reloc_addr`. `[HIGH·OBSERVED]`

### 3.2 `reloc_addr @0x9b6130` — the 2-way segment rebase `[HIGH·OBSERVED]`

The only "address → device VA" mechanism. Disassembled this session, byte-exact:

```c
// reloc_addr @0x9b6130
u32 reloc_addr(desc_t *d, u32 addr) {
    u32 sep = d->scratch_section_found ? 0x400 : 0;   // @9b6132 cmpl ; setne ; shl $0xa (×0x400)
    if (addr + sep >= d->src_data_offs)                // @9b613e mov 0xc ; cmp ; jae
        return (d->dst_data_addr - d->src_data_offs) + addr;  // @9b614d  data side
    else
        return (d->dst_addr - d->src_offs) + addr;            // @9b6145  text side
}
```

An address in the source image's **text** range is rebased to the device **code**
base (`cayman[0x10]`); one at/above the data offset to the device **data** base
(`cayman[0x30]`). The `+0x400` `MIN_SEPARATION` bias, applied when a scratch
section is present, pushes a scratch-region address onto the data side. The `pil`
fields consumed here: `dst_addr@0`, `src_offs@4`, `dst_data_addr@8`,
`src_data_offs@0xc`, `scratch_section_found@0x3c`.

### 3.3 The `Elf32_Rela` iteration + the type-5 data-word fixup `[HIGH·OBSERVED]`

```c
int prelink_relocate_lib(desc_t *desc, pil_t *pil) {
    if (pil->rela_count <= 0) return 0;                 // @prologue cmpl $0,0x28 ; jle
    u8 *cur = (u8*)pil->rela + 8;                       // &rela[0].r_addend (table + 8)
    for (int i = 0; i < pil->rela_count; i++, cur += 0xc) {  // Elf32_Rela stride 12
        u32 r_off = *(u32*)(cur - 8);                   // rela[i].r_offset
        u32 r_info= *(u32*)(cur - 4);                   // rela[i].r_info
        if (r_info > 0xff) return 5;                    // STN_UNDEF gate (§3.1)
        u32 type  = r_info & 0xff;
        u32 site  = reloc_addr(desc, r_off);            // device VA of the patch site
        if (type == 0) continue;                        // NONE
        if (type == 5) {                                // §3.1 CORRECTION: R_XTENSA_32
            u32 add = *(u32*)cur;                       // rela[i].r_addend
            // map site → host word via desc {0x10,0x18}/{0x30,0x38}; *w = reloc_addr(desc, *w + add)
            // unaligned sites: 2-load shld-extract → rebase → shld-combine → 2-store
            continue;
        }
        u32 value = reloc_addr(desc, *(u32*)cur);        // resolved target
        if (type >= 35) value >>= 16;                    // @9b62fb shr $0x10 — ALT high half
        u32 slot  = (type < 35) ? type - 20 : type - 35;
        int rc = relocate_op(desc, site, type, value);   // @9b61a4 (sole call site)  §3.4
        if (rc) return rc;
    }
    return 0;
}
```

The type-5 unaligned path reads two adjacent words, `shld`-extracts the straddling
32-bit value (`a_ofs = site & 3` → `shld $8/$10/$18`), rebases it, then
`shld`/mask-combines it back. The byte-lane **keep-masks** are six dwords at
`0x9aaf70` (`xxd`'d, little-endian):

| `a_ofs` | low-word mask `dword_9AAF70[a_ofs-1]` | high-word mask `dword_9AAF7C[a_ofs-1]` |
|---------|--------------------------------------|----------------------------------------|
| 1 | `0x000000ff` | `0xffffff00` |
| 2 | `0x0000ffff` | `0xffff0000` |
| 3 | `0x00ffffff` | `0xff000000` |

> **GOTCHA — dword vs raw-byte rendering of the keep-masks.** `objdump -s` prints
> the 24 bytes at `0x9aaf70` left-to-right (`ff000000 ffff0000 …`). Read as
> little-endian `u32`s (how the code indexes them) they are
> `{0x000000ff, 0x0000ffff, 0x00ffffff}` (low table @`0x9aaf70`) and
> `{0xffffff00, 0xffff0000, 0xff000000}` (high table @`0x9aaf7c`). The table above
> uses the LE dword values — the form the relocator actually `and`s. `[HIGH·OBSERVED]`

### 3.4 `relocate_op @0x9b6660` — the per-slot operand-field patcher `[HIGH·OBSERVED]`

```c
// relocate_op @0x9b6660
int relocate_op(desc_t *desc, u32 site /*device VA*/, int type, u32 value) {
    // 1. map site → host instruction word(s) via desc {0x30,0x38}(data) / {0x10,0x18}(code)
    //    @9b6676 mov 0x30(%r9); @9b6681 mov 0x38(%r9); @9b6693 mov 0x10(%r9); @9b669e mov 0x18(%r9)
    //    no mapping → "Invalid relocation address %u" (.L.str.1) + rc 8
    u8 *w = host_word_for(site);
    u32 insn = *(u32*)w;                                  // @9b66bf
    if (!(insn & 0x8)) {                                  // @9b66c1 test $0x8,%al — 16-bit NARROW
        value = (value & 0xffff) << 8;                    // @9b66c5 movzwl ; shl $0x8
        *(u32*)w = (insn & 0xff0000f7) | value;           // @9b66cb and $0xff0000f7 ; or ; store
        return 0;
    }
    // WIDE / CONST16 / FLIX: classify bundle length
    u32 len;                                              // @9b66e8..9b6733
    if      ((insn & 0x100000f) == 0xf)        len = 16;  // class A
    else if ((insn & 0xf)       == 0xe)        len = 16;  // class B
    else if ((insn & 0x300000f) == 0x100000f)  len = 8;   // 8-byte bundle
    else  { log("Unknown instruction format [0x%x]" /*.L.str.2*/); return 12; } // INSN_FMT_ERR
    // aligned 16-byte local copy; scatter `value` into the matched FLIX slot,
    // clear the old field with one of the five SSE field-clear masks, OR in new bits,
    // memcpy the patched bundle back.   On unmatched sub-format → rc 8 (.L.str.4)
    ...
    return 0;
}
```

The narrow 16-bit set (mask `0xff0000f7`, value bits `[0..15] << 8`) is byte-exact.
The wide path makes an aligned 16-byte local copy, then matches the FLIX format-id
in `insn[0]`'s top bits and `pand`s one of **five 16-byte SSE field-clear masks**
(at `cayman+0x40..+0x80`, `xxd`'d this session — qword0 LE):

| `.a` label | VA | qword0 mask | format-id match |
|------------|----|-------------|-----------------|
| `.LCPI2_4` | `0x9aaf20` | `0xffa0ffffdfffffff` | `& 0x410c0000 == 0x41000000` |
| `.LCPI2_0` | `0x9aaf30` | `0x3ff9bfffdfffffff` | `& (esi) == 0x41060000` |
| `.LCPI2_2` | `0x9aaf40` | `0xffe07fff9fffffff` | `& 0x410e8000 == 0x41080000` |
| `.LCPI2_3` | `0x9aaf50` | `0xfff07fffdffffff7` | `& 0x410e0000 == 0x41040000` |
| `.LCPI2_1` | `0x9aaf60` | `0xffe87f7f9fffffff` | `cmp 0x41088000` |

The repeated `shl $0x8 ; movzwl ; shl $0x12 ; and $0x4000000 ; or` micro-ops are
the operand scatter that lays the relocated target across the Xtensa
instruction's **non-contiguous** immediate bits (a `CALL`/`J`/`L32R` target is
split across instruction bits; the shifts reconstruct that layout). On success rc
`0`. `[bit-mechanics HIGH·OBSERVED; the format→operand semantics MED·INFERRED from
the CONST16 FLIX model]`

> **NOTE — these masks are operand-field masks, not memory bounds.** They sit
> immediately after `cayman_memory_bounds` in `.rodata` (which ends at
> `0x9aaf20`), so an earlier pass mistook them for a `0x40`-byte extension of the
> bounds table. They are the `.LCPI2_*` `.rodata.cst16` field-clear masks. The
> `cayman_memory_bounds` table is exactly **8 qwords (0x40 B)**. `[CORRECTION·HIGH]`

### 3.5 Relocator error codes `[HIGH·OBSERVED]`

| rc | enum | trigger | string |
|----|------|---------|--------|
| `0` | `XTLIB_NO_ERR` | every reloc applied | — |
| `5` | `XTLIB_UNKNOWN_SYMBOL` | `r_info > 0xff` (nonzero sym idx) | — (bare return) |
| `8` | `XTLIB_RELOCATION_ERR` | unknown type / unmapped addr / bad store/load / FLIX sub-format | `"Unknown relocation type %d"`, `"Invalid relocation address %u"`, `"Invalid store (0x%x) @ 0x%x"`, `"Invalid load @ 0x%x"`, `"Failed to set large immediate field in instruction slot %i"` |
| `12` | `XTLIB_INSN_FMT_ERR` | instruction length-class decode failed | `"Unknown instruction format [0x%x]"` |

Any nonzero rc aborts the loop and propagates to `prelink` → `ll_create`, failing
the load. The on-device image is **never** partially relocated and staged.

### 3.6 The UCPL header — byte-exact emit `[HIGH·OBSERVED]`

After load + relocate succeed, `prelink @0x9b5e16..0x9b5e5b` emits the **0x20-byte
UCPL header** into `*out_header`. Disassembled this session, instruction-anchored:

```c
// emit_ucpl_header(out, desc, pil)  —  prelink @0x9b5e16
out->magic        = 0x204c504355;                       // @9b5e1e movabs ; @9b5e28 store
                                                         //   = "UCPL " + 3 NUL: 55 43 50 4c 20 00 00 00
out->code_seg_len = (desc->code_seg_len + 3) & ~3;      // @9b5e2b align4(code p_memsz) → +0x08
out->data_seg_off = (out->code_seg_len + 0x3f) & ~0x1f; // @9b5e38 0x20 + align32(code) → +0x0c
out->data_seg_len = (desc->data_seg_len + 3) & ~3;      // @9b5e41 align4(data p_memsz) → +0x10
*(u64*)&out->init = *(u64*)&pil[0x18];                  // @9b5e4e 8-B store {init,fini} → +0x14
out->start_sym    = *(u32*)&pil[0x10];                  // @9b5e57 pil.start_sym → +0x1c
```

```c
typedef struct ucpl_header_t {     /* 0x20 = 32 bytes, written to device offset 0 */
    char     magic[8];     /* +0x00  "UCPL " + 3 NUL  (qword 0x204c504355)        */
    uint32_t code_seg_len; /* +0x08  align4(code p_memsz)                          */
    uint32_t data_seg_off; /* +0x0c  0x20 + align32(code_seg_len)                  */
    uint32_t data_seg_len; /* +0x10  align4(data p_memsz)                          */
    uint32_t init_fn;      /* +0x14  pil.init  (low half of the 8-B store)         */
    uint32_t fini_fn;      /* +0x18  pil.fini  (high half of the 8-B store)        */
    uint32_t start_sym;    /* +0x1c  pil.start_sym (the device entry point)        */
} ucpl_header_t;           /* magic 0x204c504355; emitted once per artifact        */
```

> **QUIRK — the magic's trailing `0x20` byte *is* the code-segment device offset.**
> The magic qword `"UCPL \0\0\0"` has bytes `[0x04..0x07] = 20 00 00 00 =
> 0x00000020`. `ll_create` reads the code segment's device offset straight out of
> `OUT[0x04]` (`@0x9b1c95 mov 0x1c(%rsp),%edx`). So the **space character (0x20)**
> in `"UCPL "` doubles as the literal value `0x20` = the header size = the device
> offset of the first body segment. `prelink` never writes a separate
> `code_seg_off` field; it falls out of the magic. The on-device layout is:
>
> ```
> device[0x00 .. 0x20)              = the UCPL header
> device[0x20 .. 0x20+code_len)     = relocated CODE  (from scratch_code)
> device[data_seg_off .. +data_len) = relocated DATA  (from scratch_data)
> ```
>
> where `data_seg_off = 0x20 + align32(code_seg_len)` (compile-checked:
> code=0→0x20, code=0x21→0x60, code=0x100→0x120, code=0x1234→0x1260). `[HIGH·OBSERVED]`

> **NOTE — what the UCPL header deliberately does NOT carry.** No version field
> (the EI_DATA byte is the only version-like discriminator, §2.1); no checksum; no
> section/segment count (fixed 2 body segments, inferred from the three extent
> words); no per-section descriptor array, reloc-table pointer, or symbol/import
> table. All symbol/relocation work is flattened on the host (§3); the device
> needs only the entry/init/fini addresses and the two segment extents. This is
> the "prelinked" contract. `[HIGH·OBSERVED]`

---

## 4. The memhandle staging ABI — physically staging onto the device

The relocated image is staged onto device memory through the **polymorphic
memhandle vtable** owned by the context (`ctx + 0x08`). The default install is the
stub `plat_memhandle_dummy @0x9b8cf0` (errors on real work); a real platform
backend (HBM-backed, dataram-backed, host-pinned, or simulator) is attached by
the embedder via `nrtucode_context_set_memhandle_impl`. The "polymorphism" is
exactly the vtable swap: the same five entry points dispatch to whichever backend
is installed.

> **GOTCHA — this is a plain C function-pointer table, not a `_ZTV`.** The vtable
> is in `.data.rel.ro` with **`R_X86_64_RELATIVE`** relocs (one per populated
> slot). **Slot N = `plat_memhandle_dummy + 8*N`.** `readelf -rW` this session:
>
> ```
> 0x9b8cf0  R_X86_64_RELATIVE  → 0x9b1820   (slot +0x00  device_malloc)
> 0x9b8cf8  R_X86_64_RELATIVE  → 0x9b1850   (slot +0x08  device_free)
> 0x9b8d00  R_X86_64_RELATIVE  → 0x9b1860   (slot +0x10  read_memhandle)
> 0x9b8d08  R_X86_64_RELATIVE  → 0x9b1870   (slot +0x18  write_memhandle)
> 0x9b8d10  (no reloc; raw qword 0)         (slot +0x20  device_addr = NULL in stub)
> ```
>
> The dummy populates slots 0..3 and leaves slot 4 NULL. A **real** backend must
> additionally supply `device_addr` (called unconditionally in two host paths).
> Table size = 0x28 (5 slots). `[HIGH·OBSERVED]`

```c
typedef struct memhandle_vtbl {                              /* 0x28 = 5 slots */
  int      (*device_malloc)(void *ctx, /*A*/, /*B*/, void **out_handle); /* +0x00 */
  int      (*device_free)  (void *ctx, void *handle /*by value*/);       /* +0x08 */
  int      (*read)         (void *ctx, void *handle, uint64_t dev_off,
                            uint64_t len, void *host_dst);               /* +0x10 COPYOUT */
  int      (*write)        (void *ctx, void *handle, uint64_t dev_off,
                            uint64_t len, const void *host_src);         /* +0x18 COPYIN  */
  uint64_t (*device_addr)  (void *ctx, void *handle);  /* real impl only */ /* +0x20 */
} memhandle_vtbl;
extern const memhandle_vtbl plat_memhandle_dummy;   /* @0x9b8cf0; slot 4 NULL */
```

Every slot takes `ctx` as arg1 (so the backend can log through `ctx` and reach its
own state) and the region **handle by value** as arg2. The handle is an opaque
8-byte cookie; size/region tags live in the owner struct (`ll+0x18 prelinked_size`,
`core+0x40 log_size`), not in the handle. The dummy bodies prove the contract:

```c
// dummy_device_malloc @0x9b1820: *out = NULL; log("…memhandle platform implementation is
//                                 required but is not attached"); return 8;
// dummy_device_free    @0x9b1850: ret                  (no-op)
// dummy_read_memhandle @0x9b1860: mov $8,%eax ; ret    (errcode 8)
// dummy_write_memhandle@0x9b1870: mov $8,%eax ; ret    (errcode 8)
```

Error code `8` = "memhandle platform not attached" — an unconfigured context fails
loudly on every device op except `free` (silent no-op).

### 4.1 `dmem_alloc` / `device_malloc` (+0x00) — the staging buffer `[HIGH·OBSERVED]`

`device_malloc(ctx, A, B, out_handle)` returns 0/errcode and writes the new device
handle through `out`. `ll_create @0x9b1c4e` allocates the staging buffer:

```c
// nrtucode_ll_create @0x9b1c4e
edx = 0x1000000;                              // @9b1c52  16 MiB device buffer
rdi = ctx; rcx = &ll->device_memhandle;       // out → ll[+0x08]
(*ctx->memhandle->device_malloc)(...);        // @9b1c5d  call *(%rax)
```

> **GOTCHA — `device_malloc`'s (size, align) argument order is genuinely
> ambiguous.** Its two callers put the size in **different registers**:
> `ll_create` carries 16 MiB in `edx` (`@9b1c52`), while
> `core_enable_logs @0x9b0c41` carries the rounded log size in `esi` and `0x20`
> (alignment) in `edx`. The NULL-tolerant dummy reads neither, so the binary
> cannot break the tie. The contract is best stated as `device_malloc(ctx, A, B,
> out)` where the backend must treat the meaningful of `{A,B}` as the byte size.
> Both per-site values are `[HIGH·OBSERVED]`; the canonical slot-roles are `[MED]`.
> This `device_malloc` is the role-equivalent of the other host runtime's
> `dmem_alloc`; the two are independent realizations of the same staging boundary
> (different symbols, never cross-referenced — they reconcile by role only).

### 4.2 `dmem_buf_copyin` / `write_memhandle` (+0x18) — staging the image `[HIGH·OBSERVED]`

`ll_create @0x9b1c81..0x9b1cc0` issues exactly **three** `write_memhandle` calls —
the header first at device offset 0, then the two prelink-assigned body segments
(this is the `dmem_buf_copyin` host→device copy):

```c
// args: rdi=ctx, rsi=handle (by value), edx=dev_off, ecx=len, r8=host_src
write(ctx, handle, /*off*/0,         /*len*/0x20,     /*src*/&OUT);      // @9b1c81  UCPL header
write(ctx, handle, /*off*/OUT[0x04], /*len*/OUT[0x08],/*src*/scratch1);  // @9b1ca3  code seg
write(ctx, handle, /*off*/OUT[0x0c], /*len*/OUT[0x10],/*src*/scratch2);  // @9b1cc0  data seg
```

`OUT` is the UCPL header (`OUT[0x04] = 0x20` from the magic-space, §3.6;
`OUT[0x08] = code_seg_len`; `OUT[0x0c] = data_seg_off`; `OUT[0x10] = data_seg_len`).
Any write returning nonzero jumps to the device-free path (`@9b1cd8 call *0x8(%rax)`
= `device_free`) and propagates rc. After all three writes:

```c
// @9b1d0b  the 64 KiB cap
if (ll->prelinked_size >= 0x10001) {          // cmpq $0x10001,0x18(%rbx) ; jb OK
    log_error(ctx, "Prelinked library would be larger than the available buffer on device"); // .rodata 0x4d44
    /* device_free + free scratches + free ll */ return 7;
}
```

The 16 MiB device buffer is the allocation; **0x10000 = 64 KiB inclusive** is the
real ceiling on the staged image. The `read_memhandle` (+0x10) slot is the
*copyout* mirror (`read(ctx, handle, dev_off, len, host_dst)` — drains the device
log ring `head..tail` to host in `core_print_logs @0x9b0f08`); `device_addr`
(+0x20) resolves a handle to the absolute device address the firmware uses
(published into the device control block in `enable_logs`; consumed by
`ll_get_sequence_common` to compute the staged image's device base).

> **NOTE — args do not cross via the handle.** The memhandle staging delivers the
> CODE IMAGE + the control-block addresses; the per-call `at::Tensor`/scalar args
> are pulled *on-device* by `customop_next_*` once the kernel runs (a separate
> ABI). The result-reap for a kernel is not an image copyout — the image is code,
> not result data. `[HIGH cross-report]`

---

## 5. End-to-end reconstruction

```
INPUT   pi_library = Xtensa ELF32 ET_DYN split-load .so
          { PT_LOAD R+X (text), PT_LOAD R+W (data), [PT_LOAD -WX scratch],
            PT_DYNAMIC R+W with RELA + symtab/strtab/hash }

prelink @0x9b5d60 :
  poison scratch_code(32K) + scratch_data(8K) with 0xA5 ; zero the pil (0x40 B)
  │
  ├─ prelink_load_lib @0x9b5e70 :
  │    xtlib_verify_magic   →  \x7fELF + ELFCLASS32 + EI_DATA endian flag
  │    validate_dynamic_load→  R+X text / R+W data / [-WX scratch] / PT_DYNAMIC R+W
  │    find_align / align_ptr→  dst = align_ptr(cayman base, al) + (p_paddr & (al-1))
  │    get_dyn_info         →  pil { rela(scratch-aware @+0x20), rela_count=RELASZ/12,
  │                                  start_sym/init/fini, hash/symtab/strtab, align,
  │                                  scratch_section_found }      (d_tag jump table @0x9aaf88)
  │    xtlib_load_seg ×2    →  memcpy(p_filesz) + memset bss(p_memsz-p_filesz);
  │                            desc.{code,data}_seg_len = p_MEMSZ
  │    bounds check         →  (dst_off + p_memsz) > cayman[0x18]/[0x38] → rc13
  │
  ├─ prelink_relocate_lib @0x9b6160 :
  │    walk Elf32_Rela[rela_count] (stride 0xc) from pil.rela+8
  │    r_info > 0xff                          → rc5  (STN_UNDEF-only)
  │    type 0  NONE                           → skip
  │    type 5  R_XTENSA_32                    → *w = reloc_addr(*w + addend)  (aligned/shld)
  │    type 20..34 SLOT*_OP / 35..49 SLOT*_ALT→ relocate_op @0x9b6660
  │         reloc_addr @0x9b6130 : 2-way rebase + 0x400 scratch bias
  │         relocate_op          : narrow (and 0xff0000f7) | wide FLIX (5 SSE masks)
  │    → host scratch buffers now FULLY device-VA-resolved; ZERO residual relocs
  │
  └─ emit UCPL header (0x20 B) :
       magic "UCPL " (0x204c504355; space=0x20=code offset) | align4(code memsz) |
       0x20+align32(code) | align4(data memsz) | init/fini (8B) | start_sym

ll_create (memhandle vtable, ctx+0x08) :
   device_malloc(16 MiB) → handle ; 3× write_memhandle(header@0, code@0x20, data@data_off)
   64 KiB cap (cmpq 0x10001) ; device_addr(handle) → device base for the load sequence

DEVICE  xtlib_target_init_pi_library : runs init/fini, exposes start_sym. NO relocation.
```

The trio is stock `xt-libloader` (host-load split path), generation-invariant
except the `cayman` vs `sunda` region bases/sizes (and the core-config FLIX
templates). Used by device coretypes `{13, 21, 29}`; the host-only family
(coretype 6) skips prelink entirely (size stays 0, no `device_malloc`/`write`).

---

## Cross-references

* [runtime/ucode-relocation-consumer.md](ucode-relocation-consumer.md) — the
  on-device relocation/init consumer (DX-RT-12); shares the
  `prelink_relocate_lib @0x9b6160` / `relocate_op @0x9b6660` / `reloc_addr
  @0x9b6130` anchors and the device-blob reloc histogram that grounds the type-5
  = `R_XTENSA_32` naming (§3.1 CORRECTION).
* [firmware/pool/external-lib-loader.md](../firmware/pool/external-lib-loader.md)
  — the device-side external-library loader that runs the staged UCPL image.
* [runtime/object-model-graph.md](object-model-graph.md) — the
  `nrtucode_context_t` / `nrtucode_ll_t` object model that owns the memhandle
  vtable and the staged-library handle.
* `neff/neff-elf-relationship.md` *(stub — pending)* — the NEFF↔ELF packaging
  relationship for the `pi_library` blob.

---

## Confidence ledger

**HIGH · OBSERVED** (single-instruction / verbatim-string / reloc-anchored,
re-verified against the binary this session):

* the whole chain symbol map (`nm -n`), all addresses;
* the UCPL emit (`movabs $0x204c504355`, the align4/align32/align4 stores, the 8-B
  init/fini store, the start_sym store) — byte-exact;
* `xtlib_verify_magic` ELF/class/EI_DATA; `reloc_addr` `shl $0xa` + 2-way rebase;
* the R_XTENSA dispatch constants (`-0x14`/`-0x23`/`$0xf`/`$0xff`/`$0x5`), the ALT
  `shr $0x10`, the narrow set `and $0xff0000f7`, the wide length classes
  (`0x100000f`/`0x300000f`/`0xe`/`0xf`);
* `cayman_memory_bounds` (8 qwords) + `sunda_memory_bounds` (dormant); the five SSE
  field-clear masks @`cayman+0x40..+0x80`; the type-5 keep-masks @`0x9aaf70`;
* the memhandle vtable (4 `R_X86_64_RELATIVE` slots + NULL slot 4), the dummy
  bodies + errcode 8; the 3 `ll_create` `write_memhandle` calls; the 16 MiB
  `device_malloc`; the 64 KiB cap `cmpq $0x10001`;
* `nm | rg -c '_ZTV'` = 0 (no C++ vtables; slot N = symbol + 8*N).

**MED · INFERRED** — the UCPL `[0x14]/[0x1c]` field *naming* (init/fini/start_sym;
the stores are HIGH, the mapping onto `xtlib_pil_info` is inferred); the
FLIX-format → specific-Xtensa-operand semantics (bit-mechanics HIGH, the
slot→operand mapping inferred from the CONST16 model); the `device_malloc`
(size,align) canonical order; the by-role mapping of memhandle slots onto the
device-side allocator/copy/translate primitives (the platform backend is
embedder-supplied, not in this binary); the `sunda`-mode selection mechanism
(table present, unwired).

**CARRIED · SIBLING** — the on-device `xtlib_target_init_pi_library` (init + entry
expose, no relocation) is not in this binary; carried from the xtlib host/target
API contract and the committed consumer page.

> **INFERRED — v5 / Maverick interior.** This library is wired to the `cayman`
> (HW-decode) region table; `sunda` is present but dormant. Any claim about a
> v5/Maverick-interior region layout or selection switch is **INFERRED** — the
> shipped binary exercises only the cayman path, and no v5-specific bounds table
> is present here.
