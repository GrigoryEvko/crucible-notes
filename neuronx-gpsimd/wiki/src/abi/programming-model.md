# The Custom-Op Programming Model (overview)

This page is the **orientation** for Part 7. If you are a C++/LLVM developer about to
write your first GPSIMD custom op, read this first: it gives you the *mental model* —
what a "custom op" actually is on this hardware, what you write, what the toolchain does
to it, and the handful of constraints that will bite you — and then points you at the
deep pages that take each mechanism apart byte by byte. It is deliberately lighter on raw
disassembly than those pages; its job is to make them navigable.

The companion to this page is the worked, end-to-end trace
[A Custom Op, End to End](../orientation/customop-end-to-end.md): that follows **one**
op (`vector_add`) through every host↔device hop, from the `.cpp` on your laptop to a
`callx8` on a Q7 core. This page is the *model*; that page is the *journey*. Read them
together — neither repeats the other's depth.

> **How to read the tags.** Substantive claims carry `[CONF/PROV]` per
> [the Confidence & Walls Model](../reference/confidence-model.md): confidence
> `HIGH`/`MED`/`LOW`; provenance `OBSERVED` (read from a binary/header/disasm),
> `INFERRED` (derived), or `CARRIED` (from a sibling page, re-grounded where possible).
> All prose here is derived from static analysis of the shipped
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0` package (the `libneuroncustomop.a` archive
> + DWARF v4, the `build_custom_op.py` driver, the per-core LSPs) and the host
> `libnrtucode`, disassembled natively with the shipped
> `xtensa-elf-objdump --xtensa-core=ncore2gp`. Five of this page's framing claims are
> grounded directly in those objects — see *Self-Verification* at the end.

---

## 1. The mental model in one paragraph

A GPSIMD custom op is a **PyTorch ATen kernel that you write in plain C++**, whose body
runs **SPMD on 8 Cadence Tensilica Vision-Q7 "Cairo" DSP cores** inside a Trainium
NeuronCore's POOL cluster. You write one free function with a fixed signature —
`at::Tensor fn(const at::Tensor&, …)` — using an `at::Tensor`-shaped API. You never see
the device's real pointer type. The driver `build_custom_op.py` code-generates a thin
wrapper, compiles your kernel **once**, and links it **eight times** (one image per Q7
core), packs each into a loadable ucode library, and the host runtime stages it onto the
cores. At run time **all 8 cores execute the same kernel image** (that is the "single
program"); each one self-identifies by reading its hardware **PRID** register
(`get_cpu_id()`) and partitions its slice of the work (that is the "multiple data"). The
tensors your kernel receives live in **HBM** as 64-bit addresses; the Q7 is a **32-bit**
core that cannot address HBM directly, so each tensor pointer is a 16-byte `Q7PtrType`
value that is **lazily translated** to a 32-bit NX-local address — per element via a
small window TLB, or in bulk by staging a span through a fast on-core **TCM** buffer over
DMA. The whole kernel may run on an **HBM-allocated stack** if it needs more than the
tiny on-core one. When it finishes, the result is copied back to the output HBM
descriptor, the op drains-and-fences its DMA queues, and (if the stack was switched) control returns
through `switchBack`. There is **no cross-core synchronization** inside the custom-op
library — the 8 cores are independent; any join is the host runtime's job. `[HIGH ·
OBSERVED — synthesized from the Part-7 deep pages, each cited below.]`

The single most useful thing to internalize: **you write libtorch, but every `at::Tensor`
is retargeted onto a 64-bit HBM address reached through a hardware translation window, and
your one kernel is replicated across 8 independent cores.** Everything in Part 7 is a
consequence of those two facts.

---

## 2. What you actually write — the skeleton op

The author contract is exactly one free function per op
([build-custom-op-codegen](build-custom-op-codegen.md) §3a/§7):

```cpp
// myop.cpp — the kernel you write. xt-clang++ compiles this as its OWN TU.
#include "neuron/wrapper_api.h"     // pulls in torch/torch.h + the device APIs

// Fixed contract: ONE at::Tensor return; args are PyTorch-level types only.
//   schema arg  ->  C++ param
//   Tensor      ->  const at::Tensor&
//   Scalar      ->  const at::Scalar&
//   int/float/long long -> int/float/long long
at::Tensor relu(const at::Tensor& x) {
    auto n  = x.size(0);
    auto out = at::empty_like(x);                 // a device-backed output tensor

    // ---- SPMD: each of the 8 cores claims its slice (Section 4.1) ----
    uint32_t me = get_cpu_id();                   // rank  = cached raw PRID, in [0,8)
    uint32_t np = get_cpu_count();                // size  = 8 (constant)
    int64_t per = (n + np - 1) / np;              // ceil-divide the work across cores
    int64_t lo  = me * per;
    int64_t hi  = (lo + per < n) ? lo + per : n;  // this core's [lo, hi)

    // ---- touch tensor data (Section 4.2 / 4.3) ----
    auto xa = x.accessor<float, 1>();             // strided, lazy-translating indexer
    auto oa = out.accessor<float, 1>();
    for (int64_t i = lo; i < hi; ++i) {
        float v = xa[i];                          // each [] => one neuron_translate at the leaf
        oa[i] = v > 0.f ? v : 0.f;
    }
    return out;                                   // copied back to the output HBM descriptor
}
```

You never write the wrapper, the marshalling, the stack switch, or the registration
table — the codegen emits all of it. You **may** call, by hand, the runtime device APIs
declared in `custom_op.h`: the accessors (`accessor<T,N>()`, `q7_data_ptr()`,
`sizes()`/`strides()`), the bulk-staging families (`read_stream_accessor`,
`write_stream_accessor`, `tcm_accessor` + `tcm_malloc`), the SPMD identity
(`get_cpu_id`/`get_cpu_count`), the allocators (`neuron_hbm_allocate`,
`neuron_dataram_allocate`/`tcm_malloc`), `neuron_memcpy`, and `get_dst_tensor()` for
in-place output. **None of those are auto-emitted** — they are runtime APIs your kernel
calls directly. `[HIGH · OBSERVED — build-custom-op-codegen §7; device-abi-reference Part 3.]`

### The wrapper the codegen wraps around it

For the op above, `build_custom_op.py` emits — mechanically, per op — a wrapper and a
launcher ([build-custom-op-codegen](build-custom-op-codegen.md) §3b/§3c):

```cpp
void relu_wrapper() {
    customop_setup(true);                         // hasReturn is HARDCODED true
    at::Tensor output = relu( customop_next_tensor() );   // 1:1, in declared arg order
    customop_return_tensor(output);               // COPY result -> output HBM descriptor
    customop_cleanup();                           // fsync(2); fsync(1); memw -- drain + fence
    if (switched_stack) asm("j switchBack");      // the HBM-stack return wiring
}
int relu_stack_switch() {                         // the REGISTERED launcher (int())
    return switch_stack_or_call_wrapper((uint32_t)relu_wrapper);  // stack_size defaults to 4196
}
```

plus a one-time registration trio — `get_func_cnt()`, `get_func_name(idx)`,
`get_func_ptr(idx)` (the last returning `relu_stack_switch`). Those three are **undefined
references** in the shipped library; the library's `register_funcs` (in `start_exit.o`)
walks them at boot to publish each op. So the generated `wrapper.cpp` is the
*table-of-contents* the runtime reads to discover your ops. `[HIGH · OBSERVED —
build-custom-op-codegen §4.]`

The k-th kernel argument is fed by the k-th `customop_next_<type>()` — that signature →
marshalling map is the whole front door. **Deep pages:**
[build_custom_op.py Codegen](build-custom-op-codegen.md),
[customop_* Marshalling Entries](customop-marshalling.md), and the capstone
[The Complete Custom-Op ABI](complete-customop-abi.md).

---

## 3. The five-phase pipeline at a glance

Your `.cpp` becomes a running op in five phases. You only invoke phase 1 (`compile()`);
the rest happen for you.

| Phase | Where | What happens | Deep page |
|---|---|---|---|
| **Build** | your machine | `build_custom_op.py`: compile kernel once → codegen `wrapper.cpp` → link **1× or 8×** vs per-core LSPs → strip/pack via `xt-pkg-loadlib` → `.packed.so` | [Build Flow](build-flow.md), [Codegen](build-custom-op-codegen.md) |
| **Load** | host runtime | `libnrtucode` resolves the image, prelinks/relocates it on the **host**, stages it onto each Q7 core; on-device init brings up the translate ctx, the heaps, and the SDMA ring | [End-to-End trace](../orientation/customop-end-to-end.md) Stages 5–8 |
| **Invoke** | device | POOL dispatcher scans `kernel_info_table` for the opcode → calls `<fn>_stack_switch` → (maybe) HBM-stack switch → `customop_setup` slurps the arg descriptors → `customop_next_*` marshals each into `at::Tensor`/scalar | [Stack-Switch](stack-switch.md), [Marshalling](customop-marshalling.md) |
| **Execute** | device (8× SPMD) | your kernel runs: accessor (lazy per-element translate) **or** TCM/stream staging (bulk DMA); each core works its slice of private memory | [TensorAccessor](tensor-accessor.md), [TCM Staging](tensorstream-tcm.md), [Multicore SPMD](multicore-spmd.md) |
| **Return** | device | `customop_return_tensor` writes the result back over SDMA → `customop_cleanup` drains the DMA queues + fences → if switched, `j switchBack` restores the stack | [Marshalling](customop-marshalling.md) §5, [Stack-Switch](stack-switch.md) §5 |

The full host↔device journey — image resolve, host-side prelink/relocate, the `0x1095`
load records, device bind — is the
[end-to-end trace](../orientation/customop-end-to-end.md). This page focuses on what the
*kernel author* must understand: the SPMD shape, the translate/stage data path, and the
constraints.

---

## 4. The execution shape — SPMD, translate, stage, return

### 4.1 SPMD: 8 cores, one kernel, no shared sync

The runtime is **SPMD** — single program, multiple data. The multicore identity API is
exactly two functions ([multicore-spmd](multicore-spmd.md) §1–§3):

- `get_cpu_id()` returns the core's **rank** in `[0, 8)`. It is a *cached read of the
  hardware PRID special register* (SR 235): a static constructor does `rsr.prid a3;
  s32i.n a3 → cpu_id` once at init, with **no mask and no shift**, and `get_cpu_id()`
  then just reloads that cached word. The logical core id == the raw PRID. `[HIGH ·
  OBSERVED.]`
- `get_cpu_count()` returns the **size** — a folded compile-time constant `8`
  (`movi.n a2, 8`). The custom-op ABI targets a fixed 8-core POOL cluster. `[HIGH ·
  OBSERVED.]`

This is the classic `(rank, size)` pair (`get_cpu_id` is the rank, `get_cpu_count` the
size); a kernel partitions its work over `[me, np)` — typically by channel — exactly as
the skeleton in §2 does. The per-kernel slicing arithmetic lives in *your* kernel, not in
the library; the library supplies only the two primitives. `[HIGH · OBSERVED —
multicore-spmd §7.]`

> **There is no cross-core synchronization inside the custom-op library.** An exhaustive
> sweep of all archive members finds **zero** atomics (`l32ex`/`s32ex`), barriers,
> semaphores, spinlocks, or mutexes (the only "mutex" symbols are libc++ no-op shims).
> Each core runs on **per-core-private memory** (its own 256 KiB dataram, its own 64 KiB
> SoC aperture, its own 2 MiB HBM-scratch sub-window), which is precisely why the heaps
> run lock-free. HBM is *physically* shared but each core's *view* is window-private. If
> an op needs the 8 cores to rendezvous, that synchronization must come from **outside**
> this library; the host-side join after a custom op (DRAIN + completion-semaphore wait)
> is the runtime's responsibility, not the device library's (see
> [the 8-Core SPMD Execution Model + Teardown](../runtime/spmd-teardown.md)).
> `[HIGH · OBSERVED — multicore-spmd §8.]`

The core id is encoded **twice**: at *link* time (each `_cpuN.so` is linked to its own
SRAM window, §5) and at *run* time (the PRID read). Both agree on the `{0..7}` domain.

**Deep page:** [The Multicore API (8-core SPMD)](multicore-spmd.md).

### 4.2 Translate: how a 64-bit HBM tensor becomes a 32-bit dereference

The Q7 is a 32-bit core (`void*` is 4 bytes) and **cannot directly address HBM**. So a
tensor's data is **not** a native pointer — it is a 16-byte
[`Q7PtrType`](q7ptrtype.md) value `{hbm_addr (u64), ctx_ptr, is_nullptr}`. The stock
ATen storage pointer is deliberately **poisoned** (any native deref throws); only
Q7-aware accessors reach HBM. `[HIGH · OBSERVED — q7ptrtype §1/§6, tensor-object-chain.]`

When your kernel does `xa[i]`, the accessor peels strided dimensions with pure 64-bit
*address arithmetic* (`Q7PtrType::operator+`, **no HBM touch**), and the single
`neuron_translate` happens at the **leaf**. `neuron_translate(ctx, hbm_addr +
sizeof(T)*i)` searches a 5-entry window TLB: on a **hit** the result is pure arithmetic
`window + (addr & ~mask)`; on a **miss** it evicts a round-robin victim (only 3 of the 5
records are dynamic; 2 are pinned), reprograms a hardware `MEM_WINDOW` register through an
MMIO pointer, and returns the mapped 32-bit NX-local address. `[HIGH · OBSERVED —
neuron-translate-windows §2/§4/§6.]`

> **GOTCHA — translate is not pure for a cold address.** The first touch of a
> never-before-seen 16 MB region **programs a hardware window register** (two `memw`
> fences around an MMIO store). A working set spanning more than 3 distinct dynamic 16 MB
> regions will **thrash** the TLB. The returned NX address is also valid only *until the
> next `neuron_translate`* — do not cache a `T&` from `[]` across another `[]`. `[HIGH ·
> OBSERVED — neuron-translate-windows §6, q7ptrtype §3a.]`

**Deep pages:** [Q7PtrType + Lazy Translation](q7ptrtype.md),
[neuron_translate + the Window Table](neuron-translate-windows.md),
[The Retargeted TensorAccessor](tensor-accessor.md),
[The at::Tensor Object Chain](tensor-object-chain.md).

### 4.3 Stage: bulk DMA through TCM for sequential sweeps

Per-element translate is correct for random access but ruinous for a linear sweep. For
sequential or tiled access, stage a span through the on-core **dataram** used as
tightly-coupled memory (**TCM == dataram**; `tcm_malloc` is literally
`neuron_dataram_allocate`, returning a directly-dereferenceable 32-bit NX address). Two
families ([tensorstream-tcm](tensorstream-tcm.md)):

- **`read_stream_accessor` / `write_stream_accessor`** — a library-owned **4 KiB**
  dataram bounce buffer, auto-refilled (read) / auto-flushed (write) as you sweep
  forward; one bulk DMA per 4 KiB, native dataram derefs in between; coherency-tracked.
- **`tcm_accessor` + `tcm_malloc`** — *you* own the TCM tile and call
  `tensor_to_tcm`/`tcm_to_tensor` for arbitrary spans (the tiling path); **not**
  coherency-tracked.

Every staging copy funnels through `neuron_memcpy`, which dispatches a 3-entry method
table — `C_MEMCPY(0)`, `VEC_MEMCPY(1)`, `DMA(2)` — that **defaults to DMA** (the on-die
SDMA engine; the `.data` table word reads `2` = `DEFAULT_METHOD`). The DMA backend builds a CME BD ring and kicks the **M2S** doorbell for
HBM→dataram reads / **S2M** for dataram→HBM writes. `[HIGH · OBSERVED — tensorstream-tcm
§3/§6, data-transfer-backends.]`

> **GOTCHA — the `>= 4` method-set bound.** `neuron_set_memcpy_method` guards `>= 4`, so
> it **accepts `3` = `MAX_METHODS`**, which indexes `table[3]` **out of bounds** (the
> table has only 3 entries, `0..2`). Pass only `C_MEMCPY`/`VEC_MEMCPY`/`DMA`
> (`0`/`1`/`2`); `3` is a latent OOB. `[LOW · OBSERVED — data-transfer-backends.]`

> **GOTCHA — streams ignore strides.** The stream/TCM families sweep **storage order**
> (`hbm_addr + sizeof(T)*idx`), ignoring `strides_`. A non-contiguous (transposed /
> broadcast) tensor will be staged in storage order, not logical order — only the
> unbuffered `accessor<T,N>()` honours strides. `[MED · OBSERVED — tensorstream-tcm §4.]`

**Deep pages:** [TensorStream + TCM Staging](tensorstream-tcm.md),
[Data-Transfer Backends](data-transfer-backends.md),
[The CoherencyEnforcer](coherency-enforcer.md),
[Device Memory Allocators](device-allocators.md).

### 4.4 Return: copy back, drain, fence, restore

The codegen's tail does three things ([customop-marshalling](customop-marshalling.md) §5,
[device-abi-reference](device-abi-reference.md) Part 5):

1. `customop_return_tensor(output)` validates dtype/rank/shape, then stages the result
   back to the **output HBM descriptor** through a dataram bounce buffer over SDMA. (The
   alternative is the manual in-place path: call `get_dst_tensor()` to get the
   pre-allocated destination tensor and write into it — **not** auto-emitted.)
2. `customop_cleanup()` is exactly `fsync(2); fsync(1); memw;` — flush the descriptor
   channels then a write barrier so the result is globally visible before the op is
   reported complete. **Order matters**: return-tensor must precede cleanup, and the
   trailing `memw` is the only thing guaranteeing the stores land. `[HIGH · OBSERVED.]`
3. If the stack was switched, `j switchBack` restores the old SP/SRs/ISL and frees the
   HBM stack; otherwise the wrapper falls off the end with a normal windowed `retw`.

---

## 5. The toolchain

The build half is **Cadence Tensilica XtensaTools 14.09 / RI-2022.9**, core config
**`Xm_ncore2gp`** (the Vision-Q7 "Cairo" engine, hardware spec `NX1.1.4`, little-endian,
vector pipe on), with the compiler being **clang 10.0.1**. Every object's `.comment`
carries `XtensaTools-14.09 clang version 10.0.1`. `[HIGH · OBSERVED — build-flow §0,
build-custom-op-codegen.]`

The compile command is fixed by `build_custom_op.py` (`CC_OPT`, verbatim):

```sh
xt-clang++ -g -std=c++14 -stdlib=libc++-e -fno-jump-tables -Os -Wall -Werror \
           -Wno-c99-designator -mcoproc -MMD -MP -fpic -mlongcalls -c \
           --xtensa-core=ncore2gp --xtensa-system=.../ncore2gp/config
```

Why the non-obvious flags are essential for a faithful rebuild: `--xtensa-core=ncore2gp`
selects the core (**not** a `-target` triple); `-stdlib=libc++-e` is the
**exceptions-enabled** libc++ (linked later as `-lc++-e` — mismatch ⇒ undefined `__cxa_*`);
`-fno-jump-tables` keeps the image flat for the host `R_XTENSA` relocator; `-fpic` +
`-mlongcalls` let the **same** objects link to 8 different per-core base addresses;
`-mcoproc` lights up the Q7 vector pipe (without it SIMD intrinsics won't assemble); `-Os`
because each per-core SRAM window is only **2 MiB**. (Full flag-by-flag rationale lives on
the deep page.)

The link pulls in a precise library order — `libneuroncustomop.a libc10.a -lloader
-mlsp=<lsp> -lxmem -lhal -lc++-e -lm -lgcc libcweak.a` — where the **only** thing that
differs across cores is the `-mlsp=` linker-spec directory. The multicore build links the
**same object set eight times** (`NUM_CPUS = 8`), one per core, each against
`lsp_fll_load_cpu{0..7}`; the single-core fallback uses `lsp_fll_load_cpu_single`. The
sole material per-core LSP difference is the SRAM window origin:

```
sram0_0_seg org = 0x84000000 + cpu_id · 0x200000,  len 0x200000 (2 MiB)
```

The 8 windows **tile** `[0x84000000, 0x85000000)` (16 MiB) with no gap or overlap; the
single-core LSP spans the whole 32 MiB. `0x84000000` is the pinned `hbm_scratch` window
base, so each core's code+rodata+data+bss live in its private 2 MiB slice. Output naming
is a hard contract: `<base>_cpu{i}.so`. Finally `xt-pkg-loadlib` strips + packs each `.so`
into a `.packed.so` load-lib (`-e lib_func`). `[HIGH · OBSERVED — build-flow §5/§6,
multicore-spmd §6.]`

> **The licensing split — your compile and link are license-free.** The FlexNet (FlexLM
> 11.15.1.0) gate sits on the loaded **ISS** engine (`libsimxtcore`), the TIE compiler
> (`tcgen`), and `extend.so` — **not** on the clang/binutils path. `clang-10`,
> `xt-clang++`, the GNU binutils, and `xt-pkg-loadlib` carry **zero** `tenlp_checkout`
> bytes; a pure compile → link → pack is **ungated**. `build_custom_op.py` does export
> `LM_LICENSE_FILE` (defaulting to the node-locked `amzn_vq7_us_582883.out`), but that is
> for the *gated engines* a flow might later invoke, not for clang itself. `[HIGH ·
> OBSERVED — flexlm-licensing §2/§7.]`

**Deep pages:** [Build → Compile → Link → Strip → Package Flow](build-flow.md),
[build_custom_op.py Codegen](build-custom-op-codegen.md),
[LSP Linker Specs + ELF Layout](lsp-elf.md),
[The Q7 ELF VADDR + Per-Core Memory Model](q7-elf-vaddr.md),
[FlexLM Licensing Gate](flexlm-licensing.md),
[Toolchain Inventory & Versions](../reference/toolchain-versions.md).

---

## 6. The constraints that will bite you

These are the model's sharp edges — internalize them before you write a kernel.

### Only 8 marshallable dtypes cross the ATen boundary

The on-wire ISA dtype space has 16 codes, but only **8** have a same-meaning
`c10::ScalarType` target — exactly the set the wrapper's decoded compare chain accepts
(`beqi/bnei` immediates `{2,3,4,6,7,8,10,12}`): `INT8`→`Char`, `UINT8`→`Byte`,
`INT16`→`Short`, `INT32`→`Int`, `INT64`→`Long`, `FP16`→`Half`, `BFLOAT16`→`BFloat16`,
`FP32`→`Float`. The marshalling layer has a hard **dtype-match gate**
(`aten_t.dtype() == isa_to_torch_dtype(t_.dtype)`); every other code — `UINT16`/`UINT32`/
`UINT64`, **`FP32R`**, the `FP8_EXP3/4/5` micro-formats — has no ScalarType target and is
**structurally rejected** (the abort arm) at marshal time. Those formats are
device-internal accumulator/quantization types that never reach an `at::Tensor` boundary
in 0.21.2.0. The GPSIMD value path is integer-only soft-float (16f/32f; no HW FP, no
fp64). `[HIGH · OBSERVED — scalartype-dtype-rosetta §1.]` **Deep page:**
[ScalarType ↔ DTYPE Rosetta](scalartype-dtype-rosetta.md).

> **CORRECTION — FP32R is *not* marshallable; the count is 8, not 9.** An earlier
> end-to-end synthesis (ABI-18 §7.2) listed **9** marshallable dtypes, counting
> `FP32R`→`Float` as a 9th (alongside `FP32`). The binary disagrees: the wrapper's decoded
> ordinal tree accepts exactly `{0x2,0x3,0x4,0x6,0x7,0x8,0xA,0xC}` and routes `FP32R`
> (`0xB`) to the **abort** arm — it has no ScalarType and is treated as a programming
> error, not aliased to `Float`. The grounded count is **8**. `[HIGH · OBSERVED —
> scalartype-dtype-rosetta §1, the `beqi/bnei` immediates `{2,3,4,6,7,8,10,12}`.]`

### The op's tensor surface is HBM, not PSUM

The memory map a custom-op kernel sees is: its 32-bit local space (direct), the **HBM**
tensor windows (via `neuron_translate`), the **dataram/TCM** staging window
`[0x80000, 0x90000)`, the per-core HBM-scratch slice, and the `extended_isa::sdk` scratch
globals. **PSUM is not part of the kernel's exposed tensor surface** — it is a TPB
engine-private accumulator, not a region the custom-op marshalling or accessor API hands
you. Your inputs and output are HBM-resident descriptors (or, if `location == SBUF`, a
directly-usable local address); do not expect to read or write PSUM from the op. `[HIGH ·
OBSERVED on the exposed map (device-abi-reference Part 4); HIGH · INFERRED that PSUM is
out of the kernel surface — it never appears as a marshalled tensor location.]` **Deep
page:** [The Device-Side Custom-Op ABI Reference](device-abi-reference.md) Part 4.

### The HBM-stack switch — and what it costs

The on-core Xtensa stack is tiny. `switch_stack_or_call_wrapper` computes
`remaining = SP − ISL` (ISL = the hardware stack-limit register) and, only if your
declared `stack_size` would not fit, allocates an HBM stack (**max 4 MiB**,
`MAX_STACK_SIZE = 0x400000`), translates it to a 32-bit SP, spills the register windows,
and swaps in. The default `STACK_SIZE = 4196` is deliberately tiny so **the common case
takes no switch**; a kernel that needs a deep stack passes a large `STACK_SIZE` to *force*
it. Overflow on the HBM stack is caught by the hardware ISL fault — there is **no software
bounds check** and **no fallback** (alloc failure is a hard `abort()`). Your kernel
arguments do **not** cross the switch; they are pulled by `customop_next_*` after the
wrapper is already running, on whichever stack. `[HIGH · OBSERVED — stack-switch
§2/§3/§5/§7.]` **Deep page:** [Stack-Switch Dispatch](stack-switch.md).

### Per-core memory is private; no cross-core sharing in the customop lib

Each core owns a private 2 MiB SRAM/HBM-scratch window (link-assigned
`0x84000000 + prid·0x200000`), a private 64 KiB SoC aperture (`idx = 2·prid + 9`), and a
private 256 KiB dataram heap. Two cores do **not** see each other's allocations unless
the *host* hands over a shared HBM address. The customop library provides no primitive to
share or synchronize across the 8 cores (§4.1). Design your op as 8 independent workers
over a host-partitioned tensor. `[HIGH · OBSERVED — multicore-spmd §7d/§8.]`

### Don't confuse the address bands

Three numbers look similar and are not:

- **`[0x80000, 0x90000)`** — the 64 KiB dataram / TCM DMA-staging window (where
  `tcm_malloc` buffers must live; the SDMA path asserts this range). `[HIGH · OBSERVED.]`
- **`0x84000000`** — the pinned `hbm_scratch` window base; each core's *code+data* image
  lives in its 2 MiB slice. `[HIGH · OBSERVED.]`
- **`0x02000000`** — the **firmware** image's own data band (the POOL/Q7 engine ELF's
  `.rodata`/`.data`/`.kernel_info_table` VADDR), **not** anything the customop kernel
  addresses. `[HIGH · OBSERVED — q7-elf-vaddr §4.]`

> **CORRECTION — `0x00080000` is the dataram staging window, not a firmware band.** If you
> see `0x80000` (= `0x00080000`) associated with "firmware", that is the **low end of the
> dataram DMA-staging window `[0x80000, 0x90000)`** (a 32-bit NX-local data address), not
> the firmware image band — the firmware band is `0x02000000`. Don't conflate the two.
> `[HIGH · OBSERVED — tensorstream-tcm §1, q7-elf-vaddr §4.]`

---

## 7. Where to go next

| You want to understand… | Read |
|---|---|
| The whole op, host→device, as one journey | [A Custom Op, End to End](../orientation/customop-end-to-end.md) |
| The build pipeline + the 8× link + packaging | [Build Flow](build-flow.md), [Codegen](build-custom-op-codegen.md), [LSP / ELF](lsp-elf.md) |
| How `at::Tensor` becomes a Q7 pointer; the translate TLB | [Q7PtrType](q7ptrtype.md), [Tensor Object Chain](tensor-object-chain.md), [neuron_translate + Windows](neuron-translate-windows.md) |
| Touching data: index vs stage | [TensorAccessor](tensor-accessor.md), [TCM Staging](tensorstream-tcm.md), [Data-Transfer Backends](data-transfer-backends.md) |
| The 8-core SPMD model; the stack switch | [Multicore SPMD](multicore-spmd.md), [Stack-Switch Dispatch](stack-switch.md) |
| Marshalling, dtypes, allocators, coherency | [customop_* Marshalling](customop-marshalling.md), [DTYPE Rosetta](scalartype-dtype-rosetta.md), [Allocators](device-allocators.md), [CoherencyEnforcer](coherency-enforcer.md) |
| The whole device ABI on one screen | [Device-Side ABI Reference](device-abi-reference.md), [The Complete Custom-Op ABI](complete-customop-abi.md) |

Host-side load/prelink/dispatch belongs to [the Host Prelinker](../runtime/prelinker-ucpl.md)
and the 8-core join to
[the 8-Core SPMD Execution Model + Teardown](../runtime/spmd-teardown.md); the cross-rank
device collective barrier to [the NEFF Device Barrier](../collectives/ncfw/neff-device-barrier.md);
the Xtensa KSL/ISL stack-limit exception model to
[the XEA3 Interrupt / Exception Architecture](../control/interrupt/xea3-interrupt-architecture.md).

---

## Self-verification — five framing claims, grounded in the binary

Each is read directly from a shipped object with the native
`xtensa-elf-objdump --xtensa-core=ncore2gp` / `xtensa-elf-nm`, not carried from a report.

1. **`get_cpu_id` is a cached PRID read.** `parallel.o` @0x00 disassembles to
   `entry a1,32; const16 a2,0 (R_XTENSA .bss); l32i.n a2,a2,0; retw.n` — it reloads
   `cpu_id`, which the static ctor `_GLOBAL__sub_I_parallel.cpp` @0x18 latches with
   `rsr.prid a3` (no mask/shift). `[HIGH · OBSERVED.]`
2. **`get_cpu_count` is the folded constant 8.** `parallel.o` @0x10 is
   `entry a1,32; movi.n a2,8; retw.n`. `[HIGH · OBSERVED.]`
3. **The 8 marshalling/launch symbols are defined in the runtime archive.** `nm` resolves
   `customop_setup(bool)`@0x224, `customop_next_tensor()`@0x35c,
   `customop_return_tensor(at::Tensor&)`@0x47c, `customop_cleanup()`@0x5d4, and
   `switch_stack_or_call_wrapper(uint,uint)`@0xd4 — all `T` (defined), byte-exact with
   the deep pages. `[HIGH · OBSERVED.]`
4. **The compile flags + 8× link are verbatim in `build_custom_op.py`.** `CC_OPT` is
   `-g -std=c++14 -stdlib=libc++-e -fno-jump-tables -Os … -mcoproc -fpic -mlongcalls -c
   --xtensa-core=ncore2gp`; `NUM_CPUS=8` builds 8 link strings over
   `lsp_fll_load_cpu{0..7}` plus the `_single` fallback. `[HIGH · OBSERVED.]`
5. **The per-core SRAM origin steps by 2 MiB.** The shipped LSPs read
   `sram0_0_seg org = 0x84000000` (cpu0), `0x84200000` (cpu1), `0x84E00000` (cpu7),
   `len 0x200000`; `cpu_single` is `org 0x84000000, len 0x2000000` (32 MiB). `[HIGH ·
   OBSERVED.]`

**Confidence summary.** Every framing claim on this page is `HIGH/OBSERVED` for its named
symbol, file, address, or flag, or is explicitly tagged where it is `MED`/`INFERRED`
(the stride-ignoring streams; PSUM being out of the kernel surface). The one `LOW`
item is the `>= 4` method-set OOB (off the default path). No contradiction with the
committed Part-7 pages was found; the in-place CORRECTION above (the `0x00080000` band)
re-grounds a common confusion against `tensorstream-tcm` and `q7-elf-vaddr`.
