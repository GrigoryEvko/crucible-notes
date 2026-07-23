# The Custom-Op CPU ABI: `extended_isa::sdk`

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22. The images are `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so` — eight Tensilica Xtensa ELF executables, one per GPSIMD core, 579,380 bytes each. Every virtual address is the per-core link base plus a fixed offset; the link base is `0x84000000 + id·0x200000` (2 MiB stride). File offsets are into the cpu0 image (sha256 `0232625a…7b2a0f96`, md5 `2f8d136633e3f09e6674fd9e5f0aa65f`). Other wheels and other versions will differ — treat every address as version-pinned.*

## Abstract

The "GPSIMD CPUs" run NKI builtin custom-ops as **software** on eight Tensilica Xtensa DSP cores — not as BIR ISA instructions and not on the on-chip pool-aliased GPSIMD *engine* (those are a different thing; see [11.1](gpsimd-xtensa-layout.md)). Each core runs a statically-linked Xtensa ELF that bundles a freestanding PyTorch-`c10`/ATen subset plus a small SDK layer rooted at the C++ namespace `extended_isa::sdk`. This page documents the **op-runtime ABI** that those images implement: the contract a custom-op author must satisfy to ship a kernel onto a GPSIMD core — argument parsing and its `_TENSOR` type gate, the `isa_to_torch_dtype` map (and its nine-of-twenty dtype gap), the shared-DRAM `data_scratch_map_t` coordination struct, the fixed DRAM staging window `[0x80000, 0x90000)`, and the start/exit/allocator lifecycle.

This is a reverse-engineering of a target with **no recovered code bodies**. The Xtensa cores have no disassembler in this extraction — host binutils has no Xtensa backend, `objdump` reports architecture *unknown*, and IDA's auto-analysis recovered exactly **2** trivial stub functions and **0** decompiled bodies. The entire ABI here is reconstructed from non-code evidence: the verbatim `__FILE__:__LINE__` + predicate-expression assert strings the compiler baked into `.rodata` (these literally transcribe the C++ conditions the runtime evaluates), the `.rodata` typename and op-name tables, the raw ELF section / `.ctors` / `.dtors` bytes, and the per-core link-rebase delta. Where a fact is a literal string or raw byte it is **CERTAIN**; a single reasonable reading of such a literal is **HIGH**; a structural deduction with no literal behind it is **INFERRED**; anything not recoverable from strings or bytes at all is **SPECULATIVE**. The page never presents instruction-level behaviour as observed — only what the strings and structure force.

> **NOTE — rodata-only grounding.** Every claim on this page rests on `.rodata`/`.data` strings, `readelf`/`xxd` of the ELF, and a byte-diff across the eight per-core images. No claim is grounded in recovered Xtensa instruction logic, because none exists. The asserts are the contract: a stripped C++ binary still ships its `assert(expr)` message strings, and each one transcribes a runtime invariant verbatim. That is the entire evidence floor.

For reimplementation, the contract a GPSIMD custom-op must satisfy is:

- **One image per core**, linked against `extended_isa::sdk` + the bundled ATen/`c10`, re-linked at each core's `0x84000000 + id·0x200000` base.
- **Arguments arrive as a typed ISA vector**; every consumed argument must carry the `_ARG_TYPE_TENSOR` tag, with inline (not out-of-line) shape and rank ≥ 1. Scalar attributes ride as 1-element tensors.
- **Dtype must round-trip** `isa_to_torch_dtype` — one of nine real-numeric `c10::ScalarType`s; complex/qint are rejected and **no FP8/FP4/MX type is mappable**.
- **Tensor bytes stage through a fixed 64 KiB DRAM window** `[0x80000, 0x90000)`, validated per-transfer; the per-core id is `< 8`; memory window 0 must already map `SUNDA_APB_BASE`.
- **Outputs allocate through the `NeuronAllocator`-backed ATen path**; working/coordination storage overlays `data_scratch_map_t` on the shared scratch.

| | |
|---|---|
| **Images** | `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so` |
| **Arch / class** | Tensilica Xtensa, `ELF32` LSB, `EXEC`, statically linked, stripped, `Flags 0x300` |
| **SDK namespace** | `extended_isa::sdk` (`extended_isa::sdk::data_scratch_size`, `…::data_scratch`) |
| **SDK source root** | `/opt/workspace/SundaCustomOpLibrary/custom_op/library/` |
| **Entry** | `start` @ `base + 0xcd94` (cpu0 `0x8400cd94`) |
| **Static ctors** | **6** (`.ctors` @ off `0x75d30`); **0** dtors; empty `.eh_frame` |
| **Arg type gate** | `NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR` (`arg_parser.hpp:67`) |
| **Dtype map** | `isa_to_torch_dtype` (`utypes.hpp:65`) — **9** ScalarTypes, no FP8/FP4/MX |
| **DRAM window** | `[0x80000, 0x90000)` = 64 KiB (`data_transfer.cpp:160`) |
| **Core count** | `cpu_id < 8` (`data_transfer.cpp:171`) |
| **Window invariant** | `READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE` (`data_transfer.cpp:240`) |
| **Coordination struct** | `data_scratch_map_t` in shared DRAM (`start_exit.cpp:451`) |
| **Assert sites** | **67** distinct `file:line` (SDK 23 · operator 25 · `c10`/ATen 19) |
| **Evidence floor** | `readelf -h/-S`, `xxd`, `strings -a -t x`, `cmp` across cpu0..cpu7 |

The op this binary actually implements is the bitonic SORT / TopK builtin — its algorithm and merge tree are documented at [11.2](bitonic-sort-topk.md). This page is about the **substrate ABI** that SORT (and any future GPSIMD custom-op) is built on.

---

## 1. The SDK Source Tree

The assert strings carry full paths, so the SDK's file inventory is directly readable. The ABI proper lives under one root; the bundled PyTorch and the embedded operator are separate trees linked into the same image.

```text
/opt/workspace/SundaCustomOpLibrary/custom_op/library/      ── the SDK ABI
  arg_extractor.hpp   (1 assert)   ── bounded arg accumulator
  arg_parser.hpp      (2 asserts)  ── per-arg ISA cursor + _TENSOR type gate
  utypes.hpp          (13 asserts) ── ISA-tensor <-> at::Tensor wrapper, dtype map
  start_exit.cpp      (1 assert)   ── lifecycle: scratch sizing
  allocator.cpp       (1 assert)   ── wires the Sunda mem-mgr into c10::Allocator
  data_transfer.cpp   (5 asserts)  ── DMA in/out of the DRAM window

/opt/amazon/custom_op/{c10,torch/include}/...               ── bundled PyTorch-c10
/opt/amazon/include/c10/...                                 ── (2nd dedup'd copy)
  .../neuron_torch_extension/NeuronAllocator.h              ── ATen<->Sunda allocator bridge

sort_and_merge.cpp / bitonic_sort.cpp                       ── the embedded SORT/TopK op (11.2)
```

The two PyTorch spellings (`/opt/amazon/custom_op/...` and `/opt/amazon/include/c10/...`) are two build trees merged at link time; a third spelling, `/workplace/auderian/custom-ops/dana-libc-cr/src/KaenaPytorchCustomOps/pytorch_source/c10/...`, names the cross-build umbrella (`dana-libc-cr` = the libc + PyTorch-`c10` cross-build for the Sunda CPU; `KaenaPytorchCustomOps` the package). The SDK is therefore a **freestanding, statically-linked PyTorch-`c10` subset for an Xtensa core** — `at::Tensor` and `c10::ScalarType` semantics, but no Python, no autograd, no dynamic loader.

> **QUIRK — "Sunda" is the NeuronCore family, not a vendor.** `SundaCustomOpLibrary`, `SUNDA_APB_BASE`, and the `targets/sunda/` codegen tree all name the Trainium/Sunda NeuronCore. The GPSIMD custom-op runtime is a per-target software layer named after the core generation it ships on.

---

## 2. Image Geometry and the Per-Core Rebase

Every address-bearing claim depends on the link layout, which `readelf -S` gives directly:

```text
[ 1] .text          VMA 84000000  off 000094  size 0x737d8 (473048)  AX
[ 2] .clib.rodata   VMA 84073800  off 073894  size 0x2004            A
[ 3] .clib.data     VMA 84075840  off 0758d4  size 0x0458            WA
[ 4] .eh_frame      VMA 84075c98  off 075d2c  size 0x0004  (= 0x00000000, empty)
[ 5] .ctors         VMA 84075c9c  off 075d30  size 0x0020  (8 words)
[ 6] .dtors         VMA 84075cbc  off 075d50  size 0x0008  (2 words)
[ 7] .rodata        VMA 84075d00  off 075d94  size 0x5a48 (23112)    A
[ 8] .data          VMA 8407b780  off 07b814  size 0x11cc8 (72904)   WA
[ 9] .bss           VMA 8408d480  off 08d4dc  size 0x12f20 (77600)   WA
```

For the low-VMA allocated sections, **VMA == link base + (file offset − 0x94)**; `.rodata` runs from VMA `0x84075d00` (file `0x75d94`). All assert-string VMAs on this page are cpu0; the per-core VMA is `base(id) + (cpu0_VMA − 0x84000000)`.

The eight images are **not byte-identical** — they are one program re-linked at a per-core base. `md5sum` over cpu0/cpu1/cpu7 yields three distinct digests (`2f8d1366…`, `9bd70ccf…`, `dfef2996…`), while all eight are exactly 579,380 bytes with an identical section-table shape. `cmp` cpu0↔cpu1 reports ~12,706 differing bytes, all absolute-address fixups bumped by `+0x200000` (Xtensa code is non-PIC; every `l32r` literal and data pointer is rebased). The entry advances in lockstep: `0x8400cd94` (cpu0), `0x8420cd94` (cpu1), …, `0x84e0cd94` (cpu7), i.e. `base(id) = 0x84000000 + id·0x200000`.

> **GOTCHA — "identical" is true on one axis and false on the other.** Across **cores**, the eight `cpu*.so` are link-rebased copies with eight distinct md5s, identical only in size and section shape. Across **wheels**, cpu0 from cp310/cp311/cp312 shares one md5 (`2f8d1366…`). A tool that dedupes the eight core images as one file will silently drop seven distinct link bases.

The architectural consequence is the memory model in [§5](#5-memory-model-the-dram-window): each core runs from its own private 2 MiB code/data aperture (`0x84000000 + id·0x200000`), but the SDK's coordination invariants — `dram_addr ∈ [0x80000,0x90000)`, `cpu_id < 8`, `MEM_WINDOW0_LO == SUNDA_APB_BASE` — are **byte-identical** in all eight images, which is what fixes a single physically-shared DRAM aperture across the cores.

---

## 3. Argument Parsing

The op receives an ISA argument vector — the decoded `0x86` `CUSTOM_OP_PAYLOAD` stream emitted by the BIR-side encoder ([NeuronCodegen `builtin_custom_op`](../nki/neuroncodegen-builtin-customop.md), [collective/custom-op encoding](../isa/collective-customop-encoding.md)). Two classes drive parsing; both assert verbatim.

### Purpose

`arg_extractor` is a bounded accumulator (a fixed-capacity slot array, no heap growth in the hot path); `arg_parser` is the per-argument cursor that walks the ISA vector and type-gates each entry. Together they turn the wire payload into a sequence of `at::Tensor`s built by `utypes`.

### Algorithm

```c
// arg_extractor.hpp:16   @0x8407ed00   num_args < max_args_
function add_arg(extractor, slot):                 // bounded accumulator
    assert(extractor.num_args < extractor.max_args_); // fixed-capacity, no growth
    extractor.slots[extractor.num_args++] = slot;

// arg_parser.hpp:66/67   @0x8407ed60 / @0x8407edc7
function next_tensor(parser, isa_args):            // per-arg cursor
    assert(parser.curr_arg_ < isa_args.get_num());          // :66  in-bounds
    assert(isa_args.get_types()[parser.curr_arg_]
             == NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR);  // :67  type gate
    utype t = build_utype(isa_args, parser.curr_arg_);      // -> utypes.hpp (§4)
    ++parser.curr_arg_;
    return t;
```

`isa_args` is the ISA-side container with two parallel arrays: `get_num()` (the count) and `get_types()[i]` (a per-argument type tag from the enum `NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_*`). The cursor **hard-requires** the tag to equal `_ARG_TYPE_TENSOR` — a single `==`, not a `switch`.

### The arg-type enum

Only `_ARG_TYPE_TENSOR` is string-resident (it is the only member named in an assert). The enum is the shared ISA contract — defined in the neuron-isa headers, consumed by both the encoder and this runtime — so sibling tags (e.g. `_SCALAR`/`_IMM`/`_SHAPE`) plausibly exist but are unused here and appear in no string — **INFERRED**.

> **GOTCHA — scalar attributes are not a separate arg tag.** SORT's `K`/`axis`/`descending` reach the op as **rank-1, 1-element tensor arguments**, narrowed to `at::Scalar` at `utypes.hpp:172/178` (`nelem_u32[i] == 0` / `nelem_u16[i] == 0` — "Cannot create an at::Scalar. Argument has more than 1 element"). A reimplementation that expects a `_SCALAR`-tagged argument will never see one through this path; every argument is `_TENSOR`, and "scalars" are 1-element tensors. The `u32` and `u16` element-count paths are two index-width specialisations of the same narrowing.

### Tensor shape encoding

```c
// utypes.hpp:94  @0x8407f13a   (and :167 @0x8408012a — second build site)
assert(t_.framework_shape_type
         != NEURON_ISA_TPB_CUSTOM_OP_TENSOR_SHAPE_TYPE_OUT_OF_LINE_SHAPE);
// utypes.hpp:139 @0x8407f1d6
assert(num_dim != 0 &&
       "Input and output tensors cannot have 0 dimensions - must be of at least rank 1");
```

Each tensor argument `t_` carries a `framework_shape_type` of enum `NEURON_ISA_TPB_CUSTOM_OP_TENSOR_SHAPE_TYPE_*`. There are (at least) two encodings — an in-line/default (dims packed directly into the arg record) and `_OUT_OF_LINE_SHAPE` (rank/dims in a separate side-buffer, for high-rank tensors). Both `utypes` sites **assert the shape is not `_OUT_OF_LINE_SHAPE`**: this runtime accepts only the inline encoding, and every in/out tensor must be **rank ≥ 1** — zero-dim/scalar-rank tensors are forbidden.

---

## 4. The Universal-Tensor Wrapper and the Dtype Map

`utypes.hpp` (13 asserts — the densest SDK file) wraps an ISA tensor as an `at::Tensor`. `build_utype()` validates the shape, acquires the on-core data pointer, gates the dtype, and runs the staging transfers.

### Algorithm

```c
// build_utype()   utypes.hpp, asserts at the cited VMAs
function build_utype(isa_args, i):
    // 1. shape: inline only, rank>=1   (:94 @0x8407f13a, :139 @0x8407f1d6)
    assert(t_.framework_shape_type != ..._OUT_OF_LINE_SHAPE);
    assert(num_dim != 0);

    // 2. backing pointer must be set    (:64 @0x8407eea1)
    assert(xt_ptr_);                                   // on-core Xtensa data buffer

    // 3. DTYPE GATE — round-trip ISA<->torch   (:65 @0x8407eeed)
    assert(aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype));

    // 4. staging transfers + size sanity (:73/:74/:77/:89)
    assert(ret == 0);                                  // :73  alloc/copy ok
    assert(buffer_bytes > 0);                          // :74  non-empty backing
    assert(ret == 0);                                  // :77
    assert(ret == 0);                                  // :89
    // utypes.hpp:37 / :59 are assert(0) — unreachable/unsupported defaults
    return aten_t;
```

The pivotal line is `:65`: the wrapped `at::Tensor`'s `ScalarType` must equal `isa_to_torch_dtype(t_.dtype)`, where `t_.dtype` is the ISA wire-tag (`NEURON_ISA_TPB_DTYPE`). The map's domain is the BIR/ISA dtype enum and its range is `c10::ScalarType`.

### The nine supported dtypes

The map body is a code `switch` (no string table for the arms themselves), but its **range is pinned by a verbatim `.rodata` typename table** — the AT_DISPATCH-style typed-kernel instantiation table for the SORT op. `xxd` at file offset `0x7ecda` shows a contiguous NUL-separated run, immediately preceding the compute-fn name table at `0x7ed20`:

```text
0007ecda:  uint8_t\0 int8_t\0 int16_t\0 int\0 int64_t\0 float\0 double\0
           at::BFloat16\0 at::Half\0
           sort_singlecore_compute\0 ...    <- compute-fn table starts here
```

A second partial copy lives at `0x7d29a` (`uint8_t … at::Half`). These are exactly **nine** typenames → nine `c10::ScalarType`s:

| ISA wire-tag (BIR Dtype) | `.rodata` typename | `c10::ScalarType` | Confidence |
|---|---|---|---|
| `uint8` | `uint8_t` | `kByte` | CERTAIN (in table) |
| `int8` | `int8_t` | `kChar` | CERTAIN (in table) |
| `int16` | `int16_t` | `kShort` | CERTAIN (in table) |
| `int32` | `int` | `kInt` | CERTAIN (in table) |
| `int64` | `int64_t` | `kLong` | CERTAIN (in table) |
| `float32` | `float` | `kFloat` | CERTAIN (in table) |
| `float64` | `double` | `kDouble` | CERTAIN (in table) |
| `bfloat16` | `at::BFloat16` | `kBFloat16` | CERTAIN (in table) |
| `float16` | `at::Half` | `kHalf` | CERTAIN (in table) |

The map is bijective over these nine real-numeric types; the reverse direction (`c10` → ISA) is the `utypes.hpp:65` equality check itself (a round-trip consistency gate). The SORT op's `int32` index output uses `kInt`. The typenames are literal; the exact switch-arm ordinals are not byte-verified, since there is no disassembler for this target.

> **GOTCHA — nine of twenty: no FP8, no FP4, no MX.** The BIR Dtype universe is **twenty** entries (the `qword_1DFC040` 20×`u64` stride table — see [7.6, BIR Dtype tables](../bir/dtype-tables.md)), and includes `float8e3/e4/e5`, `float8_e4m3fn`, `float8_e5m2`, `float8_e8m0fnu`, `float4_e2m1fn_x4`, and the `_x4`-packed MX containers. The GPSIMD CPU runtime maps **only nine** of them. A `strings` sweep of the cpu0 image for `float8`/`fp8`/`e4m3`/`e5m2`/`fp4`/`float4`/`e2m1`/`mxfp` returns **nothing** — none of the low-precision types has a typename, a switch arm, or any presence in the image. A custom-op author who feeds an FP8/FP4/MX tensor to a GPSIMD op trips the `:65` equality assert (or the explicit reject below). The GPSIMD CPU is a real-numeric, ≥8-bit compute substrate only.

### The reject path

```text
@0x7bd60 / @0x7d7bf   "Invalid datatype. Note that complex scalars and qints are not supported"
@0x7f1bb              "Unknown ScalarType"   (c10/core/ScalarType.h)
```

Complex (`c10::complex<…>`) and quantized (`c10::qint8`/`qint32`) types are explicitly rejected — those typenames appear in the image only inside `fully_qualified_type_name_impl()` diagnostics, never as a supported compute path.

> **NOTE — relation to the BIR dtype tables.** `isa_to_torch_dtype`'s domain is the on-wire `NEURON_ISA_TPB_DTYPE` tag (the encoder side, [11-encoding](../nki/neuroncodegen-builtin-customop.md)); its range is the nine `c10::ScalarType`s here. The full BIR-Dtype → wire-tag mapping and the twenty-entry roster are owned by [7.6](../bir/dtype-tables.md); this page owns only the nine-element GPSIMD CPU subset and its gap.

---

## 5. Memory Model: the DRAM Window

The op's tensor data does not live natively in the Xtensa core's address space — it sits in TPB DRAM and is windowed in. `data_transfer.cpp` (5 asserts) gates every move.

```text
@0x84080400  data_transfer.cpp:92    ret == 0
@0x84080454  data_transfer.cpp:160   dram_addr >= 0x80000 && dram_addr < 0x90000
@0x840804cc  data_transfer.cpp:171   cpu_id < 8
@0x84080523  data_transfer.cpp:200   0                       (unreachable default)
@0x84080571  data_transfer.cpp:240   READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE
```

### The `[0x80000, 0x90000)` aperture

Every tensor's source/target `dram_addr` must satisfy `0x80000 ≤ dram_addr < 0x90000`. This is a fixed **64 KiB DRAM aperture** — the only region from which the op pulls or pushes tensor bytes, and the region onto which `data_scratch_map_t` ([§6](#6-the-data-scratch-map-coordination-struct)) is overlaid. The `0x86` payload's AccessPattern base addresses are range-checked into here; any tensor outside the window trips the assert and aborts the op.

This 64 KiB window is **disjoint** from each core's private 2 MiB code/data aperture (`0x84000000 + id·0x200000`). The low `[0x80000,0x90000)` region is the cross-core shared staging mailbox; the high `0x84xxxxxx` region is per-core private code/data. Because the bound is a byte-identical assert in all eight images, the window is the same physical DRAM for every core. The bound itself is literal; reading the split as private-vs-shared is the interpretation that fits it.

### The core count

`cpu_id < 8` fixes exactly **eight** GPSIMD cores. The id is read at runtime (a core cannot read it from a baked literal — the images differ only by rebase; the runtime source is `MEM_WINDOW0_LO`, per [11.1](gpsimd-xtensa-layout.md)), and is used both to select this core's slice in the multicore split and to index the per-core slot of the coordination struct.

### The window-0 invariant

`READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE` is a hard ISA invariant checked before window-0 DMA: the SDK reads the 64-bit user register `MEM_WINDOW0_LO` (the low base of memory-window #0, an APB-mapped config register) and asserts it equals the compile-time constant `SUNDA_APB_BASE` (the Sunda Advanced Peripheral Bus base). i.e. window #0 must already be programmed to map the APB aperture before the op issues a transfer.

```c
// INFERRED copy mechanism (names only; no code body recovered):
//   TPB DRAM [0x80000,0x90000) is reachable from the Xtensa core through
//   memory-window 0 (a base+limit aperture in UREG pair MEM_WINDOW0_LO/_HI)
//   mapped onto SUNDA_APB_BASE. A copy primitive (the ret==0 calls at
//   :92 and utypes :73/:77/:89) streams windowed bytes into the core-local
//   xt_ptr_ buffer (utypes:64) where the at::Tensor wraps them. The op
//   computes on the core-local copy, then writes results back through the
//   same window to each output's dram_addr.
```

> **NOTE — `SUNDA_APB_BASE` is not recoverable.** Its numeric value is an Xtensa `l32r` literal-pool immediate; with no disassembler, only the constraint *text* survives. The same is true of `extended_isa::sdk::data_scratch_size`. Both numeric values are therefore **SPECULATIVE** here — recover them from the hardware memory-model side. The *constraint* itself, that window-0 must equal the APB base, is literal.

---

## 6. The Data-Scratch-Map Coordination Struct

The SDK provides a fixed-size data-scratch region and overlays a single coordination struct on it. The one surviving literal is the size gate:

```c
// start_exit.cpp:451   @0x8408031d
assert(extended_isa::sdk::data_scratch_size >= sizeof(data_scratch_map_t));
```

Read it as the classic "fixed shared-memory mailbox" pattern: at op init the runtime hands the SDK a region of `extended_isa::sdk::data_scratch_size` bytes (a build-time namespace constant), the SDK overlays one `data_scratch_map_t` at its head, and asserts the region is at least `sizeof` that struct. All cores agree on the layout because they share the physical window. The struct lives in the `[0x80000, 0x90000)` aperture ([§5](#5-memory-model-the-dram-window)), so `sizeof(data_scratch_map_t) ≤ data_scratch_size ≤ 0x10000` (64 KiB) — the inner bound is literal, the 64 KiB ceiling follows from the aperture.

> **GOTCHA — the struct's *fields* are not in the binary.** The SDK asserts the struct's **size**, never any field name — so the field layout below is **INFERRED** from the surrounding flow, not read from a literal. What is literal: (1) the struct exists and is named `data_scratch_map_t`; (2) it is size-bounded by `data_scratch_size`; (3) it is overlaid on the 64 KiB shared window; (4) it is indexed by `cpu_id < 8` (so per-core arrays are dimensioned `[8]`); (5) it carries paired `(data, indices)` buffer references, because every sort/merge assert validates a `(data_buffer, indices_buffer)` pair as non-null ([11.2](bitonic-sort-topk.md)).

```c
// data_scratch_map_t — overlaid on DRAM [0x80000, 0x90000)
// FIELD LAYOUT IS INFERRED (no field-name strings); only the struct's
// existence, size bound, location, and cpu_id[8] indexing are literal-grounded.
struct data_scratch_map_t {
    struct {                       // (a) per-core scratch descriptors, index = cpu_id
        uint32_t base;             //     DRAM addr of this core's buffer (in window)
        uint32_t size;             //     byte length
    } core[8];                     //     dim 8 follows from the cpu_id < 8 bound

    volatile uint32_t arrive_count;   // (b) doSyncPhysicalCores barrier — arrival count
    volatile uint32_t release_phase;  //     sense flag flipped by the last arriver
    volatile uint32_t per_core_flag[8]; //   optional per-core ready flags

    struct {                       // (c) data-transfer descriptors for the merge step
        uint32_t data_addr;        //     DRAM addr of subtensor values
        uint32_t indices_addr;     //     DRAM addr of subtensor indices
        uint32_t nelem;            //     run length
    } xfer[8];                     //     count bounded by num_subtensors <= cpu_count
};
```

The barrier interpretation (b) rests on the cross-core software-sync the multicore SORT requires; the buffer-pair interpretation (c) mirrors the non-null `(data_buffer, indices_buffer)` assertion pattern that recurs through `sort_and_merge.cpp` and `bitonic_sort.cpp`. Widths and ordering are deductions; the per-core `[8]` dimension is the one structurally-forced dimension.

---

## 7. Start / Exit and the Allocator Flow

### Start

The ELF entry `start` @ `0x8400cd94` runs the C++ static-initializer table, then transfers to SDK main. The `.ctors` table (`xxd` at file `0x75d30`, 8 LE words) reads:

```text
[0] 0xffffffff   <- GNU .ctors sentinel / count marker
[1] 0x8400db90   <- ctor_001
[2] 0x8400e380   <- ctor_002
[3] 0x8401b3c8   <- ctor_003
[4] 0x84037018   <- ctor_004
[5] 0x84037ecc   <- ctor_005
[6] 0x8404097c   <- ctor_006
[7] 0x00000000   <- terminator
```

Exactly **6** static constructors, read straight from the `.ctors` bytes. What they construct is **INFERRED** from the `c10`/SDK string-reference pattern: the bundled `c10` globals (type-meta registry, the `Allocator` table / `GetAllocator`, `Logging`, the `DispatchKey` maps, `ThreadLocalDebugInfo`, the `UndefinedTensorImpl` singleton) and the SDK singletons (the `NeuronAllocator`/Sunda mem-mgr, the arg-parser scaffolding) — the standard "before `main()`" PyTorch-runtime bring-up for a freestanding `c10` subset.

The per-invocation lifecycle (`start_exit.cpp`), reconstructed from the assert ordering:

```c
// start_exit.cpp — per-invocation flow (assert ordering @ :451 is the init step)
function op_main():
    read cpu_id;  assert(cpu_id < 8);                    // data_transfer.cpp:171
    assert(READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE); // :240
    assert(data_scratch_size >= sizeof(data_scratch_map_t));     // start_exit.cpp:451
    overlay data_scratch_map_t on [0x80000, 0x90000);

    for each ISA arg:                                    // arg_parser.hpp:66/67
        t = build_utype(isa_args, i);                    // §4: shape, dtype, DMA-in
    dispatch on function_name -> one of the four *_compute entries (§8);
    // ... compute ...
    write results back through MEM_WINDOW0 to each output dram_addr;
    signal completion to the BIR engine that issued the 0x85/0x86 op;  // INFERRED
```

### Exit

The `.dtors` table (`xxd` at file `0x75d50`) is two words: `0xffffffff` (sentinel) + `0x00000000` (terminator) — **zero destructors**. `.eh_frame` is 4 bytes of zero (no C++ unwind tables). The op is a one-shot kernel: there is no global C++ teardown on exit, and asserts `abort()`/trap rather than throw across the SDK boundary.

### The allocator

```c
// allocator.cpp:45     @0x840803a9   mgr != nullptr
// NeuronAllocator.h:18 @0x8407c446 (+ dup @0x8407de86)   ret == 0
```

Two allocator layers. The SDK's `allocator.cpp` manager (`mgr`) must be non-null before use; it backs `at::Tensor` storage on the core. `NeuronAllocator` (`/opt/amazon/.../neuron_torch_extension/NeuronAllocator.h`) is the bridge that registers the Sunda allocator into ATen's `c10::GetAllocator`, so `at::empty`/`at::Tensor` allocate from the on-core scratch arena rather than host `malloc`. The chain is forced by the `c10` `StorageImpl` asserts that fire on misuse — `StorageImpl.h:51 allocator_`, "For resizable storage, allocator must be provided", `Allocator.cpp:36 alloc` — and by `allocator<T>::allocate(size_t n) 'n' exceeds maximum supported size`. ATen storage is wired to the custom allocator; the Sunda mem-mgr hands out bytes from the per-core private arena — the upper headroom of the 2 MiB image window.

> **NOTE — no Python, no autograd.** `c10/PyInterpreter.cpp:12..99` asserts are all literal `assert(0)` — every PyObject dispatch hook is a hard trap. The op runs bare-metal `at::Tensor` with no interpreter and no autograd graph. The bundled `c10` exists purely to model op I/O as tensors.

---

## 8. The Author-Facing Compute Contract

A GPSIMD custom-op author exports a **named compute function** and registers its name as the op's `function_name` (threaded from NKI `builtin_custom_op`). The dispatch names are a contiguous `.rodata` table at `0x7ed20`, terminated by an `error` sentinel (`xxd` shows them immediately following the dtype table):

```text
@0x7ed20  sort_singlecore_compute
@0x7ed38  sort_multicore_compute
@0x7ed4f  partial_sort_multicore_compute
@0x7ed6e  partial_merge_multicore_compute
@0x7ed8e  error                            <- sentinel / default
```

The names are literal; the signature below is reconstructed, with the parameter list deduced from the parse/alloc flow:

```c
// <name>_compute — author entry point, dispatched on function_name
void name_compute(/* at::Tensor in/out views built by utypes from the ISA arg vector,
                     cpu_id, cpu_count, the data_scratch_map_t scratch */);
```

The function receives `at::Tensor` views wrapping the windowed core-local buffers and writes into output tensors allocated via the `NeuronAllocator` path ([§7](#7-start--exit-and-the-allocator-flow)).

### The multicore split

`sort_and_merge.cpp` asserts pin the 8-way fan-in:

```text
@0x8407d5df  sort_and_merge.cpp:598   num_subtensors <= cpu_count   <- <= 8
@0x8407d612  sort_and_merge.cpp:608   dim0 == 2
@0x8407d633  sort_and_merge.cpp:618   right_start < dim2
```

The input is tiled into `num_subtensors` (≤ `cpu_count` = 8); each core sorts its sub-tensor (`sort_singlecore` on its slice); then a merge tree (`partial_sort`/`partial_merge` across cores) combines them through the shared scratch. A `*_multicore_compute` function must honor `cpu_id`/`cpu_count` for its slice.

> **QUIRK — the output is always rank-3 `[2, d0, d1]`.** Three identical assert triples (`sort_and_merge.cpp:365-367`, `:394-396`, `:444-446`) pin `t_out.size(0) == 2`, `t_out.size(1) == data_dim0`, `t_out.size(2) == data_dim1`. `dim0 == 2` is the `{values, indices}` pair: `[0]` = sorted values, `[1]` = `int32` indices (`bitonic_sort.cpp:523` names `indices_buffer_i32`). This top-k-style packed output, and the `len % (SIMD_WIDTH·2) == 0` bitonic constraint (`bitonic_sort.cpp:302`), are the SORT consumer's shape contract — detailed at [11.2](bitonic-sort-topk.md).

### The full author contract

1. **Ship** an Xtensa `.so` linked against `extended_isa::sdk` + the bundled ATen/`c10`, re-linked per core (or rebased per `cpu_id`, [§2](#2-image-geometry-and-the-per-core-rebase)).
2. **Export** a named `*_compute` function; register its name as the op `function_name`. **≤ 1 output**; all arguments `_TENSOR`-tagged (1-element tensors carry scalar attrs).
3. **Inputs**: rank ≥ 1, inline shape (not `_OUT_OF_LINE`), dtype in the nine-element `isa_to_torch_dtype` set (no complex/qint/FP8/FP4/MX), `dram_addr ∈ [0x80000, 0x90000)`.
4. **Read** `at::Tensor` inputs via the `arg_parser`/`utypes` views (DMA'd through `MEM_WINDOW0`/`SUNDA_APB_BASE`); **allocate** outputs via the `NeuronAllocator`-backed ATen path; use `extended_isa::sdk::data_scratch` (overlay `data_scratch_map_t`) for working/coordination storage.
5. **For multicore**: honor `cpu_id (<8)` and `cpu_count`; tile into ≤ `cpu_count` sub-tensors; merge across cores (`partial_sort`/`partial_merge` pattern).
6. **On exit**: write results back through the window to each output `dram_addr`, signal completion. No C++ teardown runs.

---

## 9. The Assert Inventory

The 67 distinct `file:line` assert sites in the cpu0 image partition into three layers. The exact count is reproducible: `strings -a libbuiltincustomop_cpu0.stripped.so | rg -o '[A-Za-z_][A-Za-z0-9_]*\.(cpp|hpp|h|cc):[0-9]+' | sort -u | wc -l` → **67**.

| Layer | Sites | Source | What it guards |
|---|---|---|---|
| **SDK ABI** | 23 | `SundaCustomOpLibrary/custom_op/library/*` | arg parse, dtype, window, scratch, allocator |
| **Operator** | 25 | `sort_and_merge.cpp` / `bitonic_sort.cpp` | output shape, buffer non-null, multicore bound |
| **`c10`/ATen** | 19 | bundled PyTorch (3 path spellings) | tensor-API misuse, allocator presence |

The 23 SDK sites are the contract this page documents (the full list is in [§1](#1-the-sdk-source-tree)'s file map, expanded inline through [§3](#3-argument-parsing)–[§8](#8-the-author-facing-compute-contract)). The 19 `c10`/ATen sites are stock upstream `INTERNAL_ASSERT`/`TORCH_CHECK` — notably `ATen/Functions.h:197 tensor.is_non_overlapping_and_dense()` (inputs must be dense), the `c10/core/Device.h` CPU-index checks, and the `intrusive_ptr.h` refcount invariants — present only because the SDK links the tensor subset; they are not part of the GPSIMD-specific contract. The 25 operator sites belong to SORT/TopK ([11.2](bitonic-sort-topk.md)).

> **NOTE — the asserts *are* the documentation.** Because no code body was recovered, this page is a transcription of the binary's own runtime predicates. Every `assert(expr)` is a contract the runtime enforces at op entry; a reimplementer who satisfies all 23 SDK predicates produces input the runtime will accept. The 67-site count, the nine-entry dtype table, the `.ctors`/`.dtors`/window bytes, and the per-core rebase are the verifiable spine; the field layout of `data_scratch_map_t`, the copy mechanism, and the compute-fn signature are the **INFERRED** scaffolding around it.

---

## Related Components

| Name | Relationship |
|---|---|
| GPSIMD Xtensa ELF layout (11.1) | The image geometry, per-core rebase, and `MEM_WINDOW0_LO` cpu_id source this ABI runs on |
| Bitonic SORT/TopK builtin (11.2) | The operator embedded in these images; consumes this ABI |
| ATen/`c10` dispatch surface (11.4) | The bundled tensor subset and `NeuronAllocator` bridge ([aten-c10-surface.md](aten-c10-surface.md)) |
| BIR Dtype tables (7.6) | The twenty-entry dtype universe of which `isa_to_torch_dtype` maps nine |
| NeuronCodegen `builtin_custom_op` | The BIR-side encoder that emits the `0x85`/`0x86` payload this ABI decodes |

## Cross-References

- [The GPSIMD CPUs: 8-core Xtensa ELF Layout](gpsimd-xtensa-layout.md) — image geometry, the 2 MiB per-core aperture, runtime cpu_id derivation
- [The Bitonic SORT / TOPK Builtin Algorithm](bitonic-sort-topk.md) — the operator these images implement, the `[2,d0,d1]` output, the merge tree
- [BIR Dtype Tables](../bir/dtype-tables.md) — the full 20-entry dtype roster and stride LUT (`qword_1DFC040`); the FP8/FP4/MX types absent here
- [NeuronCodegen `builtin_custom_op` Emitter](../nki/neuroncodegen-builtin-customop.md) — the encoder side that produces the ISA arg vector
- [Collective / GPSIMD / CustomOp Encoding](../isa/collective-customop-encoding.md) — the `0x85`/`0x86` wire format this ABI's `arg_parser` decodes
