# Version + Ext-ISA Getters

This page pins down the small, self-contained getter surface of the GPSIMD host ucode library `libnrtucode`: the three **version getters** (`nrtucode_get_build_version`, `nrtucode_get_git_version`, `nrtucode_get_api_level`) and the two **EXTISA getters** (`nrtucode_get_num_ext_isa_libs`, `nrtucode_get_ext_isa`). The version getters are one-instruction-plus-`ret` leaves; the EXTISA getters resolve, per silicon generation, the host-resident Q7 EXTISA shared-object (`.so`) blob and its JSON manifest, dispatched through a **32-case coretype jump table**. The page is reconstruction-grade: every getter is given as annotated C pseudocode naming the real `symbol@addr`, and every one of the 32 jump-table slots carries its coretype and its EXTISA meaning.

Everything below is recovered by static analysis of the shipped binary only — symbol table (`.symtab`), relocation table (`.rela.dyn`), `.rodata` string slices (VMA == file offset on this image), and bounded disassembly. The EXTISA inventory (the blob carve census) is the companion page [EXTISA inventory](../images/extisa-inventory.md); the coretype↔codename↔generation join is [codename cross-walk](../reference/codename-crosswalk.md) and its deep companion [codename → generation map](../generations/codename-generation-map.md); the base-region resolver that shares this coretype space is [image + HW-decode resolvers](image-hwdecode-resolvers.md).

**Binary under analysis.** `libnrtucode_internal.so`, x86-64 ELF, **10,276,288 bytes**, `BuildID[sha1] 9cbf78c6f59cdb5839f155fdb2113bbe51e585fd`, **not stripped** (full `.symtab`). It is the **5-generation symbol twin** — it embeds and dispatches the MAVERICK generation in addition to SUNDA/CAYMAN/MARIANA/MARIANA_PLUS. Path: `extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/libnrtucode_internal.so`. The **shipped front** library `libnrtucode.so` (3,208,440 bytes, stripped but `.dynsym` retained) is the **4-generation** build (no MAVERICK); its mirror addresses are given throughout for the reimplementer who only has the front lib.

> **NOTE — `.rodata` is VMA == file offset; `.text` and `.data.rel.ro` are not.** `readelf -SW` on `libnrtucode_internal.so` gives `.rodata` at VMA `0x46b0` / file offset `0x46b0` (Δ = 0), `.text` at VMA `0x9b01a0` / file offset `0x9af1a0` (Δ = `0x1000`), and `.data.rel.ro` at VMA `0x9b8cf0` / file offset `0x9b6cf0` (Δ = `0x2000`). Every version-string address below is a `.rodata` VMA and may be sliced from the file verbatim. Every getter-body and jump-table-target address is a `.text`/`.rodata` VMA that `objdump --start-address=` takes verbatim (it maps file offsets internally); the per-gen lib tables live in `.data.rel.ro` and need the `0x2000` subtraction only if you slice the raw file by hand.

> **GOTCHA — this image has ZERO C++ vtables.** `nm libnrtucode_internal.so | rg -c '_ZTV'` → `0`. Every dispatch structure here is a **plain C array of function pointers**: slot *N* in a lib table is at `table_symbol + 16*N` (two 8-byte pointers per entry: `{SO_get, JSON_get}`), and the coretype jump table is a 32-entry array of **int32 self-relative offsets** with stride 4 (`table + 4*N`). The `_ZTV + 0x10` vtable-base rule used on C++ images does **not** apply here.

The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag. Escaped `\|` inside a table cell is a literal pipe.

---

## 0. Orientation — the five symbols and their addresses

All seven relevant symbols (five primary + one internal worker + one boundary sibling) are present at fixed addresses in both libraries.
| Symbol | Bind | internal twin addr | front lib addr | Role |
|---|---|---|---|---|
| `nrtucode_get_api_level` | `T` | `0x9b0260` | `0x308740` | returns the constant ABI/API level **3** |
| `nrtucode_get_build_version` | `T` | `0x9b0270` | `0x308750` | returns `"1.21.1.0"` (host `.rodata` literal) |
| `nrtucode_get_git_version` | `T` | `0x9b0280` | `0x308760` | returns the 40-hex git commit SHA |
| `nrtucode_get_num_ext_isa_libs` | `T` | `0x9b2c90` | `0x30b110` | writes per-coretype EXTISA lib count |
| `nrtucode_get_ext_isa` | `T` | `0x9b2c80` | `0x30b100` | public wrapper → internal worker, lib index = 0 |
| `nrtucode_get_ext_isa_internal` | `t` (local) | `0x9b2b30` | `0x30afc0` | the dispatcher: flavor gate + 32-case jump table |
| `nrtucode_get_memory_image` | `T` | `0x9b2960` | `0x30adf0` | **boundary** — separate base-region resolver (see [image-hwdecode-resolvers](image-hwdecode-resolvers.md)) |

> **NOTE — the front lib exposes these via `.dynsym`.** `libnrtucode.so` is stripped of `.symtab` but keeps `.dynsym`; `nm -D libnrtucode.so` lists `nrtucode_get_api_level@0x308740`, `nrtucode_get_build_version@0x308750`, `nrtucode_get_git_version@0x308760`, `nrtucode_get_ext_isa@0x30b100`, `nrtucode_get_num_ext_isa_libs@0x30b110`. The internal worker `nrtucode_get_ext_isa_internal` is a `t` local and is named only in the internal twin's `.symtab`; in the front lib it is the unnamed body at `0x30afc0` that `0x30b100` tail-calls.

---

## 1. The version getters — byte-exact

These three are the simplest functions in the library: a single `mov`/`lea` followed by `ret`. No argument is read; the return is a constant.

### 1a. `nrtucode_get_api_level` @ `0x9b0260`
```asm
9b0260:  b8 03 00 00 00     mov    $0x3,%eax        ; return 3
9b0265:  c3                 ret
```

```c
/* nrtucode_get_api_level @ internal 0x9b0260 / front 0x308740 */
int nrtucode_get_api_level(void) {
    return 3;                 /* ABI/API level — the ONLY numeric version surface */
}
```

`api_level = 3` is the lone numeric version surface and is **byte-verified** (`b8 03 00 00 00`); the value matches the committed object-model graph. It does **not** track the generation count — it is `3` in both the 4-gen front lib and the 5-gen internal twin.

### 1b. `nrtucode_get_build_version` @ `0x9b0270`
```asm
9b0270:  48 8d 05 e9 51 65 ff   lea    -0x9aae17(%rip),%rax   ; -> VA 0x5460
9b0277:  c3                     ret
```

```c
/* nrtucode_get_build_version @ internal 0x9b0270 / front 0x308750 */
const char *nrtucode_get_build_version(void) {
    return /* .rodata @0x5460 */ "1.21.1.0";   /* 8 chars + NUL */
}
```

The string at `.rodata` VA `0x5460` is `31 32 2e 32 31 2e 31 2e 30 00` = `"1.21.1.0"` (length 8). In the host string pool it sits right after `"op_set\0"` — a genuine standalone literal, **before** the first embedded Q7 ELF blob (`0x5460 < 0x14020`), so it is not aliased into any embedded image. The front-lib mirror at `0x308750` `lea`s VA `0x38c6`, which holds the same `"1.21.1.0"`.

### 1c. `nrtucode_get_git_version` @ `0x9b0280`
```asm
9b0280:  48 8d 05 61 4f 65 ff   lea    -0x9ab09f(%rip),%rax   ; -> VA 0x51e8
9b0287:  c3                     ret
```

```c
/* nrtucode_get_git_version @ internal 0x9b0280 / front 0x308760 */
const char *nrtucode_get_git_version(void) {
    return /* .rodata @0x51e8 */
        "6db9edc09474e79d0c0e283c14c16e51ee7417e5";   /* 40-hex git commit SHA */
}
```

The string at `.rodata` VA `0x51e8` is the 40-character git commit SHA `6db9edc09474e79d0c0e283c14c16e51ee7417e5`. This SHA occurs **exactly once** in the internal twin (at `0x51e8`) and exactly once in the front lib (at `0x364e`) — a single host copy each.

### 1d. Phantom-version check — NEGATIVE
`"1.21.1.0"` is the **only** `x.y.z.w` dotted-version string in either library. A direct file scan of the internal twin finds it **27 times**: 1 host getter copy at `0x5460` (below the first blob at `0x14020`) and 26 copies **inside** the embedded Q7 ELF blobs (per-gen version stamps + the SUNDA real-JSON `ulib_to_ucode_version`). The host getter copy is distinct from every blob copy. No competing dotted-version string exists, so the libtpu-style "phantom version" risk is ruled out: the reported `build_version` is real and is the value the getter actually returns.

> **NOTE — build_version is identical across the 4-gen and 5-gen builds.** `"1.21.1.0"` is byte-identical in the 4-gen front lib and the 5-gen internal twin: adding MAVERICK did **not** bump `build_version`. The OBSERVED fact is the identical string; the interpretation — that `build_version` identifies the ulib/opcode contract generation rather than the binary's gen-count — is `[MED/INFERRED]`.

---

## 2. `nrtucode_get_num_ext_isa_libs` — the per-coretype lib count

This getter answers "how many EXTISA libs does this generation expose?" It takes a coretype and an out-pointer, writes the count, and returns `0` (ok) or `1` (error). The internal twin and the front lib implement it with **different code shapes** but identical observable behaviour for the four shared generations.

### 2a. Internal twin @ `0x9b2c90` — the bitmask form
```asm
9b2c90:  48 c7 06 00 00 00 00   movq   $0x0,(%rsi)            ; *out = 0
9b2c97:  b8 01 00 00 00         mov    $0x1,%eax              ; default return = 1 (error)
9b2c9c:  83 ff 25               cmp    $0x25,%edi             ; coretype > 37 ?
9b2c9f:  77 1c                  ja     9b2cbd                 ;   -> return 1 (out stays 0)
9b2ca1:  89 fa                  mov    %edi,%edx
9b2ca3:  48 b9 00 20 20 20 20.. movabs $0x2020202000,%rcx
9b2cad:  48 0f a3 d1            bt     %rdx,%rcx              ; bit[coretype] set ?
9b2cb1:  73 0b                  jae    9b2cbe                 ;   no -> SUNDA check
9b2cb3:  b9 04 00 00 00         mov    $0x4,%ecx             ; *out = 4
9b2cb8:  48 89 0e               mov    %rcx,(%rsi)
9b2cbb:  31 c0                  xor    %eax,%eax              ; return 0
9b2cbd:  c3                     ret
9b2cbe:  b9 01 00 00 00         mov    $0x1,%ecx             ; *out = 1
9b2cc3:  48 83 fa 06            cmp    $0x6,%rdx             ; coretype == 6 (SUNDA) ?
9b2cc7:  74 ef                  je     9b2cb8                 ;   yes -> *out=1, return 0
9b2cc9:  eb f2                  jmp    9b2cbd                 ;   else return 1
```

```c
/* nrtucode_get_num_ext_isa_libs @ internal 0x9b2c90 */
int nrtucode_get_num_ext_isa_libs(uint32_t coretype, uint64_t *out) {
    *out = 0;
    if (coretype > 37) return 1;                  /* cmp $0x25 */
    if (_bittest64(0x2020202000ULL, coretype)) {  /* bits {13,21,29,37} */
        *out = 4;                                 /* the four PERF gens */
        return 0;
    }
    if (coretype == 6) { *out = 1; return 0; }    /* SUNDA = 1 lib (RELEASE) */
    return 1;                                      /* any other coretype = error */
}
```

The immediate `0x2020202000` has set bits at exactly `{13, 21, 29, 37}` (verified by direct bit-scan). Those are the four "4-lib" PERF coretypes; coretype `6` (SUNDA) is the lone "1-lib" RELEASE case; everything else errors.
| coretype | codename | libs | `*out` | return |
|---:|---|---:|---:|---:|
| 6 | SUNDA (RELEASE) | 1 | 1 | 0 |
| 13 | CAYMAN (PERF) | 4 | 4 | 0 |
| 21 | MARIANA (PERF) | 4 | 4 | 0 |
| 29 | MARIANA_PLUS (PERF) | 4 | 4 | 0 |
| 37 | MAVERICK (PERF) | 4 | 4 | 0 *(internal twin only)* `[INFERRED interior]` |
| any other | — | — | 0 | 1 |

> **NOTE — the count comes from the lib-table size.** `cayman_libs`/`mariana_libs`/`mariana_plus_libs`/`maverick_libs` are `0x40` bytes each = 4 × 16-byte entries; `sunda_libs` is `0x10` bytes = 1 entry. The "4" is the four POOL Q7 EXTISA engines (EXTISA_0..3), stored in **reverse engine order** (idx 3,2,1,0) in the embedded container per the [EXTISA inventory](../images/extisa-inventory.md). "5 generations" but **per-coretype count = 4** for each PERF gen, **1** for SUNDA. Total getter entries = 4·4 + 1 = 17; unique blobs = 13 because MARIANA ≡ MARIANA_PLUS by byte-identity.

### 2b. Front lib @ `0x30b110` — the jump-table form, 4-gen bound
```asm
30b110:  48 c7 06 00 00 00 00   movq   $0x0,(%rsi)
30b117:  b8 01 00 00 00         mov    $0x1,%eax
30b11c:  83 c7 fa               add    $0xfffffffa,%edi      ; edi = coretype - 6
30b11f:  83 ff 17               cmp    $0x17,%edi            ; (coretype-6) > 23  -> coretype>29 ?
30b122:  77 21                  ja     30b145                ;   yes -> return 1
30b124:  48 8d 0d c9 89 cf ff   lea    -0x307637(%rip),%rcx  ; jump table @ VA 0x3af4
30b12b:  48 63 14 b9            movslq (%rcx,%rdi,4),%rdx    ; stride 4 (int32 rel)
30b12f:  48 01 ca               add    %rcx,%rdx
30b132:  ff e2                  jmp    *%rdx                  ; -> 1-lib / 4-lib / error
```

The front lib uses a **24-entry** jump table at VA `0x3af4` bounded by `cmp $0x17` (coretype − 6 ≤ 23 ⇒ coretype ≤ 29). Coretype **37 is out of bounds** here — the shipped front lib knows only four generations and **cannot report MAVERICK**. (See §5.) Behaviour for `{6,13,21,29}` is identical to §2a: ct6→1, ct13/21/29→4, all else→error.

---

## 3. `nrtucode_get_ext_isa` — the dispatcher and the 32-case jump table

### 3a. Public wrapper `nrtucode_get_ext_isa` @ `0x9b2c80`
```asm
9b2c80:  31 c9                  xor    %ecx,%ecx             ; lib = 0
9b2c82:  e9 a9 fe ff ff         jmp    9b2b30                ; tail-call internal worker
```

```c
/* nrtucode_get_ext_isa @ internal 0x9b2c80 / front 0x30b100 */
int nrtucode_get_ext_isa(int coretype, int flavor, void *out_struct) {
    /* the public API HARDCODES lib index 0 = the first/largest EXTISA_0 POOL lib */
    return nrtucode_get_ext_isa_internal(coretype, flavor, out_struct, /*lib=*/0);
}
```

The public entry point always selects lib index **0** (EXTISA_0). A different lib index is reachable only through the internal worker directly (see §4).

### 3b. Worker `nrtucode_get_ext_isa_internal` @ `0x9b2b30` — flavor gate
Args at entry: `edi`=coretype (→ `r15d`), `esi`=flavor, `rdx`=out (→ `rbx`), `rcx`=lib (→ `r14`).

**(i) Flavor validation.** If `flavor != 0`: compute `flavor - 4`; the explicit (non-`0`) flavor is valid iff `(uint32_t)(flavor - 4) >= 0xFFFFFFFD`, i.e. valid explicit flavors are `{1,2,3}`; flavor `0` is handled below; flavor `>= 4` returns error code **2**.

If `flavor == 0` (auto): `getenv("NEURON_UCODE_FLAVOR")` (string at `0x52e2`) and four `strcmp`s drive the choice:

```c
/* flavor == 0 auto path, strings: "NEURON_UCODE_FLAVOR"@0x52e2,
   "debug"@0x4733, "DEBUG"@0x51b8, "test"@0x503e, "TEST"@0x4ddf */
const char *e = getenv("NEURON_UCODE_FLAVOR");
if (e == NULL)            goto dispatch;          /* unset  -> proceed   */
if (!strcmp(e,"debug"))   goto dispatch;          /* "debug"-> proceed   */
if (!strcmp(e,"DEBUG"))   goto dispatch;          /* "DEBUG"-> proceed   */
if (!strcmp(e,"test"))    goto dispatch;          /* "test" -> proceed   */
flavor = 1 + 2 * (strcmp(e,"TEST") == 0);         /* "TEST"->3, else ->1 */
/* range-gate flavor, then proceed */
```

Net: **flavor == 0 always proceeds.** Critically, the flavor value does **not** change which per-gen lib table is chosen — it only gates validity and selects the error code. The dispatch is keyed on coretype alone.

> **CORRECTION — this is `NEURON_UCODE_FLAVOR`, not `NRTUCODE_MPLUS_ON_MARIANA`.** `get_ext_isa_internal` reads `NEURON_UCODE_FLAVOR` (`0x52e2`). The unrelated `NRTUCODE_MPLUS_ON_MARIANA` env belongs to the **separate** `nrtucode_get_memory_image` resolver, where it is now a *removed/deprecation guard* (see §7 and [image-hwdecode-resolvers](image-hwdecode-resolvers.md)). The two functions must not be conflated.
**(ii) The 32-case coretype jump table** @ VA `0x556c`:

```asm
9b2bdb:  41 83 c7 fa            add    $0xfffffffa,%r15d     ; r15 = coretype - 6
9b2bdf:  41 83 ff 1f            cmp    $0x1f,%r15d           ; (coretype-6) > 31 ?
9b2be3:  0f 87 81 00 00 00      ja     9b2c6a                ;   yes -> return 1
9b2be9:  48 8d 0d 7c 29 65 ff   lea    -0x9ad684(%rip),%rcx  ; jump table base @ VA 0x556c
9b2bf0:  4a 63 14 b9            movslq (%rcx,%r15,4),%rdx    ; stride 4: int32 self-relative
9b2bf4:  48 01 ca              add    %rcx,%rdx              ; target = 0x556c + table[idx]
9b2bf7:  ff e2                  jmp    *%rdx
```

The table is **32 entries** of int32 self-relative offsets (base `0x556c`, stride 4), indexed by `idx = coretype - 6`, bounded by `cmp $0x1f` (idx > 31 ⇒ coretype > 37 ⇒ error). Decoded **every slot** from the raw table bytes:

| idx | coretype | rel32 target VA | handler → lib-table base | generation |
|---:|---:|---|---|---|
| 0 | **6** | `0x9b2bf9` | `sunda_libs` @ `0x9b8f80` | **SUNDA** (1 lib, RELEASE) |
| 1–6 | 7–12 | `0x9b2c6a` | *error/default* (return 1) | — |
| 7 | **13** | `0x9b2c13` | `cayman_libs` @ `0x9b8f90` | **CAYMAN** (4 libs, PERF) |
| 8–14 | 14–20 | `0x9b2c6a` | *error/default* | — |
| 15 | **21** | `0x9b2c06` | `mariana_libs` @ `0x9b8fd0` | **MARIANA** (4 libs, PERF) |
| 16–22 | 22–28 | `0x9b2c6a` | *error/default* | — |
| 23 | **29** | `0x9b2c20` | `mariana_plus_libs` @ `0x9b9010` | **MARIANA_PLUS** (4 libs, PERF) |
| 24–30 | 30–36 | `0x9b2c6a` | *error/default* | — |
| 31 | **37** | `0x9b2c2d` | `maverick_libs` @ `0x9b9050` | **MAVERICK** (4 libs, PERF) `[INFERRED interior]` |

Only **5 of 32** slots are live — coretypes `{6, 13, 21, 29, 37}`; the other 27 fall to the error tail at `0x9b2c6a` (`return 1`). This maps coretype → EXTISA blob set exactly as the [EXTISA inventory](../images/extisa-inventory.md) §3 and the [codename → generation map](../generations/codename-generation-map.md) §1 describe, cross-checked below.

> **NOTE — `{6,13,21,29,37}` is the DEVICE Q7-POOL coretype set, distinct from the HOST coretype set `{2,9,17,25,32}`.** This ext-isa dispatch is keyed on the **Q7-POOL** coretypes `{6,13,21,29,37}` = SUNDA/CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK Q7-POOL. The **core-struct validity gate** documented in the committed `nrtucode-core` and object-model-graph pages keys on a different set, the HOST coretypes `{2,9,17,25,32}` (the `NX_ACT`/host-engine ordinals of the same five generations). Do not cross-apply the two sets: a value of `6` is a *valid* Q7-POOL coretype here but is *not* in the host validity set, and vice versa. The two sets are the same five generations viewed from two enum positions in the one `nrtucode_coretype_t` enum (Q7-POOL slot vs `NX_ACT` slot per generation).

> **GOTCHA — the `get_hwdecode_table` coretype set is yet a THIRD, orthogonal set.** The sibling `nrtucode_get_hwdecode_table` @ `0x9b2cd0` (immediately after this getter) keys on `{7–10, 15–18, 23–26, 31–33}` — the *intra-generation engine* indices, not the per-gen POOL coretypes. It has nothing to do with the EXTISA `{6,13,21,29,37}` set. See [image-hwdecode-resolvers](image-hwdecode-resolvers.md).

**(iii) Per-gen handler + out-struct fill**. Each live slot runs the same epilogue (shown for the CAYMAN slot `0x9b2c13`; the others differ only in the lib-table `lea`):

```asm
9b2c13:  49 c1 e6 04           shl    $0x4,%r14             ; r14 = lib * 16 (16B per entry)
9b2c17:  48 8d 0d 72 63 00 00  lea    cayman_libs(%rip),%rcx ; -> 0x9b8f90
9b2c38:  4c 01 f1              add    %r14,%rcx             ; rcx = &table[lib]
9b2c3b:  48 8b 11              mov    (%rcx),%rdx           ; SO_get fnptr
9b2c3e:  b8 03 00 00 00        mov    $0x3,%eax             ; preload return 3
9b2c43:  48 85 d2              test   %rdx,%rdx ; je 9b2c6a ; SO_get NULL -> return 3
9b2c48:  4c 8b 71 08           mov    0x8(%rcx),%r14        ; JSON_get fnptr
9b2c4c:  4d 85 f6              test   %r14,%r14 ; je 9b2c6a ; JSON_get NULL -> return 3
9b2c51:  48 8d 73 08           lea    0x8(%rbx),%rsi
9b2c55:  48 89 df              mov    %rbx,%rdi
9b2c58:  ff d2                 call   *%rdx                 ; SO_get(&out.so_ptr, &out.so_len)
9b2c5a:  48 8d 7b 10           lea    0x10(%rbx),%rdi
9b2c5e:  48 83 c3 18           add    $0x18,%rbx
9b2c62:  48 89 de              mov    %rbx,%rsi
9b2c65:  41 ff d6              call   *%r14                 ; JSON_get(&out.json_ptr, &out.json_len)
9b2c68:  31 c0                 xor    %eax,%eax             ; return 0
```

```c
struct ext_isa_out {           /* 0x20 bytes */
    void   *so_ptr;            /* +0x00 — EXTISA Q7 .so blob base */
    size_t  so_len;            /* +0x08 */
    void   *json_ptr;          /* +0x10 — JSON manifest base */
    size_t  json_len;          /* +0x18 */
};

/* per-gen handler (CAYMAN shown) */
{
    struct lib_entry { void (*so_get)(void**, size_t*);
                       void (*json_get)(void**, size_t*); };
    struct lib_entry *e = &cayman_libs[lib];          /* base + lib*16 */
    if (!e->so_get)   return 3;                        /* empty slot */
    if (!e->json_get) return 3;
    e->so_get  (&out->so_ptr,   &out->so_len);
    e->json_get(&out->json_ptr, &out->json_len);
    return 0;
}
```

**(iv) Return codes**:

| code | meaning |
|---:|---|
| 0 | success — `out` (0x20 bytes) filled |
| 1 | invalid coretype (not in `{6,13,21,29,37}`) |
| 2 | invalid flavor (`flavor >= 4`) |
| 3 | lib slot empty / NULL fnptr (lib index out of range for that gen, or SUNDA weak-undef unresolved) |

### 3c. The per-gen lib tables (the data behind the dispatch)
The five lib tables live in `.data.rel.ro` (VMA `0x9b8cf0`, file `0x9b6cf0`, Δ `0x2000`). Each is an array of 16-byte `{SO_get, JSON_get}` entries. The pointers are materialised by relocations: the four PERF gens use `R_X86_64_RELATIVE` (resolved at load to the in-image getter stubs), while **SUNDA** uses `R_X86_64_64` against its **weak-undefined** symbols (resolved from the EXTISA container at final link). `nm` confirms the table symbols at their addresses, and `readelf -rW` confirms the slot relocs:

| table | base | entries | first SO/JSON stub | reloc kind |
|---|---|---:|---|---|
| `sunda_libs` | `0x9b8f80` | 1 | `SUNDA_Q7_POOL_RELEASE_EXTISA_0_{SO,JSON}_get` (weak `w`) | `R_X86_64_64` addend 0 |
| `cayman_libs` | `0x9b8f90` | 4 | `0x9b3aa0` / `0x9b3ac0` | `R_X86_64_RELATIVE` |
| `mariana_libs` | `0x9b8fd0` | 4 | `0x9b4720` / `0x9b4740` | `R_X86_64_RELATIVE` |
| `mariana_plus_libs` | `0x9b9010` | 4 | `0x9b53a0` / `0x9b53c0` | `R_X86_64_RELATIVE` |
| `maverick_libs` | `0x9b9050` | 4 | `0x9b5ba0` / `0x9b5bc0` | `R_X86_64_RELATIVE` `[INFERRED interior]` |

The getter stubs are the 4-instruction `{blob,size}` form. For example `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` @ `0x9b3aa0`:

```asm
9b3aa0:  48 8d 05 39 bd 93 ff   lea    -0x6c42c7(%rip),%rax   ; blob @ 0x2ef7e0
9b3aa7:  48 89 07               mov    %rax,(%rdi)            ; *out_ptr = blob
9b3aaa:  48 c7 06 60 a2 00 00   movq   $0xa260,(%rsi)         ; *out_len = 0xa260
9b3ab1:  c3                     ret
```

There are exactly **16 named SO_get + 16 named JSON_get** stubs (4 each × CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK) plus the **1 SUNDA weak-undef pair** — confirmed by `nm` symbol count. CAYMAN/MARIANA/MARIANA_PLUS EXTISA_0 each carry SO length `0xa260`; **MAVERICK EXTISA_0 differs** (`0x7fb0`, blob @ `0x994de0`), corroborating that MAVERICK is a distinct, newer build rather than a copy. These VAs and sizes reconcile with the [EXTISA inventory](../images/extisa-inventory.md) §2 carve census.

### 3d. Front lib `get_ext_isa` (the shipped 4-gen form)
`nrtucode_get_ext_isa` @ `0x30b100`: `xor %ecx,%ecx ; jmp 0x30afc0` (worker, lib=0). The worker `nrtucode_get_ext_isa_internal` @ `0x30afc0` has the same flavor gate, and a **24-entry** jump table at VA `0x3a94` bounded by `cmp $0x17` (coretype ≤ 29):

| coretype | front handler | front lib base |
|---:|---|---|
| 6 | `0x30b085` | `0x30f740` (sunda) |
| 13 | `0x30b09f` | `0x30f750` (cayman) |
| 21 | `0x30b092` | `0x30f790` (mariana) |
| 29 | `0x30b0ac` | `0x30f7d0` (mariana_plus) |
| all else | `0x30b0e9` | error |

Exactly **four** lib bases; **no MAVERICK**. Same `{SO_get,JSON_get}` 16-byte entries, SUNDA again weak-undef (`R_X86_64_64` to `SUNDA_…_EXTISA_0_{SO,JSON}_get`).

---

## 4. Consumer — who passes a non-zero lib index

The public `nrtucode_get_ext_isa` hardcodes lib 0, but `nrtucode_ll_create` calls the worker directly with a **non-zero** lib index:

```c
/* nrtucode_ll_create (sibling lifecycle page) */
int v16 = 3;
if (!getenv("NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE")) v16 = a4;
if (a4) v16 = a4;
result = nrtucode_get_ext_isa_internal(coretype, flavor, &out, v16);
```

So the lifecycle path selects EXTISA lib index = `a4`, defaulting to **3** when the undocumented env `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` is unset **and** `a4 == 0`. Lib index 3 is EXTISA_3 (the `cptc_decode` / extended-instruction variant). The `cptc_decode → lib3` binding is `[MED/INFERRED]` from the env name + the constant `3` + the inventory's decode-kernel roster; the exact opcode semantics are out of scope here.

---

## 5. MAVERICK inclusion — the "4 vs 5" reconciliation
The generation count differs by **binary**, not by an inconsistent table inside one binary:

| packaging view | EXTISA generations | MAVERICK | dispatch bound |
|---|---|---|---|
| `libnrtucode.so` (shipped front; jump-table `get_num`) | SUNDA, CAYMAN, MARIANA, MARIANA_PLUS | **NO** | coretype ≤ 29 (`cmp $0x17`) |
| `libnrtucode_internal.so` (twin; bitmask `get_num`) | + MAVERICK (5) | **YES** | coretype ≤ 37 (`cmp $0x25` / `cmp $0x1f`) |
| `libnrtucode.a` (static) | CAYMAN, MARIANA, MARIANA_PLUS contents | **NO** | n/a |
| `libnrtucode_extisa.so` (container) | SUNDA, CAYMAN, MARIANA, MARIANA_PLUS | **NO** | n/a |

Hard evidence: `rg -c -a MAVERICK` → **0** in the front lib, **187** in the internal twin; the internal worker has 5 lib-table `lea` sites (incl. `maverick_libs` @ `0x9b9050`), the front has 4 (max base `0x30f7d0`); the internal `get_num` bound is `cmp $0x25` with bitmask bit 37 set, the front bound is `cmp $0x17` with the MAVERICK slot absent; the four MAVERICK Q7 ELFs exist only inside the internal twin's `.rodata`.

> **NOTE — coretype 37 is OBSERVED here; only the v5 *interior* is INFERRED.** The MAVERICK slot at idx 31 → `maverick_libs` is read directly from the jump-table bytes and is therefore OBSERVED in the internal twin (and confirmed by the `nrtucode.h` enum ordinal placing `MAVERICK_Q7_POOL` at 37). What is INFERRED `[MED]` is the v5 *silicon binding*: the MAVERICK `arch_id` (`36*`, derived as coretype − 1, with no NCFW firmware byte stating 36) and the interpretation that the internal twin is a newer 5-gen build while the shipped front lib is the older 4-gen one. The shipped runtime path is **4-gen** (no MAVERICK), consistent with the sibling libtpu/libncfw "shipped 4 not 5" shape; MAVERICK lives only in the symbol-twin's dispatch and its internal-only blobs. The "v5 coretype 37 OBSERVED / arch_id 36 INFERRED" split is carried as an open question on the [Open-Questions Register](../appendix/open-questions-register.md).

---

## 6. Coretype → arch_id mapping the getters key on

`libnrtucode` keys **everything** on coretype (`core+0x10`, via `nrtucode_get_coretype` @ `0x9b0a10`). The sibling `libncfw` keys on arch_id. Reconciled `[HIGH/OBSERVED for the four lower gens; MAVERICK arch_id INFERRED]`:

| codename | coretype | arch_id | Δ | ncfw_ver |
|---|---:|---:|---:|---|
| SUNDA | 6 | 5 | 1 | v2 |
| CAYMAN | 13 | 12 | 1 | v3 |
| MARIANA | 21 | 20 | 1 | v4 |
| MARIANA_PLUS | 29 | 28 | 1 | v4+ |
| MAVERICK | 37 | 36* | 1 | *(no ncfw v5 observed)* `[INFERRED]` |

Uniformly `coretype = arch_id + 1`. The bitmask `0x2020202000` (bits 13,21,29,37) and the jump-table index `(coretype-6)` are both pure **coretype** encodings — no arch_id appears anywhere in `libnrtucode`; the coretype→arch_id translation happens in a lower NRT/ncfw layer. Full derivation in the [codename → generation map](../generations/codename-generation-map.md).

---

## 7. `get_memory_image` boundary (separate resolver)
`nrtucode_get_memory_image` @ `0x9b2960` is a **different, broader** resolver and must not be confused with the EXTISA path:

```c
int nrtucode_get_memory_image(uint coretype, int region, int flavor,
                              void *out_ptr, void *out_len);
```

It bounds `coretype ≤ 37` (`cmp $0x25`), checks `getenv("NRTUCODE_MPLUS_ON_MARIANA")` as a **removed/deprecation guard** (a non-`"1"` value emits the "flag has been removed from libnrtucode; please transition to … `NEURON_RT_DBG_V4_PLUS=0/1`" error and returns **8**), indexes `image_list` @ `0x9b8d20` (38 × 16-byte `{count, table_ptr}` by coretype) with the **same** `NEURON_UCODE_FLAVOR` auto-flavor logic, scans 40-byte region descriptors, and routes through a **4-way region jump table** @ `0x555c` (region 0→`+8`, 1→`+0x10`, 2→`+0x18`, 3→`+0x20` = IRAM/DRAM/SRAM/EXTRAM).

> **BOUNDARY.** `get_ext_isa_internal` has **0** references to `image_list`; it is an independent dispatch over the per-gen EXTISA lib tables. `get_memory_image` resolves the **base region images** (IRAM/DRAM/SRAM/EXTRAM), **not** EXTISA. The two share `(coretype, NEURON_UCODE_FLAVOR)` but are distinct functions over distinct tables; EXTISA is its own region, surfaced only via `get_ext_isa`. The full `image_list`/region decode is the [image + HW-decode resolvers](image-hwdecode-resolvers.md) page.

---

## 8. Reconciliation summary

* Jump-table targets `{6→sunda, 13→cayman, 21→mariana, 29→mariana_plus, 37→maverick}` reproduce the [EXTISA inventory](../images/extisa-inventory.md) §3 and [codename → generation map](../generations/codename-generation-map.md) §1 **exactly** (VA `0x556c`, 32 entries, 4-byte stride). **No divergence** — no CORRECTION required against either committed page.
* Lib-table bases (`sunda_libs`@`0x9b8f80`, `cayman_libs`@`0x9b8f90`, `mariana_libs`@`0x9b8fd0`, `mariana_plus_libs`@`0x9b9010`, `maverick_libs`@`0x9b9050`) match the inventory §4 exactly.
* `build_version 1.21.1.0` equals the SUNDA real-JSON `ulib_to_ucode_version 1.21.1.0` embedded in the SUNDA EXTISA blob — version is real, phantom ruled out.
* The CORRECTIONs embedded on this page are local clarifications, not contradictions of committed pages: (a) `get_ext_isa_internal` reads `NEURON_UCODE_FLAVOR`, **not** `NRTUCODE_MPLUS_ON_MARIANA` (§3b); (b) the EXTISA dispatch keys on the **Q7-POOL** coretype set `{6,13,21,29,37}`, distinct from the **HOST** core-validity set `{2,9,17,25,32}` and from the `get_hwdecode_table` engine set `{7–10,15–18,23–26,31–33}` (§3b NOTE/GOTCHA).

## 9. Confidence / honest gaps

**HIGH / OBSERVED** — both version getters + their strings (`"1.21.1.0"`, the 40-hex git SHA); `api_level = 3`; phantom-version ruled out; `get_num_ext_isa_libs` counts (sunda 1, others 4) + bitmask `{13,21,29,37}` + ct6; the `lib=0` wrapper; the worker's flavor gate, the 32-entry jump table @ `0x556c` with all 32 slots decoded, the 5 per-gen handlers, the 0x20-byte out-struct, return codes 0/1/2/3; lib-table slot→getter symbol resolution via relocs; MAVERICK present only in the internal twin (187 refs, ct=37 handler) and absent from the front lib (0 refs, bound ct ≤ 29).

**MED / INFERRED** — `build_version` tracks the opcode/ulib contract gen, not the binary gen-count (from the identical string across builds); lib index 3 == `cptc_decode`/EXTISA_3 (from env name + value 3 + decode roster); MAVERICK arch_id 36 (from `coretype = arch_id + 1`, no ncfw v5 image to confirm); "internal twin = newer 5-gen build, front lib = older 4-gen shipped one" (from the MAVERICK toolchain being newer + the bound/ref divergence). All MAVERICK(37)/v5 *interior* claims are flagged INFERRED at their site.

**LOW / NOT CLAIMED** — which physical silicon part each coretype binds to; the full `image_list`/region descriptor decode (the [image-hwdecode-resolvers](image-hwdecode-resolvers.md) deliverable); whether a field deployment links the 4-gen front lib or a 5-gen internal build (out of these two binaries' scope).
