# End-to-End ABI Synthesis

> **Scope — the whole custom-op host↔device ABI as one flow.** This is the
> **ABI-lane capstone**. Every other page in this section decodes *one organ* of
> the GPSIMD custom-op machine — the pointer, the marshalling, the translate
> windows, the allocators, the stack switch, the multicore identity, the build
> recipe. This page wires them into a **single coherent trace**: a user's C++
> kernel becomes eight per-core `.so`s (BUILD), is resolved and stood up on the
> Q7 POOL cluster (LOAD), is reached through the `kernel_info_table` dispatch
> (INVOKE), has its `at::Tensor` arguments materialised from on-wire descriptors
> (MARSHAL), runs against HBM through translate windows or staged DMA (EXECUTE),
> and writes its result back and unwinds the stack (RETURN). Each stage **cites
> its deep page**; this page does **not** re-derive their byte-exact anchors — it
> consolidates them and grounds the seams between them.
>
> This is the *flow* companion to [The Complete Custom-Op ABI](complete-customop-abi.md)
> (the device-side `libneuroncustomop.a` reference) and
> [The Device-Side Custom-Op ABI Reference](device-abi-reference.md) (the
> normative per-symbol contract). Where those answer "what is the ABI", this page
> answers "what happens, in order, on one invocation". For a gentler narrative
> walkthrough see the orientation page
> [A Custom Op, End to End](../orientation/customop-end-to-end.md).
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read this task
> from `ar`/`nm`/`readelf`/DWARF/native Xtensa disassembly), `INFERRED` (an ABI
> rule applied to an observed fact), `CARRIED` (consolidated from the cited deep
> page and re-confirmed here only where this synthesis spot-checks it).

> **NOTE — provenance & artifacts.** All claims are derived **solely from static
> analysis** of the shipped `aws-neuronx-gpsimd-customop-lib_0.21.2.0` package and
> the in-tree Xtensa toolchain. The device runtime is
> `custom_op/neuron/libneuroncustomop.a` (**md5 `ef49ef4b5bffb95e1f3df2e4c1a8f09e`,
> 898 172 bytes, 10 members**, each ELF32-Xtensa `REL`, not stripped, DWARF v4,
> producer `XtensaTools-14.09 clang version 10.0.1`); the build driver is
> `script/build_custom_op.py` (`__version__ = '0.21.2.0'`). Native disassembly
> used the shipped `xtensa-elf-objdump` with the device core registered
> (`XTENSA_SYSTEM=…/tools/ncore2gp/config`, `XTENSA_CORE=ncore2gp`); a benign
> "TIE checksum does not match" warning is emitted, the decode is byte-exact.
> Because each `.o` is relocatable, `.text`/`.data` offsets are section-relative.
> The domain is **AWS Annapurna Trainium** — the device is a Cadence Tensilica
> **Vision-Q7 NX "Cairo"** 512-bit FLIX/VLIW DSP, config `ncore2gp`, **8 cores per
> TPB POOL cluster**. There is no NVIDIA content here.

> **QUIRK — two version strings, both real.** The package version is
> **`0.21.2.0`** (`build_custom_op.py __version__`, matches the lib filename). The
> *ucode-lib* version string baked into `parallel.o` `.data` is **`1.21.1.0`**
> (`SUNDA_UCODE_LIB_VERSION_STR`, also `build_custom_op.py`'s
> `versions['ulib_to_ucode_version']`). These are two different version axes (the
> package vs the ucode-lib ABI), not a typo — cite both, invent neither.
> `[HIGH × OBSERVED]` (both re-read this task).

---

## 0. The whole machine in one paragraph

A GPSIMD custom op is a user-written C++ free function
`at::Tensor fn(const at::Tensor&, const at::Scalar&, int, float, long long …)`.
`build_custom_op.py` codegens a `wrapper.cpp` that drives the `customop_*`
marshalling, compiles the kernel + wrapper **once**, then links the *same* objects
**1× (single-core) or 8× (one `.so` per Q7 POOL core)** against per-core LSP linker
scripts, and strips/packs each into a fixed-location loadable library. At load time
the host runtime resolves the firmware substrate and registers each op via the
codegen's `get_func_*` table; the device CRT stands up the translate context (a
5-entry window TLB), the three `xmem` heaps, and the SDMA ring. On invocation the
POOL dispatcher routes the opcode through the `kernel_info_table` to the registered
launcher `<fn>_stack_switch`, which calls `switch_stack_or_call_wrapper` (optionally
moving onto a ≤4 MB HBM stack), then `<fn>_wrapper()`: `customop_setup(true)` slurps
the on-wire `ARG_TENSOR` descriptors into an `ArgParser` singleton;
`customop_next_<T>()` materialises each into an `at::Tensor` (whose data is a 16-byte
`Q7PtrType` = 64-bit HBM address + cached translate ctx) or a scalar; the kernel runs,
touching tensor data either lazily per element (`TensorAccessor` → `neuron_translate`
→ NX-local deref) or in bulk via TCM/stream staging (`neuron_memcpy` → default SDMA →
dataram); `customop_return_tensor` copies the result back through dataram-staged SDMA;
`customop_cleanup` drains and fences; and if the stack was switched the wrapper does
`j switchBack`. Each core self-identifies by PRID (`get_cpu_id`), partitions its share
(SPMD), and runs entirely on private memory with **no** cross-core sync in the library.

## 0a. The five-phase pipeline at a glance

| phase | what happens | deep page(s) |
|---|---|---|
| **BUILD** | kernel.cpp + schema → `build_custom_op.py` → `wrapper.cpp` → compile-once → link 1×\|8× vs LSPs → strip/pack → `.so` | [Codegen](build-custom-op-codegen.md), [Build Flow](build-flow.md), [LSP/ELF](lsp-elf.md), [Multicore SPMD](multicore-spmd.md) |
| **LOAD** | host resolves firmware image + opset presence-mirror → FLL loader drops `_cpuN.so` → `register_funcs` walks `get_func_*` → device init: translate ctx, 3 `xmem` heaps, SDMA ring | [Codegen §4](build-custom-op-codegen.md), [neuron_translate](neuron-translate-windows.md), [Allocators](device-allocators.md), [Data-Transfer](data-transfer-backends.md), [FlexLM gate](flexlm-licensing.md) |
| **INVOKE** | opcode → POOL `kernel_info_table` linear scan → `<fn>_stack_switch` → `switch_stack_or_call_wrapper` → `<fn>_wrapper` → `customop_setup(true)` | [POOL dispatch](../firmware/pool/pool-dispatch.md), [kernel_info_table](../firmware/pool/kernel-info-table.md), [Stack Switch](stack-switch.md), [Marshalling](customop-marshalling.md) |
| **EXECUTE** | kernel sees `at::Tensor`/`at::Scalar` → accessor (strided → translate → 5-entry TLB → NX deref) \| TCM staging (TensorStream → dataram → SDMA M2S/S2M) → coherency RAII → SPMD partition | [Accessor](tensor-accessor.md), [TensorStream/TCM](tensorstream-tcm.md), [Coherency](coherency-enforcer.md), [Q7PtrType](q7ptrtype.md), [Rosetta](scalartype-dtype-rosetta.md) |
| **RETURN** | `customop_return_tensor` (SDMA write-back) → `customop_cleanup` (drain+fence) → if(switched) `j switchBack` → free HBM stack | [Marshalling §6](customop-marshalling.md), [Stack Switch §6](stack-switch.md) |

---

## 1. BUILD — kernel → codegen → compile → 1×\|8× link → strip/pack

**Source of record:** [`build_custom_op.py` Codegen](build-custom-op-codegen.md),
[LSP / ELF](lsp-elf.md), [Multicore SPMD](multicore-spmd.md),
[Q7 ELF VADDR](q7-elf-vaddr.md). All `[HIGH × OBSERVED]` from verbatim script and
raw LSP text.

### 1.1 The user-kernel contract

The author writes **one** free function per op — `at::Tensor <fn>(<mapped args>)`.
The arg-type map the codegen enforces ([Codegen §3a](build-custom-op-codegen.md)):
`Tensor → const at::Tensor&`, `Scalar`/`number → const at::Scalar&`, `int → int`,
`float → float`, `long long → long long`; the return **must** be a single
`at::Tensor` (anything else raises at codegen). The user sees **PyTorch-level
types**, never the raw `Q7PtrType` / `NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR`
descriptors. Inside the kernel the author reaches tensor data via the accessor API
(`q7_data_ptr()`/`sizes()`/`strides()`, [Accessor](tensor-accessor.md)) or TCM/stream
staging ([TensorStream/TCM](tensorstream-tcm.md)), and may self-partition SPMD work
via `get_cpu_id()`/`get_cpu_count()` ([Multicore](multicore-spmd.md)). The in-place
output path `get_dst_tensor()` is a **manual** API the kernel may call — it is **not**
auto-emitted. `[HIGH × OBSERVED]`

### 1.2 `compile()` — five phases, compile once, link many

`compile(name, fn_names, src_files, schema, build_directory, …, multicore=False)`
([Codegen §1](build-custom-op-codegen.md)) runs five phases in order:
(1) `_compile_sources` — each user `.cpp` → `.o`, **independently**;
(2) `_create_wrapper` — parse schema, **emit `wrapper.cpp`**;
(3) `_compile_wrapper` — `wrapper.cpp` → `wrapper.o` (a **separate** TU);
(4) `_link` — 1× (single) or 8× (per-core);
(5) `_strip` — `xt-pkg-loadlib` pack/strip each `.so` (skipped iff the user passed
`-g` in `cflags`). The kernel and the wrapper **never share a TU**; they meet only at
link, wired by the codegen's forward declaration. `[HIGH × OBSERVED]`

The generated `<fn>_wrapper()` is the exact `customop_*` call sequence
([Codegen §3b](build-custom-op-codegen.md)):

```c
void <fn>_wrapper() {
    customop_setup(true);                         // hasReturn HARD-CODED true
    at::Tensor output = <fn>( customop_next_<T>()... );  // 1:1 in declared order
    customop_return_tensor(output);               // the COPY write-back path
    customop_cleanup();
    if (switched_stack) asm("j switchBack");       // the HBM-stack return wiring
}
int <fn>_stack_switch() {                          // the REGISTERED int() launcher
    return switch_stack_or_call_wrapper((uint32_t)<fn>_wrapper);  // STACK_SIZE=4196
}
```

The codegen also emits **once**: `get_func_cnt()=N`, `get_func_name(idx)` switch,
`get_func_ptr(idx)` switch (returning `<fn>_stack_switch`). These three are
**undefined references** in `libneuroncustomop.a`; the lib's `register_funcs`
(`start_exit.o`) walks them at load. `[HIGH × OBSERVED]`

### 1.3 The 8× per-core link — the SPMD link-time identity

From the **same** object set, `multicore=True` links **eight** `.so`s
`<base>_cpu0.so … <base>_cpu7.so`, each against a per-core LSP whose *only* material
difference is the SRAM origin ([Codegen §6](build-custom-op-codegen.md),
[LSP/ELF](lsp-elf.md), [Multicore §6b](multicore-spmd.md)):

```
sram0_0_seg org = 0x84000000 + cpu_id·0x200000,  len 0x200000  (2 MiB)
```

The eight windows **tile** `[0x84000000, 0x85000000)` = 16 MiB with no gap/overlap;
the single-core fallback (`cpu_single`) gets the whole 32 MiB. `0x84000000` is the
**pinned `hbm_scratch` 64 MB window** NX base ([Q7 ELF VADDR §1](q7-elf-vaddr.md)),
so each `.so`'s code+rodata+data+bss live in **that core's private 2 MiB
`hbm_scratch` sub-window**. The 4 KiB IRAM vectors are byte-identical across all 9.
The LSP's `MEMORY{}` has **exactly two regions** (`iram0_0_seg 0x0`/`0x1000` +
`sram0_0_seg`); it places **no** stack and **no** heap — those are runtime objects
(§2). "FLL" = **Fixed Location Library** (the absolute LSP origins are why the image
is non-PIC-relocated at a fixed address). The core ID is thus encoded **twice**: at
LINK time (the SRAM origin) and at RUN time (the PRID, §4.5). `[HIGH × OBSERVED]`

> **GOTCHA — the customer `_cpuN.so` are NOT in the shipped package.** They are
> produced on the customer machine by `build_custom_op.py` from the customer kernel.
> What ships — and what this whole synthesis decodes — is the **emitter**
> ([Codegen](build-custom-op-codegen.md)), the **LSP recipes** ([LSP/ELF](lsp-elf.md)),
> and the **runtime lib** every emitted symbol resolves against. `[HIGH × OBSERVED]`

---

## 2. LOAD — image resolve → opset mirror → on-device runtime init

**Source of record:** the host load path (consolidated from the runtime survey),
[Stack Switch §3](stack-switch.md) (the init prologue),
[neuron_translate §5](neuron-translate-windows.md),
[Allocators](device-allocators.md), [Data-Transfer](data-transfer-backends.md),
[FlexLM gate](flexlm-licensing.md).

### 2.1 The host/device boundary

The host runtime resolves the **firmware/runtime images** embedded in the host lib
(a multi-level image-list walk + region/flavor switch) and maintains an
**opset presence-mirror** (a 256-slot opcode table whose per-opcode entries are
lazily-allocated specialization-presence bitmaps). That mirror keys on the same
`(opcode, spec)` pair the **device** `kernel_info_table` dispatch uses
([kernel_info_table](../firmware/pool/kernel-info-table.md)). The boundary
([complete-customop-abi §0](complete-customop-abi.md)): the host resolver serves the
device **firmware substrate**; the customer's built `_cpuN.so` is a **separate**
image produced on the customer machine and dropped onto each Q7 core by the **FLL
loader** (`-lloader`). `libneuroncustomop.a` is the per-core runtime that `.so` links
against. `[HIGH × CARRIED]`

### 2.2 The registration handshake

The FLL loader places `<base>_cpuI.so` on Q7 core *I* at the fixed `sram0_0_seg`
address. The library's `register_funcs` (`start_exit.o`) calls the generated
`get_func_cnt()` for *N*, then loops `get_func_name(i)` + `get_func_ptr(i)`,
registering each op's `<fn>_stack_switch` launcher with the loader callback
`SUNDA_UCODE_LIB_STATUS(void* handle, const char* name, int(*fn)())`. So the
generated `wrapper.cpp` is the **table-of-contents** the library reads to expose the
customer's ops ([Codegen §4](build-custom-op-codegen.md),
[complete-customop-abi §1](complete-customop-abi.md)). The `entry_func` ABI gates the
handoff block (`< 36` size → `FAILURE_ABI_MISMATCH`) and the kernel count
(`< 0x8000`). `[HIGH × OBSERVED]`

> **NOTE — the licensing gate is a *build*-time wall, not a runtime one.** A FlexLM
> checkout fences the `xt-clang++`/`xt-pkg-loadlib` toolchain that *builds* the
> `.so`; it does **not** appear in the device runtime path. See
> [FlexLM Licensing Gate](flexlm-licensing.md). `[HIGH × CARRIED]`

### 2.3 The on-device init prologue

Before any kernel runs, `switch_stack_or_call_wrapper` stands up the whole runtime
([Stack Switch §3](stack-switch.md), byte-exact native disasm):

```c
_use_default_malloc = 1;                 // force libc malloc DURING init
init_neuron_hbm_allocator();             // the HBM-scratch xmem heap   [Allocators]
init_neuron_dataram_allocator();         // the dataram/TCM xmem heap   [Allocators]
_init_libc_heap_allocator();             // the libc backing heap
_use_default_malloc = 0;                 // switch to the neuron allocators
_init_translate_ctx();                   // the 5-entry window TLB       [neuron_translate]
```

`customop_setup` additionally calls `init_dma_queue()` (the SDMA ring,
[Data-Transfer](data-transfer-backends.md)). `[HIGH × OBSERVED]`

### 2.4 The three runtime constructs the init builds

- **The 5-entry translate ctx** — `_init_translate_ctx` carves the 168-byte
  `_translation_ctx_t` (5 × 32-byte `_map_record {ptr@0, window@8, mask@16,
  reg_location@24}` + `uint8_t next_alloc@160`) from the host-provided
  `data_scratch_map`. Three windows are dynamic 16 MB (`0x07/0x09/0x0a000000` →
  `MEM_WINDOW3/5/6_LO` at `0x100218/228/230`), two are pinned 64 MB (SBUF
  `0x80000000`, `hbm_scratch` `0x84000000`). `next_alloc` recycles **only** the
  three dynamic slots (`% 3`). `neuron_translate_ctx()` returns this singleton;
  every `Q7PtrType.ctx_ptr` is stamped from it. ([neuron_translate §2/§5](neuron-translate-windows.md))
- **The three `xmem` heaps** — all the same Cadence `xmem` first-fit,
  address-sorted, coalescing free-list (88-byte mgr, 32 block headers, 64-byte
  align, `_lock=NULL` → **no** locking). `neuron_hbm_allocate(uint, uint64*, uint)`
  @`0xb0` returns a 64-bit HBM SoC addr; `neuron_dataram_allocate(uint, void**,
  uint)` @`0x1d0` returns a directly-dereferenceable 32-bit NX address. OOM →
  return **−1/NULL**, never abort (the abort lives one layer up, in the stack-switch
  caller). ([Allocators](device-allocators.md))
- **The SDMA ring** — `init_dma_queue()` arms the descriptor ring backing the DMA
  memcpy backend. ([Data-Transfer](data-transfer-backends.md))

`[HIGH × OBSERVED]` (struct sizes/offsets re-confirmed via DWARF;
`init_neuron_*_allocator` / `_init_translate_ctx` imports re-confirmed in
`stack_switch.o`).

> **GOTCHA — `_lock=NULL` is the *design*, not an oversight.** The heaps run without
> locks **because** the multicore model is per-core-private memory (§4.5): each core
> owns its own heap, so there is no shared structure to race. The "no-lock" finding
> and the "no cross-core sync" finding are the *same* fact seen from two pages.
> `[HIGH × OBSERVED]`

---

## 3. INVOKE — opcode → kernel_info_table → launcher → setup

**Source of record:** [POOL dispatch](../firmware/pool/pool-dispatch.md),
[kernel_info_table](../firmware/pool/kernel-info-table.md),
[Stack Switch](stack-switch.md), [Marshalling](customop-marshalling.md),
[Rosetta](scalartype-dtype-rosetta.md).

### 3.1 The device dispatch

The SEQ engine fetches the instruction stream and hands opcodes to the POOL cores;
each per-core POOL engine runs the `kernel_info_table` dispatch. The table is **17
records × 8 bytes @ VMA `0x02000380`** (in the firmware EXTISA data band, **not**
the custom-op `0x84000000` band — these are *different programs* in the same NX
space, see [Q7 ELF VADDR §4c](q7-elf-vaddr.md)). Each record packs a `(spec, opcode)`
key + a `u32` funcVA (`R_XTENSA_RELATIVE`, in the `0x01000000` `.text` band). Lookup
is a **linear scan** matching the packed key; on hit, `callx8 funcVA`. The DKL
(dynamic-kernel-load) build is the device path that loads a custom-op prelink library
at runtime and dispatches into it via the **same** `kernel_info_table` mechanism.
([POOL dispatch](../firmware/pool/pool-dispatch.md),
[External-Lib Loader](../firmware/pool/external-lib-loader.md)) `[HIGH × OBSERVED]`

### 3.2 The launch handoff (the stack switch)

The registered `<fn>_stack_switch` calls
`switch_stack_or_call_wrapper(&<fn>_wrapper, 4196)`. After the init prologue (§2.3),
it tests `remaining = SP − ISL` (the HW stack-limit SR): if `remaining ≥ stack_size`
it `callx8`s the wrapper on the tiny on-core stack (the **common** case — the 4196-B
default is deliberately tiny); else it sets `switched_stack=true`, `neuron_hbm_allocate`s
a ≤4 MB (`MAX_STACK_SIZE 0x400000`) HBM stack, `neuron_translate`s it to a 32-bit NX
SP, lays out a downward stack, `saveContext` + window-spill, `switchStack` installs
SP/PS and `ISL=new_stack_base`, and `jx`s into the wrapper. The kernel **arguments do
not cross the switch** — they are pulled by `customop_next_*` from the `ArgParser`
once the wrapper runs, on whichever stack. ([Stack Switch](stack-switch.md)) `[HIGH × OBSERVED]`

### 3.3 `customop_setup(true)` — the open handshake

`customop_setup` arms the DMA queue (`init_dma_queue()`), clears the
`get_dst_tensor_called` flag (`.bss 0x808`), and binds the on-wire arg blob into the
global `ArgParser` singleton (`.bss 0x608`, 512 B). The `ArgExtractor` (504 B) holds
`args_[10]` (10 × 48-byte `ARG_TENSOR`) + `arg_types_[10]`, bounded by
`max_args_ = 10`. `hasReturn` is hard-coded `true` by the codegen.
([Marshalling §5.1](customop-marshalling.md),
[complete-customop-abi §3.3](complete-customop-abi.md)) `[HIGH × OBSERVED]`

### 3.4 The on-wire tensor descriptor

Every tensor argument **and** the output arrive as a 48-byte
`NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR` (DWARF `byte_size = 48`, re-confirmed this
task):

| off | field | meaning |
|---|---|---|
| `+0` | `location` | `INVALID=0 / SBUF=1 / HBM=2` — selects the translate window |
| `+1` | `framework_shape_type` | `INLINE_SHAPE8D=0 / OUT_OF_LINE_SHAPE=1 / INLINE_SHAPE4D=2` |
| `+2` | `dtype` | `NEURON_ISA_TPB_DTYPE` (1 byte) — selects the `ScalarType` (§6) |
| `+8` | `framework_shape` | 16-byte union (inline 4D/8D dims \| out-of-line ref) |
| `+24` | `storage` | 16-byte union: HBM `{u64 addr@24; u32 num_elem@32}` \| TPB `{u32 addr@24; …}` |
| `+40` | `reserved1[8]` | — |

`storage.hbm.addr` (u64) becomes `Q7PtrType.hbm_addr`; `dtype@+2` selects the
`ScalarType`; `location@+0` selects SBUF vs HBM. ([Marshalling §2](customop-marshalling.md))
`[HIGH × OBSERVED]`

### 3.5 The validation gates (the `utypes.hpp` assert table)

`num_args < 10`; `curr_arg_ < num`; `type == ARG_TYPE_TENSOR`
(`arg_parser.hpp:66/67`); `xt_ptr_` non-null (`utypes.hpp:64`); the **dtype-match
gate** `aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)`
(`utypes.hpp:65`); `num_dim != 0` (`utypes.hpp:139`); inline-shape-only
(`utypes.hpp:94`). An ISA dtype with no `ScalarType` target is structurally
**rejected** here (it aborts via `_Assert`, `utypes.hpp:37`). ([Marshalling §6](customop-marshalling.md),
[Rosetta §3/§5](scalartype-dtype-rosetta.md)) `[HIGH × OBSERVED]`

---

## 4. EXECUTE — the kernel touches data

**Source of record:** [Accessor](tensor-accessor.md),
[TensorStream/TCM](tensorstream-tcm.md), [Coherency](coherency-enforcer.md),
[neuron_translate](neuron-translate-windows.md), [Data-Transfer](data-transfer-backends.md),
[Multicore](multicore-spmd.md).

### 4.1 Lazy per-element — the strided accessor

`t.accessor<T,N>()` builds a 40-byte `TensorAccessor` holding the `Q7PtrType` **by
value** and a `CoherencyEnforcer&`; its ctor acquires coherence (RAII; the virtual
dtor releases). Multi-dim indexing does only `Q7PtrType::operator+<T>` (**64-bit SoC
address arithmetic, no HBM touch**) while peeling dimensions; the single
`neuron_translate` happens at the **leaf**, through a `TensorElementReference` proxy
that defers the touch to `operator T()/operator=` so no translate window is held
across accesses. Net offset = `sizeof(T) · Σ strides[k]·idx[k]` — PyTorch strided
addressing over a 64-bit HBM address. ([Accessor](tensor-accessor.md)) `[HIGH × CARRIED]`

### 4.2 The terminal translate — the only HBM-touching step

`Q7PtrType::operator[]<T>` → `neuron_translate(ctx_ptr, hbm_addr + sizeof(T)·idx)`.
`neuron_translate` (`_Z16neuron_translatePvy` @`0x120`) does a fully-unrolled 5-way
associative scan: **HIT** (pure arithmetic) = `window + (addr & ~mask)`; **MISS**
(at `0x215`) evicts the round-robin victim (slots 0/1/2 only), installs the tag, and
**programs the HW window register** at `reg_location` (`MEM_WINDOW3/5/6_LO`), flanked
by `memw` fences. A ≤4 MB region fits one 16 MB dynamic window; SBUF and `hbm_scratch`
are pinned 64 MB (HIT-only). ([neuron_translate §4/§6](neuron-translate-windows.md))
`[HIGH × OBSERVED]`

> **GOTCHA — `neuron_translate` is NOT pure for cold addresses.** The first touch of a
> never-before-seen 16 MB region reprograms a hardware window register (the MMIO
> writes in the miss path). A working set > 3 distinct dynamic regions thrashes —
> exactly the transient-reference hazard the accessor's leaf-only-translate design
> avoids. `[HIGH × OBSERVED]`

### 4.3 Bulk sequential — TCM/stream staging

For sweeps the kernel uses the buffered families instead of per-element translate:
`TensorStream` / `TensorReadStreamAccessor` / `TensorWriteStreamAccessor` (a 4 KiB
dataram buffer auto-refilled/flushed via `neuron_memcpy`, tracked by the coherency
enforcer), or `TensorTcmAccessor` (manual `tcm_malloc`'d staging, **untracked** by the
enforcer — the [Coherency §5](coherency-enforcer.md) asymmetry). **DATARAM == TCM**:
`tcm_malloc` *is* `neuron_dataram_allocate`. ([TensorStream/TCM](tensorstream-tcm.md))
`[HIGH × CARRIED]`

### 4.4 The transfer backend

`neuron_memcpy` (overloads `_Z13neuron_memcpyPvyj` @`0x65c` HBM→local /
`_Z13neuron_memcpyyPvj` @`0x694` local→HBM) dispatches through a **3-entry method
table** @`.data 0x200` `{C_MEMCPY=0, VEC_MEMCPY=1, DMA=2}` by the runtime-settable
`active_neuron_memcpy_method` (`.data 0x20c` = **`02` = DMA default**, re-verified this
task). The DMA path builds a CME BD ring, fences with two `memw`, writes the **M2S**
inc reg (read/refill, HBM→dataram) or **S2M** inc reg (write/flush, dataram→HBM), and
busy-polls the completion BD. The local dataram buffer is relocated into a per-core
64 KiB SoC aperture (`window_index = 2·cpu_id + 9`) so the SDMA engine can address it.
([Data-Transfer](data-transfer-backends.md)) `[HIGH × OBSERVED]`

> **GOTCHA — `neuron_set_memcpy_method` accepts an out-of-range method.** The guard is
> `bgeui … 4` — it rejects only `method ≥ 4`, so `method == 3 == MAX_METHODS` **passes**
> and leaves the active method indexing one past the 3-entry table. Guard `≥ 3`, not
> `≥ 4`. ([Data-Transfer](data-transfer-backends.md),
> [complete-customop-abi §2](complete-customop-abi.md)) `[HIGH × OBSERVED]`

### 4.5 The coherency model and SPMD partition

Every `at::TensorBase` embeds a 16-byte mutable `CoherencyEnforcer`
`{buffered_readers_, buffered_writers_, unbuffered_accessors_, policy_
(COHERENT=0/INCOHERENT_VERBOSE=1/INCOHERENT_QUIET=2)}`. Accessors
acquire-in-ctor/release-in-dtor (RAII): many buffered readers OR many unbuffered MAY
coexist; a buffered writer is exclusive; cached and direct never mix. It is
single-thread advisory bookkeeping (no atomics) that gates *whether* an accessor may
open, never the data path. ([Coherency](coherency-enforcer.md))

The model is **SPMD**: each core self-IDs by `get_cpu_id()` = a cached **raw
`rsr.prid`** (dedicated SR **235 = 0xEB**, **NOT** MISC, no mask/shift — re-confirmed
this task) in `[0,8)`, and `get_cpu_count()` = constant **8**. Memory is per-core
private (private 256 KiB dataram, private 64 KiB SoC aperture `2·prid+9`, private
2 MiB `hbm_scratch` sub-window). There is **zero** atomics/barriers/semaphores in the
whole archive; any cross-core rendezvous would come from outside this library.
([Multicore §2/§8](multicore-spmd.md)) `[HIGH × OBSERVED]`

---

## 5. RETURN — write-back → drain/fence → stack restore

**Source of record:** [Marshalling §6](customop-marshalling.md),
[Stack Switch §6](stack-switch.md), [TensorStream/TCM](tensorstream-tcm.md),
[Data-Transfer](data-transfer-backends.md).

### 5.1 `customop_return_tensor(at::Tensor& output)`

The codegen emits the **copy** path. `customop_return_tensor` (`0x47c`) loads the
output descriptor from `(anon)::retval` (`.bss 0x80c`), resolves its `location`
(SBUF adds `_sbuf_window`), and — unless `get_dst_tensor_called` is set (the
**one-output-claim** protocol) — runs `UTensor::copy(output)`. `copy` asserts the
dtype-match gate (`utypes.hpp:65`), then **stages through a dataram bounce buffer**:
`neuron_dataram_allocate` a scratch span, `neuron_memcpy` HBM→dataram and
dataram→HBM (the Q7 core cannot stream HBM→HBM directly), `neuron_dataram_deallocate`.
([Marshalling §6](customop-marshalling.md)) `[HIGH × OBSERVED]`

> **NOTE — the in-place alternative.** If the kernel instead called `get_dst_tensor()`
> it wrote results directly into the output storage; the flag makes the later
> `customop_return_tensor` a no-op. The hazard is calling **neither**. The codegen
> emits only the copy path; `get_dst_tensor()` is manual. `[HIGH × OBSERVED]`

### 5.2 `customop_cleanup()` — drain + fence

`customop_cleanup` (`0x5d4`) is `fsync(2); fsync(1); memw` — a DMA-queue drain on both
directions then a memory-write fence, draining in-flight DMA/SBUF writes before the op
returns. ([Marshalling §5.2](customop-marshalling.md)) `[HIGH × OBSERVED]`

> **CORRECTION — `customop_cleanup` does NOT call `respond()`.** An earlier framing
> (carried into SX-ABI-18 §5.2) described cleanup as "respond + fsync". The decoded
> body is **`fsync(2); fsync(1); memw`** — a *drain + fence*, with no `respond`. The
> `respond(TPB_WRITE_RESPONSE)` calls belong to the **setup-side** handshake
> (`rd_args_from_insns` acknowledging each pulled arg payload), **not** to cleanup.
> Use [Marshalling §5.2](customop-marshalling.md) as the settled fact. **Reconcile
> note for Part-7:** SX-ABI-18 §5.2 and §0 ("cleanup responds+fsyncs") should be
> corrected to `fsync(2);fsync(1);memw`. `[HIGH × OBSERVED]`

### 5.3 Stack restore

If `switched_stack`, the codegen tail does `j switchBack`. `switchBack`
(`switch_stack.o`) rotates `WINDOWBASE`, restores the old SP/SRs/ISL from the
saved-context block, and `jx`s back to the resume label in
`switch_stack_or_call_wrapper`, which `deallocate_hbm_stack()`s the HBM block and
returns 0. If the switch was **not** taken, the wrapper falls off the end via a normal
windowed `retw` on the on-core stack. The returned `int` is discarded — the kernel's
output went back via `customop_return_tensor`, not this int.
([Stack Switch §6](stack-switch.md)) `[HIGH × OBSERVED]`

---

## 6. The dtype Rosetta — the one translation across the seam

**Source of record:** [ScalarType ↔ DTYPE Rosetta](scalartype-dtype-rosetta.md).

One dtype code space (`NEURON_ISA_TPB_DTYPE`) is shared by the custom-op descriptor,
the ucode decoder, the SDMA descriptor, and the collective op. The **only**
translation is ISA-code → `c10::ScalarType` at the `at::Tensor` boundary, realized
**inline** in `UTensor::operator at::Tensor() const` as an **if/else compare chain**
(not a jump table). There is no reverse `torch_to_isa_dtype` — the ABI is one-directional.

> **CORRECTION — there are EXACTLY 8 marshallable dtypes, not 9; FP32R aborts.** The
> backing SX-ABI-18 report (§VII.7.2) lists **9** marshallable dtypes and maps
> **FP32R (0xB) → Float**. The committed [Rosetta](scalartype-dtype-rosetta.md)
> page — and a direct decode this task of the ordinal tree in
> `_ZNK7UTensorcvN2at6TensorEEv` — shows the accept set is the **eight** compare
> immediates `{2, 3, 4, 6, 7, 8, 10, 12}`; **FP32R (0xB) is NOT in it** and falls
> through to the abort arm (`_Assert`, `utypes.hpp:37`). Use **8**, and FP32R →
> **abort**. **Reconcile note for Part-7:** SX-ABI-18 §VII.7.2/§7.3 ("9 marshallable",
> "FP32R → Float, ALLOW_FP32R-gated") must be corrected to 8 / FP32R-aborts.
> `[HIGH × OBSERVED]` (the eight immediates re-read this task).

The eight marshallable codes (ISA → `ScalarType`, with element size):

| ISA code | DTYPE | ScalarType | elem |
|---|---|---|---|
| `0x2` | `INT8` | `Char` (1) | 1 |
| `0x3` | `UINT8` | `Byte` (0) | 1 |
| `0x4` | `INT16` | `Short` (2) | 2 |
| `0x6` | `BFLOAT16` | `BFloat16` (15) | 2 |
| `0x7` | `FP16` | `Half` (5) | 2 |
| `0x8` | `INT32` | `Int` (3) | 4 |
| `0xA` | `FP32` | `Float` (6) | 4 |
| `0xC` | `INT64` | `Long` (4) | 8 |

Everything else — `INVALID`, `UINT64`, `UINT16`, `UINT32`, **`FP32R`**, `FP8_EXP3/4/5`
— has **no** same-meaning `ScalarType` in this pre-FP8 `c10` build and aborts at the
dtype-match gate. ([Rosetta §1](scalartype-dtype-rosetta.md)) `[HIGH × OBSERVED]`

---

## 7. The unified memory model

**Source of record:** [Q7 ELF VADDR](q7-elf-vaddr.md),
[neuron_translate](neuron-translate-windows.md).

A Q7 core dereferences a **32-bit NX-local** space; the 57-bit Cayman/Sunda SoC
physical space is reached through HW window registers. The single-core NX map
([Q7 ELF VADDR §1](q7-elf-vaddr.md)):

| NX base / range | what | kind |
|---|---|---|
| `[0x00000, 0x80000)` | low-NX IRAM (custom-op uses first 4 KiB for vectors) | DIRECT |
| `[0x80000, 0x90000)` | per-core on-core **dataram** (xmem dataram + libc heaps + `.bss`) | DIRECT (no TLB) |
| `[0x100000, 0x100850)` | NX **MEM REG** MMIO block (`MEM_WINDOW0..7`, etc.) | DIRECT (MMIO) |
| `0x07/09/0a000000` | 3 dynamic 16 MB windows (`MEM_WINDOW3/5/6`, round-robin `%3`) | DYNAMIC TLB |
| `0x80000000` | SBUF 64 MB pinned window | PINNED |
| `0x84000000 (+i·2 MiB)` | `hbm_scratch` 64 MB pinned; **custom-op `.so` lives in the per-core 2 MiB slice** | PINNED |

> **CORRECTION — the firmware `.data` band is `0x02000000`, not `0x00080000`.** The
> backing brief (and SX-ABI-18 §VI) conflated two regions. The carved EXTISA firmware
> ELF's data band — where `kernel_info_table @0x02000380` lives — is **`0x02000000`**
> ([Q7 ELF VADDR §4a](q7-elf-vaddr.md)). `0x00080000` is the **dataram DMA-staging
> window** `[0x80000, 0x90000)`, a *different* region. Both are real; they are
> distinct facts. (This is the same correction the committed [q7-elf-vaddr §2](q7-elf-vaddr.md)
> already records; restated here so the synthesis does not reintroduce the conflation.)
> `[HIGH × OBSERVED]`

> **NOTE — the allocator proxy base `0x80000000` ≠ the SBUF window NX base.** The HBM
> `xmem` allocator uses `0x80000000` as offset-bookkeeping; it numerically coincides
> with the SBUF window but is never dereferenced through it. Do not conflate.
> ([Allocators](device-allocators.md), [Q7 ELF VADDR §1](q7-elf-vaddr.md)) `[HIGH × OBSERVED]`

---

## 8. The master end-to-end trace (C pseudocode)

One custom-op invocation, top to bottom, naming the real symbols and citing the deep
page per stage. `[HIGH × OBSERVED on the call order; CARRIED on each body.]`

```c
/* ========================= BUILD (host machine) =========================== */
/*  build_custom_op.py: compile kernel.cpp + wrapper.cpp ONCE, link 8x.       */
/*  [Codegen][LSP/ELF][Multicore]                                             */
//  for i in 0..7:  link {kernel.o, wrapper.o} + libneuroncustomop.a + libc10.a
//                   -lloader -mlsp=lsp_fll_load_cpu{i}  -lxmem -lhal -lc++-e -lm -lgcc libcweak.a
//                   => <base>_cpu{i}.so  (sram0_0_seg org = 0x84000000 + i*0x200000)
//  xt-pkg-loadlib -e lib_func => <base>_cpu{i}.{packed,stripped}.so

/* ========================= LOAD (host + device) =========================== */
host_resolve_firmware_image(idx, region, flavor);    /* host image-list walk          */
opset_add_instruction(opcode, spec);                 /* host 256-slot presence mirror */
fll_loader_drop("<base>_cpu{me}.so", /*core=*/me);   /* fixed sram addr   [Codegen]   */
register_funcs(loader_cb);  /* walks get_func_cnt/name/ptr; registers <fn>_stack_switch */

/* on-device init prologue (inside switch_stack_or_call_wrapper)  [Stack Switch]       */
_use_default_malloc = 1;
init_neuron_hbm_allocator();        /* [Allocators]  */
init_neuron_dataram_allocator();    /* [Allocators]  */
_init_libc_heap_allocator();
_use_default_malloc = 0;
_init_translate_ctx();              /* 5-entry window TLB  [neuron_translate] */
/* (customop_setup later calls init_dma_queue() — the SDMA ring) [Data-Transfer]      */

/* ========================= INVOKE (device) ================================ */
/* SEQ -> POOL kernel_info_table LINEAR SCAN (17 records @0x02000380)  [POOL dispatch]*/
funcVA = kernel_info_lookup(opcode, spec);           /* callx8 funcVA = <fn>_stack_switch */

int <fn>_stack_switch() {                            /* the registered launcher           */
    return switch_stack_or_call_wrapper((uint32_t)<fn>_wrapper);  /* stack_size=4196   */
    /* SP - ISL test: tiny stack -> callx8 wrapper; else neuron_hbm_allocate <=4MB HBM */
    /* stack, neuron_translate to NX SP, saveContext/switchStack, jx wrapper [Stack]   */
}

void <fn>_wrapper() {
    customop_setup(true);                            /* ArgParser singleton; init_dma_queue */
                                                     /*   [Marshalling §5.1]                */

    /* MARSHAL: per arg, in declared order, via ONE templated reader            */
    /*   ArgParser::next_ucode_arg<UTensor> -> UTensor::operator T()  [Marshalling]      */
    at::Tensor x = customop_next_tensor();
    /*   UTensor::operator at::Tensor():                                         */
    /*     Q7PtrType q7{ desc.storage.hbm.addr, neuron_translate_ctx() };  [Q7PtrType]   */
    /*     UniqueQ7Ptr up{ q7, Q7Deleter::delete_nothing };  // INPUT => non-owning      */
    /*     storage = make_intrusive<NeuronStorageImpl>(num_elem, move(up));   (48 B)     */
    /*     impl    = make_intrusive<NeuronTensorImpl>(storage, key,                       */
    /*                  TypeMeta(isa_to_torch_dtype(desc.dtype)));  (216 B)  [Rosetta]    */
    /*     t.set_sizes_contiguous(sizes);  return make_tensor(impl); // CPU + poisoned    */
    int        k = customop_next_int();    /* scalar = degenerate 1-elem tensor:          */
    float      s = customop_next_float();  /*   neuron_translate(ctx, addr); *(T*)win      */
    /*   [dtype gate at writeback: aten.dtype == isa_to_torch_dtype(desc.dtype)]          */

    /* ========================= EXECUTE (kernel) ============================ */
    at::Tensor y = <fn>(x, k, s);          /* USER kernel; SPMD: get_cpu_id()/_count()    */
    /*  lazy:   t.accessor<T,N>()[i..] -> Q7PtrType::operator[] -> neuron_translate        */
    /*            -> 5-entry TLB (HIT=arith | MISS=evict %3 + program MEM_WINDOW3/5/6)      */
    /*            -> NX-local deref                                  [Accessor][neuron_tr]  */
    /*  bulk:   TensorStream/TensorTcm -> neuron_memcpy -> method table (DMA default)       */
    /*            -> dma_data_transfer -> CME BD ring -> M2S(read)/S2M(write) doorbell       */
    /*            -> dataram(TCM) buffer -> native deref            [TCM][Data-Transfer]     */
    /*  CoherencyEnforcer RAII gates which accessors may coexist   [Coherency]              */

    /* ========================= RETURN (device) ============================ */
    customop_return_tensor(y);             /* if !get_dst_tensor_called: UTensor::copy:     */
    /*   dtype gate; neuron_dataram_allocate bounce; neuron_memcpy HBM->dram->HBM            */
    /*   -> output descriptor's HBM addr                            [Marshalling §6]         */
    customop_cleanup();                    /* fsync(2); fsync(1); memw  (drain+fence)        */
                                           /*   NOT respond()           [Marshalling §5.2]   */
    if (switched_stack) asm("j switchBack");/* restore SP/SRs/ISL; deallocate_hbm_stack       */
}                                          /*   else: normal windowed retw    [Stack Switch] */
```

---

## 9. Adversarial self-verify — top-5 claims spot-checked this task

These were re-grounded **directly against the shipped objects** (bounded commands per
the anti-hang rules), not trusted from the deep pages. All confirmed.

| # | Claim | Check (this task) | Result |
|---|---|---|---|
| 1 | Link line: `libneuroncustomop.a libc10.a -lloader -mlsp=lsp_fll_load_cpuI -lxmem -lhal -lc++-e -lm -lgcc libcweak.a`; `NUM_CPUS=8`; core `ncore2gp`; ver `0.21.2.0`/ucode `1.21.1.0` | `rg` over `build_custom_op.py` (lines 10,12,25,45,47,50) | **CONFIRMED** |
| 2 | The 8 `customop_*` symbols at `setup@0x224, next_tensor@0x35c, next_scalar@0x390, next_int@0x3c4, next_longlong@0x3fc, next_float@0x43c, return_tensor@0x47c, cleanup@0x5d4` | `nm wrapper_api.o` | **CONFIRMED** (byte-exact) |
| 3 | `ARG_TENSOR` `byte_size = 48`; `active_neuron_memcpy_method @.data 0x20c = 02` (DMA default); table 3 reloc slots @0x200/4/8 | `readelf --debug-dump=info` + `readelf -x .data` (`wrapper_api.o`/`data_transfer.o`) | **CONFIRMED** |
| 4 | `neuron_translate` match `(addr & mask) == ptr`, return `window + (addr & ~mask)` | range-bounded `xtensa-elf-objdump 0x13b–0x214` (`translation.o`): `and/xor/or/beqz`, then `slli·32; l32i +8; xor; add.n` | **CONFIRMED** |
| 5 | `get_cpu_id` = raw `rsr.prid` (SR 0xEB) no mask/shift; `get_cpu_count` = `movi.n a2,8`; 1479 FLIX bundles total (wrapper_api=1094) | bounded disasm `parallel.o 0x10–0x28` + brace-line census over all 10 members | **CONFIRMED** (1479) |

Two further checks corroborate the §6 dtype correction: the ordinal-tree compare
immediates in `_ZNK7UTensorcvN2at6TensorEEv` are exactly **`{2,3,4,6,7,8,10,12}`**
(eight), with `0xB` (FP32R) absent → falls to the `_Assert` abort at `+0x2f0`
(re-read this task) — confirming **8 marshallable, FP32R aborts**, against the raw
report's "9 / FP32R→Float".

---

## 10. The corrections this synthesis carries (settled facts)

The sub-reports corrected each other; the settled facts a reimplementer uses:

- **C1 — allocator never aborts on OOM.** `xmem` returns NULL+status; the neuron
  wrappers return **−1/NULL**; the abort lives **one layer up** in the stack-switch
  caller (`hbm_stack_addr==0 → abort`). ([Allocators](device-allocators.md))
- **C2 — `reg_location` = absolute NX `MEM_WINDOW3/5/6_LO` (`0x100218/228/230`).** The
  two pinned records have **no** `reg_location` store (benign under the `%3` cursor).
  ([neuron_translate §5](neuron-translate-windows.md), [Q7 ELF VADDR §1](q7-elf-vaddr.md))
- **C3 — IRAM *vectors* (4 KiB) byte-identical; `.text` is in the SRAM/`hbm_scratch`
  window, content-identical but base-shifted per core.** "FLL" = **Fixed Location
  Library**. ([Multicore §6c](multicore-spmd.md), [LSP/ELF](lsp-elf.md))
- **C4 — DKL on-ramp gated off on CAYMAN.** The build/marshalling/execute ABI here is
  generation-independent; the device DKL dynamic-load gate is where "is this op
  loadable on this silicon" is decided, and on CAYMAN it is off. The
  `kernel_info_table` dispatch itself is identical CAYMAN/MARIANA/MARIANA+.
  ([External-Lib Loader](../firmware/pool/external-lib-loader.md))
- **C5 — DMA is the default backend** (`active_neuron_memcpy_method = 2`), M2S/S2M
  directions reconciled. ([Data-Transfer](data-transfer-backends.md))
- **C6 — `_lock=NULL` is the design, not an oversight** (per-core-private memory).
  ([Multicore §7d](multicore-spmd.md))
- **C7 — the `wrapper_fn → switchBack` wiring is codegen-emitted**
  (`if(switched_stack) asm("j switchBack")`). ([Codegen §3c](build-custom-op-codegen.md))

Plus the two corrections this page raises against the **raw SX-ABI-18** backing report
(flagged inline above, repeated here for the Part-7 reconcile): **(a)** "9 marshallable
dtypes / FP32R→Float" → **8 / FP32R aborts** (§6); **(b)** "`customop_cleanup`
responds+fsyncs" → **`fsync(2);fsync(1);memw`, no `respond`** (§5.2).

---

## 11. Open gaps (honestly flagged)

- **G1 — the inlined `isa_to_torch_dtype` arm map.** Now fully resolved by the
  committed [Rosetta](scalartype-dtype-rosetta.md) (decoded from the Xtensa machine
  code, re-confirmed here). The earlier SX-ABI-06 inference is superseded; no gap
  remains for the marshallable set. `[HIGH × OBSERVED]`
- **G2 — the per-kernel SPMD channel-slicing formula** lives in the customer/firmware
  kernel images, not in `libneuroncustomop.a` — the lib supplies only
  `(get_cpu_id, get_cpu_count)`. ([Multicore §7c](multicore-spmd.md)) `[MED — out of scope]`
- **G3 — the host window-manager vtable write** (the producer side of the translate
  windows) is INFERRED byte-identical plumbing; the device consumer is fully observed.
  ([neuron_translate §8](neuron-translate-windows.md)) `[MED × INFERRED]`
- **G4 — the `neuron_memcpy` `int` return is vestigial** (the backends are `void`;
  failures are asserts). Do not rely on it as an error code.
  ([Data-Transfer](data-transfer-backends.md)) `[MED × OBSERVED]`
- **G5 — three catalogued latent fragilities, none on the default path:**
  `neuron_set_memcpy_method`'s `≥4` guard (§4.4); the int64→size_t index narrowing at
  the accessor↔`Q7PtrType` boundary; the uninitialised pinned-window `reg_location`
  (benign under `%3`). `[LOW × OBSERVED]`

---

## 12. Cross-references

- [The Complete Custom-Op ABI](complete-customop-abi.md) — the device-side
  `libneuroncustomop.a` reference (the 10 members, the entry/registration contract).
- [The Device-Side Custom-Op ABI Reference](device-abi-reference.md) — the normative
  per-symbol contract.
- [A Custom Op, End to End](../orientation/customop-end-to-end.md) — the orientation
  narrative.
- **BUILD:** [`build_custom_op.py` Codegen](build-custom-op-codegen.md) ·
  [Build Flow](build-flow.md) · [LSP / ELF](lsp-elf.md) ·
  [The Custom-Op Programming Model](programming-model.md) ·
  [FlexLM Licensing Gate](flexlm-licensing.md).
- **LOAD/INVOKE:** [Stack Switch](stack-switch.md) ·
  [`customop_*` Marshalling](customop-marshalling.md) ·
  [POOL Engine Main Dispatch Loop](../firmware/pool/pool-dispatch.md) ·
  [`kernel_info_table` Binary Layout](../firmware/pool/kernel-info-table.md) ·
  [External-Lib Loader](../firmware/pool/external-lib-loader.md) ·
  [Prelink Validation](../firmware/pool/prelink-validation.md).
- **EXECUTE:** [Q7PtrType + Lazy Translation](q7ptrtype.md) ·
  [The at::Tensor Object Chain](tensor-object-chain.md) ·
  [The Retargeted TensorAccessor](tensor-accessor.md) ·
  [TensorStream + TCM Staging](tensorstream-tcm.md) ·
  [The CoherencyEnforcer](coherency-enforcer.md) ·
  [neuron_translate + the Window Table](neuron-translate-windows.md) ·
  [Data-Transfer Backends](data-transfer-backends.md) ·
  [Device Memory Allocators](device-allocators.md) ·
  [The Multicore API (8-core SPMD)](multicore-spmd.md).
- **MEMORY/DTYPE:** [The Q7 ELF VADDR + Per-Core Memory Model](q7-elf-vaddr.md) ·
  [ScalarType ↔ DTYPE Rosetta](scalartype-dtype-rosetta.md) ·
  [Unified Datatype Model](../firmware/kernels/dtype-model.md).
