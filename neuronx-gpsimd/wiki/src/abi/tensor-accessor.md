# The Retargeted TensorAccessor

> **Scope.** This page decodes `at::TensorAccessorBase<T,N,index_t>` and
> `at::TensorAccessor<T,N,read_only,index_t>` — the **device-tensor indexer** a GPSIMD custom op uses
> to walk a tensor `a[i][j][k]` by stride over **64-bit HBM SoC addresses**. The stock PyTorch
> accessor stores a native `T*` and does a raw `data_[k]` deref; this build **retargets** the pointer
> type to the 16-byte [`Q7PtrType`](q7ptrtype.md) value and routes every leaf element touch through
> `neuron_translate` (the window mapper). The result reproduces PyTorch's exact strided addressing,
> but realised over an HBM address with **one lazy window-translate at the leaf only**. This is the
> per-element-translate sibling of the [buffered/TCM stream accessors](tensorstream-tcm.md); it builds
> on [the `at::Tensor` object chain](tensor-object-chain.md) (which supplies `q7_data_ptr()`) and the
> [`CoherencyEnforcer`](coherency-enforcer.md) (which it acquires as an RAII token).
>
> **Provenance.** Source = the shipped `aws-neuronx-gpsimd-customop-lib_0.21.2.0` package: the
> modified ATen headers (`ATen/core/TensorAccessor.h`, `neuron_torch_extension/Q7PtrType.h`) plus the
> compiled static lib `custom_op/neuron/libneuroncustomop.a` (ELF32 Tensilica Xtensa, DWARF v4, **not
> stripped**; producer `XtensaTools-14.09 clang 10.0.1`). All prose is derived from binary/static
> analysis alone (lawful interoperability RE, DMCA 17 U.S.C. 1201(f)). No vendor source tree is
> referenced.
>
> **Tags per claim:** `[CONF × PROV]` — confidence `HIGH/MED/LOW` × provenance `OBSERVED` (read from
> DWARF/ELF/symtab/disasm), `SOURCE` (read from the shipped ATen/Q7 header text, with the callee
> runtime signatures independently OBSERVED), `COMPOSED` (aggregate layout built from DWARF-pinned
> component sizes and confirmed by an Xtensa-faithful ILP32 compile-verify — see §7), `INFERRED`
> (C-ABI consequence, no contradicting evidence).

> **GOTCHA — the accessor structs carry NO DWARF and NO symbol, ANYWHERE in the archive.**
> `TensorAccessorBase` / `TensorAccessor` / `TensorElementReference` are **pure header templates**,
> instantiated only at a *customer op's* compile site — never inside the vendor lib. Verified this
> pass over the unstripped archive: `llvm-dwarfdump --debug-info` = **0** hits, `readelf -p
> .debug_str` = **0** hits, `llvm-nm` (defined+undef) = **0** hits for all three names across all 10
> members (`for o in *.o; do readelf -p .debug_str $o | rg -c TensorAccessor; done` → all 0). So the
> aggregate byte-exact layout below is **COMPOSED** from DWARF-pinned *component* sizes (Q7PtrType,
> CoherencyEnforcer, `int64_t`, the CU `addr_size`) and **compile-verified**, not read off the
> aggregate's own DWARF (which does not exist). What *is* OBSERVED is every piece it is built from —
> §2 marks each cell `OBSERVED` vs `COMPOSED`. `extracted/` is gitignored — reach it with an absolute
> path or `fd --no-ignore`. Ground every count with `nm <lib> | rg -c`, never a decompile.

---

## 0. Headline — the retarget in one line `[HIGH × OBSERVED]`

Stock upstream ATen parameterises the accessor on a *pointer-traits policy* whose `PtrType` is a
native `T*`; indexing is a raw `this->data_[strides_[0]*i]` deref. The Q7 core is **32-bit** and
cannot ride a 64-bit HBM address on a `T*`, and a raw deref of an HBM address would be meaningless on
the NX-local space. So this build makes exactly five changes in `ATen/core/TensorAccessor.h`, all
read directly from the shipped header:

| # | Change | Stock | This build | Header anchor |
|---|--------|-------|------------|---------------|
| R1 | `PtrType` retarget | `typedef typename PtrTraits<T>::PtrType` (= `T*`) | `typedef Q7PtrType PtrType;` (16-byte **value**) | `:102, :147, :168` |
| R2 | template params | `<T,N,PtrTraits,index_t>` | `TensorAccessorBase<T,N,index_t>`; the 3rd `TensorAccessor` slot is now `bool read_only` | `:99-100, :144-145` |
| R3 | dim advance | native `data_ + strides_[0]*i` | `data_.template operator+<T>(strides_[0]*i)` (Q7 address arith) | `:157, :161` |
| R4 | leaf access | returns raw `T&` | returns a `TensorElementReference<T,read_only>` **proxy** (lazy translate) | `:176-182` |
| R5 | coherence + RAII | none | adds `CoherencyEnforcer &coherency_enforcer_` member + **virtual dtor**; ctor `acquire`, dtor `release` | `:108-114, :137` |

> **NOTE — the stale `#if 0` block proves R2 is real.** The disabled
> `GenericPackedTensorAccessor` block (`:187-351`) still names `TensorAccessor<T, N-1, PtrTraits,
> index_t>` (`:268, :271, :274, :277`) — the **old 4-arg signature whose 3rd slot is `PtrTraits`**.
> That no longer matches the live `TensorAccessor<T,N-1,read_only,index_t>`; the block is fenced out
> with the comment *"Can't compile GenericPackedAccessors without modifying them … ignore them with
> `#if 0`"* (`:185-187`). It is stale dead code, never instantiated. `[HIGH × OBSERVED]`

---

## 1. Component grounding `[HIGH × OBSERVED]`

Everything the accessor is *built from* is DWARF-pinned in `wrapper_api.o` (and the runtime leaf in
`translation.o`). Read this pass:

```
$ readelf -h wrapper_api.o | rg -i 'class|machine'
  Class:    ELF32              Machine:  Tensilica Xtensa Processor
$ llvm-dwarfdump --debug-info wrapper_api.o | rg -m1 addr_size
  ... version = 0x0004 ... addr_size = 0x04 ...        # 32-bit: void* = size_t = 4 bytes
```

| Component | byte_size | members (DWARF `data_member_location`) | DWARF anchor |
|-----------|-----------|----------------------------------------|--------------|
| `c10::Q7PtrType` | `0x10` | `hbm_addr@0x00` (uint64), `ctx_ptr@0x08` (void*), `is_nullptr@0x0c` (bool) | `wrapper_api.o` DIE `0x169b` |
| `at::CoherencyEnforcer` | `0x10` | `buffered_readers_@0x00`, `buffered_writers_@0x04`, `unbuffered_accessors_@0x08`, `policy_@0x0c` | `wrapper_api.o` DIE `0x9f0b` |
| `long long int` (= `int64_t` = `index_t`) | `0x08` | — (8-aligned on Xtensa) | OBSERVED |
| `const int64_t*` | `4` (CU `addr_size 0x04`) | — | OBSERVED |
| `neuron_translate(void*, uint64_t) → void*` | — (the leaf callee) | linkage `_Z16neuron_translatePvy` | **defined** in `translation.o` `@0x120` |

```
$ llvm-dwarfdump --debug-info wrapper_api.o | rg -A1 '"Q7PtrType"'
  DW_AT_name ("Q7PtrType")   DW_AT_byte_size (0x10)
    hbm_addr  @0x00     ctx_ptr  @0x08     is_nullptr  @0x0c
$ llvm-dwarfdump --debug-info wrapper_api.o | rg -A1 '"CoherencyEnforcer"'
  DW_AT_name ("CoherencyEnforcer")  DW_AT_byte_size (0x10)
    buffered_readers_@0x00  buffered_writers_@0x04  unbuffered_accessors_@0x08  policy_@0x0c
$ llvm-dwarfdump --debug-info wrapper_api.o | rg -A6 'q7_data_ptrEv'
  _ZNK3c1016NeuronTensorImpl11q7_data_ptrEv  DW_AT_type (0x169b "c10::Q7PtrType")   # returns by VALUE
  _ZNK2at10TensorBase11q7_data_ptrEv         DW_AT_type (0x169b "c10::Q7PtrType")
```

> **GOTCHA — 32-bit core, but `int64_t` is 8-ALIGNED on Xtensa.** `void*`/`size_t` are 4 bytes (CU
> `addr_size 0x04`), but `uint64_t`/`int64_t` are 8 bytes **aligned to 8** (proven by DWARF:
> `Q7PtrType.ctx_ptr@0x08` ⇒ `hbm_addr` fills `0x00–0x07`). This is *not* x86-32 (i386 aligns
> `int64` to 4). The §7 compile-verify force-aligns the 64-bit member types to model the Xtensa ABI;
> without that override an i386 model wrongly yields `Q7PtrType=12`. Do not transpose the §2 offsets
> onto a 64-bit host build. `[HIGH × OBSERVED]`

---

## 2. Struct layout — `at::TensorAccessorBase<float,3>` (40 bytes) `[HIGH × COMPOSED]`

Member order = declaration order (`TensorAccessor.h:134-137`): `data_`, `sizes_`, `strides_`,
`coherency_enforcer_`. The **virtual dtor** (R5) makes the class polymorphic ⇒ a `vptr` at offset 0;
the embedded `Q7PtrType` forces struct align 8 (its `uint64 hbm_addr`).

| off | size | member | type | conf × prov |
|-----|------|--------|------|-------------|
| `0x00` | `0x04` | *(vptr)* | `__vtable*` — class is polymorphic (sole virtual = the dtor) | `HIGH × COMPOSED` |
| `0x04` | `0x04` | *(pad)* | align `data_` to 8 (`Q7PtrType` needs align 8) | `HIGH × INFERRED` (`-Wpadded`) |
| `0x08` | `0x10` | `data_` | `Q7PtrType` (`PtrType`) — the device pointer **BY VALUE** = `q7_data_ptr()` return. Internally `hbm_addr@0x08, ctx_ptr@0x10, is_nullptr@0x14` | `HIGH × OBSERVED` (component=0x10) |
| `0x18` | `0x04` | `sizes_` | `const int64_t*` = `sizes().data()` — borrowed into `TensorImpl`, **not** copied | `HIGH × OBSERVED` (4B ptr) |
| `0x1C` | `0x04` | `strides_` | `const int64_t*` = `strides().data()` — in **ELEMENTS**; drives §4 | `HIGH × OBSERVED` (4B ptr) |
| `0x20` | `0x04` | `coherency_enforcer_` | `CoherencyEnforcer&` — **REFERENCE** bound to `TensorBase+0x04` (not a copy) | `HIGH × OBSERVED` (ref=4B) |
| `0x24` | `0x04` | *(pad)* | tail pad to round size to 8-align | `HIGH × INFERRED` (`-Wpadded`) |
| **`0x28`** | | **`sizeof = 40`** | compile-verify: `sizeof(TensorAccessorBase<float,3>)==40` | `HIGH × COMPOSED` |

**Vtable** (GCC class dump, ILP32 Itanium-style) `_ZTV18TensorAccessorBaseIfLj3EE`, 4 entries:
`[0]` offset-to-top=0, `[1]` `&typeinfo`, `[2]` `~TensorAccessorBase` (complete/D1 — runs
`release_unbuffered_accessor()`), `[3]` `~TensorAccessorBase` (deleting/D0). The **single virtual is
the dtor** — its only reason to exist is to make the RAII release (`§5`) run reliably even when the
accessor is held by base reference. `[HIGH × COMPOSED]`

### 2a. `at::TensorAccessor<T,N,read_only,index_t>` (40 bytes, no new members)

`TensorAccessor : public TensorAccessorBase<T,N,index_t>` (`:145`) **adds zero data members** — only
a forwarding ctor and the two `operator[]` overloads. So `sizeof(TensorAccessor<float,3,false>) ==
sizeof(TensorAccessorBase<float,3>) == 40`, base at offset 0, **shared vptr** (primary base). The
`read_only` bool is a **type-level flag with NO storage**: it selects whether the leaf
`TensorElementReference` permits writes (`static_assert` in `operator=`). `const` Tensors yield
`read_only=true`; non-const yield `false`. `[HIGH × SOURCE; sizeof × COMPOSED]`

### 2b. `at::TensorElementReference<T,read_only>` (24 bytes — the N==1 leaf proxy)

The proxy returned by the **1-D** accessor's `operator[]` (`TensorAccessor.h:60-92`). It holds a
`Q7PtrType` **by value** (a *copy* of the accessor's `data_`) plus the precomputed **element** index,
and translates lazily on read (`operator T()`) or write (`operator=`).

| off | size | member | type | conf × prov |
|-----|------|--------|------|-------------|
| `0x00` | `0x10` | `data_` | `Q7PtrType` — a COPY of the accessor's `data_` (`:90`) | `HIGH × OBSERVED` (0x10) |
| `0x10` | `0x04` | `index_` | `const size_t` — the already-multiplied `strides_[0]*i` ELEMENT index (`:91`); **note `size_t` = 4B** | `HIGH × OBSERVED` (size_t=4) |
| `0x14` | `0x04` | *(pad)* | align 8 (`data_`'s uint64) | `HIGH × INFERRED` |
| **`0x18`** | | **`sizeof = 24`** | | `HIGH × COMPOSED` |

> **NOTE — the proxy is the whole point (the header's own bug comment, `:31-59`).** A translation
> *window* is valid only until the next `neuron_translate`. The old design returned `T&`, which would
> **dangle** as soon as a later access remapped the window — the header spells out exactly this hazard
> (`a[0] = b[0] + c[0] + d[0]`). The proxy defers the `neuron_translate` to the moment the element is
> read/written, so **each element touch remaps its own window** and no window is held across
> accesses. `[HIGH × SOURCE]`

---

## 3. The factory — `TensorBase::accessor<T,N>()` `[HIGH × SOURCE]`

`TensorBase.h:542-557` is a header-inline template (no out-of-line symbol in the lib — `accessor<T,N>`
is never instantiated by the vendor; only `q7_data_ptr` / `tcm_accessor` of the TensorBase accessor
family appear in DWARF, because only those are referenced by a *defined* function). It has three
overloads:

```c
// const Tensor  → read-only accessor
template<typename T, size_t N>
TensorAccessor<T,N,true>  TensorBase::accessor() const& {
    static_assert(N > 0, "...");                            // 0-D ⇒ use *data_ptr<T>()
    TORCH_CHECK(dim() == N, "TensorAccessor expected ", N, " dims ...");
    return TensorAccessor<T,N,true >( q7_data_ptr(),        // (1) Q7PtrType BY VALUE — the HBM addr + ctx
                                      sizes().data(),       // (2) const int64_t*  (borrowed, not copied)
                                      strides().data(),     // (3) const int64_t*  (in ELEMENTS)
                                      coherency_enforcer_ ); // (4) CoherencyEnforcer& (TensorBase+0x04)
}
// non-const Tensor → read/write accessor — same body, TensorAccessor<T,N,false>
template<typename T, size_t N>
TensorAccessor<T,N,false> TensorBase::accessor()  &      { /* ... <false> ... */ }
template<typename T, size_t N>
TensorAccessor<T,N,false> TensorBase::accessor()  &&     = delete;   // no accessor on an rvalue Tensor
```

Each argument is grounded: **(1)** `q7_data_ptr()` returns `c10::Q7PtrType` **by value** (DWARF type
`0x169b`, OBSERVED above) — the 64-bit HBM address packaged with its cached translation ctx, *not* a
raw pointer; **(2)/(3)** `sizes()/strides()` are `IntArrayRef = ArrayRef<int64_t>`, `.data()` →
`const int64_t*` straight out of the stock `TensorImpl::sizes_and_strides_` — so view/reshape/
transpose/broadcast metadata flows through unchanged; **(4)** `coherency_enforcer_` is the
`TensorBase` member **at offset `0x04`** (OBSERVED: `coherency_enforcer_` DIE → `DW_AT_type 0x9f0b
"at::CoherencyEnforcer"`, `DW_AT_data_member_location 0x04`), passed **by reference**. Because the
member is `mutable`, the `const&` overload can hand a non-const ref out of a `const TensorBase`.

> **NOTE — ctor side effect (RAII open).** `TensorAccessorBase`'s ctor body runs
> `coherency_enforcer_.acquire_unbuffered_accessor()` (`:110`); its virtual dtor runs
> `release_unbuffered_accessor()` (`:113`). So a `TensorAccessor` is an **unbuffered** accessor (direct
> per-element HBM access), mutually exclusive with the *buffered* stream accessors but compatible with
> other unbuffered ones. See §5. `[HIGH × SOURCE]`

---

## 4. The strided-indexing mechanism — `index → translate → deref` `[HIGH × SOURCE]`

This is the core of the page: how `accessor[i][j]...` composes the **stride math** with the
**`Q7Ptr::operator[]` lazy translate**. The N-dim `operator[]` returns a **lower-dim accessor**
carrying the advanced `Q7Ptr` (via `operator+`, **no translate**); only the **final** scalar access
goes through `Q7Ptr::operator[]` (translate). The recursion is `TensorAccessor.h:156-162`:

```c
// N-dim:  TensorAccessor<T,N>::operator[](i)  →  a TensorAccessor<T,N-1> by VALUE
TensorAccessor<T, N-1, read_only, index_t>
TensorAccessor<T,N,read_only,index_t>::operator[](index_t i) {
    return TensorAccessor<T, N-1, read_only, index_t>(
        this->data_.template operator+<T>(this->strides_[0] * i), // (A) advance Q7 ptr — ADDRESS ARITH ONLY
        this->sizes_   + 1,                                       // (B) drop dim 0 (sizes  pointer += 8B)
        this->strides_ + 1,                                       // (C) drop dim 0 (strides pointer += 8B)
        this->coherency_enforcer_);                               // (D) SAME enforcer ref threaded forward
}
```

**(A)** `Q7PtrType::operator+<T>` (`Q7PtrType.h:31-34`) is **pure 64-bit address arithmetic** — it
does **NOT** call `neuron_translate` and **NOT** touch HBM:

```c
template<typename T>
Q7PtrType Q7PtrType::operator+(size_t idx) const {
    uint64_t result = hbm_addr + sizeof(T) * idx;     // byte offset; idx = strides_[0]*i (in elements)
    return Q7PtrType(result, ctx_ptr);                // forwards ctx_ptr; is_nullptr = false
}
```

`strides_[0]` is in **elements**, so `sizeof(T)*` converts to bytes. **Descending a dimension is
therefore FREE** — no window mapping until the leaf. **(B)/(C)** `sizes_+1 / strides_+1` is ordinary
C pointer arithmetic on `const int64_t*` (advance by `sizeof(int64_t)=8`), peeling the leading
dimension so the lower-rank accessor sees dims `[1..N-1]`. **(D)** the *same* enforcer reference is
threaded through every rank.

The recursion bottoms out at the **N==1 specialisation** (`TensorAccessor.h:166-183`), which does
**not** advance `data_` — it hands the *current* Q7 base pointer and the element index to the proxy:

```c
// N==1 base case: returns the lazy proxy, NOT a lower accessor
TensorElementReference<T, read_only>
TensorAccessor<T,1,read_only,index_t>::operator[](index_t i) {
    return TensorElementReference<T, read_only>(this->data_, this->strides_[0] * i);  // copy ptr + element idx
}
```

The proxy defers translation to the read/write (`TensorAccessor.h:77-87`), and
`Q7PtrType::operator[]<T>` (`Q7PtrType.h:26-29`) is the **TERMINAL** step that finally touches HBM:

```c
// read:   (T)proxy           →  data_.operator[]<T>(index_)
// write:  proxy = value      →  data_.operator[]<T>(index_) = value   (read_only ⇒ static_assert FAILS)
template<typename T>
T& Q7PtrType::operator[](size_t idx) const {
    void* nx = neuron_translate(ctx_ptr, hbm_addr + sizeof(T) * idx);  // <<< THE ONLY HBM-touching step
    return ((T*)nx)[0];                                               // deref NX-local mapped address
}
```

### Worked example — `float a[i][j][k]` (sizes/strides in elements)

```c
auto acc = t.accessor<float,3>();   // TensorAccessor<float,3,false> ; data_ = q7_data_ptr()
acc[i]        // → TA<float,2,false> : data_.hbm_addr += 4*strides[0]*i ; sizes_=&sizes[1], strides_=&strides[1]   (operator+, NO translate)
acc[i][j]     // → TA<float,1,false> : data_.hbm_addr += 4*strides[1]*j (on top)                                  (operator+, NO translate)
acc[i][j][k]  // → TensorElementReference{ data_(2-level-advanced hbm_addr), index_ = strides[2]*k }              (proxy ctor, NO translate)
(float) acc[i][j][k];   // proxy read → Q7Ptr::operator[]<float>(strides[2]*k)
                        //   = neuron_translate(ctx, hbm_addr_2lvl + 4*strides[2]*k) ; deref                       (THE single translate)
```

Net byte offset from the base = `4 * (strides[0]*i + strides[1]*j + strides[2]*k)` — **exactly
PyTorch's strided addressing**, but over a 64-bit HBM SoC address with **one** lazy window-translate
at the leaf.

> **QUIRK — `int64 → size_t` (64→32) narrowing at the accessor↔Q7Ptr boundary.** `index_t` is
> `int64_t` and `strides_[0]*i` is computed in int64, but **all three** Q7-side consumers take
> `size_t` (= **unsigned int, 4 bytes** on this ILP32 target): `operator+<T>(size_t)`,
> `operator[]<T>(size_t)`, and `TensorElementReference::index_ : const size_t`. The element index is
> therefore **truncated to 32 bits** before the byte multiply. Confirmed by the mangling: the 7
> shipped `operator[]` instantiation decls end in `…ix…j` (`j` = `unsigned int` = `size_t`/4B):
>
> ```
> $ readelf -p .debug_str wrapper_api.o | rg '_ZNK3c109Q7PtrTypeixI'      # 7 hits
> _ZNK3c109Q7PtrTypeixIxEERT_j   _ZNK3c109Q7PtrTypeixIaEERT_j   _ZNK3c109Q7PtrTypeixItEERT_j
> _ZNK3c109Q7PtrTypeixIsEERT_j   _ZNK3c109Q7PtrTypeixIfEERT_j   _ZNK3c109Q7PtrTypeixIhEERT_j
> _ZNK3c109Q7PtrTypeixIiEERT_j
> #   T ∈ { x=long long, a=signed char, t=unsigned short, s=short, f=float, h=unsigned char, i=int }
> ```
>
> The **byte address** arithmetic inside `operator+/[]` is done in `uint64` (`hbm_addr` is 64-bit), so
> only the *element* index is 32-bit-limited, not the HBM address space. On a 32-bit core element
> counts are well under 2³², so this is **LOW** severity, but it is a genuine narrowing.
> `[MED × SOURCE — types/mangling OBSERVED; the wrap not exercised in the binary]`

---

## 4a. Disassembly evidence — what the binary can and cannot show `[HIGH × OBSERVED]`

> **GOTCHA — the operator chain has NO body to disassemble in this archive.** `Q7PtrType::operator+`
> and `operator[]`, and the whole accessor recursion, are **inlined header templates**: they appear
> in `wrapper_api.o` **only as template-instantiation *declarations* in `.debug_str`** (the 7
> `…ix…j` strings above; `operator+`/`pl` produces **zero** out-of-line symbols —
> `llvm-nm wrapper_api.o | rg 'Q7PtrTypeix|Q7PtrTypepl'` returns **nothing**, not even a weak symbol).
> There
> is no defined `operator[]`/`operator+` body anywhere in the lib to step through; they materialise
> only when a *customer* op compiles `accessor<T,N>()`. The *intermediate-dim = `operator+`,
> no-translate* and *leaf = `operator[]`, translate* split is therefore established from the **header
> source** (Q7PtrType.h: `operator+` has no `neuron_translate`; `operator[]` does), corroborated by
> the symtab fact that `operator+` has **no out-of-line instantiation** (consistent with a trivial
> inlined address add) while `operator[]` is instantiated 7× (it calls an external function, so it is
> emitted). `[HIGH × OBSERVED for the symtab facts; HIGH × SOURCE for the split]`

What the binary **does** show byte-exactly is the **leaf callee** — `neuron_translate`, the single
HBM-touching step at the end of every element access. It is **defined with code** in `translation.o`
at `0x120` and disassembles cleanly under the native `ncore2gp` (Vision-Q7) backend
(`XTENSA_CORE=ncore2gp`, `xtensa-elf-objdump`):

```asm
00000120 <_Z16neuron_translatePvy>:          ; neuron_translate(void* ctx_ptr=a2, uint64_t addr=a4:lo/a5:hi)
 120:  entry  a1, 32
 123:  { l32i a2, a2, 16 ; l32i a6, a2, 20 ; mov.a a3, a2 ; nop }   ; load mapping-table base from ctx_ptr
 133:  { l32i a7, a3, 0  ; l32i a8, a3, 4 }                         ; entry[0].base  (64-bit)
 13b:  { and  a6, a6, a5 ; nop ; and a9, a2, a4 }                  ; (addr & mask) — 64-bit, hi/lo
 143:  { xor  a7, a9, a7 ; nop ; xor a15, a6, a8 }                 ; compare (addr&mask) ^ base
 14b:  or     a7, a7, a15
 150:  beqz   a7, 204 <…+0xe4>                                     ; window 0 hit?
 153:  { l32i a2, a3, 48 ; l32i a6, a3, 52 } …                     ; else entry[1] (32-byte stride) …
 1c7:  { beqz.w15 a7, 204 ; movi a6, 3 ; movi a10,-1 ; movi a11,-1 }
 …
00000204 <…+0xe4>:
 204:  slli   a5, a6, 5                                            ; matched_index * 32  (32-byte entries)
 207:  add.n  a3, a3, a5                                           ; → &entry[match]
 209:  and    a2, a4, a2                                           ; addr & entry.mask → NX-local offset
```

This is the window mapper the leaf access invokes: it walks a table of **32-byte** entries
(`slli …,5; add.n`), masks the 64-bit `hbm_addr + sizeof(T)*idx` against each window's `base/mask`,
and returns the NX-local mapped address that `((T*)nx)[0]` then dereferences. The FLIX `{ … }`
bundles confirm this is genuine Vision-Q7 device code. `[HIGH × OBSERVED — native ncore2gp disasm]`

> **NOTE — why no per-insn disasm of the accessor itself.** The local Xtensa backend has no defined
> accessor symbol to target (the templates are not emitted in the vendor lib), so the byte-exact
> *layout* stands on DWARF-pinned components + compile-verify (§7), and the *mechanism* stands on the
> header source with the **OBSERVED** runtime callee (`neuron_translate`, disassembled above) and the
> **OBSERVED** 7-way `operator[]` instantiation set. This is the only gap, and it does not touch the
> ABI. `[HIGH × OBSERVED]`

---

## 5. Coherency integration — RAII over the shared enforcer `[HIGH × SOURCE]`

The accessor doubles as a **scoped coherence token** (cross-ref [`CoherencyEnforcer`](coherency-enforcer.md)):

- It holds the enforcer **BY REFERENCE** (`TensorAccessorBase` member `@0x20`), bound to the tensor's
  own `mutable` enforcer at **`TensorBase+0x04`** (OBSERVED member offset). It is **not copied** — every
  accessor over the same tensor shares the live counts (`buffered_readers_@0x00`,
  `buffered_writers_@0x04`, `unbuffered_accessors_@0x08`, `policy_@0x0c`).
- **ctor** → `acquire_unbuffered_accessor()` (`++unbuffered_accessors_`, after the policy assert that
  no buffered reader/writer is open). **dtor (virtual)** → `release_unbuffered_accessor()`
  (`--unbuffered_accessors_`). So a `TensorAccessor` opens exactly one **unbuffered** slot for its
  lifetime — direct per-element HBM access via `neuron_translate`.
- **Recursion is coherent.** Every `operator[]` builds a *new* `TensorAccessor` (= a fresh
  `TensorAccessorBase` ctor), so each lower-rank temporary acquires + releases one unbuffered slot in
  its short life. Because **unbuffered-vs-unbuffered is compatible**, the nesting is fine.
- The **virtual dtor is the sole virtual** (§2 vtable `{D1,D0}`): it exists *only* to guarantee the
  release runs even when the accessor is held by a base reference — the functional reason the
  otherwise-POD accessor is made polymorphic and pays a `vptr`.
- The enforcer is **purely advisory bookkeeping** gating whether the accessor was *allowed* to open;
  it does **not** participate in the address math or the translate. The data path (§4) is orthogonal.

> **NOTE — contrast with the buffered stream accessors ([TensorStream + TCM](tensorstream-tcm.md)).**
> `TensorReadStreamAccessor` acquires a **buffered reader** and DMAs a local buffer from HBM via
> `neuron_memcpy`, serving elements from the buffer with **no per-element translate**;
> `TensorWriteStreamAccessor` acquires a **buffered writer** (exclusive) and flushes the buffer back;
> `TensorTcmAccessor` takes **no** enforcer at all. The `TensorAccessor` documented here is the only
> family doing **per-element lazy translation**. `[HIGH × SOURCE]`

---

## 6. Why this design — synthesis

- **Device pointer as a VALUE, not a pointer.** A 64-bit HBM address cannot ride a 32-bit `T*`, so
  `PtrType` is retargeted to the 16-byte `Q7PtrType` value, stored inline at `data_@0x08`; the
  accessor never dereferences a host pointer.
- **Descend free, touch lazily.** Multi-dim indexing only does 64-bit *address arithmetic*
  (`operator+`, no HBM access) while peeling dimensions; the single window-translate happens at the
  leaf, through `TensorElementReference`, so no translation window is held across accesses — the exact
  hazard the header comment (`:31-59`) calls out. `a[i][j][k]` stays cheap and window-safe.
- **Strides stay in elements, ATen-native.** `sizes_`/`strides_` are borrowed straight from the stock
  `TensorImpl`, so view/reshape/transpose/broadcast metadata flows through unchanged; the accessor
  reproduces PyTorch's exact strided addressing, only over an HBM address.
- **Coherence for free via RAII.** The accessor is a scoped coherence token; the virtual dtor exists
  solely to make the release reliable.
- **Header-only, zero vendor footprint.** None of the accessor types are emitted in the shipped lib;
  they materialise only when a customer op instantiates `accessor<T,N>()`. The lib ships the *types*
  it is built from (`Q7PtrType`, `CoherencyEnforcer`) and the runtime callees (`neuron_translate`,
  `neuron_memcpy`), but not a compiled copy of the indexer.

---

## 7. Compile-verify — the Xtensa-faithful ILP32 model `[HIGH × COMPOSED]`

Because the accessor structs carry **no DWARF** (the §0 GOTCHA), their byte-exact layout was composed
from the DWARF-pinned component sizes (§1) and confirmed by compiling a faithful ILP32 replica with
`offsetof`/`sizeof` `static_assert`s.

- Tool: `gcc -m32 -std=c++14 -nostdinc -ffreestanding -c` (compile-only; the asserts are
  compile-time, no link needed).
- Pitfall handled: plain x86-32 aligns `int64_t` to **4** bytes, which does **not** match Xtensa
  (which 8-aligns `int64/uint64` — proven by DWARF: `Q7PtrType.ctx_ptr@0x08`). The replica
  force-aligns the 64-bit member types with `__attribute__((aligned(8)))`, reproducing
  `Q7PtrType = 16 / align 8` exactly as DWARF reports. (Without the override the i386 model wrongly
  yields `Q7PtrType=12`, `TER=20`, `TAB=36` — a documented mismatch that confirms the override is
  required.)

All `static_assert`s held; `-Wpadded` + GCC `-fdump-lang-class` independently confirmed:

```
Q7PtrType                           : 16  (3 tail pad)                  [matches §1]
CoherencyEnforcer                   : 16  (no pad)                      [matches §1]
TensorElementReference<float,false> : 24  (data_@0, index_@0x10, 4 pad)
TensorAccessorBase<float,3>         : 40  align 8 ; vptr@0, (4 pad), data_@0x08,
                                          sizes_@0x18, strides_@0x1C, enforcer&@0x20, (4 pad);
                                          vtable _ZTV18TensorAccessorBaseIfLj3EE = {D1,D0} dtor pair
TensorAccessor<float,3,false>       : 40  base@0 (primary-for), shared vptr, adds no members
```

---

## 8. Confidence / OBSERVED-vs-COMPOSED ledger

**HIGH × OBSERVED** (DWARF byte_size / member_location of the *component* types in `wrapper_api.o`,
emitted by the ABI's own clang; ELF/symtab scans; native `ncore2gp` disasm):
`Q7PtrType=0x10` (`hbm_addr@0`/`ctx_ptr@8`/`is_nullptr@0xc`); `CoherencyEnforcer=0x10`
(`readers@0`/`writers@4`/`unbuffered@8`/`policy@0xc`); `int64_t=8`; CU `addr_size=4`;
`coherency_enforcer_@TensorBase+0x04` (type `at::CoherencyEnforcer`); `q7_data_ptr()` returns
`c10::Q7PtrType` by value; the **7** `Q7PtrType::operator[]<T>` (`…ix…j`) instantiation decls;
`operator+` has **zero** out-of-line symbol (inlined); `neuron_translate` defined in `translation.o`
@`0x120` (disassembled); **accessor structs carry NO DWARF / NO symbol / NO string anywhere** in the
10-member archive.

**HIGH × SOURCE** (shipped ATen/Q7 header text; the callee runtime signatures independently OBSERVED):
the retarget R1–R5; the `operator[]` recursion (advance via `operator+<T>`, no HBM touch; `sizes_+1`/
`strides_+1` peel dims; N==1 returns the proxy; proxy read/write → `Q7PtrType::operator[]<T>` →
`neuron_translate`); the factory `accessor<T,N>()` ctor args; RAII acquire/release of one unbuffered
slot; `read_only` as a storage-free type flag.

**HIGH × COMPOSED** (DWARF components + compile-verify): the aggregate layouts —
`TensorAccessorBase=40`, `TensorAccessor=40`, `TensorElementReference=24`; the `{D1,D0}` vtable.

**MED:** the `int64 → size_t` (64→32) element-index narrowing (types/mangling OBSERVED; the wrap not
exercised in the shipped binary).

**LOW / NOTED:** the `#if 0` `GenericPackedTensorAccessor` / `packed_accessor32/64` block is stale
dead code (still names the old `PtrTraits` arg; never instantiated); no per-instruction disasm of the
inlined operator chain (no defined symbol; does not affect the ABI).

> **CORRECTION (to SX-ABI-03).** None required — every claim re-verified against DWARF/ELF/disasm
> this pass holds. One **sharpening**: SX-ABI-03 §8 lists "no native Xtensa per-instruction disasm" as
> the sole gap; this page narrows that — the *operator chain* has no per-insn disasm **because it has
> no defined symbol** (inlined header templates), but the **leaf callee `neuron_translate` IS defined
> and disassembled** here under `XTENSA_CORE=ncore2gp` (§4a), so the terminal translate step is now
> directly OBSERVED at the instruction level, not merely DWARF-declared.

---

### Cross-references

- [`Q7PtrType` + Lazy Translation](q7ptrtype.md) — `operator[]<T>` (the leaf translate) and
  `operator+<T>` (the no-translate dim advance) bodies; the 16-byte value layout.
- [The `at::Tensor` Object Chain](tensor-object-chain.md) — `q7_data_ptr()` → `Q7PtrType` by value
  and the `TensorBase` member layout (`coherency_enforcer_@+0x04`).
- [TensorStream + TCM Staging](tensorstream-tcm.md) — the **buffered** counterpart accessors
  (`neuron_memcpy` into a local buffer; no per-element translate) and the TCM accessor.
- [The `CoherencyEnforcer`](coherency-enforcer.md) — the acquire/release matrix, the unbuffered slot
  semantics, and the `mutable` member at `TensorBase+0x04`.
