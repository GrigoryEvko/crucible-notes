# The Device-Side Custom-Op ABI Reference

This page is the **normative contract**: the per-symbol set of obligations a
compiled GPSIMD kernel object file (`.o`, ELF32 little-endian Xtensa, target
core `ncore2gp` — the Vision-Q7 "Cairo" POOL DSP) **must** satisfy to be
loadable and dispatchable by the Neuron device runtime. The earlier Part-7
mechanism pages explain *how* each piece works and *why*; this page reduces them
to a single dense table-driven reference you can hold open while building a
Vision-Q7-compatible engine. Where a mechanism page proves a claim, this page
restates the contract and cites the proving symbol; it does not re-derive.

Every claim is anchored to a symbol, address, struct offset, ELF section, enum
value, or `.rodata` string recovered from the static analysis corpus, with a
confidence tag `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`. **OBSERVED** = read
directly out of the binary in this page; **CARRIED** = established by a cited
Part-7 page and reused here unchanged; **INFERRED** = deduced from observed
facts. Source report **DX-RT-11**.

> **Binary of record.** All addresses, offsets and disassembly on this page are
> from `libneuroncustomop.a` (Neuron GPSIMD custom-op library **v0.21.2.0**,
> amd64 package), and its sibling `libc10.a`. Disassembly was produced with the
> Tensilica `xtensa-elf-objdump`/`xtensa-elf-nm` configured for
> `XTENSA_CORE=ncore2gp`. Addresses are **section-relative file offsets inside
> the archive member**; the final link assigns absolute Q7 virtual addresses
> (see [q7-elf-vaddr.md](q7-elf-vaddr.md)). Member→symbol layout is the same as the runtime sees
> after `ar x`.

The library has exactly ten members; the contract is spread across them:

| Member | Provides (contract surface) |
|---|---|
| `start_exit.o` | `_start`, `entry_func`, `register_funcs`, the libc/Newlib reentrant shims, `fsync`, the `extended_isa::sdk` scratch globals |
| `wrapper_api.o` | The 9 `customop_*` entry points, `ArgParser`, `UTensor`, the `retval`/`get_dst_tensor_called` `.bss` globals, the inline ISA→ScalarType map |
| `translation.o` | `neuron_translate`, `neuron_translate_ctx`, `_init_translate_ctx`, `_ctx`, `_sbuf_window` |
| `allocator.o` | `neuron_hbm_*`, `neuron_dataram_*`, libc-heap allocator |
| `data_transfer.o` | `neuron_memcpy` ×2, the 3-method dispatch table, `dram_addr_to_soc_addr` |
| `stack_switch.o` | `switch_stack_or_call_wrapper`, `allocate_hbm_stack`, `deallocate_hbm_stack` |
| `switch_stack.o` | `saveContext`, `switchStack`, `switchBack` (register-window machinery) |
| `parallel.o` | `get_cpu_id`, `get_cpu_count` (SPMD identity) |
| `NeuronAllocator.o` | `c10::GetNeuronAllocator` |
| `TensorTcmAccessor.o` | `at::neuron::tcm_malloc`/`tcm_free` |

---

## Part 1 — The Entry Contract (registration + dispatch)

The runtime never calls a kernel's compute function directly. It links the
kernel `.o` against this library, then drives a fixed three-stage handshake:
**(a)** boot via `_start`→`entry_func`; **(b)** enumerate and register every
exported op via `register_funcs`, which walks a **kernel-supplied registration
trio**; **(c)** dispatch one op through a generated `<fn>_wrapper`, optionally
behind a stack switch, with `switchBack` restoring the caller's window on
return.

### 1.1 The registration trio (kernel-supplied, runtime-walked)

`register_funcs` (`_Z14register_funcsPF22SUNDA_UCODE_LIB_STATUSPvPKcPFivEE` @
`start_exit.o:0x8c0`) is the single point where the runtime discovers what a
kernel exports. It takes one argument: a **registration callback**
`SUNDA_UCODE_LIB_STATUS (*)(void* handle, const char* name, int(*fn)())`. The
disassembly proves the walk:

```
8c0:  entry  a1,32
8c3:  const16 a3, get_func_cnt          ; a3 := &get_func_cnt
8c9:  callx8 a3                          ; count = get_func_cnt()
8cc:  blti.w15 a10,1, 92a               ; count < 1 → return 0
...                                      ; loop i = 0 .. count-1:
8fb:  const16 a6, get_func_name          ;   a6 := &get_func_name
901:  mov.a  a10, a4 ; a10 = i
904:  l32i.n a5, my_handle               ;   handle = my_handle (start_exit.o:.bss 0x30)
906:  callx8 a6                          ;   name = get_func_name(i)
909:  mov.a  a10, a4 ; a10 = i
911:  callx8 a7                          ;   ptr  = get_func_ptr(i)   ; a7 := &get_func_ptr
924:  callx8 a2                          ;   cb(handle, name, ptr)    ; a2 = caller's callback
927:  j      8e8                         ;   ++i
92f:  retw.n                             ; return 0
```

So the kernel object **must define** exactly three symbols (they are
**undefined** in `start_exit.o`, i.e. the library *requires* them):

| Mangled symbol | C signature | Contract |
|---|---|---|
| `_Z12get_func_cntv` | `int get_func_cnt()` | Number of ops the kernel exports. `< 1` ⇒ nothing registered, `register_funcs` returns 0. |
| `_Z13get_func_namei` | `const char* get_func_name(int i)` | NUL-terminated op name for slot `i`; the host matches this against the requested op string. |
| `_Z12get_func_ptri` | `int (*get_func_ptr(int i))()` | Address of the entry thunk for slot `i` — this is the `<fn>_wrapper`, not the raw user function. |

> **GOTCHA.** All three are walked by **index** `0..count-1`; there is no
> name→ptr map inside the library. `get_func_name(i)` and `get_func_ptr(i)`
> **must agree on `i`** or the host will register the wrong code under a name.
> *(HIGH / OBSERVED: index `a4` is the sole argument to both callees @ 0x901,
> 0x909.)*

> **NOTE.** The relocation at `0x8cc` shows `get_func_cnt` is reached via a
> PLT-style indirect (`const16` pair patched at link); the kernel may define the
> trio in C++ with C linkage as long as the **mangled** names match exactly.
> *(HIGH / OBSERVED: `R_XTENSA_SLOT0_OP _Z12get_func_cntv` @ 0x8c6.)*

The codegen that emits this trio (a `get_func_*` table built from the user's
`__attribute__`-tagged kernels) is described in [build-custom-op-codegen.md](build-custom-op-codegen.md).

### 1.2 The boot path: `_start` → `entry_func`

| Symbol | Member:addr | Role |
|---|---|---|
| `_start` | `start_exit.o:0xaf8` | Reset/entry trampoline; sets up `a1` stack, jumps into C runtime, reaches `entry_func`. |
| `entry_func` | `start_exit.o:0x93c` | One-time init gate: sets `_use_default_malloc`, installs `null_environ`, latches `first_entry` (`start_exit.o:.data 0x1c`) so re-entry is idempotent, wires the libc function table via `get_libc_fn`. |
| `first_entry` | `start_exit.o:.data 0x1c` | `D`-symbol guard so global ctors run once. *(HIGH / OBSERVED.)* |

`entry_func` reads its argument block at `[a2+0]`, `[a2+12]` (the dispatch
descriptor handed in by the SEQ/POOL firmware) and stores a `1` flag at a
`.data` byte before branching. The byte-level dispatch into the runtime is the
firmware concern of the POOL dispatch loop (`firmware/pool/pool-dispatch.md`);
the contract a kernel must respect is only that **global constructors and the
libc shim are already initialized** by the time its `<fn>_wrapper` runs.

### 1.3 The `<fn>_wrapper` / stack-switch shape

`get_func_ptr(i)` returns a thunk — call it `<fn>_wrapper` — not the user
function. The wrapper is what runs in the dispatched context. Its job (verified
by what `switch_stack_or_call_wrapper` sets up around it, below) is:

1. `customop_setup(bool)` — initialize the DMA queue and the arg cursor.
2. Pull arguments via the `customop_next_*` stream (Part 2).
3. Invoke the user kernel body.
4. Publish the result via `customop_return_tensor` (Part 5).
5. `customop_cleanup()` — flush and fence.

When a kernel needs more stack than the tiny default budget, the wrapper is
reached **through** a stack switch rather than called directly. The driver is
`switch_stack_or_call_wrapper` (`_Z28switch_stack_or_call_wrapperjj` @
`stack_switch.o:0xd4`):

```
d4:  entry a1,48
d7:  const16 a4, _use_default_malloc ; force libc malloc on (s8i 1)
e3:  callx8 init_neuron_hbm_allocator
ec:  callx8 init_neuron_dataram_allocator
f5:  callx8 _init_libc_heap_allocator
107: callx8 _init_translate_ctx        ; build the 168B translate ctx (Part 4)
110: rsr.isl a4                        ; current stack limit
117: sub.a  a4, switched_stack_base, a4
122: saltu  a7, a4, a3                 ; remaining < requested?
12a: s8i    a7, switched_stack         ; record "did we switch?"
12d: bgeu.w15 a4,a3, 1e5               ; enough stack → call wrapper in place
135: const16 a4, neuron_hbm_allocate   ; else allocate an HBM stack...
141: callx8 a4
14c: const16 a4, neuron_translate_ctx  ; ...translate it...
                                         ; ...then saveContext/switchStack into it
```

So the two shapes are:

| Shape | When | Mechanism |
|---|---|---|
| **`<fn>_wrapper` in place** | `remaining_stack >= requested` (the common case, since the default budget is tiny) | Plain `callx8` on the wrapper on the existing local stack. |
| **`<fn>_stack_switch`** | `remaining_stack < requested` | `allocate_hbm_stack` → `neuron_translate_ctx`/`neuron_translate` to map it into the local 32-bit window → `saveContext`/`switchStack` to run the wrapper on the HBM stack. |

`saveContext` (`switch_stack.o:0x0`) snapshots `a0,a1,a2,a4..a7`, `EPC`,
`LBEG/LEND/LCOUNT`, `SAR`, `PS`, `PREFCTL`, `ISL` into a 92-byte frame at
`a1-128`; `switchStack` (`switch_stack.o:0x58`) loads the new `PS`/`ISL` and
`jx` into the wrapper; `switchBack` (`switch_stack.o:0x81`) reverses it.

> **QUIRK — `switchBack` rotates the register window by hand.** It reads `WB`
> (window base) via `rsr.wb`, recomputes WBC/WBP/WBN nibbles, and `wsr.wb`s a
> corrected value before reloading the saved state and `jx a8`. This is *not* a
> normal `retw`: the stack-switch crosses register-window frames, so the
> standard windowed return would underflow. *(HIGH / OBSERVED:
> `switch_stack.o:0x81..0x10d`.)* A reimplementation that uses a flat ABI
> instead of Xtensa register windows can replace this whole file with a simple
> `setjmp`/`longjmp`-style swap, but **must** preserve `LBEG/LEND/LCOUNT` and
> `ISL` across the switch or hardware loop and stack-limit state will be
> corrupted. See [stack-switch.md](stack-switch.md).

`STACK_SIZE` defaults to **4196 bytes** and is bounded by
`MAX_STACK_SIZE = 0x400000` (4 MiB); the default is deliberately tiny so the
common path stays in place. *(HIGH / CARRIED: [stack-switch.md](stack-switch.md).)*

---

## Part 2 — Argument Passing

Arguments are not passed in registers. The host serializes a **micro-code
argument stream** (a flat array of fixed-size records) into a per-op buffer; the
kernel pulls them in declaration order through a cursor. `customop_setup` arms
the cursor; the `customop_next_<type>` family advances it.

### 2.1 `customop_setup(bool)`

`_Z14customop_setupb` (`wrapper_api.o:0x224`) is the first call the wrapper
makes. It:

* calls `init_dma_queue` (arms the descriptor ring used by `dma_data_transfer`);
* clears `get_dst_tensor_called` (`.bss 0x808`) and resets the `arg_parser`
  state (`.bss 0x608`), including the cursor at `arg_parser+0x1f8`.

*(HIGH / OBSERVED: `0x233` reloc → `init_dma_queue`; `0x23c` → `.bss+0x808`;
`0x242` → `.bss+0x608`.)*

### 2.2 The `ARG_TENSOR` 48-byte descriptor

The tensor argument record is 48 bytes. This layout is canonical across Part 7;
it is reproduced here as the wire contract a host serializer must emit and a
kernel `next` routine must parse. *(HIGH / CARRIED: [customop-marshalling.md](customop-marshalling.md),
and OBSERVED below via `ArgParser::next_ucode_arg<UTensor>`.)*

| Offset | Size | Field | Values / meaning |
|---|---|---|---|
| `0x00` | 1 | `location` | `INVALID=0`, `SBUF=1`, `HBM=2` |
| `0x01` | 1 | `framework_shape_type` | framework-side shape kind |
| `0x02` | 1 | `dtype` | ISA dtype code (mapped in §2.4) |
| `0x03` | 1 | `resv` | reserved |
| `0x08` | 16 | shape-union | shape descriptor (framework-dependent) |
| `0x18` | 8 | storage-union | **HBM**: `u64` SoC address @ `0x18`; **SBUF**: `u32` local address @ `0x18` |
| `0x20` | 4 | `num_elem` | element count (`u32`) |
| `0x28` | 8 | `resv` | reserved / padding to 48 |

The arg-pull is `ArgParser::next_ucode_arg<UTensor>`
(`_ZN9ArgParser14next_ucode_argI7UTensorEET_v`), and its disassembly confirms
both the 48-byte stride and the field copy:

```
3:  l32i.n a5, [a3+0]                  ; a5 = base of record array
5:  l32i   a4, [a3+0x1f8]              ; a4 = byte cursor
8:  bgeu.w15 a4,a5, ...                ; bounds check
8:  add a6, a3, a4                     ; a6 = &record[cursor]
2c: s32i a10, [a3+0x1f8]              ; cursor += 1 record
2f..75: l32i [a6+8],[a6+12],[a6+16],[a6+20],[a6+24],[a6+28],
            [a6+32],[a6+36],[a6+40],[a6+44],[a6+48],[a6+52]
            → s32i into the 48B UTensor at [a2+8 .. a2+44]
```

> **NOTE.** The copy starts at record offset `+8` (the shape-union) and walks to
> `+52`; the first 8 bytes (`location/framework_shape_type/dtype/resv` + low
> half of shape-union) are read as a packed word and split with `extui …,0,8`.
> The cursor lives at `arg_parser+0x1f8` and is advanced **per record**, so the
> host must lay records out contiguously with this exact 48-byte stride.
> *(HIGH / OBSERVED.)*

### 2.3 The `customop_next_<type>` pull stream

All five pull entry points read the **same** cursor (`arg_parser` @ `.bss
0x608`) and dispatch through `next_ucode_arg`. Their addresses match the Part-7
pack exactly:

| Symbol | Addr | Returns | Contract |
|---|---|---|---|
| `_Z20customop_next_tensorv` | `0x35c` | `UTensor` (48 B by value) | Pulls one `ARG_TENSOR` record; the canonical tensor argument. |
| `_Z20customop_next_scalarv` | `0x390` | `c10::Scalar` | Pulls a scalar (wraps `UTensor`→`Scalar`). |
| `_Z17customop_next_intv` | `0x3c4` | `int` | Pulls a 32-bit integer immediate. |
| `_Z22customop_next_longlongv` | `0x3fc` | `long long` | Pulls a 64-bit integer immediate. |
| `_Z19customop_next_floatv` | `0x43c` | `float` | Pulls a 32-bit float immediate. |

*(HIGH / OBSERVED: all five resolve to `_ZN9ArgParser14next_ucode_argI7UTensorEET_v`
on the `arg_parser` global; e.g. `customop_next_int` @ 0x3cf/0x3d5 relocs.)*

> **GOTCHA.** Argument order is positional and **stateful** — the cursor only
> moves forward. The kernel must call the `customop_next_*` functions in the
> exact order the host serialized them; there is no random access and no
> type-tag check at pull time beyond the bounds check at `0x8` in
> `next_ucode_arg`. A mismatched declaration order silently reads the wrong
> bytes. *(HIGH / INFERRED from the single forward cursor.)*

### 2.4 ISA dtype → `c10::ScalarType` map (inline)

There is no dtype-helper symbol; the map is inlined into `UTensor::copy`
(`_ZN7UTensor4copyERKN2at6TensorE`) as an `if/else` ladder on the `dtype` byte.
Verified case-by-case from the disassembly (`movi a6, <ScalarType>` paired with
each `bnei a5, <code>`):

| ISA dtype code | `bnei`/`beqi` test | `c10::ScalarType` value | (kind) |
|---|---|---|---|
| `2` | `bnei a5,2` → `movi a6,1` | `1` | (e.g. int8/uint8 family) |
| `3` | `bnei a5,3` → `movi a6,0` | `0` | |
| `4` | `bnei a5,4` → `movi a6,2` | `2` | |
| `6` | `bnei a5,6` → `movi a6,15` | `15` | |
| `7` | `beqi a5,7` → `movi a6,5` | `5` | |
| `8` | `bnei a5,8` → `movi a6,3` | `3` | |
| `0xA` (10) | `bnei a5,10` → `movi a6,6` | `6` | |
| `0xC` (12) | `bnei a5,12` → `movi a6,4` | `4` | |
| *default* | falls to `+0x2a0` | `_Assert` / abort | invalid dtype traps |

*(HIGH / OBSERVED: `wrapper_api.o:.text._ZN7UTensor4copy…:0x28..0x82`.)* The
reverse direction (ScalarType→ISA) appears at `+0xa0..+0x12c` of the same
function. See [scalartype-dtype-rosetta.md](scalartype-dtype-rosetta.md) for the full table and the inverse.

> **CORRECTION.** This is an **inline** ladder, not a table lookup — searching
> for an `isa_to_torch_dtype` symbol returns nothing. Reimplementers must port
> the ladder, including the `_Assert` default that **aborts** on any unlisted
> code rather than returning a sentinel. *(HIGH / OBSERVED: no such symbol in
> `nm`; default branch targets the assert path.)*

---

## Part 3 — The Support-Routine Table

These are every device-runtime symbol a kernel may legally call. They are the
**stable internal ABI**: a reimplemented runtime must provide all of them with
the listed mangled names, signatures, and semantics, because compiled kernels
and the generated wrapper reference them by name.

### 3.1 Address translation

| Symbol | Signature | Semantics |
|---|---|---|
| `_Z16neuron_translatePvy` | `void* neuron_translate(void* local_addr, uint64_t hbm_addr)` | Map an HBM/SoC address into the local 32-bit window using `_ctx`. Walks 5 records; for each, if `(hbm_addr & mask) == ptr` returns `window + (hbm_addr & ~mask)`. |
| `_Z20neuron_translate_ctxv` | `void neuron_translate_ctx()` | Allocates/refreshes the live `_translation_ctx_t` pointed to by `_ctx`. |
| `_Z19_init_translate_ctxv` | `void _init_translate_ctx()` | One-time build of the 168-byte ctx (5 records + `next_alloc`). |
| `_Z31neuron_translate_mapping_lookupPvyPyS0_` | `… mapping_lookup(void*, uint64_t, uint64_t*, void*)` | Lower-level lookup returning the matched window/base. |

`neuron_translate` disassembly proves the 5-record / 32-byte-stride / mask-match
shape (records at ctx `+0`,`+32`,`+64`,`+96`,`+128`; each tests
`(addr&mask)==ptr` with the window at `+8`):

```
123: l32i a2,[a2+16] ; a6,[a2+20]      ; first record's mask
133: l32i a7,[a3+0]  ; a8,[a3+4]       ; first record's ptr
13b: and a6,a6,a5 ; and a9,a2,a4       ; (addr & mask)
143: xor a7,a9,a7 ; xor a15,a6,a8      ; compare to ptr
150: beqz a7, 204  (match → return window+offset)
153/17b/1a4/1d7: same test at +32/+64/+96/+128
```

*(HIGH / OBSERVED.)* The canonical struct (carried from
[neuron-translate-windows.md](neuron-translate-windows.md)):

```c
// _translation_ctx_t : 168 bytes
struct translation_record {       // 32 bytes
    uint64_t ptr;                 // +0   matched HBM/SoC base
    uint32_t window;              // +8   local-window base to add
    uint32_t _pad0;               // +12
    uint64_t mask;                // +16  match/clear mask
    uint64_t reg_location;        // +24  SoC register / window id
};
struct _translation_ctx_t {
    translation_record records[5];// +0   (5 × 32 = 160)
    uint8_t  next_alloc;          // +160 records-in-use / next free slot
    // +161..167 padding to 168
};
```

`_init_translate_ctx` (`translation.o:0x0`) corroborates `next_alloc` at
offset **160**: it does `s8i a6, a2, 160` (zero the count) at `0x52`, then
writes record fields at `+0/+4/+8/+16/+20/+24/+28`, `+48/+52`, `+80`, etc., and
loads the window constants `0x218/0x228/0x230` (the SoC window register ids).
*(HIGH / OBSERVED.)* `_ctx` itself is a 4-byte `.bss` pointer
(`translation.o:.bss _ctx`, size 4); `_sbuf_window` is an 8-byte `.bss` global
(size 8) holding the SBUF window descriptor.

### 3.2 Data transfer (`neuron_memcpy`)

| Symbol | Signature | Direction |
|---|---|---|
| `_Z13neuron_memcpyPvyj` | `void neuron_memcpy(void* dst_local, uint64_t src_hbm, uint32_t n)` | **HBM → local** (dir flag `1`) |
| `_Z13neuron_memcpyyPvj` | `void neuron_memcpy(uint64_t dst_hbm, void* src_local, uint32_t n)` | **local → HBM** (dir flag `0`) |
| `_Z24neuron_set_memcpy_method18NeuronMemcpyMethod` | `void neuron_set_memcpy_method(NeuronMemcpyMethod)` | Override the active method. |
| `_Z21dram_addr_to_soc_addrj` | `uint64_t dram_addr_to_soc_addr(uint32_t)` | DRAM→SoC address fix-up. |

Both `neuron_memcpy` overloads dispatch through the **method table** at `.data
0x200` (named `_GLOBAL__N_1::data_transfer_method_table`), selecting via the
active-method index at `.data 0x20c`
(`_GLOBAL__N_1::active_neuron_memcpy_method`):

```
65f: const16 a3, .data+0x20c ; movi a14, 1   ; HBM→local: dir = 1
697: const16 a6, .data+0x20c ; movi a14, 0   ; local→HBM: dir = 0
```

The table holds three function pointers, in this fixed order, with the active
method initialized to **2 = DMA**:

| `.data` offset | Slot | `NeuronMemcpyMethod` |
|---|---|---|
| `0x200` | `c_memcpy_data_transfer` (`_Z22c…Pvybj`) | `0` |
| `0x204` | `vec_memcpy_data_transfer` (`_Z24vec…Pvybj`) | `1` |
| `0x208` | `dma_data_transfer` (`_Z17dma…Pvybj`) | `2` |
| `0x20c` | `active_neuron_memcpy_method` | **default `2`** (DMA) |

*(HIGH / OBSERVED: `R_XTENSA_32` relocs at `.data 0x200/0x204/0x208`; raw bytes
at `0x20c` = `02 00 00 00`.)* All three backends funnel through
`memcpy_data_transfer_impl(char*,uint64_t,bool,uint32_t,bool)`
(`_Z25memcpy_data_transfer_implPcybjb`). See [data-transfer-backends.md](data-transfer-backends.md).

> **GOTCHA — the dataram-staging window is hard-asserted.** `data_transfer.o`
> carries the `.rodata` assert string
> `"dram_addr >= 0x80000 && dram_addr < 0x90000"`
> (`data_transfer.cpp:160`). The C/vector backends stage through this fixed
> on-core dataram/TCM window `[0x80000, 0x90000)`; a transfer outside it aborts.
> *(HIGH / OBSERVED.)*

### 3.3 Allocators

| Symbol | Signature | Pool | Align | Status |
|---|---|---|---|---|
| `_Z19neuron_hbm_allocatejPyj` | `int neuron_hbm_allocate(uint size, uint64_t* out, uint align)` | HBM | 64 B | `0` ok / `-1` fail |
| `_Z21neuron_hbm_deallocatey` | `void neuron_hbm_deallocate(uint64_t)` | HBM | — | — |
| `_Z25neuron_hbm_get_alloc_sizeyPj` | `int neuron_hbm_get_alloc_size(uint64_t, uint*)` | HBM | — | — |
| `_Z23neuron_dataram_allocatejPPvj` | `int neuron_dataram_allocate(uint size, void** out, uint align)` | dataram/TCM | 64 B | `0` ok / `-1` fail |
| `_Z25neuron_dataram_deallocatePv` | `void neuron_dataram_deallocate(void*)` | dataram | — | — |
| `_Z29neuron_dataram_max_free_spacePjj` | `int neuron_dataram_max_free_space(uint*, uint)` | dataram | — | — |
| `_Z25init_neuron_hbm_allocatorv` | `void init_neuron_hbm_allocator()` | HBM | — | — |
| `_Z29init_neuron_dataram_allocatorv` | `void init_neuron_dataram_allocator()` | dataram | — | — |
| `_ZN2at6neuron10tcm_mallocEj` | `void* at::neuron::tcm_malloc(uint)` | TCM | — | thin wrapper over dataram |
| `_ZN2at6neuron8tcm_freeEPv` | `void at::neuron::tcm_free(void*)` | TCM | — | — |
| `_ZN3c1018GetNeuronAllocatorEv` | `c10::Allocator* c10::GetNeuronAllocator()` | — | — | the ATen-facing allocator |

`neuron_hbm_allocate` writes a **64-bit** SoC/HBM address through `out`
(`uint64_t*`, addresses don't fit in 32-bit Q7); `neuron_dataram_allocate`
writes a **32-bit** on-core pointer through `out` (`void**`, dataram is in the
core's own address space). Both round the size up to a 64-byte alignment and run
the `xmem_heap_alloc` heap (e.g. `neuron_dataram_allocate` @ `0x1d0` uses a
default `align=64` in `a12`, calls `xmem_heap_alloc`, then `s32i a10,[a3]` /
`movi a2,-1` for the fail path). *(HIGH / OBSERVED & CARRIED:
[device-allocators.md](device-allocators.md).)*

### 3.4 SPMD identity + libc shims

| Symbol | Signature | Semantics |
|---|---|---|
| `_Z10get_cpu_idv` | `int get_cpu_id()` | This core's POOL lane id (`0..7`); reads a per-core global. |
| `_Z13get_cpu_countv` | `int get_cpu_count()` | **Returns the constant `8`** — 8 POOL cores. *(HIGH / OBSERVED: `movi.n a2,8; retw.n` @ `parallel.o:0x13`.)* |
| `fsync` | `int fsync(int fd)` | Device flush primitive (see Part 5); pairs with `memw`. |
| `malloc`/`free`/`realloc`/`memalign`/`memcpy`/`memset`/`memmove`/`memcmp` | libc | Newlib-style; routed through `get_libc_fn`. |

> **NOTE.** `get_cpu_count()` is a compiled-in constant `8`, not a register read.
> A reimplementation targeting a different POOL-core count must change this
> constant *and* the per-core LSP base spacing in Part 4 together — they encode
> the same fan-out. *(HIGH / OBSERVED.)* SPMD launch semantics:
> [multicore-spmd.md](multicore-spmd.md).

---

## Part 4 — The Device Memory Map a Kernel Sees

A custom-op kernel runs on a Q7 POOL core with a **32-bit local address space**.
Everything larger than that — HBM tensors, the spilled stack — is reached
through `neuron_translate` windows. The map below is the union of what the
support routines expose.

| Region | Address / window | Width | Reached via | Confidence |
|---|---|---|---|---|
| Local Q7 address space | 32-bit flat | `void*` | direct loads/stores | HIGH / OBSERVED (Q7 is a 32-bit core) |
| HBM tensor windows | `(addr & mask) == ptr → window + (addr & ~mask)` | 64-bit HBM ↔ 32-bit local | `neuron_translate` over `_ctx` (5 records) | HIGH / OBSERVED |
| dataram / TCM staging | **`[0x80000, 0x90000)`** | 64 KiB | `neuron_dataram_allocate` / `tcm_malloc`; data-transfer staging | HIGH / OBSERVED (assert string) |
| Per-core LSP base | **`0x84000000 + i · 0x200000`** (i = `get_cpu_id()`) | 2 MiB/core stride | local-scratchpad (LSP) ELF mapping | MED / CARRIED ([lsp-elf.md](lsp-elf.md)) |
| HBM scratch stack | `neuron_hbm_allocate`(≤ 4 MiB) then `neuron_translate` | 64-bit | `allocate_hbm_stack` / stack-switch | HIGH / OBSERVED |
| `extended_isa::sdk` scratch | `data_scratch`, `hbm_scratch` globals + sizes | — | firmware-provided scratch | HIGH / OBSERVED (`start_exit.o:.bss`) |

The `extended_isa::sdk` scratch globals a kernel inherits (all `B`-symbols in
`start_exit.o`):

| Global (mangled) | C name | Offset |
|---|---|---|
| `_ZN12extended_isa3sdk12data_scratchE` | `sdk::data_scratch` | `.bss 0x0` |
| `_ZN12extended_isa3sdk17data_scratch_sizeE` | `sdk::data_scratch_size` | `.bss 0x4` |
| `_ZN12extended_isa3sdk11hbm_scratchE` | `sdk::hbm_scratch` | `.bss 0x8` |
| `_ZN12extended_isa3sdk16hbm_scratch_sizeE` | `sdk::hbm_scratch_size` | `.bss 0x10` |
| `_ZN12extended_isa3sdk20dma_queue_m2s_offsetE` | `sdk::dma_queue_m2s_offset` | `.bss 0x18` |
| `_ZN12extended_isa3sdk20dma_queue_s2m_offsetE` | `sdk::dma_queue_s2m_offset` | `.bss 0x20` |

> **NOTE.** `neuron_translate` performs **no** thread-pointer / `rur` read — the
> per-core map is achieved by each core running its **own copy** of the image in
> its own address space (SPMD), not by an explicit per-core table inside
> `translation.o`. The per-core LSP base `0x84000000 + i·0x200000` is the
> address-space convention the firmware imposes, not a value computed inside the
> custom-op library. *(MED / CARRIED: [neuron-translate-windows.md](neuron-translate-windows.md),
> [lsp-elf.md](lsp-elf.md); the LSP base is a firmware constant, not in this binary.)*

> **GOTCHA — never dereference a 64-bit HBM address directly.** A `uint64_t`
> SoC/HBM address (e.g. the `0x18` storage-union of an `ARG_TENSOR` whose
> `location == HBM`) is **not** a valid Q7 pointer. It must be passed through
> `neuron_translate` (or copied with `neuron_memcpy`) first. Only an
> `ARG_TENSOR` with `location == SBUF` carries a directly-usable 32-bit local
> address at `0x18`. *(HIGH / INFERRED from the 32-bit core + the translate
> window machinery.)*

---

## Part 5 — Completion / Return

The kernel does not `return` a tensor in the C sense. It **publishes** a result
into a `.bss` global that the wrapper reads back, then flushes. Two writeback
paths and one cleanup primitive form the contract.

### 5.1 The writeback globals

| Global (`wrapper_api.o:.bss`) | Offset | Type | Role |
|---|---|---|---|
| `_GLOBAL__N_1::retval` | `0x80c` | pointer to result tensor | The published result. |
| `_GLOBAL__N_1::get_dst_tensor_called` | `0x808` | `bool` | Latches whether the kernel asked for the destination tensor. |
| `_GLOBAL__N_1::arg_parser` | `0x608` | `ArgParser` | The argument cursor (Part 2). |

### 5.2 `customop_return_tensor` (publish)

`_Z22customop_return_tensorRN2at6TensorE` (`wrapper_api.o:0x47c`) takes a
reference to an `at::Tensor`, marks `get_dst_tensor_called` (`.bss 0x808`),
loads `retval` (`.bss 0x80c`), and writes the result tensor fields back through
it (`l32i [a6+0/+4/+8/+12/+16/+20/+24/+28]` — the tensor handle/storage block).
*(HIGH / OBSERVED: relocs at `0x47f`→`.bss+0x808`, `0x48b`→`.bss+0x80c`.)*

### 5.3 `get_dst_tensor` (request the destination)

`_Z14get_dst_tensorv` (`wrapper_api.o:0x528`) sets `get_dst_tensor_called = 1`
(`s8i a5, .bss+0x808` with `a5 = 1` @ `0x539`/`0x53b`) and returns the runtime's
pre-allocated destination tensor (read from `retval`, `.bss 0x80c`). A kernel
that writes in place into the host-provided output tensor calls this instead of
allocating its own. *(HIGH / OBSERVED.)*

### 5.4 `customop_cleanup` — flush + fence

`_Z16customop_cleanupv` (`wrapper_api.o:0x5d4`) is the **last** call the wrapper
makes. It is exactly:

```
5d4: entry a1,32
5d7: const16 a2, fsync ; movi a10, 2
5e9: callx8 a2                ; fsync(2)
5ec: movi.n a10, 1
5ee: callx8 a2                ; fsync(1)
5f1: memw                     ; memory-write barrier
5fa: retw.n
```

i.e. **`fsync(2); fsync(1); memw;`** — flush descriptor channels 2 then 1, then
a `memw` barrier so all stores are globally visible before the op is reported
complete. *(HIGH / OBSERVED.)*

> **GOTCHA — order and barrier are mandatory.** `customop_return_tensor` (or
> `get_dst_tensor` writeback) must happen **before** `customop_cleanup`, and the
> trailing `memw` is the only thing guaranteeing the result tensor's stores are
> visible to the host/DMA engine when the op completes. Skipping `customop_cleanup`
> (or dropping the `memw`) yields stale or torn output even though the compute
> ran. *(HIGH / OBSERVED: `cleanup` is `fsync;fsync;memw` with no other
> side-effects.)*

---

## Conformance Checklist (the minimal contract)

A kernel `.o` is dispatchable iff **all** of the following hold:

1. **Defines** `_Z12get_func_cntv`, `_Z13get_func_namei`, `_Z12get_func_ptri`
   with consistent indexing. *(Part 1.1.)*
2. Each `get_func_ptr(i)` returns a wrapper that calls
   `customop_setup(bool)` → `customop_next_*` (in declaration order) → user body
   → `customop_return_tensor`/`get_dst_tensor` → `customop_cleanup`.
   *(Parts 2, 5.)*
3. **References only** the support routines in Part 3 by their exact mangled
   names; satisfies all undefined symbols against this library (or an
   ABI-identical reimplementation).
4. Treats `ARG_TENSOR` records as the 48-byte layout in §2.2 and only
   dereferences `HBM`-located addresses through `neuron_translate` /
   `neuron_memcpy`. *(Parts 2, 4.)*
5. Uses only `ISA dtype` codes in the §2.4 map (anything else aborts via the
   inline `_Assert`).
6. Targets `XTENSA_CORE=ncore2gp`, ELF32-LE; the final link resolves
   section-relative offsets to Q7 virtual addresses ([q7-elf-vaddr.md](q7-elf-vaddr.md)).

---

## Cross-references

* [customop-marshalling.md](customop-marshalling.md) — host-side serialization of the argument stream and
  the `ARG_TENSOR` wire format.
* [neuron-translate-windows.md](neuron-translate-windows.md) — full derivation of the 5-record translation
  ctx and window-match algorithm.
* [device-allocators.md](device-allocators.md) — `neuron_hbm_*` / `neuron_dataram_*` heap internals and
  the `xmem_heap` backing store.
* [data-transfer-backends.md](data-transfer-backends.md) — the C / vector / DMA `neuron_memcpy` backends and
  the dataram staging window.
* [stack-switch.md](stack-switch.md) — `switch_stack_or_call_wrapper`, `STACK_SIZE`/`MAX_STACK_SIZE`,
  and the `saveContext`/`switchBack` register-window machinery.
* [scalartype-dtype-rosetta.md](scalartype-dtype-rosetta.md) — the complete bidirectional dtype map.
* [lsp-elf.md](lsp-elf.md) — the per-core LSP base `0x84000000 + i·0x200000` and image layout.
* [q7-elf-vaddr.md](q7-elf-vaddr.md) — how section-relative offsets become Q7 virtual addresses.
* `../orientation/customop-end-to-end.md` — the narrative walk-through this page
  formalizes.

Forward links: the host-runtime side of registration and dispatch (Part 8) and
the collectives ABI (Part 9) are covered on their own pages once authored.
