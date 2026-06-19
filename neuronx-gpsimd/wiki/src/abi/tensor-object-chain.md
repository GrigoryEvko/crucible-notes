# The at::Tensor Object Chain

> **Scope.** This page documents the exact C++ object chain by which a PyTorch `at::Tensor`
> carries **GPSIMD / Q7 device metadata** down to HBM in the `aws-neuronx-gpsimd-customop-lib`
> custom-op ABI. It owns three layouts — **`c10::NeuronTensorImpl` (216 B)**,
> **`c10::NeuronStorageImpl` (48 B)**, and the `q7_data_ptr()` accessor — plus the
> `make_intrusive` producers, the CPU-key + disabled-storage model, and the customised
> `at::TensorBase` top of the chain. The bottom of the chain (`Q7PtrType` 16 B, `UniqueQ7Ptr`
> 24 B) is owned by [Q7PtrType + Lazy Translation](q7ptrtype.md); the embedded
> `CoherencyEnforcer` is owned by [The CoherencyEnforcer](coherency-enforcer.md); both are
> re-grounded here only where they touch this chain.
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read from DWARF /
> symtab / relocs / native disasm), `INFERRED` (a C-ABI rule applied to an observed
> offset), `SOURCE` (modified ATen header text, with the emitted symbol / reloc / DWARF
> independently confirming it).

> **GOTCHA — this is a 32-bit target; do not transpose 64-bit-host offsets.** The shipped
> objects are **ELF32 Tensilica Xtensa** (`ncore2gp` core), `addr_size = 4`. A pointer and
> `size_t` are **4 bytes**, so every `c10::intrusive_ptr` is a single 4-byte word — **not 8**.
> The well-known 64-bit-host `sizeof(c10::TensorImpl)` figures **do not apply** here; this
> build's `TensorImpl` is 208 B (§2). All offsets below are 32-bit-target offsets. `[HIGH ×
> OBSERVED]`

> **NOTE — artifacts & tooling.** Layouts are read from **DWARF v4** emitted by the ABI's
> own compiler (`XtensaTools-14.09 clang 10.0.1`, `DW_AT_producer`) in
> `libneuroncustomop.a(wrapper_api.o)` — the only TU that emits full definitions of the
> `Neuron*` types (it instantiates the `make<…>` producers). The `libc10.a` base objects
> (`TensorImpl.cpp.o` / `StorageImpl.cpp.o` / `SymInt.cpp.o`) carry **zero** `.debug_*`
> sections, so the authoritative DWARF for the whole chain — including the size of the
> `TensorImpl` base — lives in `wrapper_api.o`. Sizes are `DW_AT_byte_size`; offsets are
> `DW_AT_data_member_location`; both cross-checked against ELF relocs and **native** Xtensa
> disassembly (`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`; the benign "TIE checksum does
> not match" warning does not affect decode). `extracted/` is gitignored — reach it with an
> absolute path or `fd --no-ignore`. Counts are grounded with `nm <obj> | rg -c`, never a
> decompile. All prose is derived from binary/static analysis alone (lawful interoperability
> RE, DMCA 17 U.S.C. 1201(f)).

---

## 0. Headline — a Neuron tensor is a CPU tensor with its data pointer kidnapped

`[HIGH × OBSERVED]` A GPSIMD `at::Tensor` is, to the entire ATen framework, an ordinary
**CPU** tensor: shape, stride, dtype, device, dispatch keys, contiguity and numel all live
in an **unmodified `c10::TensorImpl` base** and are served by the **stock** `TensorImpl`
virtuals (the override vtable in §6 touches only the 3 dtor/release slots). What is
retargeted is **only the data pointer**, and **out-of-band**: a single extra 4-byte member
`neuron_storage_impl_` at `+0xD0` points at a `NeuronStorageImpl`, whose `data_ptr_` is a
`UniqueQ7Ptr` embedding a **16-byte `Q7PtrType` value** `{hbm_addr, ctx_ptr, is_nullptr}` —
a **64-bit HBM SoC address** plus a cached translation context, *not* a native 32-bit
pointer. Simultaneously the stock `TensorImpl::storage_` is **poisoned** (any access
throws), so the only correct way to reach the data is `q7_data_ptr()` /
[the TCM accessor](tensor-accessor.md). The chain in one line:

```
tensor.impl_->neuron_storage_impl_->data_ptr_.get()  →  Q7PtrType{hbm_addr, ctx_ptr, is_nullptr}
```

---

## 1. The full chain (top to bottom)

`[HIGH × OBSERVED]` Every size/offset is from `wrapper_api.o` DWARF; the chain arithmetic
closes exactly at all three levels.

```
at::Tensor                          byte_size 0x14 (20); : at::TensorBase, adds no members
  └ at::TensorBase                  byte_size 0x14 (20)              DIE @0x940d
      ├ impl_              @0x00     intrusive_ptr<TensorImpl,UndefinedTensorImpl>  (4B)  DIE @0x303c
      │    └ target_ : TensorImpl*  ──► points AT the NeuronTensorImpl object
      │                                  (TensorImpl base @0x00 → value-preserving upcast)
      └ coherency_enforcer_ @0x04   at::CoherencyEnforcer (0x10)     DIE @0x9f0b  [→ coherency page]

c10::NeuronTensorImpl               byte_size 0xD8 (216)            DIE @0x0e45
  ├ [base c10::TensorImpl] @0x00    0xD0 = 208 B; holds sizes/strides/TypeMeta/device/keys,
  │                                  stock storage_ DISABLED (throws), autograd/named meta…
  ├ neuron_storage_impl_   @0xD0    intrusive_ptr<NeuronStorageImpl> (4B)  ← the ONLY added field
  └ (tail pad)             @0xD4    4 B  → rounds 0xD4 up to the 8-aligned 0xD8

c10::NeuronStorageImpl              byte_size 0x30 (48)             DIE @0x1472
  ├ [base intrusive_ptr_target] @0x00   0x0C real + 4 pad; vptr@0/refcount_@4/weakcount_@8
  ├ data_ptr_              @0x10    UniqueQ7Ptr (0x18 = 24 B)        DIE @0x1690 → unique_ptr @0xa2b7
  │    └ __compressed_pair<Q7PtrType, Q7Deleter>
  │         ├ Q7PtrType    @0x10    16 B, BY VALUE                  DIE @0x169b   [→ q7ptrtype page]
  │         │    ├ hbm_addr   @0x10  uint64_t   ◄── the 64-bit GPSIMD/HBM address
  │         │    ├ ctx_ptr    @0x18  void*      (cached _translation_ctx)
  │         │    └ is_nullptr @0x1C  bool
  │         └ Q7Deleter   @0x20    4-byte fn ptr (delete_nothing | delete_ptr)
  └ size_bytes_           @0x28    c10::SymInt (0x08; packed int64, top bit = IS_SYM)  DIE @0x188d
```

```
Tensor:        impl_(4)@0 + CoherencyEnforcer(0x10)@4                       = 0x14 ✓
NeuronTensor:  TensorImpl(0xD0)@0 + storage_ipt(4)@0xD0 + pad(4)            = 0xD8 ✓
NeuronStorage: ipt_target(0x10)@0 + UniqueQ7Ptr(0x18)@0x10 + SymInt(8)@0x28 = 0x30 ✓
```

The net data of an `at::Tensor` is therefore a **16-byte VALUE**, reached as
`tensor.impl_->neuron_storage_impl_->data_ptr_.get()`. It is **not** a native pointer and is
**not** stored in `TensorImpl::storage_` / `data_ptr_`.

---

## 2. `c10::NeuronTensorImpl` — 216 bytes, derives from `c10::TensorImpl`

`[HIGH × OBSERVED]` DWARF DIE `wrapper_api.o @0x00000e45`:

```
DW_TAG_structure_type "NeuronTensorImpl"
  DW_AT_containing_type  (0x000015c5 "c10::intrusive_ptr_target")   ← via the TensorImpl base
  DW_AT_calling_convention (DW_CC_pass_by_reference)
  DW_AT_byte_size        (0xd8)                                     == 216 bytes   [OBSERVED]
  DW_TAG_inheritance     type c10::TensorImpl, data_member_location 0x00
  DW_TAG_member "neuron_storage_impl_"  type @0x11ca
                         intrusive_ptr<NeuronStorageImpl, …>  data_member_location 0xD0, PRIVATE
```

| off  | size | member / base            | type                                       | conf | source |
|------|------|--------------------------|--------------------------------------------|------|--------|
| 0x00 | 0xD0 | (base) `c10::TensorImpl` | `c10::TensorImpl`                          | HIGH | OBSERVED `DW_TAG_inheritance @0x00`; 208 B, size pinned by the 0xD0 placement of the next member (note A) |
| 0xD0 | 0x04 | `neuron_storage_impl_`   | `intrusive_ptr<NeuronStorageImpl,…>` (4 B) | HIGH | OBSERVED `DW_AT_data_member_location(0xD0)`; member-type DIE @0x11ca `byte_size 0x04`, `DW_ACCESS_private` |
| 0xD4 | 0x04 | (tail padding)           | —                                          | HIGH | INFERRED (C ABI): `0xD8 − (0xD0+4) = 4`; 8-byte struct alignment inherited from `TensorImpl`'s `uint64`/`SymInt` members |

`neuron_storage_impl_` is the **only data member** `NeuronTensorImpl` adds over its base — a
single 4-byte `intrusive_ptr` target, so multiple tensors (views/aliases) can refcount-share
one HBM allocation (§8).

> **NOTE A — how the 208-byte `TensorImpl` base size is established.** `c10::TensorImpl`
> appears in `wrapper_api.o` only as a forward declaration (`DW_AT_declaration true`, no
> `byte_size`), and `libc10.a` carries no debug info — so `TensorImpl`'s member layout is not
> directly in any DWARF here. But the inheritance DIE places `neuron_storage_impl_` at exactly
> `0xD0`, with `TensorImpl` the sole base at `0x00`. A derived-class member offset is an
> authoritative aligned bound on base size, and `0xD0` is 8-aligned, so in **this** build
> `sizeof(c10::TensorImpl) = 0xD0 = 208`. `[HIGH × INFERRED-from-OBSERVED]` This is a 32-bit
> `TensorImpl`; the 64-bit-host figures are irrelevant.

**Methods (OBSERVED DWARF DIEs + emitted symbols):**

- Two public ctors — `(intrusive_ptr<NeuronStorageImpl>, DispatchKeySet, TypeMeta)` and
  `(…, DispatchKey, TypeMeta)`; the `DispatchKey` form **delegates** to the `DispatchKeySet`
  form. The emitted out-of-line ctor symbol is the `DispatchKey` variant
  `_ZN3c1016NeuronTensorImplC2E…11DispatchKey…` (weak/COMDAT, `.text…` size **0x181**).
- `q7_data_ptr() const → Q7PtrType` (`_ZNK3c1016NeuronTensorImpl11q7_data_ptrEv`); DWARF
  subprogram DIE `@0x0e9d`, `DW_AT_type (0x0000169b "c10::Q7PtrType")` — the return type is
  the struct itself, **not** a pointer (§4). Inline; no out-of-line text symbol in this TU.
- `virtual ~NeuronTensorImpl` (`DW_VIRTUALITY_virtual`, vtable slot 0).

Emitted (weak `W` / vague-linkage `V`): `C2` ctor, `D0`/`D2` dtors, `_ZTI`/`_ZTS`/`_ZTV`.
Grounded: `nm wrapper_api.o | rg -c NeuronTensorImpl` = **7**.

---

## 3. `c10::NeuronStorageImpl` — 48 bytes, derives from `c10::intrusive_ptr_target`

`[HIGH × OBSERVED]` DWARF DIE `wrapper_api.o @0x00001472`:

```
DW_TAG_structure_type "NeuronStorageImpl"
  DW_AT_containing_type  (0x000015c5 "c10::intrusive_ptr_target")
  DW_AT_calling_convention (DW_CC_pass_by_reference)
  DW_AT_byte_size        (0x30)                                  == 48 bytes   [OBSERVED]
  DW_TAG_inheritance     type c10::intrusive_ptr_target, data_member_location 0x00
  DW_TAG_member "data_ptr_"    type @0x1690 c10::UniqueQ7Ptr   data_member_location 0x10, PRIVATE
  DW_TAG_member "size_bytes_"  type @0x188d c10::SymInt        data_member_location 0x28, PRIVATE
```

| off  | size | member / base                      | type                            | conf | source |
|------|------|------------------------------------|---------------------------------|------|--------|
| 0x00 | 0x10 | (base) `c10::intrusive_ptr_target` | `c10::intrusive_ptr_target`     | HIGH | OBSERVED `DW_TAG_inheritance @0x00`; base `byte_size 0x0C` + 4 pad (§3a) |
| 0x10 | 0x18 | `data_ptr_`                        | `c10::UniqueQ7Ptr`              | HIGH | OBSERVED `data_member_location(0x10)`; typedef DIE @0x1690 → `unique_ptr<Q7PtrType,Q7Deleter>` @0xa2b7 `byte_size 0x18`, PRIVATE |
| 0x28 | 0x08 | `size_bytes_`                      | `c10::SymInt`                   | HIGH | OBSERVED `data_member_location(0x28)`; type DIE @0x188d `byte_size 0x08`, PRIVATE |

`0x10 + 0x18 + 0x08 = 0x30` — no tail padding `[OBSERVED byte_size + arithmetic]`. The
storage **uniquely owns** exactly one Q7 pointer: copy ctor/assign and default ctor are
`= delete`; move ctor/assign are `= default`.

**Methods (OBSERVED DWARF + emitted symbols):**

- ctor `NeuronStorageImpl(SymInt size_bytes, UniqueQ7Ptr data_ptr)` — move-stores both; no
  standalone `C2` symbol (inlined into the `make<NeuronStorageImpl>` producer, which **does**
  emit a call to the `SymInt` copy-ctor `_ZN3c106SymIntC2ERKS0_` to build `size_bytes_` from
  the `uint32` nbytes arg — confirmed in the producer disasm, §5).
- `data_ptr()` / `data_ptr() const → UniqueQ7Ptr&` (`_ZN3c1017NeuronStorageImpl8data_ptrEv`).
- `data()` / `data() const → Q7PtrType` `= data_ptr_.get()` **BY VALUE** — this is exactly
  what `NeuronTensorImpl::q7_data_ptr()` returns (§4).
- `nbytes() → size_t` (`size_bytes_.expect_int()`); `sym_nbytes() → SymInt`;
  `set_nbytes(size_t)`.
- `virtual ~NeuronStorageImpl` (override; vtable slot 0).

Emitted (weak/vague): `D0`/`D2` dtors, `_ZTI`/`_ZTS`/`_ZTV`. Grounded:
`nm wrapper_api.o | rg -c NeuronStorageImpl` = **8**.

**vtable** `.data._ZTVN3c1017NeuronStorageImplE`, **0x14 = 20 bytes** (`objdump -h`), reloc
slots (`objdump -r`):

```
+0x04  _ZTIN3c1017NeuronStorageImplE                       (typeinfo)
+0x08  _ZN3c1017NeuronStorageImplD2Ev                      (complete dtor)
+0x0C  _ZN3c1017NeuronStorageImplD0Ev                      (deleting dtor)
+0x10  _ZN3c1020intrusive_ptr_target17release_resourcesEv  (INHERITED, NOT overridden)
```

> **QUIRK — release happens via the member-dtor chain, not `release_resources`.** Slot 2 is
> the **inherited** `intrusive_ptr_target::release_resources`, not a `NeuronStorageImpl`
> override `[HIGH × OBSERVED reloc]`. So freeing HBM is **not** done in `release_resources`;
> it happens when the storage's member dtor runs `data_ptr_`'s destructor, which invokes the
> `UniqueQ7Ptr`'s `Q7Deleter` — `delete_ptr` (calls `neuron_hbm_deallocate`) for an owning
> allocation, or `delete_nothing` for an aliased/input pointer.

### 3a. `c10::intrusive_ptr_target` base (12 bytes) — DWARF DIE @0x15c5

`[HIGH × OBSERVED]` `DW_AT_byte_size 0x0c`:

```
_vptr$intrusive_ptr_target  @0x00  (4-byte vtable ptr)
refcount_                   @0x04  size_t (4 B, strong refs)
weakcount_                  @0x08  size_t (4 B, weak refs)
```

In `NeuronStorageImpl` this 12-byte base is padded to `0x10` because the next member starts
with `Q7PtrType.hbm_addr` (`uint64_t`, align 8) `[pad INFERRED from C ABI + the OBSERVED 0x10
member offset]`.

### 3b. `c10::SymInt` (8 bytes) — DWARF DIE @0x188d

`[HIGH × OBSERVED]` `DW_AT_byte_size 0x08`; the sole instance member is `data_ @0x00 :
int64_t`. `MASK` / `IS_SYM` / `MAX_SYM_IDX` / `MIN_INT` are static `const` (DWARF
`DW_AT_const_value`), not instance fields. From the const values: `IS_SYM =
0x8000000000000000` — when that bit is set, `data_` is a tagged pointer to a symbolic node;
otherwise `data_` is a plain `int64` byte count. `nbytes() = expect_int()` asserts
not-symbolic and returns the int. `[HIGH: const values OBSERVED; MED: symbolic-node packing
is standard-PyTorch semantics, not re-derived from the binary]`

---

## 4. `q7_data_ptr()` — returns `Q7PtrType` BY VALUE (the lazy device handle)

`[HIGH × OBSERVED]` The accessor is header-inline and one line:

```c
/* c10::NeuronTensorImpl::q7_data_ptr() const   linkage _ZNK3c1016NeuronTensorImpl11q7_data_ptrEv
 * DWARF return type = DIE @0x169b "c10::Q7PtrType" (the 16-byte struct, NOT a pointer/ref). */
Q7PtrType q7_data_ptr(const NeuronTensorImpl *self) {
    /* delegates straight through to the storage */
    return self->neuron_storage_impl_->data();      /* NeuronStorageImpl::data() */
}

/* NeuronStorageImpl::data() const -> Q7PtrType  (by value) */
Q7PtrType data(const NeuronStorageImpl *self) {
    return self->data_ptr_.get();                   /* unique_ptr<Q7PtrType,Q7Deleter>::get() */
}
```

`unique_ptr<Q7PtrType, Q7Deleter>::get()` returns `Q7Deleter::pointer`, and
`Q7Deleter::pointer` is `typedef Q7PtrType pointer;` — **not** `Q7PtrType*`. So `.get()` hands
back the **16-byte `Q7PtrType` value held inside the `__compressed_pair`**, copied out by
value, all the way up through `data()` → `q7_data_ptr()`.

> **GOTCHA — by-value is the whole point: the Q7 pointer is a lazy device handle, not a
> machine pointer.** A Q7/Xtensa core is 32-bit and **cannot directly address HBM**; the data
> "pointer" is a `{hbm_addr:u64, ctx_ptr:void*, is_nullptr:bool}` triple that must be
> *window-translated* on dereference (see [Q7PtrType + Lazy Translation](q7ptrtype.md) for
> `operator[]` / `neuron_translate`). Returning it **by value** copies the cached `ctx_ptr`
> and the 64-bit `hbm_addr` together so the caller can offset and translate without going back
> through HBM. There is no `T*` to hand out — a raw pointer would be a lie. `[HIGH ×
> OBSERVED]`

> **GOTCHA — `q7_data_ptr()` has no out-of-line symbol; it is instantiated per use site.**
> Because it is `inline` in the header, `nm wrapper_api.o | rg q7_data_ptr` is **empty** — the
> body is emitted only where a custom kernel calls it. Its existence, signature, and return
> type are pinned by the DWARF subprogram DIE in `wrapper_api.o` (`@0x0e9d`, `DW_AT_type
> @0x169b "c10::Q7PtrType"`) and by the header text. `[HIGH × OBSERVED DWARF + SOURCE]`

> **NOTE — Xtensa aggregate ABI for the 16-byte `Q7PtrType` (memory, not registers).** On
> this windowed-ABI target a `>8`-byte aggregate is passed/returned **via a hidden pointer to
> caller memory**, not packed into the return registers `a2:a3`. `q7_data_ptr()` itself is
> inlined and has no standalone symbol, but the identical mechanism is **decoded directly** at
> the one concrete producer callsite, `UTensor::operator at::Tensor() const`
> (`_ZNK7UTensorcvN2at6TensorEEv`), where the `Q7PtrType` is materialised into a stack buffer
> and a **pointer** to it is handed to the storage `make<>` producer:
>
> ```
> ; build the 16-byte Q7PtrType into a caller stack buffer at sp+40 :
>   s32i  a2, a1, 40        ; +0   hbm_addr lo  (uint64)
>   s32i.n a3, a1, 44       ; +4   hbm_addr hi
>   s32i.n a10, a1, 48      ; +8   ctx_ptr  (a10 = inlined neuron_translate_ctx() result)
>   s32i.n a15, a1, 52      ; +C   is_nullptr (bool, written as a word here)
> ; the trailing 4-byte Q7Deleter fn-ptr (delete_nothing) completes UniqueQ7Ptr :
>   const16 a15, <delete_nothing>   ; R_XTENSA reloc → Q7Deleter::delete_nothing
>   s32i.n a15, a1, 56      ; +16  deleter_  → unique_ptr spans sp+40..sp+63 = 0x18 (24 B)
> ; hand a POINTER to the aggregate to make<NeuronStorageImpl> (NOT a register pack) :
>   addi  a10, a1, 28       ; arg0 = sret slot for the returned intrusive_ptr
>   addi  a11, a1, 32       ; arg1 = unsigned int& nbytes
>   addi  a12, a1, 40       ; arg2 = &unique_ptr  (the 16-byte Q7PtrType lives here)
>   callx8 a2               ; → intrusive_ptr<NeuronStorageImpl>::make<...>
> ```
>
> Inside `make<>` (`entry a1, 80`) the argument arrives as a **pointer in `a4`** and is read
> field-by-field — proving by-memory, not by-register: `l32i a14, a4, 0` / `l32i a7, a4, 4`
> (the 64-bit `hbm_addr`), `l32i a15, a4, 8` (`ctx_ptr`), `s8i a9, a4, 12` (**confirms `+0xC`
> is the 1-byte `is_nullptr` bool**), `l32i a5, a4, 16` (the **4-byte `Q7Deleter` fn-ptr**, the
> field after the 16-byte `Q7PtrType`). This is the textbook `>8`-byte-aggregate handling on
> the 32-bit windowed ABI. `[HIGH × OBSERVED disasm]`

> **CORRECTION — there is no *out-of-line* function returning `Q7PtrType` by value in this
> archive.** Every `Q7PtrType`-producing routine (`q7_data_ptr`, `NeuronStorageImpl::data`,
> `Q7PtrType::operator+`, `NeuronAllocator::allocate`) is **header-inline** and is emitted only
> at its use site; `neuron_translate` returns `void*`, not `Q7PtrType`. The two `Q7PtrType`
> references that *do* survive as symbols in `wrapper_api.o` are the inlined producer callsite
> above and `c10::Q7Deleter::delete_nothing(c10::Q7PtrType)`
> (`_ZN3c109Q7Deleter14delete_nothingENS_9Q7PtrTypeE`), which takes the 16-byte `Q7PtrType`
> **by value as a parameter** — independently confirming the by-value aggregate convention.
> `[HIGH × OBSERVED — nm + disasm]`

---

## 5. The `make_intrusive` producers — how the chain is constructed

`[HIGH × OBSERVED]` `NeuronStorageImpl` and `NeuronTensorImpl` are never `new`-ed directly;
they are produced through `c10::make_intrusive`, which dispatches to the
`intrusive_ptr<T>::make<Args…>` static template. **Both** producers are emitted (weak/COMDAT)
in `wrapper_api.o`:

```
/* producer 1 — the storage  (.text…, size 0x168) */
c10::intrusive_ptr<NeuronStorageImpl>
  intrusive_ptr<NeuronStorageImpl>::make<unsigned int&, unique_ptr<Q7PtrType,Q7Deleter>>(
      unsigned int& nbytes,
      unique_ptr<Q7PtrType,Q7Deleter>&& data_ptr);
/* linkage _ZN3c1013intrusive_ptrINS_17NeuronStorageImpl…E4makeIJRj…Q7Deleter…EEE…  */

/* producer 2 — the tensor  (.text…, size 0xB0) */
c10::intrusive_ptr<NeuronTensorImpl>
  intrusive_ptr<NeuronTensorImpl>::make<intrusive_ptr<NeuronStorageImpl>, DispatchKey, TypeMeta>(
      intrusive_ptr<NeuronStorageImpl>&&,
      DispatchKey&&,
      caffe2::TypeMeta&&);
/* linkage _ZN3c1013intrusive_ptrINS_16NeuronTensorImpl…E4makeIJ…11DispatchKeyN6caffe28TypeMetaEEE… */
```

**Producer 1's call chain** (relocs in the `make<NeuronStorageImpl>` text, `objdump -dr`):

```
_Znwj                                   operator new(unsigned int)  → heap-allocate the 48-byte storage
_ZTVN3c1017NeuronStorageImplE           install the NeuronStorageImpl vtable
_Z20neuron_translate_ctxv               Q7PtrType default-ctor caches ctx_ptr = neuron_translate_ctx()
_ZN3c106SymIntC2ERKS0_                  SymInt copy-ctor → builds size_bytes_ from the uint32 nbytes
_ZN3c106detail23torchInternalAssertFailEPKcS2_jS2_S2_   the assert path
```

So `make<NeuronStorageImpl>` **inlines** the storage ctor: `new` the 48-byte object, write
its vptr, move the `UniqueQ7Ptr` into `data_ptr_@0x10`, and construct `size_bytes_@0x28` from
the `uint32` byte count via the `SymInt` copy-ctor. `[HIGH × OBSERVED relocs]`

The full producer pipeline (matching the allocator path in
[Device Allocators](device-allocators.md)):

```c
/* nbytes → owning UniqueQ7Ptr → refcounted storage → tensor */
uint32_t nbytes = /* numel * itemsize */;
UniqueQ7Ptr p = GetNeuronAllocator()->allocate(nbytes);     /* {Q7PtrType(hbm), Q7Deleter(&delete_ptr)} */
auto storage = make_intrusive<NeuronStorageImpl>(nbytes, std::move(p));
auto impl    = make_intrusive<NeuronTensorImpl>(std::move(storage), dispatch_key, type_meta);
at::Tensor t = at::detail::make_tensor<NeuronTensorImpl>(/* … */);   /* see §6 for the upcast */
```

Input / aliased tensors instead wrap a **pre-existing** `hbm_addr` with
`Q7Deleter(&delete_nothing)` (no free), selected by `is_nullptr` — see
[Q7PtrType + Lazy Translation](q7ptrtype.md).

---

## 6. The glue — a `NeuronTensorImpl` inside a stock `at::Tensor`

`[HIGH × OBSERVED]` Disassembling the emitted ctor
`_ZN3c1016NeuronTensorImplC2E…DispatchKey…` (native `xtensa-elf-objdump`, `XTENSA_CORE=
ncore2gp`) shows the header body instruction-by-instruction. The key moments (offsets within
the ctor `.text`):

```
+0x4b/+0xe4  callx8 __ashldi3                          64-bit shifts (DispatchKeySet from DispatchKey)
+0x124       s16i a3, a1, 8                            stage the CPU Device on the stack
+0x127/+0x12a const16/callx8 _ZN3c106Device8validateEv validate the DeviceType::CPU device
+0x146/+0x14e callx8 _ZN3c1010TensorImplC2E…DispatchKeySet…TypeMeta…optional<Device>
                                                       base TensorImpl(key_set, dtype, CPU) ctor
+0x154       l8ui a4, a2, 197                          read TensorImpl bitfield byte @ this+197 (0xC5)
+0x154/+0x164 const16 _ZTVN3c1016NeuronTensorImplE     materialise the NeuronTensorImpl vtable address
+0x16c       s32i a5, a2, 208                          store neuron_storage_impl_ at this+208 == 0xD0  ◄──
+0x174       s32i a3, a2, 0                            install vptr at this+0
+0x174       or  a4, a4, a15  (a15 = 2)                set bit 1 of the bitfield byte
+0x17c       s8i a4, a2, 197                           write it back  → set_storage_access_should_throw()
+0x17f       retw.n
```

> **GOTCHA — the `+0xD0` store is the offset, proven from machine code.** `s32i a5, a2, 208`
> stores the moved `intrusive_ptr<NeuronStorageImpl>` (loaded from the incoming arg slot)
> into `this+208 = 0xD0` — the `neuron_storage_impl_` member. This corroborates the DWARF
> `data_member_location(0xD0)` directly from the ctor body, independent of the debug info.
> `[HIGH × OBSERVED disasm]`

### The CPU-key + disabled-storage model

`[HIGH × OBSERVED]` The ctor builds the base `TensorImpl` with **`DeviceType::CPU`** — that
is the `Device::validate` call at `+0x127` and the `optional<Device>` arg to the base ctor at
`+0x146`. Constructing as CPU is deliberate: **the stock ATen dispatch / shape / dtype
machinery all run unchanged** (the framework "thinks" it has a normal CPU tensor). The header
then calls `set_storage_access_should_throw()` — and the disasm shows it has **no out-of-line
symbol**: it is the inline read-modify-write at `+0x154 … +0x17c` that OR-s bit 1 (value
`2`) into the `storage_access_should_throw_` bitfield byte at `this+197 (0xC5)`, inside the
`TensorImpl` base. (Confirmed: `nm` shows no `set_storage_access` symbol; only the inline
RMW.)

> **GOTCHA — the stock storage is *poisoned*, fail-loud.** Once the bitfield bit is set, any
> code that reaches through the normal `storage()` / `data_ptr()` path makes `TensorImpl`
> invoke the out-of-line helper `_ZNK3c1010TensorImpl26throw_storage_access_errorEv` and
> **throw**, instead of returning a bogus host pointer. That helper is present as an
> **undefined `U` reference** in `wrapper_api.o` (`nm` confirms it). So a Neuron kernel that
> tries `tensor.data_ptr<float>()` does **not** silently read garbage — it throws. The only
> correct data access is `q7_data_ptr()` / [the TCM accessor](tensor-accessor.md). `[HIGH ×
> OBSERVED symtab + disasm]`

### The vtable — metadata virtuals are all stock

`[HIGH × OBSERVED]` `.data._ZTVN3c1016NeuronTensorImplE` is **0x70 = 112 bytes** (`objdump
-h`) with **27 relocs** (`objdump -r`). It **overrides only**:

```
+0x04  _ZTIN3c1016NeuronTensorImplE                  (typeinfo)
+0x08  _ZN3c1016NeuronTensorImplD2Ev                 (complete dtor)
+0x0C  _ZN3c1016NeuronTensorImplD0Ev                 (deleting dtor)
+0x10  _ZN3c1010TensorImpl17release_resourcesEv      (STOCK TensorImpl release)
```

Every other slot points at the **stock `c10::TensorImpl`** implementation —
`sym_sizes`, `sym_sizes_custom`, `sym_numel_custom`, `strides_custom`, `is_contiguous_custom`,
`size_custom`, `sizes_custom`, `device_custom`, `layout_custom`, `dim_custom`,
`numel_custom`, `has_storage`, `storage`, `storage_offset`, `set_size`, `set_stride`, … So
**`NeuronTensorImpl` does not customise shape / stride / dtype / device / numel / contiguity
at all** — exactly the separation in §0. `[HIGH × OBSERVED reloc list]`

### The upcast into `impl_`

`[HIGH × OBSERVED]` `at::detail::make_tensor<NeuronTensorImpl>(…)` builds an
`intrusive_ptr<NeuronTensorImpl>` then stores it into `TensorBase::impl_` (an
`intrusive_ptr<TensorImpl, UndefinedTensorImpl>`) through the upcast helper, whose DWARF
subprogram DIE (`@0x03a4`) takes a `NeuronTensorImpl*` and **returns `c10::TensorImpl*`**
(DWARF `DW_AT_type 0x00012166 "c10::TensorImpl *"`):

```
_ZN3c106detail11assign_ptr_INS_10TensorImplENS_19UndefinedTensorImplE
   NS0_34intrusive_target_default_null_typeINS_16NeuronTensorImplEEEEEPT_S8_
  ≡  detail::assign_ptr_<TensorImpl, UndefinedTensorImpl,
                         intrusive_target_default_null_type<NeuronTensorImpl>>(NeuronTensorImpl*)
       -> TensorImpl*
```

Because `TensorImpl` is the base at offset 0, the upcast **preserves the pointer value**, so
at runtime `impl_->target_` is literally the address of the `NeuronTensorImpl` object. `[HIGH
× OBSERVED DWARF]`

---

## 7. The customised `at::TensorBase` — `impl_` + `coherency_enforcer_@0x04`

`[HIGH × OBSERVED]` DWARF DIE `wrapper_api.o @0x940d`, `DW_AT_byte_size 0x14`:

| off  | size | member               | type                                          | source |
|------|------|----------------------|-----------------------------------------------|--------|
| 0x00 | 0x04 | `impl_`              | `intrusive_ptr<TensorImpl, UndefinedTensorImpl>` (4 B, DIE @0x303c) | OBSERVED @0x9416 |
| 0x04 | 0x10 | `coherency_enforcer_`| `at::CoherencyEnforcer` (0x10, DIE @0x9f0b)   | OBSERVED @0x9424 `data_member_location(0x04)` |

`at::Tensor` (DIE `@0x91f1`, `byte_size 0x14`) inherits `at::TensorBase` at offset 0 and adds
**no** data members — it is a pure interface wrapper.

> **FINDING — this is a *customised* `at::TensorBase`.** Stock upstream `TensorBase` has
> **only** `impl_`. This build inserts a non-stock 16-byte `coherency_enforcer_` at **`+0x04`**
> (DWARF-confirmed `data_member_location(0x04)`), so the `intrusive_ptr` `impl_` occupies
> `+0x00..+0x04` and the enforcer occupies `+0x04..+0x14`. The enforcer is four 4-byte fields
> — `buffered_readers_@0`, `buffered_writers_@4`, `unbuffered_accessors_@8`,
> `policy_`(enum, `COHERENT`/`INCOHERENT_VERBOSE`/`INCOHERENT_QUIET`)`@0xC` — and tracks
> concurrent buffered-reader / buffered-writer / unbuffered-accessor counts so accessors can
> assert/warn on incoherent concurrent access to the same HBM tensor. It is **orthogonal to
> the Q7 data path** but ships in the same modified ATen headers; full semantics are in
> [The CoherencyEnforcer](coherency-enforcer.md). `[HIGH × OBSERVED layout; LOW × SOURCE for
> purpose prose]`

---

## 8. Design rationale (why the chain is shaped this way)

`[HIGH × OBSERVED, corroborated by the §6 ctor/vtable relocs]`

- **Separation of metadata from the data pointer.** Shape, stride, dtype (`TypeMeta`),
  device, dispatch keys, contiguity, numel — all stay in the unmodified `c10::TensorImpl`
  base (`0x00..0xD0`) and use the **stock** `TensorImpl` virtuals (§6 vtable: only 3
  dtor/release slots overridden). Every generic ATen op, view, reshape, broadcast and print
  path works on a Neuron tensor unchanged.
- **Only the data pointer is retargeted, out-of-band.** One extra 4-byte member
  (`neuron_storage_impl_@0xD0`) reaches a `NeuronStorageImpl` whose `data_ptr_` embeds the
  16-byte `Q7PtrType` value. The data is a **64-bit HBM SoC address + cached translation
  context**, not a native 32-bit pointer.
- **The stock storage is deliberately disabled.** Constructed CPU, then
  `set_storage_access_should_throw()` poisons `TensorImpl::storage_`; the normal
  `storage()`/`data_ptr()` path **throws** rather than returning a bogus host pointer — a
  fail-loud safety design that forces kernels through `q7_data_ptr()` / the TCM accessor.
- **Allocator bypass.** A Q7 "pointer" is a 16-byte value, not a `void*`, so it cannot ride
  the `c10::Allocator`/`DataPtr` contract; the stateless 1-byte `NeuronAllocator` returns a
  `UniqueQ7Ptr` directly and frees via `neuron_hbm_deallocate` (see
  [Device Allocators](device-allocators.md)). Ownership (free vs no-op) is encoded in the
  `UniqueQ7Ptr`'s `Q7Deleter` (`delete_ptr` vs `delete_nothing`), selected by `is_nullptr`.
- **Reference-counted sharing.** `NeuronStorageImpl : intrusive_ptr_target`, held by
  `intrusive_ptr`, so multiple `NeuronTensorImpl`s (views/aliases) share one HBM allocation;
  the storage — and its `UniqueQ7Ptr` deleter — runs exactly once when the last referencing
  tensor dies.

---

## 9. Confidence ledger

| claim | conf × prov | anchor |
|-------|-------------|--------|
| `NeuronTensorImpl` byte_size **0xD8 (216)**; `TensorImpl` base @0; `neuron_storage_impl_` (4 B) @0xD0 | HIGH × OBSERVED | DWARF DIE @0x0e45; disasm `s32i a5,a2,208` |
| `NeuronStorageImpl` byte_size **0x30 (48)**; ipt_target base @0; `data_ptr_` (24 B) @0x10; `size_bytes_` (8 B) @0x28 | HIGH × OBSERVED | DWARF DIE @0x1472 |
| `q7_data_ptr()` returns `Q7PtrType` **by value** (DIE @0x169b, 16 B, not a pointer) | HIGH × OBSERVED | DWARF subprogram @0x0e9d; 16 B aggregate passed/returned via hidden memory ptr — decoded at the inlined `UTensor::operator at::Tensor()` callsite + `Q7Deleter::delete_nothing(Q7PtrType)` by-value param (§4) |
| `at::TensorBase` 0x14; `impl_`@0x00; `coherency_enforcer_`@**0x04** | HIGH × OBSERVED | DWARF DIE @0x940d, member @0x9424 |
| disabled-storage model: inline `set_storage_access_should_throw()` bitfield RMW + `throw_storage_access_error` `U` ref | HIGH × OBSERVED | ctor disasm `l8ui/or/s8i` @ this+197; `nm` U symbol |
| `intrusive_ptr_target` 0x0C (vptr@0/refcount_@4/weakcount_@8); both `intrusive_ptr` = 4 B | HIGH × OBSERVED | DWARF DIEs @0x15c5, @0x303c, @0x11ca |
| `UniqueQ7Ptr` = 0x18 (24 B); `Q7PtrType` = 0x10 (16 B); `SymInt` = 0x08 | HIGH × OBSERVED | DWARF DIEs @0xa2b7, @0x169b, @0x188d |
| `make<NeuronStorageImpl>` / `make<NeuronTensorImpl>` producers + `SymInt` copy-ctor call | HIGH × OBSERVED | weak symbols; producer relocs `_ZN3c106SymIntC2ERKS0_` |
| `assign_ptr_(NeuronTensorImpl*) → TensorImpl*` value-preserving upcast | HIGH × OBSERVED | DWARF subprogram @0x03a4, return DIE @0x12166 |
| NeuronTensorImpl vtable 0x70 (27 relocs): only D2/D0/release overridden, all metadata stock | HIGH × OBSERVED | `objdump -h/-r` |
| `SymInt` symbolic-node packing semantics | MED | const values OBSERVED; interpretation standard-PyTorch |
| `coherency_enforcer_` *purpose* prose | LOW × SOURCE | header text; layout OBSERVED |

### Corrections to the SX-ABI-02 backing analysis

Re-verification against DWARF + **native** `xtensa-elf-objdump` (the backing analysis lacked
a working Xtensa disassembler) **upholds every byte-exact claim** — `NeuronTensorImpl 0xD8`,
`NeuronStorageImpl 0x30`, `neuron_storage_impl_@0xD0`, `data_ptr_@0x10`, `size_bytes_@0x28`,
`coherency_enforcer_@0x04`, the disabled-storage poison, the vtable override sets, the
`make<>` producers, and the `assign_ptr_` upcast. **No CORRECTIONS were required.** The one
gap the backing analysis flagged — "no native Xtensa disassembly, so the exact store writing
`neuron_storage_impl_@0xD0` is not decoded" — is now **closed**: the ctor body decodes to
`s32i a5, a2, 208` (§6), confirming the `0xD0` store directly from machine code, and the
inline `set_storage_access_should_throw()` RMW (`l8ui/or a15=2/s8i` at `this+197`) is decoded
as well.

One **scope CORRECTION** to the by-value attribution: there is **no out-of-line symbol that
returns `Q7PtrType` by value** — every `Q7PtrType` producer is header-inline. The by-value
aggregate ABI is instead proven (§4) at the inlined producer callsite
`UTensor::operator at::Tensor() const` (16-byte buffer materialised on the caller stack, a
pointer to it handed to `make<NeuronStorageImpl>`, dereferenced field-by-field inside) and by
`c10::Q7Deleter::delete_nothing(c10::Q7PtrType)` taking `Q7PtrType` **by value as a
parameter** — not by any standalone `operator+` / `allocate` text symbol (`neuron_translate`
returns `void*`). The allocator singleton is also re-confirmed: `GetNeuronAllocator`
(`NeuronAllocator.o` `.text` @0x230, size 0x0B) materialises `&_neuron_alloc`
(`.bss+0x608`, a **1-byte** static), proving `NeuronAllocator` is a stateless 1-byte façade,
not a `c10::Allocator`.

---

## See also

- [Q7PtrType + Lazy Translation](q7ptrtype.md) — the 16-byte device handle at the bottom of the chain.
- [The Retargeted TensorAccessor](tensor-accessor.md) — the correct way to dereference `q7_data_ptr()`.
- [The CoherencyEnforcer](coherency-enforcer.md) — the `TensorBase+0x04` member, in full.
- [ScalarType ↔ DTYPE Rosetta](scalartype-dtype-rosetta.md) — the `TypeMeta` dtype carried in the base.
