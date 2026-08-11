# Relocation / Patch Subsystem + Weight Layout

A NEFF carries each engine's program as a stream of fixed **64-byte sequencer
slots** (see [seq-microcode.md](./seq-microcode.md)) compiled against a *device-IP*
address space. The runtime's own kbin-construction passes expand the compiler's
pseudo-instructions into the final device image, and as they do so they record a
per-engine list of `kbin_patch_location_t` records describing where slots were
**INSERTed, MODIFYed, or DELETEd**. That list is the NEFF analog of ELF dynamic
relocations: it is consumed at fault/trace time to map a live on-chip program
counter back to the source `.asm` line.

Orthogonally, weight/constant tensors ride as **separate tar members** named by
the var table, become `MR_BUFFER` mem_refs, and are bulk-DMA-staged into
HBM(DRAM) / mapped into on-chip SBUF at load. That path is the NEFF analog of a
loader copying `.data`/`.rodata` into a process image.

This page documents both halves byte-exactly. The patches consumed here are
**built** by the assembly pipeline ([assembly-pipeline.md](./assembly-pipeline.md));
the mem_ref / `kbin_mr_type` values match [metaneff-io-abi.md](./metaneff-io-abi.md);
the 64-byte slot stride matches [seq-microcode.md](./seq-microcode.md). The
runtime `mem_ref` C++ object referenced below is field-mapped in
`appendix/struct-host-runtime-layouts.md`.

> **Evidence base.** All offsets/enums are from the IDA sidecars of
> `libnrt.so` v2.31.24.0 (`*_structures.json` / `*_enums.json`), decompiled
> bodies (`decompiled/*.c`), raw disassembly (`disasm/*.asm`), and the embedded
> NEFF sample carved at `.rodata` offset `0xC08220` (`tar/sg00/`). `.text` and
> `.rodata` have VMA == file offset. Tag legend: confidence **HIGH/MED/LOW** ×
> provenance **OBSERVED** (byte-exact in a sidecar/disasm/sample) /
> **INFERRED** (control/data-flow) / **CARRIED** (from a sibling page).

---

## 1. `kbin_patch_location_t` — byte-exact (16 B)

`[HIGH/OBSERVED]` — `structures.json` ordinal **8667**, `size = 16`:

| off  | size | field     | type                   | meaning |
|------|------|-----------|------------------------|---------|
| +0x0 | 4    | `offset`  | `uint32_t`             | byte offset of the patch site in the **translated** (kbin) instruction buffer |
| +0x4 | 4    | `count`   | `uint32_t`             | **slot** count (`byte_count >> 6`, i.e. number of 64-byte slots, **not** bytes) |
| +0x8 | 4    | `type`    | `kbin_patch_type_t`    | `INSERT=0` / `MODIFY=1` / `DELETE=2` |
| +0xC | 4    | `section` | `kbin_patch_section_t` | `PREAMBLE=0` / `POSTAMBLE=1` / `MAIN=2` / `FUNCTION=3` |

```c
enum kbin_patch_type_t    /* enums.json ord 8669 */
  { KBIN_PATCH_TYPE_INSERT = 0, KBIN_PATCH_TYPE_MODIFY = 1, KBIN_PATCH_TYPE_DELETE = 2 };

enum kbin_patch_section_t /* enums.json ord 8668 */
  { KBIN_PATCH_SECTION_PREAMBLE = 0, KBIN_PATCH_SECTION_POSTAMBLE = 1,
    KBIN_PATCH_SECTION_MAIN     = 2, KBIN_PATCH_SECTION_FUNCATION = 3 };
```

> **GOTCHA — the symbol is misspelled.** Section 3 is literally
> `KBIN_PATCH_SECTION_FUNCATION` in the binary's enum table
> (`enums.json` ord 8668). Reproduce the typo if you want symbol parity; the
> *value* is 3 either way. `[OBSERVED]`

> **NOTE — `count` is a slot count, not a byte count.** `kbin_patch_append`
> (§2) stores `byte_count >> 6` (a divide-by-64) into the field. A record
> `{offset=O, count=N, type=INSERT, section=MAIN}` means *"at translated byte
> `O`, `N` extra 64-byte slots exist beyond what the source stream had."* Every
> address step in the consumer (§4) is therefore `count << 6`. `[OBSERVED]`

### Per-engine and per-model containers

`[HIGH/OBSERVED]`

```c
struct kbin_eng_patch_t {           /* ord 8666, 16 B */
  int                     count;        /* +0x0  valid entries                     */
  int                     array_count;  /* +0x4  allocated capacity (grow-doubling)*/
  kbin_patch_location_t  *locations;    /* +0x8  calloc'd array                    */
};

struct kbin_patch_info_t {          /* ord 8577, 80 B */
  kbin_eng_patch_t eng_patch[5];        /* one per al_hal_tpb_eng_type             */
};
```

The 5 engines are indexed in `al_hal_tpb_eng_type_t` order
`{PE=0, ACT=1, POOL=2, DVE=3, SP=4}` (`enums.json`; `MAX_ENG = 5`). At runtime
`kbin_patch_info` lives at `model_t + 0x18A0` (`structures.json`:
`model_t.kbin_patch_info` is at byte offset **6304 = 0x18A0**). This is why the
section-dispatch consumer (§5) computes the table base as
`mod + (eng + 0x18A)*16` (`0x18A * 16 = 0x18A0`):

```
0x2fb3ad: lea  rax, [rcx + 18Ah]   ; rcx = eng
0x2fb3b5: shl  rax, 4              ; *16
0x2fb3ba: add  rax, mod            ; -> &mod->kbin_patch_info.eng_patch[eng]
```
`[OBSERVED — disasm kbin_patch_device_ip_to_neff_ip_0x2fb3a0.asm]`

---

## 2. Building the patch list (kbin construction)

The patch list is produced by libnrt's kbin builder while it lowers the
compiler's pseudo-instruction stream (the NEFF `.bin`) into the final per-engine
device image. The allocator/mutators live in `tdrv/kbin_patch.c`; the producers
are the `ib_*` / `translate_*` passes (`instruction_block_sunda.c` /
`instr_sunda.c`).

### 2.1 Allocators / mutators

`[HIGH/OBSERVED]` — `decompiled/kbin_patch_*.c`.

| symbol | addr | behaviour |
|--------|------|-----------|
| `kbin_patch_allocate(info, eng, n)` | `0x2fb000` | `eng_patch[eng].count = 0`; `locations = calloc(n, 16)`; `array_count = n`. The builder reserves **`0x400` (1024)** slots per engine up front (`ib_create_one_block` lines 325/337). |
| `kbin_patch_append(info, eng, offset, byte_count, type, section)` | `0x2fb0f0` | early-out `if (!byte_count) return SUCCESS`; writes one 16-B location with `count = byte_count >> 6`; **grows by doubling** (`calloc(2*array_count, 16)`, `memcpy`, `free`) when full. |
| `kbin_patch_extend(src, dst, eng)` | `0x2fb240` | concatenates `src->eng_patch[eng]` onto `dst`; grows `dst` to `next_power_of_two(dst.count + src.count)`; asserts `count <= array_count` (`kbin_patch.c:0x6E`). Splices a per-function list into the model list. |
| `kbin_patch_add_base_offset(info, eng, base)` | `0x2fb350` | `locations[i].offset += base` for all entries — rebases a sub-list placed at a non-zero final position. |
| `kbin_patch_free` / `kbin_patch_free_all` | `0x2fb070` / `0x2fb0c0` | per-engine / all-engine teardown. |

`kbin_patch_append` packs the four `u32`s with SSE
(`_mm_unpacklo_epi32`/`_mm_unpacklo_epi64`) and stores `byte_count >> 6` as the
slot count in one 16-byte aligned write:

```c
/* decompiled/kbin_patch_append_0x2fb0f0.c:19,31 */
if (!byte_count) return NRT_SUCCESS;                       /* skip zero-length patches  */
locations[count] = _mm_unpacklo_epi64(
    _mm_unpacklo_epi32(offset, byte_count >> 6),           /* {offset, count}           */
    _mm_unpacklo_epi32(type,   section));                  /* {type,   section}         */
```
`[OBSERVED]`

### 2.2 The core builder — `translate_pseudo_instrs_partial_v2 @0x2763d0`

`[HIGH/OBSERVED]` This pass walks the **source** stream one 64-byte slot at a
time, lowers each through `translate_one_pseudo_instr_v2` (which writes the
expanded real slots into `src_translated` and returns
`num_instructions_inserted`), and records the size delta as a patch:

```c
/* annotated from decompiled/translate_pseudo_instrs_partial_v2_0x2763d0.c:89-157 */
while (src_pos < src_end) {
    num_instructions_inserted = 0;
    size_used = src_translated->size_used;                 /* translated pos BEFORE emit */
    rc = translate_one_pseudo_instr_v2(.., &src_original[src_pos], .., src_translated,
                                       &num_instructions_inserted, ..);
    if (rc) return rc;

    if (num_instructions_inserted > 1)                     /* 1 source slot -> N>1 slots  */
        kbin_patch_append(info, eng, size_used,
                          (num_instructions_inserted - 1) << 6,  /* count = N-1           */
                          KBIN_PATCH_TYPE_INSERT, patch_section);
    else if (num_instructions_inserted == 0)               /* pure pseudo -> nothing      */
        kbin_patch_append(info, eng, size_used, 0x40,            /* count = 1             */
                          KBIN_PATCH_TYPE_DELETE, patch_section);
    /* == 1 : 1->1 passthrough, NO record emitted (the common case)                       */

    src_pos += 64;                                         /* advance one source slot      */
}
```

So byte-exactly:

* **INSERT** `count = N-1` — *"at translated `O`, `N-1` slots more than the source had"*.
* **DELETE** `count = 1` — *"the source slot at `O` was removed"* (`0x40` byte_count → `>>6` = 1).
* **MODIFY** `count = 1` — *"1→1 slot rewritten in place"* (see §2.4); **not** emitted here.

`patch_section` is chosen by `itf_setup_functions_one_eng @0x2798c0` per block:

```c
/* itf_setup_functions_one_eng_0x2798c0.c:166-168 */
v37 = KBIN_PATCH_SECTION_FUNCATION;
if (!*(_DWORD *)(func_block + 256))   /* no function id at +0x100 */
    v37 = KBIN_PATCH_SECTION_MAIN;
/* ... translate_pseudo_instrs_partial_v2(.., patch_section = v37, ..) */
```
`[OBSERVED]` — i.e. **FUNCTION** for a named callable block, **MAIN** for the body.

> **NOTE — the patch list is a debug/trace aid, not required for execution.**
> On an append failure the builder only `WARN`s
> (`"Failed to add patch insert info. Debug info may be inaccurate"` /
> `"...delete info..."`, lines 139/156) and continues. Execution correctness
> does **not** depend on the relocation table; only PC→source-line resolution
> degrades. `[OBSERVED]`

### 2.3 PREAMBLE / MAIN / POSTAMBLE / FUNCTION assembly — `ib_create_one_block @0x2f7e50`

`[HIGH/OBSERVED]` The final per-engine image is assembled in a fixed order, each
region recorded as a whole-region INSERT around the bare translated body —
exactly the order the relocation walk later traverses
(`decompiled/ib_create_one_block_0x2f7e50.c`):

```c
/* (a) PREAMBLE — sync / all-engine barrier prepended at offset 0           :1169 */
kbin_patch_append(info, eng, 0, final_instr_buf.size_used,
                  KBIN_PATCH_TYPE_INSERT, KBIN_PATCH_SECTION_PREAMBLE);

/* (b) MAIN body — translate_pseudo_instrs_partial_v2 (§2.2) for function[0],       */
/*     then splice its per-instruction INSERT/DELETE patches into the model  :1185 */
kbin_patch_extend(src, info, eng->eng_type);

/* (c) POSTAMBLE — common postamble + add_br_rel execution-reset branch      :1202 */
kbin_patch_append(info, eng, offset_0,
                  (u32)final_instr_buf.size_used - (u32)offset_0,
                  KBIN_PATCH_TYPE_INSERT, KBIN_PATCH_SECTION_POSTAMBLE);

/* (d) FUNCTION — extra named functions spliced via extend                   :1329 */
kbin_patch_extend(src_fn_buf_patch, info, eng->eng_type);
```

This maps 1:1 onto the kbin code-range structs (§3): PREAMBLE ↔ `one_offset`,
MAIN ↔ `core_offset`, the FUNCTION blocks ↔ `ib_functions[eng]`.

### 2.4 Where `MODIFY` (type 1) comes from

`[MED/INFERRED]` `translate_pseudo_instrs_partial_v2` itself emits only INSERT
and DELETE. `MODIFY` records are carried **through**
`ib_add_one_function_to_final_buf @0x27c4f0`, which copies a function's
pre-existing source patch list verbatim:

```c
/* ib_add_one_function_to_final_buf_0x27c4f0.c:147 */
kbin_patch_append(src_buf_patch, eng->eng_type,
                  (u32)final_instr_buf->size_used - v85,
                  v69[1] << 6,                       /* count                          */
                  (kbin_patch_type_t)   v69[2],      /* type    — INSERT/MODIFY/DELETE */
                  (kbin_patch_section_t)v69[3]);     /* section — preserved as-is      */
```

So `MODIFY` denotes a 1→1 slot rewritten in place (operand/address relocated,
slot count unchanged) and is consumed by `get_neff_ip` as a single `0x40` step
(§4). The in-place *producer* (the per-function pseudo lowering) is observed only
through this copy path here. `[MED]`

### 2.5 Handing the table off — `kbl_model_get_kbin_patch_info @0x3076d0`

`[HIGH/OBSERVED]` Per engine (loop bound `al_hal_tpb_get_tpb_eng_count`), it
`memmove`s `16 * array_count` bytes of `model->kbin_patch_info.eng_patch[i].locations`
into a caller-provided `kbin_patch_info_t` and copies the `count`. This is the
bridge that hands the finished relocation table to a downstream kbin consumer.

---

## 3. Relocation-target structs — kbin code ranges

`[HIGH/OBSERVED]` The consumer needs to know *where* each section physically
lives in the engine's instruction memory. Two parallel structs exist — a
host-side staging form (`ib_code_one_eng_t`) and a device-resolved form
(`ib_addrs_one_eng`):

```c
struct ib_code_range_t   { size_t offset; size_t size; };          /* ord 2492, 16 B  */

struct ib_code_one_eng_t {            /* ord 8578, 56 B — host pre-stage              */
  inst_block      *code;            /* +0x00                                          */
  ib_code_range_t  preamble;        /* +0x08                                          */
  ib_code_range_t  model_switch;    /* +0x18                                          */
  ib_code_range_t  core;            /* +0x28  == the MAIN body                        */
};

struct ib_addrs_one_eng {             /* 56 B — device-resolved; model_t.ib_addrs[5]  */
  dmem_t          *base;            /* +0x00  the DRAM/SBUF instruction buffer        */
  ib_code_range_t  one_offset;      /* +0x08  PREAMBLE region                         */
  ib_code_range_t  model_switch_offset; /* +0x18                                      */
  ib_code_range_t  core_offset;     /* +0x28  MAIN body region                        */
};
```

Model-level anchors (`structures.json` `model_t`):

| field | offset | meaning |
|-------|--------|---------|
| `kbin_patch_info` | **0x18A0** (6304) | the per-engine patch lists (§1) |
| `ib_addrs[5]`     | **0x1988** (6536) | `ib_addrs_one_eng_t*` per engine; accessed `mod + eng*8 + 0x1988` |
| `ib_functions[5]` | **0x19B0** (6576) | the FUNCTION section `{offset,size}` per engine; size at `+0x19B8` |

Supporting struct offsets, `[OBSERVED]`:

```c
struct dmem_t {                       /* 192 B */
  physical_core_t *pcore;     /* +0x00 */   uint64_t mem_handle;     /* +0x08 */
  size_t           size;      /* +0x10 */   uint64_t _pa;            /* +0x18 */
  volatile void   *_va;       /* +0x20 */   size_t   align_offset;   /* +0x28 */
  size_t allocated_size;      /* +0x30 */   dma_mem_location_t mem_type; /* +0x38 */
  /* ... */
};
```

The device address of the MAIN body's first slot is therefore
`base->_pa + base->align_offset + core_offset.offset`.

---

## 4. `get_neff_ip @0x2faee0` — the relocation walk (byte-exact)

`[HIGH/OBSERVED]` Hex-Rays failed on this routine; the algorithm below is
transcribed from `disasm/get_neff_ip_0x2faee0.asm` (283 bytes). It translates a
runtime device address back to a **1-based instruction index in the original
source stream**, accounting for every INSERT/DELETE/MODIFY between
`first_address` and `device_ip`.

Prototype (from the caller and the asm operand uses):

```c
uint32_t get_neff_ip(uint32_t              num_patches,    /* edi      */
                     kbin_patch_location_t *patches,       /* rsi      */
                     uint32_t              first_patch_idx,/* edx      */
                     uint64_t              base_address,   /* rcx      */
                     uint64_t              first_address,  /* r8       */
                     uint64_t              device_ip);     /* r9       */
```

Field reads confirm the §1 layout: `[loc+0]=offset`, `[loc+4]=count`,
`[loc+8]=type`, `[loc+0xC]=section`
(asm `0x2faf2e`/`0x30`/`0x33`/`0x37`).

```c
/* annotated translation of get_neff_ip_0x2faee0.asm */
neff_ip = 1;                                   /* r11d = 1                  (0x2faf01) */
section = patches[first_patch_idx].section;    /* ebp                       (0x2faef1) */
addr    = first_address;
i       = first_patch_idx;

if (addr >= device_ip) return 1;               /* nothing to walk           (0x2faf05) */

while (addr < device_ip && i < num_patches) {
    e = patches[i];
    if (e.section != section)         { i++; continue; }   /* wrong section (0x2faf37) */
    if (base_address + e.offset != addr) { i++; continue; }/* site not here (0x2faf3c) */

    switch (e.type) {
      case KBIN_PATCH_TYPE_MODIFY:             /* 1 source slot, 1 step     (0x2faf98) */
        addr += 0x40; neff_ip += 1; break;
      case KBIN_PATCH_TYPE_DELETE:             /* count source slots dropped(0x2faf16) */
        neff_ip += e.count;                    /*   index advances...                  */
        addr    += 0x40;                       /*   ...address by 0x40 per following run*/
        break;
      case KBIN_PATCH_TYPE_INSERT:             /* skip synthetic slots      (0x2faf55) */
        addr += (uint64_t)e.count << 6;        /*   address only; neff_ip UNCHANGED     */
        break;
      default:
        __assert_fail("Invalid patch type %d", "kbin_patch.c", 0xAC); /*      (0x2fafad)*/
    }
    i++;
}

/* tail: remaining distance is straight 1:1 source slots                     (0x2faf70) */
neff_ip += (device_ip - addr) >> 6;
return neff_ip;
```

Arithmetic facts, all `[OBSERVED]`:

* The `0x40` step / `<< 6` everywhere confirms the **64-byte slot stride** that
  [seq-microcode.md](./seq-microcode.md) establishes.
* **INSERT** advances the **address** but not the **NEFF-IP** counter — inserted
  runtime slots map to no source slot (synthetic relocation slack).
* **MODIFY** advances **both** by exactly one slot.
* **DELETE** adds `count` to the NEFF-IP (the dropped source slot still has an
  index) while the address advance for the run is folded into the `0x40` step and
  the following entries.

> **GOTCHA — DELETE bookkeeping is read from the branch structure, not a trace.**
> The asm DELETE path (`0x2faf16`) adds `count` to `neff_ip` then re-enters the
> address-advance loop; the exact interleaving of the count-add vs the per-slot
> `0x40` advance for a multi-DELETE run would need a dynamic trace to pin. The
> single-DELETE common case (`count = 1`) is unambiguous. `[MED]`

---

## 5. `kbin_patch_device_ip_to_neff_ip @0x2fb3a0` — section dispatch

`[HIGH/OBSERVED]` (disasm; Hex-Rays failed) The caller,
`exec_print_engine_instruction_pointer @0x260640`, reads the engine's **live**
hardware PC (`aws_hal_stpb_read_program_counter(eng, tpb->tpb_regs_base)`) and
formats `"%s 0x%lx (%d)"` = *engine, raw PC, NEFF IP*, for error/trace output.
So `device_ip` is a real on-chip sequencer PC.

```c
NRT_STATUS kbin_patch_device_ip_to_neff_ip(model_t *mod,
        al_hal_tpb_eng_type_t eng, uint64_t device_ip, uint32_t *neff_ip_out);
```

Structure of the routine (`disasm/kbin_patch_device_ip_to_neff_ip_0x2fb3a0.asm`):

```c
patch = &mod->kbin_patch_info.eng_patch[eng];    /* mod + (eng+0x18A)*16      (0x2fb3ad) */
count = patch->count;  locations = patch->locations;
if (count <= 0) goto fail_section_2;

/* (A) scan #1: first row whose [loc+0] == 2  -> anchor index `first_patch` (0x2fb3eb) */
for (i = 0; i < count; i++) if (locations[i].<+0> == 2) break;
if (i == count) { log("Failed to find section %d in the patch list"); return 1; }

/* (B) scan #2: first row whose [loc+0xC] == 1                            (0x2fb400) */
for (j = i; j < count; j++) if (locations[j].<+0xC> == 1) break;
if (j == count) { log("Failed to find section %d in the patch list"); return 1; }

/* device-resolved code ranges */
ib   = mod->ib_addrs[eng];                       /* mod + eng*8 + 0x1988      (0x2fb470) */
base = ib->base->_pa + ib->base->align_offset;   /* [r10+0x28] + [r10+0x18]   (0x2fb483) */
first_main = base + ib->core_offset.offset;      /* [rdx+0x28]                (0x2fb47b) */
last_main  = first_main + (ib->core_offset.size - 0x40);                   /* (0x2fb48e) */

/* (C) is device_ip inside the buffer AND inside MAIN? */
if (base <= device_ip < base + ib->base->size) {
    if (first_main <= device_ip <= last_main) {
        *neff_ip_out = get_neff_ip(count, locations, first_patch,
                                   base, first_main, device_ip);            /* (0x2fb53a) */
        return NRT_SUCCESS;
    }
}

/* (D) else try the FUNCTION section */
fn_sz = mod->ib_functions[eng].size;             /* [rdi+0x19B8]              (0x2fb4b0) */
if (fn_sz != 0) {
    fn_base = mod->ib_functions[eng].offset       /* [rdi+0x19B0]                        */
            + ib->core_offset.size + base;        /*                          (0x2fb4c0) */
    if (fn_base <= device_ip < fn_base + fn_sz) {
        /* scan for the FUNCTION row ([loc+0]==3, 0x2fb560), then TWO walks:             */
        r1 = get_neff_ip(.., MAIN base ..,  first_main, device_ip);   /* up to fn entry  */
        r2 = get_neff_ip(.., fn_base base.., fn_base,    device_ip);  /* within fn       */
        *neff_ip_out = r1 + r2;                   /* accumulate across sections (0x2fb5fe)*/
        return NRT_SUCCESS;
    }
}

/* (E) out of bounds */
log("device_ip 0x%lx is out of bounds of main and function buffer");        /* (0x2fb4db) */
/* (or "...of the main buffer" if no function section, 0x2fb54a) */
return 1;
```

The two-call accumulation in (D) is how a PC inside a FUNCTION block is mapped:
the MAIN-section walk yields the index up to the function entry, the
FUNCTION-section walk yields the offset within the function, and their sum is the
source IP.

> **CORRECTION vs a "find DELETE / find MAIN" gloss.** Read
> literally, scan #1 compares `dword [loc+0]` against `2` and scan #2 compares
> `dword [loc+0xC]` against `1`. With the §1 layout those are
> `location.offset == 2` and `location.section == POSTAMBLE(1)` — *not* a clean
> "first DELETE row / first MAIN row" by *type*. Because Hex-Rays produced no
> pseudocode for this routine, the precise **semantic** role of the two anchors
> (almost certainly bracketing the section boundary and seeding `first_patch` for
> `get_neff_ip`) is **INFERRED**, while the byte behaviour (`cmp [loc+0],2` then
> `cmp [loc+0xC],1`, both stepping `+0x10`; the FUNCTION scan compares
> `[loc+0]==3` at `0x2fb560`) is **OBSERVED**. Treat the field comparisons as
> ground truth and any "DELETE/MAIN/FUNCTION-row" naming as a working hypothesis.
> `[asm OBSERVED / role INFERRED]`

---

## 6. Worked example — the sample's PE collective

`[HIGH]` Ground truth from the carved sample (`tar/sg00/`,
[concrete-carve.md](./concrete-carve.md)):

```
pe.bin   = 64 B  = ONE source slot. First word little-endian 0xC810 -> 0x10C8.
pe.asm   = "PSEUDO_TRIGGER_COLLECTIVE $S[10]>0 $S[22]++@complete ctype=ALL_REDUCE
            input_tensor_id=3 output_tensor_id=4 num_elements=32 dtype=fp32 op=ADD group_id=0;"
pool.bin = 192 B = THREE slots: 0x10C1 (PSEUDO_DMA_TRIGGER q_gradient_in),
            0x10C1 ($S[22]>0 q_gradient_out), 0x10A0 (EVENT_SEMAPHORE $S[11]>0).
```
`[OBSERVED — xxd of pe.bin/pool.bin; pe.asm/pool.asm verbatim]`

> **NOTE — the lead halfword is `{opcode, word_len}`, not a 16-bit opcode (per
> [seq-microcode.md](./seq-microcode.md) §0/§1.1).** Each LE lead word above (`0x10C8`,
> `0x10C1`, `0x10A0`) is the first two bytes of the 4-byte `TONGA_ISA_TPB_INST_HEADER`:
> `byte0 = opcode` (the 1-byte `TONGA_ISA_TPB_OPCODE`; here `0xC8` / `0xC1` / `0xA0`) and
> `byte1 = inst_word_len = 0x10` (`== NWORDS == 16`, the **constant** 64-B slot-length marker).
> The relocation walk below strides whole 64-B slots regardless; the per-slot opcode that selects
> which pseudo to lower is the single `byte0`, not the halfword `0x10Cx`.

Load-time lowering of the **PE** engine (`eng = PE = 0`):

1. `ib_create_one_block` prepends a PREAMBLE (sync/all-engine barrier), say `K`
   slots, at translated offset 0 → `append(info, PE, 0, K*64, INSERT, PREAMBLE)`.
2. `translate_pseudo_instrs_partial_v2` walks `pe.bin`. The single
   `PSEUDO_TRIGGER_COLLECTIVE` source slot is expanded by
   `translate_one_pseudo_instr_v2` into the concrete collective micro-program
   (semaphore waits, the CC DMA-ring triggers, the `$S[22]` post-increment) — `N`
   real slots, `N > 1`, at translated offset `O` → `append(info, PE, O,
   (N-1)*64, INSERT, MAIN)` (count field = `N-1`).
3. POSTAMBLE (common postamble + reset branch), `M` slots at offset `P` →
   `append(info, PE, P, M*64, INSERT, POSTAMBLE)`.

The PE `eng_patch[0]` then holds, e.g.:

```
{offset=0, count=K-1, type=INSERT, section=PREAMBLE}
{offset=O, count=N-1, type=INSERT, section=MAIN}
{offset=P, count=M-1, type=INSERT, section=POSTAMBLE}
```

(Counts are slot counts; a barrier that is a single slot yields `count = 0` and
is skipped by `kbin_patch_append`'s `!byte_count` guard.)

**Trace-time reverse map.** A fault inside the expanded collective at device PC
`X` (with `first_main <= X <= last_main`) is fed to
`kbin_patch_device_ip_to_neff_ip`, which calls `get_neff_ip`. The
`INSERT(MAIN, count=N-1)` entry makes the walk skip the `N-1` synthetic slots
(address advances `N*64`, NEFF-IP advances only by 1), so `X` resolves back to
NEFF IP **1** — the original single `PSEUDO_TRIGGER_COLLECTIVE` line in
`pe.asm`. That is the precise host↔source correspondence.

For the **POOL** engine, a GPSIMD custom-op `PSEUDO_EMBEDDING_UPDATE` lowers via
`translate_one_pseudo_embedding_update_instr_v2 @0x2770f0` into roughly
`[dma_config ×2][dma_config_size][LOAD_POOL_ARGUMENT][DRAIN][EVENT_SEMAPHORE]` —
several slots from one source slot, i.e. an `INSERT` with `count = 5+` in the
POOL `eng_patch`. The `LOAD_POOL_ARGUMENT` slot is emitted by
`add_load_pool_arguments @0x276780` (the embedding-table base / SBUF base /
scalar-read / completion-write pusher), so its runtime PC maps back through the
same `INSERT` entry to the single source line. `[opcode roster CARRIED;
add_load_pool_arguments_0x276780.c and
translate_one_pseudo_embedding_update_instr_v2_0x2770f0.c OBSERVED present]`

---

## 7. Weight / constant data — tar layout → mem_ref

### 7.1 In the NEFF tar

`[OBSERVED schema / INFERRED for production weight bins]` Constant tensors are
**separate tar members**, named by the var table (`def.json` `var{}`). A var of
type `"file"` names the payload via `"file_name"` (plus the sub-graph `"prefix"`,
e.g. `sg00`). The payload is either a NumPy `.npy` (`-simout.npy` convention) or
a raw `.bin` of element bytes.

The carved sample is a tiny gradient test with no weights, but its `def.json`
shows the schema (`output` var carries
`"#file-name": "value_nn__output:0-simout.npy"`, `element_size=4`,
`internal_shape=[1,1,1,32]`, `var_id=2`; the `SB` var is `type=state-buffer,
var_id=1`; `gradient_in`/`gradient_out` are `tmp-buf` at `var_id` 3/4 — matching
the collective's `input_tensor_id=3` / `output_tensor_id=4`). `[OBSERVED —
tar/sg00/def.json]`

### 7.2 `parse_one_variable @0x4b36b0`, the `"file"` branch

`[HIGH/OBSERVED]` (`decompiled/ZL18parse_one_variable..._0x4b36b0.c:514-720`)

```c
/* read "file_name" and "size"; build path = prefix + "/" + file_name */
if (ends_with(file_name, ".npy")) {                         /* :544                       */
    numpy_load(neff, path, &ptr, &size);                    /* :601 — strips npy header   */
} else {                                                     /* raw .bin                   */
    neff_get_file_content(neff, path, &buf, &size);         /* :671 — tar-map lookup      */
    /* error label: "load_weight_bin" / "Failed to get neff weight content for %s" :712  */
}

/* create a MR_BUFFER mem_ref whose buffer points at the constant bytes */
mr = operator new(0x88);                                    /* :691  136-byte C++ object  */
mem_ref::mem_ref(mr, var_id, MR_BUFFER, name, shape);       /* :697                       */
mr->buffer    /* +0x58 */ = ptr;                            /* :700  payload pointer      */
mr->size      /* +0x48 */ = size;                           /* :702                       */
mr->alignment /* +0x50 */ = alignment;                      /* :703                       */
mr->dtype_id  /* +0x80 */ = dtype_id;                       /* :705                       */
mem_ref_info::add_var_to_lookup_maps(info, name, var_id, mr);/* :708                      */
```

`numpy_load @0x4cb810` strips the header:

```c
/* decompiled/numpy_load_0x4cb810.c:14-35 */
neff_get_file_content(neff, path, &hdr, &total);            /* tar-map lookup             */
if (hdr[0/*u16*/] != 0x4E93) return NRT_INVALID;            /* magic '\x93N'      :21     */
hlen = hdr[4/*u16 @ byte 8*/];                              /* npy v1 header length :27   */
*ptr  = (uint8_t*)hdr + (hlen + 10);                        /* data = hdr + hlen + 10 :33 */
*size = total - (hlen + 10);                                /*                       :35  */
```

So a constant tensor becomes an `MR_BUFFER` mem_ref whose `buffer` points at the
constant bytes — **inside the tar buffer** for `.npy` (header skipped, zero-copy),
or a private copy for raw `.bin` (via `load_bin_file @0x4ae500`, the generic
"malloc + memcpy out of the tar map" helper). This is the device-side analog of
metaneff weights: the var table binds *tar payload → var_id → mem_ref*, while
metaneff ([metaneff-io-abi.md](./metaneff-io-abi.md)) binds *host tensors → the
same var_id I/O slots* and carries **no** weights — weights live only here.

> **GOTCHA — two distinct `mem_ref` structs.** The `+0x58/+0x48/+0x50/+0x80`
> offsets above belong to the **runtime C++ `mem_ref` object** (`new(0x88)`,
> 208-B `mem_ref_t` form). The **on-disk `kbin_mem_ref`** (§7.3, 152 B) is a
> *different* struct with `buffer` at `+0x80`. Do not conflate them; §8 stages
> the on-disk form into the runtime form. `[OBSERVED — both in structures.json]`

### 7.3 `kbin_mem_ref` recap (152 B on-disk form)

`[OBSERVED]` `structures.json` `kbin_mem_ref`, `size = 152`:

```c
struct kbin_mem_ref {
  kbin_mr_type_t mr_type;     /* +0x00 */   const char *name;     /* +0x08 */
  size_t  size;               /* +0x10 */   size_t alignment;     /* +0x18 */
  uint32_t var_id;            /* +0x20 */   char dtype[16];       /* +0x24 */
  uint64_t shape[8];          /* +0x38 */   kbin_debug_tensor_md_t *dtensor_md; /* +0x78 */
  uint8_t *buffer;            /* +0x80  constant payload ptr; NULL for non-constants */
  union { ... } u;            /* +0x88  16-B union {var_id/io_ref | offset/backing | list | remote} */
};
```

`kbin_mr_type_t` (`enums.json`): `MR_INVALID=0, MR_SB=1, MR_BUFFER_STAGED=2,
MR_BUFFER=3, MR_TMP_BUF=4, MR_INPUT=5, MR_OUTPUT=6, MR_PTR=7,
MR_VIRTUAL_TMP_BUF=8, MR_LIST=9, MR_PTR_TABLE=10, MR_REMOTE=11`. The `buffer`
field is non-NULL only for `MR_BUFFER` constants; `INPUT/OUTPUT/TMP/PTR/...`
leave it NULL.

---

## 8. Placing constants into DRAM/SBUF + the loading DMA

`mem_ref_copy_and_stage_mr @0x2fb780` (`tdrv/mem_ref.c`) iterates the kbin's
`mem_ref_set` and resolves each mem_ref to a physical device address, copying
constant payloads into HBM as it goes. `[HIGH/OBSERVED]`

### 8.1 Which MR types get staged

`[OBSERVED]` (`decompiled/mem_ref_copy_and_stage_mr_0x2fb780.c:109`)

```c
if ((unsigned)mr_type > 8 || (mask = -283, _bittest64(&mask, mr_type)))
    skip;   /* not bulk-staged here */
```

`-283 = 0xFFFFFFFFFFFFFEE5`. The **clear** bits of `0xFEE5` are `{1,3,4,8}` — the
staged types:

| type | value | destination |
|------|-------|-------------|
| `MR_SB`              | 1 | on-chip State Buffer (AXI aperture, §8.3, no copy) |
| `MR_BUFFER`          | 3 | weights/constants → HBM (alloc + DMA copy) |
| `MR_TMP_BUF`         | 4 | scratch → HBM (alloc only, no copy) |
| `MR_VIRTUAL_TMP_BUF` | 8 | scratchpad (CC buffers, 4K-aligned) |

The set bits `{0,2,5,6,7,9,10,11}` (`INVALID / BUFFER_STAGED / INPUT / OUTPUT /
PTR / LIST / PTR_TABLE / REMOTE`) are resolved differently — I/O DMA at execute,
pointer tables (`MR_LIST` builds a `ptr_table`, lines 83-107), etc.

### 8.2 The HBM(DRAM) stage path for `MR_BUFFER` / `MR_TMP_BUF`

`[HIGH/OBSERVED]` (lines 195-266)

```c
hbm_idx = get_default_hbm_index(pcore->device_tpb_idx);     /* pick HBM channel  :60     */

/* usage tag: TMP_BUF(4) and VIRTUAL_TMP_BUF(8) -> NOT_SHARED; everything else -> WEIGHT  */
if (((mr.mr_type - 4) & 0xFFFFFFFB) != 0)                   /* :198 — true unless 4 or 8 */
    usage = DMA_MEM_USAGE_TYPE_WEIGHT;          /* = 6  */
else
    usage = DMA_MEM_USAGE_TYPE_SCRATCHPAD_NOT_SHARED;       /* = 16 */

dmem_alloc_aligned(allocator, &dmem, mr.size, TONGA_DRAM,   /* TONGA_DRAM = 2     :209   */
                   hbm_idx, tracker, usage, mr.name, mr.alignment);

if (mr.buffer != NULL)                                      /* a constant?               */
    dmem_buf_copyin(dmem, mr.buffer, 0, mr.size);           /* DMA the bytes in   :229   */
    /* log "Staged memref {%s} ... (alloc+copy)"                                          */
else
    /* log "(alloc only)" — scratch/tmp: reserve, no data                                */

mr.pointer.mr_ptr_id  = dmem;                               /* :264                      */
mr.physical_address   = dmem->_pa + dmem->align_offset;     /* resolved device PA :263   */
mr.mem_offset         = 0;                                  /* :266                      */
ht_insert(model_mr_set, mr.var_id, &mr.ht_node);            /* keyed by var_id    :269   */
```

So the resolved device address of a constant is `dmem._pa + dmem.align_offset`.
`TONGA_DRAM` is the MLA's HBM; weights are tagged `DMA_MEM_USAGE_TYPE_WEIGHT(6)`
for accounting.

> **CORRECTION — the usage predicate.** An earlier reading wrote the
> scratchpad branch as *"type in {TMP_BUF(4), BUFFER_STAGED?}"*. The binary's test
> is `((mr_type - 4) & 0xFFFFFFFB) != 0` (line 198), which masks bit 2 of
> `mr_type - 4` — it is false **only** for `mr_type ∈ {4, 8}`
> (`TMP_BUF` and `VIRTUAL_TMP_BUF`). `BUFFER_STAGED(2)` is *not* in the
> scratchpad set; it is filtered out earlier by §8.1's bit mask and never reaches
> this branch. `[OBSERVED]`

### 8.3 SBUF (`MR_SB`) and scratchpad

`[OBSERVED]` (lines 117-194)

* **`MR_SB(1)`**: `physical_address = aws_hal_stpb_get_axi_offset(tpb_idx, 0)`;
  `size = 0x2000000` (**32 MiB** State Buffer AXI aperture). **No copy** — the SB
  is on-chip and addressed directly (lines 120-122).
* **`MR_VIRTUAL_TMP_BUF(8)` + scratchpad enabled**: routed to local/shared
  scratchpad via `tdrv_scratchpad_get_mem`; allocations must be **4K-aligned**
  (`"...need to be 4K aligned. Required by EFA for CC buffers"`, line 131);
  `physical_address = mem_offset + dmem->_pa + dmem->align_offset` (line 153).
* `debug_tensor_md` (when present) is `malloc(0x204)` and copied per var
  (lines 307-311).

### 8.4 `dmem_buf_copyin @0x229820` → the actual transfer

`[HIGH/OBSERVED]`

```c
/* decompiled/dmem_buf_copyin_0x229820.c:22-36 — wrapped in nrt_sys_trace event type 30 */
if (mem->mem_type == HOST_DRAM)        ndl_memory_buf_copyin(...);     /* host-pinned     */
else if (nrt_gconf()->test_zerocopy)   ndl_memory_buf_zerocopyin(...); /* BAR4 direct     */
else                                   dmem_device_copy(mem, buffer, 0, size, 1); /* HBM  */
```

`dmem_device_copy @0x228090` performs the async bounce-buffer DMA:

```c
/* decompiled/dmem_device_copy_0x228090.c */
cpy_buf = mem->cpy_buf;                                       /* ndl_copy_buf_t ring      */
assert(cpy_buf != NULL);   /* dma_memory.c:0x2E6 :64                                       */
/* chunk size by class: <=0x3FFF -> 0x4000; >0x100000 -> 0x100000; else ~half, 4K-rounded  */
for each chunk:
    memcpy(cpy_buf->mmap_va + off, buffer + pos, n);          /* :127                     */
    ndl_memory_copy_as(cpy_buf->mem_handle, mem->mem_handle, .., wait_handle); /* :128    */
ndl_memory_copy_as_wait(mem->mem_handle, wait_handle[0]);     /* :113                     */
```

i.e. an **asynchronous DMA through a copy buffer** into HBM, double-buffered
chunk-by-chunk. This is the constant-load DMA.

### 8.5 Relationship to the var table & DMA rings

* The var table (`parse_one_variable`, §7) is the **index**:
  `var_id → mem_ref{type, size, buffer, dtype, shape}`. Constants are
  `MR_BUFFER`; I/O are `MR_INPUT`/`MR_OUTPUT`.
* At stage time `mem_ref_copy_and_stage_mr` turns `MR_BUFFER` mem_refs into
  HBM-resident `dmem` regions with a resolved `physical_address` (bytes DMA'd via
  `dmem_device_copy`).
* At execute time the engine sequencer programs (the `.bin` streams,
  [seq-microcode.md](./seq-microcode.md)) reference these regions by `var_id`;
  the typed DMA rings move operands SBUF↔HBM. **Weight staging is a load-time
  bulk copy** (this section); per-inference operand movement is the descriptor
  rings (a separate subsystem — see
  [../runtime/host-device-descriptor-handoff.md](../runtime/host-device-descriptor-handoff.md)).

---

## 9. End-to-end load / relocate / stage sequence

`[HIGH]`

```
nrt_load
 └─ neff_parse                       (tar -> files map)
     └─ kelf_load_from_neff (per sub-graph)
         ├─ parse_one_variable       var{} -> mem_refs; "file" vars -> MR_BUFFER w/ buffer
         │                           (numpy_load / load_bin_file out of the tar)      §7
         ├─ load per-engine .bin      (64-byte slot streams)               seq-microcode
         ├─ kbin construction         ib_create_one_block / itf_setup_functions_one_eng /
         │                            translate_pseudo_instrs_partial_v2:
         │                            expand pseudo-instrs -> final per-engine image,
         │                            BUILDING the kbin_patch_info INSERT/DELETE/MODIFY
         │                            lists per PREAMBLE/MAIN/POSTAMBLE/FUNCTION         §2
         └─ mem_ref_copy_and_stage_mr MR_BUFFER -> dmem_alloc_aligned(TONGA_DRAM) +
                                      dmem_buf_copyin (DMA); MR_SB -> AXI aperture        §8

At fault/trace time:
  exec_print_engine_instruction_pointer  reads the engine HW PC
   └─ kbin_patch_device_ip_to_neff_ip    (§5) -> get_neff_ip (§4)
        walks the patch list -> maps the runtime PC back to the source NEFF IP (.asm line)
```

---

## 10. Design points / cross-refs

1. `kbin_patch_location_t` is 16 B `{offset, count(slots = byte>>6), type, section}`;
   5 engines × `kbin_eng_patch_t` in `kbin_patch_info_t` at `model_t + 0x18A0`.
2. INSERT/DELETE come from pseudo-expansion (1→N / 1→0); MODIFY is a
   carried-through 1→1 in-place rewrite. PREAMBLE/POSTAMBLE/FUNCTION are
   whole-region INSERTs around the MAIN body, matching `ib_addrs_one_eng`'s code
   ranges. Section names match the
   [assembly-pipeline.md](./assembly-pipeline.md) construction side.
3. `get_neff_ip` math: INSERT advances address only (synthetic slots), MODIFY +1
   slot both, DELETE +count both; tail is `(device_ip - addr) >> 6` — all on the
   64-byte slot stride of [seq-microcode.md](./seq-microcode.md).
4. The relocation is a **debug/trace facility** (PC→source line); append failures
   only `WARN`. Execution correctness does not depend on it.
5. Constants ride as tar members named by the var table; `numpy_load` strips the
   npy header (magic `0x4E93`, data at `hdr + hlen + 10`); mem_ref type
   `MR_BUFFER(3)`.
6. Weights are DMA-staged into HBM (`TONGA_DRAM`) via `dmem_alloc_aligned` +
   `dmem_buf_copyin` → `dmem_device_copy` → `ndl_memory_copy_as` (async
   copy-buffer DMA), tagged `DMA_MEM_USAGE_TYPE_WEIGHT(6)`; `MR_SB` lives in the
   32 MiB on-chip SBUF AXI aperture (no copy). Only MR types
   `{SB, BUFFER, TMP_BUF, VIRTUAL_TMP_BUF}` stage.
7. The mem_ref / `kbin_mr_type` values match
   [metaneff-io-abi.md](./metaneff-io-abi.md) (var/mem_ref ABI; metaneff carries
   no weights); the on-disk `kbin_mem_ref` 152-B form (buffer @ +0x80) is
   field-mapped in `appendix/struct-host-runtime-layouts.md`.

## 11. Open items / not fully decoded

`[LOW]`

* The exact in-place producer of `MODIFY` (type 1) records inside per-function
  pseudo lowering — only the copy-through path (§2.4) is observed here.
* `model_switch_offset` (the middle code range) semantics — model-switch /
  fast-reconfigure; not exercised by the trace path read here.
* The precise semantic role of the two anchor scans in
  `kbin_patch_device_ip_to_neff_ip` (§5) — field behaviour OBSERVED, role
  INFERRED (no Hex-Rays output for this routine).
* `get_neff_ip`'s DELETE inner-loop interaction with the following entry's
  address bookkeeping — read from the asm branch structure; a dynamic trace would
  pin the count-add vs address-advance ordering for multi-DELETE runs.
* `numpy_load` only handles **v1** npy headers (`u16` hlen at byte 8); v2/v3
  (`u32` hlen) would need the producer to stay on v1 — consistent with the
  `-simout.npy` fixtures but not separately proven for large weights.
* The mapping of a weight `var_id` → which DMA ring (if any) re-loads it per
  inference vs. a one-time stage — scoped to the DMA subsystem.
