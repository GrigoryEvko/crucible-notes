# Opset Create / Add-Instruction / Query

The **opset** is the host-side presence record of *which* extended-ISA operations a
loaded model uses. It is a pure-host object — `malloc`/`calloc`/`free`, no device
memory — that the NEFF custom-op loader builds before any IRAM load and then queries
to drive [library resolution](opcode-to-lib-resolver.md). Its entire public surface
is **nine exported C functions** over a single **`0x830`-byte struct** whose central
field is a **256-slot opcode array**. There are no C++ vtables anywhere in this
binary, so every table is a plain C array and every claim below is grounded in raw
instruction bytes.

This page decodes the opset *trilogy* byte-exact:

- **`nrtucode_opset_create`** + the `nrtucode_opset_t` struct (`0x830`, 256 slots) — §2, §3;
- **`nrtucode_opset_add_instruction`** + the `0xF0` *ExtendedInst* specialization
  escape (the presence-populate path) — §4;
- the **private query API** — `get_num_opcodes` / `get_num_specializations` /
  `has_opcode` / `has_specialization` — §5.

`destroy`, `set_friendly_name`, and `get_library_index` are decoded as supporting
context (§6). The struct has **no DWARF**; its layout is recovered from the
`malloc`/`memset`/`calloc` *sizes* and every field-access *displacement* in the nine
opset functions, cross-checked against the shipped private header.

> All addresses, bytes, struct displacements, and strings below were read directly
> from the shipped host library with stock `objdump` / `nm` / `readelf` and a small
> Python `.rodata` reader. Recovered symbol names, format strings, and the shipped
> header are binary-derived artifacts and are cited as such. Addresses are the
> **internal twin** (`libnrtucode_internal.so`) unless tagged *stripped*.

---

## 0. Binaries, grounding, and the no-vtable note

| Item | Value | Source |
| --- | --- | --- |
| Shipped runtime (authoritative) | `libnrtucode.so` (stripped) | `extracted/…/c10/lib/libnrtucode.so` |
| Symbol twin (names) | `libnrtucode_internal.so` (**10,276,288 B**, not stripped) | same dir |
| Static archive | `libnrtucode.a` → `nrtucode_opset.c.o` | same dir |
| BuildID (internal) | `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd` | `file` *[HIGH/OBSERVED]* |
| `.rodata` | VMA `0x46b0` = file off `0x46b0` → **Δ = 0** | `readelf -SW` *[HIGH/OBSERVED]* |
| `.text` | VMA `0x9b01a0`, file off `0x9af1a0` → **Δ = 0x1000** | `readelf -SW` *[HIGH/OBSERVED]* |
| `.data.rel.ro` | VMA `0x9b8cf0`, file off `0x9b6cf0` → **Δ = 0x2000** | `readelf -SW` *[HIGH/OBSERVED]* |
| `.data` | VMA `0x9ba4a8`, file off `0x9b74a8` → **Δ = 0x3000** | `readelf -SW` *[HIGH/OBSERVED]* |
| `_ZTV` vtable count | **0** (`nm … \| rg -c '_ZTV'` → 0) | *[HIGH/OBSERVED]* |
| `__FILE__` token | `"nrtucode_opset.c"` @ `.rodata` file-off `0x9c076a` | Python `.rodata` reader *[HIGH/OBSERVED]* |

> **GOTCHA — no `_ZTV`, no `+0x10` rule.** This library ships **zero** C++ vtables.
> Every function-pointer / slot table here is a plain C array: slot *N* is
> `table_symbol + 8*N`, addressed from the table base, **not** from a vptr `+ 0x10`.
> The opset's `opcode_slot[]` is exactly such a flat C pointer array. All counts on
> this page are grounded via `nm … | rg -c`, never assumed.

### The nine exported opset functions

`nm` resolves the full opset API at fixed addresses; the stripped twin mirrors each
in `.dynsym`. **No internal call site references any of the nine** in either library
(`objdump -d | rg -c 'call.*nrtucode_opset_…'` = 0 for all nine) — the opset is a
**pure exported API** consumed by the external NRT runtime / NEFF loader, never by
`libnrtucode` itself. *[HIGH/OBSERVED]*

| Internal `@` | Stripped `@` | ELF size | Symbol |
| --- | --- | --- | --- |
| `0x9b24c0` | `0x30a950` | 241 B | `nrtucode_opset_create` |
| `0x9b25c0` | `0x30aa50` | 153 B | `nrtucode_opset_destroy` |
| `0x9b2660` | `0x30aaf0` | 294 B | `nrtucode_opset_add_instruction` |
| `0x9b1950` | `0x309e00` | — | `nrtucode_opset_get_library_index` |
| `0x9b2790` | `0x30ac20` | **178 B** | `nrtucode_opset_private_get_num_opcodes` |
| `0x9b2850` | `0x30ace0` | **113 B** | `nrtucode_opset_private_get_num_specializations` |
| `0x9b28d0` | `0x30ad60` | **22 B** | `nrtucode_opset_private_has_opcode` |
| `0x9b28f0` | `0x30ad80` | **30 B** | `nrtucode_opset_private_has_specialization` |
| `0x9b2910` | `0x30ada0` | — | `nrtucode_opset_set_friendly_name` |

ELF `FUNC` sizes (`readelf -sW`) match the disassembled extents exactly. *[HIGH/OBSERVED]*

---

## 1. The model in one paragraph

The opset is a **256-entry pointer array keyed by the raw opcode byte**. `slot[op]`
NULL means "opcode `op` not present". `slot[op]` non-NULL means "present", and the
pointer is a lazily-`calloc`'d **256-byte specialization presence bitmap**
(`bitmap[spec] ∈ {0,1}`). For *every opcode except `0xF0`* the bitmap stays all-zero
and the non-NULL pointer **alone** signals presence. Opcode **`0xF0`** is the device's
[*ExtendedInst* escape](../firmware/pool/pool-ext-0xf0.md): one primary opcode that
multiplexes many real ops, sub-selected by a *specialization* byte. For `0xF0`, and
only `0xF0`, `add_instruction` populates `bitmap[spec] = 1`, so `slot[0xF0]` is a true
sub-op presence set. The opset thus mirrors *which* `(opcode, spec)` pairs a library
provides; the *handlers* live device-side (this is a presence mirror, not a dispatch
table). See [object-model-graph](object-model-graph.md) for where the opset sits in
the `context → core → memhandle → ll → opset` graph.

> **Two 256s — do not conflate.** `opcode_slot[]` is **256 entries** of stride **8**
> (`0x800 / 8`). Each non-NULL slot points at a **256-byte bitmap** of stride **1**
> (`calloc(1, 0x100)`). The slot count is 256; the per-slot bitmap is 256 bytes.

---

## 2. `nrtucode_opset_t` — the `0x830`-byte struct

A single `malloc(0x830)` builds the whole object; there is **no** secondary
allocation in `create` (the per-opcode bitmaps are allocated lazily by
`add_instruction`). The `0x800` `memset` length over 256 slots fixes the slot record
size: **`0x800 / 256 = 8` bytes per slot** — a raw C pointer. *[HIGH/OBSERVED]*

| Off | Size | Type | Field | Purpose | Conf |
| --- | --- | --- | --- | --- | --- |
| `0x000` | 8 | `nrtucode_context_t*` | `context` | Owning context (create arg1). Read by every method only to reach the logger. `mov %rbx,(%rax)` @`0x9b24f1`. | HIGH OBS |
| `0x008` | `0x800` | `uint8_t* [256]` | `opcode_slot[256]` | **The 256-slot array.** One 8-byte pointer per opcode `0..255`, addressed `*(opset + 8 + op*8)`. NULL = absent; non-NULL = a `calloc(1,0x100)` 256-byte spec bitmap. `memset(opset+8,0,0x800)` @`0x9b2504` zeroes it in `create`. | HIGH OBS |
| `0x808` | `0x28` | `char[0x28]` | `friendly_name` | 40-byte inline name buffer. `create` `snprintf`s the default `"nrtucode_opset_t@%p"` (≤ `0x21`=33 B); `set_friendly_name` overwrites (`strncpy 0x20` + forced NUL @`+0x828`). The `"%s"` in every log line. | HIGH OBS |
| | | | | **Total = `0x830` = 8 + 256·8 + 0x28.** | |

The only struct-tail accesses anywhere in the nine functions are `+0x808`
(`friendly_name` base) and `+0x828` (the forced NUL in `set_friendly_name`) — there
are **no hidden fields**, **no count field**, **no handler pointers**. *[HIGH/OBSERVED]*

The **slot record is 8 bytes: a single `uint8_t*`.** It is *not* a `{handler, count,
flags}` struct. The presence semantics live entirely in (a) whether the pointer is
NULL and (b) the bytes of the bitmap it points at. *[HIGH/OBSERVED]*

```c
typedef struct nrtucode_opset_t {        /* sizeof = 0x830 (2096 B) */
    nrtucode_context_t *context;          /* +0x000  owning context (create arg1)        */
    uint8_t            *opcode_slot[256];  /* +0x008  0x800 B; slot[op]==NULL => absent,   */
                                           /*         else -> calloc(1,256) spec bitmap     */
    char                friendly_name[0x28]; /* +0x808  default "nrtucode_opset_t@%p"       */
} nrtucode_opset_t;                        /* total 0x830 */

/* per-opcode bitmap (the calloc'd block): uint8_t spec_present[256];
   spec_present[s] = 1 iff (op,s) registered. Only opcode 0xF0 ever sets s>0. */
```

> **CORRECTION (vs `object-model-graph.md`).** The committed object-model-graph page
> types `friendly_name` as **`char[0x21]`** with a separate **`0x07` tail pad** at
> `+0x829`. The byte evidence makes the field **`char[0x28]` (40 B)** spanning
> `+0x808 .. +0x830` with **no separate pad**: `set_friendly_name` writes the forced
> terminating NUL at **`+0x828`** (`movb $0x0,0x828(%r14)` @`0x9b294e`), and the
> buffer therefore runs to the end of the struct. The `0x21` figure is the
> **default-name `snprintf` cap** (`mov $0x21,%esi` @`0x9b251a`), not the field size.
> The whole `0x808 .. 0x830` range is the name buffer; `0x830 = 8 + 0x800 + 0x28`
> exactly. *[HIGH/OBSERVED]*

> **NOTE — `malloc`, not `calloc`, for the struct.** `create` `memset`s only the
> `0x800` slot array; the `0x28` name tail is left undefined by `malloc` and then
> `snprintf`'d before return, so it is defined on every return path. *[OBSERVED]*

---

## 3. `nrtucode_opset_create` — allocate, zero slots, name, log

```c
int nrtucode_opset_create(nrtucode_context_t *context,  /* rdi, arg "context" @0x479f */
                          nrtucode_opset_t  **op_set);   /* rsi, arg "op_set"  @0x5459 */
/* return: 0 success ; 5 = OOM (NRTUCODE error 5). */
```

Byte-exact body (internal `0x9b24c0`, 241 B; stripped `0x30a950` byte-identical
constants):

```c
int nrtucode_opset_create(nrtucode_context_t *context, nrtucode_opset_t **op_set)
{
    if (!context)                                     /* test rdi @0x9b24c7      */
        FATAL("…`%s` is null", "nrtucode_opset_create", "context"); /* fprintf+abort @0x9b255b */
    if (!op_set)                                      /* test rsi @0x9b24d3      */
        FATAL("…`%s` is null", "nrtucode_opset_create", "op_set");  /* @0x9b2586 */

    nrtucode_opset_t *p = malloc(0x830);              /* mov $0x830,%edi @0x9b24df */
    *op_set = p;                                      /* mov %rax,(%r14) @0x9b24e9 */
    if (!p) return 5;                                 /* je …; ebp=5 @0x9b2549     */

    p->context = context;                             /* mov %rbx,(%rax) @0x9b24f1 */
    memset((char*)p + 0x08, 0, 0x800);                /* zero opcode_slot[256] @0x9b24f4..0x9b2504 */
    snprintf((char*)p + 0x808, 0x21,                  /* @0x9b250c..0x9b2524 */
             "nrtucode_opset_t@%p", p);               /* fmt @0x5548          */
    nrtucode_context_log(context, /*sev*/4,           /* @0x9b252e..0x9b2542  */
                         "Initialized %s", &p->friendly_name); /* fmt @0x52fa */
    return 0;                                         /* ebp=0 */
}
```

- **NULL args are FATAL** (`fprintf(stderr, "nrtucode: invalid API usage in \`%s\`:
  \`%s\` is null\n", …)` @`0x5469`, then `abort`) — the same idiom as the context
  lifecycle functions and `add`/`destroy`. *[HIGH/OBSERVED]*
- **Severity `4`** is the trace/debug tier; every opset bookkeeping line
  (Initialized / Destroyed / added / renamed) uses it. Only the byte `4` is OBSERVED;
  the tier *name* is inferred from message content. *[OBSERVED byte / INFERRED name]*
- **No device touch.** `create` is host-RAM only — `malloc` + `memset` + `snprintf`
  + a log call. It never touches a core, a memhandle, or device memory. *[HIGH/OBSERVED]*

---

## 4. `nrtucode_opset_add_instruction` — the populate path + the `0xF0` escape

```c
int nrtucode_opset_add_instruction(nrtucode_opset_t *op_set,          /* rdi, "op_set"           */
                                   const void       *instruction_data); /* rsi, "instruction_data" */
/* return: 0 success (incl. idempotent re-add) ; 5 = OOM (calloc fail). */
```

The instruction descriptor is read at **exactly two byte offsets and no others**
(every `rsi`-relative access in the body is `(rsi)` or `0xc(rsi)`):

| Offset | Field | Read when |
| --- | --- | --- |
| `instruction_data[0x00]` | `opcode` (uint8) — indexes `opcode_slot[256]` | always |
| `instruction_data[0x0c]` | `spec` (uint8) — the specialization sub-opcode | **only** when `opcode == 0xF0` |

The public `nrtucode.h` documents this arg as a *"64-byte instruction data"* pointer,
so both byte `0x00` and byte `0x0c` are safely in range. Everything else in the NEFF
instruction word is **opaque** to the opset. *[HIGH/OBSERVED]*

Byte-exact body (internal `0x9b2660`, 294 B; instruction-identical across the stripped
`.so`, internal `.so`, and the `nrtucode_opset.c.o` archive object):

```c
int nrtucode_opset_add_instruction(nrtucode_opset_t *op_set, const void *instr)
{
    if (!op_set) FATAL(…, "op_set");                  /* test rdi/je @0x9b2730 -> fprintf+abort */
    if (!instr)  FATAL(…, "instruction_data");        /* test rsi/je @0x9b275b -> fprintf+abort */

    uint8_t opcode = ((const uint8_t*)instr)[0];      /* movzbl (rsi),r14d @0x9b267a */

    if (op_set->opcode_slot[opcode] == NULL) {        /* cmpq $0,0x8(rdi,r14,8) @0x9b267e */
        /* --- FIRST SIGHTING: lazily allocate the 256-byte spec bitmap --- */
        uint8_t *bitmap = calloc(1, 0x100);           /* edi=1,esi=0x100 @0x9b269a; call @0x9b26a4 */
        op_set->opcode_slot[opcode] = bitmap;         /* STORE FIRST @0x9b26a9 */
        if (!bitmap) return 5;                         /* test/je @0x9b26ae -> mov $5 @0x9b2725 */
        log(op_set->context, 4,                        /* @0x9b26b3..0x9b26ce */
            "%s added opcode 0x%02x",                  /* fmt @0x4f97 */
            op_set->friendly_name, opcode);            /* r8d = opcode */
        if (opcode != 0xF0) return 0;                  /* cmp 0xF0 @0x9b26d8; jne @0x9b26df -> ret 0 */
    } else {
        if (opcode != 0xF0) return 0;                  /* cmp 0xF0 @0x9b2688; not-je -> ret 0 (noop) */
    }

    /* --- reached iff opcode == 0xF0 (new-fallthrough OR existing-je) @0x9b26e1 --- */
    uint8_t  spec   = ((const uint8_t*)instr)[0x0c];  /* movzbl 0xc(rsi),r9d @0x9b26e1 */
    uint8_t *bitmap = op_set->opcode_slot[0xF0];      /* mov 0x8(rbx,r14,8),rcx @0x9b26e6 */
    uint8_t  already = bitmap[spec];                  /* cmpb $0,(rcx,r9,1) @0x9b26eb */
    bitmap[spec] = 1;                                  /* movb $1,(rcx,r9,1) @0x9b26f0 (ALWAYS) */
    if (!already)                                      /* jne skip-log @0x9b26f5 */
        log(op_set->context, 4,
            "%s added extended opcode 0x%02x with specialization 0x%02x", /* fmt @0x5359 */
            op_set->friendly_name, 0xF0, spec);        /* r8d=0xF0 HARD-CODED @0x9b2710, spec=r9 */
    return 0;
}
```

### 4a. The slot record store — where presence lives

On first sighting of an opcode the function `calloc(1, 0x100)`s a 256-byte
zero-initialized spec bitmap and stores the pointer into `slot[opcode]`
(`mov %rax,0x8(%rbx,%r14,8)` @`0x9b26a9`, scale-8 indexing the pointer array). The
store happens **before** the NULL-check of `calloc`'s result — harmless: on failure
the slot just holds NULL again and the function returns 5 (the opcode stays "absent").
`calloc` (not `malloc`+`memset`) is what zeroes the bitmap, so every spec starts
"not present". *[HIGH/OBSERVED]*

### 4b. The non-`0xF0` path sets *no* bitmap byte

For any `opcode != 0xF0`, the function **never touches the bitmap**. It does not set
`bitmap[0]`, does not set any presence byte. The **non-NULL slot pointer itself is the
sole presence signal**; the calloc'd bitmap stays entirely zero. Both non-`0xF0` exit
edges (`jne @0x9b26df -> 0x9b2691 ret`, and the existing-non-`0xF0` no-jump
`@0x9b268f -> 0x9b2691 ret`) reach the return without any `movb` into the bitmap. The
only `movb $1` in the whole function (`@0x9b26f0`) is inside the `0xF0`-gated block.
*[HIGH/OBSERVED — decisive]*

> **NOTE.** A 256-byte bitmap is therefore *allocated* for every registered opcode but
> *used* only for `0xF0`. The spec dimension is structurally present for all opcodes
> and semantically populated for one — a consequence of the slot pointer doubling as
> the presence marker.

### 4c. The `0xF0` *ExtendedInst* two-level escape

The spec block `@0x9b26e1` has **two entry edges into one block**:

1. **existing-`0xF0`** — `cmp $0xf0,%r14d; je 0x9b26e1` @`0x9b268f` (slot already
   non-NULL; `rsi` never clobbered on this path, so `0xc(rsi)` is valid directly);
2. **new-`0xF0` fall-through** — `cmp $0xf0,%r14d; jne 0x9b2691` @`0x9b26df` *not
   taken* (slot just calloc'd; `rsi` was saved/restored across the `calloc`/log in
   `r15`).

A single primary-opcode test (`0xF0`?) gates an entire second-level (spec)
registration sub-path, shared between the just-allocated and pre-existing cases. This
is the "`0xF0` ExtendedInst specialization handling" — fully contained in this one
function. Why two levels: `0xF0` is the device's *ExtendedInst* opcode, one escape
that multiplexes many real ops sub-selected by the spec byte; the device realises the
second level as the **five `0xF0` rows** of the POOL `kernel_info_table` (specs
`{0,1,2,3,4}`, +`7` for the CPTC `lib3`). See
[pool-ext-0xf0](../firmware/pool/pool-ext-0xf0.md) and the
[opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) (`0xF0` =
`EXTENDED_INST`, POOL, *escape*). The host bitmap is the presence mirror of exactly
that second level — which is why **only** `0xF0` ever reads `instr[0x0c]`. *[HIGH/OBSERVED
host; device facts cited]*

> **QUIRK — `instr[0x0c]` ≠ the device key offset.** The host reads spec from the NEFF
> descriptor at `+0x0c`; the device `kernel_info_table` stores spec at *its* record
> `+0x02` (8-byte rows). Same logical "spec" value, unrelated byte positions —
> different structures. Do not conflate. *[HIGH/OBSERVED]*

### 4d. Validation, idempotency, return codes

| Check | Behavior | Conf |
| --- | --- | --- |
| `op_set == NULL` | abort (FATAL) | HIGH OBS |
| `instruction_data == NULL` | abort (FATAL) | HIGH OBS |
| opcode range | **none** — `movzbl` uint8 indexes a 256-wide array, can't overflow | HIGH OBS |
| spec range | **none** — uint8 indexes a 256-byte bitmap | HIGH OBS |
| `{0,1,2,3,4,7}` spec set | **not enforced** host-side | HIGH OBS |
| duplicate opcode | idempotent no-op, rc 0 (no calloc, no log) | HIGH OBS |
| duplicate `(0xF0,spec)` | idempotent re-set (rewrites 1 over 1), rc 0, `jne` skips the log | HIGH OBS |
| `calloc` failure | rc **5** — the only error return | HIGH OBS |

The opset is a **trusting presence recorder**: it validates the two pointers, then
records whatever opcode/spec bytes the caller hands it. Any opcode/spec *legality*
checking lives upstream (the NEFF parser / the device dispatch tables, which simply
"Bad Opcode" a key that was never registered). *[opcode/spec-trust HIGH/OBSERVED;
upstream-validates INFERRED]*

---

## 5. The private query API — the readers of the presence model

Four `_private_` introspectors read the struct §2 / populate §4 model. They are
**pure const reads**: the static-archive object has **zero relocations** in the entire
query byte-range — no calls, no GOT loads, no stores. All four **gracefully return 0
on `op_set == NULL`** (no abort, no log) — the *defensive* tier, opposite to
`create`/`add`/`destroy`. Range safety is **structural**: uint8 args index 256-wide
structures, so no bounds check exists or is needed. The shipped private header
(`nrtucode_private.h`) pins every signature, return type, the `const` qualifier, and
the documented semantics — and the binary agrees on every point. *[HIGH/OBSERVED]*

> **Header ground truth** (`nrtucode_private.h`, verbatim): `size_t get_num_opcodes`,
> `size_t get_num_specializations(…, uint8_t opcode)` (*"or 0 if opcode doesn't
> exist"*), `bool has_opcode(…, uint8_t opcode)`, `bool has_specialization(…, uint8_t
> opcode, uint8_t specialization)`, all `const nrtucode_opset_t *`. Banner: *"PRIVATE
> HEADER … Direct use will trigger SEV2 incident"* — these are NRT-runtime
> introspectors, not third-party API.

### 5a. `has_opcode` (`0x9b28d0`, 22 B) — presence predicate

```c
bool has_opcode(const nrtucode_opset_t *op_set /*rdi*/, uint8_t opcode /*esi*/);
  9b28d0  test rdi,rdi ; je 9b28e3          ; op_set==NULL -> 0
  9b28d5  movzx eax,sil                     ; eax = opcode (uint8)
  9b28d9  cmpq $0x0, [rdi+rax*8+8]          ; slot = opcode_slot[opcode]; ==NULL?
  9b28df  setne al ; ret                    ; return (slot != NULL)
  9b28e3  xor eax,eax ; ret                 ; NULL op_set -> false
```
`return op_set ? (opcode_slot[opcode] != NULL) : false;` — works **uniformly** for
every opcode (no `0xF0` special-case). *[HIGH/OBSERVED]*

### 5b. `has_specialization` (`0x9b28f0`, 30 B) — the bitmap byte

```c
bool has_specialization(const nrtucode_opset_t *op_set /*rdi*/,
                        uint8_t opcode /*esi*/, uint8_t specialization /*edx*/);
  9b28f0  test rdi,rdi ; je 9b290b          ; op_set==NULL -> 0
  9b28f5  movzx eax,sil                     ; opcode
  9b28f9  mov rax, [rdi+rax*8+8]            ; slot = opcode_slot[opcode]
  9b28fe  test rax,rax ; je 9b290b          ; slot==NULL (opcode absent) -> 0
  9b2903  movzx ecx,dl                      ; spec (uint8)
  9b2906  movzx eax, byte [rax+rcx*1]       ; return bitmap[spec] (0/1)
  9b290b  xor eax,eax ; ret                 ; NULL op_set OR absent opcode -> 0
```
`slot = op_set ? opcode_slot[opcode] : NULL; return slot ? slot[spec] : 0;`. The
NULL-slot guard prevents a NULL-bitmap deref. *[HIGH/OBSERVED]*

> **The decisive cross-confirmation.** For any **present, non-`0xF0`** opcode,
> `slot != NULL` but the bitmap is all-zero (§4b), so `has_specialization` returns 0
> for *every* spec — **even though `has_opcode == 1`**. This is achieved **without any
> opcode guard**; it falls out of the populate invariant. For `0xF0` it returns the
> recorded `0/1`. *[HIGH/OBSERVED]*

### 5c. `get_num_specializations` (`0x9b2850`, 113 B) — byte-sum popcount

```c
size_t get_num_specializations(const nrtucode_opset_t *op_set /*rdi*/, uint8_t opcode /*esi*/);
  9b2850  test rdi,rdi ; je 9b28be          ; op_set==NULL -> 0
  9b2859  mov rcx, [rdi+rax*8+8]            ; slot = opcode_slot[opcode]
  9b285e  test rcx,rcx ; je 9b28be          ; slot==NULL -> 0  ("or 0 if opcode doesn't exist")
  9b2870..9b28bb  8-WAY UNROLLED byte-sum: sum += bitmap[j] for j=0..255
                  add rdx,8 ; cmp rdx,0x100 ; jne   ; 32 iterations => 256 bytes
  9b28bd  ret (sum in rax) ; 9b28be xor eax,eax ; ret
```
`if (!slot) return 0; size_t s = 0; for (j=0;j<256;++j) s += bitmap[j]; return s;`.
This is a **byte-SUM** of the 256-byte bitmap, **not a stored counter** (the struct has
no count field). Because `add_instruction` only ever stores the value `1`
(`movb $1`), each byte ∈ `{0,1}` and the sum **equals** the popcount of registered
specs. The `cmp rdx,0x100` loop bound independently re-confirms the bitmap is
**256 bytes**. *[byte-sum HIGH/OBSERVED; popcount-equivalence HIGH via §4]*

### 5d. `get_num_opcodes` (`0x9b2790`, 178 B) — SSE2 non-NULL popcount

```c
size_t get_num_opcodes(const nrtucode_opset_t *op_set /*rdi*/);
  9b2790  test rdi,rdi ; je 9b283f          ; op_set==NULL -> 0
  9b2799  pxor xmm0(=0) ; pcmpeqd xmm3(=all-ones) ; pxor xmm2/xmm1 (accumulators)
  9b27b0..9b282a  LOOP (rax=0,8,…,0xF8; 32 iters, 8 slots/iter):
                  movdqu 4x16B of [rdi+rax*8+0x08..0x38]  ; 8 slot pointers
                  per 16B group: pcmpeqd vs 0 ; pshufd 0xb1 ; pand (fold qword is-zero) ;
                                 pxor all-ones (=> is-NONzero) ; psubq accumulate
                  add rax,8 ; cmp rax,0x100 ; jne
  9b282c..9b283e  paddq + pshufd 0xee + paddq ; movq rax,xmm0 ; ret   ; horizontal sum
  9b283f  xor eax,eax ; ret                 ; NULL op_set -> 0
```
`if (!op_set) return 0; size_t n=0; for (i=0;i<256;++i) n += (opcode_slot[i] != NULL);
return n;` — a **live SSE2 popcount of non-NULL slots = number of distinct primary
opcodes registered**, **not** a stored counter. The `cmp rax,0x100` bound re-confirms
the **256-slot array**. *[HIGH/OBSERVED]*

### 5e. Query semantics summary

| Function | `op_set==NULL` | absent opcode | mutates? | Conf |
| --- | --- | --- | --- | --- |
| `has_opcode` | `0` | `0` (cmp) | no | HIGH OBS |
| `has_specialization` | `0` | `0` (NULL-slot guard) | no | HIGH OBS |
| `get_num_specializations` | `0` | `0` (header: *"or 0"*) | no | HIGH OBS |
| `get_num_opcodes` | `0` | n/a (scans 256) | no | HIGH OBS |

```c
/* reimplementation-grade — matches the shipped header exactly */
size_t  nrtucode_opset_private_get_num_opcodes(const nrtucode_opset_t *s) {
    if (!s) return 0; size_t n=0;
    for (int i=0;i<256;++i) n += (s->opcode_slot[i]!=NULL); return n; }       /* SSE2 in binary */

size_t  nrtucode_opset_private_get_num_specializations(const nrtucode_opset_t *s, uint8_t op) {
    if (!s) return 0; const uint8_t *b = s->opcode_slot[op];
    if (!b) return 0; size_t k=0; for (int j=0;j<256;++j) k += b[j]; return k; } /* 8-way unrolled */

bool    nrtucode_opset_private_has_opcode(const nrtucode_opset_t *s, uint8_t op) {
    return s ? (s->opcode_slot[op]!=NULL) : false; }

bool    nrtucode_opset_private_has_specialization(const nrtucode_opset_t *s, uint8_t op, uint8_t sp) {
    const uint8_t *b = s ? s->opcode_slot[op] : NULL; return b ? (bool)b[sp] : false; }
```

---

## 6. Supporting members — `destroy`, `set_friendly_name`, `get_library_index`

**`nrtucode_opset_destroy`** (`0x9b25c0`, 153 B) — `abort` on NULL; log `"Destroyed
%s"` (`@0x50c0`, sev 4); then the loop `for (r14 = 1; r14 < 0x101; ++r14) { p =
*(opset + r14*8); if (p) free(p); }` (`@0x9b2600..0x9b2615`) frees every non-NULL spec
bitmap (`opset+0x8 .. opset+0x800` = `opcode_slot[0..255]`), then `free(opset)`
(`@0x9b261f`). Context is caller-owned and untouched. *[HIGH/OBSERVED]*

**`nrtucode_opset_set_friendly_name`** (`0x9b2910`) — log `"%s renamed %s"`
(`@0x51be`, sev 4); `strncpy(opset+0x808, name, 0x20)` (`@0x9b2949`); forced NUL at
`opset+0x828` (`movb $0x0,0x828(%r14)` @`0x9b294e`). Truncates to 31 chars + NUL. This
`+0x828` write is the evidence that `friendly_name` is `char[0x28]`, not `char[0x21]`
(§2 CORRECTION). *[HIGH/OBSERVED]*

**`nrtucode_opset_get_library_index`** (`0x9b1950`) — opcode→microcode-library
resolver; aborts on NULL `op_set`/`lib_index`. Does an SSE2 non-NULL-slot popcount; if
the opset is empty `*lib_index = 0` / return 0; for a small special-opcode set it also
emits lib 0; otherwise returns 1 ("use default lib 0"). The real opcode→library work
lives in [opcode-to-lib-resolver](opcode-to-lib-resolver.md)
(`nrtucode_ll_get_libraries_from_opcodes`), which this page only references.

> **CORRECTION / refinement — the special-opcode mask diverges stripped vs internal.**
> Internal `@0x9b1a18`: `cmp $0x25` (37); `movabs $0x2020202040,%rsi`; `bt` → mask bits
> **`{6,13,21,29,37}`**, bound 37. Stripped (shipped) `@0x309ec8`: `cmp $0x1d` (29);
> `mov $0x20202040,%ecx`; `bt` → **`{6,13,21,29}`**, bound 29. The shipped binary's
> tighter `≤29` bound makes bit 37 unreachable, so the **authoritative shipped set is
> `{6,13,21,29}`**; bit 37 (a later-generation slot) exists only in the dev/internal
> build — the same stripped-vs-internal divergence seen in the `ll_create` coretype
> table. *[HIGH/OBSERVED — both masks read directly]*

---

## 7. Strings (read byte-exact from `.rodata`)

| VMA | String | Used by |
| --- | --- | --- |
| `0x5548` | `nrtucode_opset_t@%p` | `create` default name |
| `0x52fa` | `Initialized %s` | `create` |
| `0x4f97` | `%s added opcode 0x%02x` | `add` (any opcode) |
| `0x5359` | `%s added extended opcode 0x%02x with specialization 0x%02x` | `add` (`0xF0`) |
| `0x50c0` | `Destroyed %s` | `destroy` |
| `0x51be` | `%s renamed %s` | `set_friendly_name` |
| `0x5469` | `nrtucode: invalid API usage in \`%s\`: \`%s\` is null\n` | all FATAL guards |
| `0x4861` / `0x4b2f` / `0x51d1` | `nrtucode_opset_create` / `…add_instruction` / `…destroy` | `__func__` in FATAL |
| `0x479f` / `0x5459` / `0x5195` | `context` / `op_set` / `instruction_data` | arg names in FATAL |

---

## 8. Lifecycle, device reconciliation, and the build/read split

No internal caller pins the order; the lifecycle is **inferred** from the exported ABI
and shipped headers:

1. `nrtucode_context_create(...)` → `context` (the owner).
2. `nrtucode_opset_create(context, &op_set)` — `malloc 0x830`, slots zeroed. *[OBSERVED]*
3. **for each** extended-ISA instruction parsed from the NEFF:
   `nrtucode_opset_add_instruction(op_set, instr)` — marks `instr[0]` (+ `instr[0x0c]`
   for `0xF0`). Per-instruction; the function consumes exactly one record. *[per-instruction OBSERVED; order INFERRED]*
4. introspect via the §5 queries to enumerate the model's unique opcodes / `0xF0`
   sub-ops — exactly the *"unique opcodes / subopcodes"* lists `nrtucode.h` (339-357)
   feeds into [`ll_get_libraries_from_opcodes`](opcode-to-lib-resolver.md). *[MED/INFERRED wiring]*
5. resolve the library and (downstream, not the opset) stage to device and resolve the
   device `kernel_info_table`.

**Device reconciliation** — the opset is the **on-demand `(opcode,spec)` presence
mirror** of the device tables, **not** a fixed 1:1 capability mirror. `create` never
pre-seeds (only zeroes slots); the opset is filled on demand, so it holds exactly what
the loaded model uses — a sparse subset over the same namespace the SEQ table (178
entries, `0xF0` = *ExtendedInst*) and the POOL `kernel_info_table`
(`(opcode<<24 | spec<<16)` key) dispatch on. For a NEFF exercising the full POOL `lib0`
set, `get_num_opcodes` reconciles to the **13 distinct primary opcodes**
(`0x41,0x45,0x46,0x47,0x51,0x52,0x7b,0x7c,0x7d,0x7e,0xbe,0xf0,0xf2`; `+0xe4` →
14 with the CPTC `lib3`), and `get_num_specializations(0xF0)` to **5** (specs
`{0,1,2,3,4}`; → 6 with CPTC spec 7). The `0xF0`-spec dimension is the deliberate point
of structural correspondence. See the
[opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) (140 real
opcodes) and [pool-ext-0xf0](../firmware/pool/pool-ext-0xf0.md). *[device facts
HIGH/OBSERVED; per-model membership INFERRED]*

The opset is the **build half** (`create` §3 + `add_instruction` §4) and **read half**
(the four queries §5) of a build/read split; it is host-RAM-only and runs *before* any
device IRAM load.

---

## 9. Adversarial self-verification

The five strongest claims, re-challenged against the binary:

1. **Struct = `0x830`, 256 slots, 8-byte slot record.** `mov $0x830,%edi` @`0x9b24df`
   (malloc); `mov $0x800,%edx` @`0x9b24fa` (memset over `opset+8`). `0x800 / 256 = 8`
   bytes/slot — a raw `uint8_t*`. Both count bounds re-confirmed in the queries
   (`cmp rax,0x100` in `get_num_opcodes`; `cmp rdx,0x100` in
   `get_num_specializations`). `0x830 = 8 + 0x800 + 0x28`. **CONFIRMED.**
   *Challenge:* could `friendly_name` be `0x21` with a `0x07` pad (as object-model-graph
   states)? *Resolution:* `set_friendly_name` writes the forced NUL at `+0x828`, the
   buffer spans `+0x808..+0x830`, and `0x830 - 0x808 = 0x28`. The `0x21` is the
   default-name `snprintf` cap, not the field size — **CORRECTION issued (§2).**

2. **The `add_instruction` store path.** First-sighting `calloc(1,0x100)`
   (`edi=1,esi=0x100` @`0x9b269a`) → store `mov %rax,0x8(%rbx,%r14,8)` @`0x9b26a9`
   (scale-8 pointer array). OOM → `mov $5,%eax` @`0x9b2725`. Store-before-NULL-check is
   harmless. **CONFIRMED.**

3. **The `0xF0` spec presence.** Spec read `movzbl 0xc(%rsi),%r9d` @`0x9b26e1`; bitmap
   read `cmpb $0,(%rcx,%r9,1)` @`0x9b26eb`; **set `movb $1,(%rcx,%r9,1)` @`0x9b26f0`**
   — scale 1, the only `movb` in the function, gated by two `cmp $0xf0` tests
   (@`0x9b2688`, @`0x9b26d8`). Non-`0xF0` paths execute no bitmap write. **CONFIRMED.**
   *Challenge:* does the non-`0xF0` path set `bitmap[0]`? *Resolution:* no — both
   non-`0xF0` edges (`jne 0x9b2691`, no-jump `0x9b268f`) reach `ret` before any
   `movb`. **CONFIRMED.**

4. **The four query semantics.** `has_opcode` = `cmpq 0,[rdi+rax*8+8]; setne` (slot ≠
   NULL). `has_specialization` = NULL-slot-guarded `movzbl bitmap[spec]`.
   `get_num_opcodes` = SSE2 non-NULL popcount (no stored counter). `get_num_specializations`
   = 8-way byte-sum (= popcount via the `movb $1` invariant). All four NULL `op_set` →
   0 gracefully. ELF sizes 22/30/178/113 match disasm extents. **CONFIRMED.**
   *Challenge:* is `get_num_opcodes` a stored counter? *Resolution:* the struct has no
   count field (§2) and the function recomputes via `movdqu`/`pcmpeqd`/`psubq` over 256
   slots — **live popcount, CONFIRMED.**

5. **No `_ZTV`, no internal callers, byte-exact strings.** `nm | rg -c '_ZTV'` = 0
   (so `opcode_slot[]` is a plain C array, slot = base + 8·N). `objdump | rg -c
   'call.*nrtucode_opset_…'` = 0 for all nine (pure exported API). Every `.rodata`
   string read byte-exact at its cited VMA (§7). **CONFIRMED.**

---

## 10. Honesty ledger

**HIGH / OBSERVED** — direct disasm or byte read, cross-checked stripped ↔ internal
(and, for `add`/queries, the static `.a` object):

- `0x830` struct = `context`(+0x00) + `opcode_slot[256]`(+0x08, 0x800 B) +
  `friendly_name char[0x28]`(+0x808). 8-byte slot record (`0x800/256`). The only
  struct-tail accesses are `+0x808` and `+0x828` (no hidden fields, no count field).
- Slot keyed by opcode byte (direct index, stride 8); NULL = absent, else a lazily
  `calloc(1,0x100)` 256-byte spec presence bitmap.
- The `0xF0` two-level escape (spec @ `instr[0x0c]`, the only `movb $1`); idempotent
  add; the OOM rc=5 path; all four query bodies + their graceful NULL→0; create /
  destroy / set_name sequences.
- ELF sizes (241/153/294 ; 178/113/22/30) = disasm extents; zero `_ZTV`; zero internal
  callers; all `.rodata` strings byte-exact; `__FILE__` `nrtucode_opset.c` @`0x9c076a`.
- `get_library_index` mask divergence: shipped `{6,13,21,29}` vs internal `+37`.
- CORRECTION vs `object-model-graph.md`: `friendly_name` is `char[0x28]`, not
  `char[0x21]`+`0x07` pad.

**MED / INFERRED** — the "presence mirror" framing and the *subset (not 1:1)*
conclusion (opset never pre-seeded, filled on demand → runtime content is
NEFF-dependent; the opcode *space* correspondence is OBSERVED, per-model *membership*
INFERRED); the lifecycle *order* (create → add → introspect → ll_create), inferred
from the ABI since no internal caller pins it; severity-tier *naming* (4 = trace),
inferred from message content (only the byte is OBSERVED).

**LOW / UNRESOLVED** — the NEFF `instruction_data` layout beyond bytes `[0]` and
`[0xc]` is opaque to the opset (consumed by the marshalling/dispatch path, out of
scope); whether the byte-sum in `get_num_specializations` is a deliberate choice over a
true popcount is a source-intent question the binary cannot decide (the OBSERVED
behavior and its popcount-equivalence are certain).
