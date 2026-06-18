# A Custom Op, End to End

This is the worked trace. We take **one** GPSIMD custom op — a tiny elementwise
kernel a user writes in C++ — and follow it through every hop, from the source file on
the developer's laptop to a `callx8` on a Vision-Q7 "Cairo" core inside a NeuronCore.
At each stage we name the **real** symbol, file, or tool that performs the work and the
**artifact** it produces, then forward-link the deep Part-7 (ABI) and Part-8 (Host
Runtime) page where the mechanism is taken apart byte by byte.

Read this page first — the deep pages assume you already know *where in the pipeline you
are*. It is a trace, not a reference: every claim here is stated once and proved elsewhere.

> **How to read the tags.** Every substantive claim carries `[CONF/PROV]` per
> [the Confidence & Walls Model](../reference/confidence-model.md): confidence is
> `HIGH`/`MED`/`LOW`, provenance is `OBSERVED` (read from a binary/header/disasm),
> `INFERRED` (derived), or `CARRIED` (from a sibling corpus, re-grounded where possible).
> Where this page says **the binary wins**, it means a symbol name or address was read
> straight out of a shipped file with `nm`/`objdump`/`strings`. The five strongest
> stage-claims in this trace were re-verified that way (see *Self-Verification* at the end).

---

## The example op we will follow

To keep the trace concrete, our op is `vector_add` — a single-output kernel that adds
two HBM-resident tensors elementwise. In user C++ it is one function with the fixed
custom-op signature:

```cpp
// vector_add.cpp  — the user's kernel
#include "custom_op.h"
at::Tensor vector_add(const at::Tensor& a, const at::Tensor& b) {
    at::Tensor out = at::empty_like(a);
    auto av = a.accessor<float, 1>();
    auto bv = b.accessor<float, 1>();
    auto ov = out.accessor<float, 1>();
    for (int64_t i = 0; i < a.size(0); ++i)
        ov[i] = av[i] + bv[i];     // each [] is a lazy HBM dereference (Stage 9)
    return out;
}
```

It looks like ordinary libtorch. It is not: `at::Tensor` here is a *retargeted* tensor
whose storage is a 64-bit HBM address, and `accessor[]` is a translating dereference
onto a Q7 hardware window. That retargeting is the whole game, and it is invisible in the
source — which is exactly why a reimplementer needs this trace.

---

## Stage 1 — Codegen: `build_custom_op.py` emits the wrapper

**What happens.** The user invokes the shipped driver
`build_custom_op.compile(name, fn_names, src_files, schema, build_dir, …)`. Its real act
(`_create_wrapper`) is to *parse the op schema* and emit a second translation unit,
`wrapper.cpp`, that bridges the on-wire instruction interface to the user's plain-C++
signature. The kernel itself is never touched — it is compiled in a separate TU
(`_compile_sources`) and the two meet only at link.

The generated wrapper is mechanical and per-op. For `vector_add` it is:

```cpp
void vector_add_wrapper() {
    customop_setup(true);                                  // hasReturn hardcoded true
    at::Tensor output = vector_add( customop_next_tensor(),  // arg 0
                                    customop_next_tensor() ); // arg 1  (1:1, declared order)
    customop_return_tensor(output);
    customop_cleanup();
    if (switched_stack) asm("j switchBack");
}
int vector_add_stack_switch() {
    return switch_stack_or_call_wrapper((uint32_t)vector_add_wrapper);  // stack 4196
}
```

The arg-type map is fixed: `Tensor → customop_next_tensor`, `Scalar/number →
customop_next_scalar`, `int/float/long long →` the matching `customop_next_*`. The codegen
also emits, once per library, a `get_func_cnt()=N` / `get_func_name(idx)` /
`get_func_ptr(idx)` table (the last returning `<fn>_stack_switch`); these are the undefined
references the runtime's `register_funcs` later walks to publish each op. `[HIGH/OBSERVED]`
— the `customop_setup(true)`, `customop_return_tensor(output)`, `asm("j switchBack")`, and
`int <fn>_stack_switch()` lines are emitted verbatim by `script/build_custom_op.py`
(`_create_wrapper`, lines ~152–251).

**Artifact:** `wrapper.cpp` (generated), plus the as-yet-unresolved `customop_*` and
`<fn>_stack_switch` references it will pull from the device runtime archive.

**Deep page:** [build_custom_op.py Codegen](../abi/build-custom-op-codegen.md);
the marshalling entry points themselves at
[customop_* Marshalling Entries](../abi/customop-marshalling.md).

---

## Stage 2 — Compile: `xt-clang++` builds Q7 objects via the `ncore2gp` backend

**What happens.** Both the user kernel and the generated wrapper are compiled by
`xt-clang++` — a thin 39 KB wrapper shell that `exec`s the real `clang-10` driver and loads
the per-core code-generator plugin `libXtensaCodeGen.so`. The exact flag string (`CC_OPT`)
is fixed by `build_custom_op.py`:

```
xt-clang++ -g -std=c++14 -stdlib=libc++-e -fno-jump-tables -Os -Wall -Werror
           -Wno-c99-designator -mcoproc -MMD -MP -fpic -mlongcalls -c
           --xtensa-core=ncore2gp --xtensa-system=<sys>
```

The core is selected by `--xtensa-core=ncore2gp`, **not** by a `-target` triple. C++14 is
the frozen ABI level; exceptions are excluded by linking the embedded `libc++-e` rather than
by a flag; `-mcoproc` lights up the seven coprocessors the vector pipe needs; `-mlongcalls`
relaxes far calls so code can sit anywhere in the 1 GB SRAM window. The provenance string
baked into each object's `.comment` section is the strongest single anchor in the corpus:
`XtensaTools-14.09 clang version 10.0.1`. The assembler/linker/strip underneath are GNU
binutils `2.34.20200201`, driven through the `xt-clang++`/`xt-pkg-loadlib` shells.
`[HIGH/OBSERVED]`

> **NOTE — two version axes, one core.** The hardware this targets is `NX1.1.4`
> (= LX7.1.4 = HW RI-2020.4); the *tools* that build for it are the later `14.09 =
> RI-2022.9` release. Both labels are correct and name different axes — do not collapse
> them. See [Toolchain Inventory & Versions](../reference/toolchain-versions.md).

**Artifact:** `vector_add.o` and `wrapper.o` — ELF32-LSB-relocatable Tensilica Xtensa
objects (`elf32-xtensa-le`), separate translation units.

**Deep pages:** [Build → Compile → Link → Strip → Package Flow](../abi/build-flow.md);
provenance in [Toolchain Inventory & Versions](../reference/toolchain-versions.md).

---

## Stage 3 — Link: per-core LSPs bind the objects to fixed SRAM windows

**What happens.** The same object set is linked **once** for a single-core build or
**eight times** for a multicore (SPMD) build — one shared object per Q7 POOL core. The link
is driven through `xt-clang++` (invoking `xtensa-elf-ld` underneath) against a per-core
*linker support package* (LSP), selected with `-mlsp=…/lsp_fll_load_cpu{N}`, and pulls in the
device runtime libraries: `libneuroncustomop.a` (the ABI), `libc10.a` (retargeted c10),
`-lloader` (the FLL loader runtime), `-lxmem -lhal`, `libc++-e`, and `libcweak.a`. The link
entry is `ENTRY(_start)`.

The eight LSPs differ in exactly one material way — the SRAM window origin:

```
sram0_0_seg org = 0x84000000 + cpu_id * 0x200000,  len 0x200000 (2 MiB)
```

The eight 2 MiB windows **tile** `[0x84000000, 0x85000000)` = 16 MiB; `0x84000000` is the
pinned `hbm_scratch` NX base, so each `_cpuN.so`'s code+rodata+data+bss live in *that core's*
private 2 MiB sub-window. "FLL" = *Fixed Location Library* — a packaged library loaded at an
absolute address, which is why the LSP origins are non-PIC absolutes. The LSP places **no**
stack and **no** heap; those are runtime objects (Stage 9). So the core identity is encoded
**twice**: at link time (the SRAM window) and at run time (the PRID, Stage 9).
`[HIGH/OBSERVED]`

**Artifact:** `vector_add_cpu0.so … vector_add_cpu7.so` (or a single `.so`) — each a
position-independent `ET_DYN` Xtensa image carrying a `.kernel_info_table` section.

**Deep pages:** [LSP Linker Specs + ELF Layout](../abi/lsp-elf.md);
[The Q7 ELF VADDR + Per-Core Memory Model](../abi/q7-elf-vaddr.md);
the 8-core link identity in [The Multicore API (8-core SPMD)](../abi/multicore-spmd.md).

---

## Stage 4 — Strip & pack: `xt-pkg-loadlib` produces the shippable image

**What happens.** Unless the user passed `-g`, each linked `.so` is stripped and packed by
`xt-pkg-loadlib`, invoked as
`xt-pkg-loadlib -e lib_func --xtensa-core=ncore2gp -o <base>.packed.so -s <base>.stripped.so <lib>`.
The sequence is: `xt-strip` → `xt-objdump -h` to locate empty sections → `xt-objcopy` to drop
a fixed section set plus the empties (emitting `<base>.stripped.so`) → embed the stripped
library as a `libdata` section inside an empty relocatable object, with the exported symbol
`lib_func` placed at the *start* of the embedded blob (`STRIP_OPT` carries `-e lib_func`),
emitting `<base>.packed.so`. `lib_func` is the symbol that marks the packaged-library blob
base — it is **not** a code entry point (the device run entries are `_start`/`entry_func`,
Stage 9). `[HIGH/OBSERVED]`

> **CORRECTION — where the per-generation EXTISA images actually live.** Distinct from the
> customer-built op, the runtime ships *pre-built per-generation* EXTISA device libraries (the
> firmware images the host resolves and stages). These are embedded as rodata blobs behind
> getter stubs `<GEN>_Q7_POOL_<flavor>_EXTISA_0_SO_get` / `_JSON_get` — **but only for the
> modern generations**. In `libnrtucode_internal.so`, `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get`
> (and the MARIANA / MARIANA_PLUS / MAVERICK siblings) are **defined** (`t`) with a `.data`
> payload, whereas `SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get` / `_JSON_get` are **weak-undef**
> (`w`) with **no** payload — SUNDA's EXTISA blob is genuinely *absent* there. SUNDA's
> kernels live exclusively in a standalone container `libnrtucode_extisa.so`, which is **not
> present in this checkout** (it belongs to the sibling `libnrt.so` runtime package). So "the
> EXTISA blobs are embedded in `libnrtucode_internal.so`" is exactly true for the four modern
> gens (`[HIGH/OBSERVED]`) and *false for SUNDA* (`CARRIED`; the container is the sibling
> corpus). Forward-link: [Version + Ext-ISA Getters](../runtime/version-extisa-getters.md).

> **QUIRK — the customer never ships `_cpuN.so`.** The redistributable artifacts are the
> `build_custom_op.py` driver, the headers, the LSPs, and the static runtime archives —
> **not** any built `_cpuN.so` (those are produced on the customer machine). This wiki
> therefore documents the *emitter* and verifies every emitted symbol resolves against the
> shipped runtime archive.

**Artifact:** `vector_add_cpuN.packed.so` — the staged, position-independent Xtensa image the
host runtime consumes.

**Deep pages:** [Build → Compile → Link → Strip → Package Flow](../abi/build-flow.md);
[FlexLM Licensing Gate](../abi/flexlm-licensing.md) (next).

> **GOTCHA — the FlexLM gate sits beside the compiler, not on it.** The Xtensa tools are
> FlexNet-licensed (client `tenlp.so`, FlexNet v11.15.1.0). But the checkout lives in the
> **loaded engines** — the ISS core (`libsimxtcore`), the legacy XCC/TIE engine (`extend.so`),
> and the TIE compiler (`tcgen`) — not in the `clang-10` path. The per-kernel **compile + link
> is ungated**; only the ISS oracle and TIE/config-gen flow require a live `xtensad` checkout.
> `build_custom_op.py` forces `LM_LICENSE_FILE`, when unset, to the node-locked
> `amzn_vq7_us_582883.out` shipped in the tools `.deb`. `[HIGH/OBSERVED]`. Full mechanism:
> [FlexLM Licensing Gate](../abi/flexlm-licensing.md).

---

## Stage 5 — Host resolve & construct: `nrtucode_ll_create` builds the loadable

**What happens.** Control now crosses to the **host runtime** (`libnrtucode`, whose symbol
twin is `libnrtucode_internal.so`). The orchestrator owns three object families —
`nrtucode_context_t` (the platform back-end + log holder), `nrtucode_core_t` (one per booted
Q7 POOL core's DRAM control block), and `nrtucode_ll_t` (one per loadable library). There is
**no** `nrtucode_ll_load` symbol; the host load *is* `nrtucode_ll_create(ctx, coretype, arg3,
library, out_ll)`:

1. **Resolve the library selector.** A single
   `getenv("NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE")` decides: a caller-supplied
   `library` wins; otherwise the env forces selector 3 (the CPTC superset) or defaults to
   selector 0 (the base POOL EXTISA). `[HIGH/OBSERVED]`
2. **Fetch the EXTISA image.**
   `nrtucode_get_ext_isa_internal(coretype, arg3, &pi_lib, selector)` returns the packaged
   Xtensa-ELF blob for `{coretype, arg3, selector}` (these are the Stage-4 EXTISA blobs,
   resolved through the per-gen `{SO_get, JSON_get}` table). `[HIGH/OBSERVED]`
3. **Per-coretype switch.** A jump table accepts the shipped set `{6, 13, 21, 29}` =
   {SUNDA, CAYMAN, MARIANA, MARIANA_PLUS}; coretype 6 is a degenerate host-only family needing
   no device prelink, `{13,21,29}` take the prelink path, anything else returns error 8. (The
   internal twin additionally accepts 37 = MAVERICK; the shipped jump table does not — this is
   the single genuine divergence between the two binaries.) `[HIGH/OBSERVED]`

**Artifact:** a partly-built `nrtucode_ll_t` (0x48-byte host struct) holding the selector and,
after Stage 6, the staged device handle.

**Deep pages:** [The nrtucode Subsystem + Device Bring-Up](../runtime/nrtucode-bringup.md);
[nrtucode_ll_create / destroy / name / size](../runtime/nrtucode-ll-create.md);
[Opcode-Set → Library Resolver](../runtime/opcode-to-lib-resolver.md);
[Version + Ext-ISA Getters](../runtime/version-extisa-getters.md).

---

## Stage 6 — Host prelink & relocate: `prelink` emits a UCPL image

**What happens.** This is the heart of the host side. The static function `prelink` (verified
at `0x9b5d60` in `libnrtucode_internal.so`) is a direct compilation of Tensilica's
`xt-libloader` host split-load path. It runs entirely on the **host x86-64**; there is no
device-resident prelink routine. Given the EXTISA blob and the per-generation
`cayman_memory_bounds` table it:

1. **Validate + load segments** (`prelink_load_lib` → `validate_dynamic_load`): check
   `\x7fELF` + ELFCLASS32 + endian (`xtlib_verify_magic`), parse `Phdr`/`PT_DYNAMIC`, copy the
   two `PT_LOAD` segments into scratch, zero-fill bss, bounds-check each against the device
   code/data region extents.
2. **Relocate** (`prelink_relocate_lib` → `relocate_op` / `reloc_addr`): apply **every**
   `R_XTENSA` relocation against the device base addresses, in place, in scratch. Symbol
   resolution is *degenerate by contract* — any reloc with a nonzero symbol index
   (`r_info > 0xff`) is rejected; a packaged image must be fully self-relative. The result is a
   **fully-relocated** image with **zero** residual relocations.
3. **Emit the UCPL header** — a compact 0x20-byte header (magic `"UCPL "` =
   qword `0x204c504355`, verified present in the binary) recording the two segment extents
   (`code_seg_len`, `data_seg_off`, `data_seg_len`), the init/fini addresses, and the device
   `start_sym`. A neat trick: the magic's byte `[0x04]` = `0x20` (the space in `"UCPL "`)
   doubles as the literal code-segment device offset, so no separate `code_seg_off` field
   exists. The header carries **no** version, checksum, section count, or reloc/symbol table —
   the image is already relocated. `[HIGH/OBSERVED]`

`nrtucode_ll_create` then `device_malloc`s a 16 MiB buffer (the actual library is capped at
64 KiB — `cmpq $0x10001`, "Prelinked library would be larger than the available buffer on
device") and issues **three** ordered `write_memhandle` calls — `[UCPL header][code][data]` —
through the embedder-supplied 5-slot memhandle vtable. `[HIGH/OBSERVED]`

**Artifact:** a staged, fully-relocated `[UCPL header | code | data]` image resident in device
memory, plus `prelinked_size` recorded in the `ll`.

**Deep pages:** [The Host Prelinker — UCPL / Segment Loader / R_XTENSA / Staging](../runtime/prelinker-ucpl.md);
[The UCODE Relocation / Prelink Engine (runtime consumer)](../runtime/ucode-relocation-consumer.md);
the device memory model in [Device Memory Allocators](../abi/device-allocators.md).

---

## Stage 7 — Host emit load records + opcode→library resolve

**What happens.** Two host facts close here. First, the staged image must be announced to the
device: `nrtucode_ll_get_load_sequence` (a thin `direction=1` shim over
`nrtucode_ll_get_sequence_common`) emits fixed 0x40-byte **load records** stamped with the
16-bit magic `0x1095` into a caller buffer. The record names the staged image's device
address, the library selector, the image size, and a load/unload direction:

```
+0x00 u16 magic = 0x1095        +0x0c u8 direction (1=load, 2=unload)
+0x10 u64 device_addr           +0x18 u32 library_selector   +0x1c u32 image_size
```

The normal path emits **one** record (target core == the `ll`'s context); a cross-context
fallback emits **eight** identical copies — most plausibly one per Q7 POOL core — plus a
warning. This is the sole producer of the `0x1095` record. `[HIGH/OBSERVED]` — verified: the
constant is written as `movw $0x1095,(%rsi)` at five consecutive 0x40-byte offsets (the
8-record fallback). Note the two distinct magics: `0x1095` is the host→device load-*record*
container; `"UCPL "` is the prelinked device-*image* the record points at.

Second, which library? The host resolver `nrtucode_ll_get_libraries_from_opcodes(coretype,
opcodes[], …, libs, num_libs)` is **opcode-content-blind**: for a Q7_POOL coretype it emits a
single scalar library index — SUNDA(6) → 0; CAYMAN/MARIANA/MARIANA_PLUS(13/21/29) → (CPTC env
? 3 : 0). The actual per-opcode→handler map is **not** computed here; it lives device-side in
the staged lib's `.kernel_info_table`. `[HIGH/OBSERVED]`

**Artifact:** a stream of `0x1095` load records (and a resolved library index) handed to the
device transport.

**Deep pages:** [ll Load / Unload Sequence Generators](../runtime/nrtucode-ll-load-unload.md);
[Opcode-Set → Library Resolver](../runtime/opcode-to-lib-resolver.md);
[Host↔Device Descriptor Handoff (runtime side)](../runtime/host-device-descriptor-handoff.md).

---

## Stage 8 — Device load & bind: `load_external_libraries_impl` installs the table

**What happens.** On the **device**, the Q7 POOL firmware's external-lib loader
(`load_external_libraries_impl`) consumes each `0x1095` record. Per load record:

1. Read direction (`+0x0c`); 1 = load.
2. Use the selector (`+0x18`) to pick/create a resident library entry in an object array;
   each entry is a 280-byte record holding `total_cpus` and the `.kernel_info_table` it will
   install.
3. Translate the staged image's SoC address (`+0x10`) to a Q7 XT address via a 16-entry SoC↔XT
   window table (`translate_soc_to_xt_address`).
4. **DMA-pull** the fully-relocated `"UCPL "` image into Q7 IRAM/DRAM/EXTRAM
   (`load_from_nx_addr` / `init_dma_queue` / `dma_data_transfer`). No device relocation runs —
   the host already did it.
5. **Locate `.kernel_info_table` by section name** and bind it as the POOL dispatcher's active
   table. The table is 8-byte entries `{ BE key (spec<<8 | opcode) | LE funcVA }`.
6. Run the library's start/init symbol (`call_start_symbol`) so it registers its kernels, then
   assert the `NUM_POOL_CORES = 8` invariant (`total_cpus` ∈ {1, 8}, a `bnei a,8` immediate).
   `[HIGH/OBSERVED]`

> **NOTE — `.kernel_info_table` is not compiler-emitted.** The opcode→handler binding is baked
> into the *device* image (the EXTISA / the customer-built `.so`), never into the NEFF the
> compiler emits. The compiler emits only the upstream TPB instruction (opcode + descriptor).
> `[HIGH/OBSERVED]`. The `.kernel_info_table` section name is present in the binary (verified
> by `strings`).

**Artifact:** a bound, active `.kernel_info_table` on each POOL core's dispatcher; the op is
now invocable.

**Deep pages:** [Execute-Time GPSIMD Custom-Op Dispatch](../runtime/execute-time-dispatch.md);
[The 8-Core SPMD Execution Model + Teardown](../runtime/spmd-teardown.md).

---

## Stage 9 — Invoke & run: dispatch → stack switch → marshal → execute → return

**What happens.** At execution the SEQ engine fetches the instruction stream and hands the
`EXTENDED_INST` opcode (`0xf0`, the custom-op lane) to the POOL cores. The device library was
entered at `_start` → `entry_func`, whose `register_funcs` walked the codegen's
`get_func_cnt/name/ptr` table to publish each op's `<fn>_stack_switch` launcher. Each per-core
POOL engine then does a **linear scan** over its bound `.kernel_info_table` matching the packed
`(spec<<8 | opcode)` key, and calls the registered launcher — for our op,
`vector_add_stack_switch`. From there:

- **Stack switch.** `switch_stack_or_call_wrapper(&vector_add_wrapper, 4196)` first stands up
  the *whole* device runtime: the HBM, dataram/TCM, and libc heaps
  (`init_neuron_hbm_allocator` / `init_neuron_dataram_allocator` / `_init_libc_heap_allocator`),
  plus the 5-entry translate-window TLB (`_init_translate_ctx`). It then tests remaining stack
  (`SP − ISL`); if the tiny 4196-byte frame fits it `callx8`s the wrapper in place; otherwise
  it allocates a ≤4 MB HBM stack, `saveContext` + `xthal_window_spill_nw` + `switchStack`, and
  jumps in. The kernel arguments do **not** cross the switch — they are pulled lazily by the
  wrapper. `[HIGH/OBSERVED]` — `switch_stack_or_call_wrapper(uint,uint)` is present in
  `libneuroncustomop.a`.
- **Marshal.** `customop_setup(true)` reads the payload count, starts the SDMA ring, and slurps
  every on-wire `ARG_TENSOR` descriptor (0x30 bytes: `location`, `dtype`, `storage.hbm.addr`,
  `num_elem`) into a global `ArgParser` singleton. Each `customop_next_tensor()` then builds a
  16-byte `Q7PtrType` `{hbm_addr, neuron_translate_ctx()}`, wraps it through `NeuronStorageImpl
  → NeuronTensorImpl`, and returns an `at::Tensor` with **poisoned** storage — only Q7-aware
  accessors can reach HBM. A **dtype-match gate** rejects any ISA dtype with no
  `c10::ScalarType` target (UINT16/32/64, FP8_*). `[HIGH/OBSERVED]`
- **Execute.** Inside `vector_add`, each `accessor[]` is a *lazy translating dereference*:
  multi-dim indexing does pure 64-bit address math (`Q7PtrType::operator+`), and the single
  `neuron_translate` happens at the leaf — searching the 5-entry window TLB (HIT = arithmetic;
  MISS = evict a round-robin victim and reprogram a `MEM_WINDOW` CSR). Bulk paths instead stage
  through a TCM/dataram buffer over SDMA (`neuron_memcpy` → `dma_data_transfer`, M2S read / S2M
  write). Per-core work is partitioned by `get_cpu_id()` (a cached `PRID` in `[0,8)`) and
  `get_cpu_count()` (constant 8) over **private** memory with **zero** cross-core sync.
  `[HIGH/OBSERVED]`
- **Return.** `customop_return_tensor(output)` stages the result through a dataram bounce
  buffer + SDMA into the output HBM address; `customop_cleanup()` does
  `respond(TPB_WRITE_RESPONSE)` then `fsync`; and if the stack was switched, the codegen's tail
  `j switchBack` restores `SP`/`SR`/`ISL` and frees the HBM stack. `[HIGH/OBSERVED]` —
  `customop_return_tensor(at::Tensor&)` and `customop_cleanup()` are present in
  `libneuroncustomop.a`.

**Artifact:** the result tensor written back to its HBM address; the op is complete.

**Deep pages:** [Q7PtrType + Lazy Translation](../abi/q7ptrtype.md);
[Stack-Switch Dispatch](../abi/stack-switch.md);
[customop_* Marshalling Entries](../abi/customop-marshalling.md);
[The Retargeted TensorAccessor](../abi/tensor-accessor.md);
[neuron_translate Window Family](../abi/neuron-translate-windows.md);
[TensorStream + TCM Staging](../abi/tensorstream-tcm.md);
[The Multicore API (8-core SPMD)](../abi/multicore-spmd.md).

---

## Teardown (the symmetric tail)

When the model unloads, `nrtucode_ll_get_unload_sequence` emits a `0x1095` record with
`direction=2` (naming only the selector — no address); the device `xtensa_unload` resolves the
selector via its resident entry array + window table, reclaims the window
(`push_unallocated_window`), and clears the `.kernel_info_table` binding (`total_cpus → 0`).
Only then does `nrtucode_ll_destroy` `device_free` the 16 MiB buffer and free the host `ll`.
The correct idiom is **emit-unload-sequence then destroy** — they are disjoint operations.
`[HIGH/OBSERVED]`. Deep page:
[The 8-Core SPMD Execution Model + Teardown](../runtime/spmd-teardown.md).

---

## The trace on one screen

| # | Stage | Symbol / file / tool | Artifact | Deep page |
|---|---|---|---|---|
| 1 | Codegen | `build_custom_op.py` `_create_wrapper` | `wrapper.cpp` | [build-custom-op-codegen](../abi/build-custom-op-codegen.md) |
| 2 | Compile | `xt-clang++` → `clang-10` + `libXtensaCodeGen.so`, `--xtensa-core=ncore2gp` | `vector_add.o`, `wrapper.o` | [build-flow](../abi/build-flow.md) |
| 3 | Link | `-mlsp=lsp_fll_load_cpu{N}` (8× SRAM windows), `-lloader` | `vector_add_cpuN.so` | [lsp-elf](../abi/lsp-elf.md) |
| 4 | Strip/pack | `xt-pkg-loadlib` (`-e lib_func`) | `…packed.so`; EXTISA blobs in `libnrtucode_internal.so` (SUNDA container-only) | [build-flow](../abi/build-flow.md) |
| 5 | Host resolve | `nrtucode_ll_create` → `nrtucode_get_ext_isa_internal` | `nrtucode_ll_t` + `pi_lib` blob | [nrtucode-ll-create](../runtime/nrtucode-ll-create.md) |
| 6 | Prelink | `prelink` @`0x9b5d60` (`prelink_load_lib` / `prelink_relocate_lib`) | `[UCPL][code][data]` staged | [prelinker-ucpl](../runtime/prelinker-ucpl.md) |
| 7 | Load records | `nrtucode_ll_get_load_sequence` (`0x1095`); `…_get_libraries_from_opcodes` | `0x1095` records + lib index | [nrtucode-ll-load-unload](../runtime/nrtucode-ll-load-unload.md) |
| 8 | Device bind | `load_external_libraries_impl` | bound `.kernel_info_table` | [execute-time-dispatch](../runtime/execute-time-dispatch.md) |
| 9 | Run | POOL scan → `…_stack_switch` → `customop_*` → accessor | result in HBM | [q7ptrtype](../abi/q7ptrtype.md) |

The single most useful thing to internalize: **the host does all the relocation; the device
only DMA-pulls a finished image and binds a table.** Every "where does X happen" question
reduces to *which side of the `0x1095` record am I on*. For the full byte-level synthesis,
jump to [End-to-End ABI Synthesis](../abi/abi-synthesis.md) and
[Runtime End-to-End Call-Graph Spine](../runtime/callgraph-spine.md).

---

### Self-verification (the five strongest stage-claims, re-grounded in the binary)

Each claim below was re-read directly from a shipped file, not carried from a report.

1. **Stage 1 wrapper template.** `script/build_custom_op.py` `_create_wrapper` emits
   `customop_setup(true)`, `customop_return_tensor(output)`, `asm("j switchBack")`, and
   `int <fn>_stack_switch()` verbatim (lines ~152–251). `[HIGH/OBSERVED]`
2. **Stage 9 marshalling + entry symbols.** `nm libneuroncustomop.a` resolves
   `customop_setup(bool)`, `customop_next_tensor()`, `customop_return_tensor(at::Tensor&)`,
   `customop_cleanup()`, `switch_stack_or_call_wrapper(uint,uint)`, and the device entries
   `_start` @0xaf8, `entry_func` @0x93c, `register_funcs`, `get_cpu_id`, `get_cpu_count` — all
   defined (`T`). `[HIGH/OBSERVED]`
3. **Stage 5/6 host symbols + address.** `nm libnrtucode_internal.so` resolves
   `nrtucode_ll_create`, `nrtucode_get_ext_isa_internal`, `nrtucode_ll_get_load_sequence`, and
   `prelink` at `0x9b5d60` with `prelink_load_lib` / `prelink_relocate_lib`. `[HIGH/OBSERVED]`
4. **Stage 6/7/8 magics + section.** `strings libnrtucode_internal.so` contains `"UCPL "`,
   `.kernel_info_table`, and "Prelinked library would be larger than the available buffer on
   device"; `objdump -d` shows `movw $0x1095,(%rsi)` at five 0x40-spaced offsets (the 8-record
   cross-context fallback). `[HIGH/OBSERVED]`
5. **Stage 4 EXTISA embedding (with the SUNDA correction).** `nm libnrtucode_internal.so` shows
   `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` defined (`t`) with a `.data` payload, while
   `SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get` / `_JSON_get` are weak-undef (`w`) with no payload —
   confirming the modern-gen EXTISA blobs are embedded there but SUNDA's is container-only.
   `[HIGH/OBSERVED]`

**Confidence summary.** All nine stages are `HIGH/OBSERVED` for their named symbol, file, or
tool. The `MED/INFERRED` softenings are the `device_malloc(size,align)` argument order, the
"8 records = one per POOL core" reading, and the device loader's flat-image entry VA. The one
`CARRIED` item is SUNDA's standalone `libnrtucode_extisa.so` container (absent from this
checkout, present in the sibling `libnrt.so` runtime), forward-linked to
[The libnrt Surface Map (GPSIMD lens)](../runtime/libnrt-surface.md) rather than asserted here.
