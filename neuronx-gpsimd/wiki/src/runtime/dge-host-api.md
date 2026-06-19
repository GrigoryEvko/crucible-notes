# The DGE Host-Private API — priority / mailbox / PC-bounds

This page documents the x86-64 **host** runtime trio that configures the device DGE
(Descriptor Generation Engine) and the SEQ PC-fetch window on a **per-Q7-core** basis.
All three groups stage their writes into the **per-core DEVICE-DRAM control block**
through the platform read/write *memhandle* vtable — the host never pokes a CSR for
these; it forms a device SoC address `dram_base + OFFSET` and hands it to the
context-installed `read`/`write` function pointers (see
[The nrtucode context & memhandle ABI](nrtucode-context.md)).

The trio is a single compilation unit: the eight accessors sit **contiguously** in
`.text` from `0x9b1000` to `0x9b177f` (`nm -DS` ordering, [§ Symbol table](#symbol-table)),
sharing the `nrtucode_core_t` field layout and the same `rw_impl` dispatch convention.
All addresses below are in **`libnrtucode_internal.so`** (ELF64 x86-64, **not stripped**,
BuildID `9cbf78c6…`, 10,276,288 B); the section deltas are `.rodata` Δ=0, `.text`
Δ=0x1000, `.data.rel.ro` Δ=0x2000, `.data` Δ=0x3000 — so `.rodata` string offsets quoted
here are VMA==file-offset, while `.text` VMAs are 0x1000 above their file offset.

> NOTE — provenance. Everything here is from static analysis of the shipped, redistributable
> `libnrtucode_internal.so` (the not-stripped oracle) cross-checked against the stripped-but-
> exported `libnrtucode.so`, the shipped C headers `nrtucode.h` / `nrtucode_private.h`, and
> stock binutils. Recovered symbol names, `__PRETTY_FUNCTION__` / assert text, and header
> declarations are the binary's own embedded ground truth.

---

## The shared model: `nrtucode_core_t` + the rw_impl vtable

Every accessor reads the same five fields of `nrtucode_core_t` and dispatches through the
same two-slot read/write vtable. This is the spine the rest of the page hangs on.

```c
// nrtucode_core_t — the fields these accessors touch (offsets OBSERVED from the disasm
// across all eight functions; the full struct is the core runtime object).  [HIGH/OBSERVED]
struct nrtucode_core_t {
    /* +0x00 */ nrtucode_context_t *context;   // *context == rw_impl, the memhandle vtable
    /* +0x10 */ uint32_t            coretype;   // the NX_POOL kind gate reads this
    /* +0x20 */ uint64_t            dram_base;  // per-Q7-core DEVICE-DRAM control-block base
    /* +0x30 */ uint32_t            boot_state; // == 1 (BOOTED_LEGACY) gate
    /* +0x48 */ /* logger ctx */               // arg to nrtucode_context_log @0x9b0540
};

// The platform read/write memhandle vtable (rw_impl). It lives at *(core->context):
//   slot +0x00 = READ  (ctx, soc_addr, len, *dst)
//   slot +0x08 = WRITE (ctx, soc_addr, len, *src)
// The "soc_addr" is a DEVICE address (dram_base + OFFSET); the transfer is a memhandle
// DMA the platform layer performs — NOT a host memcpy.                      [HIGH/OBSERVED]
typedef nrtucode_result_t (*rw_fn)(void *ctx, uint64_t soc_addr, size_t len, void *buf);
struct rw_impl { rw_fn read /*+0x00*/; rw_fn write /*+0x08*/; /* ... */ };
```

The dispatch idiom, byte-exact from the priority SET tail (`0x9b103f`):

```
9b103f: mov rax,[rdi]        ; rax = core->context
9b1042: mov rsi,[rdi+0x20]   ; rsi = core->dram_base          (DEVICE SoC base)
9b1046: mov rdi,[rax]        ; rdi = *context = rw_impl vtable
9b1049: mov r8, [rdi+0x8]    ; r8  = rw_impl->write           (slot +0x08)
9b104d: add rsi,0x18         ; rsi = dram_base + 0x18         (the field address)
9b1051: mov rdi,rax          ; rdi = context  (arg0 of the call)
9b1055: jmp r8               ; tail-call write(context, dram_base+0x18, 4*n, src)
```

> GOTCHA — the staging target is **device** memory, not host. `rsi` is built from
> `core->dram_base` (`[rdi+0x20]`), the per-core control block that lives in **device DRAM**;
> the call writes through the memhandle `write` slot, which performs the host→device transfer.
> If you treat `dram_base+0x18` as a host pointer you will scribble on whatever the context
> object happens to alias. The whole trio is a **host-writes-device-DRAM** control surface.
> This is verified for every group below: each forms `[rdi+0x20]+OFFSET` and dispatches
> through `*(core->context)`. [HIGH/OBSERVED — `mov rax,[rdi]; mov rsi,[rdi+0x20]; mov rdi,[rax]`.]

The per-core DEVICE-DRAM control block, mapped from the four group offsets (each base
OBSERVED byte-exact; the non-overlap is INFERRED from the bases + each group's write cap):

| off (dram_base) | field                         | width        | group / public symbol                              |
|-----------------|-------------------------------|--------------|----------------------------------------------------|
| `+0x18`         | `priority_classes[0..n-1]`    | `n*4` (n≤4)  | priority — `dge_set/get_priority_class_map`         |
| `+0x28`         | `dge_mailbox[0..3]`           | `4 × 4` B    | mailbox — `get_dge_mailbox_addr` (returns +0x28)    |
| `+0x38`         | `pc_bounds_soc_addr_lo` (u64) | 8 B          | pc-bounds — `enable/disable_pc_bounds_check`        |
| `+0x40`         | `pc_bounds_soc_addr_hi` (u64) | 8 B          | pc-bounds — `private_get_pc_bounds`                 |

Live ranges are non-overlapping: priority `+0x18..+0x27` (4 effective u32), mailbox
`+0x28..+0x37` (4 × 4 B), pc-bounds `+0x38..+0x47` (two u64). The nominal 5th priority
u32 slot region (`+0x28..`) is **reused** by the mailbox — both groups cap their writes
so the live spans never collide.

> QUIRK — the trio does **not** share one prologue. Priority and mailbox-addr carry the full
> `null → boot_state==1 → NX_POOL-kind` gate; **pc-bounds carries neither the boot_state nor
> the kind gate** (only `core != NULL` plus its value checks). See the
> [CORRECTION](#correction-pc-bounds-has-the-light-gate). The shared part is the
> `dram_base+OFFSET` rw-vtable *model* and the `core != NULL` abort — not the prologue.

<a name="symbol-table"></a>
### Symbol table (`nm -DS --defined-only`, internal build)

| addr      | size  | symbol                                             | header / class           |
|-----------|-------|----------------------------------------------------|--------------------------|
| `0x9b1000`| `0xec`| `nrtucode_core_dge_set_priority_class_map`         | `nrtucode.h` — PUBLIC    |
| `0x9b10f0`| `0xeb`| `nrtucode_core_dge_get_priority_class_map`         | `nrtucode.h` — PUBLIC    |
| `0x9b11e0`| `0x19d`| `nrtucode_core_get_dge_mailbox_addr`              | `nrtucode.h` — PUBLIC    |
| `0x9b1380`| `0x79`| `nrtucode_core_private_set_dge_mailbox`            | `nrtucode_private.h` — PRIVATE |
| `0x9b1400`| `0xc3`| `nrtucode_core_private_get_dge_mailbox_values`     | `nrtucode_private.h` — PRIVATE |
| `0x9b14d0`| `0xf1`| `nrtucode_core_enable_pc_bounds_check`             | `nrtucode.h` — PUBLIC    |
| `0x9b15d0`| `0xb0`| `nrtucode_core_disable_pc_bounds_check`            | `nrtucode.h` — PUBLIC    |
| `0x9b1680`| `0xff`| `nrtucode_core_private_get_pc_bounds`              | `nrtucode_private.h` — PRIVATE |

The PUBLIC/PRIVATE split is the binary's own header partition. `nrtucode_private.h` opens
with `"PRIVATE HEADER: Do not include nrtucode_private.h directly!"` and the recovered comment
`"Direct use will trigger SEV2 incident against your team."` — the `_private_` symbols are
same-core-only and not part of the supported surface. The customop build `libnrtucode.so`
exports the identical set at `0x3094e0 / 0x3095d0 / 0x3096c0 / …`.

---

## Group 1 — priority class map (`+0x18`, PUBLIC)

### What it is — the 5-class model

`nrtucode_core_dge_set_priority_class_map` / `_get_priority_class_map` program and read
back the device DGE's per-class priority table: a contiguous `uint32_t[≤4]` at
`dram_base + 0x18`. The DGE supports **five** descriptor-generation priority classes,
ISA indices `0..4` — matching the device selector's predicate `priority_class <= 4`
(see [DGE backend selector](../firmware/dge/dge-backend-selector.md)). The host model
(from `nrtucode.h`):

- **ISA class 0 is reserved** as the firmware built-in default — *not* host-writable through
  this table.
- User-defined elements start at index 1: `priority_classes[0]` maps to **ISA class 1**,
  `priority_classes[1]` → ISA class 2, and so on. The cap is **4** user classes
  (`num_elements ≤ 4`, OBSERVED `cmp rdx,0x4`).
- Each `uint32_t` value is the *"preferred bytes per packet for DGE data transfer"* for that
  class (header doc; the value is a transfer-tuning hint, not a queue or weight field).

So **4 user-settable (ISA 1..4) + 1 reserved default (ISA 0) = the 5 classes** the device
selector reads. [HIGH/OBSERVED — the `cmp rdx,0x4` cap and the `+0x18` base from disasm; the
index→ISA-class map and bytes-per-packet semantics from the shipped header.]

```text
byte off (dram_base)   ISA priority class   user array index      origin
  +0x18  (u32)         class 1              priority_classes[0]    host-set
  +0x1c  (u32)         class 2              priority_classes[1]    host-set
  +0x20  (u32)         class 3              priority_classes[2]    host-set
  +0x24  (u32)         class 4              priority_classes[3]    host-set
  [ISA class 0]        built-in default     (not in this table)    firmware
```

> NOTE — there is **no per-class C struct** for the priority map. The header signature is
> `(nrtucode_core_t*, const uint32_t *priority_classes, size_t num_elements)`; the "struct"
> *is* the `uint32_t[≤4]` at `+0x18`. The write is a **single bulk `4*n`-byte transfer** with
> no per-element loop and no field unpacking — confirmed by the `shl rdx,0x2` (n→bytes) then a
> single `write` call. The queue / arbitration / QoS the table "configures" is realized
> **device-side**: the selector keys on the per-descriptor `priority_class` index; the host
> table supplies only the per-class bytes-per-packet value. The mailbox (Group 2), by contrast,
> *does* have a packed struct.

### SET — `nrtucode_core_dge_set_priority_class_map` @ `0x9b1000`

```c
// @0x9b1000  size 0xec  PUBLIC (nrtucode.h)
// rdi=core, rsi=priority_classes, rdx=num_elements ; ret eax = nrtucode_result_t
nrtucode_result_t nrtucode_core_dge_set_priority_class_map(
        nrtucode_core_t *core, const uint32_t *priority_classes, size_t num_elements)
{
    if (core == NULL)                                   // 9b1001 test rdi,rdi ; je 9b1096
        fprintf(stderr,"...`%s` is null","...set...","core"), abort();  // programmer error
    if (core->boot_state != 1 /*BOOTED_LEGACY*/)        // 9b100a cmp [rdi+0x30],1 ; jne
        return log_invalid("(core)->boot_state == BOOTED_LEGACY"), 8;   // 9b1058 → ret 8
    uint64_t k = core->coretype;                        // 9b1010 mov eax,[rdi+0x10]
    if (k > 0x20 || !( (0x102020204ULL >> k) & 1 ))     // 9b1013 cmp rax,0x20 ; ja
        return log_invalid("Expected core kind: NX_POOL"), 8;  // 9b101c movabs 0x102020204; bt; jae
    if (priority_classes == NULL)                       // 9b102c test rcx,rcx ; je 9b10c1
        fprintf(stderr,"...`%s` is null","...set...","priority_classes"), abort();
    if (num_elements > 4)                               // 9b1035 cmp rdx,0x4 ; ja 9b107c
        return 8;                                       // (no log — plain return 8)
    size_t len = num_elements << 2;                     // 9b103b shl rdx,0x2  (n*4 bytes)
    void  *ctx = core->context;                         // 9b103f mov rax,[rdi]
    uint64_t addr = core->dram_base + 0x18;             // 9b1042 mov rsi,[rdi+0x20]; 9b104d add rsi,0x18
    rw_fn write = ((struct rw_impl*)*ctx)->write;       // 9b1046 mov rdi,[rax]; 9b1049 mov r8,[rdi+0x8]
    return write(ctx, addr, len, priority_classes);     // 9b1055 jmp r8   (TAIL-CALL)
}
```

The five gates, byte-exact:

| # | gate                         | bytes / form                                              | failure        |
|---|------------------------------|-----------------------------------------------------------|----------------|
| 1 | `core != NULL`               | `test rdi,rdi ; je`                                       | fprintf + abort|
| 2 | `boot_state == 1`            | `cmp DWORD [rdi+0x30],0x1 ; jne`                          | log + return 8 |
| 3 | coretype ∈ NX_POOL set       | `mov eax,[rdi+0x10]; cmp rax,0x20; ja` then `movabs rsi,0x102020204; bt rsi,rax; jae` | log + return 8 |
| 4 | `priority_classes != NULL`   | `test rcx,rcx ; je`                                       | fprintf + abort|
| 5 | `num_elements <= 4`          | `cmp rdx,0x4 ; ja`                                        | return 8       |

> GOTCHA — two distinct failure modes. A **null pointer** (gate 1, gate 4) is a *hard
> `abort()`* — a programmer-error contract, not a returned code. A **policy violation**
> (gates 2/3/5) returns `nrtucode_result_t == 8` (the INVALID class). Callers must never feed
> these functions a null `core`/`priority_classes` expecting an error code back — the process
> dies. The null-abort uses `fprintf(stderr, "nrtucode: invalid API usage in `%s`: `%s` is
> null\n", __func__, argname)`; the policy gates use `nrtucode_context_log(ctx, 1, fmt, …)` via
> the logger context at `core+0x48`.

The core-kind mask `0x102020204` sets bits `{2,9,17,25,32}` — the five `*_NX_POOL` kinds
`{SUNDA(2), CAYMAN(9), MARIANA(17), MARIANA_PLUS(25), MAVERICK(32)}`. The recovered assert
string is the five-way `||` chain that the compiler lowered to the single `movabs ; bt`:
`"((core)->kind == NRTUCODE_CORE_SUNDA_NX_POOL) || … || ((core)->kind ==
NRTUCODE_CORE_MAVERICK_NX_POOL)) && \"Expected core kind: \" \"NX\" \"_\" \"POOL\""`
(confirmed present in `.rodata`). The bit positions are the enum indices, so the mask *is* the
five kinds, byte-for-byte.

### GET — `nrtucode_core_dge_get_priority_class_map` @ `0x9b10f0`

GET is **byte-identical to SET through all five gates and the address/length formation**.
The only semantic difference is the vtable slot it tail-calls:

```
SET @0x9b1049:  mov r8,[rdi+0x8]   ->  rw_impl->write  (slot +0x08)   ; host → device DRAM
GET @0x9b1139:  mov r8,[rdi+0x0]   ->  rw_impl->read   (slot +0x00)   ; device DRAM → host
```

(plus the `__func__`/assert strings carry `...get...` vs `...set...`). The `+0x18` address,
the `4*n` length, and the tail-call convention `(context, dram_base+0x18, 4*n, buf)` are
identical. This **read=+0x00 / write=+0x08, one-instruction delta** is the family signature —
it recurs in every group below.

---

## Group 2 — DGE mailbox (`+0x28`, addr PUBLIC + per-index PRIVATE)

### What it is — a 4-entry priority-DMA access-control array

The DGE mailbox is a **priority-based DMA access-control filter** — *not* a descriptor-ring
doorbell, *not* an SDMA trigger, *not* a host↔DGE command queue. Up to **4** mailboxes live at
`dram_base + 0x28`, each a packed 4-byte struct (the binary's own header):

```c
// nrtucode.h — sizeof == 4. This is the "u32 mailbox" the disasm copies (edx=4, stride rsi*4).
typedef struct nrtucode_dge_mailbox {
    uint8_t  reserved;  // byte 0  — must be 0
    uint8_t  priority;  // byte 1  — [0-255], higher value = LOWER priority, default 0xFF
    uint16_t bitmask;   // bytes 2-3 — 16-bit DMA enable mask (little-endian), default 0xFFFF
} nrtucode_dge_mailbox_t;
```

The runtime rule (header doc): the device starts with all DMAs enabled (`0xFFFF`); for each
mailbox, **if the DGE operation's priority > the mailbox priority, AND-mask the DMA bitmask**.
So the host throttles *which* of the 16 DMA channels a given priority tier of descriptor
generation may use. A default mailbox `{0, 0xFF, 0xFFFF}` never masks anything (priority `0xFF`
is the lowest, so nothing exceeds it; bitmask `0xFFFF` enables all). The device consumer is the
descriptor-emit `dma_mask` gating (see [DGE backend selector](../firmware/dge/dge-backend-selector.md)).

### `nrtucode_core_get_dge_mailbox_addr` @ `0x9b11e0` (PUBLIC)

```c
// @0x9b11e0  size 0x19d  PUBLIC
// rdi=core, rsi=num_mailbox [0-4], rdx=uint64_t *addr (OUT) ; ret eax
nrtucode_result_t nrtucode_core_get_dge_mailbox_addr(
        nrtucode_core_t *core, size_t num_mailbox, uint64_t *addr)
{
    if (core == NULL)              abort();                  // 9b11eb test rdi,rdi; je
    if (core->boot_state != 1)     return log_invalid(...), 8;  // 9b11f7 cmp [rdi+0x30],1
    uint64_t k = core->coretype;                             // 9b1201 mov eax,[r14+0x10]
    if (k > 0x20 || !((0x102020204ULL >> k) & 1))            // 9b120f movabs r13,0x102020204; bt
                                   return log_invalid(...), 8;
    if (addr == NULL)              abort();                  // 9b1226 test rdx,rdx; je
    if (num_mailbox > 4)           return 8;                 // 9b1237 cmp rsi,0x4; ja

    // reset the trailing (4 - num_mailbox) entries to the default sentinel 0xffffff00:
    for (i = num_mailbox; i < 4; i++)                        // 9b1290 mov [rsp+0x4],0xffffff00
        write(ctx, core->dram_base + 0x28 + i*4, 4, &sentinel); // 9b12bd call [rax+0x8]  (WRITE)

    *addr = core->dram_base + 0x28;                          // 9b12cd mov rax,[r14+0x20];
                                                             // 9b12d1 add rax,0x28; 9b12d5 mov [rbx],rax
    return 0;
}
```

It carries the **full** five-gate prologue (same boot_state + NX_POOL mask as priority). Two
behaviours worth pinning:

- **The returned address is the array BASE** (`dram_base+0x28`), not an entry. The caller —
  typically an *external* host/core — then DMA-writes the 4-entry array directly, exactly as the
  header example shows (`write(dge_mailbox_addr, mailboxes, sizeof(mailboxes))` for the bulk
  write, or `write(dge_mailbox_addr + 3*sizeof(nrtucode_dge_mailbox_t), &mb3, …)` for one slot).
  This get-addr path exists *precisely so* an external agent can bypass the in-core-only private
  set/get below.
- **It resets excess slots** to the `0xffffff00` sentinel (little-endian = `{reserved 0,
  priority 0xFF, bitmask 0xFFFF}` — the per-field defaults) via the `write` slot. "When reducing
  mailbox count, only the first N mailboxes retain their values; excess mailboxes are reset to
  defaults" (header). The `je 0x4` short-circuits the loop when all 4 are requested.

### `nrtucode_core_private_set_dge_mailbox` @ `0x9b1380` (PRIVATE)

```c
// @0x9b1380  size 0x79  PRIVATE (nrtucode_private.h: in-core only)
// rdi=core, rsi=mailbox_idx [0-3], edx=nrtucode_dge_mailbox_t (BY VALUE, 4 bytes in a reg)
nrtucode_result_t nrtucode_core_private_set_dge_mailbox(
        nrtucode_core_t *core, size_t mailbox_idx, nrtucode_dge_mailbox_t mailbox)
{
    /* thin gate — NO null-core abort, NO boot_state check */
    uint64_t k = core->coretype;                            // 9b1385 mov eax,[rdi+0x10]
    if (k > 0x20 || !((0x102020204ULL >> k) & 1))           // 9b138e movabs rcx,0x102020204; bt
                                   return log_invalid(...), 8;
    if (mailbox_idx > 3)           return 8;                // 9b13a3 cmp rsi,0x3; ja
    uint64_t addr = core->dram_base + 0x28 + mailbox_idx*4; // 9b13ac mov rcx,[rdi+0x20];
                                                            // 9b13b3 lea rsi,[rcx+rsi*4]; 9b13b7 add rsi,0x28
    return write(ctx, addr, 4, &spilled_mailbox);           // 9b13c0 mov edx,0x4; 9b13c8 call [r8+0x8] (WRITE)
}
```

### `nrtucode_core_private_get_dge_mailbox_values` @ `0x9b1400` (PRIVATE)

Mirror of the setter: same thin gate (kind-mask + `cmp rsi,0x3` idx cap), **plus** an out-ptr
`abort()` check (`test rdx,rdx ; je`), then `lea rsi,[rcx+rsi*4]; add rsi,0x28` (entry @
`+0x28+idx*4`), `edx=0x4`, and `call [r8]` — the **READ** slot (`+0x00`). It reads one 4-byte
mailbox back into `*mailbox`.

> NOTE — the two private mutators **omit the boot_state gate** the public addr-getter enforces.
> In-core private callers run after boot by construction, so the boot check is redundant for
> them; the public addr path (callable by an external agent at arbitrary time) keeps it. All
> three still require an NX_POOL kind — the mailbox is a DGE feature, never relaxed to non-POOL
> cores. The name asymmetry (`private_set_dge_mailbox` singular vs
> `private_get_dge_mailbox_values`) is in the symbol table itself — both operate on one index.

So Group 2 is a **single control-block field** (the 4×4-byte array at `+0x28`) with a 3-way
API: addr-getter (returns base + resets trailing slots), set-one (write index), get-one (read
index). The set/reset paths use `write` (`+0x08`); get-one uses `read` (`+0x00`) — the same
read/write split as Group 1.

---

## Group 3 — PC-bounds check (`+0x38`/`+0x40`, enable/disable PUBLIC + get PRIVATE)

### What it is — a `[lo,hi]` SEQ-fetch window

`enable_pc_bounds_check` / `disable_pc_bounds_check` / `private_get_pc_bounds` arm, disarm,
and read back the **device SEQ engine's PC-range bound**: a pair of 64-bit SoC addresses
`[lower, upper]` staged at `dram_base + 0x38` (lower) / `dram_base + 0x40` (upper). The device
SEQ front-end (`is_pc_in_bounds`, see [SEQ PC-bounds](../firmware/seq/pc-bounds.md)) consults
this window when deciding whether a **speculative** instruction-fetch address is in range;
out-of-range speculative prefetch is *skipped with a warning* (SOFT), never a hard fault on the
speculative path.

```text
dram_base + 0x38  (u64, 8 B)  = pc_bounds_soc_addr_lo  (LOWER SoC bound)
dram_base + 0x40  (u64, 8 B)  = pc_bounds_soc_addr_hi  (UPPER SoC bound)
```

Two **independent u64 fields**, not a `u32[2]` and not a packed struct with an enable byte:
each store/load is a separate vtable call with `edx=8` (`mov edx,0x8` at all six call sites).
ENABLE/DISABLE do two `write` calls (`+0x38` then `+0x40`); GET does two `read` calls.

<a name="correction-pc-bounds-has-the-light-gate"></a>
> CORRECTION — PC-bounds has the **LIGHT** gate; it does **not** share the DGE prologue. A
> full-body scan of `0x9b14d0..0x9b177f` finds **zero** `[rdi+0x30]` (boot_state), **zero**
> `[rdi+0x10]` (coretype), and **no** `movabs 0x102020204 ; bt` and **no** `cmp rax,0x20` /
> `cmp eax,0x19` cap anywhere in the three functions (verified — the grep returns nothing). The
> only `+0x30`/`+0x10` byte values present are stack arithmetic (`[rsp+0x10]`, `sub/add rsp`),
> not struct fields. The pc-bounds family validates **only** `core != NULL` (abort) + (for
> ENABLE) `lo < hi` + (for GET) the two out-pointers non-null. This is a structurally
> *lighter* gate than priority/mailbox. Any trio summary that asserts "all three share the exact
> `boot_state==1` + NX_POOL bt-mask prologue" is wrong for this leg — that prologue is specific
> to the DGE legs. The reason is semantic: pc-bounds is a **SEQ-engine fetch** feature
> applicable to any ucode-running core, whereas priority/mailbox are DGE features whose device
> consumer exists only on NX_POOL cores. So pc-bounds is deliberately *not* kind-restricted.
> [HIGH/OBSERVED — full-body scan, both builds.]

### ENABLE — `nrtucode_core_enable_pc_bounds_check` @ `0x9b14d0`

```c
// @0x9b14d0  size 0xf1  PUBLIC (nrtucode.h)
// rdi=core, rsi=pc_bounds_soc_addr_lo, rdx=pc_bounds_soc_addr_hi ; ret eax
nrtucode_result_t nrtucode_core_enable_pc_bounds_check(
        nrtucode_core_t *core, uint64_t lo, uint64_t hi)
{
    if (core == NULL)  abort();                 // 9b14df test rdi,rdi; je 9b1596 (fprintf "core")
    if (lo >= hi)                               // 9b14f1 cmp rsi,rdx; jae 9b1518  (UNSIGNED)
        return log_invalid(/* "lower bound(0x%lx) must be smaller than upper bound(0x%lx)" */), 8;
    void *ctx = core->context;
    if (write(ctx, core->dram_base + 0x38, 8, &lo) != 0)   // 9b14fd add rsi,0x38; 9b1506 edx=8;
        return err;                                        // 9b150b call [rax+0x8]  (WRITE)
    if (write(ctx, core->dram_base + 0x40, 8, &hi) != 0)   // 9b1552 add rsi,0x40; 9b1560 call [rax+0x8]
        return err;
    log_info("%s: enabled PC bounds check for range [0x%lx, 0x%lx]", __func__, lo, hi); // 9b1567
    return 0;
}
```

Note: there is **no enable bit** — "enabling" *is* writing a real bracketing `[lo,hi]` window
with `lo < hi`. The `cmp rsi,rdx ; jae` rejects `lo >= hi` with the recovered error string
`"enable_pc_bounds_check : lower bound(0x%lx) must be smaller than upper bound(0x%lx)"`. The
store **order** (lo first → `+0x38`, hi second → `+0x40`) fixes the polarity.

### DISABLE — `nrtucode_core_disable_pc_bounds_check` @ `0x9b15d0`

```c
// @0x9b15d0  size 0xb0  PUBLIC
nrtucode_result_t nrtucode_core_disable_pc_bounds_check(nrtucode_core_t *core)
{
    if (core == NULL) abort();                       // 9b15d5 test rdi,rdi; je
    uint64_t lo = 0x0;                               // 9b15dd mov QWORD [rsp+0x8],0x0
    uint64_t hi = 0xFFFFFFFFFFFFFFFF;                // 9b15e6 mov QWORD [rsp],-1
    write(ctx, core->dram_base + 0x38, 8, &lo);      // 9b15f8 add rsi,0x38; 9b1606 call [rax+0x8]
    write(ctx, core->dram_base + 0x40, 8, &hi);      // 9b161d add rsi,0x40; 9b1629 call [rax+0x8]
    log_info("%s: disabled PC bounds check", __func__); // 9b1630
    return 0;
}
```

> CORRECTION — DISABLE writes the **FULL** window `[LOWER=0x0, UPPER=0xFFFFFFFFFFFFFFFF]`, not
> an inverted/empty window. The bytes are `mov QWORD [rsp+0x8],0x0` and `mov QWORD [rsp],-1`,
> and the slot pairing is settled **beyond doubt** by the GET store-out (below): GET reads
> `+0x38` into `[rsp+0x8]` and copies it to `*lo`, reads `+0x40` into `[rsp]` and copies it to
> `*hi`. Therefore DISABLE puts `0x0` at `+0x38` (LOWER) and `0xFFFF..` at `+0x40` (UPPER).
> Because the device check is `pc >= lower && pc <= upper`, a `[0, MAX]` window makes
> `is_pc_in_bounds` return **true for every address** → no speculative prefetch is ever
> skipped → "no PC-range restriction", the natural meaning of *disable*. Any reading that
> DISABLE programs `[0xFFFF.., 0]` (so every prefetch is skipped) is a lo↔slot mis-pairing of
> these same bytes. [HIGH/OBSERVED — disable sentinel bytes + GET store-out ground truth.]

> GOTCHA — the power-on **zero default** is *not* the same as DISABLE. The device bounds global
> is zero-initialised → a `{0,0}` window where only `pc==0` passes, so every nonzero speculative
> prefetch is skipped until the host arms or disables. An explicit DISABLE *opens* all
> speculation (`[0,MAX]`); the zero default *blocks* it. The host `lo < hi` ENABLE guard
> guarantees an *armed* window is always a proper non-empty interval.

### GET — `nrtucode_core_private_get_pc_bounds` @ `0x9b1680` (PRIVATE)

```c
// @0x9b1680  size 0xff  PRIVATE (nrtucode_private.h)
// rdi=core, rsi=uint64_t *lo (OUT), rdx=uint64_t *hi (OUT) ; ret eax
nrtucode_result_t nrtucode_core_private_get_pc_bounds(
        nrtucode_core_t *core, uint64_t *lo_out, uint64_t *hi_out)
{
    if (core == NULL)    abort();                    // 9b1689 test rdi,rdi; je ("core")
    if (lo_out == NULL)  abort();                    // 9b1691 test rsi,rsi; je ("pc_bounds_soc_addr_lo")
    if (hi_out == NULL)  abort();                    // 9b169d test rdx,rdx; je ("pc_bounds_soc_addr_hi")
    uint64_t lo, hi;
    if (read(ctx, core->dram_base + 0x38, 8, &lo) != 0) return err; // 9b16b3 add rsi,0x38; 9b16c1 call [rax]
    if (read(ctx, core->dram_base + 0x40, 8, &hi) != 0) return err; // 9b16d1 add rsi,0x40; 9b16dd call [rax]
    *lo_out = lo;   // 9b16e3 mov rax,[rsp+0x8]; mov [r14],rax   (+0x38 → *lo  : LOWER)
    *hi_out = hi;   // 9b16eb mov rax,[rsp];     mov [rbx],rax   (+0x40 → *hi  : UPPER)
    return 0;
}
```

GET uses the **READ** slot (`call [rax]`, `+0x00`) for both loads — the one-slot delta from
ENABLE/DISABLE's WRITE (`call [rax+0x8]`, `+0x08`). The two device reads land in stack locals
and are copied to the caller's out-pointers **only on full success**. This store-out is the
ground truth that fixes `+0x38 = LOWER`, `+0x40 = UPPER`.

> NOTE — unlike Groups 1/2, the pc-bounds functions do **not** tail-call. They make two
> sequential vtable calls (one per u64 field) and inspect each return code, because both
> independent fields must succeed. On any nonzero vtable return the function short-circuits and
> returns that error (a vtable failure is a *returned* error, distinct from the null-arg abort).

---

## Per-build stability — the MAVERICK delta

The two shipped builds differ in **exactly one place**: the NX_POOL core-kind gate of the
**priority** and **mailbox** legs.

| build                       | cap          | mask form              | kinds admitted                          |
|-----------------------------|--------------|------------------------|-----------------------------------------|
| `libnrtucode_internal.so`   | `cmp rax,0x20`| `movabs r,0x102020204` | `{2,9,17,25,32}` = +MAVERICK_NX_POOL    |
| `libnrtucode.so` (customop) | `cmp eax,0x19`| `mov e,0x2020204`      | `{2,9,17,25}` — MAVERICK gated out      |

This tracks the header: `NRTUCODE_CORE_MAVERICK_NX_POOL` is guarded by
`#if defined(NRTUCODE_INTERNAL_NAMES)`. The internal build compiled that branch in (bit 32
needs a 64-bit `movabs`); the customop build did not (a 32-bit `mov` suffices). Everything
else — the `+0x18`/`+0x28`/`+0x38`/`+0x40` offsets, the `n≤4` / `idx≤3` caps, the bulk vs
per-index transfers, the read(+0x00)/write(+0x08) dispatch — is byte-identical across both
builds.

> QUIRK — **pc-bounds has NO build delta.** Because it carries no coretype gate at all
> (see the [CORRECTION](#correction-pc-bounds-has-the-light-gate)), there is nothing to differ:
> all three pc-bounds functions are mnemonic-identical between the two builds (only PIC
> displacements into each build's own `.rodata` and the resolved `context_log`/`fprintf`/`abort`
> targets differ). The MAVERICK question simply does not arise for the SEQ-fetch leg.

---

## Cross-references

- [Host↔device descriptor handoff](host-device-descriptor-handoff.md) — pins the priority SET
  `@0x9b1000` anchors (`cmpl $1,0x30`, `movabs $0x102020204; bt`, `add $0x18,%rsi`); this page
  reuses those exact anchors and goes deeper on the set/get pair + the 5-class semantics.
- [The nrtucode context & memhandle ABI](nrtucode-context.md) — the 5-slot memhandle vtable at
  `*context` (read `+0x00` / write `+0x08`) every accessor here dispatches through.
- [DGE backend selector](../firmware/dge/dge-backend-selector.md) — the **device** consumer of
  the `+0x18` priority table (`priority_class <= 4` predicate) and the mailbox DMA gating.
- [SEQ PC-bounds](../firmware/seq/pc-bounds.md) — the **device** `is_pc_in_bounds` check that
  reads the `[lo,hi]` window this page's pc-bounds API writes.

---

## Confidence ledger

- **HIGH / OBSERVED** — all eight symbols' addr/size/export (`nm`); the priority SET/GET full
  disasm + five gates + `+0x18` + `4*n` length + write(+0x08)/read(+0x00) one-instruction delta;
  the `0x102020204` mask = `{2,9,17,25,32}`; the mailbox `+0x28` base/return/reset-sentinel
  `0xffffff00` + per-index `+0x28+idx*4` + `nrtucode_dge_mailbox_t` 4-byte struct + `idx≤3`/
  `num≤4` caps; the pc-bounds `+0x38`/`+0x40` u64 fields (`edx=8` at all six call sites) + the
  **absence** of the boot_state/coretype/NX_POOL gate (full-body scan) + the DISABLE full-window
  `[0x0, 0xFFFF..]` sentinel settled by the GET store-out; the device-DRAM staging path
  (`mov rax,[rdi]; mov rsi,[rdi+0x20]; mov rdi,[rax]`); the public/private header partition; the
  per-build MAVERICK delta byte-exact; pc-bounds no-delta mnemonic identity.
- **HIGH / INFERRED** — the priority table's "queue/arbitration is device-side, host value =
  bytes-per-packet only" reading (the flat `u32` copy is OBSERVED; the device arbitration is the
  selector's); pc-bounds applying to a wider core set than NX_POOL (gate absence OBSERVED; the
  wider-applicability conclusion from the SEQ-front-end grounding).
- **MED** — the exact device read site of the `+0x18` table; the host-field `+0x38`/`+0x40` ↔
  device bounds-global write-edge (a register-window/CSR mapping, not a const-offset STORE).
- **CARRIED** — the device-side selector/`is_pc_in_bounds` semantics are summarised from the
  linked firmware pages, not re-decoded here.
