# Image + HW-Decode Resolvers

Two sibling host-runtime accessors in `libnrtucode_internal.so` resolve an
embedded firmware blob to a `(pointer, size)` pair by walking a flat
`(generation, engine)`-indexed table of getter function pointers:

- **`nrtucode_get_memory_image`** `@0x9b2960` — the base firmware-image region
  resolver. Indexes the **`image_list`** table `@0x9b8d20`, matches a flavor,
  then selects one of four memory regions (IRAM / DRAM / SRAM / EXTRAM) and
  calls the matching getter.
- **`nrtucode_get_hwdecode_table`** `@0x9b2cd0` — the **HW instruction-decode
  PROFILER** table resolver. Indexes the **`hwdecode_table_list`** table
  `@0x9b9090`, picks `PROF_CAM` (kind 0) or `PROF_TABLE` (kind 1), and calls
  the matching getter. This is the *instruction-decode profiler*, **not** the
  activation-function CAM — see [§5](#5-prof_cam--prof_table-identity-the-hw-instruction-decode-profiler).

Both are the dispatch behind the [Firmware-Image Accessor Index](../images/image-catalog-index.md)
catalog: that page is the *table of contents*; this page is the *two functions
that read it*. The third sibling resolver — `get_ext_isa` over the per-gen
`*_libs` tables — is documented in [Version + Ext-ISA Getters](version-extisa-getters.md)
and the [EXTISA Q7 SO-Blob Inventory](../images/extisa-inventory.md).

> **NOTE (binary provenance).** Every address, offset, stride, count, and enum
> value on this page was recovered by static analysis of the shipped host
> binary `libnrtucode_internal.so` (BuildID `9cbf78c6…`, 10,276,288 B, **not
> stripped**) with stock `nm`/`objdump`/`readelf` plus `python3` byte-decode of
> the descriptor tables. The recovered symbol names (`image_list`,
> `CAYMAN_NX_ACT_PERF_IRAM_get`, `…_PROF_CAM_get`, …) are themselves
> binary-derived and citeable. Section file-offset deltas used below (confirmed
> with `readelf -SW`): `.rodata` Δ=0, `.text` Δ=0x1000, `.data.rel.ro` Δ=0x2000,
> `.data` Δ=0x3000.
>
> **GOTCHA (no C++ vtables).** `nm libnrtucode_internal.so | rg -c '_ZTV'` →
> **0**. Every function-pointer table here is a *plain C array*: slot `N` is
> `table_symbol + 8*N`, and the `image_list`/`hwdecode_table_list` entries are
> 16-byte records. The `_ZTV + 0x10` vtable-base rule does **not** apply.

---

## 1. `nrtucode_get_memory_image` — the base-region resolver

### 1.1 Signature

```c
// nrtucode_get_memory_image @0x9b2960 (T, exported)
int nrtucode_get_memory_image(
    unsigned idx,     /*edi: flat (gen,engine) image index 0..37 — NOT a coretype*/
    int      region,  /*esi: 0=IRAM 1=DRAM 2=SRAM 3=EXTRAM ; >3 -> err 2*/
    int      flavor,  /*edx: 0=auto | 1=PERF/RELEASE | 2=DEBUG | 3=TEST
                             | 17/18/19 = DKL_{PERF,DEBUG,TEST} (Q7_POOL only)*/
    void*    out_ptr, /*rcx: receives the image pointer*/
    void*    out_len  /*r8 : receives the image size  */ );
// returns: 0 ok | 1 idx>37 | 2 flavor-not-found / region>3 / empty slot
//          | 3 region getter NULL | 8 removed-env-flag tripwire
```

`[HIGH/OBSERVED]` — signature from the prologue register moves at `0x9b2979`
(`r8→rbx`, `rcx→r14`, `edx→r15d`, `esi→ebp`, `edi→r12d`) and the SysV ABI.

### 1.2 Annotated body

Decoded `@0x9b2960..0x9b2b2f` (`objdump -d --start-address=0x9b2960
--stop-address=0x9b2b30`). Each pseudocode line carries its instruction
address; symbol names are the recovered `nm` names.

```c
int nrtucode_get_memory_image(unsigned idx, int region, int flavor,
                              void* out_ptr, void* out_len)
{
    // (A) bad-index guard                                    9b2960 / 9b2965
    if (idx > 37) return 1;                  // cmp $0x25,%edi ; ja -> ret 1

    // (B) NRTUCODE_MPLUS_ON_MARIANA removed-flag tripwire    9b2987..9b29a8
    const char* mp = getenv("NRTUCODE_MPLUS_ON_MARIANA");      // env str @VA 0x4fae
    if (mp) {
        int c = (unsigned char)mp[0] - '1';   // 9b2998 movzbl ; sub $0x31
        if (mp[0] == '1') c = mp[1];          // 9b29a0 first byte == '1'
        if (-c == 0)                          // 9b29a4 neg ; test ; je
            goto removed_flag;                // value == "1" -> ret 8 (below)
    }

    // (C) index image_list[idx]                              9b29ae..9b29bc
    image_list_ent* e = (image_list_ent*)((char*)&image_list + 16*idx);
                                              // shl $0x4 ; lea 0x6364(%rip) # 9b8d20

    // (D) flavor resolution                                  9b29bf
    if (flavor == 0) {                        // test r15d,r15d ; je env-auto
        const char* fv = getenv("NEURON_UCODE_FLAVOR");        // env str @VA 0x52e2
        if (!fv)                          flavor = 1;          // 9b2aa5 unset  -> PERF
        else if (!strcmp(fv,"debug")
              || !strcmp(fv,"DEBUG"))     flavor = 2;          // 9b2a13/9b2a2c -> DEBUG
        else if (!strcmp(fv,"test"))      flavor = 3;          // 9b2a3f       -> TEST
        else flavor = (strcmp(fv,"TEST")==0) ? 3 : 1;          // 9b2a56  TEST->3 else PERF
    }

    // (E) linear scan of `count` descriptors for flavor_key == flavor
    uint64_t   n   = e->count;                // 9b29c4 mov 0(%r13)
    if (n == 0) return 2;                      // 9b29cd empty slot (e.g. idx 36)
    image_desc* d  = e->table;                // 9b29d6 mov 0x8(%r13)
    for (; flavor != d->flavor_key; d++) {    // 9b29e0 cmp (%rdi),%r15d
        if (--n == 0) return 2;               // 9b29f1 stride +0x28 -> not found
    }

    // (F) region select (4-case switch via the @0x555c jump table) + call
    if ((unsigned)region > 3) return 2;       // 9b2a79 cmp $0x3,%ebp ; ja -> ret 2
    img_get* g;
    switch (region) {                          // 9b2a87 jmp *0x555c(,region,4)
        case 0: g = d->IRAM_get;   break;     // 9b2a97 add $0x08,%rcx
        case 1: g = d->DRAM_get;   break;     // 9b2ad6 add $0x10,%rcx
        case 2: g = d->SRAM_get;   break;     // 9b2ab0 add $0x18,%rcx
        case 3: g = d->EXTRAM_get; break;     // 9b2abe add $0x20,%rcx
    }
    if (g == NULL) return 3;                   // test %rax,%rax ; je -> ret 3
    g(out_ptr, out_len);                       // 9b2ad0 call *%rax  (rdi=out_ptr,rsi=out_len)
    return 0;                                  // 9b2ad2 success

removed_flag:                                  // 9b2ae9
    fwrite(/*0x99-byte removal message @VA 0x4877*/, 0x99, 1, stderr);
    return 8;
}
```

> **QUIRK (the env guard is a tombstone, not a selector).** `(B)` reads
> `NRTUCODE_MPLUS_ON_MARIANA` *before any table access*. The byte logic
> (`9b2998..9b29a8`) trips the error path precisely when the value is `"1"`;
> the `0x99`-byte stderr message states the flag *has been removed*. Treat it
> as a removed-flag tombstone — leaving it **unset** is the only clean pass;
> setting it to `"1"` returns 8 before any image is resolved.
> `[HIGH/OBSERVED]` (byte logic) / the human-intent gloss is `[MED/INFERRED]`.

> **GOTCHA (the flavor enum is sparse and arg-only for DKL).** The `flavor==0`
> auto path produces only `{1,2,3}` from `NEURON_UCODE_FLAVOR`. The
> `DYNAMIC_KERNEL_LOAD` keys `17/18/19` (`0x11/0x12/0x13`) exist in the
> `image_list` descriptors (Q7_POOL rows only) but are **unreachable via env** —
> a caller must pass `flavor = 17/18/19` explicitly. `[HIGH/OBSERVED]`

### 1.3 Return codes

| code | meaning |
|---|---|
| `0` | success — descriptor matched, region getter non-NULL, called; `*out_ptr`/`*out_len` written (`out_len` may be `0` for a boundary cursor — still success) |
| `1` | bad image index: `idx > 37` (`cmp $0x25 ; ja`). `idx == 36` is *in range* but empty, so it falls to code `2`, not `1` |
| `2` | flavor not matched across `count` descriptors, **or** `region > 3`, **or** `image_list[idx].count == 0` |
| `3` | the selected region getter pointer is NULL (variant present in table shape but not linked — e.g. front-lib MAVERICK / DEBUG / TEST) |
| `8` | `NRTUCODE_MPLUS_ON_MARIANA` set to `"1"`: the removed-flag `fwrite` fires and the function returns before any table access |

> **NOTE (boundary cursor vs absent slot).** A *boundary-cursor* getter (a
> non-NULL stub that writes `(ptr, 0)`) returns code **0** with `*out_len == 0`
> — "present but empty". Code **3** is reserved for a genuinely NULL getter
> *slot*. The caller distinguishes "this image has no SRAM bytes" (code 0,
> len 0) from "this variant is not in this build" (code 3) by the **return
> code**, not the size. `[HIGH/OBSERVED]`

---

## 2. The `image_list` table — descriptor layout

`image_list @0x9b8d20` lives in `.data.rel.ro` (Δ=0x2000 → file `0x9b6d20`).
Size `0x260` = **38 entries × 16 bytes**. Each entry points at a per-`(gen,
engine)` descriptor array in `.data` (Δ=0x3000).

```c
// 16-byte table entry — image_list @0x9b8d20 is 38 of these (size 0x260)
struct image_list_ent {
    uint64_t    count;   // +0x00  # of flavor descriptors for this (gen,engine)
    image_desc* table;   // +0x08  R_X86_64_RELATIVE addend = descriptor array base
};

// 40-byte descriptor (stride 0x28) — array lives in .data
struct image_desc {
    uint64_t flavor_key;   // +0x00  literal: 1/2/3 or 17/18/19 (matched against `flavor`)
    img_get* IRAM_get;     // +0x08  region 0   (R_X86_64_RELATIVE getter)
    img_get* DRAM_get;     // +0x10  region 1
    img_get* SRAM_get;     // +0x18  region 2
    img_get* EXTRAM_get;   // +0x20  region 3
};
// typedef void img_get(void** out_ptr, size_t* out_len);
```

`[HIGH/OBSERVED]` — `count` is read from the file image; `table` and the four
getters are zero in the file and supplied by `R_X86_64_RELATIVE` relocations
(addends read with `readelf -rW`). The stride is the `add $0x28,%rdi` at
`9b29ed`; the four getter offsets are the `add $0x08/0x10/0x18/0x20` in the
region switch.

**Decoded descriptor (idx 7, CAYMAN_NX_ACT, count 3, `table=0x9ba5c8`):**

| desc | `flavor_key` | IRAM_get | resolved symbol |
|---|---|---|---|
| `[0]` `@0x9ba5c8` | `1` (PERF) | `0x9b3020` | `CAYMAN_NX_ACT_PERF_IRAM_get` |
| `[1]` `@0x9ba5f0` | `2` (DEBUG) | `0x9b3520` | `CAYMAN_NX_ACT_DEBUG_IRAM_get` |
| `[2]` `@0x9ba618` | `3` (TEST) | `0x9b32a0` | `CAYMAN_NX_ACT_TEST_IRAM_get` |

The four getter slots of `[0]` resolve to
`CAYMAN_NX_ACT_PERF_{IRAM=0x9b3020, DRAM=0x9b3040, SRAM=0x9b3060,
EXTRAM=0x9b3080}_get` — confirming the `GEN_CLS_ENG_VARIANT_REGION` symbol
ordering and the `+0x08/+0x10/+0x18/+0x20` region offsets. `[HIGH/OBSERVED]`

### 2.1 The flat `(gen, engine)` index — arg0

`idx` is **not** a raw coretype. It is a flat `(generation, engine)` image
index `0..37` the host driver precomputes; it is *not* the coretype
`6/13/21/29/37` that `get_ext_isa` keys on. Decoded from the binary (counts
from the file image, gen/engine from each descriptor's IRAM getter symbol):

| idx | GEN | CLS·ENG | count | `table` (`.data`) | flavor keys |
|---|---|---|---|---|---|
| 0 | SUNDA | NX·ACT | 1 | `0x9ba4b0` | `{1}` (RELEASE) |
| 1 | SUNDA | NX·DVE | 1 | `0x9ba4d8` | `{1}` |
| 2 | SUNDA | NX·POOL | 1 | `0x9ba500` | `{1}` |
| 3 | SUNDA | NX·PE | 1 | `0x9ba528` | `{1}` |
| 4 | SUNDA | NX·SP | 1 | `0x9ba550` | `{1}` |
| 5 | SUNDA | NX·SP | 1 | `0x9ba578` | `{1}` (2nd SP) |
| 6 | SUNDA | Q7·POOL | 1 | `0x9ba5a0` | `{1}` |
| 7 | CAYMAN | NX·ACT | 3 | `0x9ba5c8` | `{1,2,3}` |
| 8 | CAYMAN | NX·DVE | 3 | `0x9ba640` | `{1,2,3}` |
| 9 | CAYMAN | NX·POOL | 3 | `0x9ba6b8` | `{1,2,3}` |
| 10 | CAYMAN | NX·PE | 3 | `0x9ba730` | `{1,2,3}` |
| 11 | CAYMAN | NX·SP | 3 | `0x9ba7a8` | `{1,2,3}` |
| 12 | CAYMAN | NX·SP | 3 | `0x9ba820` | `{1,2,3}` (2nd SP) |
| 13 | CAYMAN | Q7·POOL | 6 | `0x9ba898` | `{1,17,18,19,2,3}` |
| 14 | CAYMAN | Q7·POOL | 3 | `0x9ba988` | `{1,2,3}` (2nd Q7) |
| 15 | MARIANA | NX·ACT | 3 | `0x9baa00` | `{1,2,3}` |
| 16 | MARIANA | NX·DVE | 3 | `0x9baa78` | `{1,2,3}` |
| 17 | MARIANA | NX·POOL | 3 | `0x9baaf0` | `{1,2,3}` |
| 18 | MARIANA | NX·PE | 3 | `0x9bab68` | `{1,2,3}` |
| 19 | MARIANA | NX·SP | 3 | `0x9babe0` | `{1,2,3}` |
| 20 | MARIANA | NX·SP | 3 | `0x9bac58` | `{1,2,3}` (2nd SP) |
| 21 | MARIANA | Q7·POOL | 6 | `0x9bacd0` | `{1,17,18,19,2,3}` |
| 22 | MARIANA | Q7·POOL | 3 | `0x9badc0` | `{1,2,3}` (2nd Q7) |
| 23 | MARIANA_PLUS | NX·ACT | 3 | `0x9bae38` | `{1,2,3}` |
| 24 | MARIANA_PLUS | NX·DVE | 3 | `0x9baeb0` | `{1,2,3}` |
| 25 | MARIANA_PLUS | NX·POOL | 3 | `0x9baf28` | `{1,2,3}` |
| 26 | MARIANA_PLUS | NX·PE | 3 | `0x9bafa0` | `{1,2,3}` |
| 27 | MARIANA_PLUS | NX·SP | 3 | `0x9bb018` | `{1,2,3}` |
| 28 | MARIANA_PLUS | NX·SP | 3 | `0x9bb090` | `{1,2,3}` (2nd SP) |
| 29 | MARIANA_PLUS | Q7·POOL | 6 | `0x9bb108` | `{1,17,18,19,2,3}` |
| 30 | MARIANA_PLUS | Q7·POOL | 3 | `0x9bb1f8` | `{1,2,3}` (2nd Q7) |
| 31 | MAVERICK | NX·DVE | 3 | `0x9bb270` | `{1,2,3}` |
| 32 | MAVERICK | NX·POOL | 2 | `0x9bb2e8` | `{1,3}` (no DEBUG) |
| 33 | MAVERICK | NX·PE | 2 | `0x9bb338` | `{1,3}` (no DEBUG) |
| 34 | MAVERICK | NX·SP | 2 | `0x9bb388` | `{1,3}` (no DEBUG) |
| 35 | MAVERICK | NX·SP | 2 | `0x9bb3d8` | `{1,3}` (2nd SP, no DEBUG) |
| 36 | — | — | **0** | `0x0` | **empty boundary slot** |
| 37 | MAVERICK | Q7·POOL | 3 | `0x9bb428` | `{1,2,3}` |

Σ = **102 descriptors** (SUNDA 7 + CAYMAN 27 + MARIANA 27 + MPLUS 27 +
MAVERICK 14). The counts in this table were read byte-exact from the file
image; this reconciles with the [Firmware-Image Accessor Index](../images/image-catalog-index.md)
key-map. `[HIGH/OBSERVED]`

> **QUIRK (double-SP + empty idx 36).** Every generation enumerates `NX_SP`
> *twice* (e.g. `il[4]&il[5]`, `il[34]&il[35]`); both SP descriptors alias the
> *same* SP getters. MAVERICK ships **no `NX_ACT`** row, so the `(gen,engine)`
> flattening leaves a one-slot gap at **idx 36** (count 0, NULL table) between
> MAVERICK `NX_SP` (35) and MAVERICK `Q7_POOL` (37). A
> `get_memory_image(36, …)` returns **2** (count==0). The aliasing is
> `[HIGH/OBSERVED]`; the *reason* for two SP slots is `[MED/INFERRED]`.

> **NOTE (engine identity).** The engine is encoded by the `image_list` slot
> position, not a separate arg. Per gen the NX order is ACT, DVE, POOL, PE, SP,
> SP, then Q7_POOL. This reconciles with the sequencer-aperture ordering
> (PE=0, POOL=1, ACT=2, DVE=3): PE/DVE are `[HIGH/OBSERVED]` from the carve
> getters; POOL/ACT are `[MED/INFERRED]` from the CSR ordering.

---

## 3. The region jump table `@0x555c` — the 4-case switch

The four-region select at `9b2a87` is a relative jump table in `.rodata`
(Δ=0 → file `0x555c`), four `int32` rel32 entries (`target = 0x555c + rel32`).
Decoded byte-exact:

| `region` | rel32 | target | block does | descriptor field |
|---|---|---|---|---|
| `0` IRAM | `0x9ad53b` | `0x9b2a97` | `add $0x08,%rcx` | `IRAM_get` (+0x08) |
| `1` DRAM | `0x9ad57a` | `0x9b2ad6` | `add $0x10,%rcx` | `DRAM_get` (+0x10) |
| `2` SRAM | `0x9ad554` | `0x9b2ab0` | `add $0x18,%rcx` | `SRAM_get` (+0x18) |
| `3` EXTRAM | `0x9ad562` | `0x9b2abe` | `add $0x20,%rcx` | `EXTRAM_get` (+0x20) |

> **NOTE (case-block order is 0/2/3/1).** The four switch *targets* are emitted
> in the binary in the order **IRAM (0) → SRAM (2) → EXTRAM (3) → DRAM (1)** —
> the compiler reordered the case blocks (`0x9b2a97 < 0x9b2ab0 < 0x9b2abe <
> 0x9b2ad6`). The *region enum* is still the natural `0=IRAM 1=DRAM 2=SRAM
> 3=EXTRAM`; only the physical block layout is permuted. `region > 3` never
> reaches the table (`cmp $0x3,%ebp ; ja -> ret 2`). There is **no PROF
> region** here — `PROF_CAM`/`PROF_TABLE` are a separate table reached only via
> `get_hwdecode_table` ([§4](#4-nrtucode_get_hwdecode_table--the-prof-profiler-resolver)).
> `[HIGH/OBSERVED]`

---

## 4. `nrtucode_get_hwdecode_table` — the PROF profiler resolver

### 4.1 Signature + body

```c
// nrtucode_get_hwdecode_table @0x9b2cd0 (T, exported) — body is a tight 0x44 bytes
int nrtucode_get_hwdecode_table(
    unsigned idx,     /*edi: flat (gen,engine) index 0..37 — SAME space as image_list*/
    int      kind,    /*esi: 0=PROF_CAM | 1=PROF_TABLE | else -> err 2*/
    void*    out_ptr, /*rdx: receives blob pointer*/
    void*    out_len  /*rcx: receives blob size  */ );
// returns: 0 ok | 1 idx>37 | 2 (bad kind OR getter NULL)
```

Decoded `@0x9b2cd0..0x9b2d14`:

```c
int nrtucode_get_hwdecode_table(unsigned idx, int kind,
                                void* out_ptr, void* out_len)
{
    if (idx > 37) return 1;                          // 9b2cd5 cmp $0x25 ; ja
    hwdecode_ent* e =                                // 9b2cdc shl $0x4 (×16)
        (hwdecode_ent*)((char*)&hwdecode_table_list + 16*idx);  // lea # 9b9090
    int result = 2;                                  // 9b2cea mov $0x2  (set BEFORE kind test)
    void** slot;
    if (kind == 0)      slot = &e->PROF_CAM_get;     // 9b2cef test esi ; je -> +0x00
    else if (kind == 1) slot = &e->PROF_TABLE_get;   // 9b2cf3 cmp $0x1 ; +0x08
    else                return 2;                    // 9b2cf6 jne -> ret 2 (BAD KIND)
    img_get* g = *slot;                              // 9b2cfc mov (%rdi),%r8
    if (g == NULL) return 2;                         // 9b2cff test ; je -> ret 2 (absent)
    g(out_ptr, out_len);                             // 9b2d0b call *%r8 (rdi=out_ptr,rsi=out_len)
    return 0;                                         // 9b2d0e xor eax,eax
}
```

> **CORRECTION (bad-kind returns 2, not 1).** `result` is loaded with `2` at
> `9b2cea`, *before* the kind comparison at `9b2cf3`. A `kind ∉ {0,1}` therefore
> falls out with `eax == 2`, **not** `1`. An earlier read that bad-kind returns
> `1` is wrong; this lane has no code `3` at all (contrast `get_memory_image`'s
> code 3). `[HIGH/OBSERVED]`

> **NOTE (no flavor, no env, no guard).** Unlike `get_memory_image`, this
> resolver has **no** `getenv`, **no** `strcmp`, **no** flavor key, **no**
> region jump table, and **no** `MPLUS` deprecation guard. PROF tables are a
> single variant per `(gen, engine)` — there is no PERF/DEBUG/TEST or DKL
> split. `[HIGH/OBSERVED]`

### 4.2 The `hwdecode_table_list` table

`hwdecode_table_list @0x9b9090` lives in `.data.rel.ro` (Δ=0x2000 → file
`0x9b7090`), size `0x260` = 38 × 16 bytes (parallel to `image_list`'s shape and
sharing its flat index). The 16-byte entry holds two getter pointers, both
`R_X86_64_RELATIVE` (zero in the file image):

```c
struct hwdecode_ent {
    void(*PROF_CAM_get)  (void** out_ptr, size_t* out_len);  // +0x00  kind 0
    void(*PROF_TABLE_get)(void** out_ptr, size_t* out_len);  // +0x08  kind 1
};
```

Only **15** of the 38 entries are populated (30 getters total), decoded from
the relocation addends:

| idx | GEN | ENG | `PROF_CAM_get` | `PROF_TABLE_get` |
|---|---|---|---|---|
| 7 | CAYMAN | ACT | `0x9b3ba0` | `0x9b3bc0` |
| 8 | CAYMAN | DVE | `0x9b3be0` | `0x9b3c00` |
| 9 | CAYMAN | POOL | `0x9b3c60` | `0x9b3c80` |
| 10 | CAYMAN | PE | `0x9b3c20` | `0x9b3c40` |
| 15 | MARIANA | ACT | `0x9b4820` | `0x9b4840` |
| 16 | MARIANA | DVE | `0x9b4860` | `0x9b4880` |
| 17 | MARIANA | POOL | `0x9b48e0` | `0x9b4900` |
| 18 | MARIANA | PE | `0x9b48a0` | `0x9b48c0` |
| 23 | MARIANA_PLUS | ACT | `0x9b54a0` | `0x9b54c0` |
| 24 | MARIANA_PLUS | DVE | `0x9b54e0` | `0x9b5500` |
| 25 | MARIANA_PLUS | POOL | `0x9b5560` | `0x9b5580` |
| 26 | MARIANA_PLUS | PE | `0x9b5520` | `0x9b5540` |
| 31 | MAVERICK | DVE | `0x9b5ca0` | `0x9b5cc0` |
| 32 | MAVERICK | POOL | `0x9b5d20` | `0x9b5d40` |
| 33 | MAVERICK | PE | `0x9b5ce0` | `0x9b5d00` |

15 entries × 2 = **30 PROF getters** (`nm | rg -c 'PROF_(CAM|TABLE)_get$'`
→ 30). The CAM getter at idx 7 (`0x9b3ba0`) resolves to
`CAYMAN_NX_ACT_PROF_CAM_get`; idx 9 (`0x9b3c60`) → `CAYMAN_NX_POOL_PROF_CAM_get`
and idx 10 (`0x9b3c20`) → `CAYMAN_NX_PE_PROF_CAM_get`, confirming the
flat-index → engine mapping even though the getter VAs are non-monotonic.
`[HIGH/OBSERVED]`

> **NOTE (sparse population — who has no profiler).** SUNDA (idx 0–6) is
> entirely NULL — no HW-decode profiler. `NX_SP` (idx 4/5, 11/12, 19/20, 27/28,
> 34/35) and `Q7_POOL` (idx 6, 13/14, 21/22, 29/30, 37) slots exist in the
> index space but are NULL. MAVERICK has **no ACT row** (idx 31 starts at DVE),
> matching `image_list`. Any NULL slot → `get_hwdecode_table` returns 2.
> `[HIGH/OBSERVED]`

### 4.3 The PROF getter stubs

Each PROF getter is the standard `lea blob; movq size; ret` stub. The
immediates are uniform: **all CAM getters return size `0x400` (1 KiB)**, **all
TABLE getters return `0x2000` (8 KiB)**:

```asm
; CAYMAN_NX_ACT_PROF_CAM_get @0x9b3ba0
  lea  -0x6b1307(%rip),%rax   # 3028a0    ; CAM blob
  movq $0x400,(%rsi)          ;            ; size = 1 KiB
  ret
; CAYMAN_NX_ACT_PROF_TABLE_get @0x9b3bc0
  lea  -0x6b0f27(%rip),%rax   # 302ca0    ; TABLE blob
  movq $0x2000,(%rsi)         ;            ; size = 8 KiB
  ret
```

The blobs live in `.rodata` (Δ=0 → VA == file offset), so a carve is
`dd skip=<blobVA> count=<size>`. `[HIGH/OBSERVED]`

---

## 5. PROF_CAM / PROF_TABLE identity: the HW instruction-decode PROFILER

> **CORRECTION (identity — this is the decode PROFILER, NOT the activation
> CAM).** `get_hwdecode_table` delivers the **HW instruction-decode profiler**
> tables: an opcode-match CAM (`PROF_CAM`) that arms *which decoded opcodes the
> hardware profiler counts*, plus a parallel per-opcode profiler-descriptor
> table (`PROF_TABLE`). It is **categorically not** the activation-function
> CAM / PWL lookup. The two collide only on the number "47" and the "128-byte
> record"; that coincidence dissolves under the byte structure. This page
> reconciles with [PROF_CAM / PROF_TABLE Blob Formats](../images/prof-cam-table-formats.md),
> which decodes the blob internals; here is the resolver-side proof.

Binary evidence (each independently sufficient):

1. **Naming.** The resolver is `nrtucode_get_hwdecode_table`, the table is
   `hwdecode_table_list`, and the getters are
   `<GEN>_NX_<ENG>_PROF_{CAM,TABLE}_get` — "**hwdecode**" = HW instruction
   *decode*. `[HIGH/OBSERVED]`
2. **No activation tables in the library.**
   `nm libnrtucode_internal.so | rg -ci 'stpb|act_profile|_BUCKET_|_CONTROL_TABLE'`
   → **0**. The activation CAM/PROFILE/BUCKET/CONTROL PWL tables are *not in
   this library at all* — they are a different, DMA-staged subsystem. There is
   nothing here for `get_hwdecode_table` to deliver *except* the decode
   profiler. `[HIGH/OBSERVED]`
3. **Record format.** `PROF_CAM` (CAYMAN ACT, `@0x3028a0`, size `0x400`,
   sha256 `8fd7e422…`) is a 16-byte record `{u32 opcode_id; u32 mask; u32
   enable; u32 reserved}` × 64, with **47 armed** (`enable==1`). Slot 0 arms
   opcode `0x01`, slot 13 arms `0x21` (ACTIVATE); **slot 46 is the wildcard**
   (`opcode 0x00, mask 0x00, enable 1`) — the catch-all bucket. A CAM keyed on
   *executing instruction opcodes* with a per-opcode enable is a decode
   profiler; the activation CAM is a 32-byte record keyed on
   `(opcode, func_id)`. Different stride (16 vs 32), different key, different
   count (47 vs 128). `[HIGH/OBSERVED]`
4. **`PROF_TABLE`** (`@0x302ca0`, size `0x2000`) is 64 × 128-byte records, 47
   populated, **parallel to `PROF_CAM`** (record `i` describes the opcode armed
   by CAM slot `i`), and **sparse** (≤ ~36 of 128 bytes used) — not a dense
   128-byte activation PWL config. `[HIGH/OBSERVED]`

The full blob decode (the 47-opcode model, the per-engine specialisation from
MARIANA on, the cross-gen sha256 matrix) is in
[PROF_CAM / PROF_TABLE Blob Formats](../images/prof-cam-table-formats.md).

---

## 6. The front-lib twin + the MAVERICK boundary

The shipped front lib `libnrtucode.so` (3,208,440 B, **stripped**) carries
byte-logic-identical twins of both resolvers, indexing the *same-shaped* tables
under stripped (auto-named) symbols:

| function | front VA | indexes | note |
|---|---|---|---|
| `get_memory_image` | `0x30adf0` | `unk_30F4E0` (`lea 0x4694(%rip),%r13 # 30f4e0`) | the front-lib `image_list` |
| `get_hwdecode_table` | `0x30b150` | `0x30f810` (`lea 0x46a9(%rip),%rdi # 30f810`) | the front-lib `hwdecode_table_list` |

> **NOTE (`unk_30F4E0` identified).** `unk_30F4E0` is **not** a region-offset
> table and **not** a separate index — it is the front-lib's `image_list`
> (38 × 16 `{count, descriptor*}`), unnamed only because the front lib is
> stripped. The front lib reserves the full 38-slot (5-gen) table shape but
> relocates only the **PERF / DKL_PERF** getters and **no MAVERICK** getters:
> a MAVERICK or DEBUG/TEST request resolves a descriptor, then hits a NULL
> getter → code **3**. The internal twin populates DEBUG/PERF/TEST and MAVERICK
> from an external archive (functionally 5-gen). `[HIGH/OBSERVED]`

For the PROF lane, the front `hwdecode_table_list @0x30f810` populates only the
12 entries idx 7–10/15–18/23–26 (CAYMAN/MARIANA/MARIANA_PLUS) and leaves
MAVERICK (idx 31–33) NULL → `get_hwdecode_table(31/32/33, …)` returns 2 in the
front lib. `[HIGH/OBSERVED]`

---

## 7. The three-resolver partition

The two resolvers here, plus `get_ext_isa`, partition the firmware-accessor
getter space cleanly:

| resolver | table | key scheme | getters |
|---|---|---|---|
| `get_memory_image` `@0x9b2960` | `image_list` (38×16) `@0x9b8d20` | `idx` + `region` + `flavor` | base IRAM/DRAM/SRAM/EXTRAM (+DKL) |
| `get_hwdecode_table` `@0x9b2cd0` | `hwdecode_table_list` (38×16) `@0x9b9090` | `idx` + `kind` (CAM/TABLE) | 30 PROF getters |
| `get_ext_isa` | per-gen `*_libs` | `coretype` + lib index | EXTISA Q7 SO/JSON |

The first two share the **flat `(gen, engine)` index** `0..37`; `get_ext_isa`
keys on coretype instead. There is **no PROF region** in `get_memory_image`
([§3](#3-the-region-jump-table-0x555c--the-4-case-switch)) and **no flavor** in
`get_hwdecode_table` ([§4](#4-nrtucode_get_hwdecode_table--the-prof-profiler-resolver))
— three resolvers, three tables, three disjoint key schemes. See
[Version + Ext-ISA Getters](version-extisa-getters.md) for the third lane and
the [Firmware-Image Accessor Index](../images/image-catalog-index.md) for the
catalog over all three.

---

## Reproduction

```sh
INT=…/libnrtucode_internal.so          # BuildID 9cbf78c6…, 10,276,288 B, not stripped
SO=…/libnrtucode.so                    # stripped front lib

# resolver bodies:
objdump -d --start-address=0x9b2960 --stop-address=0x9b2b30 $INT   # get_memory_image
objdump -d --start-address=0x9b2cd0 --stop-address=0x9b2d20 $INT   # get_hwdecode_table

# region jump table (.rodata Δ=0): 4 × int32 rel32, target = 0x555c + rel32
# image_list (.data.rel.ro Δ=0x2000 → file 0x9b6d20): 38 × {count@+0, table_ptr@+8 reloc}
# descriptors (.data Δ=0x3000): stride 0x28, key@+0, getters @+8/+10/+18/+20 (relocs)
# hwdecode_table_list (.data.rel.ro): 38 × {CAM_get@+0, TABLE_get@+8} ; 30 RELATIVE relocs
readelf -rW $INT | rg RELATIVE                      # addends populate both tables
nm $INT | rg -c 'PROF_(CAM|TABLE)_get$'             # → 30
nm $INT | rg -ci 'stpb|act_profile|_BUCKET_'        # → 0 (no activation tables here)

# PROF_CAM carve (.rodata id-mapped, VA == file offset):
dd if=$INT bs=1 skip=$((0x3028a0)) count=$((0x400)) | sha256sum   # 8fd7e422… CAYMAN ACT
```
