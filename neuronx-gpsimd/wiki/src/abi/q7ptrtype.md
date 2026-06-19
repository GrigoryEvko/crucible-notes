# Q7PtrType + Lazy Translation

`c10::Q7PtrType` is the bottom of the device-tensor pointer chain. It is the
**16-byte value** that a GPSIMD custom op actually holds when it touches tensor
data, and it is the layer at which a 64-bit on-chip HBM address gets turned into
a CPU-dereferenceable, NX-local address — **lazily, per element access**.

The NX (Vision-Q7 / Xtensa) core is a 32-bit machine (`addr_size = 4`, `void*`
is 4 bytes) and **cannot directly address HBM**. A tensor's data therefore is
*not* a native pointer; it is the triple `{hbm_addr, ctx_ptr, is_nullptr}`. To
read element `i`, the core must map a *window* of HBM into its 32-bit address
space at access time. `Q7PtrType` carries the raw HBM address plus a cached
translation handle so that the per-access mapping primitive (`neuron_translate`)
needs no HBM round-trip to find its context.

This page documents the byte-exact struct, the owning `UniqueQ7Ptr`, and both
element operators, anchored to DWARF offsets and native Xtensa disassembly.

## Provenance and tooling

All claims here are derived **solely from static analysis** of the shipped
`aws-neuronx-gpsimd-customop-lib_0.21.2.0` package: the compiled static archive
`custom_op/neuron/libneuroncustomop.a` and its DWARF v4 debug info, emitted by
the same compiler that *defines* this ABI (`DW_AT_producer`:
`XtensaTools-14.09 clang version 10.0.1`). The objects are **ELF 32-bit LSB,
Tensilica Xtensa, not stripped**.

> NOTE — Native disassembly **was available** for this analysis. The Xtensa
> binutils ship in-tree under `tools/XtensaTools/bin/xtensa-elf-objdump`; the
> objdump only needs its core registered:
> ```
> export XTENSA_SYSTEM=.../gpsimd_tools/tools/ncore2gp/config
> export XTENSA_CORE=ncore2gp
> xtensa-elf-objdump -dr translation.o
> ```
> Every instruction-level citation below was produced with this `ncore2gp`
> (`Xm_ncore2gp`, `arch = Xtensa24`, uarch "Cairo", `TargetHWVersion = NX1.1.4`)
> configuration. Because these are **relocatable objects**, addresses are
> section-relative (`.text` offset 0 per member) and there is no VMA delta to
> reason about — DWARF offsets + section offsets are used throughout, never VMA
> arithmetic.

Relevant archive members: `translation.o`, `allocator.o`, `NeuronAllocator.o`,
`wrapper_api.o`, `TensorTcmAccessor.o`.

---

## 1. Struct layout — `c10::Q7PtrType` (16 bytes)

DWARF DIE `NeuronAllocator.o @0x149a` (an identical DIE appears independently in
`wrapper_api.o`; the two translation units agree on every offset and size):

```
DW_TAG_structure_type "Q7PtrType"
  DW_AT_calling_convention : 5  (DW_CC_pass_by_value)   <- passed/returned BY VALUE
  DW_AT_byte_size          : 16                          [OBSERVED, 0x10]
  decl: Q7PtrType.h:8
```

| off  | size | type       | name         | DWARF anchor | conf |
|------|------|------------|--------------|--------------|------|
| 0x00 | 8    | `uint64_t` | `hbm_addr`   | member @`0x14a3`, `DW_AT_data_member_location` 0; type `0x1669` → typedef `uint64_t` → `_ULonglong` → "long long unsigned int", byte_size 8 | HIGH · OBSERVED |
| 0x08 | 4    | `void*`    | `ctx_ptr`    | member @`0x14af`, location 8; type `0x1409` `DW_TAG_pointer_type` (no `DW_AT_type` → `void*`), `addr_size = 4` → 4 bytes | HIGH · OBSERVED |
| 0x0c | 1    | `bool`     | `is_nullptr` | member @`0x14bb`, location 12 (`0x0c`); type `0x140a` `DW_TAG_base_type` `bool`, `DW_ATE_boolean`, byte_size 1 | HIGH · OBSERVED |
| 0x0d | 3    | —          | *(tail pad)* | no DWARF member; `0x10 − (0x0c+1) = 3` to keep `uint64_t hbm_addr` 8-byte aligned in arrays / inside the `__compressed_pair` | HIGH · INFERRED (C ABI) |

Struct alignment **8** (max member alignment = `align(uint64_t)` = 8; no
`DW_AT_alignment` override on the DIE). No vtable, no base class: the DIE has no
`DW_AT_containing_type` and exactly three data members.

### Field semantics

- **`hbm_addr` (0x00, uint64).** The 64-bit SoC/HBM address of the tensor data.
  It is **not** a host pointer and **not** an NX-local 32-bit pointer — it is the
  on-chip High-Bandwidth-Memory address that must be window-mapped before the
  32-bit NX core can dereference it. `TensorTcmAccessor` consumes this field
  directly for bulk `neuron_memcpy`, independently confirming that offset 0x00 is
  the raw SoC address. `neuron_hbm_deallocate` (below) also frees by exactly this
  value.

- **`ctx_ptr` (0x08, void*).** A cached opaque translation-context handle: the
  return value of `neuron_translate_ctx()`. Stored once per pointer so that the
  per-access `neuron_translate()` need not re-fetch the context from HBM on every
  element. Internally it is a `_translation_ctx*` (the translation TU types the
  local that way; see §3a) but it is typed `void*` in the ABI.

- **`is_nullptr` (0x0c, bool).** Empty/null sentinel.

  > GOTCHA — `hbm_addr == 0` is a **valid** HBM address, so `0` cannot signal
  > null. A separate boolean is therefore mandatory. `std::unique_ptr` decides
  > whether to invoke the deleter by comparing the *managed pointer* against
  > `nullptr`; the free `operator==(Q7PtrType, nullptr_t)` (§3d) forwards that
  > comparison to `!is_nullptr`, **not** to `hbm_addr`. A `Q7PtrType` with
  > `hbm_addr == 0, is_nullptr == false` is a live pointer to HBM offset 0; a
  > `Q7PtrType` with any `hbm_addr` and `is_nullptr == true` is null.

---

## 2. Constructors

Four `DW_TAG_subprogram "Q7PtrType"` declaration DIEs sit inside the struct DIE
at `decl_line` 21–24 (`NeuronAllocator.o @0x14c7, 0x14d4, 0x14e6, 0x14f8`),
giving the four signatures:

| ctor | parameters (DWARF type) | effect |
|------|-------------------------|--------|
| `Q7PtrType()` | *(none + `this`)* | `is_nullptr = true`; `hbm_addr` **uninitialised** |
| `Q7PtrType(std::nullptr_t)` | param `0x13fd` (`nullptr_t`) | `is_nullptr = true`; `hbm_addr` **uninitialised** |
| `Q7PtrType(uint64_t)` | param `0x1669` (`uint64_t`) | `hbm_addr = ptr`; `is_nullptr = false` |
| `Q7PtrType(uint64_t, void*)` | params `0x1669` (`uint64_t`) + `0x1409` (`void*`) | `hbm_addr = ptr`; `ctx_ptr = ctx`; `is_nullptr = false` |

```c
/* The four Q7PtrType constructors. ctx_ptr defaults to the cached translation
   handle so that the first operator[] need not re-query it. */
Q7PtrType() {                  /* default */
    this->ctx_ptr    = neuron_translate_ctx();   /* cache handle once */
    this->is_nullptr = true;                     /* hbm_addr left indeterminate */
}
Q7PtrType(nullptr_t) {                            /* null */
    this->ctx_ptr    = neuron_translate_ctx();
    this->is_nullptr = true;
}
Q7PtrType(uint64_t ptr) {                          /* from raw HBM addr */
    this->hbm_addr   = ptr;
    this->ctx_ptr    = neuron_translate_ctx();
    this->is_nullptr = false;
}
Q7PtrType(uint64_t ptr, void *ctx) {               /* forward an already-resolved ctx */
    this->hbm_addr   = ptr;
    this->ctx_ptr    = ctx;                        /* used by operator+ (§3b) */
    this->is_nullptr = false;
}
```

> QUIRK — the default and `nullptr_t` ctors leave `hbm_addr` **indeterminate**;
> only `is_nullptr` is meaningful for a null `Q7PtrType`. This is harmless *only*
> because every consumer gates on `is_nullptr` before reading `hbm_addr`. The
> 2-arg ctor is the one `operator+` uses to carry an already-resolved `ctx_ptr`
> forward without re-querying it.

The `ctx_ptr`-caching claim is grounded by `neuron_translate_ctx` itself
(`translation.o`, 0xd bytes, native disasm):

```
00000110 <_Z20neuron_translate_ctxv>:
 110:  004136   entry  a1, 32
 113:  000024   const16 a2, 0          ; reloc R_XTENSA_SLOT0_ALT _ctx
 116:  000024   const16 a2, 0          ; reloc R_XTENSA_SLOT0_OP  _ctx
 119:  0228     l32i.n  a2, a2, 0       ; a2 = *_ctx  (load the global handle)
 11b:  f01d     retw.n                  ; return a2
```

i.e. `neuron_translate_ctx()` returns the global `_ctx` (reloc-confirmed) — the
handle that the constructors copy into `ctx_ptr@0x08`.

---

## 3. Operator semantics

> EMISSION FACT [OBSERVED] — `operator[]<T>` and `operator+<T>` are **header-only
> function templates**, inlined at each customer compile site. They are **never**
> emitted as out-of-line machine code in `libneuroncustomop.a`:
> ```
> nm libneuroncustomop.a | rg -c 'Q7PtrTypeix'    # operator[] text symbols -> 0
> nm libneuroncustomop.a | rg -c 'Q7PtrTypeplE'   # operator+  text symbols -> 0
> ```
> `wrapper_api.o`'s DWARF *does* carry **7 declaration DIEs** for instantiated
> `operator[]<T>` variants (the TU named them in its type info) but **no bodies**.
> No `operator+` DIE exists anywhere. Operator bodies are therefore documented
> from the header source, cross-checked against the *real, compiled, natively
> disassembled* signatures of the runtime functions they call
> (`neuron_translate`, `neuron_hbm_deallocate`), which **are** present.

### 3a. `T& operator[]<T>(size_t idx) const` — the lazy translating deref

```c
/* LAZY TRANSLATING dereference: maps an HBM window covering this element,
   then reads through the returned NX-local address. */
template <typename T>
T& Q7PtrType::operator[](size_t idx) const {
    /* hbm_addr is the 64-bit SoC base; advance by element stride in BYTES */
    void *host_visible = neuron_translate(this->ctx_ptr,
                                          this->hbm_addr + sizeof(T) * idx);
    /* host_visible is a 32-bit NX-local address valid until the NEXT
       neuron_translate() call (the window may be remapped). */
    return ((T*)host_visible)[0];
}
```

Every element access calls `neuron_translate(ctx, hbm_addr + sizeof(T)*idx)`,
which maps a translation window covering that SoC address into NX address space
and returns the corresponding 32-bit NX-local address; that is cast to `T*` and
dereferenced.

> GOTCHA — the mapping is valid **only until the next `neuron_translate()`
> call**. References returned by `operator[]` are *transient* — each access
> remaps the window. Do not cache a `T&` from `[]` across another `[]`.

**DWARF-confirmed instantiations** (`wrapper_api.o`, 7 declaration DIEs; mangling
`_ZNK3c109Q7PtrTypeix<T>ERT_j` = `const`, returns `T&`, param `j` = `unsigned int`
= `size_t`):

| linkage name | mangled `T` | C type |
|--------------|-------------|--------|
| `...ixIxEERT_j` | `x` | `long long` |
| `...ixIaEERT_j` | `a` | `signed char` |
| `...ixItEERT_j` | `t` | `unsigned short` |
| `...ixIsEERT_j` | `s` | `short` |
| `...ixIfEERT_j` | `f` | `float` |
| `...ixIhEERT_j` | `h` | `unsigned char` |
| `...ixIiEERT_j` | `i` | `int` |

**`neuron_translate` grounding** — `translation.o`, DWARF subprogram
`@0x99f` (`decl_line 85`) and native disasm (`_Z16neuron_translatePvy`):

```
DW_TAG_subprogram  _Z16neuron_translatePvy  "neuron_translate"
  return  : 0x149  void*               (the NX-local mapped address)
  param   : ctx_ptr  0x149  void*       (cast internally to _translation_ctx*)
  param   : ptr      0x0bb  uint64_t    (the 64-bit SoC address)
  local   : ctx      0x037  _translation_ctx*   (typedef _translation_ctx_t, byte_size 168, decl_line 28)
  local   : result   0x0d9  uint32_t            (the 32-bit NX address, then -> void*)
```

The body is a **window-table walk** over the `_translation_ctx` (entries at
context offsets +16, +48, +80, +112, +144 — five 32-byte window descriptors,
consistent with the 168-byte `_translation_ctx_t`). Each iteration masks the
incoming SoC `addr` and compares it against a window's masked **tag** `ptr`
(`and`/`xor`/`or` then `beqz` on match) — i.e. the predicate is
`(addr & mask) == ptr`, with `ptr` the comparison tag (never an addend). On a
hit it returns `window + (addr & ~mask)`, where `window` (record `+8`, a
`uint32_t`) is the NX-local base and `~mask` selects the in-window offset:

```
00000120 <_Z16neuron_translatePvy>:
 120:  entry a1, 32
 123:  { l32i a2,a2,16 ; l32i a6,a2,20 ; mov.a a3,a2 }   ; load records[0].mask lo/hi (ctx+16), ctx in a3
 133:  { l32i a7,a3,0  ; l32i a8,a3,4 }                  ; records[0].ptr (tag) lo/hi (ctx+0)
 13b:  and a6,a6,a5 ; and a9,a2,a4                       ; (addr & mask)
 143:  xor a7,a9,a7 ; xor a15,a6,a8                      ; compare to tag ptr
 14b:  or  a7,a7,a15                                     ; ==0  <=>  (addr & mask) == ptr
 150:  beqz a7, 204 <...+0xe4>                           ; match -> resolve  (reloc .text+0x204)
 153:  ... window[1] at +48/+32 ...  movi.n a6,1  beqz ... 0x204
 17b:  ... window[2] at +80/+64 ...  movi a6,2    beqz ... 0x204
 1a4:  ... window[3] at +112/+96 ... movi a6,3    beqz ... 0x204
 1d7:  ... window[4] at +144/+128 .. movi a6,4    bnez ... 0x215
00000204 <...+0xe4>:                                     ; matched window 'a6' -> compute NX addr
 204:  slli a5,a6,5      ; entry = a6 * 32
 207:  add.n a3,a3,a5    ; &ctx.records[a6]
 209:  and a2,a4,a2
 20c:  l32i.n a3,a3,8    ; a3 = records[a6].window (NX-local base, +8)
 20e:  xor a2,a4,a2      ; a2 = addr & ~mask
 211:  add.n a2,a3,a2    ; result = window + (addr & ~mask)
 213:  retw.n            ; return void* (32-bit NX address)
```

The `.rela.text` relocations confirm it programs a hardware scratch / window-
mapping facility: `data_scratch_map`, `_sbuf_window`,
`extended_isa::sdk::hbm_scratch` (`_ZN12extended_isa3sdk11hbm_scratchE`), and the
`_ctx` global. The miss path (`+0xf5` / `0x215`) allocates a *new* scratch window
(`l8ui a2,a3,160` reads a free-slot index, programs `hbm_scratch`, issues `memw`
barriers). This is the full behaviour [neuron-translate-windows.md](neuron-translate-windows.md) documents;
here it is cited only as the translation primitive that `operator[]` invokes.

> **CORRECTION — the window record is `{ptr@0, window@8, mask@16, reg_location@24}`,
> not `{base, mask}`.** An earlier framing of this disassembly called the `+0`
> field a "base" and gave the result as `base + (ptr & ~mask)`. The DWARF-pinned
> layout (see [neuron-translate-windows.md](neuron-translate-windows.md) §2) is a
> *masked-tag compare*: `ptr` (`+0`) is the comparison **tag** (the value
> `(addr & mask)` must equal, never an addend), and the NX-local base is the
> `uint32_t` **`window`** field (`+8`). The returned address is therefore
> **`window + (addr & ~mask)`** (the `l32i.n a3,a3,8` at `0x20c` loads `window`).
> `[HIGH × OBSERVED]`

> The lazy model in one sentence: **`Q7PtrType` stores the HBM device address;
> translation to a CPU-dereferenceable NX-local address happens only at access
> time, inside `operator[]`, via `neuron_translate`.**

### 3b. `Q7PtrType operator+<T>(size_t n) const` — pure pointer arithmetic

```c
/* POINTER ARITHMETIC ONLY: advance the SoC address; do NOT touch HBM and
   do NOT translate. The new Q7PtrType carries the SAME (already-resolved)
   ctx_ptr forward via the 2-arg ctor, so a later operator[] reuses it. */
template <typename T>
Q7PtrType Q7PtrType::operator+(size_t n) const {
    uint64_t result = this->hbm_addr + sizeof(T) * n;   /* 64-bit add, in bytes */
    return Q7PtrType(result, this->ctx_ptr);            /* is_nullptr = false  */
    /* NO neuron_translate call, NO window mapped, NO memory accessed. */
}
```

**Contrast with `operator[]`:** `operator+` performs the *identical address
arithmetic* (`hbm_addr + sizeof(T)*n`) but **stops there** — it never calls
`neuron_translate`, never maps a window, never dereferences HBM. It returns a new
`Q7PtrType` that *describes* the advanced location. You must use `operator[]`
(or `*` on a `unique_ptr`, which forwards to `[0]`) to actually touch memory.

> CORRECTION / clarification to SX-ABI-01 §3b confidence — the report tags
> `operator+` semantics MED "(no binary echo)". That is correct *and verifiable*:
> a native search confirms **zero** `operator+` text symbols
> (`nm | rg -c 'Q7PtrTypeplE'` → 0) **and zero** `operator+` DWARF DIEs in any
> shipped object (`rg -c 'Q7PtrTypeplE' *.dwarf` → 0). It was never instantiated
> by vendor code, so its codegen cannot be byte-confirmed; the body above is from
> header source. The *absence* itself is a positive, binary-grounded fact:
> nothing in the shipped library exercises `operator+`, whereas `operator[]` has
> 7 instantiation DIEs — strong evidence that the lazy model in practice is
> "advance via `+` is rare; translate via `[]` at the leaf".

### 3c. `operator bool() const`

```c
bool Q7PtrType::operator bool() const { return !this->is_nullptr; }
```

DWARF: subprogram `_ZNK3c109Q7PtrTypecvbEv` (`NeuronAllocator.o @0x150f`),
returns `bool` (type `0x140a`), `decl_line 36`, `const this` (param `0x1691`).
[OBSERVED decl]

### 3d. Free comparison operators (`Q7PtrType.h:42-53`)

```c
bool operator==(Q7PtrType qp, nullptr_t) { return !(bool)qp; }   /* == is_nullptr   */
bool operator==(nullptr_t, Q7PtrType qp) { return !(bool)qp; }
bool operator!=(Q7PtrType qp, nullptr_t) { return  (bool)qp; }   /* == !is_nullptr  */
bool operator!=(nullptr_t, Q7PtrType qp) { return  (bool)qp; }
```

> Purpose — `std::unique_ptr` requires its *managed pointer type* be comparable
> to `nullptr` (to decide whether to call the deleter on destruction / reset).
> These overloads route that check through `is_nullptr` (§3c → `operator bool`),
> **not** through `hbm_addr`. This is the mechanism that makes a by-value
> `Q7PtrType` usable as `unique_ptr::pointer`.

---

## 4. `UniqueQ7Ptr` — `unique_ptr<Q7PtrType, Q7Deleter>` (24 bytes)

```cpp
using UniqueQ7Ptr = std::unique_ptr<Q7PtrType, Q7Deleter>;   // Q7PtrType.h:74
```

DWARF (`NeuronAllocator.o`):

```
DW_TAG_class_type "unique_ptr<c10::Q7PtrType, c10::Q7Deleter>"
  DW_AT_byte_size : 24                                       [OBSERVED, 0x18]
  member __ptr_ @ DW_AT_data_member_location 0  -> type 0x22b

0x22b: DW_TAG_class_type "__compressed_pair<c10::Q7PtrType, c10::Q7Deleter>"
  DW_AT_byte_size : 24
  DW_TAG_inheritance -> 0x2c8  @ location 0     (the Q7PtrType holder)
  DW_TAG_inheritance -> 0x37c  @ location 16    (the Q7Deleter holder)

0x2c8: "__compressed_pair_elem<c10::Q7PtrType, 0, false>"
  DW_AT_byte_size : 16 ; member __value_ -> 0x149a (Q7PtrType)
0x37c: "__compressed_pair_elem<c10::Q7Deleter, 1, false>"   <- NOTE: is_empty = FALSE
  DW_AT_byte_size : 4  ; member __value_ -> 0x1525 (Q7Deleter), location 16
```

### Byte-exact layout

| off  | size | content | DWARF |
|------|------|---------|-------|
| 0x00 | 16   | `Q7PtrType __value_` (the managed pointer, **by value**) | elem0 `0x2c8`, byte_size 16, `__value_` @0 → `0x149a` |
| 0x10 | 4    | `Q7Deleter __value_` (the 4-byte function-pointer deleter) | elem1 `0x37c`, byte_size 4, `__value_` @16 → `0x1525` |
| 0x14 | 4    | *(tail pad)* to round to 24 / 8-byte alignment | INFERRED (C ABI); `0x18 − (0x10 + 4) = 4` |

**24-byte derivation:** `16` (Q7PtrType by value) `+ 4` (Q7Deleter fn ptr @0x10)
`+ 4` (tail padding @0x14) `= 24 = 0x18`.

### Why the pointer is stored BY VALUE (not as `Q7PtrType*`)

`Q7Deleter::pointer` is `typedef`'d to `Q7PtrType` (not `Q7PtrType*`). When a
deleter exposes a `pointer` typedef, `std::unique_ptr` uses **that** as its
managed-handle type, so `__ptr_` holds a `Q7PtrType` *value*, not a native
pointer to one. Nullness is then decided not by `__ptr_ == nullptr` in the
pointer-comparison sense but by the free `operator==(Q7PtrType, nullptr_t)` (§3d)
→ `is_nullptr`. This is exactly why `is_nullptr@0x0c` must exist: a 32-bit native
null-pointer test would be meaningless against a 16-byte device-address value.

> CORRECTION to SX-ABI-01 §4 — the report states the deleter element is
> `__compressed_pair_elem<Q7Deleter, 1, **true**>` (empty-base-optimised) and
> calls the 24th-byte region "tail-padded for 8-byte alignment". The DWARF
> disagrees on the EBO flag: the element is
> `__compressed_pair_elem<c10::Q7Deleter, 1, **false**>` with `DW_AT_byte_size 4`
> and a **real, stored** `__value_` member (`Q7Deleter`) at `location 16`.
> `Q7Deleter` is **not empty** — it carries a 4-byte `deleter_` function pointer
> (§5) — so the third template arg is `false` and the deleter occupies bytes
> 0x10..0x13 as a concrete member, **not** EBO'd away. The final size and field
> placements the report gives (24 bytes; Q7PtrType@0, deleter fn ptr@16) are
> nonetheless correct; only the "EBO / empty (`true`)" characterisation is wrong.
> The remaining padding is bytes **0x14..0x17** (4 bytes), as in the table above.

---

## 5. `Q7Deleter` and the two deleter variants

DWARF (`NeuronAllocator.o @0x1525`, identical in `wrapper_api.o`):

```
DW_TAG_structure_type "Q7Deleter"   DW_AT_byte_size : 4   decl_line 57
  member deleter_ @ location 0  -> 0x158c  (typedef Q7DeleterFnPtr -> 0x16b4 fn ptr)
using Q7DeleterFnPtr = void (*)(Q7PtrType);   // Q7PtrType.h:55  (4-byte fn ptr)
```

```c
struct Q7Deleter {                                  /* 4 bytes: one fn ptr */
    Q7DeleterFnPtr deleter_;                        /* @0x00 */
    using pointer = Q7PtrType;                       /* makes unique_ptr store BY VALUE */
    Q7Deleter()                : deleter_(&delete_nothing) {}
    Q7Deleter(Q7DeleterFnPtr d): deleter_(d)         {}
    void operator()(Q7PtrType p) const { deleter_(p); }   /* _ZN3c109Q7DeleterclENS_9Q7PtrTypeE */
};
```

### Variant 1 — input tensors: do-nothing

```c
static void Q7Deleter::delete_nothing(Q7PtrType) {}   /* Q7PtrType.h:71 */
```

Used when the `Q7PtrType` points to pre-existing data the custom op does not own
(e.g. ATen input tensors); destroying the `unique_ptr` must **not** free HBM.

**Native disasm [OBSERVED]** — `wrapper_api.o`, section
`.text._ZN3c109Q7Deleter14delete_nothingENS_9Q7PtrTypeE`, **size 5 bytes**
(`objdump -h` reports `0x05`):

```
00000000 <_ZN3c109Q7Deleter14delete_nothingENS_9Q7PtrTypeE>:
   0:  004136   entry  a1, 32      ; windowed-ABI prologue, 32-byte frame  (bytes 36 41 00)
   3:  f01d     retw.n             ; windowed narrow return                 (bytes 1d f0)
```

Prologue + immediate return — a genuine no-op body. CONFIRMED.

### Variant 2 — allocations: free HBM

```c
/* NeuronAllocator.h: the allocator hands out an owning UniqueQ7Ptr whose
   deleter forwards hbm_addr to the HBM free. */
UniqueQ7Ptr NeuronAllocator::allocate(size_t nbytes) const {
    uint64_t ptr; int ret = neuron_hbm_allocate(nbytes, &ptr, /*align*/);
    assert(ret == 0);
    return UniqueQ7Ptr(ptr, Q7Deleter(&delete_ptr));   /* deleter carries delete_ptr */
}
static void NeuronAllocator::delete_ptr(Q7PtrType q7) {
    neuron_hbm_deallocate(q7.hbm_addr);                 /* free by 64-bit SoC addr (offset 0x00) */
}
```

`delete_ptr` / `allocate` are header-inline (only the singleton accessor
`c10::GetNeuronAllocator()` is emitted out-of-line in `NeuronAllocator.o`), so
they are documented from the header and grounded by their compiled callee:

**`neuron_hbm_deallocate` grounding [OBSERVED]** — `allocator.o`, DWARF
`int neuron_hbm_deallocate(uint64_t)` (`allocator.cpp:132`), native disasm
`_Z21neuron_hbm_deallocatey`:

```
0000010c <_Z21neuron_hbm_deallocatey>:
 10c:  entry a1, 32
 10f:  const16 a3,0 ...   l32i.n a3,a3,0      ; load a base/limit global
 118:  const16 a4,0 ...   l32i.n a4,a4,0
 122:  sub a2,a2,a3                            ; q7.hbm_addr (a2/a3 pair) - base
 12a:  add a11,a4,a2                           ; arg for the free routine
 132:  const16 a2,0 ; callx8 a2               ; call the underlying deallocator
```

It takes the 64-bit SoC address — exactly the value `delete_ptr` passes as
`q7.hbm_addr` — and frees it. Paired with `neuron_hbm_allocate(size_t,uint64_t*,size_t)`
(`allocator.cpp:118`), whose `uint64_t*` out-param fills the SoC address that
`allocate()` wraps into a `Q7PtrType`.

> The deleter dispatch (which of `delete_nothing` / `delete_ptr` runs) is gated
> by `unique_ptr`'s `__ptr_ == nullptr` test, which (per §3d / §4) is the
> `is_nullptr@0x0c` flag — **not** `hbm_addr`, because `hbm_addr == 0` is legal.

---

## 6. ABI role — how this marshals ATen ↔ GPSIMD

- A tensor's data is represented purely by `{hbm_addr, ctx_ptr, is_nullptr}` — a
  **16-byte value, not a native pointer**. `NeuronStorageImpl` owns one
  `UniqueQ7Ptr`; `NeuronTensorImpl` holds an
  `intrusive_ptr<NeuronStorageImpl>` and exposes `q7_data_ptr()` returning a
  `Q7PtrType` **by value**. See [the tensor object chain](tensor-object-chain.md)
  for the full chain above this leaf.
- The host / ATen side hands the kernel a 64-bit HBM address. The 32-bit NX core
  reaches the data only by **window translation**. `operator[]` performs that
  translation lazily per element (§3a); `TensorTcmAccessor` instead bulk-copies
  via `neuron_memcpy` using `hbm_addr` directly — both consume offset 0x00 as the
  raw SoC address. See [the tensor accessor](tensor-accessor.md).
- Ownership: input tensors → `Q7Deleter(delete_nothing)`; allocator output →
  `Q7Deleter(delete_ptr → neuron_hbm_deallocate)`.

---

## 7. Confidence ledger

**HIGH · OBSERVED** (DWARF emitted by the ABI's own compiler, two TUs agree;
plus native `ncore2gp` disasm):
- `Q7PtrType` byte_size 16; `hbm_addr@0x00`, `ctx_ptr@0x08`, `is_nullptr@0x0c`;
  base sizes `uint64_t` = 8, `void*` = 4, `bool` = 1.
- `UniqueQ7Ptr` = 24 (`__compressed_pair`): `Q7PtrType`@0 (16) + `Q7Deleter`@16
  (4) + pad@0x14 (4); deleter element `<...,1,false>` byte_size 4 (**not** EBO).
- `Q7Deleter` byte_size 4, `deleter_`@0; 7 `operator[]<T>` instantiation DIEs.
- `delete_nothing` = 5-byte `entry` + `retw.n` no-op (section size 0x05, disasm).
- `neuron_translate(void*,uint64_t)->void*` (window-table walk, `uint32_t result`,
  `_translation_ctx*` local, `_ctx` / `_sbuf_window` / `hbm_scratch` relocs).
- `neuron_translate_ctx()->void*` returns global `_ctx` (reloc + disasm).
- `neuron_hbm_deallocate(uint64_t)->int` frees by `hbm_addr` (disasm).

**HIGH · SOURCE** (header text; callee runtime signature independently OBSERVED):
`operator[]` body = `neuron_translate` then deref; deleter dispatch logic; the
four constructors' field initialisation.

**INFERRED (C ABI, no contradicting evidence):** 3 bytes tail pad in `Q7PtrType`
@0x0d; 4 bytes tail pad in `UniqueQ7Ptr` @0x14; struct alignment 8.

**MED:** `operator+<T>` semantics — header-only and **never instantiated/emitted**
(0 text symbols, 0 DWARF DIEs), so no binary corroboration of its codegen; the
absence itself is binary-grounded.

**LOW (noted):** default / `nullptr` ctors leave `hbm_addr` indeterminate — only
`is_nullptr` is valid for a null `Q7PtrType`.

---

## See also

- [Device-tensor object chain](tensor-object-chain.md) — `NeuronTensorImpl` →
  `NeuronStorageImpl` → `UniqueQ7Ptr`, above this leaf.
- [Tensor accessor](tensor-accessor.md) — `TensorTcmAccessor` bulk-copy path that
  uses `hbm_addr` directly instead of per-element `neuron_translate`.
- [neuron_translate windows](neuron-translate-windows.md) — full behaviour of the
  window-mapping primitive `operator[]` invokes.
