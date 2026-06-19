# SUNDA arch5 EXTISA ELF (the standalone external-lib container)

This page **completes the SUNDA carves and the Part-6 image-carve lane**. It is the
SUNDA-specific deep dive that complements the census-level
[EXTISA Q7 SO-Blob Inventory + Blob→Getter Map](extisa-inventory.md): where the
inventory page maps the *entire* 13-image container corpus and the per-gen
`[JSON-manifest | ELF]` block layout, this page **carves and characterizes one
generation's delivery byte-for-byte** — the standalone runtime container
`libnrtucode_extisa.so`, the host getter stubs that resolve the SUNDA EXTISA out of
it, and the embedded **SUNDA arch5 EXTISA Q7 device ELF** (the monolithic
`all.stripped.so`).

SUNDA is the **v2 floor** — `arch_id 5` / `coretype 6`, NC-v2, the oldest GPSIMD
generation, **RELEASE-only**. Because SUNDA is v2, **everything below is
byte-grounded**: `arch_id 5` is **OBSERVED** (contrast the `arch_id 36` /
`coretype 37` MAVERICK inference wall — *not* this page). Carved ELF headers, section
tables, sha256s, demangled `.xt.prop` C++ symbols, the shipped `.json` manifest, and
the `kernel_info_table` opcode bytes are all **OBSERVED**. Tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

The single most important structural fact: **SUNDA delivers its EXTISA kernels in a
*separate* container `.so`, and *only* there.** Where CAYMAN/MARIANA/MARIANA_PLUS also
appear as defined in-library getters inside the host customop library
`libnrtucode_internal.so` (the *internal-twin* model documented on the committed
`cayman-*` / `mariana-*` / `maverick-*` pages), SUNDA's two EXTISA getters are
**`NOTYPE WEAK UND` placeholders** in `internal.so` with **no in-library body and the
blob bytes physically absent** — the kernels are resolved exclusively from the
standalone container. That weak-undef split is the structural signature of the
**v2-base external-lib model**, and proving it byte-by-byte is the spine of this page.

> **NOTE — two SUNDA EXTISA worlds, do not conflate them.** SUNDA appears *twice* in
> the toolchain: (a) the `libnrtucode.a` static archive holds **48 SUNDA members**
> (the per-engine NX + Q7 firmware getters, the *internal-twin*-present view); and (b)
> this page's **standalone EXTISA delivery** — the device ELF lives only in
> `libnrtucode_extisa.so`. The archive's 48 SUNDA members and this single external-lib
> ELF are *different deliveries* of SUNDA firmware. This page is about delivery (b).
> *[HIGH/OBSERVED — `ar t libnrtucode.a | rg -ci sunda` = 48; `… | rg -ci cayman` =
> 124; archive total 435 members.]*

---

## 1. Toolchain, targets, identity (re-validated this session)

Every offset/size/sha256 below is anchored to these exact binaries. Host-side
characterization (the container `.so` wrapper) uses **stock x86-64 `binutils`**
(`readelf`/`objdump`/`nm` + `python struct/hashlib`); the device-side Q7 ELF uses the
**Cadence Xtensa toolchain shipped inside the gpsimd-tools package**
(`xtensa-elf-{readelf,objdump,objcopy,c++filt,strings}`, `XTENSA_CORE=ncore2gp`,
ConfigName `Xm_ncore2gp`, uarch *Cairo*, Xtensa24 / RI-2022.9 / NX1.1.4, FLIX/VLIW 32 B).

| role | path (relative to `neuronx-gpsimd/`) | sha256 | conf |
|---|---|---|---|
| runtime container (13 sub-images, host resolver + EXTISA blobs) | `../neuronx-runtime/extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrtucode_extisa.so` | `dc00763dbdb27cb4…1c39159f` | HIGH/OBSERVED |
| host customop lib (the internal-twin view; SUNDA weak-undef here) | `extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/libnrtucode_internal.so` | `b7c67e898a116454…632fc329b` | HIGH/OBSERVED |
| static archive (48 SUNDA members; the 4th view) | `…/custom_op/c10/lib/libnrtucode.a` | — (435 members) | HIGH/OBSERVED |
| device disassembler / carver | `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-{readelf,objdump,objcopy,c++filt}` | — | HIGH/OBSERVED |

* **Container** `libnrtucode_extisa.so`: ELF64 x86-64 **`DYN`**, **STRIPPED**
  (`.symtab` gone), `SONAME "libnrtucode_extisa.so"`, **`NEEDED libc.so.6` (the only
  needed lib — a leaf, no `internal`/`ncfw` dependency)**, BuildID
  `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, **9,656,488 bytes**, 27 sections, 9
  program headers, `e_shoff = 9,654,760`, entry `0x0`. sha256 `dc00763d…` (matches the
  Part-6 inventory anchor; re-hashed this session). *[HIGH/OBSERVED]*

* **Device ELF carve (identity map):** the container's big read-only `.rodata` `LOAD`
  segment maps **file offset == VA**, so the carve is a flat slice:
  `blob = container[0x921660 : 0x921660 + 0xd308]`. The carved file is itself a
  complete ELF (magic `7f 45 4c 46`). The container holds **14 `\x7fELF` magics** —
  1 host wrapper + 13 embedded device ELFs; **SUNDA is the 13th**. *[HIGH/OBSERVED]*

> **GOTCHA — `.text`/`.rodata` are identity-mapped, `.data` is NOT.** Confirmed
> per-section on the container with host `readelf -SW`: `.text` VA `0x3180` == file
> offset `0x3180`, `.rodata` VA `0xb000` == file offset `0xb000` (so the `0x921660`
> blob carve is a direct slice), but `.data` VA `0x9350a0` ≠ file offset `0x9340a0`
> (Δ `0x1000`) and `.data.rel.ro` VA `0x9348a0` ≠ file offset `0x9338a0` (Δ `0x1000`).
> The per-gen lib tables (`sunda_libs` @VA `0x934b00`) live in the **`.data.rel.ro`**
> band — read them by VA, not by raw file offset. *[HIGH/OBSERVED]*

---

## 2. The container model — a full 4-gen runtime, not a blob bag

`libnrtucode_extisa.so` **is a complete, self-contained nrtucode host runtime** whose
data payload happens to be the per-generation EXTISA device ELFs. It is the
runtime-side twin of the customop `libnrtucode.so`: it **exports the full nrtucode
host API** — `nrtucode_get_ext_isa`, `nrtucode_get_num_ext_isa_libs`,
`nrtucode_get_memory_image`, `nrtucode_core_create/destroy/get_context`,
`nrtucode_ll_get_load_sequence`, `nrtucode_opset_add_instruction`,
`nrtucode_get_build_version`/`git_version`/`api_level`, … — **69 `FUNC` entries in
`.dynsym`** (`readelf --dyn-syms -W … | rg -c ' FUNC '` → **69**). *[HIGH/OBSERVED]*

### 2a. Program-header band layout (host `readelf -l`)

| LOAD | file off / size | flags | holds |
|---|---|---|---|
| `0x000000` / `0x002b28` | R | note / `.dynsym` / `.dynstr` / version / `.rela` |
| `0x003000` / `0x007f51` | R E | `.init` / `.plt` / **`.text`** / `.fini` — **the host resolver code** |
| `0x00b000` / `0x928008` (9.16 MB) | R | **`.rodata` — ALL 13 embedded device ELFs** (SUNDA @ `0x921660`) |
| `0x933888` / `0x001810` | RW | `.data.rel.ro` / `.dynamic` / `.got` / `.data` / `.bss` — **the per-gen lib tables** (`sunda_libs` @ `0x934b00`) |

The device-ELF corpus is the big R-only `.rodata` `LOAD` (identity-mapped); the getter
stubs and per-gen lib tables are the small RW band. *[HIGH/OBSERVED]*

> **NOTE — stripped container ⇒ getters recovered by reloc + disasm, not `nm`.** The
> container has no `.symtab`; the EXTISA getter stubs are **static functions with no
> symbol names**. They are recovered by (1) the `R_X86_64_RELATIVE` relocs that
> populate the per-gen lib tables, and (2) disassembling the stubs those relocs point
> at. Every getter address below is grounded that way, not by symbol. *[HIGH/OBSERVED]*

### 2b. The container's own `get_ext_isa` is the **4-gen jump-table** form

The container is a **4-generation** runtime — `{SUNDA, CAYMAN, MARIANA,
MARIANA_PLUS}`, **no MAVERICK** (that gen lives only inside `internal.so`). Two entry
points, both grounded by host `objdump`:

`nrtucode_get_num_ext_isa_libs @0x87b0` — the lib-count dispatcher:

```text
87b0: movq  $0x0,(%rsi)          ; *out_count = 0  (default)
87b7: mov   $0x1,%eax
87bc: add   $0xfffffffa,%edi      ; edi = coretype - 6     (0xfffffffa = -6)
87bf: cmp   $0x17,%edi            ; (coretype-6) > 23  ⇒  coretype > 29
87c2: ja    87e5                  ;   ⇒ out of range, return (no Maverick)
87c4: lea   0x9186ad(%rip),%rcx   ; jump table @VA 0x920e78
87cb: movslq (%rcx,%rdi,4),%rdx   ; table[coretype-6] is a self-relative i32
87cf: add   %rcx,%rdx
87d2: jmp   *%rdx                  ; → per-gen arm (SUNDA arm sets eax=1 lib)
```

`nrtucode_get_ext_isa @0x87a0` — hardcodes **lib index 0** and tail-calls the internal
resolver:

```text
87a0: xor   %ecx,%ecx             ; lib = 0   (SUNDA has exactly 1 ext-isa lib)
87a2: jmp   8660                  ; → get_ext_isa_internal
```

`cmp $0x17` (=23) bounds `coretype-6 ∈ [0,23]`, i.e. `coretype ∈ [6,29]` — SUNDA(6)
through MARIANA_PLUS(29). MAVERICK (`coretype 37`) falls out of range. This is
**identical in shape** to the front lib's resolver and **distinct from the 5-gen
bitmask twin**. *[HIGH/OBSERVED — see also `extisa-inventory.md` host-resolver section.]*

---

## 3. The embedded SUNDA device ELF (native `ncore2gp` characterization)

### 3a. ELF identity (`xtensa-elf-readelf -h`) — *the machine/EI proof*

```text
Class                  ELF32
Data                   2's complement, little endian
Type                   EXEC (Executable file)
Machine                Tensilica Xtensa Processor       (e_machine = 94 / EM_XTENSA)
Entry point address    0x010000c8
Flags                  0x300
Section headers        17  @ e_shoff 0xd060
Program headers        4
sha256 (carve)         444497066f5e1738e24d2db6a373f64e13da5625180a1bfcdf97f82f58ab84c0
```

`e_shoff 0xd060 + 17·40 (0x2a8) = 0xd308 == file size` — **exact, no trailing slack**;
the carve length is the ELF's own structural extent. *[HIGH/OBSERVED]*

### 3b. Section table (`xtensa-elf-readelf -SW`)

| # | name | VA | off | size | flags | note |
|---|---|---|---|---|---|---|
| 1 | `.text` | `0x01000000` | `0x000100` | `0xa9be` | AX al 32 | FLIX/VLIW Q7 code |
| 2 | `.eh_frame` | `0x0200000c` | `0x00ab0c` | `0x0004` | A | empty CIE terminator |
| 3 | `.ctors` | `0x02000010` | `0x00ab10` | `0x0044` | A | ~17 C++ static-init ptrs |
| 4 | `.dtors` | `0x02000054` | `0x00ab54` | `0x0008` | A | |
| 5 | `.data` | `0x02000080` | `0x00ab80` | `0x06dc` | WA al 64 | **single data band** |
| 6 | `kernel_info_table` | `0x02000760` | `0x00b260` | `0x0090` | WA al 8 | **18 entries** (§5) |
| 7 | `.bss` | `0x020007f0` | `0x00b2f0` | `0x0064` | WA NOBITS | |
| 8 | `.comment` | — | `0x00c8a4` | `0x0028` | MS | `XtensaTools-14.09 clang version 10.0.1` |
| 9–13 | `.xt.prop.<mangled kernel>` | — | `0x00c8cc`+ | — | — | **5 kernel-property sections** (§6) |
| 14 | `.dynamic` | `0x03000000` | `0x00b300` | `0x00a4` | WA | 15 entries |
| 15 | `.rela.got` | `0x030000c8` | `0x00b3c8` | `0x14dc` | A | **445 R_XTENSA relocs** |
| 16 | `.shstrtab` | — | `0x00ced8` | `0x0185` | — | |

> **QUIRK — the arch5 single-data-band layout.** SUNDA has **NO `.rodata` section** and
> **NO `.globstruct` section**. Contrast the arch3 CAYMAN EXTISA ELF (carved this
> session for comparison): CAYMAN has `.rodata` @`0x02000000` sz `0x1ec` *and*
> `.globstruct` @`0x02000408` sz `0x48`. SUNDA collapses everything into a **single
> `.data` band** @`0x02000080`. This is the structural arch5 signature: the oldest
> gen's compiler/link layout merged const + global-struct + data into one writable
> band. *[HIGH/OBSERVED]*

### 3c. Program headers (`xtensa-elf-readelf -lW`) — *the three VA windows*

| LOAD | file off | VA | FileSiz | MemSiz | flg | window |
|---|---|---|---|---|---|---|
| 0 | `0x000100` | `0x01000000` | `0xa9be` | `0xa9be` | **R E** | code window `0x010xxxxx` |
| 1 | `0x00ab0c` | `0x0200000c` | `0x07e4` | `0x0848` | **RWE** | data window `0x020xxxxx` (`.eh_frame`…`.bss`) |
| 2 | `0x00b300` | `0x03000000` | `0x15a4` | `0x15a4` | **WE** | dyn window `0x030xxxxx` (`.dynamic`+`.rela.got`) |
| DYNAMIC | `0x00b300` | `0x03000000` | `0xa4` | `0xa4` | RW | (points into window 2) |

Three device VA windows: **code @ `0x010xxxxx`, data @ `0x020xxxxx`, dyn @
`0x030xxxxx`**. *[HIGH/OBSERVED]*

> **GOTCHA — the EXTISA ELF does NOT use the base-firmware IRAM@0x0 / DRAM@0x80000 flat
> model.** The committed base-image Q7 firmware getters (e.g. `cayman-act.md`,
> `cayman-pe.md`, `maverick-pe.md`) frame the device firmware as **IRAM at device VA
> `0x0`** and **DRAM at device VA `0x80000`** (so *DRAM string file-offset = device VA
> − `0x80000`*; see [memory-model.md](../topics/memory-model.md)). That flat
> two-region model is the **base** Q7 image's convention. The **EXTISA device ELF is a
> different object**: a relocatable `ET_EXEC` with the **`0x01000000` / `0x02000000` /
> `0x03000000` PIC three-window** layout above and a `.dynamic`+`.rela.got` tail. The
> EXTISA image is **staged into the base firmware's address space at load time**
> (§7), where the host prelink resolves its `const16`-loaded VAs against the chosen
> device load base — it is *not* loaded at its file VAs verbatim. Do not apply the
> `0x80000` DRAM offset to the EXTISA ELF's data band; that subtraction is a base-image
> rule only. *[HIGH/OBSERVED for both VA models; the staging-into-base-space relation
> INFERRED-HIGH, carried from the loader path §7.]*

> **NOTE — the data `LOAD` is `RWE` (executable data).** Window 1 (`.eh_frame` …
> `kernel_info_table` … `.bss`) is **read+write+execute**. The GPSIMD vector-table /
> kernel arg-block convention places executable data (and the dispatch table the
> on-chip loader scans) in the same band — the device has no W^X separation here.
> *[HIGH/OBSERVED]*

### 3d. The PIC tail — `.dynamic` (15) + `.rela.got` (445 R_XTENSA)

`.dynamic` (15 entries): `INIT 0x10000c0`, `FINI 0x100a9b0`, `HASH 0x30000a4`,
`STRTAB 0x30000c4`, `SYMTAB 0x30000b4`, `STRSZ 1`, `SYMENT 16`, `DEBUG 0`,
`RELA 0x30000c8`, **`RELASZ 5340`** (`= 445 · 12`), `RELAENT 12`, `PLTGOT 0x30015a4`,
two processor-specific tags (`0x70000000` → `0x30015a8`, `0x70000001` → `0`), `NULL`.
*[HIGH/OBSERVED]*

`.rela.got` holds **445 `R_XTENSA` relocations** (`xtensa-elf-readelf -rW … | rg -c
R_XTENSA` → **445**), emitted as `SLOT0_ALT` / `SLOT0_OP` **pairs** that fix up
`const16`-loaded VAs. Sample first pair:

```text
0100001b  R_XTENSA_SLOT0_ALT   → 0x0200009c
0100001e  R_XTENSA_SLOT0_OP    → 0x0200009c     ; a const16-pair loading .data addr 0x0200009c into code
```

This is **larger than the arch3 CAYMAN EXTISA** ELF's **240 relocs / `0xb40`**
(`RELASZ 2880`), because SUNDA_0 is the **largest single EXTISA image** (`0xd308` vs
CAYMAN `0xa260`) and uses more VA fixups. The host prelink applies these at
device-staging time (§7). *[HIGH/OBSERVED for the table; the prelink-applies-it
semantics INFERRED-HIGH — §7.]*

### 3e. `.text` is real FLIX/VLIW Q7 GPSIMD code

Binary-FLIX decode (`xtensa-elf-objcopy -O binary --only-section=.text` →
`xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x01000000`): **2428 FLIX
bundles**, **81 `entry` prologues**, **98 `retw`**, real windowed-ABI + IVP vector
code. The entry `@0x010000c8` decodes to a genuine startup:

```text
010000c8: entry  a1, 32
010000cb: { const16 a5, 0x200 ; l32i a3, a2, 0 ; movi a4, 36 ; movi a10, 2 }
010000db: const16 a5, 0x7f8
010000de: l32i.n a6, a2, 12
```

`.ctors` holds ~17 C++ static-init pointers (`0xffffffff` sentinel head, NUL-terminated
tail: `0x1000778`, `0x1001934`, `0x10026d8`, …). *[HIGH/OBSERVED]*

---

## 4. How the container resolves the SUNDA EXTISA (the self-resolution proof)

The SUNDA arm of `get_ext_isa_internal @0x8660` (reached via the
`get_ext_isa @0x87a0` → `lib=0` tail-call) is a jump table @VA `0x920e18` indexed by
`(coretype-6)`. **`idx 0` (ct 6) → the SUNDA handler @0x8725**:

```text
8729: lea  0x92c3d0(%rip),%rcx   # sunda_libs  @VA 0x934b00
8730: jmp  8757                  ; common tail: rcx += lib·16 (lib=0 ⇒ no add)
  …
875a: mov   (%rcx),%rdx          ; rdx = SO_get   (sunda_libs[0].so_get)
8767: mov   0x8(%rcx),%r14       ; r14 = JSON_get (sunda_libs[0].json_get)
8770: lea   0x8(%rbx),%rsi       ; &out.so_len
8774: mov   %rbx,%rdi            ;  out (= &out.so_ptr)
8777: call  *%rdx                ; SO_get(&out.so_ptr, &out.so_len)
8779: lea   0x10(%rbx),%rdi      ; &out.json_ptr
8784: call  *%r14               ; JSON_get(&out.json_ptr, &out.json_len)
```

**Out-struct (0x20 B):** `{ so_ptr@+0; so_len@+8; json_ptr@+0x10; json_len@+0x18 }`.
*[HIGH/OBSERVED]*

`sunda_libs @VA 0x934b00` is a real, defined table — its two slots are populated by
`R_X86_64_RELATIVE` relocs (host `readelf -rW`):

```text
0x934b00  R_X86_64_RELATIVE  → 0xaf10   (SO_get stub)
0x934b08  R_X86_64_RELATIVE  → 0xaef0   (JSON_get stub)
```

Disassembling those stubs (host `objdump`) closes the loop **exactly onto the carve**:

```text
SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get @0xaf10:
  af10: endbr64
  af14: lea   0x916745(%rip),%rax   # 0x921660   ← the device-ELF blob pointer
  af1b: mov   %rax,(%rdi)
  af1e: movq  $0xd308,(%rsi)         ;             ← size = 0xd308
  af25: ret

SUNDA_Q7_POOL_RELEASE_EXTISA_0_JSON_get @0xaef0:
  aef0: endbr64
  aef4: lea   0x9160a5(%rip),%rax   # 0x920fa0   ← the REAL manifest pointer
  aefb: mov   %rax,(%rdi)
  aefe: movq  $0x6c0,(%rsi)          ;             ← size = 0x6c0 (NOT a 0x20 dummy)
  af05: ret
```

The container **self-resolves the SUNDA EXTISA to precisely the carved blob+JSON**:
`SO @0x921660 / 0xd308` (sha256 `444497066f5e1738…`), `JSON @0x920fa0 / 0x6c0`
(sha256 `d282bd4f37e71d87…`). Decisive. *[HIGH/OBSERVED]*

> **QUIRK — SUNDA's JSON is REAL; CAYMAN's is a decoy.** In the *same* container,
> `cayman_libs` resolves `CAYMAN_0_JSON_get @0x5300` to `lea 0x5b0ca0 ; movq $0x20` —
> the inert `{"dummy_message":"hello world"}` placeholder. SUNDA's `JSON_get` returns
> `0x6c0` — the genuine 17-function manifest. SUNDA is the **genuine-JSON exception**
> of the container (see `extisa-inventory.md` JSON-decoy verdict). Both getters are
> `endbr64`-prefixed (CET / IBT landing pads). *[HIGH/OBSERVED]*

### 4a. Pseudocode — the host EXTISA resolver path (real symbol names)

```c
/* nrtucode_get_ext_isa(coretype, flavor, OUT* out)  @0x87a0 in libnrtucode_extisa.so
 * Returns the chosen EXTISA device ELF + its JSON manifest as a (ptr,len) quad.
 * The public entry hardcodes lib index 0; SUNDA has exactly one ext-isa lib. */
typedef struct { const void *so_ptr; size_t so_len;       /* the Q7 device ELF   */
                 const void *json_ptr; size_t json_len; } extisa_out_t; /* 0x20 B */

typedef struct { void (*so_get)(const void**, size_t*);    /* SUNDA: 0xaf10 */
                 void (*json_get)(const void**, size_t*);  /* SUNDA: 0xaef0 */ } gen_lib_t;

int nrtucode_get_ext_isa(unsigned coretype, const char *flavor, extisa_out_t *out) {
    return get_ext_isa_internal(coretype, flavor, out, /*lib=*/0);  /* xor %ecx,%ecx */
}

int get_ext_isa_internal(unsigned coretype, const char *flavor,
                         extisa_out_t *out, unsigned lib) {
    validate_flavor(flavor);                 /* getenv NEURON_UCODE_FLAVOR + strcmp */
    unsigned idx = coretype - 6;             /* add $0xfffffffa,%edi  (= -6)        */
    if (idx > 0x17) return -1;               /* cmp $0x17 ⇒ coretype∈[6,29]; no Maverick */

    /* jump table @VA 0x920e18; idx 0 (ct6) → SUNDA arm: */
    gen_lib_t *libs = sunda_libs;            /* lea @VA 0x934b00 (.data.rel.ro)     */
    gen_lib_t *L = &libs[lib];               /* lib*16; SUNDA lib==0                 */

    L->so_get(&out->so_ptr, &out->so_len);   /* → 0x921660 / 0xd308   (device ELF)  */
    L->json_get(&out->json_ptr, &out->json_len); /* → 0x920fa0 / 0x6c0 (REAL JSON)  */
    return 0;
}
```

The on-disk `sunda_libs` slots are 0-valued until the dynamic loader applies the two
`R_X86_64_RELATIVE` relocs at map time; the `endbr64` prologues on `0xaf10` / `0xaef0`
make them valid indirect-branch targets. *[HIGH/OBSERVED for the control flow + reloc;
the field names are reconstructed from the access offsets.]*

---

## 5. The 18-entry `kernel_info_table` — the authoritative SUNDA EXTISA kernel set

Section [6] `kernel_info_table @VA 0x02000760` (file off `0xb260`, size `0x90` = 18
fixed 8-byte records, **no header, no in-band terminator** — the same packed layout
the committed [kernel-info-table.md](../firmware/pool/kernel-info-table.md) documents).
**Record = `{ +0x00 [0x00] [0x00] [spec] [opcode] | +0x04 funcVA (LE u32) }`**;
equivalently the key bytes form `LE u32 = opcode<<24 \| spec<<16` (with `spec = 0`
throughout), followed by the LE funcVA. Read byte-exact this session:

| idx | opcode (dec/hex) | spec | funcVA | kernel (from the SUNDA JSON name map) |
|---|---|---|---|---|
| 0 | 231 / 0xe7 | 0 | `0x01000748` | `pool_indirect_copy` |
| 1 | 116 / 0x74 | 0 | `0x01001904` | `pool_tensor_scalar_addr` |
| 2 | 103 / 0x67 | 0 | `0x010023c4` | `pool_pool_buffer_load` |
| 3 | **104 / 0x68** | 0 | **`0x01002590`** | **`pool_gather`** (`ic_util::send_gather_request`) |
| 4 | 70 / 0x46 | 0 | `0x01002700` | `pool_copy` |
| 5 | 71 / 0x47 | 0 | `0x01002844` | `pool_cast` |
| 6 | 184 / 0xb8 | 0 | `0x01002f80` | `dma_memcopy` |
| 7 | 187 / 0xbb | 0 | `0x0100474c` | `dma_memcopy_indirect` |
| 8 | 126 / 0x7e | 0 | `0x01005e80` | `pool_iota` (`iota_kernel`) |
| 9 | 65 / 0x41 | 0 | `0x01006398` | `pool_tensor_tensor_arith_op` |
| 10 | 124 / 0x7c | 0 | `0x01007d30` | `pool_cross_lane_reduce_arith` |
| 11 | 125 / 0x7d | 0 | `0x01007d48` | `pool_cross_lane_reduce_bitvec` |
| 12 | 73 / 0x49 | 0 | `0x01007fc4` | `pool_memset` |
| 13 | 122 / 0x7a | 0 | `0x010084dc` | `pool_load_pool_argument` |
| 14 | 121 / 0x79 | 0 | `0x01008520` | `pool_embedding_update` |
| 15 | 67 / 0x43 | 0 | `0x0100a0d8` | `pool_tensor_scalar_arith_op` |
| 16 | **68 / 0x44** | 0 | **`0x0100a0d8`** | `pool_tensor_scalar_arith` (**op68 alias — shares op67's funcVA**) |
| 17 | 146 / 0x92 | 0 | `0x0100a118` | `pool_tensor_scalar_affine_select` |

**Verification:** all 18 funcVAs lie in `.text` `[0x01000000, 0x0100a9be)` (max
`0x0100a118` < `.text` end); **NO `0xf0` (ExtendedInst) row**; **all `spec = 0`**.
*[HIGH/OBSERVED — read directly from `blob[0xb260:0xb260+0x90]`.]*

> **QUIRK — op68 aliases op67 (the lone SO-only opcode).** Index 16 (`opcode 68`) and
> index 15 (`opcode 67`) both dispatch to `funcVA 0x0100a0d8`. The JSON manifest omits
> opcode 68 — it carries the same `pool_tensor_scalar_arith` body as op67, so the SO
> table has **18 rows where the JSON has 17 functions**. The JSON op-set
> `{65,67,70,71,73,103,104,116,121,122,124,125,126,146,184,187,231}` is a **strict
> subset** of the SO op-set; the only difference is `{68}`. *[HIGH/OBSERVED]*

> **QUIRK — no two-level `0xf0` escape on SUNDA.** Every row has `spec = 0`. The arch3+
> CAYMAN EXTISA table uses 5 `0xf0` *ExtendedInst* spec rows for a two-level dispatch
> bridge; SUNDA's is a **flat single-level** table — opcode → funcVA, no escape. This
> is part of the arch5 monolithic-baseline character (§8). *[HIGH/OBSERVED]*

The on-device dispatch (the shell's `dispatch_wrapper`, documented at
[external-lib-loader.md](../firmware/pool/external-lib-loader.md)) builds the packed
`(opcode<<24 \| spec<<16)` key, linear-scans the 18 rows, and `callx8`s the matching
`funcVA`.

### 5a. The real JSON manifest (17 functions) ⊆ the SO

The carved JSON (`0x6c0`, sha256 `d282bd4f37e71d87…`) parses to valid JSON:
`"library": "all.stripped.so"`, `"ulib_to_ucode_version": "1.21.1.0"`, **17
functions** as `{name, opcode}` pairs. It is a faithful (slightly abridged) descriptor
of the SO — genuine metadata, not a decoy. *[HIGH/OBSERVED — parsed this session.]*

---

## 6. Kernel naming — `.xt.prop` demangled C++ symbol → opcode (the worked example)

The device ELF carries its own **kernel-name ground truth** in five
`.xt.prop.<mangled-C++-symbol>` sections (Xtensa property sections, one per compiled
kernel translation unit). These are the **arch5-new kernels** — the additions over the
CAYMAN EXTISA roster — and their *section names* are the Itanium-mangled C++ symbols.
Demangled with `xtensa-elf-c++filt`:

| `.xt.prop` mangled section name | demangled C++ symbol | binds to |
|---|---|---|
| `_ZN7ic_util19send_gather_requestE20NEURON_ISA_TPB_ADDR4Ptj20NEURON_ISA_TPB_DTYPEjb` | `ic_util::send_gather_request(NEURON_ISA_TPB_ADDR4, unsigned short*, unsigned int, NEURON_ISA_TPB_DTYPE, unsigned int, bool)` | `pool_gather`, op 104 |
| `_Z11iota_kernelILb1EEvv` | `void iota_kernel<true>()` | `pool_iota`, op 126 |
| `_Z11iota_kernelILb0EEvv` | `void iota_kernel<false>()` | `pool_iota`, op 126 |
| `_Z19tensor_tensor_arithj21NEURON_ISA_TPB_ALU_OPj` | `tensor_tensor_arith(unsigned int, NEURON_ISA_TPB_ALU_OP, unsigned int)` | `pool_tensor_tensor_arith_op`, op 65 |
| `_Z16embedding_updatePN20embedding_update_lib17embed_update_infoEt` | `embedding_update(embedding_update_lib::embed_update_info*, unsigned short)` | `pool_embedding_update`, op 121 |

### The name-binding mechanism (the core of this page)

A demangled `.xt.prop` symbol does **not** directly carry an opcode — the binding is a
**three-hop join across three independent tables baked into the same delivery**, and
all three are binary-derived:

```text
  .xt.prop section name           SUNDA JSON manifest          kernel_info_table
  (compiler symbol, the           (functions[].name →          (the SO's opcode →
   human-readable kernel)          opcode, the API name)        funcVA dispatch row)
  ─────────────────────────       ───────────────────────      ──────────────────────
  ic_util::send_gather_request ─►  "pool_gather", opcode 104 ─► opcode 104 → 0x01002590
```

**Worked example — `pool_gather` end to end:**

1. **`.xt.prop` (device ELF, OBSERVED):** section [9] name
   `_ZN7ic_util19send_gather_requestE…` → `xtensa-elf-c++filt` →
   `ic_util::send_gather_request(NEURON_ISA_TPB_ADDR4, ushort*, uint,
   NEURON_ISA_TPB_DTYPE, uint, bool)` — the kernel's *source-level identity*.
2. **JSON manifest (device-ELF sidecar, OBSERVED):** the `functions[]` entry
   `{ "name": "pool_gather", "opcode": 104 }` — binds the *API name* to the *opcode*.
   (The JSON uses the API name `pool_gather`, not the C++ symbol; the human
   correspondence `send_gather_request ↔ pool_gather` is the gather kernel's role.)
3. **`kernel_info_table` (device ELF, OBSERVED):** the row keyed `opcode 104, spec 0`
   carries `funcVA 0x01002590` — and `0x01002590` is a dense FLIX bundle in `.text`,
   the gather kernel's entry. *[HIGH/OBSERVED]*

So: **demangled symbol `ic_util::send_gather_request` → JSON `pool_gather` → opcode
104 → `kernel_info_table` funcVA `0x01002590` → engine = POOL (`engine_idx 2`).** That
join — `.xt.prop` ⨝ JSON ⨝ `kernel_info_table` — is exactly how a reimplementer maps a
host-issued POOL opcode to a named device kernel without any external source. The same
join holds for `iota_kernel` → `pool_iota` → op 126 → `0x01005e80`, `embedding_update`
→ `pool_embedding_update` → op 121 → `0x01008520`, and `tensor_tensor_arith` →
`pool_tensor_tensor_arith_op` → op 65 → `0x01006398`.

> **NOTE — these are the SUNDA-new kernels.** `gather` / `iota` / `embedding_update`
> are kernels the EXTISA inventory flagged as *not present in CAYMAN's roster* — they
> are the arch5 additions. The `.xt.prop` sections are the device-byte corroboration:
> only these five new kernels ship their own property sections in this ELF.
> *[HIGH/OBSERVED]*

> **NOTE — absence scans at the device byte level.** `xtensa-elf-strings` of the SUNDA
> device ELF returns **0 hits** for: RNG (`xorwow`/`lfsr`/`rand`/`rng`); dequant / CPTC
> / MX (`dequant`/`cptc`/`proc_*bit`/`_mx`/`mxtensor`); SB2SB / collective /
> `sequence_bound` / `nonzero` / `gather_xpose`; `extended`/`ExtendedInst`/`.dispatch`/
> `sort`. SUNDA's EXTISA is the **minimal POOL kernel set** — no RNG, no dequant, no MX,
> no `0xf0` two-level dispatch, no sort. This is the image-level corroboration of the
> v2-floor finding on the EXTISA firmware itself. *[HIGH/OBSERVED]*

---

## 7. The load path — shell → `get_ext_isa` → prelink → on-device dispatch

The full SUNDA EXTISA load chain, anchored to the RT-loader linkage:

1. **SHELL.** The SUNDA `Q7_POOL` base IRAM is a **17 KB external-lib-loading shell**
   (vs CAYMAN's ~90 KB) — its kernels are **not baked into IRAM**; its DEBUG DRAM
   carries the loader self-naming strings (`load_external_libraries_impl`,
   `external_lib_loader.hpp`, `call_start_symbol`, `xtensa_unload`,
   `load_from_nx_addr`, `.kernel_info_table`, `dispatch_wrapper.hpp`). The kernels
   **must** be staged from the container at runtime. *[CARRIED — sunda-pool.md /
   external-lib-loader.md.]*
2. **HOST RESOLVE.** The host driver (NRT) calls
   `nrtucode_get_ext_isa(coretype=6, flavor, out)`; the public entry hardcodes `lib=0`.
   In the standalone container that runs `get_ext_isa @0x87a0` → internal `@0x8660` →
   the SUNDA handler `@0x8725` → `sunda_libs[0]` → `SO_get @0xaf10` (`0x921660`,
   `0xd308`) + `JSON_get @0xaef0` (`0x920fa0`, `0x6c0`), filling the
   `{so_ptr, so_len, json_ptr, json_len}` out-struct (§4). *[HIGH/OBSERVED §4.]*
   (The customop `internal.so`'s SUNDA getters are weak-undef — they bind to **this
   container's** defined stubs at process-load time; §8.)
3. **LIB SELECTION.** `nrtucode_ll_get_libraries_from_opcodes` /
   `opset_get_library_index` for SUNDA ct6 **always return lib 0** — SUNDA has exactly
   one ext-isa lib, no CPTC/lib-3 path (SUNDA has no CPTC). `ll_create` calls
   `get_ext_isa_internal` with the chosen lib, then prelinks + device-stages the image.
   *[CARRIED — RT-loader linkage.]*
4. **PRELINK / STAGE.** The host prelink applies the device ELF's **445-entry
   `.rela.got`** (the `const16`-VA fixups, §3d) against the chosen device load base,
   then copies the fixed-up image to device memory via the memhandle (`device_malloc`/
   write). The SUNDA device ELF is `ET_EXEC` with the `.dynamic`+`.rela.got` PIC tail
   **precisely so this relocation step is possible**. *[HIGH for the reloc table
   presence; the prelink-applies-it semantics INFERRED-HIGH from the `.rela.got` +
   RT-loader prelink path.]*
5. **DEVICE DISPATCH.** On-device, the shell's `dispatch_wrapper` walks the staged
   ELF's `kernel_info_table` (§5): for an incoming opcode it linear-scans the 18 rows
   for the matching `(opcode<<24 \| spec<<16)` key (`spec` always 0 — no `0xf0` escape)
   and `callx8`s the funcVA. The boundary vs the **base-region** resolver:
   `nrtucode_get_memory_image` resolves the *base* IRAM/DRAM/SRAM/EXTRAM via
   `image_list`; `get_ext_isa` is the **separate EXTISA-lane resolver** over the per-gen
   lib tables. EXTISA is its own region, surfaced *only* via `get_ext_isa`. *[HIGH.]*

> The NEFF↔ELF relationship — how this staged device ELF relates to a deployed NEFF's
> embedded uOps and the runtime's overall image graph — is the subject of a **Part 11
> page** (`neff/neff-elf-relationship.md`), not yet authored. Treat this as a forward
> reference; the EXTISA delivery characterized here is the device-image input to that
> staging.

---

## 8. EXTISA vs internal-twin — the v2-base external-lib model

### 8a. The weak-undef cross-check (SUNDA lives ONLY in the container)

`readelf --syms libnrtucode_internal.so` for the SUNDA EXTISA getters:

```text
  21: 0x0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get
  22: 0x0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_JSON_get
1347: 0x0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get
1348: 0x0  0  NOTYPE  WEAK  UND  SUNDA_Q7_POOL_RELEASE_EXTISA_0_JSON_get
```

Both getters are **`NOTYPE WEAK UND`, value 0** — link-time placeholders with **no
in-library body**. A 64-byte-head search confirms the SUNDA device-ELF blob and its
real JSON are **NOT PRESENT anywhere in `internal.so`** (`internal.so` has 17 `\x7fELF`
magics — 1 host + the 4 internal-only MAVERICK device ELFs + others — **none** the
SUNDA blob). So the host customop lib carries only the **weak reference**; SUNDA's
kernels are resolved **exclusively** from `libnrtucode_extisa.so` (where the same two
getters are the **defined** `0xaf10` / `0xaef0` stubs, §4). *[HIGH/OBSERVED — both
checks run this session.]*

### 8b. The standalone-vs-internal-twin split, per generation

| generation | container blob? | internal.so getter | model |
|---|---|---|---|
| **SUNDA** (arch5, ct6) | **yes** (`0x921660`) | **`NOTYPE WEAK UND`, no body, blob absent** | **standalone-ONLY** (external-lib) |
| CAYMAN (arch3, ct13) | yes | defined in-library getter | BOTH (container + internal-twin) |
| MARIANA | yes | defined in-library getter | BOTH |
| MARIANA_PLUS | yes (ct29) | defined in-library getter | BOTH |
| MAVERICK (arch36, ct37) | **no** (container is 4-gen, ct≤29) | defined; 4 device ELFs live only here | **internal-twin-ONLY** |

The container itself is a **4-gen** runtime (`cmp $0x17`, ct ≤ 29, no Maverick); it is
the shipped external-lib provider for SUNDA … MARIANA_PLUS. *[HIGH/OBSERVED §2b/§8a.]*

### 8c. The arch5 device-ELF deltas (SUNDA vs CAYMAN, carved side by side)

| axis | **SUNDA arch5 EXTISA_0** | **CAYMAN arch3 EXTISA_0** |
|---|---|---|
| etype / machine | `EXEC` / `EM_XTENSA` | `EXEC` / `EM_XTENSA` |
| entry | `0x010000c8` | `0x01005610` |
| `.rodata` section | **ABSENT** | PRESENT (`0x02000000` sz `0x1ec`) |
| `.globstruct` section | **ABSENT** | PRESENT (`0x02000408` sz `0x48`) |
| data layout | **single `.data` band** | `.rodata` + `.data` + `.globstruct` |
| `kernel_info_table` | `0x02000760` / **18 entries** | `0x02000380` / 17 entries |
| `0xf0` ExtendedInst rows | **0 (no escape)** | 5 spec rows (the bridge) |
| `.dynamic`+`.rela.got` | PRESENT (**445 relocs / `0x14dc`**) | PRESENT (240 relocs / `0xb40`) |
| image size | **`0xd308` (largest single)** | `0xa260` |
| toolchain (`.comment`) | `XtensaTools-14.09 clang-10` | `XtensaTools-14.09 clang-10` |
| JSON sidecar | **REAL `0x6c0` (17-func)** | DUMMY `0x20` (decoy) |
| multi-lib split | **1 lib** (`all.stripped.so`) | 4 libs (17/1/2/9 split) |

*[HIGH/OBSERVED — both ELFs carved and `xtensa-elf-readelf`'d this session.]*

### 8d. Why SUNDA uses a separate lib (the synthesis)

**v2 SUNDA:** the external-lib model is the **base** — a 17 KB IRAM shell **plus a
separate container `.so`** (1 lib, monolithic 18-entry flat table, no `0xf0`, real
JSON). The host customop lib only **weak-references** it; the kernels are absent from
`internal.so`. What CAYMAN later factored into a separate DKL variant is the **default
on SUNDA**.

**v3 CAYMAN+:** the EXTISA is embedded **both** in the container **and** as defined
in-library getters (the internal-twin); the `0xf0` ExtendedInst dual-dispatch + the
4-lib split + the dummy-JSON decoy arrive; the base IRAM grows to ~90 KB (kernels
partly in-IRAM). SUNDA is the floor the external-lib mechanism originates from.

*[HIGH for the per-gen facts OBSERVED; the chronological transition framing
INFERRED-HIGH — every constituent fact is observed, the "v2-base default" synthesis is
carried from the 17 KB-shell + loader-string finding.]*

---

## Cross-references

* [EXTISA Q7 SO-Blob Inventory + Blob→Getter Map](extisa-inventory.md) — the
  census-level container corpus, the per-gen `[JSON | ELF]` block layout, and the
  JSON-decoy verdict this page deep-dives for SUNDA.
* [SUNDA × POOL image (the v2 baseline)](sunda-pool.md) — the 17 KB external-lib shell,
  the dual-core SEQ/Q7 structure, and the base `kernel_info_table` mechanics.
* [SUNDA × SP + remaining NX engines](sunda-sp-remaining.md) — the rest of the SUNDA
  engine set (SP / ACT / DVE / PE) and the base-subtraction dispatch.
* [kernel_info_table byte layout](../firmware/pool/kernel-info-table.md) and
  [external-lib loader / `dispatch_wrapper`](../firmware/pool/external-lib-loader.md) —
  the packed-key record format and the on-device consumer.
* [Memory model](../topics/memory-model.md) — the base-firmware IRAM@0x0 / DRAM@0x80000
  framing the EXTISA ELF's `0x01/0x02/0x03` windows are contrasted against.

The host ext-isa getter symbol that resolves the SUNDA blob in the standalone container
is the static stub at `0xaf10` (`SO_get`), exported to the wider runtime via
`nrtucode_get_ext_isa @0x87a0` (the `coretype 6` arm). In the customop
`libnrtucode_internal.so` the corresponding symbol is
`SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get` (`NOTYPE WEAK UND`, bound at load time to this
container's defined stub).
