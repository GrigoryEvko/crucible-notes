# The Bitonic SORT / TOPK Builtin Algorithm

> *All addresses, sizes, and strings on this page apply to neuronx_cc 2.24.5133.0 (build `+58f8de22`), cp310/311/312 wheels. The backing image is `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so`. Other versions will differ.*

## Abstract

The Neuron compiler ships a precompiled **sort / top-k builtin** that runs on the GPSIMD engine's eight Xtensa cores rather than on the systolic array or the vector engine. It is delivered as `libbuiltincustomop_cpu{0..7}.stripped.so` — eight per-core ELF32 Xtensa executables under `neuronxcc/data/custom_op/`, each 579,380 bytes, and shipped a *second* time, byte-for-byte, as `neuronxcc/l/l_cpu{0..7}.stripped.so` (the cpu0 copies share md5 `2f8d136633e3f09e6674fd9e5f0aa65f`). The implementation is a C++ PyTorch custom op built against a trimmed `c10` / `SundaCustomOpLibrary` runtime, compiled from two translation units: `bitonic_sort.cpp` (the in-core SIMD sort kernel) and `sort_and_merge.cpp` (the cross-core dispatch, merge, and the host-facing argument contract).

The algorithm is a classic **bitonic sort** within each core followed by a **pairwise tree merge** across cores. Each element travels as a `(value, index)` pair so that the same comparator network that sorts the values simultaneously permutes the original positions — that is what makes the op a top-k provider, not just a sort: the indices come out for free. The output is a single packed tensor of shape `[2, data_dim0, data_dim1]`, where plane 0 carries the sorted values and plane 1 carries the gathered indices. Four named entry points split the work along two axes: **single-core vs. multi-core**, and **full sort vs. partial (`partial_*` = top-k)**.

> **NOTE — strings-only reconstruction.** The eight `.so` files are fully stripped (no `.symtab`, no `.dynsym`; `nm -D` and `readelf -s` return nothing) and contain **Xtensa** machine code, for which this corpus has *no recovered disassembly or decompilation*. Every claim below is reconstructed from `.rodata` — `__FILE__:__LINE__ assert-condition` strings, the four dispatch name literals, the c10/SundaCustomOpLibrary build strings — plus the ELF layout and general knowledge of bitonic networks. The data layout, the dispatch split, the index-pairing, and the `[2,d0,d1]` shape are **directly attested by assert strings**. The comparator network and the exact merge-tree fanout are **inferred from structure**; they are tagged accordingly. No Xtensa instruction sequence is presented here because none was recovered. (Backing analysis: **D-Q04**.)

For reimplementation, the contract is:

- The **packed `(value, index)` data model** and the `[2, data_dim0, data_dim1]` output tensor.
- The **four-way dispatch** (`sort_singlecore` / `sort_multicore` / `partial_sort_multicore` / `partial_merge_multicore`) and the condition that selects each.
- The **per-core bitonic sort** alignment contract (`len % (SIMD_WIDTH*2) == 0`) and the **cross-core pairwise merge** (`num_subtensors <= cpu_count`).

| | |
|---|---|
| **Image** | `neuronxcc/data/custom_op/libbuiltincustomop_cpu{0..7}.stripped.so` (8 files) |
| **Alias** | `neuronxcc/l/l_cpu{0..7}.stripped.so` (byte-identical copy) |
| **Format / ISA** | ELF32 `EXEC`, Tensilica **Xtensa**, fully stripped |
| **Size each** | 579,380 bytes; `.text` 0x737d8, `.rodata` 0x5a48 |
| **Per-core base** | cpu0 `.text`@`0x84000000` … cpu7@`0x84e00000` (0xe00000 stride) |
| **Source TUs** | `bitonic_sort.cpp`, `sort_and_merge.cpp` |
| **Dispatch names** | `.rodata` 0x7ed20–0x7ed6e (four contiguous literals) |
| **Output shape** | `[2, data_dim0, data_dim1]` (asserted three times) |
| **Runtime** | PyTorch custom op (`c10`), `SundaCustomOpLibrary` arg-parser |

---

## The Eight-Core Image

### Purpose

GPSIMD is an eight-core Xtensa cluster (see [GPSIMD / Xtensa layout](gpsimd-xtensa-layout.md)). The sort builtin is compiled once and **relinked eight times**, once per core, to a fixed per-core code window. There is no runtime relocation: each core loads the image already located at its own base.

### Evidence

```text
readelf -h  libbuiltincustomop_cpu0:  Entry 0x8400cd94   .text @ 0x84000000
readelf -h  libbuiltincustomop_cpu7:  Entry 0x84e0cd94   .text @ 0x84e00000
```

The entry points and section bases differ by exactly `cpu_index * 0xe00000` (14 MiB per core). The eight files therefore have **different md5s** despite identical 579,380-byte sizes — the divergence is in absolute addresses (and a small block of relocated `.data` constants), not in logic. Diffing the *string sets* of cpu0 vs. cpu7 yields only address-bearing noise; every `bitonic_sort.cpp` / `sort_and_merge.cpp` assert string is present and identical in all eight.

> **QUIRK — the same op is shipped twice.** `neuronxcc/l/l_cpu0.stripped.so` and `neuronxcc/data/custom_op/libbuiltincustomop_cpu0.stripped.so` are byte-identical (same md5). The `l/` path is an aliased shipping copy; a reimplementer needs to provide only one eight-image set, not two. (CONFIRMED — md5 match.)

> **NOTE —** because the image is stripped of symbols, the four `*_compute` names below are **`.rodata` string literals** (used by the host dispatcher and by the asserts), not exported ELF symbols. They occupy a contiguous run at 0x7ed20; the host selects a kernel by name, not by symbol address.

---

## Data Model — The Index-Paired Comparator

### Purpose

A sort that must also return *where* each value came from cannot move values alone; it must carry the original index alongside each value through every compare-and-swap. The builtin does this with **two parallel buffers** at every stage — a data buffer and an index buffer — and the assert strings confirm they are allocated, checked, and written as a unit.

### Evidence

Every buffer assertion names the data buffer and its index buffer together, never separately:

```text
bitonic_sort.cpp:360   left_data_buffer   != nullptr && left_indices_buffer   != nullptr
bitonic_sort.cpp:392   right_data_buffer  != nullptr && right_indices_buffer  != nullptr
bitonic_sort.cpp:425   merged_data_buffer != nullptr && merged_indices_buffer != nullptr
bitonic_sort.cpp:523   data_buffer        != nullptr && indices_buffer_i32    != nullptr
sort_and_merge.cpp:98  left_data_buffer   != nullptr && left_indices_buffer   != nullptr
sort_and_merge.cpp:122 right_data_buffer  != nullptr && right_indices_buffer  != nullptr
sort_and_merge.cpp:147 merged_data_buffer != nullptr && merged_indices_buffer != nullptr
```

The `indices_buffer_i32` name (line 523) fixes the index type: **32-bit signed integers**. The host-side argument contract (`utypes.hpp`) cross-checks a torch dtype against the Neuron ISA dtype (`aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)`); the value dtypes the runtime carries strings for are `float`, `at::BFloat16`, and `int64_t`.

### Algorithm (structural)

The comparator is a **keyed compare-exchange**: compare on the value, swap the pair. The Xtensa body is not recovered, so this is the structural shape the buffer-pairing attests, not a transcription:

```c
// INFERRED from the paired (data,index) buffers — bitonic_sort.cpp
// Compare-exchange of two SIMD lanes a,b under a sort direction `dir`.
// SIMD_WIDTH lanes processed at once (see len % (SIMD_WIDTH*2)==0 alignment).
function compare_exchange(value[a], value[b], index[a], index[b], dir):
    swap = (value[a] > value[b]) == dir            // ascending vs. descending
    if swap:
        exchange(value[a], value[b])               // move the key ...
        exchange(index[a], index[b])               // ... and its origin together
```

> **GOTCHA — the index is not a post-hoc argsort.** A reimplementation that sorts values first and then tries to recover indices by a second pass will mis-handle ties and waste a full pass. The original carries the index in lockstep through the *same* comparator network; ties resolve to whatever the network's stable ordering produces, which an index-rebuilding pass cannot reproduce. The two buffers must be swapped by the *same* predicate, in the same step. (INFERRED — from the always-paired buffer asserts; the swap predicate itself is not recovered.)

---

## Output Layout — The `[2, d0, d1]` Packed Tensor

### Purpose

Values and indices leave the op as **one tensor** with a leading size-2 plane axis, not as two separate outputs. This is the single most-asserted fact in the image.

### Evidence

Three independent code paths (lines 365–367, 394–396, 444–446 of `sort_and_merge.cpp` — one per multi-core variant) each assert the same three-part shape on the output tensor `t_out`:

```text
sort_and_merge.cpp:365   t_out.size(0) == 2           // plane axis: [values | indices]
sort_and_merge.cpp:366   t_out.size(1) == data_dim0
sort_and_merge.cpp:367   t_out.size(2) == data_dim1
```

The internal merge buffers carry the same `dim0 == 2` invariant:

```text
sort_and_merge.cpp:608   dim0 == 2                    // merge operates on a [2, ...] view
sort_and_merge.cpp:618   right_start < dim2           // partition split along the last axis
```

### Layout

```text
t_out : shape [2, data_dim0, data_dim1]
        ┌──────────────── plane 0 (size(0) index 0) ────────────────┐
        │  sorted VALUES   : [data_dim0 rows] × [data_dim1 cols]     │
        ├──────────────── plane 1 (size(0) index 1) ────────────────┤
        │  gathered INDICES: i32, same [data_dim0 × data_dim1]       │
        └────────────────────────────────────────────────────────────┘
   data_dim0 = independent sort lanes (rows sorted in parallel)
   data_dim1 = the sorted axis length (per-row sort / merge dimension)
```

`data_dim0` is the batch of independent rows sorted side by side; `data_dim1` is the length of each sorted sequence and the axis the bitonic network and the cross-core merge operate over. The `right_start < dim2` assert (line 618) shows the cross-core partition is taken along the **last axis** (`dim2`), i.e. each core owns a contiguous slice of the `data_dim1` sequence.

> **NOTE —** a top-k caller reads the first `k` columns of plane 0 (the k largest/smallest values) and the corresponding first `k` columns of plane 1 (their original positions). The packed `[2,…]` shape is what lets the legalizer treat one op as both the values-output and the indices-output of an HLO `TopK`. See [TopK Legalization](../hlo-opt/topk-legalize.md).

---

## Dispatch — Four Named Compute Kernels

### Purpose

The host selects one of four kernels by name. The four names live as a contiguous `.rodata` run and split the work along two orthogonal axes: **core count** (one core vs. all eight) and **completeness** (full sort vs. `partial_*` = top-k).

### Evidence

```text
.rodata 0x7ed20  sort_singlecore_compute
.rodata 0x7ed38  sort_multicore_compute
.rodata 0x7ed4f  partial_sort_multicore_compute
.rodata 0x7ed6e  partial_merge_multicore_compute
```

These are the only four strings in the image ending in `compute` (a `strings | rg 'compute$'` over the image returns exactly this set, plus the unrelated `safe_compute_numel` / `compute_contiguous` c10 helpers).

### The dispatch matrix

| Kernel | Cores | Sort? | Role (INFERRED from name + asserts) | Confidence |
|---|---|---|---|---|
| `sort_singlecore_compute` | 1 | full | Whole sequence fits one core: bitonic-sort in place, no cross-core merge | STRONG |
| `sort_multicore_compute` | 8 | full | Per-core bitonic sort of a slice, then full pairwise tree merge to one sorted output | STRONG |
| `partial_sort_multicore_compute` | 8 | top-k | Per-core sort that keeps only the top-k of each slice (the `partial_*` prefix = top-k) | INFERRED |
| `partial_merge_multicore_compute` | 8 | top-k | Merge of the per-core partial results, retaining only the global top-k | INFERRED |

> **QUIRK — `partial_*` means top-k, not "incomplete".** The `partial_sort` / `partial_merge` pair is the top-k path: it is the standard C++ `std::partial_sort` vocabulary — sort *enough* to know the first `k` elements, then stop. The two `partial_*` kernels mirror the two-phase structure of the full sort (`sort` then implicit merge) but prune to `k` at each phase so the merge moves `k` elements per core instead of the whole slice. (INFERRED — the naming is the only evidence; the value of `k` and the pruning point are not recovered from strings.)

> **GOTCHA — there is no `singlecore` top-k kernel.** Only `sort_singlecore_compute` exists for the one-core path; the `partial_*` kernels are both `multicore`. A reimplementer should route a small single-core top-k either through `sort_singlecore` + a host-side slice, or replicate the partial logic single-core; the shipped image does *not* provide a `partial_sort_singlecore_compute`. (CONFIRMED — no such string exists.)

---

## Per-Core Bitonic Sort

### Purpose

Within one core, the sequence is sorted by a bitonic network over the Xtensa SIMD lanes. Bitonic sort is the natural choice on SIMD: it is **data-oblivious** — the compare-exchange schedule is fixed regardless of input, so there are no data-dependent branches to stall a vector pipeline.

### Evidence — the alignment contract

```text
bitonic_sort.cpp:302   len % (SIMD_WIDTH*2) == 0     // sequence length is a multiple of 2·SIMD_WIDTH
bitonic_sort.cpp:224   merge_vec_size >= 2           // merge stage needs at least 2 vectors
bitonic_sort.cpp:602   buffer != nullptr
```

`len % (SIMD_WIDTH*2) == 0` is the bitonic precondition surfacing as an assert: the network processes lanes in pairs, so the working length must be a multiple of twice the SIMD width. `merge_vec_size >= 2` guards the within-core bitonic *merge* stage (the final stage of a bitonic sort merges two oppositely-ordered halves).

### Algorithm (structural)

```c
// INFERRED bitonic sort over SIMD lanes — bitonic_sort.cpp
// `len` is the padded sequence length; the asserts fix the alignment, not the loop body.
function bitonic_sort_core(value[], index[], len, dir):
    assert(len % (SIMD_WIDTH * 2) == 0)              // line 302
    // Build bitonic sequences of growing size, then merge each down.
    for k = 2; k <= len; k *= 2:                     // bitonic stage
        for j = k/2; j > 0; j /= 2:                  // bitonic merge sub-stage
            for each lane pair (a, b) at distance j:
                d = (a & k) == 0 ? dir : !dir         // alternating direction
                compare_exchange(value, index, a, b, d)   // keyed swap (see Data Model)
```

> **NOTE —** the loop bounds above are the textbook bitonic schedule, shown to make the alignment assert (`len % (2·SIMD_WIDTH)`) and the `merge_vec_size >= 2` guard legible. The actual Xtensa lane scheduling is not recovered. Treat the `O(n log² n)` comparator count as the cost model, not a traced fact.

---

## Cross-Core Merge — The Pairwise Tree

### Purpose

After each of up to eight cores sorts its own slice, the eight sorted subtensors must be merged into one globally sorted sequence. The merge is **pairwise**: two sorted runs combine into one, repeatedly, until a single run remains.

### Evidence

```text
sort_and_merge.cpp:598   num_subtensors <= cpu_count          // ≤ 8 sorted runs to merge
bitonic_sort.cpp:431     merged_subtensor_write_idx < merged_size   // bounded merge write
bitonic_sort.cpp:425     merged_data_buffer != nullptr && merged_indices_buffer != nullptr
sort_and_merge.cpp:147   merged_data_buffer != nullptr && merged_indices_buffer != nullptr
sort_and_merge.cpp:618   right_start < dim2                   // left/right partition along last axis
```

`num_subtensors <= cpu_count` ties the number of sorted runs to the eight-core count: at most one sorted subtensor per core. The presence of a single `merged_*` buffer pair (data + indices), fed by a `left_*` and a `right_*` pair (lines 98/122 and 360/392), is the signature of a **binary merge** — two inputs, one output.

### The merge tree (structure CONFIRMED, fanout INFERRED)

```text
core:   0    1    2    3    4    5    6    7        ← up to 8 sorted subtensors
         \  /      \  /      \  /      \  /          (num_subtensors <= cpu_count)
level1:  m01      m23      m45      m67             ← 4 pairwise merges  (left+right→merged)
           \      /          \      /
level2:     m0123              m4567               ← 2 pairwise merges
                \              /
level3:          m01234567                          ← 1 final merge → sorted output
```

> **CORRECTION (D-Q04) — the "8→4→2→1" fanout is inferred, not asserted.** What the strings *directly* attest is (a) up to eight sorted subtensors (`num_subtensors <= cpu_count`), (b) a binary `left + right → merged` merge primitive (the paired buffer triples), and (c) a bounded merge write (`merged_subtensor_write_idx < merged_size`). The specific three-level **8→4→2→1** halving tree is the standard way to combine 8 sorted runs with a binary merge and is shown for the reimplementer; the image does **not** contain a level/round counter string proving exactly this schedule. A serial "merge run *i* into the accumulator" loop would satisfy the same asserts. Tag the fanout **INFERRED**; tag the binary-merge primitive and the ≤8-run bound **CONFIRMED**.

### Algorithm (structural)

```c
// Cross-core merge — sort_and_merge.cpp + bitonic_sort.cpp
function multicore_merge(subtensors[n], n):
    assert(n <= cpu_count)                           // line 598: ≤ 8 runs
    runs = subtensors
    while len(runs) > 1:                             // pairwise tree (INFERRED fanout)
        next = []
        for (left, right) in pairs(runs):
            // left/right each = (data, index) pair; output one merged pair
            merged = merge_two_sorted(left, right)   // keyed binary merge of (value,index)
            assert(merged.write_idx < merged.size)   // bitonic_sort.cpp:431
            next.append(merged)
        runs = next
    return runs[0]                                   // single globally-sorted [2, d0, d1] tensor
```

For the `partial_merge_multicore_compute` (top-k) variant, the same tree runs but each `merge_two_sorted` keeps only the top `k` of its output, so the final run is already trimmed to the global top-k. (INFERRED — from the `partial_` prefix mirroring the full path.)

---

## Host Argument Contract

### Purpose

The builtin is a PyTorch custom op; the host marshals tensor arguments through the `SundaCustomOpLibrary` arg-parser before any sort runs. These checks are what a reimplemented host dispatcher must reproduce.

### Evidence

```text
.../library/arg_extractor.hpp:16  num_args < max_args_
.../library/arg_parser.hpp:66     curr_arg_ < isa_args.get_num()
.../library/arg_parser.hpp:67     isa_args.get_types()[curr_arg_] == NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR
.../library/utypes.hpp:65         aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)
.../library/utypes.hpp:94         t_.framework_shape_type != ..._OUT_OF_LINE_SHAPE
.../library/utypes.hpp:139        num_dim != 0  &&  "...tensors cannot have 0 dimensions - must be of at least rank 1"
```

Each argument is fetched positionally (`curr_arg_ < get_num()`), required to be a **tensor** (`NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR`), dtype-checked against the Neuron ISA's expected scalar type, required to have an **in-line** (statically-known) shape, and required to be at least rank 1. The output tensor is then the rank-3 `[2, data_dim0, data_dim1]` checked above.

> **NOTE — the build provenance is visible in the strings.** Assert paths carry `/opt/workspace/SundaCustomOpLibrary/custom_op/library/...` (the op framework) and `/workplace/auderian/custom-ops/dana-libc-cr/.../KaenaPytorchCustomOps/pytorch_source/c10/...` (a trimmed PyTorch `c10`). The sort is therefore a `torch`-dispatched custom op compiled for Xtensa, not a hand-written ISA kernel — which is why a full `c10::TensorImpl` / `DispatchKeySet` machinery rides along in the 579 KB image.

---

## Related Components

| Component | Relationship |
|---|---|
| GPSIMD / Xtensa cluster | Host engine: eight cores, one relinked image per core |
| HLO `TopK` legalize | Lowers a framework `TopK` toward this builtin; consumes the `[2,d0,d1]` packed result |
| NKI top-k primitives | The NKI-level top-k surface that can target the same GPSIMD path |
| `SundaCustomOpLibrary` arg-parser | Host marshalling of tensor args before the sort runs |

## Cross-References

- [GPSIMD / Xtensa Layout](gpsimd-xtensa-layout.md) — the eight-core Xtensa cluster this builtin runs on (Part 11.1, sibling)
- [TopK Legalization](../hlo-opt/topk-legalize.md) — host-side legalization of `TopK` that targets this builtin (§4.26)
- [Scan / Reduce / Top-K Primitives](../nki/scan-reduce-topk.md) — the NKI top-k surface (§6.7.12)
- [GPSIMD Engine](../arch/gpsimd-engine.md) — architectural overview of the GPSIMD engine
