# customop_* Marshalling Entries

> **Scope.** This page documents the **front door** of the GPSIMD custom-op ABI:
> the handful of `customop_*` C entry points (plus the `get_dst_tensor` utility)
> that a generated custom-op kernel calls to (1) pull its arguments out of the
> host-built TPB argument stream, (2) materialise each `NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR`
> descriptor into a device `at::Tensor` (or `c10::Scalar` / scalar), (3) run, and
> (4) write its result tensor back to HBM. Every entry lives in one archive member,
> `libneuroncustomop.a(wrapper_api.o)`. The deep machinery these entries call —
> the `Q7PtrType` pointer, the `NeuronTensorImpl`/`NeuronStorageImpl` object chain,
> and the ISA-dtype → `ScalarType` table — is owned by sibling pages and only
> re-grounded here where it touches the marshalling flow.
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read from
> symtab / relocs / DWARF / native Xtensa disasm), `INFERRED` (a C-ABI rule
> applied to an observed offset), `CARRIED` (taken from a shared Part-7 pack and
> re-confirmed here only where it intersects this page).

> **NOTE — artifacts & tooling.** All claims are derived **solely from static
> analysis** of the shipped `aws-neuronx-gpsimd-customop-lib_0.21.2.0` package:
> the static archive `custom_op/neuron/libneuroncustomop.a`, member
> `wrapper_api.o` — **ELF 32-bit LSB, Tensilica Xtensa, not stripped**, with
> **DWARF v4** emitted by the ABI's own compiler (`XtensaTools-14.09 clang
> 10.0.1`). Native disassembly used the in-tree Xtensa binutils with the device
> core registered:
> ```
> export XTENSA_SYSTEM=.../gpsimd_tools/tools/XtensaTools/config
> export XTENSA_CORE=ncore2gp
> xtensa-elf-objdump -dr wrapper_api.o
> ```
> `wrapper_api.o` is a **relocatable object**: every code address below is
> `.text`-section-relative (the section starts at offset `0`), so there is no VMA
> delta to reason about. Recovered strings carry source-path tags
> (`.../custom_op/library/<file>.hpp:<line>`); these are quoted **as recovered
> binary strings** — assertion messages and `__FILE__`/`__LINE__` literals the
> compiler baked into `.rodata` — not from any external tree.

---

## 0. Entry inventory

`nm wrapper_api.o | rg ' [Tt] '` lists exactly nine externally-visible code
entries with the marshalling role; all are `extern "C++"` but trivially
`extern "C"`-callable from generated code because they take/return POD-by-value
or a single reference:

| `.text` off | mangled symbol | demangled signature | role |
|---|---|---|---|
| `0x224` | `_Z14customop_setupb` | `customop_setup(bool)` | one-time init; arms the DMA queues, binds the arg stream |
| `0x35c` | `_Z20customop_next_tensorv` | `customop_next_tensor()` | pull next arg as `at::Tensor` |
| `0x390` | `_Z20customop_next_scalarv` | `customop_next_scalar()` | pull next arg as `c10::Scalar` |
| `0x3c4` | `_Z17customop_next_intv` | `customop_next_int()` | pull next arg as `int` |
| `0x3fc` | `_Z22customop_next_longlongv` | `customop_next_longlong()` | pull next arg as `long long` |
| `0x43c` | `_Z19customop_next_floatv` | `customop_next_float()` | pull next arg as `float` |
| `0x47c` | `_Z22customop_return_tensorRN2at6TensorE` | `customop_return_tensor(at::Tensor&)` | write a result tensor back to its output descriptor |
| `0x528` | `_Z14get_dst_tensorv` | `get_dst_tensor()` | materialise the **output** descriptor as a writable `at::Tensor` |
| `0x5d4` | `_Z16customop_cleanupv` | `customop_cleanup()` | drain/sync DMA queues at op exit |

`[HIGH × OBSERVED]` — addresses and signatures from `nm`/`c++filt` on
`wrapper_api.o`. That is **8 `customop_*` entries + `get_dst_tensor`**, matching
the Part-7 estimate.

> **NOTE.** The launch handoff — switching the Q7 core onto the kernel's stack
> before any `customop_next_*` runs and back afterwards — is **not** in
> `wrapper_api.o`. It lives in `stack_switch.o` / `switch_stack.o`. See
> [Stack Switch ABI](stack-switch.md); this page assumes the kernel is already
> executing on its switched stack when it first calls a `customop_*` entry.

These nine entries are thin: each is a fixed-shape prologue + one or two
`callx8` into a worker. The real work is in three workers, also in
`wrapper_api.o`, that the entries dispatch to:

| worker | role | reloc-count anchor |
|---|---|---|
| `ArgParser::next_ucode_arg<UTensor>()` (`_ZN9ArgParser14next_ucode_argI7UTensorEET_v`) | advance the arg cursor, read one ARG_TENSOR into a stack `UTensor` | `.rela.text.…next_ucode_arg…` = **18** relocs |
| `UTensor::operator at::Tensor() const` (`_ZNK7UTensorcvN2at6TensorEEv`) | build the device `at::Tensor` from a `UTensor` | `.rela.text._ZNK7UTensorcvN2at6TensorEEv` = **74** relocs |
| `UTensor::copy(at::Tensor const&)` (`_ZN7UTensor4copyERKN2at6TensorE`) | stage a tensor through dataram and DMA it to HBM | `.rela.text._ZN7UTensor4copyERKN2at6TensorE` = **101** relocs |

`[HIGH × OBSERVED]` — `readelf -r wrapper_api.o` reports these three relocation
sections at 18 / 74 / 101 entries respectively. These counts are diagnostic: the
74-reloc COMDAT is the full `make_intrusive` construction chain, and the
101-reloc copy is the dual-direction `neuron_memcpy` writeback (both detailed
below).

---

## 1. The static state — three anonymous-namespace globals in `.bss`

The marshalling layer is **stateful across the entries** through exactly three
file-local objects. `readelf -sW wrapper_api.o` resolves them by name (they are
`LOCAL OBJECT` in section `.bss`, total `.bss` size `0x810`):

| `.bss` off | size | symbol (demangled) | meaning |
|---|---|---|---|
| `0x608` | `512` | `(anonymous namespace)::arg_parser` | the `ArgParser` singleton — holds `curr_arg_`, the bound `isa_args` view, the raw arg blob |
| `0x808` | `1` | `(anonymous namespace)::get_dst_tensor_called` | flag: has the kernel already pulled the output tensor via `get_dst_tensor()`? |
| `0x80c` | `4` | `(anonymous namespace)::retval` | 4-byte pointer to the **output** `ARG_TENSOR` descriptor |

`[HIGH × OBSERVED]` — symbol table of `wrapper_api.o`. The `512`-byte size of
`arg_parser` is the recovered `sizeof(ArgParser)` for this build.

> **GOTCHA — `retval` is a *pointer*, not a tensor.** `(anon)::retval` @`0x80c`
> is a single 32-bit word (this is a 32-bit Xtensa target; `void*` is 4 bytes
> `[CARRIED × OBSERVED]`). `get_dst_tensor` and `customop_return_tensor` both do
> `l32i.n a6, retval, 0` to *load the descriptor pointer*, then copy the 48-byte
> descriptor it points at onto the stack. Do not confuse it with a stored
> `at::Tensor`.

The `get_dst_tensor_called` flag implements a **one-output-claim** protocol
(§6): if the kernel called `get_dst_tensor()` it already owns and will mutate the
output in place, so a later `customop_return_tensor()` must be a no-op — and vice
versa.

---

## 2. Host → device wire layout: `NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR` (48 B)

Every argument — tensor *or* scalar — arrives as a 48-byte `ARG_TENSOR`
descriptor. The DWARF `DW_TAG_structure_type` for
`NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR` (DIE `wrapper_api.o @0x12f3a`,
`DW_AT_byte_size = 48`) gives the byte-exact layout:

```c
/* DWARF: NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR, byte_size = 48  [HIGH × OBSERVED] */
typedef struct {
    uint8_t  location;             /* +0  ARG_LOCATION_{INVALID=0, SBUF=1, HBM=2}      */
    uint8_t  framework_shape_type; /* +1  SHAPE_TYPE_{INLINE_8D=0, OUT_OF_LINE=1, INLINE_4D=2} */
    uint8_t  dtype;                /* +2  NEURON_ISA_TPB_DTYPE (ISA dtype enum)        */
    uint8_t  reserved0;            /* +3                                               */
    union  shape framework_shape;  /* +8  16 B: inline 4D/8D dims, or out-of-line ref  */
    union  storage storage;        /* +24 16 B: HBM or TPB storage descriptor          */
    uint8_t  reserved1[8];         /* +40                                              */
} arg_tensor_t;                    /* sizeof == 48                                     */
```

`[HIGH × OBSERVED]` — every offset/name from the DWARF member DIEs of
`@0x12f3a`. The three enum tables that decode the byte fields are recovered in
full from their own `DW_TAG_enumeration_type` DIEs:

```c
/* DWARF enums  [HIGH × OBSERVED] */
enum NEURON_ISA_TPB_CUSTOM_OP_ARG_LOCATION { INVALID=0, SBUF=1, HBM=2 };
enum NEURON_ISA_TPB_CUSTOM_OP_TENSOR_SHAPE_TYPE { INLINE_SHAPE8D=0, OUT_OF_LINE_SHAPE=1, INLINE_SHAPE4D=2 };
enum NEURON_ISA_TPB_DTYPE {
    INVALID=0, UINT64=1, INT8=2,  UINT8=3,  INT16=4,  UINT16=5, BFLOAT16=6, FP16=7,
    INT32=8,   UINT32=9, FP32=10, FP32R=11, INT64=12, FP8_EXP3=13, FP8_EXP4=14, FP8_EXP5=15
};
```

The `storage` union (16 B, DIE `NEURON_ISA_TPB_CUSTOM_OP_TENSOR_STORAGE_UNION`)
has two arms, both starting at descriptor offset `+24`:

```c
/* HBM arm (location == HBM)  [HIGH × OBSERVED] */
struct hbm_storage  { uint64_t addr;  /* +24 */  uint32_t num_elem;  /* +32 */  uint32_t reserved0; /* +36 */ };
/* TPB/SBUF arm (location == SBUF)  [HIGH × OBSERVED] */
struct tpb_storage  { uint32_t addr;  /* +24 */  uint32_t num_elem;  /* +28 */
                      uint8_t  num_partitions; /* +32 */  uint8_t reserved0[3];
                      uint32_t num_elem_per_block; /* +36 */ };
```

> **QUIRK — the HBM address is 64-bit even though the core is 32-bit.** The HBM
> arm's `addr` is a `uint64_t` at descriptor `+24` (low word `+24`, high word
> `+28`). The Q7 core cannot dereference it directly; it is handed to
> `neuron_translate` for windowed access (see [Q7PtrType + Lazy Translation](q7ptrtype.md)).
> The disassembly confirms this width: `return_tensor` loads the addr as **two**
> `l32i` words (`a6+24` low, `a6+28` high) and does a carry-propagating add
> (`add.n` + `saltu` carry) when offsetting it. `[HIGH × OBSERVED]`

> **CORRECTION — descriptor `+0` is `location`, *not* a transfer method.** An
> early read of `customop_return_tensor` saw it branch on
> `extui a5, word0, 0, 8` with `beqi a5,2` / `bnei a5,1`, which *looks* like the
> `data_transfer_method_table` ({C_MEMCPY=0, VEC_MEMCPY=1, DMA=2}). DWARF settles
> it: descriptor offset `+0` is `location` (`INVALID=0/SBUF=1/HBM=2`), so that
> branch is **storage-location resolution** (SBUF needs the `_sbuf_window` base
> added; HBM is used directly), not method selection. The transfer method is
> chosen *deeper*, inside `neuron_memcpy` from `(anon)::active_neuron_memcpy_method`
> in `data_transfer.o`. The numeric collision (both run `0/1/2`) is what made the
> two look alike. `[HIGH × OBSERVED]`

### 2.1 Walking the arg stream — `ArgParser::next_ucode_arg<UTensor>()`

Every `customop_next_*` entry first calls the **same** templated reader,
`ArgParser::next_ucode_arg<UTensor>()` (reloc target in all five next-entries),
passing `&arg_parser` (`.bss+0x608`) in `a11` and a stack-`UTensor` out-pointer
in `a10`:

```c
/* customop_next_<T> prologue, shared shape  [HIGH × OBSERVED]
 * e.g. customop_next_tensor @0x35c, customop_next_int @0x3c4 */
UTensor u;                                   /* stack slot a1+8 */
ArgParser::next_ucode_arg<UTensor>(&u, &arg_parser);  /* callx8, a10=&u, a11=&arg_parser */
```

The single-template design is the key structural fact: **there is no per-scalar
reader**. `next_ucode_arg<UTensor>` returns one `UTensor`; what differs per entry
is *which conversion operator* runs on that `UTensor` afterward (§4). Inside
`next_ucode_arg`, the recovered guard strings nail the cursor protocol:

```
arg_parser.hpp:66  "curr_arg_ < isa_args.get_num()"
arg_parser.hpp:67  "isa_args.get_types()[curr_arg_] == NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR"
arg_extractor.hpp:16 "num_args < max_args_"
```

`[HIGH × OBSERVED]` — `strings wrapper_api.o`. So the reader:

```c
/* ArgParser::next_ucode_arg<UTensor>  — reconstructed from guards + disasm  [MED × INFERRED] */
UTensor ArgParser::next_ucode_arg() {
    _Assert(curr_arg_ < isa_args.get_num());                     /* arg_parser.hpp:66 */
    _Assert(isa_args.get_types()[curr_arg_]                       /* arg_parser.hpp:67 */
                == NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR);
    arg_tensor_t *desc = &isa_args.get_args()[curr_arg_];         /* the 48 B ARG_TENSOR */
    curr_arg_ += 1;                                               /* advance cursor */
    return UTensor{ desc };                                       /* wrap, by value */
}
```

> **NOTE.** `next_ucode_arg<UTensor>` itself contains a `location`-style
> `beqi a5,2 / bnei a5,1` branch — it normalises `SBUF`/`HBM` storage addressing
> as it wraps the descriptor, so a `UTensor` already carries a usable base before
> any conversion operator runs.

---

## 3. Tensor construction: `UTensor::operator at::Tensor()` (74 relocs)

`customop_next_tensor()` is two `callx8`s (`@0x35c`):

```c
/* customop_next_tensor @0x35c  [HIGH × OBSERVED] */
at::Tensor customop_next_tensor() {
    UTensor u;
    ArgParser::next_ucode_arg<UTensor>(&u, &arg_parser);  /* @0x375 */
    return (at::Tensor) u;          /* UTensor::operator at::Tensor(), @0x388 */
}
```

The conversion operator is the COMDAT
`_ZNK7UTensorcvN2at6TensorEEv` (74 relocs). Tracing its `R_XTENSA_SLOT0_OP`
call targets **in execution order** gives the construction chain verbatim:

```c
/* UTensor::operator at::Tensor() const — call order from .text._ZNK7UTensorcvN2at6TensorEEv
 * [HIGH × OBSERVED] reloc offsets cited inline */
at::Tensor UTensor::operator at::Tensor() const {
    void *ctx = neuron_translate_ctx();                         /* @0x110 */

    /* (a) wrap raw HBM addr as a Q7PtrType {hbm_addr, ctx, is_nullptr}  [CARRIED: 16 B] */
    Q7PtrType raw = { storage.hbm.addr, ctx /*, is_nullptr=0 */ };

    /* (b) wrap in a UniqueQ7Ptr whose deleter is Q7Deleter::delete_nothing
     *     for an INPUT — the kernel does not own host-provided HBM.          */
    unique_ptr<Q7PtrType, Q7Deleter> uq{ raw, Q7Deleter::delete_nothing };   /* deleter ref @0x12d */

    /* (c) storage: intrusive_ptr<NeuronStorageImpl>::make(num_elem&, move(uq))   28 relocs */
    auto storage =
      c10::intrusive_ptr<NeuronStorageImpl>::make(num_elem, std::move(uq));   /* @0x19c */

    /* (d) impl: intrusive_ptr<NeuronTensorImpl>::make(storage, DispatchKey, TypeMeta)  21 relocs
     *     TypeMeta = caffe2::TypeMeta::fromScalarType(isa_to_torch_dtype(dtype))         */
    auto impl =
      c10::intrusive_ptr<NeuronTensorImpl>::make(std::move(storage),
                                                 dispatch_key,
                                                 TypeMeta(isa_to_torch_dtype(dtype)));  /* @0x226 */

    /* (e) start from the Undefined singleton, then stamp sizes (contiguous)  85 relocs */
    at::Tensor t = at::Tensor(c10::UndefinedTensorImpl::_singleton());        /* @0x251 */
    t.unsafeGetTensorImpl()->set_sizes_contiguous(sizes /*ArrayRef<long long>*/); /* @0x25c */
    /* impl is moved into t; ctx re-fetched @0x2a4 to finalize translation handle */
    return t;
}
```

`[HIGH × OBSERVED]` — the seven non-`.text`/non-`_GLOBAL__N` reloc targets in the
COMDAT, in their disassembly order: `neuron_translate_ctx` → `Q7Deleter::delete_nothing`
→ `intrusive_ptr<NeuronStorageImpl>::make` → `intrusive_ptr<NeuronTensorImpl>::make`
→ `UndefinedTensorImpl::_singleton` → `TensorImpl::set_sizes_contiguous` →
`neuron_translate_ctx`. The maker reloc-counts independently confirm each node:
`NeuronStorageImpl::make` = 28, `NeuronTensorImpl::make` = 21,
`NeuronTensorImpl::C2` = 20, `set_sizes_contiguous` = 85, `shallow_copy_from` = 15.

> **QUIRK — `delete_nothing` makes inputs non-owning.** The deleter wired into
> the input `UniqueQ7Ptr` is `c10::Q7Deleter::delete_nothing(Q7PtrType)`
> (`_ZN3c109Q7Deleter14delete_nothingENS_9Q7PtrTypeE`, referenced at COMDAT
> `+0x12d`). The kernel borrows host HBM; it must not free it on tensor
> destruction. Output tensors built by `get_dst_tensor` use the same path but the
> writeback (§6) — not the destructor — is what commits data. `[HIGH × OBSERVED]`

> **NOTE — `isa_to_torch_dtype` is inlined, internal-linkage.** There is no
> external `isa_to_torch_dtype` symbol; DWARF carries it as
> `_ZL18isa_to_torch_dtype20NEURON_ISA_TPB_DTYPE` (the `L` = internal linkage),
> inlined into this COMDAT. Its body is the ISA-dtype → `c10::ScalarType` table
> — owned by [ScalarType/dtype Rosetta](scalartype-dtype-rosetta.md). The
> recovered `"Unknown ScalarType"` string is its default-case trap. `[HIGH × OBSERVED]`

The full byte layout of the three objects this chain produces —
`Q7PtrType` (16 B), `UniqueQ7Ptr` (24 B, deleter non-EBO @`0x10`),
`NeuronStorageImpl` (48 B, `data_ptr_` @`0x10`, `size_bytes_` SymInt @`0x28`),
`NeuronTensorImpl` (216 B, `storage_` @`0xD0`) — is `[CARRIED × OBSERVED]` from
the Part-7 pack and detailed in [Q7PtrType](q7ptrtype.md) and
[The at::Tensor Object Chain](tensor-object-chain.md); this page is the caller.

---

## 4. Dual dtype dispatch

The custom-op ABI dispatches dtype **twice, on two different axes** — and there
is **no runtime `switch(scalar_type)` on the arg-pull side**.

**Axis 1 — runtime, value-driven (inside the tensor build).** The descriptor's
`dtype` byte (`+2`) is run through `isa_to_torch_dtype()` to produce a
`c10::ScalarType`, which becomes the `caffe2::TypeMeta` stamped into
`NeuronTensorImpl` (§3 step d). This is a data lookup, not a control-flow switch.

**Axis 2 — compile-time, template-driven (selected by the *entry the kernel
calls*).** The kernel asks for a concrete C type by choosing **which**
`customop_next_<type>` it invokes:

```c
/* The five next-entries differ only in the post-read conversion  [HIGH × OBSERVED] */
customop_next_tensor()   : UTensor -> at::Tensor    (operator at::Tensor,  @0x388 callx8)
customop_next_scalar()   : UTensor -> c10::Scalar   (operator c10::Scalar, @0x3bc callx8)
customop_next_int()      : UTensor -> int           (neuron_translate path, below)
customop_next_longlong() : UTensor -> long long     (neuron_translate path)
customop_next_float()    : UTensor -> float          (neuron_translate path)
```

For the three plain-scalar entries, the disassembly shows an identical body:
read one `UTensor`, then call `neuron_translate_ctx()` (`@0x3db`/`0x413`/`0x453`)
and `neuron_translate(void*, uint64_t)` (`_Z16neuron_translatePvy`,
`@0x3e4`/`0x41c`/`0x45c`) to map the scalar's HBM payload into the core's address
space and load it as the requested C type:

```c
/* customop_next_int @0x3c4  (next_longlong/next_float identical shape)  [HIGH × OBSERVED] */
int customop_next_int() {
    UTensor u;
    ArgParser::next_ucode_arg<UTensor>(&u, &arg_parser);   /* @0x3d8 */
    void *ctx  = neuron_translate_ctx();                    /* @0x3e1 */
    void *win  = neuron_translate(/*hbm=*/u.storage.hbm.addr, ctx); /* @ next, reads window */
    return *(int *)win;                                     /* widened/narrowed per entry */
}
```

`[HIGH × OBSERVED]` for the call shape; the final load width per type is
`[MED × INFERRED]` from the entry's return type. `neuron_translate` (the 5-entry
window walk, `ctx` 168 B) is `[CARRIED × OBSERVED]` and owned by
[Q7PtrType + Lazy Translation](q7ptrtype.md).

> **GOTCHA — the dual dispatch can disagree, and the ABI checks it.** Because the
> C type is chosen at compile time (which entry) while the dtype is chosen at run
> time (descriptor byte), a kernel could read a `float` argument with
> `customop_next_int()`. The writeback path catches the mismatch with a hard
> assert at `utypes.hpp:65` (§6). On the *pull* side there is no such guard for
> the scalar entries — a wrong choice silently reinterprets bytes. `[HIGH × OBSERVED]`

> **NOTE — `customop_next_scalar` differs from `customop_next_int/float`.**
> `next_scalar` goes through `UTensor::operator c10::Scalar()`
> (`_ZNK7UTensorcvN3c106ScalarEEv`, 59 relocs, `callx8 @0x3bc`), which builds a
> boxed `c10::Scalar` with its own dtype tag and 1-element guards
> (`utypes.hpp:172/178` "Cannot create an at::Scalar. Argument has more than 1
> element"). The plain `int`/`long long`/`float` entries skip the box and load a
> raw word via `neuron_translate`. `[HIGH × OBSERVED]`

---

## 5. Setup and cleanup

### 5.1 `customop_setup(bool)` @`0x224`

`customop_setup` is the one-time init the launch shim calls before the kernel
body. From the disassembly it (1) arms the DMA engine via `init_dma_queue()`
(`_Z14init_dma_queuev`, `callx8 @0x239`), (2) clears the
`get_dst_tensor_called` flag at `.bss+0x808` (`s8i a8, …, 0` after loading
`&(anon)::get_dst_tensor_called`), (3) initialises the 512-byte `arg_parser` at
`.bss+0x608` from the host arg blob, and (4) branches on its `bool` parameter
(`beqz a2, …` at `@0x277`) to select a setup mode.

```c
/* customop_setup(bool mode) @0x224 — reconstructed  [MED × INFERRED on field detail] */
void customop_setup(bool mode) {
    init_dma_queue();                              /* @0x239  arm DMA m2s/s2m queues   */
    get_dst_tensor_called = 0;                     /* @0x257  clear one-claim flag     */
    arg_parser.init(/*host arg view*/);            /* @0x608  bind 512 B parser state  */
    if (mode) { /* mode-1 path @0x27a */ } else { /* mode-0 path @0x2d8 */ }
}
```

`[HIGH × OBSERVED]` for the `init_dma_queue` call, the `0x808` flag clear, and
the `bool`-branch; field-level parser init is `[MED × INFERRED]`. The
`_Assert`/`init_dma_queue` worker region (`@0x326`–`0x342`, three `_Assert`
relocs) carries the setup-time invariant checks.

### 5.2 `customop_cleanup()` @`0x5d4`

```c
/* customop_cleanup @0x5d4  [HIGH × OBSERVED] */
void customop_cleanup() {
    fsync(2);   /* @0x5e9  callx8, a10=2 -- sync one DMA queue direction */
    fsync(1);   /* @0x5ee  callx8, a10=1 -- sync the other direction     */
    memw;       /* @0x5f1  memory barrier                                */
}
```

`fsync` is the DMA-queue drain primitive (the two calls flush memory-to-SBUF and
SBUF-to-memory queues); `memw` fences the result before the kernel returns to the
launch shim. `[HIGH × OBSERVED]` — two `fsync` reloc'd `callx8`s with immediates
`2` then `1`, followed by `memw`.

---

## 6. Output writeback: `get_dst_tensor` / `customop_return_tensor`

There are **two ways** to produce an output, and the `get_dst_tensor_called`
flag (`.bss+0x808`) arbitrates between them so the output is written **exactly
once**.

### 6.1 The shared descriptor-load prologue

Both `get_dst_tensor` (`@0x528`) and `customop_return_tensor` (`@0x47c`) start by
loading the **output** descriptor pointer from `(anon)::retval` (`.bss+0x80c`)
and copying its 48 bytes onto the stack (`l32i a*, a6, 0…44`, twelve words):

```c
/* shared prologue of get_dst_tensor (@0x528) and customop_return_tensor (@0x47c)
 * [HIGH × OBSERVED] */
arg_tensor_t *out = *(arg_tensor_t **)(&retval);   /* l32i.n a6, retval, 0   */
arg_tensor_t  d   = *out;                            /* copy 48 B to stack      */
uint8_t loc = d.location;                            /* extui a5, word0, 0, 8   */
/* resolve storage base by location */
uint64_t hbm = d.storage.hbm.addr;
if (loc == SBUF) {                                  /* bnei a5,1 falls through */
    hbm += *(uint64_t *)&_sbuf_window;               /* add _sbuf_window base+off, carry-safe */
}
/* loc == HBM (beqi a5,2): use hbm directly */
```

`[HIGH × OBSERVED]` — the 12-word descriptor copy, the `extui … 0,8` location
extract, the `beqi a5,2`/`bnei a5,1` location branch, and the `_sbuf_window`
base add with `add.n`+`saltu` carry are identical in both entries.

### 6.2 `get_dst_tensor()` — claim the output as a writable tensor

```c
/* get_dst_tensor @0x528  [HIGH × OBSERVED] */
at::Tensor get_dst_tensor() {
    get_dst_tensor_called = 1;           /* @0x53b  s8i 1, .bss+0x808, 0 -- claim it  */
    arg_tensor_t d = *retval;            /* shared prologue */
    /* ...resolve storage (loc/_sbuf_window)... then build like §3 but as an OUTPUT... */
    return (at::Tensor) UTensor{ &d };   /* writable; deleter still delete_nothing  */
}
```

The single distinguishing instruction vs `return_tensor` is `s8i 1` into
`get_dst_tensor_called` **before** the build: it tells a later
`customop_return_tensor` to do nothing. `[HIGH × OBSERVED]`

### 6.3 `customop_return_tensor(at::Tensor&)` — stage and DMA to HBM

```c
/* customop_return_tensor @0x47c  [HIGH × OBSERVED] */
void customop_return_tensor(at::Tensor &t) {
    if (get_dst_tensor_called)            /* @0x488  bnez -> skip: output already in place */
        return;
    arg_tensor_t d = *retval;            /* shared prologue (loc/_sbuf_window resolve)   */
    UTensor out{ &d };
    out.copy(t);                          /* @0x50d  callx8 UTensor::copy(at::Tensor&)    */
}
```

`[HIGH × OBSERVED]` — the `bnez` guard on `get_dst_tensor_called @0x488`, the
descriptor resolve, and the `callx8 UTensor::copy @0x50d`.

> **NOTE — the one-output-claim protocol.** If the kernel used `get_dst_tensor()`
> it wrote results directly into the output's storage, so `customop_return_tensor`
> sees the flag set and returns immediately. If it built a *fresh* result tensor
> instead, `customop_return_tensor` does the `UTensor::copy` that stages and DMAs
> it. Calling **both** with the flag set is safe (the second is a no-op); the
> hazard is calling **neither**. `[HIGH × OBSERVED]`

### 6.4 `UTensor::copy(at::Tensor const&)` — the staged writeback (101 relocs)

`UTensor::copy` (`_ZN7UTensor4copyERKN2at6TensorE`, 101 relocs) is the actual
HBM writeback. Its reloc'd call targets, in order:

```c
/* UTensor::copy(at::Tensor const&) — call order  [HIGH × OBSERVED] */
void UTensor::copy(const at::Tensor &src) {
    /* dtype gate — runtime check that the host descriptor dtype matches the tensor */
    _Assert(src.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)); /* utypes.hpp:65 */

    uint32_t bytes;
    neuron_dataram_max_free_space(&bytes, ...);          /* @0x136  query TCM/dataram free */
    void *stage;
    neuron_dataram_allocate(want, &stage, align);        /* @0x157  carve staging buffer   */
    void *ctx = neuron_translate_ctx();                  /* @0x178                          */

    neuron_memcpy(/*dst*/ stage, /*src hbm*/ src_hbm, n); /* @0x1b5  HBM -> dataram (read)  */
    /* ...byte transform if needed... */
    neuron_memcpy(/*dst hbm*/ out_hbm, /*src*/ stage, n); /* @0x1d0  dataram -> HBM (write) */

    neuron_dataram_deallocate(stage);                    /* @0x1f4  free staging           */
}
```

`[HIGH × OBSERVED]` — call targets and order from
`.text._ZN7UTensor4copyERKN2at6TensorE`. The two directions are **distinct
overloads**, both defined in this object and confirmed by `nm`:

| symbol | demangled | direction |
|---|---|---|
| `_Z13neuron_memcpyPvyj` | `neuron_memcpy(void*, unsigned long long, unsigned int)` | HBM(`u64`) -> core(`void*`) — **read input** |
| `_Z13neuron_memcpyyPvj` | `neuron_memcpy(unsigned long long, void*, unsigned int)` | core(`void*`) -> HBM(`u64`) — **write output** |

`[HIGH × OBSERVED]` — both `T` symbols are defined in `wrapper_api.o` (and in
`data_transfer.o`). The `void*` argument is always the dataram-resident side; the
`u64` is the HBM side; argument *order* encodes direction.

> **GOTCHA — output goes through a dataram bounce, not a direct HBM->HBM copy.**
> The Q7 core cannot stream HBM->HBM; `UTensor::copy` allocates a scratch buffer
> in the TCM/dataram window `[0x80000, 0x90000)` `[CARRIED]`, `neuron_memcpy`s the
> result HBM->dataram, then dataram->HBM into the output descriptor's HBM address.
> A too-large tensor can exhaust dataram — `neuron_dataram_max_free_space`
> (`@0x136`) is queried first and the copy is chunked. `[HIGH × OBSERVED]` for the
> alloc/dealloc bracket; chunking is `[MED × INFERRED]`. Which of
> `c_memcpy`/`vec_memcpy`/`dma` each `neuron_memcpy` actually uses is decided
> inside `data_transfer.o` from `(anon)::active_neuron_memcpy_method`
> (`.data+0x20c`); the `data_transfer_method_table` (`.data+0x200`) defaults to
> `DMA=2` — `[HIGH × OBSERVED]`: the table bytes are `00 00 00 02`, so entry
> `[3]` / `DEFAULT` = `2`. `[CARRIED]`

> **QUIRK — the `utypes.hpp:65` gate fires on the writeback, not the pull.** The
> recovered assert `aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)`
> is in `UTensor::copy`, so a dtype mismatch between the result `at::Tensor` and
> the host's declared output dtype is caught **only when you write back** (or via
> `get_dst_tensor`'s build), not when you pull arguments. A kernel that never
> returns a tensor never trips it. The same string also guards the build of a
> tensor from a descriptor. `[HIGH × OBSERVED]`

---

## 7. End-to-end flow (one custom-op invocation)

Putting the entries in call order, as a generated kernel uses them:

```c
/* host has placed N ARG_TENSOR descriptors + 1 output descriptor; the launch shim
 * (stack_switch.o) has switched the core onto the kernel stack.  [HIGH × OBSERVED on entry order] */

customop_setup(mode);                  /* arm DMA, clear claim flag, bind arg_parser   */

at::Tensor a = customop_next_tensor(); /* pull args in declared order via one shared    */
int        k = customop_next_int();    /* cursor (arg_parser.curr_arg_ advances each)    */
float      s = customop_next_float();

at::Tensor y = get_dst_tensor();       /* OPTION A: claim output, write into it in place */
/*   ...or build a fresh result and:  customop_return_tensor(y);   OPTION B (staged copy) */

kernel_body(a, k, s, /*out*/ y);       /* the actual compute                             */

/* if OPTION B was chosen: */ customop_return_tensor(y);   /* no-op under OPTION A        */

customop_cleanup();                    /* fsync(2); fsync(1); memw -- drain DMA          */
/* launch shim restores the original stack (stack_switch.o)                              */
```

`[HIGH × OBSERVED]` for which entry each step is and the `get_dst_tensor` /
`customop_return_tensor` mutual exclusion; the *ordering by the kernel* is the
ABI contract the codegen emits (see [Custom-Op Codegen](build-custom-op-codegen.md)).

### Reimplementation checklist

To rebuild a Vision-Q7-compatible front door:

1. **Arg stream** — one `ArgParser` singleton (`512 B`), `curr_arg_` cursor,
   asserting `curr_arg_ < num` and `type == ARG_TYPE_TENSOR` per pull. One
   templated reader `next_ucode_arg<UTensor>`; **no** per-scalar reader.
2. **Descriptor** — `48 B` `ARG_TENSOR`: `location`@`0`, `framework_shape_type`@`1`,
   `dtype`@`2`, `framework_shape`@`8` (16 B), `storage`@`24` (16 B; HBM `u64`
   addr @`24` / `num_elem`@`32`; SBUF `u32` addr @`24`), `reserved1`@`40`.
3. **Tensor build** — `Q7PtrType{hbm,ctx}` -> `UniqueQ7Ptr{delete_nothing}` ->
   `intrusive_ptr<NeuronStorageImpl>::make(num_elem, move(uq))` ->
   `intrusive_ptr<NeuronTensorImpl>::make(storage, key, TypeMeta(isa_to_torch_dtype(dtype)))`
   -> `at::Tensor(Undefined)` -> `set_sizes_contiguous(sizes)`.
4. **Dual dtype** — runtime `isa_to_torch_dtype` for the `TypeMeta`; compile-time
   per-`T` selection by which `customop_next_<T>` is called; assert equality at
   writeback (`utypes.hpp:65`).
5. **Writeback** — `retval` -> output descriptor; resolve `location`
   (SBUF adds `_sbuf_window`); stage via `neuron_dataram_allocate` +
   bidirectional `neuron_memcpy`; honour the `get_dst_tensor_called` one-claim
   flag; `customop_cleanup` drains the queues.

---

## Cross-references

- [Q7PtrType + Lazy Translation](q7ptrtype.md) — the 16 B `Q7PtrType`,
  `UniqueQ7Ptr`, `neuron_translate`, and the TCM window this page consumes.
- [The at::Tensor Object Chain](tensor-object-chain.md) — `NeuronStorageImpl` /
  `NeuronTensorImpl` byte layouts and the `make_intrusive` producers §3 calls.
- [ScalarType / dtype Rosetta](scalartype-dtype-rosetta.md) — the full
  `isa_to_torch_dtype` to `c10::ScalarType` table behind the dual dispatch.
- [Stack Switch ABI](stack-switch.md) — the launch handoff (`stack_switch.o`)
  that brackets every call sequence above; **not** part of `wrapper_api.o`.
- [Custom-Op Codegen](build-custom-op-codegen.md) — how the generated kernel is
  wired to emit these `customop_*` calls in the correct order.
