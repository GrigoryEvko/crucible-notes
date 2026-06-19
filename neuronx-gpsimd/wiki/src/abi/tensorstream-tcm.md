# TensorStream + TCM Staging

The [retargeted `TensorAccessor`](./tensor-accessor.md) is an *unbuffered* indexer: it
keeps the tensor's data as a [`Q7PtrType`](./q7ptrtype.md) and pays one
`neuron_translate` window-map per element on every leaf touch — correct for random
access, ruinous for a linear sweep. This page documents the two *buffered staging*
accessor families that instead **bulk-copy** a span of the tensor between HBM and a
fast on-core **local buffer** (the Xtensa *dataram* used as tightly-coupled memory,
TCM), then serve elements out of that local buffer with ordinary native pointer
dereferences — **no per-element translate**. The bulk copy goes through
[`neuron_memcpy`](./data-transfer-backends.md), whose default backend is the on-die
SDMA engine.

The two families differ in **who owns the buffer** and **how staging is triggered**:

| Family | Buffer owner | Trigger | Access pattern | Coherency |
|--------|--------------|---------|----------------|-----------|
| `TensorStream<T>` + `Read`/`WriteStreamAccessor<T>` | **library** (auto-allocated 4 KiB dataram) | automatic refill/flush as you sweep | **sequential, forward-only, one pass** | **tracked** (RAII buffered reader/writer) |
| `TensorTcmAccessor<read_only>` | **caller** (`tcm_malloc`) | explicit `tensor_to_tcm`/`tcm_to_tensor` | **arbitrary span**, manual | **NOT tracked** (no enforcer — GOTCHA) |

> **Confidence.** `TensorTcmAccessor`, `tcm_malloc`/`tcm_free`, `neuron_memcpy`, the
> transfer-method table and the dataram allocator are **HIGH × OBSERVED** — DWARF
> `DW_AT_byte_size`/`DW_AT_data_member_location` emitted by the ABI's own clang, ELF
> symtab/`.rela`/`.data` dumps, and native Xtensa disassembly. The three *stream*
> template types carry **no DWARF and no symbols anywhere in the archive** (pure
> header templates, instantiated only at a customer op's compile site); their byte
> layouts are **HIGH × compile-verify** — composed from DWARF-pinned component sizes
> and confirmed under an Xtensa-faithful ILP32 model. Every claim is anchored to a
> DWARF offset, an ELF symbol, a disasm address, or a `header:line`.

All evidence below is from the shipped `aws-neuronx-gpsimd-customop-lib_0.21.2.0`
ELF32-Xtensa objects (`readelf -h` → *Machine: Tensilica Xtensa Processor*,
DWARF v4, `addr_size 0x04`) and the modified ATen/Neuron headers. **32-bit
consequence:** every pointer/reference/`size_t` is 4 bytes, but `uint64`
(`Q7PtrType.hbm_addr`, the SDMA `buf_ptr`) is **8 bytes aligned to 8** on Xtensa — do
not transpose these offsets onto a 64-bit host, and do not model with plain i386
(which 4-aligns `int64` and mis-sizes the structs).

---

## 1. The TCM / dataram region

The local side of every staging copy is the Xtensa **dataram** scratch region, used
as tightly-coupled memory. It is carved by `neuron_dataram_allocate`, which (per
`custom_op.h:35`) *"places the 32-bit local address of allocated memory in `*ptr`"* —
i.e. the buffer pointer is a **native 32-bit NX-local address** the core dereferences
directly, with **no `neuron_translate`**. The HBM side is always the 64-bit
`Q7PtrType.hbm_addr`.

`allocator.o` defines the dataram heap over `data_scratch_map`
(see [Device Memory Allocators](./device-allocators.md)) — **[OBSERVED]** `llvm-nm`:

```
000001d0 T neuron_dataram_allocate(unsigned int, void**, unsigned int)
00000204 T neuron_dataram_deallocate(void*)
0000018c T init_neuron_dataram_allocator()
         U data_scratch_map
         U xmem_heap_alloc / xmem_heap_free / xmem_heap_init
```

The TCM allocator surfaced to kernels, `at::neuron::tcm_malloc`/`tcm_free`, is a thin
wrapper over these. **[OBSERVED]** native Xtensa disasm of `TensorTcmAccessor.o`
(`XTENSA_SYSTEM=…/ncore2gp/config XTENSA_CORE=ncore2gp xtensa-elf-objdump -d -r`):

```
00000000 <at::neuron::tcm_malloc(unsigned int)>:
   0:  entry   a1, 48
   3:  { const16 a3,0 ; movi a12,0 ; addi.a a11,a1,12 ; mov.a a10,a2 }   ; ptr=NULL, &ptr=sp+12, a10=nbytes
        R_XTENSA_SLOT0_ALT  neuron_dataram_allocate(unsigned int, void**, unsigned int)
  1b:  callx8  a3                                                        ; ret = neuron_dataram_allocate(nbytes,&ptr,0)
  1e:  beqz.n  a10, 24                                                   ; if ret==0 -> load ptr
  20:  s32i.n  a2, a1, 12
  22:  retw.n                                                            ; failure path: return 0/NULL
24:    l32i.n  a2, a1, 12                                                ; return *ptr (the 32-bit dataram addr)
26:    retw.n

00000028 <at::neuron::tcm_free(void*)>:
  28:  entry   a1, 32
  2b:  { beqz.w15 a2, 3c ; mov.a a10,a2 }                                ; if ptr==NULL skip
  33/36: const16 a3,0 ; R_XTENSA_SLOT0_*  neuron_dataram_deallocate(void*)
  39:  callx8  a3                                                        ; neuron_dataram_deallocate(ptr)
3c:    retw.n
```

So **TCM == dataram scratch**: `tcm_malloc(n)` returns the 32-bit dataram address from
`neuron_dataram_allocate`; `tcm_free` is `neuron_dataram_deallocate`. **[OBSERVED]**
both are `T` (DEFINED) symbols at `0x00`/`0x28` in `TensorTcmAccessor.o`
(`nm | rg -c ' T '` = 2).

> **NOTE — the dataram address window.** `data_transfer.o`'s `dram_addr_to_soc_addr`
> (the function that converts a dataram pointer to a SoC-bus address for the DMA
> engine) carries an assertion string in `.data`: **[OBSERVED]** `readelf -p .data`:
> `data_transfer.cpp:160  dram_addr >= 0x80000 && dram_addr < 0x90000`. The dataram /
> TCM staging window is therefore the **64 KiB region `[0x80000, 0x90000)`** in the
> core's local address space. A `tcm_malloc` result outside this range would trap the
> SDMA path. (Not in SX-ABI-05 — recovered here from the assertion literal.)

> **GOTCHA — the buffer is a *single* 4 KiB bounce buffer, not a true double-buffer.**
> Despite the "double-buffer" framing in the task brief and SX-ABI-05's loose
> "double-of-nothing" phrasing, the header defines exactly **one** owned buffer
> (`buffer_ptr_`) that is **refilled in place** (`buffer_idx_ = 0` after each
> `neuron_memcpy`) — there is no ping-pong second buffer and no overlap of DMA with
> compute. **[OBSERVED — header]** `TensorStreamAccessor.h:142` declares a single
> `BufferUPtrType buffer_ptr_`; `copy_data_to_buffer`/`copy_buffer_to_data`
> (h:80/102) reuse it. A reimplementer adding genuine double-buffering (alloc two
> 4 KiB regions, issue the next refill while consuming the current) would be a
> *latency* improvement, not a behavioural change — the ABI surface is single-buffer.

> **Geometry — 4 KiB, confirmed.** `#define BUFFER_SIZE_BYTES (4*1024)` at
> `TensorStreamAccessor.h:18`; the allocation is `neuron_dataram_allocate(BUFFER_SIZE_BYTES, (void**)&ptr)`
> at `:150`; capacity in **elements** is `BUFFER_SIZE_BYTES / sizeof(T)` (`:149`) — e.g.
> 1024 for `float`, 4096 for `u8`. **[OBSERVED — header]**. Alignment of the buffer is
> whatever the dataram heap returns; the *struct* is 8-aligned to satisfy `data_`'s
> `uint64`.

---

## 2. Struct layouts (DWARF-confirmed where present)

### 2.1 `at::TensorTcmAccessor<read_only>` — 16 bytes — **[OBSERVED, DWARF-direct]**

Directly present in `wrapper_api.o` DWARF (the `tcm_accessor()` factory references the
instantiations, so their DIEs are emitted):

```
<a152> DW_TAG_class_type   DW_AT_calling_convention: pass_by_value
       DW_AT_name        : TensorTcmAccessor<true>
       DW_AT_byte_size   : 16            ← OBSERVED
       DW_AT_decl_file/line: TensorTcmAccessor.h:30
  <a15b> DW_TAG_member  data_  type <0x11b16>  DW_AT_data_member_location: 0
  <a17a> DW_TAG_template_value_param  read_only  type <0x11aff>  DW_AT_const_value: 1
  <a167> DW_TAG_subprogram TensorTcmAccessor   ← ctor
    formal_parameter <0x12ac0>  artificial      ← this
    formal_parameter <0x11b25>                  ← const Q7PtrType &   (NO enforcer!)
```

| off | size | member | type | conf | anchor |
|----:|-----:|--------|------|------|--------|
| `0x00` | `0x10` | `data_` | `const c10::Q7PtrType` (by value, a copy) | HIGH | OBSERVED `DW_AT_data_member_location 0`; type `<0x11b16>` = `const_type → <0x169b>` = `Q7PtrType` (`byte_size 16`, pass-by-value) |
|  | | **total** | **0x10 = 16 bytes** | HIGH | OBSERVED `DW_AT_byte_size 16` |

`read_only` is a **`DW_TAG_template_value_param`** (`<a17a>`, `const_value 1` for
`<true>`, `0` for `<false>`) — a type-level write-permission flag with **no storage**.
Both `<true>` and `<false>` are 16 bytes, layout-identical. **No vtable, no enforcer
member.**

`Q7PtrType` is `{hbm_addr@0x00 uint64, ctx_ptr@0x08 void*, is_nullptr@0x0C bool; 16B}`
(cite [Q7PtrType + Lazy Translation](./q7ptrtype.md)).

> **GOTCHA — the staging asymmetry.** `TensorTcmAccessor`'s ctor takes **only** a
> `const Q7PtrType&` (DWARF formal param `<0x11b25>` = reference → `const Q7PtrType`).
> It carries **no `CoherencyEnforcer` member and no enforcer reference** — unlike every
> other accessor in this ABI. This is the same asymmetry flagged in the
> [CoherencyEnforcer](./coherency-enforcer.md) page, now confirmed a second way
> directly from the accessor's own DIE (§4). A kernel holding a `TensorTcmAccessor`
> *and* a buffered/unbuffered accessor on the same tensor is **invisible to the
> coherency check** and will not be caught.

### 2.2 `at::TensorStream<T>` — 48 bytes (`T=float`) — **[HIGH × compile-verify]**

No DWARF (header-only; `nm`/`.debug_str` over all 10 archive members = **zero**
`Tensor*Stream*` hits). Layout composed from DWARF-pinned components
(`Q7PtrType=0x10`, `int/size_t/ptr=4`, `unique_ptr<T, fn-ptr deleter>=8`) and
confirmed by `offsetof`/`sizeof` `static_assert`s under an Xtensa-faithful ILP32 model
(`gcc -m32`, `int64` force-aligned to 8). Member order = declaration order
(`TensorStreamAccessor.h:132-144`):

| off | size | member | type | role |
|----:|-----:|--------|------|------|
| `0x00` | `0x10` | `data_` | `const Q7PtrType` | tensor data in HBM, **by value** |
| `0x10` | `0x04` | `data_size_` | `const size_t` | total **element** count (`nbytes()/sizeof(T)`) |
| `0x14` | `0x04` | `data_idx_` | `size_t` | cursor into the tensor (elements consumed/produced) |
| `0x18` | `sizeof(T)` | `backup_buffer_` | `T` | inline 1-element fallback if dataram alloc fails |
| `0x1c` | `0x08` | `buffer_ptr_` | `unique_ptr<T, void(*)(T*)>` | **owned 4 KiB dataram buffer** (or `&backup_buffer_`); `{ptr,deleter}=8B`, no EBO with a function-pointer deleter |
| `0x24` | `0x04` | `buffer_size_` | `const size_t` | capacity in **elements** (`4096/sizeof(T)`, or 1 on fallback) |
| `0x28` | `0x04` | `buffer_idx_` | `size_t` | cursor into the local buffer |
| `0x2c` | `0x04` | *(tail pad)* | — | round to align 8 (`data_`'s `uint64`) |
|  | | **total** | **0x30 = 48 bytes**, align 8 | `sizeof(TensorStream<float>)==48` ✓ |

`sizeof(T)` shifts `buffer_ptr_`/`buffer_size_`/`buffer_idx_`; for `T=u8` the size
still resolves to 48 (`backup_buffer_@0x18` 1B, `buffer_ptr_` re-aligns to `0x1c`).
**Move-only** (copy ctor/assign deleted, move defaulted — h:39-42) because it owns a
`unique_ptr`.

### 2.3 `Read`/`WriteStreamAccessor<T>` — 56 bytes (`T=float`) — **[HIGH × compile-verify]**

Both have **identical** layout (h:212-214 / 267-269):

| off | size | member | type | role |
|----:|-----:|--------|------|------|
| `0x00` | `0x30` | `stream_` | `TensorStream<T>` | the engine (48 B, §2.2) |
| `0x30` | `0x04` | `coherency_enforcer_` | `CoherencyEnforcer&` | **reference** to the tensor's mutable enforcer (TensorBase+0x04), shared with all accessors on that tensor — **not copied** |
| `0x34` | `0x04` | *(tail pad)* | — | align 8 |
|  | | **total** | **0x38 = 56 bytes**, align 8 | `sizeof(...<float>)==56` ✓ |

No vtable (neither stream accessor is polymorphic — unlike the unbuffered
`TensorAccessor`, which *is*). Move-only.

---

## 3. The staging mechanism (annotated C pseudocode)

Staging is **explicit DMA/memcpy** in both families (not hardware-managed coherent
cache). The copy direction is encoded purely by **which `neuron_memcpy` overload** is
chosen — i.e. which argument is the `uint64` HBM address. Both overloads are DEFINED
in `data_transfer.o` (**[OBSERVED]** `nm -C`):

```
0000065c T neuron_memcpy(void*, unsigned long long, unsigned int)   _Z13neuron_memcpyPvyj   // HBM(src)  -> dataram(dst)  READ
00000694 T neuron_memcpy(unsigned long long, void*, unsigned int)   _Z13neuron_memcpyyPvj   // dataram(src) -> HBM(dst)  WRITE
```

### 3.1 `TensorStream` — library-managed sequential staging

The stream owns the 4 KiB buffer and auto-refills (read) / auto-flushes (write) as the
caller sweeps forward. Bodies are header-only (`TensorStreamAccessor.h`); they have
**no symbols**, so they cannot be natively disassembled here — the pseudocode below is
the verbatim header logic with the OBSERVED `neuron_memcpy` overloads and the
DWARF-pinned offsets.

```c
/* allocate_buffer()  — TensorStreamAccessor.h:146-160  [OBSERVED header] */
buffer_info allocate_buffer(TensorStream<T> *s) {
    T  *ptr;
    void (*deleter)(T*) = dataram_deleter;              /* real free  */
    size_t size = BUFFER_SIZE_BYTES / sizeof(T);        /* 4096/sizeof(T) elements */
    int ret = neuron_dataram_allocate(BUFFER_SIZE_BYTES, (void**)&ptr);  /* 4 KiB dataram */
    if (ret != 0) {                                     /* alloc failed -> degrade gracefully */
        ptr     = &s->backup_buffer_;                   /* the inline 1-T member (@0x18) */
        deleter = buffer_empty_deleter;                 /* no-op: do NOT free a struct member */
        size    = 1;                                    /* every element is then a 1-elem memcpy */
    }
    return (buffer_info){ unique_ptr<T>(ptr, deleter), size };
}

/* READ refill  — copy_data_to_buffer()  h:80-97  [OBSERVED header] */
int copy_data_to_buffer(TensorStream<T> *s) {
    if (is_closed(s)) return -1;                        /* !buffer_ptr_ */
    void    *dst = s->buffer_ptr_.get();                /* 32-bit dataram, native deref */
    uint64_t src = s->data_.hbm_addr + sizeof(T) * s->data_idx_;   /* HBM byte addr  <-- OFFSET MATH */
    size_t   remaining = s->data_size_ - s->data_idx_;
    size_t   read = (remaining < s->buffer_size_) ? remaining : s->buffer_size_;
    neuron_memcpy(dst, src, sizeof(T) * read);          /* HBM -> dataram   (READ overload _Z…Pvyj) */
    s->buffer_idx_ = 0;                                 /* rewind local cursor — refill IN PLACE */
    return 0;
}

/* WRITE flush/evict  — copy_buffer_to_data()  h:102-117  [OBSERVED header] */
int copy_buffer_to_data(TensorStream<T> *s) {
    if (is_closed(s)) return -1;
    uint64_t dst = s->data_.hbm_addr + sizeof(T) * (s->data_idx_ - s->buffer_idx_); /* HBM <-- OFFSET MATH */
    void    *src = s->buffer_ptr_.get();
    neuron_memcpy(dst, src, sizeof(T) * s->buffer_idx_);/* dataram -> HBM  (WRITE overload _Z…yPvj) */
    s->buffer_idx_ = 0;
    return 0;
}

/* leaf access — next_element()  h:123-130  [OBSERVED header] */
T* next_element(TensorStream<T> *s) {
    s->data_idx_++;
    return &s->buffer_ptr_.get()[s->buffer_idx_++];     /* PLAIN local dataram deref — NO neuron_translate */
}
```

**Read loop** (`TensorReadStreamAccessor`): ctor acquires a buffered-reader slot and
**primes** the buffer; `read()` refills only at end-of-buffer:

```c
/* ctor  h:166-171 */
acquire_buffered_reader(coherency_enforcer_);   /* RAII coherency token */
copy_data_to_buffer(&stream_);                  /* prime: first 4 KiB burst */

/* hot path  h:187-200 — sweep the whole tensor */
T read(TensorReadStreamAccessor<T> *a) {
    if (C10_UNLIKELY(end_of_buffer(&a->stream_)))   /* buffer_idx_ >= buffer_size_ */
        copy_data_to_buffer(&a->stream_);           /* REFILL: one bulk DMA for the next 1024 floats */
    return *next_element(&a->stream_);              /* else: pure dataram deref */
}
/* dtor/close  h:204-209 */ release_buffered_reader(); stream_.close();
```

**Write loop** (`TensorWriteStreamAccessor`): accumulate in dataram, evict on
buffer-full and on close/dtor:

```c
/* ctor  h:220-223 */ acquire_buffered_writer(coherency_enforcer_);    /* EXCLUSIVE slot */

/* hot path  h:241-254 */
void write(TensorWriteStreamAccessor<T> *a, T value) {
    if (C10_UNLIKELY(end_of_buffer(&a->stream_)))
        copy_buffer_to_data(&a->stream_);           /* FLUSH the full 4 KiB to HBM */
    *next_element(&a->stream_) = value;             /* append into dataram */
}
/* close/dtor  h:258-265 */
copy_buffer_to_data(&stream_);                      /* FINAL flush of the partial buffer */
release_buffered_writer();
stream_.close();
```

`close()` is **idempotent** (guarded by `is_closed()` = `!buffer_ptr_`): the accessor's
`close()` calls `stream_.close()` once, then `~TensorStream` calls it a second time
safely (h:51-65). The summary: **one HBM touch per 4 KiB burst; every element in
between is a native dataram dereference — zero `neuron_translate`.**

### 3.2 `TensorTcmAccessor` — caller-managed manual staging

The kernel allocates its **own** TCM buffer and copies an arbitrary span in/out
explicitly. The accessor holds only the `Q7PtrType` base — it neither allocates nor
tracks the buffer. Method signatures are **[OBSERVED]** in DWARF (manglings
`_ZN2at17TensorTcmAccessorILb1EE13tensor_to_tcmIhEEvPT_jj` and
`…ILb0EE13tcm_to_tensorIhEEvPT_jj` — `PT_jj` = `(T*, unsigned int, unsigned int)`,
confirming `size_t`-width offsets):

```c
/* HBM -> caller TCM   (READ)   tensor_to_tcm<T>   TensorTcmAccessor.h:36-40 */
void tensor_to_tcm(TensorTcmAccessor *a, T *tcm_ptr, size_t off, size_t n) {
    uint64_t src = a->data_.hbm_addr + sizeof(T) * off;     /* <-- OFFSET MATH, same as the stream */
    neuron_memcpy(tcm_ptr, src, sizeof(T) * n);             /* HBM -> dataram  (READ overload) */
}

/* caller TCM -> HBM   (WRITE)  tcm_to_tensor<T>   h:44-49 */
void tcm_to_tensor(TensorTcmAccessor *a, T *tcm_ptr, size_t off, size_t n) {
    static_assert(!read_only, "Can only write to non-const Tensors");  /* COMPILE-time gate, 0 runtime cost */
    uint64_t dst = a->data_.hbm_addr + sizeof(T) * off;
    neuron_memcpy(dst, tcm_ptr, sizeof(T) * n);             /* dataram -> HBM  (WRITE overload) */
}
```

A typical tiling kernel drives it manually — **copy-in → compute → copy-out**:

```c
/* read/write a [off, off+TILE) span through a caller-owned TCM tile */
T *tile = (T*)at::neuron::tcm_malloc(TILE * sizeof(T));     /* dataram, returns 32-bit local addr */
auto in  = src.tcm_accessor();                  /* TensorTcmAccessor<true>  — const& tensor */
auto out = dst.tcm_accessor();                  /* TensorTcmAccessor<false> — & tensor */

for (size_t off = 0; off < numel; off += TILE) {
    size_t n = min(TILE, numel - off);
    in.tensor_to_tcm<T>(tile, off, n);          /* HBM -> TCM : one bulk SDMA */
    for (size_t i = 0; i < n; i++)              /* compute on the tile — pure dataram derefs */
        tile[i] = f(tile[i]);
    out.tcm_to_tensor<T>(tile, off, n);         /* TCM -> HBM : one bulk SDMA */
}
at::neuron::tcm_free(tile);
```

`read_only=true` makes `tcm_to_tensor` a **compile error** (the `static_assert`) — write
protection for `const` tensors, costing nothing at runtime.

---

## 4. The factory — `TensorBase::{read_stream,write_stream,tcm}_accessor()`

`TensorBase.h:590-615` (header-inline; the `tcm_accessor` instantiations are referenced
by a DEFINED function in `wrapper_api.o`, which is why their DIEs exist — §2.1):

```cpp
// BUFFERED READER  (const& -> read-only stream)
template<typename T> TensorReadStreamAccessor<T> read_stream_accessor() const&
  { return TensorReadStreamAccessor<T>(q7_data_ptr(), nbytes()/sizeof(T), coherency_enforcer_); }  // :591
template<typename T> ... read_stream_accessor() && = delete;                                       // :595

// BUFFERED WRITER  (non-const & only; exclusive)
template<typename T> TensorWriteStreamAccessor<T> write_stream_accessor() &
  { return TensorWriteStreamAccessor<T>(q7_data_ptr(), nbytes()/sizeof(T), coherency_enforcer_); } // :598
template<typename T> ... write_stream_accessor() && = delete;                                      // :602

// TCM  (const& -> read-only, & -> read/write; NO enforcer passed)
TensorTcmAccessor<true>  tcm_accessor() const& { return {q7_data_ptr()}; }   // :607
TensorTcmAccessor<false> tcm_accessor() &      { return {q7_data_ptr()}; }   // :612
TensorTcmAccessor<false> tcm_accessor() &&     = delete;                     // :615
```

Each argument, grounded:

1. **`q7_data_ptr()` → `Q7PtrType` by value** (the 64-bit HBM base + cached ctx). All
   three families take this. **[HIGH × OBSERVED — DIE in `wrapper_api.o`]**.
2. **`nbytes()/sizeof(T)` = element count**, passed as `data_size` to the stream
   (`TensorBase.h:280`). The stream sweeps the **whole flattened tensor**.
3. **`coherency_enforcer_` by reference** (TensorBase+0x04, mutable) — for the **two
   stream families ONLY**. `tcm_accessor()` passes **nothing but** `q7_data_ptr()`.
   **[HIGH × OBSERVED]** stream ctors take `CoherencyEnforcer&`; the TCM ctor's DWARF
   formal params are only `this` + `const Q7PtrType&`.

**Ref-qualifier overloads:** `const&` yields the read-only flavour (read stream /
`Tcm<true>`), `&` the writable flavour (write stream / `Tcm<false>`), `&&` is deleted
(no accessor on a temporary) — mirroring the `read_only` template bool.

> **NOTE — streams ignore strides (contiguity assumption).** The stream walks
> `data_.hbm_addr + sizeof(T)*idx` **linearly**, ignoring `strides_`. A non-contiguous
> (strided/transposed/broadcast) tensor would be streamed in **storage order, not
> logical order**. Only the unbuffered [`TensorAccessor`](./tensor-accessor.md) honours
> strides. The address math is OBSERVED in §3.1; the non-contiguous case is the
> caller's contract. **[MED]**

---

## 5. Coherency integration — does staging bypass or drive coherency?

It **drives** coherency for the two stream families and **bypasses** it for TCM —
see the [CoherencyEnforcer](./coherency-enforcer.md) page for the full state machine.

- **Stream families = tracked.** The read accessor's ctor calls
  `acquire_buffered_reader()` (then primes); its dtor calls `release_buffered_reader()`
  then `stream_.close()`. The write accessor acquires a buffered *writer* slot, and its
  dtor `copy_buffer_to_data()`-flushes before `release_buffered_writer()`. Per the
  enforcer matrix, many buffered readers may coexist; a buffered **writer is
  exclusive**; and a buffered reader/writer is **incompatible with any unbuffered
  `TensorAccessor`** on the same tensor (the local buffer is not coherent with HBM
  until flush/refill). The acquire-time check is what stops a kernel mixing a stale
  streamed buffer with a direct read. The enforcer is held **by reference** (accessor
  member `@0x30`), shared with every accessor on that tensor. **[HIGH × OBSERVED ctor
  flow + enforcer matrix]**

- **TCM family = NOT tracked.** `TensorTcmAccessor` has no enforcer member, its ctor
  takes only a `Q7PtrType`, and `tcm_accessor()` passes no enforcer — so it neither
  acquires nor releases any slot and is invisible to the enforcer (the §2.1 GOTCHA).
  Whether this is an intentional trusted manual fast-path or a gap cannot be decided
  from the binary; the **contract (no enforcer) is OBSERVED** from both the factory and
  the accessor's own DIE. **[HIGH × OBSERVED contract, LOW intent]**

The enforcer never touches the data path — it is advisory single-thread bookkeeping
(no atomics/locks). The actual movement is the orthogonal `neuron_memcpy`/SDMA path
(§6). Coherency gates *whether* an accessor may open; it does not order the DMAs.

---

## 6. The transfer backend (default = SDMA)

Every staging copy funnels through `neuron_memcpy`, which dispatches through a 3-entry
function table that **defaults to the SDMA hardware path** (full detail on the
[Data-Transfer Backends](./data-transfer-backends.md) page). **[OBSERVED]**
`data_transfer.o` `.rela.data` + `.data`:

```
data_transfer_method_table : DataTransferFunc[3] @ .data 0x200
  [0] @0x200  R_XTENSA_32 -> c_memcpy_data_transfer    (_Z22c_memcpy_data_transferPvybj)
  [1] @0x204  R_XTENSA_32 -> vec_memcpy_data_transfer  (_Z24vec_memcpy_data_transferPvybj)
  [2] @0x208  R_XTENSA_32 -> dma_data_transfer         (_Z17dma_data_transferPvybj)
active_neuron_memcpy_method : NeuronMemcpyMethod @ .data 0x20c
  .data bytes @0x20c = 02 00 00 00 (LE) = DMA = DEFAULT_METHOD          ← OBSERVED
```

So out of the box, **every stream/TCM copy uses the on-die SDMA engine**
(`enum NeuronMemcpyMethod { C_MEMCPY=0, VEC_MEMCPY=1, DMA=2, MAX_METHODS=3,
DEFAULT_METHOD=2 }`), switchable via `neuron_set_memcpy_method` (DEFINED at `0x6cc`).
All three backends share the signature `(void* dataram_addr, uint64_t soc_addr, bool,
size_t)` — the OBSERVED parameter name `dataram_addr` confirms the local side is
dataram/TCM.

`dma_data_transfer` builds an SDMA `SDMA_CME_BD_DESC` (16 B: `word0`/`word1`/`buf_ptr
uint64`) ring inside a `_dma_ctx_t` (40 B: `m2s_inc_reg`/`s2m_inc_reg` doorbells,
`tx`/`rx`/`comp_desc` rings, tail idx, ring id) and kicks the **M2S** queue for
HBM→dataram (read / refill / `tensor_to_tcm`) and **S2M** for dataram→HBM (write /
flush / `tcm_to_tensor`). The local buffer's 32-bit dataram address is converted to a
SoC-bus address via `dram_addr_to_soc_addr(uint32)→uint64` (which carries the
`[0x80000,0x90000)` range assertion of §1). **[OBSERVED]** `data_transfer.o` links the
runtime-provided `extended_isa::sdk::dma_queue_m2s_offset` / `dma_queue_s2m_offset` as
undefined externals, plus a DEFINED `init_dma_queue`.

---

## 7. Decision rule — when to stage vs index

For the reimplementer, the three access mechanisms partition cleanly by access
pattern:

| Need | Use | Cost model |
|------|-----|------------|
| **Random / single-element** access; no locality | unbuffered [`TensorAccessor`](./tensor-accessor.md) | one `neuron_translate` window-map **per element touched** |
| **Sequential forward sweep** of the whole (contiguous) tensor; set-and-forget | `read_stream_accessor` / `write_stream_accessor` | one 4 KiB SDMA burst per 1024 elements (`float`); native deref in between; auto refill/flush; coherency tracked |
| **Arbitrary span / tiling / blocking**; caller already manages a dataram working set | `tcm_accessor` + `tcm_malloc` | explicit bulk SDMA per span; native deref in between; **caller sequences coherency** |

Rules of thumb:

- **Sweeping linearly?** Stage. Bulk SDMA amortises HBM latency into 4 KiB bursts and
  serves elements as native dataram loads — vs the unbuffered accessor's per-element
  translate. For a contiguous tensor this is the default win.
- **Touching a handful of scattered elements?** Index. Staging would DMA a whole 4 KiB
  page to serve one element.
- **Already tiling (e.g. blocked matmul/conv)?** Use `TensorTcmAccessor` — you control
  *which* span lands in *which* dataram tile, can keep multiple tiles resident, and
  avoid the stream's one-pass forward-only constraint. The price: **you** must not also
  open a tracked accessor on the same tensor (the enforcer won't catch you).
- **Strided / transposed / broadcast tensor?** The streams will copy in **storage
  order**, not logical order — fall back to the stride-aware `TensorAccessor`, or
  `tcm_accessor` with manually computed offsets.

---

## 8. Why this design

- **Bulk DMA beats per-element translate for sequential access.** The unbuffered
  accessor pays a `neuron_translate` per element — fine for random access, terrible for
  a linear sweep. The stream family amortises HBM access into 4 KiB SDMA bursts into
  fast dataram, then serves elements as native local derefs.
- **Two levels of control.** `TensorStream` = "set it and forget it" sequential
  auto-refill/flush; `TensorTcmAccessor` = full manual control of which span lands in
  which caller-owned tile — same `neuron_memcpy`/SDMA underneath.
- **DATARAM == TCM.** `tcm_malloc` is literally `neuron_dataram_allocate`; the on-core
  Xtensa data RAM (`[0x80000,0x90000)`) is the tightly-coupled memory. HBM is reached
  only by SDMA (default), addressing the dataram buffer via `dram_addr_to_soc_addr`.
- **Coherence for free (streams).** The stream accessors are RAII coherence tokens —
  acquire in ctor, release on close/dtor — so the enforcer's acquire-time matrix stops
  a kernel mixing a stale streamed buffer with a direct read. TCM staging opts out
  (untracked) — a trusted manual fast-path.
- **Header-only, near-zero vendor footprint.** The stream templates emit nothing in the
  lib (zero symbols); they materialise at customer instantiation. The lib ships the
  engine (`neuron_memcpy` + 3 backends + SDMA descriptor builder + dataram allocator +
  `tcm_malloc`/`free`) and the types (`Q7PtrType`/`CoherencyEnforcer` + the
  instantiated `TensorTcmAccessor<bool>` DIEs).

---

## 9. Corrections to SX-ABI-05

- **CORRECTION (terminology) — single bounce buffer, not a double-buffer.** SX-ABI-05's
  title and §1 describe a *"4 KiB double-of-nothing single buffer"*, and the task brief
  labels it a "double-buffer". The header (`TensorStreamAccessor.h:142,150`) defines
  exactly **one** owned 4 KiB dataram buffer, refilled in place (`buffer_idx_=0`); there
  is no second buffer and no DMA/compute overlap. The correct term is a **single
  bounce/staging buffer**. The 4 KiB figure itself (`BUFFER_SIZE_BYTES (4*1024)`,
  h:18) is confirmed. **[OBSERVED — header]**

- **ADDITION (not in SX-ABI-05) — the dataram address window `[0x80000, 0x90000)`.**
  Recovered from the `dram_addr_to_soc_addr` range assertion literal in
  `data_transfer.o` `.data` (`data_transfer.cpp:160`). SX-ABI-05 grounds the dataram
  region only to `data_scratch_map`; this pins the 64 KiB local window the SDMA path
  requires TCM buffers to live in. **[OBSERVED — `readelf -p .data`]**

Otherwise this page corroborates SX-ABI-05 exactly: `TensorTcmAccessor` = 16 B with a
single `const Q7PtrType@0x00` and a no-storage `read_only` template flag and **no
enforcer**; `tcm_malloc`/`tcm_free` wrap `neuron_dataram_(de)allocate`; both
`neuron_memcpy` overloads DEFINED; method table defaults to `DMA`; stream layouts
48 B / 56 B by compile-verify.

---

## See also

- [The Retargeted TensorAccessor](./tensor-accessor.md) — the unbuffered per-element indexer this page contrasts with.
- [The CoherencyEnforcer](./coherency-enforcer.md) — the buffered-reader/writer matrix the stream families drive (and TCM bypasses).
- [Data-Transfer Backends](./data-transfer-backends.md) — `neuron_memcpy`, the 3-entry method table, and the SDMA M2S/S2M descriptor path.
- [Device Memory Allocators](./device-allocators.md) — the dataram (TCM) heap over `data_scratch_map` behind `tcm_malloc`.
- [Q7PtrType + Lazy Translation](./q7ptrtype.md) — the `{hbm_addr, ctx_ptr, is_nullptr}` device pointer every accessor stores.
