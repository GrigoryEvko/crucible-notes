# Opcode-Set → Library Resolver

`nrtucode_ll_get_libraries_from_opcodes` is the host-side resolver that the
public ABI advertises as the front of the loadable-library pipeline: feed it the
set of opcodes a model uses plus the target core, and it hands back the firmware
**library UIDs** that must be staged to the device to cover those opcodes. The
header (`nrtucode.h:339-357`) frames it as an opcode→library *bitmap* union — a
"list of library UIDs which are required to cover the required opcodes".

The shipped binary tells a sharper, simpler story. **The resolver never reads the
opcode array.** It is opcode-*content*-blind: it keys on `core_type`, on a single
`num_opcodes != 0` non-empty gate, and on one environment variable, then emits a
**single scalar library index** (`0` or `3`) into `libs[0]` with `*num_libs = 1`.
There is no per-opcode lookup, no bitmap OR, no dependency closure. The
opcode→library knowledge the brief expects lives downstream — in the device
`kernel_info_table` *inside* the staged library, not in this host function.

This page decodes the resolver byte-exact in both shipped twins, decodes the two
`.rodata` tables that drive it (the per-coretype bitmask and the stripped jump
table), reconciles the lib-index↔EXTISA-image correspondence against the image catalog, and pins where the real per-opcode dispatch actually happens.

> All addresses, bytes, table entries, and strings below were read directly from
> the two shipped host libraries with stock `objdump`/`nm`/`readelf` and a small
> Python `.rodata` reader. Recovered symbol names, strings, and the public header
> are binary-derived artifacts and are cited as such. The page default is
> *[HIGH/OBSERVED]*; claims that depart from it carry an explicit tag.

---

## 0. Binaries, grounding, and the ZTV note

| Item | Value | Source |
| --- | --- | --- |
| Shipped runtime (authoritative) | `libnrtucode.so` (stripped) | `extracted/…/c10/lib/libnrtucode.so` |
| Symbol twin | `libnrtucode_internal.so` (10,276,288 B, **not** stripped) | same dir |
| BuildID (stripped) | `abf4e088ebef327b2abac3551f2b1de699d50f38` | `readelf -nW` |
| BuildID (internal) | `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd` | `readelf -nW` |
| `.rodata` | VMA `0x46b0`, file off `0x46b0` → **Δ = 0** | `readelf -SW` |
| `.text` | VMA `0x9b01a0`, file off `0x9af1a0` → **Δ = 0x1000** | `readelf -SW` |
| `_ZTV` vtable count | **0** (`nm libnrtucode_internal.so \| rg -c '_ZTV'` → empty) | |

> **GOTCHA — no C++ vtables in this binary.** `libnrtucode_internal.so` has
> **zero** `_ZTV` symbols. Every fn-ptr / data table referenced here (the
> per-coretype bitmask, the stripped jump table, the per-gen `*_libs` tables) is
> a plain C array: slot *N* sits at `table_symbol + 8*N`, first element at
> `+0x00`. The `_ZTV + 0x10` (offset-to-top/typeinfo header) rule does **not**
> apply. Counts on this page are grounded with `nm … | rg -c`, never a decompile.

Symbol locations (`nm`, both twins):

| Symbol | internal `.symtab` | stripped `.dynsym` |
| --- | --- | --- |
| `nrtucode_ll_get_libraries_from_opcodes` | `0x9b1880` | `0x309d30` |
| `nrtucode_opset_get_library_index` | `0x9b1950` | `0x309e00` |
| `nrtucode_ll_create` (consumer) | `0x9b1a90` | — |
| `nrtucode_ll_get_load_sequence` | `0x9b1fb0` | — |
| `nrtucode_ll_get_unload_sequence` | `0x9b2440` | — |

---

## 1. Signature and the ABI-vs-binary mismatch

From `nrtucode.h:339-357` (read verbatim from the shipped header):

```c
nrtucode_result_t nrtucode_ll_get_libraries_from_opcodes(
        nrtucode_coretype_t core_type,    /* edi  : per-coretype selector (the ONLY real input) */
        uint8_t   opcodes[],              /* rsi  : "unique opcodes in this model"  — IGNORED    */
        size_t    num_opcodes,            /* rdx  : count of opcodes[] (only != 0 is read)       */
        uint8_t   subopcodes[],           /* rcx  : "unique subopcodes in this model" — IGNORED  */
        size_t    num_subopcodes,         /* r8   : count of subopcodes[]            — IGNORED   */
        size_t   *libs,                   /* r9   : OUT — list of library UIDs (libs[0] only)    */
        size_t   *num_libs);              /* rsp+0x20 (7th arg, on stack): OUT count              */
```

Return values (`nrtucode.h:100-110`): `0 = NRTUCODE_SUCCESS`,
`1 = NRTUCODE_ERR_UNKNOWN_CORE` (core not a Q7_POOL coretype).

> **CORRECTION — the header's `[1, num_opcodes]` range is stale; the binary
> writes `0`.** `nrtucode.h:350-351` promises *"`libs` … guaranteed to be in the
> range `[1, num_opcodes]`"* and *"`num_libs` … guaranteed to be in range
> `[1, num_opcodes]`"*. The shipped function **violates both**: it writes
> `libs[0] = 0` for the common base-library case (outside `[1, …]`), and writes
> `*num_libs = 0` when `num_opcodes == 0` (an empty model). The header reads like
> a forward-looking spec for a per-opcode-UID scheme that was never implemented;
> the shipped resolver is a degenerate one-library picker. **Code wins.**
>

The `subopcodes`/`num_subopcodes` arguments exist only to match that aspirational
spec. The function never NULL-guards them and never reads them — `rcx` (the
`subopcodes` pointer) is even *overwritten* with `core_type` at `0x9b18b0` before
any possible use (§2). The doc cross-reference to a
`nrtucode_ll_get_library_index_from_opcodes` (`nrtucode.h:365`) names a function
that does not exist as a symbol in either twin — the live entry point is the
plural `…get_libraries_from_opcodes`.

NULL-argument contract is **fatal** (not error-returned), matching the
`fprintf(stderr, "…is null") + abort()` idiom used across the ll-create /
add-instruction family:

| Guarded arg | Reg | NULL → | arg-name string (`.rodata`) |
| --- | --- | --- | --- |
| `libs` | `r9` | `abort()` | `"libs"` @ `0x51cc` |
| `num_libs` | `0x20(%rsp)` | `abort()` | `"num_libs"` @ `0x551f` |

Format string @ `0x5469` = `"nrtucode: invalid API usage in \`%s\`: \`%s\` is null\n"`;
function-name token @ `0x4cee` = `"nrtucode_ll_get_libraries_from_opcodes"`.
All four offsets read directly from the binary.

---

## 2. The resolver body — byte-exact (`internal @0x9b1880`)

The complete disassembly (`objdump -d`, internal twin). The annotated trace is
the reconstructable spec; every instruction is OBSERVED.

```
9b1880  41 56              push %r14
9b1882  53                 push %rbx
9b1883  50                 push %rax              ; prologue: 8B stack realign, NO frame
9b1884  4d 85 c9           test %r9,%r9
9b1887  74 6f              je   9b18f8            ; libs == NULL  -> abort("libs")
9b1889  4c 8b 74 24 20     mov  0x20(%rsp),%r14   ; r14 = num_libs (7th arg, off the stack)
9b188e  4d 85 f6           test %r14,%r14
9b1891  0f 84 8c 00 00 00  je   9b1923            ; num_libs == NULL -> abort("num_libs")
9b1897  49 c7 06 00..00    movq $0x0,(%r14)       ; *num_libs = 0   (pre-initialise)
9b189e  48 85 d2           test %rdx,%rdx
9b18a1  74 41              je   9b18e4            ; num_opcodes == 0 -> SUCCESS, num_libs stays 0
9b18a3  b8 01 00 00 00     mov  $0x1,%eax         ; default rc = 1 (ERR_UNKNOWN_CORE)
9b18a8  83 ff 25           cmp  $0x25,%edi        ; coretype vs 37
9b18ab  77 39              ja   9b18e6            ; coretype > 37 -> return 1
9b18ad  4c 89 cb           mov  %r9,%rbx          ; rbx = libs
9b18b0  89 f9              mov  %edi,%ecx         ; rcx = coretype  (CLOBBERS subopcodes ptr)
9b18b2  48 ba 00 20 20 20  movabs $0x2020202000,%rdx  ; <<< per-coretype LIB-SET BITMASK
        20 00 00 00
9b18bc  48 0f a3 ca        bt   %rcx,%rdx         ; test bit[coretype]
9b18c0  73 2c              jae  9b18ee            ; bit CLEAR -> SUNDA/default arm (9b18ee)
   --- PERF arm: coretype in {13,21,29,37} (bit set) ---
9b18c2  48 8d 3d ..        lea  -0x9acbb4(%rip),%rdi  ; "NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE" @0x4d15
9b18c9  e8 a2 63 00 00     call getenv@plt
9b18ce  31 c9              xor  %ecx,%ecx
9b18d0  48 85 c0           test %rax,%rax
9b18d3  0f 95 c1           setne %cl              ; cl = (env != NULL) ? 1 : 0
9b18d6  48 8d 04 49        lea  (%rcx,%rcx,2),%rax ; rax = rcx + 2*rcx = 3*cl  => 0 or 3 (the lib UID)
9b18da  48 89 03           mov  %rax,(%rbx)       ; libs[0] = (env set ? 3 : 0)
9b18dd  49 c7 06 01..00    movq $0x1,(%r14)       ; *num_libs = 1
9b18e4  31 c0              xor  %eax,%eax         ; rc = 0 (SUCCESS)
9b18e6  48 83 c4 08        add  $0x8,%rsp
9b18ea  5b                 pop  %rbx
9b18eb  41 5e              pop  %r14
9b18ed  c3                 ret
   --- SUNDA / default arm: bit CLEAR @9b18ee ---
9b18ee  48 83 f9 06        cmp  $0x6,%rcx         ; coretype == 6 (SUNDA) ?
9b18f2  75 f2              jne  9b18e6            ; no -> return 1 (UNKNOWN_CORE)
9b18f4  31 c0              xor  %eax,%eax         ; yes: rax = 0  (lib 0, no CPTC for SUNDA)
9b18f6  eb e2              jmp  9b18da            ; -> libs[0]=0, *num_libs=1, SUCCESS
   --- abort tails @9b18f8 / @9b1923 ---
9b18f8  …  fprintf(stderr, NULL_FMT, "nrtucode_ll_get_libraries_from_opcodes", "libs");   abort()
9b1923  …  fprintf(stderr, NULL_FMT, "nrtucode_ll_get_libraries_from_opcodes", "num_libs"); abort()
```

### 2a. Proof that `opcodes[]` / `subopcodes[]` / `num_subopcodes` are never read

`rg '%rsi|%r8|\(%rsi\)|\(%rcx\)|\(%r8\)'` over the body returns **only** the two
`lea …,%rsi` that load the abort-format-string pointer (`0x9b1902`, `0x9b192d`).
`rsi` (= `opcodes`), `r8` (= `num_subopcodes`), and the original `rcx`
(= `subopcodes`) are **never dereferenced and never iterated**; `rcx` is reused at
`0x9b18b0` to hold `core_type`, destroying the `subopcodes` pointer before any use.
The only non-abort external call in the whole body is `getenv`. The resolver is a
pure `(core_type, num_opcodes != 0, CPTC-env)` decision.

> **NOTE — `lea (%rcx,%rcx,2)` is the entire "library bitmap".** `rcx ∈ {0,1}`
> (the `setne cl` result), so `rax = 3·rcx ∈ {0,3}`. That single LEA *is* the
> opcode→library "mapping": it maps "is the CPTC env set?" to lib UID `0` or `3`.
> No table is indexed; the value is computed.

---

## 3. The shipped (stripped) form — jump table, 4-gen, no Maverick (`@0x309d30`)

`libnrtucode.so` (the authoritative runtime) implements identical *logic* with
different *codegen*: a `.rodata` jump table replaces the `bt`/bitmask. This is the
same shipped-vs-internal split the ll-create / get-ext-isa pages document.

```
309d47  movq $0x0,(%rbx)                ; *num_libs = 0   (rbx holds num_libs here)
309d4e  test %rdx,%rdx ; je 309d9c      ; num_opcodes == 0 -> SUCCESS
309d53  mov  $0x1,%eax                  ; default rc = 1
309d58  add  $0xfffffffa,%edi          ; edi = coretype - 6   (0xfffffffa = -6)
309d5b  cmp  $0x17,%edi ; ja 309d9e     ; (coretype-6) > 23  => coretype > 29 -> ret 1
309d60  lea  <jt@0x39c4>,%rcx
309d67  movslq (%rcx,%rdi,4),%rdx ; add %rcx,%rdx ; jmp *%rdx     ; idx = coretype-6
   309d70 (PERF): getenv("NRT_UCODE…_CPTC_DECODE"); cl=setne; rax = lea(rcx,rcx,2) = 3*cl ; jmp 309d92
   309d90 (SUNDA): xor %eax,%eax (rax = 0); fall to 309d92
   309d92: mov %rax,(%r9)   ; libs[0] = {0 or 3}
   309d95: movq $0x1,(%rbx) ; *num_libs = 1
   309d9c: xor %eax,%eax ; … ret    ; SUCCESS
   309d9e: … ret                    ; rc still 1 = UNKNOWN_CORE
```

### 3a. The jump table @ `.rodata` VA `0x39c4` — decoded

24 signed `rel32` entries; `idx = coretype − 6`; `target = 0x39c4 + rel32`. Decoded
with a Python reader straight from the file. Every row carries its coretype and its
meaning — no bare address list.

| idx | coretype | enum member | target VA | meaning |
| --- | --- | --- | --- | --- |
| 0 | 6 | `SUNDA_Q7_POOL` | `0x309d90` | **SUNDA arm** → lib `0` (no CPTC; 1-lib gen) |
| 7 | 13 | `CAYMAN_Q7_POOL` | `0x309d70` | **PERF arm** → getenv → lib `0` or `3` |
| 15 | 21 | `MARIANA_Q7_POOL` | `0x309d70` | **PERF arm** → getenv → lib `0` or `3` |
| 23 | 29 | `MARIANA_PLUS_Q7_POOL` | `0x309d70` | **PERF arm** → getenv → lib `0` or `3` |
| 1–6, 8–14, 16–22 | 7–12, 14–20, 22–28 | NX cores / `Q7_CCE` | `0x309d9e` | `return 1` (UNKNOWN_CORE) |
| (out of bound) | ≥ 30 (incl. 37 = MAVERICK) | — | — | `cmp $0x17` rejects → `return 1` |

The stripped strings are identical text to the internal twin: CPTC env @ `0x3084`,
fn-name @ `0x305d`, `"libs"` @ `0x3632`, `"num_libs"` @ `0x3985`.

> **NOTE — shipped omits Maverick; internal accepts it.** The shipped jump table
> bounds `coretype − 6 ≤ 23` (`cmp $0x17`), so the accepted set is
> **`{6, 13, 21, 29}`**. The internal bitmask form (`movabs $0x2020202000` +
> `cmp $0x25` = 37) additionally accepts **`37` (MAVERICK)**. Treat
> `{6, 13, 21, 29}` as the authoritative shipped lib-set keys; `37` is an
> internal-only twin artifact.

---

## 4. The coretype → library-set table

### 4a. The key is the Q7_POOL coretype of each generation

The bitmask / jump-table index is `core_type`, and the only accepted values are
the **GPSIMD pooling core** (`Q7_POOL`) of each generation. From the
`nrtucode_coretype_t` enum (`nrtucode.h:13-58`, ordinals counted directly from the
enum block — `SUNDA_Q7_POOL` is the 7th member, ordinal 6; each generation is an
8-core stride):

| Generation | Enum member (Q7 pooling core) | coretype | arch_id | NCFW |
| --- | --- | --- | --- | --- |
| SUNDA | `NRTUCODE_CORE_SUNDA_Q7_POOL` | 6 | 5 | v2 |
| CAYMAN | `NRTUCODE_CORE_CAYMAN_Q7_POOL` | 13 | 12 | v3 |
| MARIANA | `NRTUCODE_CORE_MARIANA_Q7_POOL` | 21 | 20 | v4 |
| MARIANA_PLUS | `NRTUCODE_CORE_MARIANA_PLUS_Q7_POOL` | 29 | 28 | v4+ |
| MAVERICK | `NRTUCODE_CORE_MAVERICK_Q7_POOL` (internal-only) | 37 | 36* | — |

`coretype = arch_id + 1`; `+8` stride per generation. The MAVERICK block is gated
behind `#if defined(NRTUCODE_INTERNAL_NAMES)` in the header (`nrtucode.h:49-57`) —
consistent with it being accepted only by the internal twin. (`arch_id 36`
INFERRED from the `+1` pattern, MED; coretype ordinals OBSERVED from the enum.)

### 4b. The two `.rodata` decision masks, decoded

| Mask (constant) | In function | Set bits | Meaning |
| --- | --- | --- | --- |
| `0x2020202000` | `ll_get_libraries_from_opcodes` (`bt` @ `0x9b18bc`) | `{13, 21, 29, 37}` | **PERF gens** (getenv arm). SUNDA(6) is the bit-*clear* special case handled by the `cmp $0x6` arm. |
| `0x2020202040` | `opset_get_library_index` (`bt` @ `0x9b1a29`) | `{6, 13, 21, 29, 37}` | All valid Q7_POOL coretypes (§5) — SUNDA's bit is set here because there is no value to special-case. |

Bit positions decoded with Python (`for i in range(64): (m>>i)&1`). Net accepted
set for this resolver = `{6, 13, 21, 29 (, 37 internal)}` = the `Q7_POOL` coretype
of every generation. The NX cores (ACT/DVE/POOL/PE/SP/TOPSP) and `Q7_CCE` are
**rejected** — the EXTISA libraries are a `Q7_POOL`-only artifact (cf.
`nrtucode.h:79` *"Q7_POOL only"*).

### 4c. The per-coretype library set (how many libs each gen has)

The lib UID this resolver emits (`0` or `3`) is a 0-based index into a per-gen
library table whose *size* is decided by `nrtucode_get_num_ext_isa_libs`.
Cross-checked against the [EXTISA image catalog](../images/extisa-inventory.md):

| Generation | coretype | # EXTISA libs | Valid index range | Reachable here |
| --- | --- | --- | --- | --- |
| SUNDA | 6 | **1** (`EXTISA_0` only; RELEASE) | `{0}` | `0` only |
| CAYMAN | 13 | **4** (`EXTISA_0..3`; PERF) | `{0,1,2,3}` | `0` or `3` |
| MARIANA | 21 | **4** (`EXTISA_0..3`; PERF) | `{0,1,2,3}` | `0` or `3` |
| MARIANA_PLUS | 29 | **4** (`EXTISA_0..3`; PERF) | `{0,1,2,3}` | `0` or `3` |
| MAVERICK | 37 | **4** (internal twin only) | `{0,1,2,3}` | `0` or `3` |

This is **exactly why SUNDA is special-cased** to lib `0` and never takes the
getenv branch: with a one-entry lib table, index `3` would point past the end of
`sunda_libs`. The PERF gens have a 4-entry table, so index `3` (`EXTISA_3`) is a
legal slot. *[HIGH/OBSERVED gating; per-gen counts cross-checked against the
image catalog.]*

### 4d. The "library bitmap" is a scalar index, not a bit-set

> **CORRECTION — the "opcode→library bitmap" / "OR together the required
> library bitmap" model does not describe this binary.** The output is a *single*
> library UID written to `libs[0]` with `*num_libs = 1` (always 1 on non-empty
> success, never a multi-element list, never a bitwise OR). There is no closure,
> no dependency graph, no second library. The "bitmap" is, in this binary, a
> single integer **index** (`0` or `3`) — the same finding the
> [ll-create selector](nrtucode-ll-create.md) reports for its `library_index`. A
> reimplementer building the opcode→lib map from this page must implement a
> **picker**, not a union.

---

## 5. The parallel resolver — `nrtucode_opset_get_library_index` (`@0x9b1950`)

The header (`nrtucode.h:615-632`) documents a second, opset-based resolver:

```c
nrtucode_result_t nrtucode_opset_get_library_index(
        nrtucode_opset_t *opset /*rdi*/, nrtucode_coretype_t core_type /*esi*/,
        size_t *lib_index /*rdx*/);
```

Body (internal `@0x9b1950`):

```
9b1951  test %rdi,%rdi ; je <abort "opset">         ; opset NULL -> abort (.rodata 0x4a16)
9b195a  test %rdx,%rdx ; je <abort "lib_index">     ; lib_index NULL -> abort (0x4729)
9b1963..9b1a09  SSE2 popcount of opcode_slot[256]   ; rax = # registered primary opcodes
                (movdqu 16B x4/iter; pcmpeqd vs 0; psubq accumulate; hsum -> rax)
9b1a0e  test %rax,%rax ; je 9b1a2f                  ; ZERO opcodes -> *lib_index = 0, SUCCESS
9b1a13  mov  $0x1,%eax                              ; default rc = 1 (UNKNOWN_CORE)
9b1a18  cmp  $0x25,%esi ; ja 9b1a38                 ; coretype > 37 -> ret 1
9b1a1f  movabs $0x2020202040,%rsi                   ; <<< DIFFERENT bitmask {6,13,21,29,37}
9b1a29  bt   %rcx,%rsi ; jae 9b1a38                 ; bit clear -> ret 1
9b1a2f  movq $0x0,(%rdx)                            ; *lib_index = 0   (ALWAYS 0)
9b1a36  xor  %eax,%eax ; pop %rcx ; ret             ; SUCCESS
```

The stripped form (`@0x309e00`) uses a `cmp $0x1d` (= 29) bound and a 32-bit mask
`0x20202040` (bits `{6,13,21,29}`, no Maverick).

**Key divergences from `ll_get_libraries_from_opcodes`:**

1. **Different bitmask.** `0x2020202040` sets bit `6` (SUNDA) — no special-case arm
   — because SUNDA and the PERF gens both return index `0`; there is no value
   divergence to split out.
2. **No `getenv` / no CPTC override.** This resolver writes `lib_index = 0` for
   *any* valid `Q7_POOL` coretype, **unconditionally**. It can **never** return
   index `3`. The CPTC / lib-3 escape exists only on the list-based path (and on
   ll-create's own selector).
3. **It does inspect opset content — but only as a count.** The SSE2 popcount of
   `opcode_slot[256]` distinguishes empty (→ index `0`, SUCCESS) from non-empty.
   Like the list-based resolver, it never maps an individual opcode to a library.

> **GOTCHA — the two documented resolvers diverge on CPTC.** A model using the
> CPTC ops gets lib `3` only via `ll_get_libraries_from_opcodes` **with the env
> set**. The opset path *always* picks the base lib `0`. Both are coretype-keyed
> and opcode-content-blind beyond an empty/non-empty gate.

---

## 6. Where the opcode → library mapping actually lives (two stages)

A natural expectation is that this resolver maps each opcode/specialization to
the library that implements it. It does **not** perform that map. The real binding
is a two-stage, mostly-implicit scheme:

### Stage 1 — host (this function): coretype + CPTC-env → one union library `{0|3}`

The chosen library is a **union image**: a single EXTISA blob carries many opcode
handlers. So "opcode X needs library Y" collapses to one binary policy bit:

- **lib `0` = `EXTISA_0`** — the base POOL ext-isa, the largest blob. Selecting it
  stages the whole base compute opcode set at once. The
  [opcode-catalog ledger](../firmware/kernels/opcode-catalog-ledger.md) (140 real
  HW opcodes, cross-gen union) and the device
  [`kernel_info_table`](../firmware/pool/kernel-info-table.md) (keyed
  `(opcode<<24)|(spec<<16)`) cover this set image-internally; there is no per-opcode
  *host* selection because one library covers the compute opcodes.
- **lib `3` = `EXTISA_3`** — the CPTC / extended-instruction superset, adding the
  CPTC-decode path (opcode `0xE4 CONV_LUT_LOAD` and the `0xF0`/spec-7 cptc family;
  see the ledger rows for `0xE4`/`0xF0`). The CPTC env upgrades the chosen lib from
  base `0` to CPTC-capable `3`. *[HIGH/OBSERVED for the host mechanism; the
  index→blob *content* membership is INFERRED from the env name `…_CPTC_DECODE` +
  the image catalog, MED.]*

> **The `0xF0` specialization is not handled in this function.** Despite the
> `subopcodes[]` argument (the `0xF0` specs the opset's slot-`0xF0` bitmap tracks),
> the resolver never reads it (§2a). The spec dimension influences lib selection
> only *indirectly*: a model that uses the CPTC spec is expected to set the env so
> lib `3` is staged. The function itself does no spec→lib logic.

### Stage 2 — device (after staging): the lib's `kernel_info_table` resolves the handler

Once the chosen library is staged to Q7 IRAM/DRAM, the per-opcode dispatch is the
device [`kernel_info_table`](../firmware/pool/kernel-info-table.md) *inside that
library*: POOL keys `(opcode<<24)|(spec<<16)` → handler VA by linear scan. That is
the true opcode→handler map — but it lives in the firmware image on the Q7, not in
this host resolver. **The host picks WHICH library; the device table inside that
library picks WHICH handler.** *[HIGH host leg OBSERVED; device dispatch carried
from the `kernel_info_table` / opcode-ledger pages.]*

---

## 7. The reconstructable resolver (reimplementation-grade C)

```c
/* nrtucode_ll_get_libraries_from_opcodes — SHIPPED behaviour, exact.
 * internal @0x9b1880 ; stripped @0x309d30 (jump-table codegen, same logic).      */
nrtucode_result_t
nrtucode_ll_get_libraries_from_opcodes(nrtucode_coretype_t core_type,
        uint8_t opcodes[]    /* IGNORED */, size_t num_opcodes,
        uint8_t subopcodes[] /* IGNORED */, size_t num_subopcodes /* IGNORED */,
        size_t *libs, size_t *num_libs)
{
    if (!libs)     { fprintf(stderr, NULL_FMT, __func__, "libs");     abort(); } /* 9b1884 */
    if (!num_libs) { fprintf(stderr, NULL_FMT, __func__, "num_libs"); abort(); } /* 9b188e */

    *num_libs = 0;                                          /* 9b1897 pre-init   */
    if (num_opcodes == 0)                                   /* 9b189e empty model*/
        return NRTUCODE_SUCCESS;                            /*   num_libs stays 0 (violates the [1,..] doc) */

    if (core_type > 37)                 return NRTUCODE_ERR_UNKNOWN_CORE; /* 9b18a8 */

    /* per-coretype LIB-SET key: only the Q7_POOL core of each generation.
     * bitmask 0x2020202000 -> {13,21,29,37}; SUNDA(6) handled by the cmp arm.    */
    int is_perf  = (core_type==13 || core_type==21 || core_type==29 || core_type==37);
    int is_sunda = (core_type==6);
    if (!is_perf && !is_sunda)          return NRTUCODE_ERR_UNKNOWN_CORE; /* 9b18ee */

    size_t lib;
    if (is_perf)        /* CPTC env upgrades base 0 -> 3 (lea (rcx,rcx,2)=3*cl) */
        lib = getenv("NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE") ? 3 : 0; /* 9b18c2 */
    else                /* SUNDA: only EXTISA_0 exists in its 1-lib table */
        lib = 0;                                            /* 9b18f4 */

    libs[0]   = lib;                    /* 9b18da: a SINGLE library UID, NOT a bitmap */
    *num_libs = 1;                      /* 9b18dd */
    return NRTUCODE_SUCCESS;            /* 9b18e4 */
}
/* SHIPPED libnrtucode.so excludes coretype 37 (Maverick): is_perf drops 37. */
```

Algorithm shape (the "opset used-opcodes + coretype → required-library" map, in
reality): **(1)** validate the two out-pointers (abort on NULL); **(2)** if zero
opcodes → empty result (`num_libs = 0`); **(3)** reject non-`Q7_POOL` coretypes;
**(4)** choose **one** library index — SUNDA → `0`; PERF-gen → `CPTC-env ? 3 : 0`;
**(5)** emit it as a one-element list. No dependency closure (a single lib is
self-contained), no opcode iteration, no specialization scan, no multi-library
union.

---

## 8. Per-generation differences

- **The coretype key differs by gen** (`6 / 13 / 21 / 29 / 37` — the `+8`
  `Q7_POOL` stride).
- **The lib-set *size* differs**: SUNDA has 1 lib (index `0` only); CAYMAN /
  MARIANA / MPLUS / MAVERICK have 4 (indices `0..3`). Lib `3` (CPTC) is reachable
  *only* on the PERF gens — SUNDA can never take the getenv branch (it would index
  a non-existent slot). This is precisely why SUNDA is special-cased.
  *[HIGH/OBSERVED + per-gen counts from the image catalog]*
- **The decision *logic* is otherwise identical across gens**: every PERF gen uses
  the same `getenv → {0|3}` rule; the function never branches on gen beyond the
  coretype gate and the SUNDA/PERF split. There is **no per-gen opcode table** —
  because there is no opcode table at all.
- **MARIANA == MARIANA_PLUS at the image level**: byte-identical EXTISA blobs. So
  coretypes `21` and `29` select the *same physical lib contents* despite being
  distinct coretype keys. *[carried from the cross-gen kernel-info matrix]*

---

## 9. Boundaries — what this resolver is *not*

| Function | Role | Relation |
| --- | --- | --- |
| `nrtucode_ll_get_libraries_from_opcodes` (here) | **picks** the lib index `{0\|3}` by coretype + CPTC-env | **upstream** of ll-create |
| [`nrtucode_ll_create`](nrtucode-ll-create.md) | consumes the index as its `library_index` arg, stages the prelinked image | the **consumer** of this index |
| [`nrtucode_ll_get_load_sequence` / `…_unload_sequence`](nrtucode-ll-load-unload.md) | emit the device load / unload micro-program | **downstream** — this resolver emits no device sequence |
| [`nrtucode_get_ext_isa`](version-extisa-getters.md) | resolves the EXTISA blob `{so,len,json,…}` for a **given** lib index (public wrapper hardcodes `0`) | the **index consumer**; this is the **index picker** |
| [`nrtucode_get_memory_image`](image-hwdecode-resolvers.md) | resolves the base IRAM/DRAM/SRAM/EXTRAM **region** images (separate region jump table, zero refs to the ext-isa lib tables) | a **separate lane** — base-region, not EXTISA |
| [opset query API](nrtucode-opset.md) | the host presence mirror (`opcode_slot[256]` + per-`0xF0` spec bitmap) | this resolver takes **raw arrays**, ignores their content |

The pipeline ordering is: **opcodes-in (ignored) / coretype-in → lib-index-out →
`ll_create` → load-sequence**. This resolver is the front of that pipeline; it
picks the index and nothing else. *[HIGH/OBSERVED symbols; ordering INFERRED from
the exported ABI + the ll-create selector, which uses the same CPTC env.]*

---

## 10. Adversarial self-verification

The five strongest claims, each anchored in the binary:

1. **Resolver symbol & address** — `nrtucode_ll_get_libraries_from_opcodes` @
   `internal 0x9b1880` / `stripped 0x309d30`. → `nm` lists both; the disassembled
   prologue (`push r14/rbx/rax`) and abort tails referencing the fn-name token at
   `.rodata 0x4cee` confirm identity. **CONFIRMED.**
2. **The "bitmap" layout** — output is a *scalar* index in `libs[0]` with
   `*num_libs = 1`, not a bitwise OR. → `lea (%rcx,%rcx,2)` computes `3·cl ∈ {0,3}`;
   `mov %rax,(%rbx)` writes one element; `movq $0x1,(%r14)` sets count = 1. No loop,
   no OR, no second store. **CONFIRMED.**
3. **A specific opcode→lib mapping** — CPTC ops → lib `3` via the env, everything
   else → lib `0`. → The only data-dependent input is `getenv("NRT_UCODE_…
   _CPTC_DECODE")` (string @ `0x4d15`, OBSERVED); `setne cl` then `3·cl`. The opcode
   array is never read (§2a). The *content* membership of lib `3`
   (`0xE4` / `0xF0`-spec-7) is INFERRED from the env name + the committed
   ledger/catalog, tagged MED. **CONFIRMED (mechanism HIGH, membership MED).**
4. **The coretype gate** — accepted set `{6,13,21,29(,37)}` = the `Q7_POOL` core of
   each gen. → internal `bt`-mask `0x2020202000` (Python-decoded bits `{13,21,29,37}`)
   + the `cmp $0x6` SUNDA arm; stripped jump table (decoded: ct6→SUNDA,
   ct13/21/29→getenv, else→err, bound 29). **CONFIRMED.**
5. **lib-index ↔ image correspondence** — index `0` = `EXTISA_0`, index `3` =
   `EXTISA_3`; SUNDA has only index `0`. → cross-checked against the
   [extisa-inventory](../images/extisa-inventory.md): CAYMAN/MARIANA/MPLUS/MAVERICK
   carry `EXTISA_0..3` (4 libs), SUNDA carries `EXTISA_0` only (1 lib), and
   `get_num_ext_isa_libs` returns `1` for coretype 6 / `4` otherwise — matching the
   SUNDA-can't-reach-index-3 gate exactly. **CONFIRMED (gate OBSERVED; image identity
   carried from the catalog).**

The one deliberately conservative tag is the *content* of lib `3` (MED/INFERRED):
the host resolver proves the *index* `3` and its env trigger byte-exact, but the
EXTISA_3 blob's opcode roster is established by the image catalog and firmware
pages, not disassembled here.

---

## 11. Evidence index

| Anchor | Address / offset |
| --- | --- |
| Resolver symbol | internal `0x9b1880` / stripped `0x309d30` |
| Prologue (no frame) | `push r14/rbx/rax` @ `0x9b1880` |
| `libs` NULL guard / arg-name | `0x9b1884` / `"libs"` @ `0x51cc` |
| `num_libs` NULL guard / arg-name | `0x9b188e` / `"num_libs"` @ `0x551f` |
| fn-name / fmt strings | `0x4cee` / `0x5469` |
| Empty gate | `0x9b189e` `test %rdx` → SUCCESS, `num_libs = 0` |
| Coretype upper bound | `0x9b18a8` `cmp $0x25` (= 37) |
| PERF bitmask | `0x9b18b2` `movabs $0x2020202000` → bits `{13,21,29,37}` |
| SUNDA arm | `0x9b18ee` `cmp $0x6` |
| CPTC env / getenv / `3·cl` | `0x9b18c2` (`0x4d15`) / `0x9b18c9` / `0x9b18d6` |
| Output stores | `0x9b18da` `libs[0]` / `0x9b18dd` `*num_libs = 1` |
| opcodes/subopcodes never read (**decisive**) | `rg %rsi/%r8/(%rcx)` = 2 abort leas only |
| Stripped jump table | `.rodata 0x39c4`, 24 rel32, idx = coretype−6 |
| Stripped bound (no Maverick) | `add $0xfffffffa` / `cmp $0x17` |
| `opset_get_library_index` | internal `0x9b1950` / stripped `0x309e00` |
| opset mask / always-0 | `movabs $0x2020202040` / `movq $0x0,(%rdx)` |
| coretype enum ordinals | `nrtucode.h:13-58` (`Q7_POOL` = 6/13/21/29/37) |
| Header `[1, num_opcodes]` doc (stale) | `nrtucode.h:350-351` |
| Per-gen lib counts (`HIGH carried`) | [extisa-inventory](../images/extisa-inventory.md) (1 / 4) |
| BuildIDs | stripped `abf4e088…` / internal `9cbf78c6…` |

---

## 12. Summary

`nrtucode_ll_get_libraries_from_opcodes` (`@0x9b1880` internal / `0x309d30`
shipped) is an **opcode-content-blind** resolver. It ignores `opcodes[]`,
`subopcodes[]`, and `num_subopcodes` (proven: those registers are never
dereferenced), and keys only on `(core_type, num_opcodes != 0,
NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE)`. The accepted coretypes are the
`Q7_POOL` core of each generation — `{SUNDA=6, CAYMAN=13, MARIANA=21,
MARIANA_PLUS=29, (MAVERICK=37 internal-only)}` — everything else returns
`NRTUCODE_ERR_UNKNOWN_CORE`. The output is **one scalar library index** (not a
bitmap, not a multi-element list): SUNDA → `0`; PERF gens → `CPTC env ? 3 : 0`;
`*num_libs = 1` (or `0` for an empty model, which violates the header's stale
`[1, num_opcodes]` promise). Index `0` = `EXTISA_0` (the base POOL ext-isa union);
index `3` = `EXTISA_3` (the CPTC / cptc-decode superset). The per-opcode → handler
mapping is **not** a host lookup — it is a two-stage scheme: the
host picks one union library by coretype + CPTC-env, and the device
[`kernel_info_table`](../firmware/pool/kernel-info-table.md) inside that staged
library does the `(opcode<<24)|(spec<<16)` dispatch. The parallel
`nrtucode_opset_get_library_index` (`@0x9b1950`) is simpler still: SSE2-popcount
the opset's registered-opcode count, then return index `0` unconditionally for any
valid `Q7_POOL` coretype — never `3`, no CPTC env. This resolver is the front of the
[`ll_create`](nrtucode-ll-create.md) → [load-sequence](nrtucode-ll-load-unload.md)
pipeline, distinct from the base-region
[`get_memory_image`](image-hwdecode-resolvers.md) lane.
