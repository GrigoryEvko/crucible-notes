# The CoherencyEnforcer

A 16-byte, non-virtual bookkeeping object embedded *inside* every `at::TensorBase`
that guards how a custom op may concurrently open accessors onto one tensor's
device (Q7/HBM) memory. It does not move data, take locks, or issue fences: it is
a single-thread *coherence-assertion* layer that counts open readers, writers and
unbuffered accessors per tensor, and an acquire-time policy that decides whether a
would-be-incoherent access aborts, warns, or is silently allowed. The actual data
movement is the orthogonal `neuron_memcpy`/Q7 path; the enforcer only gates whether
an accessor was *permitted to open*.

Everything here is reconstructed from the shipped, non-stripped
`libneuroncustomop.a` (ELF32 Xtensa, DWARF v4, producer *XtensaTools-14.09 clang
10.0.1*) plus the modified ATen headers it carries. Every offset, size and
enumerator below is taken from `DW_AT_byte_size` / `DW_AT_data_member_location` /
`DW_AT_const_value` emitted by the ABI's own compiler — authoritative, not
inferred — and cross-checked against the header text and an instruction-level
DWARF location expression.

> **Audience anchor.** You are rebuilding a Vision-Q7-compatible GPSIMD custom-op
> ABI. To stay binary-compatible with a shipped op, your `at::TensorBase` *must*
> place a 16-byte `CoherencyEnforcer` at offset `+0x04` (after the 4-byte `impl_`),
> and your accessor ctors/dtors *must* increment/decrement the right counter. Get
> the offset wrong and every accessor mutates the wrong 16 bytes of a foreign
> tensor.

---

## 1. Byte layout — `at::CoherencyEnforcer` (16 bytes)

`[HIGH × OBSERVED]` DWARF DIE `wrapper_api.o @0x00009f0b`, `DW_TAG_class_type
"CoherencyEnforcer"`, `DW_AT_calling_convention (DW_CC_pass_by_value)`,
`DW_AT_byte_size (0x10)`, declared `Coherence.h:25`.

| off    | size | member                  | type (DWARF)                       | meaning                                                  | DIE / member-loc       |
| ------ | ---- | ----------------------- | ---------------------------------- | -------------------------------------------------------- | ---------------------- |
| `0x00` | 4    | `buffered_readers_`     | `int` (`DW_ATE_signed`, 4B)        | count of open **buffered readers** (concurrent OK)       | `@0x9f14`, loc `0x00`  |
| `0x04` | 4    | `buffered_writers_`     | `int` (`DW_ATE_signed`, 4B)        | count of open **buffered writers** (must be exclusive)   | `@0x9f20`, loc `0x04`  |
| `0x08` | 4    | `unbuffered_accessors_` | `int` (`DW_ATE_signed`, 4B)        | count of open **unbuffered accessors** (concurrent OK)   | `@0x9f2c`, loc `0x08`  |
| `0x0C` | 4    | `policy_`               | `enum Policy` (underlying `unsigned int`, 4B) | the active **coherence policy** (default `COHERENT`) | `@0x9f38`, loc `0x0c`  |
| —      | `0x10` | **total**             |                                    | four naturally-4-aligned 4-byte fields — **no padding**  |                        |

```c
// at::CoherencyEnforcer — 16 bytes, POD, trivially copyable (DW_CC_pass_by_value).
// Coherence.h:25; field decl lines 108-111. No vtable: the first member at +0x00
// is buffered_readers_, NOT a vptr.  [HIGH x OBSERVED]
struct at_CoherencyEnforcer {
    int32_t buffered_readers_;       // +0x00  (=0 default)
    int32_t buffered_writers_;       // +0x04  (=0 default)
    int32_t unbuffered_accessors_;   // +0x08  (=0 default)
    uint32_t policy_;                // +0x0C  enum Policy (=COHERENT=0 default)
};                                   //  sizeof == 0x10
```

Field meanings:

- **`buffered_readers_`** — number of `TensorReadStreamAccessor`s currently open.
  Buffered readers cache tensor data in a local (TCM-resident) buffer; *when* that
  cache is filled from HBM is opaque to the user. Many may coexist.
- **`buffered_writers_`** — number of `TensorWriteStreamAccessor`s open. A buffered
  writer caches writes and flushes back to HBM at an opaque time, so it needs
  **exclusive** access — no other reader, writer, or unbuffered accessor.
- **`unbuffered_accessors_`** — number of `TensorAccessor`s open (the `operator[]`
  indexer). These read/write HBM directly through a windowed `Q7PtrType`. They may
  coexist with each other, but **not** with any buffered reader/writer, because
  main memory is not guaranteed coherent with a buffered cache.
- **`policy_`** — selects the reaction when a new acquire would be incoherent with
  what is already open (§3).

> **NOTE.** The three counts are *plain signed `int`* — no `std::atomic`, no lock,
> no fence appears anywhere in the DIE. This is **single-thread advisory
> bookkeeping**: it catches misuse *patterns* in cooperative code, not true
> hardware data races. `[OBSERVED — no atomic/lock type in the DIE]`

The class is **non-polymorphic** (byte_size `0x10`, first data member at offset
`0x00`, no `_vptr` member) and **trivially copyable** (`DW_CC_pass_by_value`). Its
layout is identical on 32- and 64-bit hosts (all 4-byte ints); only its *position*
inside `TensorBase` — right after the 4-byte 32-bit `impl_` — is 32-bit-specific.
Do not transpose the `TensorBase` offsets in §4 onto a 64-bit build.

### Methods (header-only — no out-of-line symbol exists)

All eight methods are present as DWARF declarations (each with the artificial
`this` of type `at::CoherencyEnforcer *`), but **none** is emitted as a defined
text symbol — they are inlined into customer accessor-use sites.

| method                          | mangled symbol                                                  | decl |
| ------------------------------- | --------------------------------------------------------------- | ---- |
| `acquire_buffered_reader()`     | `_ZN2at17CoherencyEnforcer23acquire_buffered_readerEv`          | `:40` |
| `release_buffered_reader()`     | `_ZN2at17CoherencyEnforcer23release_buffered_readerEv`          | `:53` |
| `acquire_buffered_writer()`     | `_ZN2at17CoherencyEnforcer23acquire_buffered_writerEv`          | `:60` |
| `release_buffered_writer()`     | `_ZN2at17CoherencyEnforcer23release_buffered_writerEv`          | `:73` |
| `acquire_unbuffered_accessor()` | `_ZN2at17CoherencyEnforcer27acquire_unbuffered_accessorEv`      | `:81` |
| `release_unbuffered_accessor()` | `_ZN2at17CoherencyEnforcer27release_unbuffered_accessorEv`      | `:94` |
| `Policy get_policy()`           | `_ZN2at17CoherencyEnforcer10get_policyEv`                       | `:99` |
| `void set_policy(Policy)`       | `_ZN2at17CoherencyEnforcer10set_policyENS0_6PolicyE`            | `:103` |

> **GOTCHA — the whole machinery is header-inline.** `llvm-nm --defined-only`
> across **all 10** archive members returns **zero** defined symbols for any
> `CoherencyEnforcer` method or for `get/set_accessor_coherence_policy`. The
> mangled names and the field strings (`buffered_readers_`, `INCOHERENT_VERBOSE`,
> `INCOHERENT_QUIET`) appear **only** in `.debug_str` (section `[254]` of
> `wrapper_api.o`), never in `.text` (which is only `0x5fc` bytes) or `.rodata`.
> The verbose-path `printf` format `"WARNING: Already opened ..."` is **absent
> from every object**, and no `__assert*` callee is linked. The shipped library
> therefore carries the **type** (for ABI/layout) and the tensor-side plumbing,
> but **not** a compiled copy of the acquire/release/warn logic — that is
> materialised only when a *customer* op instantiates an accessor at a real use
> site. `[HIGH x OBSERVED]`

---

## 2. The `Policy` enum

`[HIGH × OBSERVED]` DWARF DIE `wrapper_api.o @0x00009f44`,
`DW_TAG_enumeration_type "Policy"`, `DW_AT_byte_size 0x04`, underlying type
`unsigned int` (DIE `0x68`), declared `Coherence.h:27`.

```c
enum Policy /* : unsigned int, 4 bytes */ {
    COHERENT           = 0,   // default; ENFORCE — assert no incoherent access
    INCOHERENT_VERBOSE = 1,   // WARN via printf, then allow
    INCOHERENT_QUIET   = 2    // silently allow (pure bookkeeping)
};
```

`policy_` is the **only** field that is *read* (not just counted); it is read
exclusively inside the three `acquire_*` bodies. Per-policy behaviour:

- **`COHERENT (0)` — enforce.** On every acquire, `assert(...)` that no
  incompatible accessor is already open (§3 matrix). A violation fires the
  assert, which `abort()`s in an assertions-enabled build. With `-DNDEBUG` the
  assert is a no-op, so a *release* build silently degrades `COHERENT` to "no
  check". This is the fail-loud default: a wrong concurrent-access pattern is
  treated as a bug. `[HIGH x SOURCE; assert callee not linked — bodies
  un-instantiated here]`
- **`INCOHERENT_VERBOSE (1)` — warn, allow.** On an incompatible acquire it
  `printf`s the count of currently-open readers/writers/unbuffered accessors and a
  *"...may cause incoherent behaviour."* note, then **still increments** its
  counter. Coherence becomes the user's responsibility.
- **`INCOHERENT_QUIET (2)` — silent, allow.** No assert, no warning; just
  increments. Pure bookkeeping.

> **NOTE.** `release_*` is **policy-independent**: it `assert`s the counter is
> `> 0` (an underflow / unbalanced-release guard, again a no-op under `NDEBUG`)
> then decrements. `[HIGH x SOURCE]`

The policy is per-tensor and is exposed on `TensorBase` (§4) as
`get_accessor_coherence_policy()` / `set_accessor_coherence_policy(Policy)`. A
freshly-constructed tensor defaults to `COHERENT` because the embedded enforcer's
`policy_` member-initialiser is `COHERENT`.

---

## 3. Read-vs-write tracking and the conflict rules

Three independent counters, one per accessor kind: `acquire_*` increments,
`release_*` decrements. The acquire predicates (decoded from the exact
`assert` / `printf` conditions) define the compatibility matrix:

```c
// acquire_buffered_reader:   reader is OK alongside other readers only.
assert(buffered_writers_ == 0 && unbuffered_accessors_ == 0);
// acquire_buffered_writer:   FULLY EXCLUSIVE — blocks everything, incl. writers.
assert(buffered_readers_ == 0 && buffered_writers_ == 0 && unbuffered_accessors_ == 0);
// acquire_unbuffered_accessor: OK alongside other unbuffered only.
assert(buffered_readers_ == 0 && buffered_writers_ == 0);
```

Who may coexist (rows = already open, cols = newly requested; ✓ = compatible
under `COHERENT`, ✗ = incoherent → enforce / warn / quiet per policy):

|                      | new: bReader | new: bWriter | new: unbuffered |
| -------------------- | :----------: | :----------: | :-------------: |
| **open bReader**     | ✓            | ✗            | ✗               |
| **open bWriter**     | ✗            | ✗            | ✗               |
| **open unbuffered**  | ✗            | ✗            | ✓               |

The rule set in one line: **a buffered writer is exclusive against everything
(including other writers); many readers coexist; many unbuffered coexist; cached
(buffered) and direct (unbuffered) never mix.** Reader-vs-reader and
unbuffered-vs-unbuffered are the only "same-kind compatible" pairs — and note a
writer is *not* compatible even with another writer. `[HIGH x SOURCE — predicates
decoded from Coherence.h:42/62/83; every named method exists as a DWARF DIE]`

Underflow guards on release (`buffered_readers_ > 0`, etc.) catch an unbalanced
release under assertions.

---

## 4. Embedding in `at::TensorBase` — the `mutable` member at `+0x04`

`[HIGH × OBSERVED]` DWARF `at::TensorBase @0x0000940d`, `DW_AT_byte_size 0x14`
(= 20), declared `TensorBase.h:81`:

| off    | size   | member                | type                                                                  | DIE / member-loc      |
| ------ | ------ | --------------------- | --------------------------------------------------------------------- | --------------------- |
| `0x00` | 4      | `impl_`               | `c10::intrusive_ptr<c10::TensorImpl, c10::UndefinedTensorImpl>` (4B, protected) | `@0x9416`, loc `0x00` |
| `0x04` | `0x10` | `coherency_enforcer_` | `at::CoherencyEnforcer` (16B, protected, **`mutable`**)               | `@0x9424`, loc `0x04` |
| —      | `0x14` | **total**             | no tail pad (enforcer ends 4-aligned at `0x14`)                       |                       |

`at::Tensor` (DWARF `@0x000091f1`, byte_size `0x14`) derives from `TensorBase` and
adds **no** data members — it is a pure interface wrapper. So the enforcer lives at
`Tensor+0x04` too. See [The at::Tensor Object Chain](./tensor-object-chain.md)
(owner of the full `TensorBase` layout) for `impl_` and the Q7 data path.

The member is declared `mutable`:

```c
// TensorBase.h:868-869
// Even on a const Tensor, want to be able to mutate coherency_enforcer_
mutable CoherencyEnforcer coherency_enforcer_;
```

This is corroborated *from the binary*: `get_accessor_coherence_policy`,
`set_accessor_coherence_policy`, and the `const&`-qualified accessor factories all
carry a **`const at::TensorBase *` `this`** (`_ZNK...` linkage) yet read/write the
enforcer — only legal because the member is `mutable`. `[OBSERVED:
_ZNK2at10TensorBase29set_accessor_coherence_policyE... has a const-this formal
@0x9ce8]` Rationale: incrementing a live-accessor count is "not part of the
tensor's value", so coherence tracking is excluded from const-ness; reading a
`const Tensor` through an accessor must still bump a counter.

### Instruction-level proof of `+0x04`

`[HIGH × OBSERVED — strongest layout evidence]` Inside the **defined** function
`UTensor::operator at::Tensor()` (`_ZNK7UTensorcvN2at6TensorEEv`, `wrapper_api.o`),
the `TensorBase` construction inlines the enforcer's default ctor
`_ZN2at17CoherencyEnforcerC2Ev` at PC `0x261..0x269`
(`DW_TAG_inlined_subroutine @0x138dc`). That inlined ctor's `this` has the DWARF
location:

```
DW_OP_breg5 +4, DW_OP_stack_value     // this = (TensorBase this in a5) + 4
```

i.e. `&coherency_enforcer_ == TensorBase_this + 0x04` — confirming
`data_member_location 0x04` at the instruction level, independently of the
structure DIE. Every `at::Tensor`/`at::TensorBase` construction therefore
default-constructs its embedded enforcer to `{readers=0, writers=0, unbuffered=0,
policy=COHERENT}`.

### Tensor-side API and accessor factories

```c
// TensorBase.h — all header-inline; DWARF DIEs present.
Policy get_accessor_coherence_policy() const         // :618 -> coherency_enforcer_.get_policy()
void   set_accessor_coherence_policy(Policy) const   // :622 -> .set_policy(p)

// Each factory passes coherency_enforcer_ BY REFERENCE into the accessor:
accessor<T,N>() const&  -> TensorAccessor<T,N,true >(q7_data_ptr(), sizes, strides, coherency_enforcer_)  // :546 read-only
accessor<T,N>() &       -> TensorAccessor<T,N,false>(q7_data_ptr(), sizes, strides, coherency_enforcer_)  // :554 read/write
accessor<T,N>() &&      = delete;                                                                          // :557
read_stream_accessor<T>()  const& -> TensorReadStreamAccessor<T>(q7_data_ptr(), nbytes()/sizeof(T), coherency_enforcer_)  // :592
write_stream_accessor<T>() &      -> TensorWriteStreamAccessor<T>(q7_data_ptr(), nbytes()/sizeof(T), coherency_enforcer_) // :599
tcm_accessor() const&  -> TensorTcmAccessor<true >(q7_data_ptr());   // :607  ** NO enforcer passed **
tcm_accessor() &       -> TensorTcmAccessor<false>(q7_data_ptr());   // :612  ** NO enforcer passed **
```

The `const&`/`&` ref-qualifier overloads give a read-only accessor for a `const`
tensor (`read_only = true`) and a read/write accessor for a non-`const` tensor
(`read_only = false`); the rvalue `&&` overloads are deleted to prevent an
accessor outliving a temporary tensor. `[OBSERVED]`

> **GOTCHA — TensorTcmAccessor is NOT tracked.** `Coherence.h`'s own doc comment
> lists `TensorTcmAccessor` as an example of an "unbuffered accessor", **but** the
> `tcm_accessor()` factories pass **only `q7_data_ptr()`** — no `coherency_enforcer_`
> — and the ctor `TensorTcmAccessor(const Q7PtrType &data)` takes only a
> `Q7PtrType` (`TensorTcmAccessor.h:32`). So a `TensorTcmAccessor` neither acquires
> nor releases an unbuffered slot: it is invisible to the enforcer in this build.
> Only `TensorAccessor` (the `operator[]` indexer) and the two stream accessors
> participate in enforcement. Whether this is an intentional fast path or a gap
> against the header's own comment cannot be determined from the binary — only the
> contract (no enforcer arg) is `[OBSERVED]`. If you reimplement, decide
> deliberately whether your TCM accessor should bump `unbuffered_accessors_`.

---

## 5. RAII acquire/release — how accessors drive the enforcer

The enforcer is driven scope-bound: **acquire in the ctor, release in the dtor.**
The accessor holds the enforcer **by reference** (`CoherencyEnforcer
&coherency_enforcer_`), so it mutates the *tensor's own* embedded member, not a
copy.

### (a) Unbuffered — `at::TensorAccessorBase<T,N>` (`TensorAccessor.h:100`)

```c
// TensorAccessorBase<T,N>  — RAII over an UNBUFFERED slot.  [SOURCE + OBSERVED]
TensorAccessorBase(PtrType data, const index_t* sizes, const index_t* strides,
                   CoherencyEnforcer &enforcer)            // :108
    : data_(data), sizes_(sizes), strides_(strides),
      coherency_enforcer_(enforcer) {                      // :137 holds a REFERENCE
    coherency_enforcer_.acquire_unbuffered_accessor();     // :110  ++unbuffered_accessors_
}
virtual ~TensorAccessorBase() {                            // :112  (the one virtual dtor in the chain)
    coherency_enforcer_.release_unbuffered_accessor();     // :113  --unbuffered_accessors_
}
```

`operator[]` descends one rank and forwards the **same** enforcer reference to the
lower-rank `TensorAccessor` (`:157/:161`), so a multi-dimensional index does not
re-acquire — the slot is held once for the lifetime of the top-level accessor.
Element access ultimately goes through a `TensorElementReference`, whose
`operator=` / `operator T()` call `Q7PtrType::operator[]<T>` — a **windowed HBM
read/write** (the lazy-translate path), not a native pointer dereference. See
[The Retargeted TensorAccessor](./tensor-accessor.md) for the indexer and the
`Q7Ptr` element path.

> **NOTE.** `TensorAccessorBase` has the *only* virtual destructor in the accessor
> chain — so `release_unbuffered_accessor()` runs on the base even when the most
> derived accessor is destroyed. The enforcer *itself* is not virtual.

### (b) Buffered reader — `at::TensorReadStreamAccessor<T>` (`TensorStreamAccessor.h:164`)

```c
// TensorReadStreamAccessor<T>  — RAII over a BUFFERED-READER slot.  [SOURCE]
TensorReadStreamAccessor(Q7PtrType data, size_t n, CoherencyEnforcer &enforcer)
    : coherency_enforcer_(enforcer), stream_(...) {
    coherency_enforcer_.acquire_buffered_reader();         // :168  ++buffered_readers_
    stream_.copy_data_to_buffer();                         // :170  prime the 4 KiB local buffer from HBM
}
~TensorReadStreamAccessor() { close(); }                   // :179
void close() {
    coherency_enforcer_.release_buffered_reader();         // :208  --buffered_readers_
    stream_.close();
}
```

It wraps a `TensorStream<T>` that memcpys a `BUFFER_SIZE_BYTES` = **4 KiB**
(`TensorStreamAccessor.h:18`) local buffer from HBM and serves `next_element()`
from it, refilling (`copy_data_to_buffer`) when exhausted.

### (c) Buffered writer — `at::TensorWriteStreamAccessor<T>` (`TensorStreamAccessor.h:218`)

```c
// TensorWriteStreamAccessor<T>  — RAII over the EXCLUSIVE BUFFERED-WRITER slot.  [SOURCE]
TensorWriteStreamAccessor(Q7PtrType data, size_t n, CoherencyEnforcer &enforcer)
    : coherency_enforcer_(enforcer), stream_(...) {
    coherency_enforcer_.acquire_buffered_writer();         // :222  ++buffered_writers_ (must be exclusive)
}
~TensorWriteStreamAccessor() { close(); }                  // :231
void close() {
    stream_.copy_buffer_to_data();                         // :262  flush local buffer -> HBM
    coherency_enforcer_.release_buffered_writer();          // :263  --buffered_writers_
}
```

`write()` fills the local buffer, flushing (`copy_buffer_to_data`) to HBM when the
buffer fills or on `close()`. The writer is exclusive by policy (writer
compatibility = none).

> **GOTCHA — the writer flush must precede the release.** In the writer `close()`,
> `copy_buffer_to_data()` runs **before** `release_buffered_writer()` (`:262`
> then `:263`). If you reverse them in a reimplementation, you release the
> exclusive slot while unflushed data still sits in the local buffer — another
> accessor could open and observe stale HBM. Order matters.

See [TensorStream + TCM Staging](./tensorstream-tcm.md) for the `TensorStream<T>`
buffer mechanics and TCM allocation.

---

## 6. Reaching HBM — the `neuron_memcpy` flush path

The enforcer never touches memory; the **transfer** is the orthogonal
`neuron_memcpy` path that the stream/TCM accessors call when their local buffer
must be materialised from or flushed to HBM. `[OBSERVED — data_transfer.o DWARF]`

Two overloads, direction encoded by *which argument is the 64-bit HBM address*:

```c
// data_transfer.cpp:402/407 — defined in data_transfer.o
int neuron_memcpy(void    *dst, uint64_t src, size_t n);  // _Z13neuron_memcpyPvyj  HBM -> local  (READ)
int neuron_memcpy(uint64_t dst, void    *src, size_t n);  // _Z13neuron_memcpyyPvj  local -> HBM  (WRITE)
```

`[OBSERVED: overload 1 @DIE 0x12c3, low_pc 0x65c, formal dst = void*; overload 2
@DIE 0x1315, low_pc 0x694, formal dst = uint64_t]`

The buffered-reader/writer flush bodies (from `TensorStreamAccessor.h`) compute an
HBM address as `data_.hbm_addr + sizeof(T) * idx` and hand it to the matching
overload:

```c
// READ refill: HBM -> local 4 KiB buffer  (copy_data_to_buffer)
uint64_t src_data = data_.hbm_addr + sizeof(T) * data_idx_;
size_t   read_size_bytes = sizeof(T) * read_size;          // <= BUFFER_SIZE_BYTES
neuron_memcpy(dst_buffer, src_data, read_size_bytes);      // overload 1 (HBM -> local)

// WRITE flush: local buffer -> HBM  (copy_buffer_to_data)
uint64_t dst_data = data_.hbm_addr + sizeof(T) * (data_idx_ - buffer_idx_);
size_t   write_size_bytes = sizeof(T) * buffer_idx_;
neuron_memcpy(dst_data, src_buffer, write_size_bytes);     // overload 2 (local -> HBM)
```

The TCM accessor (untracked, §4) reaches HBM the same way:

```c
// TensorTcmAccessor.h
void tensor_to_tcm(T *tcm_ptr, size_t off, size_t n) {     // :37  HBM -> TCM
    uint64_t src_data = /* hbm_addr + off */;
    neuron_memcpy(tcm_ptr, src_data, sizeof(T) * n);        // :39  overload 1
}
void tcm_to_tensor(T *tcm_ptr, size_t off, size_t n) {     // :45  TCM -> HBM
    static_assert(!read_only);                              //      write requires a non-read-only accessor
    uint64_t dst_data = /* hbm_addr + off */;
    neuron_memcpy(dst_data, tcm_ptr, sizeof(T) * n);        // :48  overload 2
}
```

`neuron_memcpy` is referenced (undefined) from `wrapper_api.o` and defined in
`data_transfer.o`; `nm | rg -c neuron_memcpy` = **3** in `data_transfer.o`
(two overloads + the impl helper), **2** in `wrapper_api.o` (the two undefined
refs). `tcm_malloc`/`tcm_free` are defined in `TensorTcmAccessor.o`
(`_ZN2at6neuron10tcm_mallocEj` / `8tcm_freeEPv`), backed by undefined
`neuron_dataram_allocate` / `neuron_dataram_deallocate`. `[OBSERVED]`

Under the hood `neuron_memcpy` dispatches through a 3-entry function table:

```c
// data_transfer.cpp — anonymous-namespace dispatch state.  [OBSERVED]
typedef void (*DataTransferFunc)(void*, uint64_t, bool, size_t);   // DIE @0xcf, decl :387
const DataTransferFunc data_transfer_method_table[3] = { ... };    // .data @0x200, DIE @0xb9 :392
NeuronMemcpyMethod active_neuron_memcpy_method = DEFAULT_METHOD;    // .data @0x20c, DIE @0xdb :399

enum NeuronMemcpyMethod /* unsigned int; custom_op.h:49 */ {
    C_MEMCPY   = 0,   // c_memcpy_data_transfer    (:2461)
    VEC_MEMCPY = 1,   // vec_memcpy_data_transfer  (:2511, SIMD vec_memcpy path)
    DMA        = 2,   // dma_data_transfer         (:2843, builds an SDMA ring)
    MAX_METHODS    = 3,
    DEFAULT_METHOD = 2   // == DMA
};
// switchable at runtime via neuron_set_memcpy_method(NeuronMemcpyMethod).
```

`[OBSERVED — data_transfer.o DWARF: enumerators C_MEMCPY=0, VEC_MEMCPY=1, DMA=2,
MAX_METHODS=3, DEFAULT_METHOD=2; backends defined as
_Z22c_memcpy_data_transferPvybj / _Z24vec_memcpy_data_transferPvybj /
_Z17dma_data_transferPvybj]` See
[Data-Transfer Backends](./data-transfer-backends.md) for the C/VEC/DMA
implementations and the SDMA ring.

> **NOTE.** The `CoherencyEnforcer` does **not** participate in the transfer. It
> gates only *whether the accessor was allowed to open*; window translation and the
> DMA ring are entirely independent. The enforcer is purely advisory bookkeeping
> *around* these transfers. `[Relationship OBSERVED]`

---

## 7. The coherency model — synthesis

- A tensor's HBM memory can be touched three ways: **cached-read** (buffered
  reader), **cached-write** (buffered writer), or **direct** (unbuffered
  accessor). Caches are not kept mutually coherent, and direct access is not
  coherent with any cache.
- The enforcer encodes the safe-concurrency rules as three counters plus an
  acquire-time compatibility check (§3): *many readers* **or** *many unbuffered*,
  but a *writer is exclusive*, and *cached vs direct never mix*.
- `policy_` chooses the reaction to a violation: `COHERENT` = assert/abort
  (default, fail-loud, no-op under `NDEBUG`), `INCOHERENT_VERBOSE` = `printf` warn
  + allow, `INCOHERENT_QUIET` = silently allow. Policy is per-tensor and settable
  even on a `const` tensor (the `mutable` member).
- It is **single-thread advisory bookkeeping** — no atomics/locks — so it catches
  misuse *patterns* in cooperative code, not real hardware data races. The actual
  data movement is the orthogonal Q7/`neuron_memcpy` path.
- The whole mechanism is **header-only**: the shipped static lib carries the
  16-byte type and the tensor-side plumbing, but the acquire/release/warn bodies
  are instantiated only at customer accessor-use sites — there is no compiled copy
  in the library.

### Reimplementation checklist

1. Place a 16-byte `CoherencyEnforcer` at `TensorBase+0x04` (after the 4-byte
   `impl_`), `mutable`, default-initialised to `{0,0,0,COHERENT}`. `TensorBase`
   must be `0x14` bytes; `at::Tensor` adds nothing.
2. Counters are plain signed `int` at `+0x00/+0x04/+0x08`; `policy_` is a 4-byte
   `unsigned`-underlying enum at `+0x0C`. No padding, no vtable.
3. Acquire in each accessor ctor, release in the dtor; hold the enforcer **by
   reference**; flush a writer's buffer **before** releasing its slot.
4. Decide deliberately whether your TCM accessor participates (the shipped one
   does not).

---

## Confidence ledger

| Claim                                                                            | Confidence | Evidence                                                                 |
| -------------------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------ |
| 16-byte layout, fields `0/4/8/c`, no vtable, no pad                              | HIGH × OBSERVED | DWARF `@0x9f0b` byte_size `0x10`; members `@0x9f14/9f20/9f2c/9f38`     |
| `Policy` = {COHERENT 0, INCOHERENT_VERBOSE 1, INCOHERENT_QUIET 2}, 4B unsigned   | HIGH × OBSERVED | DWARF `@0x9f44` `DW_AT_const_value`                                       |
| Embedded at `TensorBase+0x04`, `mutable`, protected                              | HIGH × OBSERVED | DWARF `@0x9424` member-loc `0x04`; header `:869`; const-`this` `_ZNK` API |
| `+0x04` confirmed at instruction level                                           | HIGH × OBSERVED | inlined ctor `this = DW_OP_breg5 +4` at PC `0x261-0x269` (`@0x138dc`)     |
| 4 accessor families' RAII; TCM not tracked                                       | HIGH × OBSERVED | accessor-header ctor/dtor bodies; `tcm_accessor()` passes no enforcer     |
| `neuron_memcpy` two overloads + 3-entry method table flush path                  | HIGH × OBSERVED | `data_transfer.o` DWARF (DIEs `0x12c3/0x1315/0xb9/0xdb`); stream bodies   |
| Compatibility predicates / per-policy semantics                                  | HIGH × SOURCE   | `Coherence.h:42/62/83`; every method present as a DWARF DIE              |
| `COHERENT` degrades to no-op under `NDEBUG`                                       | MED        | standard `assert()` semantics; bodies un-instantiated in this lib         |
| TCM non-tracking intentional vs oversight                                        | LOW / NOTED | only the contract (no enforcer arg) is observable                        |

**No corrections to SX-ABI-04** — every offset, size, enumerator, symbol, and the
instruction-level `+0x04` proof reproduce exactly against the DWARF and headers.

---

### See also

- [The at::Tensor Object Chain](./tensor-object-chain.md) — the `TensorBase`
  layout that owns the `+0x04` member and the `impl_`/Q7 data path.
- [The Retargeted TensorAccessor](./tensor-accessor.md) — the `operator[]` indexer
  and `Q7Ptr` element access driven through the enforcer.
- [TensorStream + TCM Staging](./tensorstream-tcm.md) — the 4 KiB stream buffer and
  TCM allocation behind the buffered reader/writer.
- [Data-Transfer Backends](./data-transfer-backends.md) — `neuron_memcpy` and the
  C/VEC/DMA dispatch table that materialises the flush.
