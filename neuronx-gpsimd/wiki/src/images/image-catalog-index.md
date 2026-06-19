# Firmware-Image Accessor Index

The **gen × engine × variant × region** matrix that the host resolver
`nrtucode_get_memory_image` walks into. This page is the single dense index of
**every embedded firmware-image getter** in `libnrtucode_internal.so`: name,
image pointer, size, real-blob-vs-cursor classification, and the
`(generation, engine, flavor, region)` key that selects it.

> **Scope.** This is the *catalog* page. Every getter gets a row; the resolver
> body is reproduced as annotated C; the per-blob byte semantics
> (`S:`/`P%i:` self-naming, the carve geometry, the three-source reconciliation)
> are summarized but live in detail on the per-generation image pages
> ([`sunda-*`](./sunda-pool.md), [`cayman-*`](./cayman-act.md),
> [`mariana-*`](./mariana-act.md), [`maverick-*`](./maverick-act.md)),
> the [EXTISA inventory](./extisa-inventory.md), the
> [PROF CAM/TABLE formats](./prof-cam-table-formats.md), and the
> [firmware-image catalog capstone](./firmware-image-catalog.md). The host
> resolver itself is documented at `runtime/image-hwdecode-resolvers.md`.

---

## 0. Target binary

| Field | Value |
|---|---|
| Library | `libnrtucode_internal.so` |
| Path | `…/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/` |
| sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` |
| Size | `10,276,288` B |
| BuildID | `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd` |
| Class | ELF64 x86-64 DYN, **NOT stripped** (the 5-generation "twin"; the shipped front lib `libnrtucode.so` is the stripped, PERF-only sibling) |

All facts below are static-analysis-derived: `nm` / `objdump -d` / `readelf` /
`dd` / `sha256sum` over the shipped binary. `[HIGH/OBSERVED]` unless tagged
otherwise.

### Section/offset model (so addresses are reproducible)

`readelf -SW` confirms three layout deltas you need to carve blobs and read the
tables: `[HIGH/OBSERVED]`

| Section | VA | File off | Delta (VA − fileoff) | Holds |
|---|---|---|---|---|
| `.rodata` | `0x46b0` | `0x46b0` | **0** (identity) | the 225 firmware blobs (`<NAME>_get.data`) |
| `.data.rel.ro` | `0x9b8cf0` | `0x9b6cf0` | `0x2000` | `image_list` (`0x9b8d20`), `hwdecode_table_list` (`0x9b9090`) |
| `.data` | `0x9ba4a8` | `0x9b74a8` | `0x3000` | the per-(gen,engine) descriptor arrays |

> **GOTCHA.** Only `.rodata` is identity-mapped, so a getter's `lea`-target
> img-ptr is *both* the runtime VA *and* the file offset — `dd skip=$imgptr
> count=$size` carves the exact blob. The `image_list`/descriptor tables are
> **not** identity-mapped (delta `0x2000`/`0x3000`); subtract the delta before
> `dd`/`xxd` or you read the wrong struct.

---

## 1. The getter shape and the naming grammar

Every accessor is the **exact 4-instruction stub** — verified
instruction-exact on multiple getters: `[HIGH/OBSERVED]`

```asm
9b3540 <CAYMAN_NX_ACT_DEBUG_DRAM_get>:
   lea    -0x84a147(%rip),%rax     # 169400 <CAYMAN_NX_ACT_DEBUG_DRAM_get.data>   ; img-ptr
   mov    %rax,(%rdi)              ; *out_ptr  = blob
   movq   $0x6260,(%rsi)           ; *out_size = size
   ret
```

i.e. `void <NAME>_get(void** out_ptr /*rdi*/, size_t* out_size /*rsi*/)` — it
writes the `(image-pointer, size)` pair through two out-pointers and returns. The
img-ptr is always a named `.rodata` symbol `<NAME>.data`; **all 386 objdump-parsed
`lea` targets equal the `nm` `.data` symbol address, 0 mismatches.** `[HIGH/OBSERVED]`

### Naming grammar `[HIGH/OBSERVED]`

```
<GEN>_<CLS>_<ENGINE>_<VARIANT>_<REGION>_get
```

| Field | Values |
|---|---|
| `GEN` | `SUNDA` (v2) · `CAYMAN` (v3) · `MARIANA` (v4) · `MARIANA_PLUS` (v4+) · `MAVERICK` (v5) |
| `CLS` | `NX` (NX-core instruction sequencer — the `S:` SEQ engine) · `Q7` (Vision-Q7 GPSIMD compute — the `P%i:` POOL engine) |
| `ENGINE` | `ACT` · `DVE` · `PE` · `POOL` · `SP` (NX side) · `POOL` (Q7 side only) |
| `VARIANT` | `DEBUG` · `PERF` · `TEST` (v3–v5) · `RELEASE` (SUNDA only) · `PROF` (→ `PROF_CAM`/`PROF_TABLE`) · `DYNAMIC_KERNEL_LOAD_{DEBUG,PERF,TEST}` (Q7_POOL, CAYMAN-family) · `PERF_EXTISA_n_{SO,JSON}` (Q7_POOL EXTISA kernels) |
| `REGION` | `IRAM` (code) · `DRAM` (data + log strings) · `SRAM` · `EXTRAM` · `CAM`/`TABLE` (under `PROF`) · `EXTISA_0..3_{SO,JSON}` (under Q7_POOL PERF) |

> **NOTE.** `NX_POOL` is the SEQ-style **sequencer** pool image (`S:` self-names);
> `Q7_POOL` is the **custom-op** Vision-Q7 compute image (`P%i:` self-names). They
> are distinct cores and distinct getter families — do not conflate the two
> `POOL`s.

---

## 2. Counts — verified against the binary

`nm libnrtucode_internal.so | rg -c '_get$'` = **388** = **386 defined-local**
(`nm` type `t`) getters **+ 2 weak-undefined** SUNDA Q7_POOL EXTISA stubs (see
§5). The 386 defined getters: `[HIGH/OBSERVED]`

| Generation | Getters | `nm \| rg -c` verification |
|---|---:|---|
| SUNDA | 24 | `' t SUNDA_.*_get$'` → 24 |
| CAYMAN | 100 | `' t CAYMAN_.*_get$'` → 100 |
| MARIANA | 100 | `' t MARIANA_.*_get$'` − `MARIANA_PLUS` → 200 − 100 = 100 |
| MARIANA_PLUS | 100 | `' t MARIANA_PLUS_.*_get$'` → 100 |
| MAVERICK | 62 | `' t MAVERICK_.*_get$'` → 62 |
| **Total** | **386** | `' t .*_get$'` → 386 |

> **GOTCHA — the MARIANA grep trap.** `rg ' t MARIANA_.*_get$'` returns **200**
> because `MARIANA_PLUS` symbols also begin `MARIANA_`. The true MARIANA-only
> count is `200 − 100 = 100`. Always subtract the `_PLUS` slice.

Category partition (each verified by `nm | rg -c`): `[HIGH/OBSERVED]`

| Category | Count | Resolver lane |
|---|---:|---|
| base (variant × region; IRAM/DRAM/SRAM/EXTRAM) | 288 | `get_memory_image` |
| `DYNAMIC_KERNEL_LOAD` (× region) | 36 | `get_memory_image` (keys 17/18/19) |
| EXTISA (`SO`+`JSON`, 4 idx × 2 forms × 4 gens) | 32 | `get_ext_isa` |
| PROF (`CAM`+`TABLE`) | 30 | `get_hwdecode_table` |
| **Total** | **386** | 324 + 30 + 32 |

The base+DKL = **324** getters are the resolver-reachable set of
`get_memory_image`; PROF (30) and EXTISA (32) are sibling lanes. `324 + 30 + 32 =
386`, exact, no residue. `[HIGH/OBSERVED]`

> **CORRECTION.** A "266-getter" figure circulated in the task brief. The binary
> does not produce 266 anywhere; the precise resolver-reachable count is **324**
> (or **288** base-only if DKL is excluded). The grand total is **386**. Use these.

### 225 distinct image pointers back 386 getters `[HIGH/OBSERVED]`

`objdump`-parsing every getter's `lea` target and `sort -u` yields **225 distinct
img-ptr addresses**. The 386 → 225 collapse is because the (almost-always-empty)
`SRAM`/`EXTRAM` segments return a `(cursor, 0)` pair where the cursor is the start
of the *next* engine's blob — a boundary marker, **not** a real alias. Region
population pattern: `[HIGH/OBSERVED]`

| Region | Population |
|---|---|
| `DRAM` | **always** non-zero (carries data + the firmware log strings) |
| `IRAM` | non-zero (code) — **except** the MAVERICK SP/Q7 anomaly (§6.5) |
| `SRAM` | almost always size 0 (76 of 81 are empty) |
| `EXTRAM` | almost always size 0 (80 of 81 are empty) |

---

## 3. The resolver — `nrtucode_get_memory_image`

`T nrtucode_get_memory_image @0x9b2960` (front-lib twin `@0x30adf0`). The first
arg is **not** a raw coretype — it is a **flat `(gen, engine)` image index 0..37**
that the host driver precomputes. `[HIGH/OBSERVED]`

```c
// nrtucode_get_memory_image — internal twin @0x9b2960, byte-exact from objdump+IDA.
// Returns: 0 ok | 1 idx>37 | 2 flavor-miss/region>3/empty-slot | 3 region-getter NULL
//        | 8 removed-env-flag tripwire (NRTUCODE_MPLUS_ON_MARIANA set to "1").
typedef void img_get(void** out_ptr, size_t* out_len);   // the 4-insn getter stub

struct image_desc {        // 0x28 = 40 bytes, .data, R_X86_64_64 getter relocs
    uint64_t  flavor_key;  // +0x00  1=PERF/RELEASE 2=DEBUG 3=TEST 17/18/19=DKL_{P,D,T}
    img_get*  IRAM_get;    // +0x08  region 0
    img_get*  DRAM_get;    // +0x10  region 1
    img_get*  SRAM_get;    // +0x18  region 2
    img_get*  EXTRAM_get;  // +0x20  region 3
};
struct image_list_ent {    // 16 bytes; image_list @0x9b8d20 is 38 of these (size 0x260)
    uint64_t       count;       // +0x00  # of flavor descriptors for this (gen,engine)
    image_desc*    table;       // +0x08  R_X86_64_RELATIVE addend = descriptor array base
};
extern image_list_ent image_list[38];   // @0x9b8d20 (.data.rel.ro)

int nrtucode_get_memory_image(unsigned idx,   /*edi: flat (gen,engine) index 0..37*/
                              int region,      /*esi: 0=IRAM 1=DRAM 2=SRAM 3=EXTRAM */
                              int flavor,      /*edx: 0=auto|1=PERF/REL|2=DEBUG|3=TEST|17/18/19=DKL*/
                              void** out_ptr,  /*rcx*/
                              size_t* out_len) /*r8 */
{
    int result = 1;
    if (idx > 0x25 /*37*/) return 1;                        // 9b2965 cmp $0x25,%edi

    // (A) REMOVED-flag tripwire — NOT a live MARIANA_PLUS-on-MARIANA selector.
    const char* mp = getenv("NRTUCODE_MPLUS_ON_MARIANA");   // 9b2987 (env @VA 0x4fae)
    if (mp) {                                                // first-byte logic:
        int t = (unsigned char)mp[0] - '1';                 //   value "1" (exactly) trips it
        if (mp[0] == '1') t = mp[1];
        if (-t == 0) {                                       // value == "1"
            fwrite(/*0x99-byte "...flag has been removed..."*/, 0x99, 1, stderr);
            return 8;                                        // 9b2ae9 → ret 8
        }
    }

    image_list_ent* e = &image_list[idx];                   // 9b29b5 lea image_list; idx*16

    // (G) flavor==0 → env-auto resolution (NEURON_UCODE_FLAVOR)
    if (flavor == 0) {                                       // 9b29bf test r15d
        const char* fv = getenv("NEURON_UCODE_FLAVOR");      // env @VA 0x52e2
        if (!fv)                            flavor = 1;      // unset → PERF
        else if (!strcmp(fv,"debug") ||
                 !strcmp(fv,"DEBUG"))       flavor = 2;      // → DEBUG
        else if (!strcmp(fv,"test"))        flavor = 3;      // → TEST
        else flavor = (strcmp(fv,"TEST")==0) ? 3 : 1;        // "TEST"→3 else PERF default
        // NOTE: no env path ever yields 17/18/19; DKL is host-driver-explicit only.
    }

    // (E) linear scan of `count` descriptors for flavor_key == flavor
    result = 2;
    if (e->count == 0) return 2;                             // empty slot (incl. idx 36)
    image_desc* d = e->table;
    for (uint64_t n = e->count; flavor != d->flavor_key; d++)// stride 0x28 (9b29e0..9b29f4)
        if (--n == 0) return 2;                              // no matching flavor → ret 2

    // (F) region select via jump table @VA 0x555c, then call the getter
    if ((unsigned)region > 3) return 2;                      // 9b2a79 cmp $0x3,%ebp
    img_get* g;
    switch (region) {                                        // 0x555c: 4×int32 rel32
        case 0: g = d->IRAM_get;   break;                    // +0x08
        case 1: g = d->DRAM_get;   break;                    // +0x10
        case 2: g = d->SRAM_get;   break;                    // +0x18
        case 3: g = d->EXTRAM_get; break;                    // +0x20
    }
    if (!g) return 3;                                        // 9b2ae2 region-getter NULL
    g(out_ptr, out_len);                                     // 9b2ad0 call *%rax
    return 0;                                                // 9b2ad2 xor %eax,%eax
}
```

Every branch, the region jump table `@VA 0x555c`, the `0x28` descriptor stride,
the env logic, and the return codes were triple-anchored (objdump disasm + IDA
decompile + raw-byte reads agree). `[HIGH/OBSERVED]`

### Return-code table `[HIGH/OBSERVED]`

| Code | Meaning |
|---|---|
| `0` | success — descriptor found, region getter non-NULL, called; `*out_ptr`/`*out_len` written (`out_len` may be `0` for a boundary cursor — still success) |
| `1` | `idx > 37` (`cmp $0x25`). idx 36 is *in-range* but empty → falls to code 2 |
| `2` | flavor not found in `count` descriptors, **or** `region > 3`, **or** `image_list[idx].count == 0` |
| `3` | the selected region getter **pointer** is NULL (variant present in table shape but not linked — front-lib MAVERICK / front-lib DEBUG/TEST) |
| `8` | `NRTUCODE_MPLUS_ON_MARIANA` set to `"1"` → removed-flag `fwrite` to stderr fires **before** any table access |

> **QUIRK — boundary cursor vs absent slot.** A zero-size `SRAM`/`EXTRAM` getter is
> a *real, non-NULL* function returning `(cursor_ptr, 0)`; the resolver's
> `test %rax,%rax` on the **getter pointer** passes, it calls the getter, the
> getter writes size 0, and the resolver returns **0** (success) with
> `*out_len == 0`. Code **3** only fires when the descriptor *slot itself* is NULL
> (a genuinely absent engine/flavor, e.g. front-lib MAVERICK or front-lib
> DEBUG/TEST). This cleanly separates *"this image has no SRAM bytes"* from *"this
> variant is not present in this build."*

---

## 4. The `image_list` key → getter selection map

`image_list @0x9b8d20` is 38 × 16 B (size `0x260`). The raw count vector read
straight from the binary (`dd skip=0x9b6d20`, delta `0x2000`) is, slot by slot:

```
[ 1,1,1,1,1,1,1,  3,3,3,3,3,3,6,3,  3,3,3,3,3,3,6,3,  3,3,3,3,3,3,6,3,  3,2,2,2,2,0,3 ]
   └─ SUNDA 0..6 ─┘└── CAYMAN 7..14 ─┘└─ MARIANA 15..22 ┘└ MPLUS 23..30 ┘└ MAVERICK 31..37 ┘
```

Σ = **102 descriptors** (SUNDA 7 + CAYMAN 27 + MARIANA 27 + MPLUS 27 + MAVERICK 14).
`102 × 4 region slots = 408 getter references → 324 distinct getters` (boundary
cursors share pointers). `[HIGH/OBSERVED]`

| idx | GEN | CLS | ENG | count | descriptor array (`.data` VA) | flavor keys present |
|---:|---|---|---|---:|---|---|
| 0 | SUNDA | NX | ACT | 1 | `0x9ba4b0` | `{1=RELEASE}` |
| 1 | SUNDA | NX | DVE | 1 | `0x9ba4d8` | `{1}` |
| 2 | SUNDA | NX | POOL | 1 | `0x9ba500` | `{1}` |
| 3 | SUNDA | NX | PE | 1 | `0x9ba528` | `{1}` |
| 4 | SUNDA | NX | SP | 1 | `0x9ba550` | `{1}` |
| 5 | SUNDA | NX | SP | 1 | `0x9ba578` | `{1}` *(2nd SP slot)* |
| 6 | SUNDA | Q7 | POOL | 1 | `0x9ba5a0` | `{1}` |
| 7 | CAYMAN | NX | ACT | 3 | `0x9ba5c8` | `{1,2,3}` |
| 8 | CAYMAN | NX | DVE | 3 | `0x9ba640` | `{1,2,3}` |
| 9 | CAYMAN | NX | POOL | 3 | `0x9ba6b8` | `{1,2,3}` |
| 10 | CAYMAN | NX | PE | 3 | `0x9ba730` | `{1,2,3}` |
| 11 | CAYMAN | NX | SP | 3 | `0x9ba7a8` | `{1,2,3}` |
| 12 | CAYMAN | NX | SP | 3 | `0x9ba820` | `{1,2,3}` *(2nd SP)* |
| 13 | CAYMAN | Q7 | POOL | 6 | `0x9ba898` | `{1,17,18,19,2,3}` = PERF, DKL_{PERF,DEBUG,TEST}, DEBUG, TEST |
| 14 | CAYMAN | Q7 | POOL | 3 | `0x9ba988` | `{1,2,3}` *(2nd Q7 slot — EXTISA-bearing)* |
| 15 | MARIANA | NX | ACT | 3 | `0x9baa00` | `{1,2,3}` |
| 16 | MARIANA | NX | DVE | 3 | `0x9baa78` | `{1,2,3}` |
| 17 | MARIANA | NX | POOL | 3 | `0x9baaf0` | `{1,2,3}` |
| 18 | MARIANA | NX | PE | 3 | `0x9bab68` | `{1,2,3}` |
| 19 | MARIANA | NX | SP | 3 | `0x9babe0` | `{1,2,3}` |
| 20 | MARIANA | NX | SP | 3 | `0x9bac58` | `{1,2,3}` *(2nd SP)* |
| 21 | MARIANA | Q7 | POOL | 6 | `0x9bacd0` | `{1,17,18,19,2,3}` |
| 22 | MARIANA | Q7 | POOL | 3 | `0x9badc0` | `{1,2,3}` *(2nd Q7)* |
| 23 | MARIANA_PLUS | NX | ACT | 3 | `0x9bae38` | `{1,2,3}` |
| 24 | MARIANA_PLUS | NX | DVE | 3 | `0x9baeb0` | `{1,2,3}` |
| 25 | MARIANA_PLUS | NX | POOL | 3 | `0x9baf28` | `{1,2,3}` |
| 26 | MARIANA_PLUS | NX | PE | 3 | `0x9bafa0` | `{1,2,3}` |
| 27 | MARIANA_PLUS | NX | SP | 3 | `0x9bb018` | `{1,2,3}` |
| 28 | MARIANA_PLUS | NX | SP | 3 | `0x9bb090` | `{1,2,3}` *(2nd SP)* |
| 29 | MARIANA_PLUS | Q7 | POOL | 6 | `0x9bb108` | `{1,17,18,19,2,3}` |
| 30 | MARIANA_PLUS | Q7 | POOL | 3 | `0x9bb1f8` | `{1,2,3}` *(2nd Q7)* |
| 31 | MAVERICK | NX | DVE | 3 | `0x9bb270` | `{1,2,3}` *(full DEBUG kept)* |
| 32 | MAVERICK | NX | POOL | 2 | `0x9bb2e8` | `{1,3}` = PERF, TEST *(**no DEBUG**)* |
| 33 | MAVERICK | NX | PE | 2 | `0x9bb338` | `{1,3}` *(no DEBUG)* |
| 34 | MAVERICK | NX | SP | 2 | `0x9bb388` | `{1,3}` *(no DEBUG)* |
| 35 | MAVERICK | NX | SP | 2 | `0x9bb3d8` | `{1,3}` *(2nd SP, no DEBUG)* |
| 36 | — | — | — | **0** | `0x0` | **EMPTY boundary slot** → `get_memory_image(36,…)` = ret 2 |
| 37 | MAVERICK | Q7 | POOL | 3 | `0x9bb428` | `{1,2,3}` |

### Flavor enum `[HIGH/OBSERVED]`

| Key | Flavor | Notes |
|---:|---|---|
| `1` | PERF (and SUNDA `RELEASE` — reuses key 1) | production default |
| `2` | DEBUG | |
| `3` | TEST | |
| `17` (`0x11`) | `DYNAMIC_KERNEL_LOAD_PERF` | Q7_POOL only (idx 13/21/29) |
| `18` (`0x12`) | `DYNAMIC_KERNEL_LOAD_DEBUG` | Q7_POOL only |
| `19` (`0x13`) | `DYNAMIC_KERNEL_LOAD_TEST` | Q7_POOL only |

> **NOTE — engine ↔ slot.** The engine is encoded by slot position, not a separate
> arg. Per gen the order is `NX_ACT, NX_DVE, NX_POOL, NX_PE, NX_SP, NX_SP, Q7_POOL
> [, Q7_POOL]`. This reconciles with the CSR sequencer-aperture ordering
> `PE=0x000, POOL=0x100, ACT=0x200, DVE=0x300`; `PE=0` and `DVE=3` are OBSERVED,
> `POOL=1`/`ACT=2` are **INFERRED** from that ordering `[MED/INFERRED]`. The same
> flat firmware binary loads on any engine slot; `engine_idx` is computed at boot
> from `engine_base_addr`.

> **QUIRK — the double-SP slots.** Every generation enumerates `NX_SP` **twice**
> (e.g. il[4]&il[5] SUNDA; il[34]&il[35] MAVERICK). Both descriptors point at the
> **same** `NX_SP` getters — byte-identical duplicates. The aliasing is OBSERVED;
> the reason (left/right SP, sync-pair, or a fixed per-gen slot stride) is
> **INFERRED** `[MED]`. idx 36's empty slot is the consequence: MAVERICK has no
> `NX_ACT`, so the NX block is one slot short and a 1-slot gap opens before
> `Q7_POOL` at idx 37.

---

## 5. SUNDA EXTISA — the two weak-undefined getters

`nm` reports **388** `_get` symbols = 386 local + **2 weak-undef**:

```
w SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get
w SUNDA_Q7_POOL_RELEASE_EXTISA_0_JSON_get      ; .dynsym: NOTYPE WEAK UND, value 0
```

These have **no body and no `.data`** — they are link-time placeholders resolved
only when the runtime `libnrtucode_extisa.so` container is present. SUNDA ships
its 24 base RELEASE engine images **in-lib**, but its Q7 POOL custom-op kernel
lives in the runtime EXTISA container, not here. `[HIGH/OBSERVED]` Consequently
`get_num_ext_isa_libs` returns 4 only for gen indices whose bit is set in mask
`0x2020202000` = `{13,21,29,37}` (CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK);
SUNDA's bit is unset → 0 in-lib EXTISA libs. See
[EXTISA inventory](./extisa-inventory.md).

---

## 6. The full getter matrix (all 386)

Columns: `GEN · CLS · ENG · VARIANT · REGION · SYMBOL · ACCESSOR(.text VA) ·
IMG-PTR(.rodata VA == file off) · SIZE · class`. **size `0x0` = empty
segment / boundary cursor.** "class": **R** = real blob, **C** = boundary cursor
(`(ptr,0)` marker into the next engine's blob).

> **GOTCHA — DRAM-string offset.** For a DRAM blob, the device-VA-to-blob offset is
> `VA − 0x80000` (DRAM sits at device VA `0x80000`, IRAM at `0x0`). The img-ptr
> column below is the **host** `.rodata` file offset of the blob, not the device
> VA — see the per-gen image pages for the device-side string carve.

### 6.1 SUNDA (24 — RELEASE only, no PROF/DKL/EXTISA-in-lib)

| GEN | CLS | ENG | VAR | REG | SYMBOL | ACCESSOR | IMG-PTR | SIZE | cls |
|---|---|---|---|---|---|---|---|---|---|
| SUNDA | NX | ACT | RELEASE | IRAM | `SUNDA_NX_ACT_RELEASE_IRAM_get` | `0x9b2d20` | `0x000055f0` | `0x8f20` | R |
| SUNDA | NX | ACT | RELEASE | DRAM | `SUNDA_NX_ACT_RELEASE_DRAM_get` | `0x9b2d40` | `0x0000e510` | `0x2120` | R |
| SUNDA | NX | ACT | RELEASE | SRAM | `SUNDA_NX_ACT_RELEASE_SRAM_get` | `0x9b2d60` | `0x00010630` | `0x0` | C |
| SUNDA | NX | ACT | RELEASE | EXTRAM | `SUNDA_NX_ACT_RELEASE_EXTRAM_get` | `0x9b2d80` | `0x00010630` | `0x0` | C |
| SUNDA | NX | DVE | RELEASE | IRAM | `SUNDA_NX_DVE_RELEASE_IRAM_get` | `0x9b2da0` | `0x00010630` | `0xbab0` | R |
| SUNDA | NX | DVE | RELEASE | DRAM | `SUNDA_NX_DVE_RELEASE_DRAM_get` | `0x9b2dc0` | `0x0001c0e0` | `0x2660` | R |
| SUNDA | NX | DVE | RELEASE | SRAM | `SUNDA_NX_DVE_RELEASE_SRAM_get` | `0x9b2de0` | `0x0001e740` | `0x0` | C |
| SUNDA | NX | DVE | RELEASE | EXTRAM | `SUNDA_NX_DVE_RELEASE_EXTRAM_get` | `0x9b2e00` | `0x0001e740` | `0x0` | C |
| SUNDA | NX | PE | RELEASE | IRAM | `SUNDA_NX_PE_RELEASE_IRAM_get` | `0x9b2e20` | `0x0001e740` | `0xb3d0` | R |
| SUNDA | NX | PE | RELEASE | DRAM | `SUNDA_NX_PE_RELEASE_DRAM_get` | `0x9b2e40` | `0x00029b10` | `0x2300` | R |
| SUNDA | NX | PE | RELEASE | SRAM | `SUNDA_NX_PE_RELEASE_SRAM_get` | `0x9b2e60` | `0x0002be10` | `0x0` | C |
| SUNDA | NX | PE | RELEASE | EXTRAM | `SUNDA_NX_PE_RELEASE_EXTRAM_get` | `0x9b2e80` | `0x0002be10` | `0x0` | C |
| SUNDA | NX | POOL | RELEASE | IRAM | `SUNDA_NX_POOL_RELEASE_IRAM_get` | `0x9b2ea0` | `0x0002be10` | `0xd040` | R |
| SUNDA | NX | POOL | RELEASE | DRAM | `SUNDA_NX_POOL_RELEASE_DRAM_get` | `0x9b2ec0` | `0x00038e50` | `0x2730` | R |
| SUNDA | NX | POOL | RELEASE | SRAM | `SUNDA_NX_POOL_RELEASE_SRAM_get` | `0x9b2ee0` | `0x0003b580` | `0x0` | C |
| SUNDA | NX | POOL | RELEASE | EXTRAM | `SUNDA_NX_POOL_RELEASE_EXTRAM_get` | `0x9b2f00` | `0x0003b580` | `0x0` | C |
| SUNDA | NX | SP | RELEASE | IRAM | `SUNDA_NX_SP_RELEASE_IRAM_get` | `0x9b2f20` | `0x0003b580` | `0xb450` | R |
| SUNDA | NX | SP | RELEASE | DRAM | `SUNDA_NX_SP_RELEASE_DRAM_get` | `0x9b2f40` | `0x000469d0` | `0x2220` | R |
| SUNDA | NX | SP | RELEASE | SRAM | `SUNDA_NX_SP_RELEASE_SRAM_get` | `0x9b2f60` | `0x00048bf0` | `0x0` | C |
| SUNDA | NX | SP | RELEASE | EXTRAM | `SUNDA_NX_SP_RELEASE_EXTRAM_get` | `0x9b2f80` | `0x00048bf0` | `0x0` | C |
| SUNDA | Q7 | POOL | RELEASE | IRAM | `SUNDA_Q7_POOL_RELEASE_IRAM_get` | `0x9b2fa0` | `0x00048bf0` | `0x42d0` | R |
| SUNDA | Q7 | POOL | RELEASE | DRAM | `SUNDA_Q7_POOL_RELEASE_DRAM_get` | `0x9b2fc0` | `0x0004cec0` | `0xa540` | R |
| SUNDA | Q7 | POOL | RELEASE | SRAM | `SUNDA_Q7_POOL_RELEASE_SRAM_get` | `0x9b2fe0` | `0x00057400` | `0x0` | C |
| SUNDA | Q7 | POOL | RELEASE | EXTRAM | `SUNDA_Q7_POOL_RELEASE_EXTRAM_get` | `0x9b3000` | `0x00057400` | `0x1b40` | **R** |

> **QUIRK.** `SUNDA_Q7_POOL_RELEASE_EXTRAM` is the **sole non-zero EXTRAM** in the
> entire catalog (`0x1b40`) — SUNDA's Q7 POOL carries an extra EXTRAM segment.

### 6.2 CAYMAN (100)

| GEN | CLS | ENG | VAR | REG | SYMBOL | ACCESSOR | IMG-PTR | SIZE | cls |
|---|---|---|---|---|---|---|---|---|---|
| CAYMAN | NX | ACT | DEBUG | IRAM | `CAYMAN_NX_ACT_DEBUG_IRAM_get` | `0x9b3520` | `0x00150220` | `0x191e0` | R |
| CAYMAN | NX | ACT | DEBUG | DRAM | `CAYMAN_NX_ACT_DEBUG_DRAM_get` | `0x9b3540` | `0x00169400` | `0x6260` | R |
| CAYMAN | NX | ACT | DEBUG | SRAM | `CAYMAN_NX_ACT_DEBUG_SRAM_get` | `0x9b3560` | `0x0016f660` | `0x0` | C |
| CAYMAN | NX | ACT | DEBUG | EXTRAM | `CAYMAN_NX_ACT_DEBUG_EXTRAM_get` | `0x9b3580` | `0x0016f660` | `0x0` | C |
| CAYMAN | NX | ACT | PERF | IRAM | `CAYMAN_NX_ACT_PERF_IRAM_get` | `0x9b3020` | `0x00058f40` | `0x13dc0` | R |
| CAYMAN | NX | ACT | PERF | DRAM | `CAYMAN_NX_ACT_PERF_DRAM_get` | `0x9b3040` | `0x0006cd00` | `0x2900` | R |
| CAYMAN | NX | ACT | PERF | SRAM | `CAYMAN_NX_ACT_PERF_SRAM_get` | `0x9b3060` | `0x0006f600` | `0x0` | C |
| CAYMAN | NX | ACT | PERF | EXTRAM | `CAYMAN_NX_ACT_PERF_EXTRAM_get` | `0x9b3080` | `0x0006f600` | `0x0` | C |
| CAYMAN | NX | ACT | PROF | CAM | `CAYMAN_NX_ACT_PROF_CAM_get` | `0x9b3ba0` | `0x003028a0` | `0x400` | R |
| CAYMAN | NX | ACT | PROF | TABLE | `CAYMAN_NX_ACT_PROF_TABLE_get` | `0x9b3bc0` | `0x00302ca0` | `0x2000` | R |
| CAYMAN | NX | ACT | TEST | IRAM | `CAYMAN_NX_ACT_TEST_IRAM_get` | `0x9b32a0` | `0x000d58a0` | `0x13940` | R |
| CAYMAN | NX | ACT | TEST | DRAM | `CAYMAN_NX_ACT_TEST_DRAM_get` | `0x9b32c0` | `0x000e91e0` | `0x2c00` | R |
| CAYMAN | NX | ACT | TEST | SRAM | `CAYMAN_NX_ACT_TEST_SRAM_get` | `0x9b32e0` | `0x000ebde0` | `0x0` | C |
| CAYMAN | NX | ACT | TEST | EXTRAM | `CAYMAN_NX_ACT_TEST_EXTRAM_get` | `0x9b3300` | `0x000ebde0` | `0x0` | C |
| CAYMAN | NX | DVE | DEBUG | IRAM | `CAYMAN_NX_DVE_DEBUG_IRAM_get` | `0x9b35a0` | `0x0016f660` | `0x1bcc0` | R |
| CAYMAN | NX | DVE | DEBUG | DRAM | `CAYMAN_NX_DVE_DEBUG_DRAM_get` | `0x9b35c0` | `0x0018b320` | `0x6d60` | R |
| CAYMAN | NX | DVE | DEBUG | SRAM | `CAYMAN_NX_DVE_DEBUG_SRAM_get` | `0x9b35e0` | `0x00192080` | `0x0` | C |
| CAYMAN | NX | DVE | DEBUG | EXTRAM | `CAYMAN_NX_DVE_DEBUG_EXTRAM_get` | `0x9b3600` | `0x00192080` | `0x0` | C |
| CAYMAN | NX | DVE | PERF | IRAM | `CAYMAN_NX_DVE_PERF_IRAM_get` | `0x9b30a0` | `0x0006f600` | `0x15c20` | R |
| CAYMAN | NX | DVE | PERF | DRAM | `CAYMAN_NX_DVE_PERF_DRAM_get` | `0x9b30c0` | `0x00085220` | `0x2fc0` | R |
| CAYMAN | NX | DVE | PERF | SRAM | `CAYMAN_NX_DVE_PERF_SRAM_get` | `0x9b30e0` | `0x000881e0` | `0x0` | C |
| CAYMAN | NX | DVE | PERF | EXTRAM | `CAYMAN_NX_DVE_PERF_EXTRAM_get` | `0x9b3100` | `0x000881e0` | `0x0` | C |
| CAYMAN | NX | DVE | PROF | CAM | `CAYMAN_NX_DVE_PROF_CAM_get` | `0x9b3be0` | `0x00304ca0` | `0x400` | R |
| CAYMAN | NX | DVE | PROF | TABLE | `CAYMAN_NX_DVE_PROF_TABLE_get` | `0x9b3c00` | `0x003050a0` | `0x2000` | R |
| CAYMAN | NX | DVE | TEST | IRAM | `CAYMAN_NX_DVE_TEST_IRAM_get` | `0x9b3320` | `0x000ebde0` | `0x15840` | R |
| CAYMAN | NX | DVE | TEST | DRAM | `CAYMAN_NX_DVE_TEST_DRAM_get` | `0x9b3340` | `0x00101620` | `0x32c0` | R |
| CAYMAN | NX | DVE | TEST | SRAM | `CAYMAN_NX_DVE_TEST_SRAM_get` | `0x9b3360` | `0x001048e0` | `0x0` | C |
| CAYMAN | NX | DVE | TEST | EXTRAM | `CAYMAN_NX_DVE_TEST_EXTRAM_get` | `0x9b3380` | `0x001048e0` | `0x0` | C |
| CAYMAN | NX | PE | DEBUG | IRAM | `CAYMAN_NX_PE_DEBUG_IRAM_get` | `0x9b3620` | `0x00192080` | `0x19180` | R |
| CAYMAN | NX | PE | DEBUG | DRAM | `CAYMAN_NX_PE_DEBUG_DRAM_get` | `0x9b3640` | `0x001ab200` | `0x6220` | R |
| CAYMAN | NX | PE | DEBUG | SRAM | `CAYMAN_NX_PE_DEBUG_SRAM_get` | `0x9b3660` | `0x001b1420` | `0x0` | C |
| CAYMAN | NX | PE | DEBUG | EXTRAM | `CAYMAN_NX_PE_DEBUG_EXTRAM_get` | `0x9b3680` | `0x001b1420` | `0x0` | C |
| CAYMAN | NX | PE | PERF | IRAM | `CAYMAN_NX_PE_PERF_IRAM_get` | `0x9b3120` | `0x000881e0` | `0x159e0` | R |
| CAYMAN | NX | PE | PERF | DRAM | `CAYMAN_NX_PE_PERF_DRAM_get` | `0x9b3140` | `0x0009dbc0` | `0x2a40` | R |
| CAYMAN | NX | PE | PERF | SRAM | `CAYMAN_NX_PE_PERF_SRAM_get` | `0x9b3160` | `0x000a0600` | `0x0` | C |
| CAYMAN | NX | PE | PERF | EXTRAM | `CAYMAN_NX_PE_PERF_EXTRAM_get` | `0x9b3180` | `0x000a0600` | `0x0` | C |
| CAYMAN | NX | PE | PROF | CAM | `CAYMAN_NX_PE_PROF_CAM_get` | `0x9b3c20` | `0x003070a0` | `0x400` | R |
| CAYMAN | NX | PE | PROF | TABLE | `CAYMAN_NX_PE_PROF_TABLE_get` | `0x9b3c40` | `0x003074a0` | `0x2000` | R |
| CAYMAN | NX | PE | TEST | IRAM | `CAYMAN_NX_PE_TEST_IRAM_get` | `0x9b33a0` | `0x001048e0` | `0x152c0` | R |
| CAYMAN | NX | PE | TEST | DRAM | `CAYMAN_NX_PE_TEST_DRAM_get` | `0x9b33c0` | `0x00119ba0` | `0x2d80` | R |
| CAYMAN | NX | PE | TEST | SRAM | `CAYMAN_NX_PE_TEST_SRAM_get` | `0x9b33e0` | `0x0011c920` | `0x0` | C |
| CAYMAN | NX | PE | TEST | EXTRAM | `CAYMAN_NX_PE_TEST_EXTRAM_get` | `0x9b3400` | `0x0011c920` | `0x0` | C |
| CAYMAN | NX | POOL | DEBUG | IRAM | `CAYMAN_NX_POOL_DEBUG_IRAM_get` | `0x9b36a0` | `0x001b1420` | `0x1c820` | R |
| CAYMAN | NX | POOL | DEBUG | DRAM | `CAYMAN_NX_POOL_DEBUG_DRAM_get` | `0x9b36c0` | `0x001cdc40` | `0x6f20` | R |
| CAYMAN | NX | POOL | DEBUG | SRAM | `CAYMAN_NX_POOL_DEBUG_SRAM_get` | `0x9b36e0` | `0x001d4b60` | `0x0` | C |
| CAYMAN | NX | POOL | DEBUG | EXTRAM | `CAYMAN_NX_POOL_DEBUG_EXTRAM_get` | `0x9b3700` | `0x001d4b60` | `0x0` | C |
| CAYMAN | NX | POOL | PERF | IRAM | `CAYMAN_NX_POOL_PERF_IRAM_get` | `0x9b31a0` | `0x000a0600` | `0x17280` | R |
| CAYMAN | NX | POOL | PERF | DRAM | `CAYMAN_NX_POOL_PERF_DRAM_get` | `0x9b31c0` | `0x000b7880` | `0x3020` | R |
| CAYMAN | NX | POOL | PERF | SRAM | `CAYMAN_NX_POOL_PERF_SRAM_get` | `0x9b31e0` | `0x000ba8a0` | `0x0` | C |
| CAYMAN | NX | POOL | PERF | EXTRAM | `CAYMAN_NX_POOL_PERF_EXTRAM_get` | `0x9b3200` | `0x000ba8a0` | `0x0` | C |
| CAYMAN | NX | POOL | PROF | CAM | `CAYMAN_NX_POOL_PROF_CAM_get` | `0x9b3c60` | `0x003094a0` | `0x400` | R |
| CAYMAN | NX | POOL | PROF | TABLE | `CAYMAN_NX_POOL_PROF_TABLE_get` | `0x9b3c80` | `0x003098a0` | `0x2000` | R |
| CAYMAN | NX | POOL | TEST | IRAM | `CAYMAN_NX_POOL_TEST_IRAM_get` | `0x9b3420` | `0x0011c920` | `0x16a00` | R |
| CAYMAN | NX | POOL | TEST | DRAM | `CAYMAN_NX_POOL_TEST_DRAM_get` | `0x9b3440` | `0x00133320` | `0x3320` | R |
| CAYMAN | NX | POOL | TEST | SRAM | `CAYMAN_NX_POOL_TEST_SRAM_get` | `0x9b3460` | `0x00136640` | `0x0` | C |
| CAYMAN | NX | POOL | TEST | EXTRAM | `CAYMAN_NX_POOL_TEST_EXTRAM_get` | `0x9b3480` | `0x00136640` | `0x0` | C |
| CAYMAN | NX | SP | DEBUG | IRAM | `CAYMAN_NX_SP_DEBUG_IRAM_get` | `0x9b3720` | `0x001d4b60` | `0x199a0` | R |
| CAYMAN | NX | SP | DEBUG | DRAM | `CAYMAN_NX_SP_DEBUG_DRAM_get` | `0x9b3740` | `0x001ee500` | `0x6360` | R |
| CAYMAN | NX | SP | DEBUG | SRAM | `CAYMAN_NX_SP_DEBUG_SRAM_get` | `0x9b3760` | `0x001f4860` | `0x0` | C |
| CAYMAN | NX | SP | DEBUG | EXTRAM | `CAYMAN_NX_SP_DEBUG_EXTRAM_get` | `0x9b3780` | `0x001f4860` | `0x0` | C |
| CAYMAN | NX | SP | PERF | IRAM | `CAYMAN_NX_SP_PERF_IRAM_get` | `0x9b3220` | `0x000ba8a0` | `0x182c0` | R |
| CAYMAN | NX | SP | PERF | DRAM | `CAYMAN_NX_SP_PERF_DRAM_get` | `0x9b3240` | `0x000d2b60` | `0x2d40` | R |
| CAYMAN | NX | SP | PERF | SRAM | `CAYMAN_NX_SP_PERF_SRAM_get` | `0x9b3260` | `0x000d58a0` | `0x0` | C |
| CAYMAN | NX | SP | PERF | EXTRAM | `CAYMAN_NX_SP_PERF_EXTRAM_get` | `0x9b3280` | `0x000d58a0` | `0x0` | C |
| CAYMAN | NX | SP | TEST | IRAM | `CAYMAN_NX_SP_TEST_IRAM_get` | `0x9b34a0` | `0x00136640` | `0x16ba0` | R |
| CAYMAN | NX | SP | TEST | DRAM | `CAYMAN_NX_SP_TEST_DRAM_get` | `0x9b34c0` | `0x0014d1e0` | `0x3040` | R |
| CAYMAN | NX | SP | TEST | SRAM | `CAYMAN_NX_SP_TEST_SRAM_get` | `0x9b34e0` | `0x00150220` | `0x0` | C |
| CAYMAN | NX | SP | TEST | EXTRAM | `CAYMAN_NX_SP_TEST_EXTRAM_get` | `0x9b3500` | `0x00150220` | `0x0` | C |
| CAYMAN | Q7 | POOL | DEBUG | IRAM | `CAYMAN_Q7_POOL_DEBUG_IRAM_get` | `0x9b38a0` | `0x00249020` | `0x1ea40` | R |
| CAYMAN | Q7 | POOL | DEBUG | DRAM | `CAYMAN_Q7_POOL_DEBUG_DRAM_get` | `0x9b38c0` | `0x00267a60` | `0x15d00` | R |
| CAYMAN | Q7 | POOL | DEBUG | SRAM | `CAYMAN_Q7_POOL_DEBUG_SRAM_get` | `0x9b38e0` | `0x0027d760` | `0x0` | C |
| CAYMAN | Q7 | POOL | DEBUG | EXTRAM | `CAYMAN_Q7_POOL_DEBUG_EXTRAM_get` | `0x9b3900` | `0x0027d760` | `0x0` | C |
| CAYMAN | Q7 | POOL | PERF | IRAM | `CAYMAN_Q7_POOL_PERF_IRAM_get` | `0x9b37a0` | `0x001f4860` | `0x16360` | R |
| CAYMAN | Q7 | POOL | PERF | DRAM | `CAYMAN_Q7_POOL_PERF_DRAM_get` | `0x9b37c0` | `0x0020abc0` | `0x13200` | R |
| CAYMAN | Q7 | POOL | PERF | SRAM | `CAYMAN_Q7_POOL_PERF_SRAM_get` | `0x9b37e0` | `0x0021ddc0` | `0x0` | C |
| CAYMAN | Q7 | POOL | PERF | EXTRAM | `CAYMAN_Q7_POOL_PERF_EXTRAM_get` | `0x9b3800` | `0x0021ddc0` | `0x0` | C |
| CAYMAN | Q7 | POOL | PERF | EXTISA_0_SO | `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` | `0x9b3aa0` | `0x002ef7e0` | `0xa260` | R |
| CAYMAN | Q7 | POOL | PERF | EXTISA_0_JSON | `CAYMAN_Q7_POOL_PERF_EXTISA_0_JSON_get` | `0x9b3ac0` | `0x002f9a40` | `0x20` | R |
| CAYMAN | Q7 | POOL | PERF | EXTISA_1_SO | `CAYMAN_Q7_POOL_PERF_EXTISA_1_SO_get` | `0x9b3ae0` | `0x002f9a60` | `0xf5c` | R |
| CAYMAN | Q7 | POOL | PERF | EXTISA_1_JSON | `CAYMAN_Q7_POOL_PERF_EXTISA_1_JSON_get` | `0x9b3b00` | `0x002fa9c0` | `0x20` | R |
| CAYMAN | Q7 | POOL | PERF | EXTISA_2_SO | `CAYMAN_Q7_POOL_PERF_EXTISA_2_SO_get` | `0x9b3b20` | `0x002fa9e0` | `0x1500` | R |
| CAYMAN | Q7 | POOL | PERF | EXTISA_2_JSON | `CAYMAN_Q7_POOL_PERF_EXTISA_2_JSON_get` | `0x9b3b40` | `0x002fbee0` | `0x20` | R |
| CAYMAN | Q7 | POOL | PERF | EXTISA_3_SO | `CAYMAN_Q7_POOL_PERF_EXTISA_3_SO_get` | `0x9b3b60` | `0x002fbf00` | `0x6974` | R |
| CAYMAN | Q7 | POOL | PERF | EXTISA_3_JSON | `CAYMAN_Q7_POOL_PERF_EXTISA_3_JSON_get` | `0x9b3b80` | `0x00302880` | `0x20` | R |
| CAYMAN | Q7 | POOL | TEST | IRAM | `CAYMAN_Q7_POOL_TEST_IRAM_get` | `0x9b3820` | `0x0021ddc0` | `0x17d60` | R |
| CAYMAN | Q7 | POOL | TEST | DRAM | `CAYMAN_Q7_POOL_TEST_DRAM_get` | `0x9b3840` | `0x00235b20` | `0x13500` | R |
| CAYMAN | Q7 | POOL | TEST | SRAM | `CAYMAN_Q7_POOL_TEST_SRAM_get` | `0x9b3860` | `0x00249020` | `0x0` | C |
| CAYMAN | Q7 | POOL | TEST | EXTRAM | `CAYMAN_Q7_POOL_TEST_EXTRAM_get` | `0x9b3880` | `0x00249020` | `0x0` | C |
| CAYMAN | Q7 | POOL | DKL_DEBUG | IRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_IRAM_get` | `0x9b39a0` | `0x002a1480` | `0x13fc0` | R |
| CAYMAN | Q7 | POOL | DKL_DEBUG | DRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_DRAM_get` | `0x9b39c0` | `0x002b5440` | `0x16680` | R |
| CAYMAN | Q7 | POOL | DKL_DEBUG | SRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_SRAM_get` | `0x9b39e0` | `0x002cbac0` | `0x0` | C |
| CAYMAN | Q7 | POOL | DKL_DEBUG | EXTRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_EXTRAM_get` | `0x9b3a00` | `0x002cbac0` | `0x0` | C |
| CAYMAN | Q7 | POOL | DKL_PERF | IRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_IRAM_get` | `0x9b3920` | `0x0027d760` | `0x10120` | R |
| CAYMAN | Q7 | POOL | DKL_PERF | DRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_DRAM_get` | `0x9b3940` | `0x0028d880` | `0x13c00` | R |
| CAYMAN | Q7 | POOL | DKL_PERF | SRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_SRAM_get` | `0x9b3960` | `0x002a1480` | `0x0` | C |
| CAYMAN | Q7 | POOL | DKL_PERF | EXTRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_EXTRAM_get` | `0x9b3980` | `0x002a1480` | `0x0` | C |
| CAYMAN | Q7 | POOL | DKL_TEST | IRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_IRAM_get` | `0x9b3a20` | `0x002cbac0` | `0x10120` | R |
| CAYMAN | Q7 | POOL | DKL_TEST | DRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_DRAM_get` | `0x9b3a40` | `0x002dbbe0` | `0x13c00` | R |
| CAYMAN | Q7 | POOL | DKL_TEST | SRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_SRAM_get` | `0x9b3a60` | `0x002ef7e0` | `0x0` | C |
| CAYMAN | Q7 | POOL | DKL_TEST | EXTRAM | `CAYMAN_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_EXTRAM_get` | `0x9b3a80` | `0x002ef7e0` | `0x0` | C |

### 6.3 MARIANA (100)

> **NOTE.** MARIANA and MARIANA_PLUS POOL ucode is byte-identical to each other
> (distinct gen labels over the same shipped kernels). The two are separate
> `image_list` slot ranges with their own getter blobs in `.rodata`, but the POOL
> kernel bytes match.

| GEN | CLS | ENG | VAR | REG | SYMBOL | ACCESSOR | IMG-PTR | SIZE | cls |
|---|---|---|---|---|---|---|---|---|---|
| MARIANA | NX | ACT | DEBUG | IRAM | `MARIANA_NX_ACT_DEBUG_IRAM_get` | `0x9b41a0` | `0x003ea060` | `0x18ba0` | R |
| MARIANA | NX | ACT | DEBUG | DRAM | `MARIANA_NX_ACT_DEBUG_DRAM_get` | `0x9b41c0` | `0x00402c00` | `0x63c0` | R |
| MARIANA | NX | ACT | DEBUG | SRAM | `MARIANA_NX_ACT_DEBUG_SRAM_get` | `0x9b41e0` | `0x00408fc0` | `0x0` | C |
| MARIANA | NX | ACT | DEBUG | EXTRAM | `MARIANA_NX_ACT_DEBUG_EXTRAM_get` | `0x9b4200` | `0x00408fc0` | `0x0` | C |
| MARIANA | NX | ACT | PERF | IRAM | `MARIANA_NX_ACT_PERF_IRAM_get` | `0x9b3ca0` | `0x0030b8a0` | `0x11180` | R |
| MARIANA | NX | ACT | PERF | DRAM | `MARIANA_NX_ACT_PERF_DRAM_get` | `0x9b3cc0` | `0x0031ca20` | `0x2ba0` | R |
| MARIANA | NX | ACT | PERF | SRAM | `MARIANA_NX_ACT_PERF_SRAM_get` | `0x9b3ce0` | `0x0031f5c0` | `0x0` | C |
| MARIANA | NX | ACT | PERF | EXTRAM | `MARIANA_NX_ACT_PERF_EXTRAM_get` | `0x9b3d00` | `0x0031f5c0` | `0x0` | C |
| MARIANA | NX | ACT | PROF | CAM | `MARIANA_NX_ACT_PROF_CAM_get` | `0x9b4820` | `0x0059c480` | `0x400` | R |
| MARIANA | NX | ACT | PROF | TABLE | `MARIANA_NX_ACT_PROF_TABLE_get` | `0x9b4840` | `0x0059c880` | `0x2000` | R |
| MARIANA | NX | ACT | TEST | IRAM | `MARIANA_NX_ACT_TEST_IRAM_get` | `0x9b3f20` | `0x0037afe0` | `0x11240` | R |
| MARIANA | NX | ACT | TEST | DRAM | `MARIANA_NX_ACT_TEST_DRAM_get` | `0x9b3f40` | `0x0038c220` | `0x2e60` | R |
| MARIANA | NX | ACT | TEST | SRAM | `MARIANA_NX_ACT_TEST_SRAM_get` | `0x9b3f60` | `0x0038f080` | `0x0` | C |
| MARIANA | NX | ACT | TEST | EXTRAM | `MARIANA_NX_ACT_TEST_EXTRAM_get` | `0x9b3f80` | `0x0038f080` | `0x0` | C |
| MARIANA | NX | DVE | DEBUG | IRAM | `MARIANA_NX_DVE_DEBUG_IRAM_get` | `0x9b4220` | `0x00408fc0` | `0x1c560` | R |
| MARIANA | NX | DVE | DEBUG | DRAM | `MARIANA_NX_DVE_DEBUG_DRAM_get` | `0x9b4240` | `0x00425520` | `0x7000` | R |
| MARIANA | NX | DVE | DEBUG | SRAM | `MARIANA_NX_DVE_DEBUG_SRAM_get` | `0x9b4260` | `0x0042c520` | `0x0` | C |
| MARIANA | NX | DVE | DEBUG | EXTRAM | `MARIANA_NX_DVE_DEBUG_EXTRAM_get` | `0x9b4280` | `0x0042c520` | `0x0` | C |
| MARIANA | NX | DVE | PERF | IRAM | `MARIANA_NX_DVE_PERF_IRAM_get` | `0x9b3d20` | `0x0031f5c0` | `0x13540` | R |
| MARIANA | NX | DVE | PERF | DRAM | `MARIANA_NX_DVE_PERF_DRAM_get` | `0x9b3d40` | `0x00332b00` | `0x31a0` | R |
| MARIANA | NX | DVE | PERF | SRAM | `MARIANA_NX_DVE_PERF_SRAM_get` | `0x9b3d60` | `0x00335ca0` | `0x0` | C |
| MARIANA | NX | DVE | PERF | EXTRAM | `MARIANA_NX_DVE_PERF_EXTRAM_get` | `0x9b3d80` | `0x00335ca0` | `0x0` | C |
| MARIANA | NX | DVE | PROF | CAM | `MARIANA_NX_DVE_PROF_CAM_get` | `0x9b4860` | `0x0059e880` | `0x400` | R |
| MARIANA | NX | DVE | PROF | TABLE | `MARIANA_NX_DVE_PROF_TABLE_get` | `0x9b4880` | `0x0059ec80` | `0x2000` | R |
| MARIANA | NX | DVE | TEST | IRAM | `MARIANA_NX_DVE_TEST_IRAM_get` | `0x9b3fa0` | `0x0038f080` | `0x13560` | R |
| MARIANA | NX | DVE | TEST | DRAM | `MARIANA_NX_DVE_TEST_DRAM_get` | `0x9b3fc0` | `0x003a25e0` | `0x34e0` | R |
| MARIANA | NX | DVE | TEST | SRAM | `MARIANA_NX_DVE_TEST_SRAM_get` | `0x9b3fe0` | `0x003a5ac0` | `0x0` | C |
| MARIANA | NX | DVE | TEST | EXTRAM | `MARIANA_NX_DVE_TEST_EXTRAM_get` | `0x9b4000` | `0x003a5ac0` | `0x0` | C |
| MARIANA | NX | PE | DEBUG | IRAM | `MARIANA_NX_PE_DEBUG_IRAM_get` | `0x9b42a0` | `0x0042c520` | `0x18c20` | R |
| MARIANA | NX | PE | DEBUG | DRAM | `MARIANA_NX_PE_DEBUG_DRAM_get` | `0x9b42c0` | `0x00445140` | `0x6400` | R |
| MARIANA | NX | PE | DEBUG | SRAM | `MARIANA_NX_PE_DEBUG_SRAM_get` | `0x9b42e0` | `0x0044b540` | `0x0` | C |
| MARIANA | NX | PE | DEBUG | EXTRAM | `MARIANA_NX_PE_DEBUG_EXTRAM_get` | `0x9b4300` | `0x0044b540` | `0x0` | C |
| MARIANA | NX | PE | PERF | IRAM | `MARIANA_NX_PE_PERF_IRAM_get` | `0x9b3da0` | `0x00335ca0` | `0x12ce0` | R |
| MARIANA | NX | PE | PERF | DRAM | `MARIANA_NX_PE_PERF_DRAM_get` | `0x9b3dc0` | `0x00348980` | `0x2da0` | R |
| MARIANA | NX | PE | PERF | SRAM | `MARIANA_NX_PE_PERF_SRAM_get` | `0x9b3de0` | `0x0034b720` | `0x0` | C |
| MARIANA | NX | PE | PERF | EXTRAM | `MARIANA_NX_PE_PERF_EXTRAM_get` | `0x9b3e00` | `0x0034b720` | `0x0` | C |
| MARIANA | NX | PE | PROF | CAM | `MARIANA_NX_PE_PROF_CAM_get` | `0x9b48a0` | `0x005a0c80` | `0x400` | R |
| MARIANA | NX | PE | PROF | TABLE | `MARIANA_NX_PE_PROF_TABLE_get` | `0x9b48c0` | `0x005a1080` | `0x2000` | R |
| MARIANA | NX | PE | TEST | IRAM | `MARIANA_NX_PE_TEST_IRAM_get` | `0x9b4020` | `0x003a5ac0` | `0x12ca0` | R |
| MARIANA | NX | PE | TEST | DRAM | `MARIANA_NX_PE_TEST_DRAM_get` | `0x9b4040` | `0x003b8760` | `0x30e0` | R |
| MARIANA | NX | PE | TEST | SRAM | `MARIANA_NX_PE_TEST_SRAM_get` | `0x9b4060` | `0x003bb840` | `0x0` | C |
| MARIANA | NX | PE | TEST | EXTRAM | `MARIANA_NX_PE_TEST_EXTRAM_get` | `0x9b4080` | `0x003bb840` | `0x0` | C |
| MARIANA | NX | POOL | DEBUG | IRAM | `MARIANA_NX_POOL_DEBUG_IRAM_get` | `0x9b4320` | `0x0044b540` | `0x1c080` | R |
| MARIANA | NX | POOL | DEBUG | DRAM | `MARIANA_NX_POOL_DEBUG_DRAM_get` | `0x9b4340` | `0x004675c0` | `0x7000` | R |
| MARIANA | NX | POOL | DEBUG | SRAM | `MARIANA_NX_POOL_DEBUG_SRAM_get` | `0x9b4360` | `0x0046e5c0` | `0x0` | C |
| MARIANA | NX | POOL | DEBUG | EXTRAM | `MARIANA_NX_POOL_DEBUG_EXTRAM_get` | `0x9b4380` | `0x0046e5c0` | `0x0` | C |
| MARIANA | NX | POOL | PERF | IRAM | `MARIANA_NX_POOL_PERF_IRAM_get` | `0x9b3e20` | `0x0034b720` | `0x14520` | R |
| MARIANA | NX | POOL | PERF | DRAM | `MARIANA_NX_POOL_PERF_DRAM_get` | `0x9b3e40` | `0x0035fc40` | `0x3180` | R |
| MARIANA | NX | POOL | PERF | SRAM | `MARIANA_NX_POOL_PERF_SRAM_get` | `0x9b3e60` | `0x00362dc0` | `0x0` | C |
| MARIANA | NX | POOL | PERF | EXTRAM | `MARIANA_NX_POOL_PERF_EXTRAM_get` | `0x9b3e80` | `0x00362dc0` | `0x0` | C |
| MARIANA | NX | POOL | PROF | CAM | `MARIANA_NX_POOL_PROF_CAM_get` | `0x9b48e0` | `0x005a3080` | `0x400` | R |
| MARIANA | NX | POOL | PROF | TABLE | `MARIANA_NX_POOL_PROF_TABLE_get` | `0x9b4900` | `0x005a3480` | `0x2000` | R |
| MARIANA | NX | POOL | TEST | IRAM | `MARIANA_NX_POOL_TEST_IRAM_get` | `0x9b40a0` | `0x003bb840` | `0x14240` | R |
| MARIANA | NX | POOL | TEST | DRAM | `MARIANA_NX_POOL_TEST_DRAM_get` | `0x9b40c0` | `0x003cfa80` | `0x3480` | R |
| MARIANA | NX | POOL | TEST | SRAM | `MARIANA_NX_POOL_TEST_SRAM_get` | `0x9b40e0` | `0x003d2f00` | `0x0` | C |
| MARIANA | NX | POOL | TEST | EXTRAM | `MARIANA_NX_POOL_TEST_EXTRAM_get` | `0x9b4100` | `0x003d2f00` | `0x0` | C |
| MARIANA | NX | SP | DEBUG | IRAM | `MARIANA_NX_SP_DEBUG_IRAM_get` | `0x9b43a0` | `0x0046e5c0` | `0x190e0` | R |
| MARIANA | NX | SP | DEBUG | DRAM | `MARIANA_NX_SP_DEBUG_DRAM_get` | `0x9b43c0` | `0x004876a0` | `0x6440` | R |
| MARIANA | NX | SP | DEBUG | SRAM | `MARIANA_NX_SP_DEBUG_SRAM_get` | `0x9b43e0` | `0x0048dae0` | `0x0` | C |
| MARIANA | NX | SP | DEBUG | EXTRAM | `MARIANA_NX_SP_DEBUG_EXTRAM_get` | `0x9b4400` | `0x0048dae0` | `0x0` | C |
| MARIANA | NX | SP | PERF | IRAM | `MARIANA_NX_SP_PERF_IRAM_get` | `0x9b3ea0` | `0x00362dc0` | `0x153c0` | R |
| MARIANA | NX | SP | PERF | DRAM | `MARIANA_NX_SP_PERF_DRAM_get` | `0x9b3ec0` | `0x00378180` | `0x2e60` | R |
| MARIANA | NX | SP | PERF | SRAM | `MARIANA_NX_SP_PERF_SRAM_get` | `0x9b3ee0` | `0x0037afe0` | `0x0` | C |
| MARIANA | NX | SP | PERF | EXTRAM | `MARIANA_NX_SP_PERF_EXTRAM_get` | `0x9b3f00` | `0x0037afe0` | `0x0` | C |
| MARIANA | NX | SP | TEST | IRAM | `MARIANA_NX_SP_TEST_IRAM_get` | `0x9b4120` | `0x003d2f00` | `0x14000` | R |
| MARIANA | NX | SP | TEST | DRAM | `MARIANA_NX_SP_TEST_DRAM_get` | `0x9b4140` | `0x003e6f00` | `0x3160` | R |
| MARIANA | NX | SP | TEST | SRAM | `MARIANA_NX_SP_TEST_SRAM_get` | `0x9b4160` | `0x003ea060` | `0x0` | C |
| MARIANA | NX | SP | TEST | EXTRAM | `MARIANA_NX_SP_TEST_EXTRAM_get` | `0x9b4180` | `0x003ea060` | `0x0` | C |
| MARIANA | Q7 | POOL | DEBUG | IRAM | `MARIANA_Q7_POOL_DEBUG_IRAM_get` | `0x9b4520` | `0x004e2440` | `0x1ed40` | R |
| MARIANA | Q7 | POOL | DEBUG | DRAM | `MARIANA_Q7_POOL_DEBUG_DRAM_get` | `0x9b4540` | `0x00501180` | `0x15d80` | R |
| MARIANA | Q7 | POOL | DEBUG | SRAM | `MARIANA_Q7_POOL_DEBUG_SRAM_get` | `0x9b4560` | `0x00516f00` | `0x0` | C |
| MARIANA | Q7 | POOL | DEBUG | EXTRAM | `MARIANA_Q7_POOL_DEBUG_EXTRAM_get` | `0x9b4580` | `0x00516f00` | `0x0` | C |
| MARIANA | Q7 | POOL | PERF | IRAM | `MARIANA_Q7_POOL_PERF_IRAM_get` | `0x9b4420` | `0x0048dae0` | `0x164e0` | R |
| MARIANA | Q7 | POOL | PERF | DRAM | `MARIANA_Q7_POOL_PERF_DRAM_get` | `0x9b4440` | `0x004a3fc0` | `0x13180` | R |
| MARIANA | Q7 | POOL | PERF | SRAM | `MARIANA_Q7_POOL_PERF_SRAM_get` | `0x9b4460` | `0x004b7140` | `0x0` | C |
| MARIANA | Q7 | POOL | PERF | EXTRAM | `MARIANA_Q7_POOL_PERF_EXTRAM_get` | `0x9b4480` | `0x004b7140` | `0x0` | C |
| MARIANA | Q7 | POOL | PERF | EXTISA_0_SO | `MARIANA_Q7_POOL_PERF_EXTISA_0_SO_get` | `0x9b4720` | `0x005893c0` | `0xa260` | R |
| MARIANA | Q7 | POOL | PERF | EXTISA_0_JSON | `MARIANA_Q7_POOL_PERF_EXTISA_0_JSON_get` | `0x9b4740` | `0x00593620` | `0x20` | R |
| MARIANA | Q7 | POOL | PERF | EXTISA_1_SO | `MARIANA_Q7_POOL_PERF_EXTISA_1_SO_get` | `0x9b4760` | `0x00593640` | `0xf5c` | R |
| MARIANA | Q7 | POOL | PERF | EXTISA_1_JSON | `MARIANA_Q7_POOL_PERF_EXTISA_1_JSON_get` | `0x9b4780` | `0x005945a0` | `0x20` | R |
| MARIANA | Q7 | POOL | PERF | EXTISA_2_SO | `MARIANA_Q7_POOL_PERF_EXTISA_2_SO_get` | `0x9b47a0` | `0x005945c0` | `0x1500` | R |
| MARIANA | Q7 | POOL | PERF | EXTISA_2_JSON | `MARIANA_Q7_POOL_PERF_EXTISA_2_JSON_get` | `0x9b47c0` | `0x00595ac0` | `0x20` | R |
| MARIANA | Q7 | POOL | PERF | EXTISA_3_SO | `MARIANA_Q7_POOL_PERF_EXTISA_3_SO_get` | `0x9b47e0` | `0x00595ae0` | `0x6974` | R |
| MARIANA | Q7 | POOL | PERF | EXTISA_3_JSON | `MARIANA_Q7_POOL_PERF_EXTISA_3_JSON_get` | `0x9b4800` | `0x0059c460` | `0x20` | R |
| MARIANA | Q7 | POOL | TEST | IRAM | `MARIANA_Q7_POOL_TEST_IRAM_get` | `0x9b44a0` | `0x004b7140` | `0x17e80` | R |
| MARIANA | Q7 | POOL | TEST | DRAM | `MARIANA_Q7_POOL_TEST_DRAM_get` | `0x9b44c0` | `0x004cefc0` | `0x13480` | R |
| MARIANA | Q7 | POOL | TEST | SRAM | `MARIANA_Q7_POOL_TEST_SRAM_get` | `0x9b44e0` | `0x004e2440` | `0x0` | C |
| MARIANA | Q7 | POOL | TEST | EXTRAM | `MARIANA_Q7_POOL_TEST_EXTRAM_get` | `0x9b4500` | `0x004e2440` | `0x0` | C |
| MARIANA | Q7 | POOL | DKL_DEBUG | IRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_IRAM_get` | `0x9b4620` | `0x0053ad40` | `0x14140` | R |
| MARIANA | Q7 | POOL | DKL_DEBUG | DRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_DRAM_get` | `0x9b4640` | `0x0054ee80` | `0x16700` | R |
| MARIANA | Q7 | POOL | DKL_DEBUG | SRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_SRAM_get` | `0x9b4660` | `0x00565580` | `0x0` | C |
| MARIANA | Q7 | POOL | DKL_DEBUG | EXTRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_EXTRAM_get` | `0x9b4680` | `0x00565580` | `0x0` | C |
| MARIANA | Q7 | POOL | DKL_PERF | IRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_IRAM_get` | `0x9b45a0` | `0x00516f00` | `0x101c0` | R |
| MARIANA | Q7 | POOL | DKL_PERF | DRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_DRAM_get` | `0x9b45c0` | `0x005270c0` | `0x13c80` | R |
| MARIANA | Q7 | POOL | DKL_PERF | SRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_SRAM_get` | `0x9b45e0` | `0x0053ad40` | `0x0` | C |
| MARIANA | Q7 | POOL | DKL_PERF | EXTRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_EXTRAM_get` | `0x9b4600` | `0x0053ad40` | `0x0` | C |
| MARIANA | Q7 | POOL | DKL_TEST | IRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_IRAM_get` | `0x9b46a0` | `0x00565580` | `0x101c0` | R |
| MARIANA | Q7 | POOL | DKL_TEST | DRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_DRAM_get` | `0x9b46c0` | `0x00575740` | `0x13c80` | R |
| MARIANA | Q7 | POOL | DKL_TEST | SRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_SRAM_get` | `0x9b46e0` | `0x005893c0` | `0x0` | C |
| MARIANA | Q7 | POOL | DKL_TEST | EXTRAM | `MARIANA_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_EXTRAM_get` | `0x9b4700` | `0x005893c0` | `0x0` | C |

### 6.4 MARIANA_PLUS (100 — same shape as CAYMAN/MARIANA)

| GEN | CLS | ENG | VAR | REG | SYMBOL | ACCESSOR | IMG-PTR | SIZE | cls |
|---|---|---|---|---|---|---|---|---|---|
| MARIANA_PLUS | NX | ACT | DEBUG | IRAM | `MARIANA_PLUS_NX_ACT_DEBUG_IRAM_get` | `0x9b4e20` | `0x006af7e0` | `0x19da0` | R |
| MARIANA_PLUS | NX | ACT | DEBUG | DRAM | `MARIANA_PLUS_NX_ACT_DEBUG_DRAM_get` | `0x9b4e40` | `0x006c9580` | `0x6560` | R |
| MARIANA_PLUS | NX | ACT | DEBUG | SRAM | `MARIANA_PLUS_NX_ACT_DEBUG_SRAM_get` | `0x9b4e60` | `0x006cfae0` | `0x0` | C |
| MARIANA_PLUS | NX | ACT | DEBUG | EXTRAM | `MARIANA_PLUS_NX_ACT_DEBUG_EXTRAM_get` | `0x9b4e80` | `0x006cfae0` | `0x0` | C |
| MARIANA_PLUS | NX | ACT | PERF | IRAM | `MARIANA_PLUS_NX_ACT_PERF_IRAM_get` | `0x9b4920` | `0x005a5480` | `0x14be0` | R |
| MARIANA_PLUS | NX | ACT | PERF | DRAM | `MARIANA_PLUS_NX_ACT_PERF_DRAM_get` | `0x9b4940` | `0x005ba060` | `0x2cc0` | R |
| MARIANA_PLUS | NX | ACT | PERF | SRAM | `MARIANA_PLUS_NX_ACT_PERF_SRAM_get` | `0x9b4960` | `0x005bcd20` | `0x0` | C |
| MARIANA_PLUS | NX | ACT | PERF | EXTRAM | `MARIANA_PLUS_NX_ACT_PERF_EXTRAM_get` | `0x9b4980` | `0x005bcd20` | `0x0` | C |
| MARIANA_PLUS | NX | ACT | PROF | CAM | `MARIANA_PLUS_NX_ACT_PROF_CAM_get` | `0x9b54a0` | `0x00868300` | `0x400` | R |
| MARIANA_PLUS | NX | ACT | PROF | TABLE | `MARIANA_PLUS_NX_ACT_PROF_TABLE_get` | `0x9b54c0` | `0x00868700` | `0x2000` | R |
| MARIANA_PLUS | NX | ACT | TEST | IRAM | `MARIANA_PLUS_NX_ACT_TEST_IRAM_get` | `0x9b4ba0` | `0x0062b180` | `0x14a80` | R |
| MARIANA_PLUS | NX | ACT | TEST | DRAM | `MARIANA_PLUS_NX_ACT_TEST_DRAM_get` | `0x9b4bc0` | `0x0063fc00` | `0x3020` | R |
| MARIANA_PLUS | NX | ACT | TEST | SRAM | `MARIANA_PLUS_NX_ACT_TEST_SRAM_get` | `0x9b4be0` | `0x00642c20` | `0x0` | C |
| MARIANA_PLUS | NX | ACT | TEST | EXTRAM | `MARIANA_PLUS_NX_ACT_TEST_EXTRAM_get` | `0x9b4c00` | `0x00642c20` | `0x0` | C |
| MARIANA_PLUS | NX | DVE | DEBUG | IRAM | `MARIANA_PLUS_NX_DVE_DEBUG_IRAM_get` | `0x9b4ea0` | `0x006cfae0` | `0x1d760` | R |
| MARIANA_PLUS | NX | DVE | DEBUG | DRAM | `MARIANA_PLUS_NX_DVE_DEBUG_DRAM_get` | `0x9b4ec0` | `0x006ed240` | `0x7160` | R |
| MARIANA_PLUS | NX | DVE | DEBUG | SRAM | `MARIANA_PLUS_NX_DVE_DEBUG_SRAM_get` | `0x9b4ee0` | `0x006f43a0` | `0x0` | C |
| MARIANA_PLUS | NX | DVE | DEBUG | EXTRAM | `MARIANA_PLUS_NX_DVE_DEBUG_EXTRAM_get` | `0x9b4f00` | `0x006f43a0` | `0x0` | C |
| MARIANA_PLUS | NX | DVE | PERF | IRAM | `MARIANA_PLUS_NX_DVE_PERF_IRAM_get` | `0x9b49a0` | `0x005bcd20` | `0x16e80` | R |
| MARIANA_PLUS | NX | DVE | PERF | DRAM | `MARIANA_PLUS_NX_DVE_PERF_DRAM_get` | `0x9b49c0` | `0x005d3ba0` | `0x32c0` | R |
| MARIANA_PLUS | NX | DVE | PERF | SRAM | `MARIANA_PLUS_NX_DVE_PERF_SRAM_get` | `0x9b49e0` | `0x005d6e60` | `0x0` | C |
| MARIANA_PLUS | NX | DVE | PERF | EXTRAM | `MARIANA_PLUS_NX_DVE_PERF_EXTRAM_get` | `0x9b4a00` | `0x005d6e60` | `0x0` | C |
| MARIANA_PLUS | NX | DVE | PROF | CAM | `MARIANA_PLUS_NX_DVE_PROF_CAM_get` | `0x9b54e0` | `0x0086a700` | `0x400` | R |
| MARIANA_PLUS | NX | DVE | PROF | TABLE | `MARIANA_PLUS_NX_DVE_PROF_TABLE_get` | `0x9b5500` | `0x0086ab00` | `0x2000` | R |
| MARIANA_PLUS | NX | DVE | TEST | IRAM | `MARIANA_PLUS_NX_DVE_TEST_IRAM_get` | `0x9b4c20` | `0x00642c20` | `0x16be0` | R |
| MARIANA_PLUS | NX | DVE | TEST | DRAM | `MARIANA_PLUS_NX_DVE_TEST_DRAM_get` | `0x9b4c40` | `0x00659800` | `0x3660` | R |
| MARIANA_PLUS | NX | DVE | TEST | SRAM | `MARIANA_PLUS_NX_DVE_TEST_SRAM_get` | `0x9b4c60` | `0x0065ce60` | `0x0` | C |
| MARIANA_PLUS | NX | DVE | TEST | EXTRAM | `MARIANA_PLUS_NX_DVE_TEST_EXTRAM_get` | `0x9b4c80` | `0x0065ce60` | `0x0` | C |
| MARIANA_PLUS | NX | PE | DEBUG | IRAM | `MARIANA_PLUS_NX_PE_DEBUG_IRAM_get` | `0x9b4f20` | `0x006f43a0` | `0x19e00` | R |
| MARIANA_PLUS | NX | PE | DEBUG | DRAM | `MARIANA_PLUS_NX_PE_DEBUG_DRAM_get` | `0x9b4f40` | `0x0070e1a0` | `0x6560` | R |
| MARIANA_PLUS | NX | PE | DEBUG | SRAM | `MARIANA_PLUS_NX_PE_DEBUG_SRAM_get` | `0x9b4f60` | `0x00714700` | `0x0` | C |
| MARIANA_PLUS | NX | PE | DEBUG | EXTRAM | `MARIANA_PLUS_NX_PE_DEBUG_EXTRAM_get` | `0x9b4f80` | `0x00714700` | `0x0` | C |
| MARIANA_PLUS | NX | PE | PERF | IRAM | `MARIANA_PLUS_NX_PE_PERF_IRAM_get` | `0x9b4a20` | `0x005d6e60` | `0x172c0` | R |
| MARIANA_PLUS | NX | PE | PERF | DRAM | `MARIANA_PLUS_NX_PE_PERF_DRAM_get` | `0x9b4a40` | `0x005ee120` | `0x2ec0` | R |
| MARIANA_PLUS | NX | PE | PERF | SRAM | `MARIANA_PLUS_NX_PE_PERF_SRAM_get` | `0x9b4a60` | `0x005f0fe0` | `0x0` | C |
| MARIANA_PLUS | NX | PE | PERF | EXTRAM | `MARIANA_PLUS_NX_PE_PERF_EXTRAM_get` | `0x9b4a80` | `0x005f0fe0` | `0x0` | C |
| MARIANA_PLUS | NX | PE | PROF | CAM | `MARIANA_PLUS_NX_PE_PROF_CAM_get` | `0x9b5520` | `0x0086cb00` | `0x400` | R |
| MARIANA_PLUS | NX | PE | PROF | TABLE | `MARIANA_PLUS_NX_PE_PROF_TABLE_get` | `0x9b5540` | `0x0086cf00` | `0x2000` | R |
| MARIANA_PLUS | NX | PE | TEST | IRAM | `MARIANA_PLUS_NX_PE_TEST_IRAM_get` | `0x9b4ca0` | `0x0065ce60` | `0x16de0` | R |
| MARIANA_PLUS | NX | PE | TEST | DRAM | `MARIANA_PLUS_NX_PE_TEST_DRAM_get` | `0x9b4cc0` | `0x00673c40` | `0x32a0` | R |
| MARIANA_PLUS | NX | PE | TEST | SRAM | `MARIANA_PLUS_NX_PE_TEST_SRAM_get` | `0x9b4ce0` | `0x00676ee0` | `0x0` | C |
| MARIANA_PLUS | NX | PE | TEST | EXTRAM | `MARIANA_PLUS_NX_PE_TEST_EXTRAM_get` | `0x9b4d00` | `0x00676ee0` | `0x0` | C |
| MARIANA_PLUS | NX | POOL | DEBUG | IRAM | `MARIANA_PLUS_NX_POOL_DEBUG_IRAM_get` | `0x9b4fa0` | `0x00714700` | `0x1d340` | R |
| MARIANA_PLUS | NX | POOL | DEBUG | DRAM | `MARIANA_PLUS_NX_POOL_DEBUG_DRAM_get` | `0x9b4fc0` | `0x00731a40` | `0x7160` | R |
| MARIANA_PLUS | NX | POOL | DEBUG | SRAM | `MARIANA_PLUS_NX_POOL_DEBUG_SRAM_get` | `0x9b4fe0` | `0x00738ba0` | `0x0` | C |
| MARIANA_PLUS | NX | POOL | DEBUG | EXTRAM | `MARIANA_PLUS_NX_POOL_DEBUG_EXTRAM_get` | `0x9b5000` | `0x00738ba0` | `0x0` | C |
| MARIANA_PLUS | NX | POOL | PERF | IRAM | `MARIANA_PLUS_NX_POOL_PERF_IRAM_get` | `0x9b4aa0` | `0x005f0fe0` | `0x17bc0` | R |
| MARIANA_PLUS | NX | POOL | PERF | DRAM | `MARIANA_PLUS_NX_POOL_PERF_DRAM_get` | `0x9b4ac0` | `0x00608ba0` | `0x32a0` | R |
| MARIANA_PLUS | NX | POOL | PERF | SRAM | `MARIANA_PLUS_NX_POOL_PERF_SRAM_get` | `0x9b4ae0` | `0x0060be40` | `0x0` | C |
| MARIANA_PLUS | NX | POOL | PERF | EXTRAM | `MARIANA_PLUS_NX_POOL_PERF_EXTRAM_get` | `0x9b4b00` | `0x0060be40` | `0x0` | C |
| MARIANA_PLUS | NX | POOL | PROF | CAM | `MARIANA_PLUS_NX_POOL_PROF_CAM_get` | `0x9b5560` | `0x0086ef00` | `0x400` | R |
| MARIANA_PLUS | NX | POOL | PROF | TABLE | `MARIANA_PLUS_NX_POOL_PROF_TABLE_get` | `0x9b5580` | `0x0086f300` | `0x2000` | R |
| MARIANA_PLUS | NX | POOL | TEST | IRAM | `MARIANA_PLUS_NX_POOL_TEST_IRAM_get` | `0x9b4d20` | `0x00676ee0` | `0x176a0` | R |
| MARIANA_PLUS | NX | POOL | TEST | DRAM | `MARIANA_PLUS_NX_POOL_TEST_DRAM_get` | `0x9b4d40` | `0x0068e580` | `0x3620` | R |
| MARIANA_PLUS | NX | POOL | TEST | SRAM | `MARIANA_PLUS_NX_POOL_TEST_SRAM_get` | `0x9b4d60` | `0x00691ba0` | `0x0` | C |
| MARIANA_PLUS | NX | POOL | TEST | EXTRAM | `MARIANA_PLUS_NX_POOL_TEST_EXTRAM_get` | `0x9b4d80` | `0x00691ba0` | `0x0` | C |
| MARIANA_PLUS | NX | SP | DEBUG | IRAM | `MARIANA_PLUS_NX_SP_DEBUG_IRAM_get` | `0x9b5020` | `0x00738ba0` | `0x1a3e0` | R |
| MARIANA_PLUS | NX | SP | DEBUG | DRAM | `MARIANA_PLUS_NX_SP_DEBUG_DRAM_get` | `0x9b5040` | `0x00752f80` | `0x6660` | R |
| MARIANA_PLUS | NX | SP | DEBUG | SRAM | `MARIANA_PLUS_NX_SP_DEBUG_SRAM_get` | `0x9b5060` | `0x007595e0` | `0x0` | C |
| MARIANA_PLUS | NX | SP | DEBUG | EXTRAM | `MARIANA_PLUS_NX_SP_DEBUG_EXTRAM_get` | `0x9b5080` | `0x007595e0` | `0x0` | C |
| MARIANA_PLUS | NX | SP | PERF | IRAM | `MARIANA_PLUS_NX_SP_PERF_IRAM_get` | `0x9b4b20` | `0x0060be40` | `0x1c300` | R |
| MARIANA_PLUS | NX | SP | PERF | DRAM | `MARIANA_PLUS_NX_SP_PERF_DRAM_get` | `0x9b4b40` | `0x00628140` | `0x3040` | R |
| MARIANA_PLUS | NX | SP | PERF | SRAM | `MARIANA_PLUS_NX_SP_PERF_SRAM_get` | `0x9b4b60` | `0x0062b180` | `0x0` | C |
| MARIANA_PLUS | NX | SP | PERF | EXTRAM | `MARIANA_PLUS_NX_SP_PERF_EXTRAM_get` | `0x9b4b80` | `0x0062b180` | `0x0` | C |
| MARIANA_PLUS | NX | SP | TEST | IRAM | `MARIANA_PLUS_NX_SP_TEST_IRAM_get` | `0x9b4da0` | `0x00691ba0` | `0x1a8a0` | R |
| MARIANA_PLUS | NX | SP | TEST | DRAM | `MARIANA_PLUS_NX_SP_TEST_DRAM_get` | `0x9b4dc0` | `0x006ac440` | `0x33a0` | R |
| MARIANA_PLUS | NX | SP | TEST | SRAM | `MARIANA_PLUS_NX_SP_TEST_SRAM_get` | `0x9b4de0` | `0x006af7e0` | `0x0` | C |
| MARIANA_PLUS | NX | SP | TEST | EXTRAM | `MARIANA_PLUS_NX_SP_TEST_EXTRAM_get` | `0x9b4e00` | `0x006af7e0` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DEBUG | IRAM | `MARIANA_PLUS_Q7_POOL_DEBUG_IRAM_get` | `0x9b51a0` | `0x007adf40` | `0x1ef00` | R |
| MARIANA_PLUS | Q7 | POOL | DEBUG | DRAM | `MARIANA_PLUS_Q7_POOL_DEBUG_DRAM_get` | `0x9b51c0` | `0x007cce40` | `0x15d80` | R |
| MARIANA_PLUS | Q7 | POOL | DEBUG | SRAM | `MARIANA_PLUS_Q7_POOL_DEBUG_SRAM_get` | `0x9b51e0` | `0x007e2bc0` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DEBUG | EXTRAM | `MARIANA_PLUS_Q7_POOL_DEBUG_EXTRAM_get` | `0x9b5200` | `0x007e2bc0` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | PERF | IRAM | `MARIANA_PLUS_Q7_POOL_PERF_IRAM_get` | `0x9b50a0` | `0x007595e0` | `0x164e0` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | DRAM | `MARIANA_PLUS_Q7_POOL_PERF_DRAM_get` | `0x9b50c0` | `0x0076fac0` | `0x13180` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | SRAM | `MARIANA_PLUS_Q7_POOL_PERF_SRAM_get` | `0x9b50e0` | `0x00782c40` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTRAM | `MARIANA_PLUS_Q7_POOL_PERF_EXTRAM_get` | `0x9b5100` | `0x00782c40` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_0_SO | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_0_SO_get` | `0x9b53a0` | `0x00855240` | `0xa260` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_0_JSON | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_0_JSON_get` | `0x9b53c0` | `0x0085f4a0` | `0x20` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_1_SO | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_1_SO_get` | `0x9b53e0` | `0x0085f4c0` | `0xf5c` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_1_JSON | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_1_JSON_get` | `0x9b5400` | `0x00860420` | `0x20` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_2_SO | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_2_SO_get` | `0x9b5420` | `0x00860440` | `0x1500` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_2_JSON | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_2_JSON_get` | `0x9b5440` | `0x00861940` | `0x20` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_3_SO | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_3_SO_get` | `0x9b5460` | `0x00861960` | `0x6974` | R |
| MARIANA_PLUS | Q7 | POOL | PERF | EXTISA_3_JSON | `MARIANA_PLUS_Q7_POOL_PERF_EXTISA_3_JSON_get` | `0x9b5480` | `0x008682e0` | `0x20` | R |
| MARIANA_PLUS | Q7 | POOL | TEST | IRAM | `MARIANA_PLUS_Q7_POOL_TEST_IRAM_get` | `0x9b5120` | `0x00782c40` | `0x17e80` | R |
| MARIANA_PLUS | Q7 | POOL | TEST | DRAM | `MARIANA_PLUS_Q7_POOL_TEST_DRAM_get` | `0x9b5140` | `0x0079aac0` | `0x13480` | R |
| MARIANA_PLUS | Q7 | POOL | TEST | SRAM | `MARIANA_PLUS_Q7_POOL_TEST_SRAM_get` | `0x9b5160` | `0x007adf40` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | TEST | EXTRAM | `MARIANA_PLUS_Q7_POOL_TEST_EXTRAM_get` | `0x9b5180` | `0x007adf40` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DKL_DEBUG | IRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_IRAM_get` | `0x9b52a0` | `0x00806a00` | `0x14300` | R |
| MARIANA_PLUS | Q7 | POOL | DKL_DEBUG | DRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_DRAM_get` | `0x9b52c0` | `0x0081ad00` | `0x16700` | R |
| MARIANA_PLUS | Q7 | POOL | DKL_DEBUG | SRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_SRAM_get` | `0x9b52e0` | `0x00831400` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DKL_DEBUG | EXTRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_DEBUG_EXTRAM_get` | `0x9b5300` | `0x00831400` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DKL_PERF | IRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_IRAM_get` | `0x9b5220` | `0x007e2bc0` | `0x101c0` | R |
| MARIANA_PLUS | Q7 | POOL | DKL_PERF | DRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_DRAM_get` | `0x9b5240` | `0x007f2d80` | `0x13c80` | R |
| MARIANA_PLUS | Q7 | POOL | DKL_PERF | SRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_SRAM_get` | `0x9b5260` | `0x00806a00` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DKL_PERF | EXTRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_PERF_EXTRAM_get` | `0x9b5280` | `0x00806a00` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DKL_TEST | IRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_IRAM_get` | `0x9b5320` | `0x00831400` | `0x101c0` | R |
| MARIANA_PLUS | Q7 | POOL | DKL_TEST | DRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_DRAM_get` | `0x9b5340` | `0x008415c0` | `0x13c80` | R |
| MARIANA_PLUS | Q7 | POOL | DKL_TEST | SRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_SRAM_get` | `0x9b5360` | `0x00855240` | `0x0` | C |
| MARIANA_PLUS | Q7 | POOL | DKL_TEST | EXTRAM | `MARIANA_PLUS_Q7_POOL_DYNAMIC_KERNEL_LOAD_TEST_EXTRAM_get` | `0x9b5380` | `0x00855240` | `0x0` | C |

### 6.5 MAVERICK (62 — no NX_ACT, no DEBUG except DVE/Q7_POOL, no DKL)

> **WALL — v5 is header-OBSERVED, interiors INFERRED.** MAVERICK getter *bytes*
> are present and carved here, but the v5 firmware *interior* semantics (instruction
> layout, `S:`/`P%i:` string structure beyond the carve) are INFERRED from the v2–v4
> precedent, not byte-grounded the way SUNDA/CAYMAN/MARIANA are. The catalog facts
> (symbol, ptr, size, real-vs-cursor) are HIGH/OBSERVED; the firmware *meaning* of a
> MAVERICK blob is `[MED/INFERRED]`.

| GEN | CLS | ENG | VAR | REG | SYMBOL | ACCESSOR | IMG-PTR | SIZE | cls |
|---|---|---|---|---|---|---|---|---|---|
| MAVERICK | NX | DVE | DEBUG | IRAM | `MAVERICK_NX_DVE_DEBUG_IRAM_get` | `0x9b56a0` | `0x008945c0` | `0x19000` | R |
| MAVERICK | NX | DVE | DEBUG | DRAM | `MAVERICK_NX_DVE_DEBUG_DRAM_get` | `0x9b56c0` | `0x008ad5c0` | `0x5f80` | R |
| MAVERICK | NX | DVE | DEBUG | SRAM | `MAVERICK_NX_DVE_DEBUG_SRAM_get` | `0x9b56e0` | `0x008b3540` | `0x0` | C |
| MAVERICK | NX | DVE | DEBUG | EXTRAM | `MAVERICK_NX_DVE_DEBUG_EXTRAM_get` | `0x9b5700` | `0x008b3540` | `0x0` | C |
| MAVERICK | NX | DVE | PERF | IRAM | `MAVERICK_NX_DVE_PERF_IRAM_get` | `0x9b55a0` | `0x00871300` | `0xec00` | R |
| MAVERICK | NX | DVE | PERF | DRAM | `MAVERICK_NX_DVE_PERF_DRAM_get` | `0x9b55c0` | `0x0087ff00` | `0x2740` | R |
| MAVERICK | NX | DVE | PERF | SRAM | `MAVERICK_NX_DVE_PERF_SRAM_get` | `0x9b55e0` | `0x00882640` | `0x0` | C |
| MAVERICK | NX | DVE | PERF | EXTRAM | `MAVERICK_NX_DVE_PERF_EXTRAM_get` | `0x9b5600` | `0x00882640` | `0x0` | C |
| MAVERICK | NX | DVE | PROF | CAM | `MAVERICK_NX_DVE_PROF_CAM_get` | `0x9b5ca0` | `0x009a42a0` | `0x400` | R |
| MAVERICK | NX | DVE | PROF | TABLE | `MAVERICK_NX_DVE_PROF_TABLE_get` | `0x9b5cc0` | `0x009a46a0` | `0x2000` | R |
| MAVERICK | NX | DVE | TEST | IRAM | `MAVERICK_NX_DVE_TEST_IRAM_get` | `0x9b5620` | `0x00882640` | `0xf5c0` | R |
| MAVERICK | NX | DVE | TEST | DRAM | `MAVERICK_NX_DVE_TEST_DRAM_get` | `0x9b5640` | `0x00891c00` | `0x29c0` | R |
| MAVERICK | NX | DVE | TEST | SRAM | `MAVERICK_NX_DVE_TEST_SRAM_get` | `0x9b5660` | `0x008945c0` | `0x0` | C |
| MAVERICK | NX | DVE | TEST | EXTRAM | `MAVERICK_NX_DVE_TEST_EXTRAM_get` | `0x9b5680` | `0x008945c0` | `0x0` | C |
| MAVERICK | NX | PE | PERF | IRAM | `MAVERICK_NX_PE_PERF_IRAM_get` | `0x9b5720` | `0x008b3540` | `0xbd60` | R |
| MAVERICK | NX | PE | PERF | DRAM | `MAVERICK_NX_PE_PERF_DRAM_get` | `0x9b5740` | `0x008bf2a0` | `0x2040` | R |
| MAVERICK | NX | PE | PERF | SRAM | `MAVERICK_NX_PE_PERF_SRAM_get` | `0x9b5760` | `0x008c12e0` | `0x0` | C |
| MAVERICK | NX | PE | PERF | EXTRAM | `MAVERICK_NX_PE_PERF_EXTRAM_get` | `0x9b5780` | `0x008c12e0` | `0x0` | C |
| MAVERICK | NX | PE | PROF | CAM | `MAVERICK_NX_PE_PROF_CAM_get` | `0x9b5ce0` | `0x009a66a0` | `0x400` | R |
| MAVERICK | NX | PE | PROF | TABLE | `MAVERICK_NX_PE_PROF_TABLE_get` | `0x9b5d00` | `0x009a6aa0` | `0x2000` | R |
| MAVERICK | NX | PE | TEST | IRAM | `MAVERICK_NX_PE_TEST_IRAM_get` | `0x9b5820` | `0x008d07e0` | `0xc320` | R |
| MAVERICK | NX | PE | TEST | DRAM | `MAVERICK_NX_PE_TEST_DRAM_get` | `0x9b5840` | `0x008dcb00` | `0x22c0` | R |
| MAVERICK | NX | PE | TEST | SRAM | `MAVERICK_NX_PE_TEST_SRAM_get` | `0x9b5860` | `0x008dedc0` | `0x0` | C |
| MAVERICK | NX | PE | TEST | EXTRAM | `MAVERICK_NX_PE_TEST_EXTRAM_get` | `0x9b5880` | `0x008dedc0` | `0x0` | C |
| MAVERICK | NX | POOL | PERF | IRAM | `MAVERICK_NX_POOL_PERF_IRAM_get` | `0x9b57a0` | `0x008c12e0` | `0xcf40` | R |
| MAVERICK | NX | POOL | PERF | DRAM | `MAVERICK_NX_POOL_PERF_DRAM_get` | `0x9b57c0` | `0x008ce220` | `0x25c0` | R |
| MAVERICK | NX | POOL | PERF | SRAM | `MAVERICK_NX_POOL_PERF_SRAM_get` | `0x9b57e0` | `0x008d07e0` | `0x0` | C |
| MAVERICK | NX | POOL | PERF | EXTRAM | `MAVERICK_NX_POOL_PERF_EXTRAM_get` | `0x9b5800` | `0x008d07e0` | `0x0` | C |
| MAVERICK | NX | POOL | PROF | CAM | `MAVERICK_NX_POOL_PROF_CAM_get` | `0x9b5d20` | `0x009a8aa0` | `0x400` | R |
| MAVERICK | NX | POOL | PROF | TABLE | `MAVERICK_NX_POOL_PROF_TABLE_get` | `0x9b5d40` | `0x009a8ea0` | `0x2000` | R |
| MAVERICK | NX | POOL | TEST | IRAM | `MAVERICK_NX_POOL_TEST_IRAM_get` | `0x9b58a0` | `0x008dedc0` | `0xd560` | R |
| MAVERICK | NX | POOL | TEST | DRAM | `MAVERICK_NX_POOL_TEST_DRAM_get` | `0x9b58c0` | `0x008ec320` | `0x27c0` | R |
| MAVERICK | NX | POOL | TEST | SRAM | `MAVERICK_NX_POOL_TEST_SRAM_get` | `0x9b58e0` | `0x008eeae0` | `0x0` | C |
| MAVERICK | NX | POOL | TEST | EXTRAM | `MAVERICK_NX_POOL_TEST_EXTRAM_get` | `0x9b5900` | `0x008eeae0` | `0x0` | C |
| MAVERICK | NX | SP | PERF | IRAM | `MAVERICK_NX_SP_PERF_IRAM_get` | `0x9b5920` | `0x008eeae0` | `0x0` | **C** |
| MAVERICK | NX | SP | PERF | DRAM | `MAVERICK_NX_SP_PERF_DRAM_get` | `0x9b5940` | `0x008eeae0` | `0x24c0` | R |
| MAVERICK | NX | SP | PERF | SRAM | `MAVERICK_NX_SP_PERF_SRAM_get` | `0x9b5960` | `0x008f0fa0` | `0xf580` | **R** |
| MAVERICK | NX | SP | PERF | EXTRAM | `MAVERICK_NX_SP_PERF_EXTRAM_get` | `0x9b5980` | `0x00900520` | `0x0` | C |
| MAVERICK | NX | SP | TEST | IRAM | `MAVERICK_NX_SP_TEST_IRAM_get` | `0x9b59a0` | `0x00900520` | `0x0` | **C** |
| MAVERICK | NX | SP | TEST | DRAM | `MAVERICK_NX_SP_TEST_DRAM_get` | `0x9b59c0` | `0x00900520` | `0x2740` | R |
| MAVERICK | NX | SP | TEST | SRAM | `MAVERICK_NX_SP_TEST_SRAM_get` | `0x9b59e0` | `0x00902c60` | `0xf6c0` | **R** |
| MAVERICK | NX | SP | TEST | EXTRAM | `MAVERICK_NX_SP_TEST_EXTRAM_get` | `0x9b5a00` | `0x00912320` | `0x0` | C |
| MAVERICK | Q7 | POOL | DEBUG | IRAM | `MAVERICK_Q7_POOL_DEBUG_IRAM_get` | `0x9b5b20` | `0x00962860` | `0x0` | **C** |
| MAVERICK | Q7 | POOL | DEBUG | DRAM | `MAVERICK_Q7_POOL_DEBUG_DRAM_get` | `0x9b5b40` | `0x00962860` | `0x15480` | R |
| MAVERICK | Q7 | POOL | DEBUG | SRAM | `MAVERICK_Q7_POOL_DEBUG_SRAM_get` | `0x9b5b60` | `0x00977ce0` | `0x1d100` | **R** |
| MAVERICK | Q7 | POOL | DEBUG | EXTRAM | `MAVERICK_Q7_POOL_DEBUG_EXTRAM_get` | `0x9b5b80` | `0x00994de0` | `0x0` | C |
| MAVERICK | Q7 | POOL | PERF | IRAM | `MAVERICK_Q7_POOL_PERF_IRAM_get` | `0x9b5a20` | `0x00912320` | `0x0` | **C** |
| MAVERICK | Q7 | POOL | PERF | DRAM | `MAVERICK_Q7_POOL_PERF_DRAM_get` | `0x9b5a40` | `0x00912320` | `0x13000` | R |
| MAVERICK | Q7 | POOL | PERF | SRAM | `MAVERICK_Q7_POOL_PERF_SRAM_get` | `0x9b5a60` | `0x00925320` | `0x14480` | **R** |
| MAVERICK | Q7 | POOL | PERF | EXTRAM | `MAVERICK_Q7_POOL_PERF_EXTRAM_get` | `0x9b5a80` | `0x009397a0` | `0x0` | C |
| MAVERICK | Q7 | POOL | PERF | EXTISA_0_SO | `MAVERICK_Q7_POOL_PERF_EXTISA_0_SO_get` | `0x9b5ba0` | `0x00994de0` | `0x7fb0` | R |
| MAVERICK | Q7 | POOL | PERF | EXTISA_0_JSON | `MAVERICK_Q7_POOL_PERF_EXTISA_0_JSON_get` | `0x9b5bc0` | `0x0099cd90` | `0x20` | R |
| MAVERICK | Q7 | POOL | PERF | EXTISA_1_SO | `MAVERICK_Q7_POOL_PERF_EXTISA_1_SO_get` | `0x9b5be0` | `0x0099cdb0` | `0xc64` | R |
| MAVERICK | Q7 | POOL | PERF | EXTISA_1_JSON | `MAVERICK_Q7_POOL_PERF_EXTISA_1_JSON_get` | `0x9b5c00` | `0x0099da20` | `0x20` | R |
| MAVERICK | Q7 | POOL | PERF | EXTISA_2_SO | `MAVERICK_Q7_POOL_PERF_EXTISA_2_SO_get` | `0x9b5c20` | `0x0099da40` | `0x1280` | R |
| MAVERICK | Q7 | POOL | PERF | EXTISA_2_JSON | `MAVERICK_Q7_POOL_PERF_EXTISA_2_JSON_get` | `0x9b5c40` | `0x0099ecc0` | `0x20` | R |
| MAVERICK | Q7 | POOL | PERF | EXTISA_3_SO | `MAVERICK_Q7_POOL_PERF_EXTISA_3_SO_get` | `0x9b5c60` | `0x0099ece0` | `0x55a0` | R |
| MAVERICK | Q7 | POOL | PERF | EXTISA_3_JSON | `MAVERICK_Q7_POOL_PERF_EXTISA_3_JSON_get` | `0x9b5c80` | `0x009a4280` | `0x20` | R |
| MAVERICK | Q7 | POOL | TEST | IRAM | `MAVERICK_Q7_POOL_TEST_IRAM_get` | `0x9b5aa0` | `0x009397a0` | `0x0` | **C** |
| MAVERICK | Q7 | POOL | TEST | DRAM | `MAVERICK_Q7_POOL_TEST_DRAM_get` | `0x9b5ac0` | `0x009397a0` | `0x13300` | R |
| MAVERICK | Q7 | POOL | TEST | SRAM | `MAVERICK_Q7_POOL_TEST_SRAM_get` | `0x9b5ae0` | `0x0094caa0` | `0x15dc0` | **R** |
| MAVERICK | Q7 | POOL | TEST | EXTRAM | `MAVERICK_Q7_POOL_TEST_EXTRAM_get` | `0x9b5b00` | `0x00962860` | `0x0` | C |

> **QUIRK — MAVERICK runs SP & Q7_POOL from SRAM, not IRAM.** For MAVERICK
> `NX_SP` *and* `Q7_POOL`, the `IRAM` getter is **size 0** (a cursor) and the
> **`SRAM`** getter carries the real code (`0xf580`/`0xf6c0` for SP PERF/TEST;
> `0x14480`/`0x1d100`/`0x15dc0` for Q7_POOL PERF/DEBUG/TEST). `NX_DVE`/`NX_PE`/
> `NX_POOL` still load from IRAM normally. Verified by getter-body `movq $0x0` on
> IRAM and the non-zero `movq` on SRAM. `[HIGH/OBSERVED]`

---

## 7. The FILE-ABSENT cells (do **not** claim these as present)

The MAVERICK row is structurally pared down. The following `(engine, gen, flavor)`
cells have **zero getter symbols** in this binary — verified `nm | rg -c` = 0:
`[HIGH/OBSERVED]`

| Absent cell | `nm \| rg -c` | Why |
|---|---:|---|
| **`MAVERICK_NX_ACT_*`** (entire engine) | 0 | MAVERICK ships **no** NX_ACT image at all (no `image_list` slot — the ACT→DVE fold) |
| **`MAVERICK_NX_POOL_DEBUG_*`** | 0 | the single missing `(engine, gen)` DEBUG cell — the **`0xf3` body-depth wall**; only DVE & Q7_POOL keep DEBUG on MAVERICK |
| `MAVERICK_NX_PE_DEBUG_*` | 0 | MAVERICK PE/POOL/SP ship `{PERF, TEST}` only |
| `MAVERICK_NX_SP_DEBUG_*` | 0 | (both SP slots) — no DEBUG |
| `MAVERICK_*_DYNAMIC_KERNEL_LOAD_*` | 0 | MAVERICK ships **no** DKL (only CAYMAN/MARIANA/MARIANA_PLUS Q7_POOL carry DKL) |
| `SUNDA_Q7_POOL_RELEASE_EXTISA_0_{SO,JSON}` | weak UND | present as **weak-undefined** stubs only; bodies live in the runtime `libnrtucode_extisa.so` container |

> **CORRECTION.** Cells in the table above must **never** be rendered as present
> getters. `MAVERICK_NX_POOL_DEBUG` in particular is the canonical missing cell —
> the `0xf3` body-depth wall flagged in the Part-6 coherence pack. It is absent
> from both `nm` and `image_list` (idx 32 = MAVERICK NX_POOL carries keys `{1,3}`
> only). The internal twin *does* keep `MAVERICK_Q7_POOL_DEBUG` (key 2 at idx 37,
> count 3 = `{1,2,3}`), so do not over-generalize "MAVERICK has no DEBUG."

---

## 8. Three-source reconciliation (one corpus, three packaging views)

The 386 `*_get` accessors are **not** distinct firmware — they are pointers into
`.rodata` that is the byte-identical, statically-linked-in copy of three archive
families, which are in turn the same kernels the runtime EXTISA container ships.
Proven by **sha256 byte-identity**, not inference: `[HIGH/OBSERVED]`

| Blob family | Archive source | Verified identity |
|---|---|---|
| base IRAM/DRAM/SRAM/EXTRAM | `img_<…>_<SEG>_contents.c.o` (`.rodata`) | `CAYMAN_NX_ACT_DEBUG_DRAM` 0x6260 → sha `f6c5136e…` == carve |
| PROF CAM/TABLE | `hwdecode_<…>_PROF_{CAM,TABLE}_contents.c.o` | `CAYMAN_NX_ACT_PROF_CAM` 0x400 == member `.rodata` |
| EXTISA SO/JSON | `img_<…>_EXTISA_n_{SO,JSON}_contents.c.o` | `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO` 0xa260 → **sha `910d41c3ededce67…`** == the byte the runtime EXTISA container ships |

The EXTISA `SO` sha256 was re-verified live here:
`dd skip=0x2ef7e0 count=0xa260 | sha256sum` =
`910d41c3ededce67cd00ec7041a5e66c3c39536d2e9b16fe21ea019db4b55527`, matching the
runtime container. `[HIGH/OBSERVED]`

> **NOTE — internal.so is a host-side superset, not a 1:1 mirror.** The shipped
> *front* lib `libnrtucode.so` (stripped, sha `06d3f0b1…`) carries the same 38-slot
> `image_list` shape but populates **only** the PERF (and DKL_PERF) getters;
> DEBUG/TEST and **all** MAVERICK getters are NULL → a request for those returns
> code 3. The internal twin populates DEBUG/PERF/TEST and MAVERICK fully (linked
> from an external MAVERICK build object — MAVERICK has 0 members in *this*
> `libnrtucode.a`). The front-lib `image_list` is the unnamed `unk_30F4E0`
> @`0x30f4e0`, structurally identical to internal `image_list`.

> **NOTE — SP is a real programmable NX core.** SP (Top Sync Processor) has **no**
> sequencer-config CSR aperture (unlike PE/POOL/ACT/DVE) yet ships a full NX
> firmware-image set per gen. The *only* structural way SP differs in this catalog:
> SP has **no** `PROF_CAM`/`PROF_TABLE` getters (no HW-decode opcode CAM) —
> consistent with SP routing notifications/SPC rather than decoding instructions.

---

## 9. Adversarial self-verification

Five strongest claims, re-challenged against the binary:

1. **Getter count = 386 defined-local (+2 weak SUNDA EXTISA).**
   `nm libnrtucode_internal.so | rg -c '_get$'` = **388**; `' t .*_get$'` = **386**;
   `' w .*_get$'` = **2**. ✓ Per-gen 24/100/100/100/62 each independently
   `rg -c`-verified (with the MARIANA-vs-MARIANA_PLUS subtraction trap noted). ✓
   Category split 288 base + 36 DKL + 32 EXTISA + 30 PROF = 386. ✓

2. **Resolver key tuple = `(idx 0..37, region 0..3, flavor)` — NOT raw coretype.**
   Disassembly of `nrtucode_get_memory_image @0x9b2960` confirms `cmp $0x25,%edi`
   (idx ≤ 37) → `image_list[idx*16]` → `count`/`descriptor*`; descriptor scan at
   `0x28` stride matching `flavor_key`; region jump table `@VA 0x555c` with `add
   $0x8/$0x10/$0x18/$0x20` = IRAM/DRAM/SRAM/EXTRAM; `call *%rax`. ✓ The raw
   `image_list` count vector read from `0x9b6d20` is exactly
   `[1×7, …, 0, 3]` Σ = 102. ✓ **Challenge:** is the first arg really not coretype?
   The map enumerates `(gen, engine)` with double-SP slots and an empty idx-36
   between MAVERICK SP and Q7_POOL — a coretype index (6/13/21/29/37) would not
   produce that shape. Resolved: it is the flat `(gen,engine)` index. ✓

3. **FILE-ABSENT: `MAVERICK_NX_POOL_DEBUG` (and MAVERICK_NX_ACT, MAVERICK DKL,
   non-DVE/Q7 DEBUG) are genuinely absent.** `nm | rg -c` = **0** for each. ✓
   `image_list` idx 32 (MAVERICK NX_POOL) count = 2, keys `{1,3}` — DEBUG (key 2)
   not in the descriptor set. ✓ **Challenge:** does the front lib hide them as
   NULL rather than absent? No — they are absent from the *symbol table* of the
   *internal twin*, the most complete view; the front lib NULLs them additionally.
   Distinction preserved (absent symbol vs NULL slot vs `(cursor,0)`). ✓

4. **225 distinct image pointers back 386 getters.** `objdump`-parse every getter's
   `lea` target, `sort -u` = **225**. ✓ **Challenge:** are the duplicates real
   aliases (two images sharing bytes) or boundary cursors? Spot-checked
   `CAYMAN_NX_PE_PERF_SRAM_get` → ptr `0xa0600` = `CAYMAN_NX_POOL_PERF_IRAM.data`
   with `movq $0x0` → it is a `(next-engine-cursor, 0)` empty marker, not an alias.
   The 386 → 225 collapse is entirely the zero-size SRAM/EXTRAM cursors. ✓

5. **EXTISA SO bytes are the runtime container kernel.**
   `dd skip=0x2ef7e0 count=0xa260 | sha256sum` = `910d41c3ededce67…` ✓ — identical
   to the byte the EXTISA container ships, proving the three packaging views are one
   corpus. ✓ All `*_EXTISA_n_JSON` sizes are uniformly `0x20` (the
   `{"dummy_message":"hello world"}` manifest placeholder for the 4 EXTISA-bearing
   gens). ✓

All five survive re-challenge with zero corrections to the catalog data.

---

## 10. Cross-references

- [EXTISA inventory](./extisa-inventory.md) — the 32 EXTISA `SO`/`JSON` getters and the `get_ext_isa` lane.
- [PROF CAM/TABLE formats](./prof-cam-table-formats.md) — the 30 PROF getters and the `get_hwdecode_table` lane.
- Per-generation image pages: [SUNDA POOL](./sunda-pool.md) · [CAYMAN ACT](./cayman-act.md) / [DVE](./cayman-dve.md) / [PE](./cayman-pe.md) / [POOL](./cayman-pool.md) / [SP](./cayman-sp.md) · [MARIANA ACT](./mariana-act.md) · [MARIANA_PLUS ACT](./mariana-plus-act.md) · [MAVERICK ACT](./maverick-act.md) (the ACT→DVE fold).
- [Firmware-image catalog capstone](./firmware-image-catalog.md) — the cross-gen overview that consumes this index.
- `runtime/image-hwdecode-resolvers.md` — the host `get_memory_image` / `get_hwdecode_table` resolvers in full (this page reproduces the `get_memory_image` body; the resolver page covers both lanes plus the front-lib twin).
