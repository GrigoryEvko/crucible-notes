# neuron_translate + the Window Table

> **Scope.** This page decodes the device-side address-translation primitive
> at the bottom of every tensor touch on the Vision-Q7 GPSIMD engine:
> `neuron_translate(void*, unsigned long long)` and the
> `neuron_translate*` family, the 168-byte `_translation_ctx_t` window table,
> the per-window match math, and the host counterpart that fills the table.
> Every `Q7Ptr::operator[]`, every `TensorAccessor` leaf dereference, every
> TCM staging copy and the stack-switch funnel a 64-bit HBM/SoC address through
> this code to obtain a 32-bit NX-local address the Q7 LX core can actually
> dereference. This is the *consumer* side of the SoC↔Q7 translation windows;
> the *producer* (the host window manager) is described at the end and in the
> companion control page.
>
> **Evidence base.** All anchors below are from
> `libneuroncustomop.a(translation.o)` — an ELF32-Xtensa relocatable with DWARF
> v4 debug info, producer string `XtensaTools-14.09 clang version 10.0.1`,
> compiled from `/opt/workspace/SundaCustomOpLibrary/custom_op/library/translation.cpp`
> (`DW_AT_comp_dir`, `.debug_info` @off 0xc). Disassembly was produced with the
> shipped `xtensa-elf-objdump` under `XTENSA_CORE=ncore2gp`. The host
> counterpart is `libnrtucode_internal.so`. Confidence/provenance tags follow
> each claim: HIGH/MED/LOW × OBSERVED (read directly) / INFERRED (derived) /
> CARRIED (from the shared Part-7 pack).

---

## 1. The four exported symbols

`nm translation.o` exposes the entire family plus two BSS globals it owns
(HIGH/OBSERVED):

| Mangled symbol | Demangled | `.text` offset | Role |
|---|---|---|---|
| `_Z19_init_translate_ctxv` | `_init_translate_ctx()` | `0x000` | One-shot: fills the 5 window records |
| `_Z20neuron_translate_ctxv` | `neuron_translate_ctx()` | `0x110` | Accessor: returns the global ctx pointer |
| `_Z16neuron_translatePvy` | `neuron_translate(void*, unsigned long long)` | `0x120` | The hot path: address → NX-local address |
| `_Z31neuron_translate_mapping_lookupPvyPyS0_` | `neuron_translate_mapping_lookup(void*, unsigned long long, unsigned long long*, void*)` | `0x264` | Reverse / diagnostic: returns window + offsets |

Owned globals (`nm` symbol types, HIGH/OBSERVED):

| Symbol | Bind | Offset | DWARF type |
|---|---|---|---|
| `_ctx` | `B` (BSS) | `0x00` | `_translation_ctx*` (`DW_TAG_pointer_type` → typedef `_translation_ctx`, DIE @0x37) |
| `_sbuf_window` | `B` (BSS) | `0x08` | `uint64_t` (DIE @0x110) |

Undefined inputs it imports (the table is *built from these*, HIGH/OBSERVED via `.rela.text`):

| Undefined symbol | Demangled | Used by |
|---|---|---|
| `data_scratch_map` | `data_scratch_map` | `_init_translate_ctx` (lines 58–62) |
| `_ZN12extended_isa3sdk11hbm_scratchE` | `extended_isa::sdk::hbm_scratch` | `_init_translate_ctx` |

`_ctx`, `data_scratch_map`, and `hbm_scratch` are all defined elsewhere in the
same archive — `start_exit.o` exports `data_scratch_map` (`B`, @0x28) and the
`extended_isa::sdk::{data_scratch,data_scratch_size,hbm_scratch,hbm_scratch_size}`
quartet (HIGH/OBSERVED, `nm start_exit.o`). The callers of all three exported
translate functions are `data_transfer.o` and `wrapper_api.o`
(`U _Z16neuron_translatePvy` etc., HIGH/OBSERVED); `TensorTcmAccessor.o` reaches
the primitive transitively through `data_transfer.o`, not directly.

> **NOTE.** There are exactly four `neuron_translate`-prefixed symbols. A sweep
> of `libnrtucode_internal.so` (host runtime) finds **no** `neuron_translate`
> symbol and **no** `_translation_ctx`/`_map_record` string — confirming the
> primitive is device-only. The host's role is to *populate* the window source
> (`data_scratch_map`, `hbm_scratch`) via its own `soc_window_manager`; see §8.

---

## 2. The window table: `_translation_ctx_t` (168 bytes)

DWARF gives the layout without ambiguity (HIGH/OBSERVED, `.debug_info` DIE
@0x47, declared at `translation.hpp:24`):

```c
// translation.hpp:17  — one window record, 32 bytes
typedef struct {
    uint64_t ptr;          // +0   masked tag: the value (addr & mask) must equal
    uint32_t window;       // +8   NX-local base address of this window (32-bit!)
    // +12 : 4 bytes implicit padding (mask @16 is 8-aligned)
    uint64_t mask;         // +16  selects the tag bits; ~mask selects the offset
    uint64_t reg_location;  // +24  SoC register/aperture this window maps (diagnostic/setup)
} _map_record_t;            // sizeof == 32  (DW_AT_byte_size)

// translation.hpp:24  — the context, 168 bytes
typedef struct {
    _map_record records[5]; // +0    5 windows × 32B = 160B  (DW_AT_count = 5)
    uint8_t     next_alloc;  // +160  round-robin victim cursor for the dynamic slot
    // +161 : 7 bytes tail padding → sizeof == 168
} _translation_ctx_t;        // sizeof == 168  (DW_AT_byte_size)
```

The per-field types are pinned by DWARF type DIEs, not guessed:
`ptr`/`mask`/`reg_location` → typedef `uint64_t` (DIE 0xbb → `long long unsigned int`, 8B);
`window` → typedef `uint32_t` (DIE 0xd9 → `unsigned int`, 4B);
`next_alloc` → typedef `uint8_t` (DIE 0xfe → `unsigned char`, 1B). (HIGH/OBSERVED.)

> **CORRECTION (vs the task's verified-anchor framing).** The anchor brief
> described the per-entry sub-layout as *base/limit/mask/soc-offset* and a walk
> reading "ctx slots at +16/+48/+80/+112/+144". The binary's DWARF disagrees on
> two points and agrees on a third:
> 1. There is **no `limit` field**. The per-window fields are
>    `{ptr@0, window@8, mask@16, reg_location@24}`. The translation is a
>    *masked-tag compare*, not a `[base,limit)` range check.
> 2. The window base is `window` (a **`uint32_t`**, the NX-local address), and
>    the returned address is **`window + (addr & ~mask)`**, not
>    `base + (ptr & ~mask)`. `ptr` is the comparison tag, never an addend.
> 3. The offsets `+16/+48/+80/+112/+144` are exactly the **`mask`** fields of
>    records 0..4 (`16 + 32·k`), which is the first field the walk reads per
>    iteration — so that observation is correct, but it is the mask, not a
>    standalone "slot".
>
> Net layout truth: `records[k].{ptr,window,mask,reg_location}` at byte offsets
> `{32k+0, 32k+8, 32k+16, 32k+24}` for `k ∈ 0..4`.

The five window-record byte offsets the hot path actually loads are therefore:

| Window `k` | `ptr` (tag) | `window` (base) | `mask` |
|---|---|---|---|
| 0 | +0  | +8  | +16 |
| 1 | +32 | +40 | +48 |
| 2 | +64 | +72 | +80 |
| 3 | +96 | +104 | +112 |
| 4 | +128 | +136 | +144 |

These match the literal `l32i` displacements in §4 exactly (HIGH/OBSERVED).

---

## 3. `neuron_translate_ctx()` — sourcing the context pointer

The accessor is four instructions (HIGH/OBSERVED, `.text` @0x110):

```
110: entry   a1, 32
113: const16 a2, 0          ; \  R_XTENSA_SLOT0_*  →  _ctx
116: const16 a2, 0          ; /  (two halves of the absolute address of _ctx)
119: l32i.n  a2, a2, 0      ; a2 = *(_translation_ctx**)&_ctx   ==  _ctx
11b: retw.n                 ; return that pointer
```

`.rela.text` confirms both `const16` slots relocate against `_ctx + 0`
(`R_XTENSA_SLOT0_AL`/`R_XTENSA_SLOT0_OP` @0x113/0x116, HIGH/OBSERVED). So:

```c
_translation_ctx* neuron_translate_ctx(void) { return _ctx; }
```

`_ctx` is a **single global pointer** in this module's BSS — there is **no
per-core indirection** inside `translation.o` itself (no thread-pointer read,
no core-id multiply). On a GPSIMD engine each NX core runs its own copy of the
custom-op image, so "global" is per-core by virtue of the address space, not by
an explicit per-core table.  (HIGH/INFERRED: absence of a `rur`/thread-id read
in the disassembly; the per-image-per-core model is the GPSIMD SPMD norm.)

`_ctx` is set up by `_init_translate_ctx` (§5), which writes the *address of the
ctx storage* — note `_init_translate_ctx` reads `_ctx` first (`l32i a2,a2,0` at
off 0x46 against `data_scratch_map`) so the actual `_translation_ctx_t` bytes
live inside the `data_scratch_map` region, and `_ctx` is a handle into it.
(MED/INFERRED from the `data_scratch_map` relocation feeding the same `a2` that
all the `s32i a?,a2,*` record stores target.)

---

## 4. `neuron_translate()` — the hot path

Signature (Itanium mangling `Pvy`): `void* neuron_translate(void* ctx, unsigned long long addr)`.
Under the Xtensa windowed ABI the incoming args after `entry` are `ctx = a2`,
`addr_lo = a4`, `addr_hi = a5`. The body is a fully-unrolled 5-way linear scan;
each window is one ~6-instruction block, identical except for the record
offset and the index constant. Window 0 (HIGH/OBSERVED, `.text` @0x120):

```
120: entry   a1, 32
123: { l32i a2, a2, 16 ;  l32i a6, a2, 20 ;  mov.a a3, a2 ; nop }
        ; a2 = records[0].mask_lo  (ctx+16)
        ; a6 = records[0].mask_hi  (ctx+20)   (reads pre-bundle a2 = ctx)
        ; a3 = ctx                 (mov.a reads pre-bundle a2)
133: { l32i a7, a3, 0  ;  l32i a8, a3, 4 }
        ; a7 = records[0].ptr_lo   (ctx+0)
        ; a8 = records[0].ptr_hi   (ctx+4)
13b: { and a6, a6, a5  ;  and a9, a2, a4 }   ; a6 = mask_hi&addr_hi ; a9 = mask_lo&addr_lo
143: { xor a7, a9, a7  ;  xor a15, a6, a8 }  ; a7 = (addr_lo&mask_lo)^ptr_lo
                                             ; a15= (addr_hi&mask_hi)^ptr_hi
14b: or    a7, a7, a15                       ; a7 == 0  ⟺  (addr & mask) == ptr
14e: movi.n a6, 0                            ; a6 = matching window index = 0
150: beqz  a7, 0x204                         ; on match → finalize
```

Windows 1–4 are byte-for-byte copies with record displacements
`{48/52, 32/36}`, `{80/84, 64/68}`, `{112/116, 96/100}`, `{144/148, 128/132}`
and index constants `movi a6, 1..4` (HIGH/OBSERVED @0x153, 0x17b, 0x1a4,
0x1d7). All four hit-branches target the same finalizer `0x204`; the last
window (4) instead falls through to a *miss* path at `0x215` via
`bnez.n a7, 0x215` (off 0x202).

### The match predicate

Every window computes, in full 64-bit width:

```c
matched = ((addr & record.mask) ^ record.ptr) == 0;   // i.e. (addr & mask) == ptr
```

`record.ptr` is therefore the **canonical masked tag** of the window — the
value `(addr & mask)` takes for any address that belongs to this window. This
is a classic TLB-style associative compare, not a base/bound range test
(HIGH/OBSERVED + INFERRED).

### The finalizer (`0x204`)

```
204: slli  a5, a6, 5          ; a5 = index * 32  (record byte offset)
207: add.n a3, a3, a5         ; a3 = &records[index]
209: and   a2, a4, a2         ; a2 = addr_lo & mask_lo
20c: l32i.n a3, a3, 8         ; a3 = records[index].window   (the 32-bit base)
20e: xor   a2, a4, a2         ; a2 = addr_lo ^ (addr_lo & mask_lo) = addr_lo & ~mask_lo
211: add.n a2, a3, a2         ; a2 = window + (addr & ~mask)
213: retw.n                   ; return  (32-bit NX-local address in a2)
```

> **GOTCHA.** Only the **low 32 bits** of the offset participate
> (`a4 = addr_lo`); `mask_hi` was used only in the compare, never in the
> offset. The returned address is a 32-bit NX-local pointer — exactly what an
> LX `l32i`/`s32i` needs. This is why `window` is a `uint32_t` and why the
> function's nominal return type is `void*` (32-bit on this ELF32 target).

The complete fast path as annotated C:

```c
// neuron_translate.cpp:reconstructed  — translation.cpp, _Z16neuron_translatePvy
void* neuron_translate(void* ctx_, unsigned long long addr) {
    _translation_ctx_t* ctx = (_translation_ctx_t*)ctx_;
    for (unsigned k = 0; k < 5; ++k) {                 // fully unrolled in the binary
        const _map_record_t* r = &ctx->records[k];
        if (((addr & r->mask) ^ r->ptr) == 0) {        // associative tag match
            uint32_t off = (uint32_t)addr & ~(uint32_t)r->mask;  // low offset only
            return (void*)(r->window + off);           // NX-local base + offset
        }
    }
    return /* miss path */ neuron_translate_miss(ctx, addr);  // §6
}
```

(HIGH/OBSERVED for the loop body and finalizer; the `for` is a faithful
re-rolling of five identical blocks.)

---

## 5. `_init_translate_ctx()` — how the five windows are written

`_init_translate_ctx` (`.text` @0x000, `translation.cpp:31`–`62`) is a one-shot
builder. It materialises five sign/shift constants, then stores them into the
record array, and finally derives two windows from the runtime
`data_scratch_map` / `hbm_scratch` globals. Key constants (HIGH/OBSERVED):

| Asm (init) | Value | Stored to | Meaning |
|---|---|---|---|
| `movi a4,-1; slli a4,a4,24` | `0xFF000000` | `records[0].mask` (+16), `records[1].mask` (+48) | top-byte tag mask |
| `movi a3,-1` (`0x..FFFFFFFF`) | mask_hi | `records[*].mask`+4 | full high-word mask |
| `movi a9,9; slli a4,a9,24` | `0x09000000` | `records[1].ptr`/`window` region | static aperture tag |
| `movi a5,7; slli a5,a5,24` | `0x07000000` | `records[0].window` (+8) | NX SBUF/aperture base |
| `movi a11,16 / const16 a11,0x218` | `0x218` (e.g.) | `records[2].ptr` (+64) | register-window tag |
| `s8i a6, a2, 160` (`translation.cpp:37`) | `0` | `next_alloc` (+160) | victim cursor reset |

The two **dynamic** windows are computed at `translation.cpp:58`–`62` from
loads of `data_scratch_map` (off 0x2c) and a second scratch global (off 0x28/0x40):

```
aa: movi.n a4, 16 ; const16 a4, 44 ; l32i.n a4, a4, 0   ; a4 = *data_scratch_map slot
b4: movi a7,-41   ; slli a7,a7,20                       ; a7 = 0xFD700000-style base delta
bc: const16 a6, 40; l32i.n a6, a6, 0                    ; a6 = hbm/scratch base
c4: add.n a7, a6, a7                                    ; window base = scratch + delta
d1: addi a4, a4, -1                                     ; limit-1 (size adjust)
d9: s32i a4, a2, 100 ; s32i a7, a2, 96                  ; records[3].{window?,…}
...: s32i a5,a2,144 / s8i… (records[4] dynamic mask/ptr from hbm_scratch)
```

So three windows are **statically baked** (a fixed register/SBUF aperture map
from `aws_neuron_isa_tpb_nx_map.h` and `regs.hpp`, which are file entries 5 and
6 in this object's DWARF line table — HIGH/OBSERVED), and two are **filled at
runtime** from the host-provided scratch descriptors (`data_scratch_map`,
`hbm_scratch`). (MED/INFERRED on which-record-is-which from the store-offset
sequence; the static/dynamic split itself is HIGH/OBSERVED.)

> **NOTE.** `_init_translate_ctx` is not in the `neuron_translate*` name family
> by mangling but is the table's constructor; it is invoked from the custom-op
> startup path (`start_exit.o` / `wrapper_api.o`), consistent with
> `start_exit.cpp:451` asserting
> `extended_isa::sdk::data_scratch_size >= sizeof(data_scratch_map_t)` before
> the scratch region is usable (HIGH/OBSERVED string in `start_exit.o`).

---

## 6. The miss path (`0x215`) — dynamic window allocation

When none of the five static/preloaded windows match (the `bnez` at 0x202),
control reaches `0x215`. This block does **not** abort — it *allocates* a new
mapping over the round-robin `next_alloc` victim and programs the SoC register
aperture so the address becomes reachable (HIGH/OBSERVED):

```
215: l8ui  a2, a3, 160        ; a2 = ctx->next_alloc
218: slli  a2, a2, 5          ; a2 = next_alloc * 32  (record byte offset)
21b: { srli a2, a11, 8 ; add a6, a3, a2 }   ; a6 = &records[next_alloc]
223: and   a2, a4, a2         ; compute the new tag = addr & alloc_mask
226: and   a4, a4, a12        ; a4 = addr & 0xFF000000-ish (window base/page)
229: { l32i a8, a6, 8 ; l32i a9, a6, 24 }   ; a8 = window base ; a9 = reg_location (MMIO!)
231: s32i.n a5, a6, 4         ; write back updated tag/window fields
233: { s32i a4, a6, 0 ; add a2, a8, a2 }    ; records[v].ptr = new tag ; a2 = base+off
23b: memw                     ; ordering barrier (before MMIO program)
23e: s32i.n a5, a9, 4         ; *(reg_location+4) = … program the SoC window register
240: s32i.n a4, a9, 0         ; *(reg_location+0) = window base → MMIO aperture
242: memw                     ; ordering barrier (after MMIO program)
245: const16 a5,0xaaab; l8ui a4,a3,160      ; reload next_alloc
250: addi.n a4, a4, 1         ; next_alloc + 1
252: muluh/srli/addx2/sub     ; next_alloc = (next_alloc+1) % 3   (mul-by-0xAAAB ÷3)
25e: s8i   a4, a3, 160        ; store wrapped next_alloc
261: retw.n                   ; return base + offset (now mapped)
```

> **QUIRK.** The miss path is a tiny software-managed TLB refill. It (a) picks
> the victim record via `next_alloc`, (b) recomputes its `ptr` tag from the
> faulting address, (c) writes the SoC window register through the record's
> `reg_location` MMIO pointer (the two `s32i …, a9, …` flanked by `memw`
> barriers), and (d) advances `next_alloc` **modulo 3** — the `muluh` by
> `0xAAAB` then `srli #1`, `addx2`, `sub` is the canonical "divide by 3"
> reciprocal sequence, so **only three records are reused as dynamic slots**
> while the other two stay pinned. This confirms the 5 windows split as
> *2 pinned + 3 round-robin* (HIGH/OBSERVED on the `%3`; the exact pinned set is
> MED/INFERRED).

> **GOTCHA.** Because the miss path *programs MMIO* (`reg_location` writes), a
> translate of a never-before-seen window has a side effect on SoC state and is
> ordered by two `memw` fences. A reimplementation must treat
> `neuron_translate` as **not pure** for cold addresses — the first touch of a
> new HBM region reconfigures a hardware window register.

---

## 7. `neuron_translate_mapping_lookup()` — the diagnostic / reverse form

Signature `PvyPyS0_` = `void neuron_translate_mapping_lookup(void* ctx, unsigned long long addr, unsigned long long* out_window, void* out_offset)`.
The body (`.text` @0x264) reuses the **same five-way unrolled match** as
`neuron_translate` (identical `and/xor/or/beqz` per window, HIGH/OBSERVED), but
on a hit it does not relocate the address — it **writes the decomposition out**:

```
325 (hit): s32i a9, a6, 0 ; … ; xor a2,a3,a2 ; xor a4,a8,a2   ; a2 = -1 (init)
            add a13,a9,a4 ; add a14,a10,a2                     ; out_window = window + tag(64-bit)
            s32i a13,a7,0 ; saltu a4,a13,a9 ; add a15,a14,a4   ; carry-correct the high word
            s32i a15,a7,4 ; movi a2,0 ; retw.n                 ; out fields written, return 0
```

On a total miss it stores `-1` (`movi.n a2,-1` at 0x321, returned to the
caller). So `mapping_lookup` is the **read-only / introspection** twin: same
associative match, but it yields `{matched window base, masked offset}` as
out-parameters and a status, and it **never** triggers the MMIO refill of §6.
It is what a debugger / coherency check uses to ask "which window does this HBM
address currently live in?" without perturbing hardware state (HIGH/OBSERVED on
the structure; MED/INFERRED on the precise out-field semantics from the two
`s32i a?, out, {0,4}` pairs and the `saltu` carry fixup).

---

## 8. The host counterpart (window producer)

`neuron_translate` only *reads* a table that something else *fills*. The filling
party is the host runtime in `libnrtucode_internal.so`. Its window machinery is
visible as embedded source-path / assertion strings (HIGH/OBSERVED, `strings`):

- `soc_window_manager.hpp`, `xt_window.hpp`, `xt_window_view`
- `get_window_addr`, `push_unallocated_window`

These are inlined (no exported `nm` symbol survives), but the names map cleanly
onto the device side: the host `soc_window_manager` allocates SoC apertures and
pushes their `{base, mask, reg_location}` descriptors into the `data_scratch_map`
/ `hbm_scratch` regions that `_init_translate_ctx` reads back (§5). `get_window_addr`
is the host-side analogue of the device `neuron_translate` finalizer; the device
function is the on-engine, latency-critical reader of those windows.

> **NOTE.** There is therefore no *symbol-level* mirror of `neuron_translate` in
> the host `.so`; the contract between host and device is the **memory layout**
> of `data_scratch_map_t` / the `_map_record` window descriptors, not a shared
> function. A Vision-Q7-compatible reimplementation must agree with the host on
> that byte layout, not on any ABI call.

---

## 9. Where it sits in the tensor-touch chain

`neuron_translate` is the leaf of the device pointer chain (CARRIED, shared
Part-7 pack; cross-checked against the callers in §1):

1. `ARG_TENSOR.storage.hbm.addr` carries the **untranslated 64-bit** SoC/HBM
   address (CARRIED).
2. `c10::Q7PtrType` (16 bytes: `hbm_addr@0`, `ctx_ptr@8`, `is_nullptr@0xC`,
   CARRIED) bundles that 64-bit address with the `_translation_ctx*` this page
   decodes. `Q7PtrType::ctx_ptr` is exactly the `void* ctx` argument
   `neuron_translate` expects.
3. `q7_data_ptr()` / `Q7Ptr::operator[]` call `neuron_translate(hbm_addr, ctx_ptr)`
   to obtain the 32-bit NX-local pointer the LX core dereferences (CARRIED;
   consistent with `data_transfer.o`/`wrapper_api.o` being the only direct
   importers, HIGH/OBSERVED).
4. The TCM staging path (`TensorTcmAccessor`/`data_transfer`) stages
   HBM↔TCM through the `[0x80000, 0x90000)` window (CARRIED) — itself one of
   the static apertures programmed into the table by `_init_translate_ctx`.

> **CROSS-LINK / CONSISTENCY.** This page is the **device-side consumer** of
> the SoC↔Q7 translation windows. The control-side **producer** view
> (`soc_window_manager`, aperture allocation policy, the SoC register map behind
> `reg_location`) belongs to
> `control/address/soc-q7-translation-windows.md`. *(That page is referenced in
> the book outline but not yet authored at the time of writing — forward
> reference.)* No divergence with the shared Part-7 pack was found; the only
> reconciliation needed is the §2 **CORRECTION** of the per-window field layout
> (`{ptr, window, mask, reg_location}`, no `limit`), which supersedes the
> base/limit/mask/soc-offset description in the task anchor.

### See also

- [Q7PtrType + Lazy Translation](q7ptrtype.md) — the 16-byte pointer that
  carries `ctx_ptr` into `neuron_translate`.
- [The Retargeted TensorAccessor](tensor-accessor.md) — the accessor leaf whose
  per-element dereference funnels here.
- [TensorStream / TCM staging](tensorstream-tcm.md) — the HBM↔TCM `[0x80000,
  0x90000)` staging window, one of the static apertures in this table.
- `control/address/soc-q7-translation-windows.md` — the host/SoC producer of
  these windows *(planned companion page)*.

---

### Anchor table (quick re-derivation)

| Claim | Anchor | Conf/Prov |
|---|---|---|
| `neuron_translate` @0x120, `_Z16neuron_translatePvy` | `nm translation.o`; objdump | HIGH/OBSERVED |
| `neuron_translate_ctx` @0x110 → `return _ctx;` | objdump + `.rela.text` `_ctx` | HIGH/OBSERVED |
| `_init_translate_ctx` @0x000 | objdump; `translation.cpp:31` | HIGH/OBSERVED |
| `neuron_translate_mapping_lookup` @0x264 | `nm`; objdump | HIGH/OBSERVED |
| ctx = 168B, `records[5]` + `next_alloc@160` | DWARF DIE 0x47, `translation.hpp:24` | HIGH/OBSERVED |
| record = 32B `{ptr@0,window@8,mask@16,reg_location@24}` | DWARF DIE 0x81, `translation.hpp:17` | HIGH/OBSERVED |
| `window` is `uint32_t` (4B); `ptr/mask/reg_location` `uint64_t` | DWARF type DIEs 0xd9/0xbb | HIGH/OBSERVED |
| match: `(addr & mask) == ptr` | objdump 0x13b–0x150 | HIGH/OBSERVED |
| return: `window + (addr & ~mask)` (32-bit) | objdump 0x204–0x213 | HIGH/OBSERVED |
| miss path programs MMIO via `reg_location`, `next_alloc % 3` | objdump 0x215–0x261 | HIGH/OBSERVED |
| host producer = `soc_window_manager.hpp`/`get_window_addr` | `strings libnrtucode_internal.so` | HIGH/OBSERVED |
| callers: `data_transfer.o`, `wrapper_api.o` | `nm` (undef refs) | HIGH/OBSERVED |
