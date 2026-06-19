# The Complete Custom-Op ABI (`libneuroncustomop.a`)

> **Scope — the whole ABI on one screen.** This is the **capstone** page for the
> GPSIMD custom-op ABI. `libneuroncustomop.a` is the **device-side static
> runtime** an op author links into a loadable ucode library: it provides
> `_start`/`entry_func`, the freestanding device libc/libc++, the `customop_*`
> argument marshalling, address translation, the HBM/dataram allocators, the
> DMA/memcpy backends, the HBM-stack trampoline, and the PyTorch `c10`/`at::`
> glue. Everything below is "how to write a GPSIMD custom op": the **entry
> contract** the SDK calls, the **link line** that produces a loadable library,
> the **on-wire tensor descriptor** the host pushes, the **pull stream** a kernel
> drains, and the **return** it hands back. This page does **not** re-derive the
> per-topic machinery — it ties the per-topic pages together and stays consistent
> with them. The device half lives here; the **host** loader half (the loader that
> builds the handoff block and calls `entry_func`) is a separate component
> (`libnrtucode*` / `capi.so`, not in this archive).
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read from
> `ar`/`nm`/`c++filt`/`readelf` symtab, relocations, DWARF, or native Xtensa
> disassembly), `INFERRED` (a C/C++-ABI rule applied to an observed offset),
> `CARRIED` (taken from a shared Part-7 pack and re-confirmed here only where it
> intersects this page).

> **NOTE — artifacts & tooling.** All claims are derived **solely from static
> analysis** of the shipped `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`
> package, file
> `/opt/aws/neuron/gpsimd/custom_op/neuron/libneuroncustomop.a`
> (**md5 `ef49ef4b5bffb95e1f3df2e4c1a8f09e`, 898 172 bytes**). Every one of its
> **10 members** is **ELF 32-bit LSB, Tensilica Xtensa, type `REL`
> (relocatable), not stripped, with DWARF v4** (producer
> `XtensaTools-14.09 clang version 10.0.1`). Native disassembly used the in-tree
> Xtensa binutils with the device core registered:
> ```
> export XTENSA_SYSTEM=.../gpsimd_tools/tools/XtensaTools/config
> export XTENSA_CORE=ncore2gp
> xtensa-elf-objdump -dr <member.o>      # FLIX bundles print as { op; op; … }
> xtensa-elf-readelf  --debug-dump=info  # struct byte_size / member offsets / enums
> ```
> Each `.o` is relocatable (section VMA `= 0`), so `.text`/`.data` offsets below
> are **section-relative** — there is no VMA delta to reason about; cross-member
> data addresses resolve through `.rela.*`. Source-path strings
> (`/opt/workspace/SundaCustomOpLibrary/custom_op/.../<file>:<line>`) are quoted
> **as recovered `.rodata` literals** — `__FILE__`/assert text the compiler baked
> in — not from any external tree.

---

## 0. The archive — 10 members, what each one owns

`ar t libneuroncustomop.a` lists exactly **10 objects**. Each is a self-contained
translation unit; together they are the entire device runtime. `[HIGH × OBSERVED]`

| member | size (B) | role (one line) | per-topic page |
|---|---|---|---|
| `start_exit.o` | 77 776 | device CRT: `_start` / `entry_func` / `register_funcs` + freestanding libc/libc++ | §1 below |
| `wrapper_api.o` | 652 840 | the `customop_*` marshalling C API + `UTensor` + `at::Tensor` glue | [customop-marshalling](customop-marshalling.md) |
| `translation.o` | 17 032 | `neuron_translate`: framework addr → SoC addr (5-region software TLB) | [neuron-translate-windows](neuron-translate-windows.md) |
| `allocator.o` | 26 128 | `neuron_hbm_*` / `neuron_dataram_*` over Cadence `xmem` heaps | [device-allocators](device-allocators.md) |
| `data_transfer.o` | 34 184 | `neuron_memcpy` / `dma_data_transfer` (SDMA descriptor build) | [data-transfer-backends](data-transfer-backends.md) |
| `NeuronAllocator.o` | 46 680 | `c10::GetNeuronAllocator()` — the PyTorch `c10::Allocator` hook | [q7ptrtype](q7ptrtype.md), [tensor-object-chain](tensor-object-chain.md) |
| `stack_switch.o` | 16 648 | HBM-stack trampoline: `switch_stack_or_call_wrapper` | [stack-switch](stack-switch.md) |
| `TensorTcmAccessor.o` | 5 188 | `at::neuron::tcm_malloc` / `tcm_free` → dataram alloc | [tensor-accessor](tensor-accessor.md), [tensorstream-tcm](tensorstream-tcm.md) |
| `parallel.o` | 4 488 | `get_cpu_id` (PRID) / `get_cpu_count()==8` / version string | [multicore-spmd](multicore-spmd.md) |
| `switch_stack.o` | 4 940 | raw `.S`: `saveContext` / `switchBack` (windowed-register save) | [stack-switch](stack-switch.md) |

`[HIGH × OBSERVED]` All 10 source TUs live under
`/opt/workspace/SundaCustomOpLibrary/custom_op/` —
`{library,torch/include/neuron_torch_extension}/` (DWARF `.debug_str`). "Sunda" is
the NeuronCore-v2 (Trn1/Inf2) arch family; the ABI itself is **arch-agnostic at
this layer**.

> **NOTE — every member is debuggable.** `readelf -h` on all ten reports
> `ELF32 / Tensilica Xtensa / REL`; `readelf -S` finds a populated `.symtab`
> **and** `.debug_info`/`.debug_abbrev`/`.debug_str`/`.debug_line`/`.debug_frame`
> in **every** member. That is why the entire ABI — struct byte-sizes, member
> offsets, enum `const_value`s, the on-wire descriptor — is read directly out of
> DWARF rather than inferred from prologues. `[HIGH × OBSERVED]`

### 0.1 The library is real Xtensa code — the FLIX bundle census

This is a genuine compiled DSP runtime, not a thunk table. Counting FLIX bundles
(the wide multi-issue `{ op; op; … }` lines the native `objdump` prints) across
all members:

| member | FLIX bundles |
|---|---|
| `wrapper_api.o` | 1 094 |
| `start_exit.o` | 122 |
| `data_transfer.o` | 110 |
| `translation.o` | 66 |
| `allocator.o` | 43 |
| `stack_switch.o` | 21 |
| `NeuronAllocator.o` | 20 |
| `TensorTcmAccessor.o` | 3 |
| `parallel.o` | 0 |
| `switch_stack.o` | 0 |
| **TOTAL** | **1 479** |

`[HIGH × OBSERVED]` Counted as `xtensa-elf-objdump -d <member> | rg -c '\{.*\}'`;
every brace line is a real instruction line (opening braces equal closing braces
per member, no multi-line bundles, no symbol-name noise). The two zero-bundle
members are `switch_stack.o` (hand-written windowed-ABI `.S`, no FLIX) and
`parallel.o` (three tiny scalar functions). The bulk (1 094 / 1 479 ≈ 74 %) lives
in `wrapper_api.o` — the marshalling + `at::Tensor` materialisation is the heavy
lifting.

---

## 1. The entry + registration contract (`start_exit.o`)

This is the **single host → device interface**. A reimplementation must honour it
exactly. `[HIGH × OBSERVED]`

### 1.1 `SUNDA_UCODE_LIB_STATUS` — the 4-byte return enum

DWARF `enumeration_type`, `byte_size = 4`:

| enumerator | value |
|---|---|
| `SUNDA_UCODE_LIB_SUCCESS` | `0` |
| `SUNDA_UCODE_LIB_FAILURE` | `1` |
| `SUNDA_UCODE_LIB_FAILURE_ABI_MISMATCH` | `2` |
| `SUNDA_UCODE_LIB_FAILURE_OPCODE_TO_KERNEL_MAP` | `3` |
| `SUNDA_UCODE_LIB_FAILURE_NUM_KERNELS` | `4` |

`[HIGH × OBSERVED]` `const_value`s read directly from `start_exit.o` DWARF.

### 1.2 `SUNDA_UCODE_LIB` — the 36-byte host → device handoff block

DWARF `structure_type`, `byte_size = 36`. This is the argument to `entry_func`.
Every member offset is a `DW_AT_data_member_location`:

| off | member | meaning |
|---|---|---|
| `+0` | `size_t sizeof_ncore_ucode_lib` | ABI/size gate — host writes `sizeof(SUNDA_UCODE_LIB)`; device requires `>= 36` |
| `+4` | `SUNDA_UCODE_LIB_HANDLE library_handle` | opaque `void*` host context, threaded back into every callback |
| `+8` | `register_ext_isa_fn` | `STATUS (*)(HANDLE, const char* name)` — **v1**, register kernel by name |
| `+12` | `get_libc_fn` | `void* (*)(…)` — libc symbol resolver for the device CRT |
| `+16` | `get_data_scratch` | `HANDLE (*)(void** addr, size_t* size)` — TCM/SBUF scratch window |
| `+20` | `get_hbm_scratch` | `HANDLE (*)(uint64_t* addr, size_t* size)` — HBM scratch window |
| `+24` | `get_dma_queue` | `HANDLE (*)(uint32_t* m2s_off, uint32_t* s2m_off)` — DMA queue offsets |
| `+28` | `register_ext_isa_fn_v2` | `STATUS (*)(HANDLE, const char*, uint8_t)` — **v2**, name + 1-byte tag |
| `+32` | `register_ext_isa_fn_v3` | fn ptr — **v3** registrar |

`[HIGH × OBSERVED]` byte_size + all nine member offsets from DWARF.
`[MED × INFERRED]` the extra `uint8_t` on v2/v3 is an opcode/coretype selector
(it lines up with the host's register-by-opcode path); v1 registers by name only.

### 1.3 `entry_func(SUNDA_UCODE_LIB* host) → STATUS`

The CRT body (disassembled at `start_exit.o` `.text 0x93c`):

```c
SUNDA_UCODE_LIB_STATUS entry_func(SUNDA_UCODE_LIB *host) {
    // a. ABI/size gate:  movi a6,36 ; l32i a4,[host+0] ; bltu.w15 a4,a6,fail
    if (host->sizeof_ncore_ucode_lib < 36)            // OBSERVED: bltu vs literal 36
        return SUNDA_UCODE_LIB_FAILURE_ABI_MISMATCH;

    my_handle = host;                                 // stash host context

    // c. wire the device-global scratch/DMA descriptors by CALLING host callbacks
    host->get_data_scratch(&sdk::data_scratch, &sdk::data_scratch_size);   // + data_scratch_map
    host->get_hbm_scratch (&sdk::hbm_scratch,  &sdk::hbm_scratch_size);
    host->get_dma_queue   (&sdk::dma_queue_m2s_offset, &sdk::dma_queue_s2m_offset);

    // d. CRT bootstrap: libc resolver, __clibrary_init, _init, atexit(my_exit)
    __clibrary_init(/*get_libc_fn=*/host->get_libc_fn, ...);
    _init();
    atexit(my_exit);

    // e. kernel-count sanity:  bltui.w15 a4,0x8000,ok
    uint32_t n = get_func_cnt();                       // YOUR symbol (undefined here)
    if (!(n < 0x8000)) return SUNDA_UCODE_LIB_FAILURE_NUM_KERNELS;

    // f. register every kernel by name (get_func_ptr supplies the entry address)
    for (uint32_t i = 0; i < n; i++) {
        STATUS s = host->register_ext_isa_fn(host->library_handle, get_func_name(i));
        if (s != SUNDA_UCODE_LIB_SUCCESS) return s;    // propagate first failure
    }
    return SUNDA_UCODE_LIB_SUCCESS;
}
```

`[HIGH × OBSERVED]` the `movi a6,36 ; bltu.w15` gate and the `bltui.w15 …,0x8000`
gate are both present in the disassembly.

`register_funcs(STATUS(*reg)(void* fn, const char* name, int(*)()))` is the
SDK-convenience bulk registrar (`start_exit.o .text 0x8c0`): it loops
`reg(get_func_ptr(i), get_func_name(i), my_handle)` for `i ∈ [0, get_func_cnt())`,
threading the lib handle through the third argument. `[HIGH × OBSERVED]`

### 1.4 The op author's obligation — three undefined symbols

```
$ xtensa-elf-nm start_exit.o | rg ' U ' | c++filt
         U get_func_cnt()
         U get_func_ptr(int)
         U get_func_name(int)
```

`[HIGH × OBSERVED]` These three are **undefined** in `start_exit.o` — they are
**your** registration table. A reimplementation defines exactly these and links
them; the SDK CRT does the rest.

```c
// the kernel table the SDK enumerates — supplied by the op author
int          get_func_cnt()      { return N; }
void*        get_func_ptr(int i) { return (void*)kernels[i]; }
const char*  get_func_name(int i){ return names[i]; }
```

> **NOTE — `start_exit.o` is also the device libc.** Beyond `_start`/`entry_func`
> it defines the freestanding libc/libc++ the op runs against: `abort`,
> `malloc`/`free`/`realloc`/`memalign`, `memcpy`/`memmove`/`memset`/`memcmp`,
> the `_*_r` reentrant file shims (`_open_r`/`_close_r`/`_read_r`/`_write_r`/
> `_unlink_r`/`fsync`), `null_environ`, `CUSTOMOP_LIB_VERSION`, and the full
> `std::__1::__libcpp_*` mutex/condvar/tls/thread stub set. The op executes in a
> **self-contained libc/libc++ sandbox** on the DSP. `[HIGH × OBSERVED]`

---

## 2. The support-routine roster — one table, keyed to the per-topic pages

This is the device API a kernel body actually calls. Each routine is grounded in
detail on its own page; offsets are `.text`-relative within the named member.
`[HIGH × OBSERVED]` symbols & offsets from `nm`/`c++filt`.

| concern | symbol (demangled) | member · `.text` off | per-topic page |
|---|---|---|---|
| **marshal in** | `customop_setup(bool)` | `wrapper_api.o` `0x224` | [customop-marshalling](customop-marshalling.md) |
| | `customop_next_tensor() → at::Tensor` | `0x35c` | ″ |
| | `customop_next_scalar() → c10::Scalar` | `0x390` | ″ |
| | `customop_next_int() → int` | `0x3c4` | ″ |
| | `customop_next_longlong() → long long` | `0x3fc` | ″ |
| | `customop_next_float() → float` | `0x43c` | ″ |
| **output** | `get_dst_tensor() → at::Tensor` | `0x528` | ″ |
| **return** | `customop_return_tensor(at::Tensor&)` | `0x47c` | ″ |
| **cleanup** | `customop_cleanup()` | `0x5d4` | ″ |
| **translate** | `neuron_translate_ctx() → ctx*` | `translation.o` `0x110` | [neuron-translate-windows](neuron-translate-windows.md) |
| | `neuron_translate(void*, uint64) → uint64` | `0x120` | ″ |
| | `neuron_translate_mapping_lookup(...)` | `0x264` | ″ |
| | `_init_translate_ctx()` | `0x0` | ″ |
| **HBM heap** | `neuron_hbm_allocate(uint, uint64*, uint=64)` | `allocator.o` `0xb0` | [device-allocators](device-allocators.md) |
| | `neuron_hbm_deallocate(uint64)` | `0x10c` | ″ |
| **dataram/TCM heap** | `neuron_dataram_allocate(uint, void**, uint)` | `allocator.o` `0x1d0` | ″ |
| | `neuron_dataram_deallocate(void*)` | `0x204` | ″ |
| | `at::neuron::tcm_malloc(uint)` / `tcm_free(void*)` | `TensorTcmAccessor.o` `0x0`/`0x28` | [tensor-accessor](tensor-accessor.md) |
| **memcpy / DMA** | `neuron_memcpy(void*, uint64, uint)` (HBM→local) | `data_transfer.o` `0x65c` | [data-transfer-backends](data-transfer-backends.md) |
| | `neuron_memcpy(uint64, void*, uint)` (local→HBM) | `0x694` | ″ |
| | `neuron_set_memcpy_method(NeuronMemcpyMethod)` | `0x6cc` | ″ |
| | `dma_data_transfer(void*, uint64, bool, uint)` | `0x4f0` | ″ |
| **PyTorch alloc** | `c10::GetNeuronAllocator() → Allocator*` | `NeuronAllocator.o` `0x230` | [q7ptrtype](q7ptrtype.md) |
| **big stacks** | `switch_stack_or_call_wrapper(uint, uint)` | `stack_switch.o` `0xd4` | [stack-switch](stack-switch.md) |
| **SPMD** | `get_cpu_id()` / `get_cpu_count() → 8` | `parallel.o` `0x0`/`0x10` | [multicore-spmd](multicore-spmd.md) |

> **CORRECTION — `NeuronMemcpyMethod` has 3 selectable methods, not 2.** A naive
> read of the prose (`C_MEMCPY=0, VEC_MEMCPY=1`) omits the DMA path. The DWARF
> `enumeration_type` in `data_transfer.o` is actually
> `{ C_MEMCPY=0, VEC_MEMCPY=1, DMA=2, MAX_METHODS=3 }`, the dispatch table at
> `data_transfer.o .data 0x200` has **3** entries, and
> `active_neuron_memcpy_method` (`.data 0x20c`) defaults to **`02` = DMA**
> (`02 00 00 00` little-endian in the section dump). `[HIGH × OBSERVED]`

> **GOTCHA — `neuron_set_memcpy_method` accepts an out-of-range method.** The
> body (`data_transfer.o 0x6cc`) guards with `bgeui.w15 a3, 4` — it rejects only
> `method >= 4`. So `method == 3 == MAX_METHODS` **passes** the guard and is
> written into `active_neuron_memcpy_method`, leaving the active method indexing
> one past the 3-entry table. A reimplementation should guard `>= MAX_METHODS`
> (`>= 3`), not `>= 4`. `[HIGH × OBSERVED]`

---

## 3. The on-wire tensor descriptor — `NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR`

Every tensor argument **and** the output tensor arrive as exactly this 48-byte
record. DWARF `byte_size = 48`; member offsets are `DW_AT_data_member_location`:

| off | member | type / meaning |
|---|---|---|
| `+0` | `location` | `enum ARG_LOCATION { INVALID, SBUF, HBM }` (1 byte) |
| `+1` | `framework_shape_type` | `enum SHAPE_TYPE { INLINE_SHAPE8D, OUT_OF_LINE_SHAPE, INLINE_SHAPE4D }` (1 byte) |
| `+2` | `dtype` | `enum NEURON_ISA_TPB_DTYPE` (1 byte; see §3.1) |
| `+3` | `reserved0[5]` | — |
| `+8` | `framework_shape` | 16-byte union: `inline_shape8d` \| `out_of_line_shape_addr` \| `inline_shape4d` |
| `+24` | `storage` | 16-byte union: `tpb` \| `hbm` (see §3.2) |
| `+40` | `reserved1[8]` | — |

`[HIGH × OBSERVED]` byte_size + all member offsets + both enums from
`wrapper_api.o` DWARF. The pointers inside `framework_shape`/`storage` are
**framework addresses** and must pass through `neuron_translate()` before any
access (see [neuron-translate-windows](neuron-translate-windows.md)).

Two `static_assert`-style invariants are baked into `.rodata`:
- `utypes.hpp:94` — `framework_shape_type != OUT_OF_LINE_SHAPE` (the device path
  requires inline shapes; out-of-line is host-only).
- `utypes.hpp:139` — `num_dim != 0` ("tensors cannot have 0 dimensions — must be
  of at least rank 1").

`[HIGH × OBSERVED]`

### 3.1 `NEURON_ISA_TPB_DTYPE` — the device dtype encoding (1 byte)

DWARF `const_value`s (`wrapper_api.o`):

| value | dtype | | value | dtype |
|---|---|---|---|---|
| `0` | `INVALID` | | `8` | `INT32` |
| `1` | `UINT64` | | `9` | `UINT32` |
| `2` | `INT8` | | `10` | `FP32` |
| `3` | `UINT8` | | `11` | `FP32R` (tf32-like "rounded") |
| `4` | `INT16` | | `12` | `INT64` |
| `5` | `UINT16` | | `13` | `FP8_EXP3` |
| `6` | `BFLOAT16` | | `14` | `FP8_EXP4` |
| `7` | `FP16` | | `15` | `FP8_EXP5` |

`[HIGH × OBSERVED]` This ISA-dtype enum is **distinct** from the `c10::ScalarType`
values; the inline `isa_to_torch_dtype` mapping (an if/else compare chain, **not**
a jump table) and the enforced `utypes.hpp:65` consistency gate are owned by
[scalartype-dtype-rosetta](scalartype-dtype-rosetta.md) — do not conflate the two
numberings.

### 3.2 `storage` union — TPB vs HBM (16 bytes each)

DWARF, both `byte_size = 16`:

```c
// NEURON_ISA_TPB_CUSTOM_OP_TPB_TENSOR_STORAGE_DESC  (TCM/SBUF)
struct { uint32_t addr;            // +0  per-partition SBUF window addr (32-bit)
         uint32_t num_elem;        // +4
         uint8_t  num_partitions;  // +8
         uint8_t  reserved0[3];    // +9
         uint32_t num_elem_per_block; }; // +12
// NEURON_ISA_TPB_CUSTOM_OP_HBM_TENSOR_STORAGE_DESC  (HBM)
struct { uint64_t addr;            // +0  full SoC addr (NEURON_ISA_TPB_NEURON_ADDR, 64-bit)
         uint32_t num_elem;        // +8
         uint32_t reserved0; };    // +12
```

> **QUIRK — HBM addr is 64-bit on a 32-bit core.** TPB (SBUF) tensors are
> addressed by a 32-bit offset inside the per-partition window; HBM tensors carry
> a full 64-bit SoC address. The marshalling `customop_next_*` bodies translate +
> dereference these through `neuron_translate()` before touching memory.
> `[HIGH × OBSERVED]`

### 3.3 The argument container — at most 10 args per invocation

The host fills an `ArgExtractor` (DWARF `byte_size = 504`); a per-invocation
`ArgParser` (DWARF `byte_size = 512`) walks it:

```c
struct ArgExtractor {            // 504 B
    size_t num_args_;            // +0    count actually present
    const size_t max_args_;      //       == 10  (the array bound)
    NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR args_[10];       // +8   (10 × 48 = 480 B)
    NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE   arg_types_[10];  // +488 (1 byte each)
};
struct ArgParser {               // 512 B
    ArgExtractor isa_args;       // +0
    size_t       curr_arg_;      // +504  running argument index
};
```

`[HIGH × OBSERVED]` byte_sizes + offsets + `DW_AT_count = 10`. The single global
`(anon)::arg_parser` lives at `wrapper_api.o .bss 0x608`; `get_dst_tensor_called`
at `.bss 0x808`, `retval` at `.bss 0x80c`. A custom op takes **at most 10**
tensor/arg slots. The full pull-stream call chains
(`customop_setup` → `customop_next_*` → `get_dst_tensor` → `customop_return_tensor`
→ `customop_cleanup`) are documented in
[customop-marshalling](customop-marshalling.md).

---

## 4. How a kernel is dispatched — the pull stream and the return

Inside a kernel body the flow is a strict drain-then-return sequence. Each scalar
`customop_next_int/longlong/float` pulls a **1-element tensor** whose storage
holds the value, translates the descriptor pointer, and dereferences it (so an
immediate argument is, on the wire, a degenerate tensor). `[HIGH × OBSERVED]` for
the bodies; `[MED × INFERRED]` for the "scalar == 1-element tensor" reading.

```c
at::Tensor my_op() {
    customop_setup(/*first call?*/ false);    // copies the incoming stride-48
                                              //   descriptors into ArgExtractor;
                                              //   init_dma_queue()
    at::Tensor x = customop_next_tensor();    // arg 0  (translated SoC ptr → at::Tensor)
    int        k = customop_next_int();       // arg 1  (1-elem tensor, translated+deref)
    at::Tensor y = get_dst_tensor();          // pre-shaped OUTPUT descriptor
    //  ... compute x -> y; tcm_malloc for on-core scratch, neuron_memcpy / DMA ...
    customop_return_tensor(y);                // copies result into the wire
                                              //   descriptor; fixes c10 metadata
                                              //   (set_sizes_and_strides + refresh)
    customop_cleanup();                       // destruct ArgParser; memw + fsync
    return y;
}
```

> **NOTE — `customop_cleanup` is a commit barrier.** Its tail is `memw` + `fsync`
> — a memory-write fence then a sync — to drain in-flight DMA/SBUF writes before
> the op returns. Skipping it loses the last writes. `[HIGH × OBSERVED]`

The deep object chain a materialised tensor wraps — `Q7PtrType` (the device
"fancy pointer"), `UniqueQ7Ptr`, `NeuronStorageImpl`/`NeuronTensorImpl`,
`make_tensor<>` — is owned by [q7ptrtype](q7ptrtype.md) and
[tensor-object-chain](tensor-object-chain.md). A GPSIMD custom op is a **real
PyTorch `at::Tensor` program** whose backend tensors wrap device storage. The
intermediate `UTensor` (DWARF `byte_size = 56`: `xt_ptr_`@`0`, the 48-byte `t_`
descriptor @`8`) bridges the wire record to an `at::Tensor` /
`c10::Scalar` via its conversion operators.

---

## 5. The link line — turning your kernels into a loadable ucode library

The op author compiles their kernel(s) + the three `get_func_*` symbols, links
against this archive plus the shipped `c10`/`at::` static lib and the device
runtime libs, then packs the result. The canonical recipe (see
[build-flow](build-flow.md) for the full toolchain): `[HIGH × CARRIED]`

```
# per-core ELF: org = 0x84000000 + i*0x200000 for i in [0,8)  (NUM_CPUS = 8)
xt-clang++ ... your_kernels.o \
    libneuroncustomop.a libc10.a \
    -lloader -mlsp=lsp_fll_load_cpuI \
    -lxmem -lhal -lc++-e -lm -lgcc libcweak.a
xt-pkg-loadlib  ...                      # pack to a relocatable load-lib image
```

`libneuroncustomop.a` supplies `_start`/`entry_func`/the CRT, the `customop_*`
marshalling, translate, the allocators, DMA, and the HBM-stack trampoline;
`libc10.a` supplies the `at::`/`c10` tensor machinery this archive references as
undefined symbols (`TensorImpl` ctor/dtor, `set_sizes_and_strides`,
`make_tensor<>`, `intrusive_ptr<...>::make`, `caffe2::TypeMeta`, `SymInt`, …).

---

## 6. Cross-references

- [customop-marshalling](customop-marshalling.md) — the `customop_*` entries, the
  pull-stream call chains, the dual `ArgParser` dispatch, the writeback.
- [neuron-translate-windows](neuron-translate-windows.md) — the 5-region software
  TLB (`_map_record` 32-byte stride, `(addr & mask) == ptr` match) that resolves
  framework addrs to SoC addrs.
- [device-allocators](device-allocators.md) — the `xmem` first-fit HBM/dataram
  heaps, 64-byte alignment, `_hbm_heap_mgr` / `_dataram_heap_mgr`.
- [data-transfer-backends](data-transfer-backends.md) — `c_memcpy` / `vec_memcpy`
  / DMA backends and the SDMA descriptor build.
- [stack-switch](stack-switch.md) — `switch_stack_or_call_wrapper`, the
  `saveContext` frame, the windowed-register rotor.
- [multicore-spmd](multicore-spmd.md) — `get_cpu_count()==8`, PRID-based
  `get_cpu_id`, per-core SoC remap.
- [scalartype-dtype-rosetta](scalartype-dtype-rosetta.md) — ISA dtype ↔
  `c10::ScalarType` inline mapping (distinct numberings).
- [q7ptrtype](q7ptrtype.md) / [tensor-object-chain](tensor-object-chain.md) —
  `Q7PtrType` / `NeuronStorageImpl` / `NeuronTensorImpl` / `c10::Allocator` hook.
- [device-abi-reference](device-abi-reference.md) — the consolidated ABI struct
  reference.
- [build-flow](build-flow.md) — the full compile/link/pack toolchain.
- [abi-synthesis](abi-synthesis.md) — the end-to-end ABI synthesis companion.

The **host** half — the loader that builds the `SUNDA_UCODE_LIB` handoff block,
calls `entry_func`, and dispatches per-op descriptors at run time via the
extended-instruction lane — is **not in this archive**; it lives in the host
runtime (`nrtucode` / `capi.so`) and the compiler front-end. The
`register_ext_isa_fn_v2`/`v3` `uint8_t` tag lines up with that host opcode/coretype
path. `[MED × INFERRED]`

---

## 7. Confidence & caveats

`[HIGH × OBSERVED]` — the 10-member inventory; every member being ELF32 Xtensa
`REL`, not-stripped, with DWARF; the 1 479 FLIX-bundle census; the
`SUNDA_UCODE_LIB` (36 B) layout and `STATUS` enum; the `entry_func` ABI-size gate
(`< 36`) and kernel-count gate (`< 0x8000`); the three `get_func_*` undefined
symbols; the 48-byte `ARG_TENSOR` descriptor and `DTYPE` enum; `ArgParser` (512 B)
/ `ArgExtractor` (504 B, `max_args_ = 10`) / `UTensor` (56 B); the `customop_*`
signatures and offsets; `get_cpu_count()==8`; version string `1.21.1.0`; the
`NeuronMemcpyMethod` enum + default `DMA=2` + the `>= 4` guard quirk — all read
directly from the archive's DWARF / `nm` / relocations / disassembly.

`[MED × INFERRED]` — semantic labels: `register_ext_isa_fn_v2/v3`'s `uint8_t`
meaning (opcode/coretype tag); the "scalar arg == 1-element tensor" reading of
`next_int/longlong/float`; the host-side dispatch wiring (described, not in this
archive).

`[LOW]` — the `reserved0/reserved1` descriptor fields are unobserved in use
(zeroed on the code paths seen).
