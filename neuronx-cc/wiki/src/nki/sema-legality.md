# NKI sema Legality-Assert Engine

> *All symbols, offsets, and `sema.py:NNNN` line numbers on this page apply to `neuronx_cc 2.24.5133.0+58f8de22`, module `neuronxcc/nki/compiler/backends/neuron/sema.cpython-310-x86_64-linux-gnu.so` (the cp310 wheel; cp311/cp312 differ only in the Python-ABI tag). Hardware constants are cross-checked against the shipped type stubs and [1.05 SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md).*

## Abstract

`sema.so` is the **semantic-legality layer** of the NKI front-end: the gate every traced NKI operation passes through before it is lowered to `penguin.ir`. When a kernel author writes `nisa.nc_matmul(stationary, moving)` or `nl.load(...)`, the op driver does not trust the tile shapes, dtypes, or memory placements — it calls a battery of `assert_*` / `check_*` predicates that re-derive each invariant against the *runtime hardware target* and raise a structured NKI semantic error the moment one fails. This is the layer that turns "256 partitions on a 128-partition machine" from a silent miscompile into a diagnostic. It is the NKI analogue of LLVM's `Verifier`, except that it runs *during tracing* (on Python objects, not on already-built IR) and its limits are read off the target object rather than baked into the checker.

Five invariant families cover the legal-program space. **Partition** asserts enforce the `par_dim <= 128` rule (the systolic array and SBUF have 128 lanes). **Free-dim** asserts enforce the per-partition byte budget — how many bytes of a tile fit in one SBUF partition or across the PSUM banks. **Dtype** asserts enforce that a tile's element type is a member of an instruction's supported set, with the special-case that a PSUM tile must be `float32` or `int32` (the accumulator's only two formats). **Shape** asserts enforce tile `(partition, free)` compatibility, leading-dim-is-partition, dimension counts, and the dense matmul-cluster constraints. **Addr-space** asserts gate each operand to SBUF / PSUM / DRAM(HBM) by IR-class subclass membership. Every one of the five families, on failure, funnels into a single raise primitive — `nki_assert(cond, msg)` — whose `msg` is built by the `err_*()` template layer (owned by the [6.4.5 diagnostic catalog](diagnostic-catalog.md)).

The defining architectural fact: **`sema.so` is arch-parametric**. The numbers `128`, `512`, `192 KiB`, `2 KiB` are *not* literals inside the checker. Each assert reads a named attribute off the `target` object (`statebuf_num_partitions`, `psum_par_size_in_bytes`, `psum_num_banks`, …) and compares against that. The same `sema.so` checks an Inferentia kernel and a Trainium-gen4 kernel; only the target attrs differ. The numeric limits cited on this page come from the shipped stubs and the geometry page, not from constants in `sema.so`.

> **CORRECTION (D-W04 grounding) —** the backing report cites every `sema.so` function at a decompiled offset (e.g. `assert_par_dim_sbuf @ 0x57250`, `sema.py:2716`). Those offsets are **not re-verifiable in this snapshot**: the IDA sidecar for `sema.cpython-310-…-.so` is absent — its entire decompiled-bodies list is empty, and the module appears only under `ida/missing_addresses/…sema…__missing_decompiled_files.txt` (0 lines). Like `neuron_isa.so` (a sibling found the same gap), `sema.so` ships no decompile/disasm/context tree here. **Every `sema.py:NNNN` line, `sym #N`, and `0x…` offset on this page is carried at the report's confidence and could not be independently confirmed against the binary.** What *is* re-grounded here as primary binary-distributed evidence is the **rule content** — the partition/free/dtype/shape/addr-space invariants and the matmul/reduce limits — re-derived from the shipped `neuronxcc-stubs/nki/isa/__init__.pyi`, `sbuf.pyi`, `psum.pyi` docstrings (which are generated from the same source the asserts enforce) and from the geometry page. Rule grades below reflect *that* corroboration, not the offset.

For reimplementation, the contract is:

- The **five invariant families** and which op-operand each gates — partition, free-dim byte budget, dtype (incl. the PSUM `{fp32,int32}` rule), shape/matmul-cluster, addr-space.
- The **`target` attribute contract** — the exact set of fields each assert reads off the hardware-target object, so the limits stay arch-parametric.
- The **single `nki_assert` sink** — every family's failure path converges on one raise primitive; the `err_*()` builders only construct the message string.
- The **RichCompare polarity** of each numeric guard (`Py_GT` for "exceeds", `Py_LT` for "doesn't fit"), so a reimplementer raises on the correct side of the comparison.

| | |
|---|---|
| **Module** | `neuronxcc/nki/compiler/backends/neuron/sema.cpython-310-x86_64-linux-gnu.so` (~6 MB, ~751 bodies, ~220 syms) |
| **Source (Cython-compiled)** | `sema.py` |
| **Single raise sink** | `nki_assert(cond, msg)` — `sym #23 @ 0xbb9a0` *(offset unverified — see CORRECTION)* |
| **Invariant families** | partition `≤128` · free-dim byte budget · dtype (PSUM=`{fp32,int32}`) · shape/matmul · addr-space |
| **Limit source** | runtime `target` attrs (arch-parametric); numeric values from stubs + [1.05](../arch/sbuf-psum-geometry.md) |
| **Failure messages** | built by `err_*()` template layer → [6.4.5 diagnostic catalog](diagnostic-catalog.md) |
| **Evidence grade** | rules CONFIRMED via stubs; symbol offsets carried at report grade (sidecar absent) |

---

## The Assert Funnel

### Purpose

Every legality predicate in `sema.so` is a thin wrapper that computes one boolean and, on the bad branch, calls **one** shared raise primitive. There is no per-family exception type, no scattered `raise AssertionError`. This single-sink design is what lets the `err_*()` template layer own *all* message formatting while the assert layer owns *only* the decision logic — the split the backing report draws between D-W04 (this page) and D-W05 (the catalog).

### Entry Point

```text
op driver (NKIFunc.check_*)              ── per-op, one call per operand
  ├─ assert_par_dim_sbuf / _psum         ── partition family
  ├─ assert_free_dim_sbuf / _psum        ── free-dim byte-budget family
  ├─ assert_dtype / _in / _psum          ── dtype family
  ├─ assert_same_shape / _tile_shapes …  ── shape family
  └─ assert_tensor_in_valid_addr_spaces  ── addr-space family
        └─ on failure: err_*(...)        ── build msg string  (→ 6.4.5)
              └─ nki_assert(False, msg)  ── THE SINK: raise   (sym #23 @ 0xbb9a0)
```

### Algorithm

```c
function nki_assert(cond, msg):          // sym #23 @ 0xbb9a0  (offset unverified)
    if cond:                             // legal — fall through, op proceeds to lowering
        return None
    raise NKISemanticError(msg)          // the single raise; msg pre-built by err_*()
```

Every `assert_*` below has the same skeleton: read an attribute off the operand (`tile.dtype`, `tensor.tensor_ir_class`, `shape`), read the matching capacity attribute off `target`, `PyObject_RichCompare` the two, and on the failing polarity build a kwargs dict and dispatch to the `err_*()` builder that calls `nki_assert(False, …)`. The legal path always returns `None`.

> **NOTE —** the single-sink claim is the structural backbone of this page and the cleanest thing to verify by reimplementation: a checker that scatters its own `raise` statements per family cannot share one message-template layer the way `err_*()` → `nki_assert` does. The split is deliberate — the catalog page documents *what each message says*; this page documents *which boolean decides it gets said*.

---

## Family 1 — Partition Dimension (the ≤128 rule)

### Purpose

A NKI tile's **leading (partition) dimension** maps to physical hardware lanes: the 128 partitions of SBUF/PSUM and the 128 rows of the PE systolic array. A tile cannot have more partitions than the machine has lanes. This is the single most-invoked invariant — every tile-producing op checks it.

### Algorithm

```c
function assert_par_dim_sbuf(par_dim, target):   // sema.py:2716  sym #177 @ 0x57250  (offsets unverified)
    limit = target.statebuf_num_partitions       // arch attr — 128 on gen1..gen4 (geometry 1.05)
    if RichCompare(par_dim, limit, Py_GT):        // disasm edx=4 (Py_GT): par_dim > limit
        err_num_partition_exceed_arch_limit(par_dim, limit, ...)   // → nki_assert(False, msg)
    return None                                   // legal

function assert_par_dim_psum(par_dim, target):   // sema.py:2710  sym #175 @ 0x57bd0
    limit = target.psum_num_partitions            // arch attr — 128
    if RichCompare(par_dim, limit, Py_GT):
        err_num_partition_exceed_arch_limit(par_dim, limit, ...)
    return None

function assert_num_partition(par_dim, shapes, max_p, api_name):  // sema.py:2877  sym #205 @ 0x96c80
    // op-facing generic form: caller passes the arch's 128 as max_p explicitly
    if RichCompare(par_dim, max_p, Py_GT):
        err_num_partition_exceed_arch_limit(par_dim, shapes, max_p, api_name)
    return None
```

`assert_par_dim_sbuf` and `assert_par_dim_psum` are buffer-specialised forms reading `statebuf_num_partitions` / `psum_num_partitions`; `assert_num_partition` is the generic op-facing wrapper that takes the limit as an argument. All three raise on the **`Py_GT`** branch (`par_dim` strictly greater than the limit). The diagnostic is the familiar `"number of partitions 256 exceed architecture limitation of 128."` for a `[256, 1024]` tile on SBUF.

### Limit Source

| Limit | `target` attribute | Value | Source / Confidence |
|---|---|---|---|
| SBUF partition count | `statebuf_num_partitions` | 128 (all gens) | `Statebuf+0x8 numPartitions`, [1.05](../arch/sbuf-psum-geometry.md) — CONFIRMED |
| PSUM partition count | `psum_num_partitions` | 128 (64 on gen1 Inferentia) | `Psumbuf+0x4`, [1.05](../arch/sbuf-psum-geometry.md) — CONFIRMED |
| matmul contraction | (caller-supplied `max_p`) | `≤128` | `nc_matmul` stub: partition axes "identical and `<=128`" — CONFIRMED |

> **QUIRK —** the partition limit is `statebuf_num_partitions`, *not* a constant `128`. On gen1 Inferentia the PSUM half (`psum_num_partitions`) is **64** while SBUF stays 128 ([1.05](../arch/sbuf-psum-geometry.md)). A reimplementer who hard-codes 128 into the partition check will let an over-wide PSUM tile through on Inferentia. The stub `nc_matmul` docstring confirms the matmul side independently: *"the partition axis sizes of the `stationary` and `moving` tiles must be identical and `<=128`, which corresponds to the contraction dimension."*

---

## Family 2 — Free Dimension (per-partition byte budget)

### Purpose

After the partition count, the second physical constraint is how many *bytes* a tile occupies in one partition. SBUF gives each partition a flat byte budget; PSUM gives each partition a set of fixed 2 KiB banks. The free-dim asserts compute `required_bytes = prod(free_shape) * sizeof(dtype)` and compare it against the target's per-partition capacity.

### Algorithm

```c
function assert_free_dim_sbuf(free_shape, dtype, target):   // sema.py:~2730  sym #181 @ 0x797a0
    required = n_elts(free_shape) * dtype.sizeinbytes        // PyNumber_Multiply
    capacity = target.statebuf_par_size_in_bytes             // SBUF bytes per partition
    if RichCompare(capacity, required, Py_LT):               // disasm edx=1 (Py_LT): capacity < required
        nki_assert(False, "Size of free dimensions <N> KB per partition on SBUF buffer.")
    return None

function assert_free_dim_psum(free_shape, dtype, target):   // sema.py:~2700  sym #179 @ 0x889c0
    required = n_elts(free_shape) * dtype.sizeinbytes
    capacity = target.psum_par_size_in_bytes * target.psum_num_banks   // all-banks total
    if RichCompare(capacity, required, Py_LT):
        nki_assert(False, "... on PSUM buffer.")             // err_stack_overflow_psum path:
                                                             //   "stack overflow: required psum banks <N>"
    return None
```

Both raise on **`Py_LT`** — note the operand order is `(capacity, required)`, so the failing condition reads "capacity is less than what the tile needs". The SBUF check is against the flat per-partition budget; the PSUM check is against the *all-banks* total (`psum_par_size_in_bytes * psum_num_banks`).

### Limit Source

| Buffer | `target` attribute(s) | Value (gen2 ref) | Source / Confidence |
|---|---|---|---|
| SBUF per-partition | `statebuf_par_size_in_bytes` | 192 KiB physical, 16 KiB compiler-reserved → `[0, 176 KiB)` usable | `sbuf.pyi`: *"0 … to 192KiB-16KiB"* — CONFIRMED (gen3/4 are 224/256 KiB per [1.05](../arch/sbuf-psum-geometry.md)) |
| PSUM per-partition | `psum_par_size_in_bytes` × `psum_num_banks` | 2 KiB/bank × 8 banks (gen2+) | `psum.pyi`: *"fdim_size cannot exceed 2KiB … size of a single PSUM bank"*; banks from [1.05](../arch/sbuf-psum-geometry.md) — CONFIRMED |

> **GOTCHA —** `assert_free_dim_psum` checks the **all-banks total**, but a single *physical* PSUM tile may not span banks at all: the `psum.pyi` stub states *"a physical PSUM tile cannot span multiple PSUM banks"* (`fdim_size <= 2 KiB`). That finer per-bank rule is enforced in the **allocator** ([Part 8 walrus](../walrus/)), not in `sema`. A reimplementer who folds the two checks into one will either reject legal multi-bank tensors (if they apply the 2 KiB cap in sema) or accept an illegal bank-spanning physical tile (if they apply only the total in the allocator). The two limits live in two layers on purpose.

### Element-count companions

Two asserts pin exact per-partition *element* counts (not byte budget), used by ops with fixed lane structure (mx packing, transpose, stream-shuffle):

```c
function assert_elements_per_partition(elements_per_partition, min_size, max_size):  // sema.py:2801  sym #191 @ 0xc6c90
    // "Check that the number of elements per partition is between min and max." (docstring, CONFIRMED)
    if elements_per_partition < min_size or elements_per_partition > max_size:
        nki_assert(False, "<N> elements per partition, but must have exactly <M>")   // exact-match path

function assert_exact_elements(...):    // sym #197 @ 0x682e0   (STRONG)
    // companion: asserts an access dimension has an exact element count
    // string " elements per partition, but must have exactly "
```

---

## Family 3 — Dtype (membership + the PSUM accumulator rule)

### Purpose

Each NKI instruction supports a fixed set of element types; a tile's dtype must be a member. Dtype membership is a **subtype** check (`np.issubdtype`), not exact equality, so `int16` satisfies a `signedinteger` requirement. The headline special case: a tile bound to a PSUM bank may only be `float32` or `int32` — the two formats the PE-array accumulator drains.

### Algorithm

```c
function assert_dtype(tile, type, name):              // sema.py:2694  sym #169 @ 0x59070
    if not np.issubdtype(tile.dtype, type):           // SUBTYPE membership, not ==
        nki_assert(False, "'<name>' is expected to have a dtype of 'np.<type>'...")
    return None

function assert_dtype_in(dtype, supported_dtypes, name):   // sema.py:2853  sym #199 @ 0x51a50
    if not PySequence_Contains(supported_dtypes, dtype):   // membership in the per-op set
        nki_assert(False, "'<name>' has dtype '<dtype>' ...")
    return None

function assert_dtype_psum(dtype, target):            // sema.py:2749  sym #185 @ 0x55940
    if dtype == target.float32:   return None         // PSUM dtype #1
    if dtype == target.int32:     return None         // PSUM dtype #2
    nki_assert(False, ...)                            // anything else illegal on PSUM
```

`assert_dtype` is a single-type subtype gate; `assert_dtype_in` is the membership gate against an op's supported-dtype set (the set is produced by the `err_instruction_supported_dtypes` / `expected_dtype_str` helpers, message-side). `assert_dtype_psum` is the hardcoded two-way `{float32, int32}` gate, resolving the two type objects via `target.float32` / `target.int32`.

### Limit Source

| Rule | Mechanism | Value | Source / Confidence |
|---|---|---|---|
| single-type | `np.issubdtype(tile.dtype, type)` | subtype, not `==` | report decompile — STRONG (offset unverified) |
| per-op set | `dtype ∈ supported_dtypes` | op-specific | `nki-dtype` stub tables — CONFIRMED |
| PSUM dtype | `dtype ∈ {float32, int32}` | exactly two | `assert_dtype_psum`; PSUM = PE-array accumulator — CONFIRMED (matmul output to PSUM, stubs) |
| gen3+ mm dtype | `assert_gen3_or_newer_mm_dtype` (`sym #189 @ 0x54460`) | branches on `target` generation | STRONG — gen3-only matmul operand dtypes |

> **NOTE —** the PSUM `{float32, int32}` rule is the dtype counterpart of the PSUM partition/byte rules: PSUM *is* the matmul accumulator, and a systolic-array accumulation lands in fp32 (for float inputs) or int32 (for integer inputs). The `nc_matmul` stub confirms matmul "write[s] outputs to PSUM"; `assert_dtype_psum` is what rejects, e.g., a `bfloat16` PSUM tile at trace time. Supporting display helpers: `dtype2str` (`sym #7 @ 0xa83b0`) renders a dtype as its `"np.<name>"` string; `expected_dtype_str` (`sym #15 @ 0xacca0`) builds the `"expected dtype of {…}"` fragment from a set.

---

## Family 4 — Address Space (SBUF / PSUM / DRAM gating)

### Purpose

Every NKI tensor carries a `tensor_ir_class` describing its physical memory (SBUF, PSUM, or HBM/DRAM). Each operand of each op has an *allowed* set of address spaces — `nl.load` source must be HBM and destination SBUF; a compute op's operands must be SBUF or PSUM; an indirect-DMA index tile must be SBUF. Legality is tested by **IR-class subclass membership**, not a string compare.

### Algorithm

```c
function assert_tensor_in_valid_addr_spaces(tensor, valid_addr_spaces, api_name):
                                                      // sema.py:2705  sym #173 @ 0xcd3d0 (+ genexpr sym #34 @ 0x3dca0)
    ok = any(cls.is_superclass(tensor.tensor_ir_class)   // subclass membership, per allowed space
             for cls in valid_addr_spaces)               // generator (scope struct #29)
    if not ok:
        err_unsupported_memory(...)                   // → nki_assert(False, msg)
    return None

function check_tensor_addr_space(self, tensor, addr_space, name):   // NKIFunc method, sym #72 @ 0xd2c60
    // per-operand wrapper: one call per tensor parameter of an op
    if not addr_space.is_superclass(tensor.tensor_ir_class):
        err_unsupported_memory(self.params_map[name], self.cur_api_name, ...)
    return None
```

The check is `addr_space_cls.is_superclass(tensor.tensor_ir_class)` — the allowed space's IR class must be a superclass of the tensor's. `assert_tensor_in_valid_addr_spaces` tests the *any-of* set form; `check_tensor_addr_space` is the per-operand wrapper an op invokes once per tensor parameter, resolving the operand's API name via `self.params_map`/`cur_api_name` for the message.

### Related addr-space guards (same family, message-side → 6.4.5)

| Guard | Rule |
|---|---|
| `err_hbm_tensor_with_init_value_not_supported` | an HBM/DRAM tensor may not carry an initial value (init values are SBUF/PSUM-only) |
| `err_shared_hbm_must_in_kernel_level` | shared-HBM tensors must be declared at kernel scope, not inside a block |
| `err_indirect_indices_sbuf` | indirect-DMA index tile must reside in SBUF — *"Indirect indices tile must be on SBUF."* |

The failure message template is `"Expected operand '<x>' of '<op>' to be in address space 'psum|sbuf' [or 'hbm'], but got a tile in '<actual>' instead."`

> **QUIRK —** address-space legality is **subclass membership**, not equality. `is_superclass(tensor.tensor_ir_class)` lets an op declare it accepts a *base* memory class and admit any IR subclass of it — the reason the allowed-set is a list of classes, not a list of enum tags. A reimplementer modeling addr-space as a flat enum compare will reject legal subclassed tiles.

---

## Family 5 — Shape Compatibility & the Matmul Cluster

### Purpose

The shape family enforces tile `(partition, free)` compatibility between operands, the leading-dim-is-partition rule for locally-declared tensors, dimension-count bounds, and the dense systolic-array constraints of `nc_matmul` / `nc_matmul_mx`. It is the largest cluster — the matmul checks alone are the densest legality logic in the module.

### Algorithm — generic shape gates

```c
function assert_same_shape(arg1, arg2):           // sema.py:2888  sym #209 @ 0xe6bb0
    if RichCompare(arg1.shape, arg2.shape) != equal:
        nki_assert(False, "<arg1> has mismatched shapes ... shape <s1> ... <s2>")

function assert_shape_valid(shape, name):         // sema.py:2882  sym #207 @ 0x4fb20
    if not valid(shape):                          // rejects non-positive / malformed dims
        nki_assert(False, "<name> has invalid shape ...")

function assert_max_dimensions(shape, max_dims, name):   // sema.py:2817  sym #193 @ 0x53670
    ndims = PyObject_Size(shape)                  // = len(shape)
    if RichCompare(ndims, max_dims, Py_GT):       // e.g. transpose max_dims = 2
        nki_assert(False, "... dimensions. Maximum allowed dimension ...")   // sema.py:2825

function assert_min_dimensions(shape, min_dims, name):   // sym #195 @ 0x52880
    if PyObject_Size(shape) < min_dims:
        nki_assert(False, ...)                    // symmetric lower bound

function assert_local_tensor_shapes(...):         // sema.py:2760  sym #187 @ 0xc92a0  (STRONG, ~1265 lines)
    // validates (block, partition, free) of a locally-declared tensor:
    //   "The leading dimension of SBUF/PSUM tensors must be the partition dimension."
    //   "Block dimension is deprecated"
    // → err_leading_dimension_of_tensor_must_be_partition
```

### Precondition gates

```c
function assert_is_tile(obj, ...):                // sema.py:1356  sym #37 @ 0x92890
    // operand must be a real NKI tile, not a python scalar / bare ndarray
    if not tile_like(obj):
        err_unexpected_type_of_operand(...)       // decompile line 757
    if local_tensor_without_view(obj):
        err_failed_to_infer_tile_from_local_tensor(...)   // line ~1371: fixed by indexing, e.g. a[i]

function assert_constant_value(value, ...):       // sym #63 @ 0x9dc10
    // shape/stride/mask args must be compile-time literals, not runtime tiles
    if not is_compile_time_constant(value):
        err_expected_constant_value(...)
```

### The matmul cluster

The matmul checks enforce the PE systolic-array layout: contraction dimension on the partition axis (`≤128`), free-axis caps, and the microscaling (`mx`, block-scaled fp4/fp8) packing rules.

```c
function check_matmul_f_dim_sizes(self, ...):     // sema.py:869  sym #112 @ 0xe7f90
    // free-axis upper bound of stationary/moving operands (call sites 1445/1723)
    if free_size > target_free_limit:             // = 512 on the moving axis (stub)
        err_size_of_dimension_exceed_arch_limit(...)

function check_matmul_low_level_shape / _high_level_shape:   // sym #108 / #110
    // enforce (contraction=partition, free) layout
    if stationary.par != moving.par:
        err_param_shape_incompatible_with_matmul("Number of partitions mismatch")

function check_matmul_mx_low_level_shape(self, ...):   // sym #125/#126 @ 0xdcd40
    // microscaling matmul: routes through assert_elements_per_partition / assert_exact_elements
    //   "nc_matmul_mx stationary/moving must access 32/64/128 partitions."
    //   "nc_matmul_mx stationary must have an even number of packed (x4) elements per partition."
    //   "quantize_mx src must have the last free dimension size to be multiple of 4."
```

### Matmul limit table

| Operand axis | Limit | `target` / source | Confidence |
|---|---|---|---|
| stationary partition (= contraction) | `≤128`, identical to moving partition | `nc_matmul` stub: *"identical and `<=128`"* | CONFIRMED |
| stationary free | `≤128` | `nc_matmul` stub: *"free axis sizes … must be `<= 128`"* | CONFIRMED |
| moving free | `≤512` | `nc_matmul` stub: *"and `<=512`, respectively"* | CONFIRMED |
| mx partition access | one of `32 / 64 / 128` | `check_matmul_mx_low_level_shape` string | CONFIRMED |
| output placement | PSUM, dtype `{fp32, int32}` | `nc_matmul` stub + Family 3 | CONFIRMED |

> **QUIRK —** the matmul caps `128` (stationary free) and `512` (moving free) are **asymmetric** and not interchangeable. The `nc_matmul` stub example is unambiguous: `stationary.shape = (128, 126)`, `moving.shape = (128, 512)` → output `(126, 512)`. The stationary operand feeds the array columns (capped at the 128 PE columns); the moving operand streams through (capped at the 512-deep accumulator depth). A reimplementer that applies one cap to both operands will reject legal `(_, 512)` moving tiles or accept illegal `(_, 512)` stationary tiles.

> **NOTE —** `nl.transpose` / `matmul(transpose=…)` on the Tensor Engine is **illegal inside direct-allocation ("allocated") kernels** — the string instructs the author to *"declare your own identity tensor and call nisa.nc_matmul"* instead. This is checked alongside `check_transpose_shape` (`sym #114`). It is a kernel-mode restriction, not a shape bound (STRONG).

---

## Per-Op Support Checks (reduce op-codes)

### Purpose

Beyond shape/dtype/space, some ops restrict the *operation code* itself. `tensor_reduce` accepts only a fixed set of reduction operators, split into **bitvec** operators (bit-pattern, integer-only) and **arithmetic** operators (with an optional `negate`). This is the op-code legality gate.

### Algorithm

```c
function check_tensor_reduce_supported_ops(self, tensor, op):   // sema.py:1375  sym #39 @ 0x5ec40
    if op:
        name = op.name
        if not PySequence_Contains(SUPPORTED_REDUCE_OPS, name):   // static tuple __pyx_tuple__147
            err_instruction_unsupported_op(...)    // sema.py:1396
    return None
```

The **supported reduce op-name set** (from the module string table):

```text
add, multiply, maximum, minimum, max, min,
bitwise_and, bitwise_or, bitwise_xor,
logical_and, logical_or
```

### Companion reduce-family rules (message-side → 6.4.5)

| Guard | Rule |
|---|---|
| `err_bitvec_operand_must_be_integer` | `bitwise_*` / `logical_*` reduces require an **integer** operand dtype |
| `err_reduce_bitvec_op_invalid_dtype` / `err_par_reduce_bitvec_op_invalid_dtype` | free-axis vs partition-axis (`par_reduce`) bitvec dtype guards |
| `err_reduce_unsupported_negate` | *"negate option can only be used with arithmetic ops"* — `negate` illegal on bitwise/logical |
| (`atomic_rmw`) | *"`op` param only supports 'add' operation currently."* |

> **NOTE —** the bitvec/arithmetic split is corroborated directly by the shipped `nki/isa/__init__.pyi` `tensor_reduce` docstring: *"There are two types of reduction operators: 1) bitvec operators (e.g., bitwise_and, bitwise_or) … 2) arithmetic operators (e.g., add, subtract, multiply)"* and *"`negate`: … only applicable when op is an arithmetic operator"*. That is the same rule `check_tensor_reduce_supported_ops` and its companion guards enforce — re-grounded against binary-distributed stub evidence (CONFIRMED), independent of the absent decompile.

---

## Type-Hint Canonicalizer

### Purpose

Before any dtype/addr-space assert can run on a parameter, its *type annotation* must be a resolved type object — not a forward-reference string. The canonicalizer rejects string hints and flattens `Union`/`Optional` annotations into the concrete set the dtype asserts iterate.

### Algorithm

```c
function canonicalize_type_hint(x):               // sema.py:61  sym #3 @ 0x45990
    if PyType_HasFeature(type(x), Py_TPFLAGS_UNICODE_SUBCLASS):   // tp_flags & 0x10000000
        raise AssertionError("Unexpected str in typehint")        // sema.py:62 — forward-refs illegal
    return x

function enumerate_all_types(annotation):         // sema.py:70  sym #5 @ 0xd9330 (+ lambda sym #19 @ 0x3d940)
    annotation = canonicalize_type_hint(annotation)      // strip/validate; reject str
    if annotation is inspect.Parameter.empty:
        return []                                        // no annotation — caller handles
    if is_union(annotation):                             // typing.Union / Optional[X] = Union[X, None]
        return flatten(map(enumerate_all_types, annotation.__args__))   // RECURSIVE flatten
    return [annotation]                                  // single concrete type
```

`enumerate_all_types` turns a possibly-`Union` parameter annotation into the flat set of admissible concrete types, resolving `tensor`/`tile` aliases to their IR classes so the addr-space and dtype asserts can run. It feeds `NKIFunc.check_param_type` / `check_param_against_dtype_hints`, which dispatch to `assert_dtype_in` / `assert_dtype` against that set.

> **GOTCHA —** a *string* type hint (`def f(x: "Tensor")`) is rejected with `"Unexpected str in typehint"` — NKI signatures must use resolved type objects, never PEP-563 deferred / forward-ref strings. A reimplementer enabling `from __future__ import annotations` (which stringizes all hints) on an NKI kernel signature would trip this assert on every parameter.

---

## The `target` Attribute Contract

`sema.so` reads every numeric limit off the runtime hardware-target object. Reproducing the checker means reproducing exactly this attribute set — these are the knobs that make the same checker arch-parametric across Inferentia / Trainium gen2–gen4.

| Attribute | Read by | Meaning | Value (cross-ref) |
|---|---|---|---|
| `statebuf_num_partitions` | `assert_par_dim_sbuf` | SBUF lane count | 128 (all gens) — [1.05](../arch/sbuf-psum-geometry.md) |
| `statebuf_par_size_in_bytes` | `assert_free_dim_sbuf` | SBUF bytes/partition | 192/224/256 KiB (gen2/3/4); 16 KiB reserved — [1.05](../arch/sbuf-psum-geometry.md) |
| `psum_num_partitions` | `assert_par_dim_psum` | PSUM lane count | 128 (64 on gen1) — [1.05](../arch/sbuf-psum-geometry.md) |
| `psum_par_size_in_bytes` | `assert_free_dim_psum` | PSUM bytes/partition/bank | 2 KiB — [1.05](../arch/sbuf-psum-geometry.md) |
| `psum_num_banks` | `assert_free_dim_psum` | PSUM bank count | 4/8/8/8 (gen1..gen4) — [1.05](../arch/sbuf-psum-geometry.md) |
| `psum_partition_size` | free-dim/stack-overflow path | PSUM partition span | per-gen |
| `float32`, `int32` | `assert_dtype_psum` | the two legal PSUM dtypes | type objects |

Operand/op attributes read on the tile side: `dtype`, `shape`, `tensor_ir_class`, `sizeinbytes`, `name` (`op.name`), `params_map`, `cur_api_name`, `is_superclass`, `issubdtype`.

### RichCompare polarity (recovered from disasm `edx` immediate — report grade)

| Assert | Op (`edx`) | Failing condition |
|---|---|---|
| `assert_par_dim_sbuf` / `_psum`, `assert_num_partition` | `Py_GT` (4) | `par_dim > limit` → error |
| `assert_free_dim_sbuf` / `_psum` | `Py_LT` (1) | `capacity < required_bytes` → error |

> **CORRECTION (D-W04 caveat) —** the report flags that the `Py_EQ` (2) compares elsewhere in these Cython wrappers are the **kwarg-name interning fast-path**, *not* semantic checks — IDA inlines them adjacent to the real `Py_GT`/`Py_LT` guards. A reimplementer reading a disasm of these wrappers must not mistake the `Py_EQ` keyword-dispatch comparisons for the legality comparison. The semantic guards are the `Py_GT` (partition) and `Py_LT` (free-dim) ones only.

---

## Adversarial Self-Verification

The five strongest claims on this page, re-challenged against binary-distributed evidence:

1. **"Five invariant families, all funneling through one `nki_assert` sink."** The five families (partition/free/dtype/shape/addr-space) are each a named assert cluster in the report's index; the single-sink design is asserted at `sym #23`. *Offset unverifiable (sidecar absent); the family taxonomy and the err→nki_assert dispatch are STRONG from the report's symbol/xref structure.* Tagged accordingly — not claimed CONFIRMED-by-binary.

2. **"PSUM tile dtype must be `float32` or `int32`."** `assert_dtype_psum` tests both via `target.float32`/`target.int32`. Re-grounded: PSUM is the matmul accumulator (`nc_matmul` stub: outputs written to PSUM); fp32/int32 are the natural accumulator formats. **CONFIRMED** by stub + architecture, independent of the offset.

3. **"Partition limit is 128, read from `statebuf_num_partitions`, not a literal."** Cross-checked against [1.05](../arch/sbuf-psum-geometry.md) `Statebuf+0x8 numPartitions = 128 (all gens)` and the `nc_matmul` stub `"<=128"`. **CONFIRMED.** The arch-parametric framing (PSUM = 64 on gen1) is also confirmed by [1.05](../arch/sbuf-psum-geometry.md).

4. **"matmul moving free ≤512, stationary free ≤128 — asymmetric."** Directly from `nc_matmul` stub lines: *"free axis sizes of `stationary` and `moving` … must be `<= 128` and `<=512`, respectively"* with the worked `(128,126)`×`(128,512)`→`(126,512)` example. **CONFIRMED** by binary-distributed stub.

5. **"Reduce op-set = {add, multiply, maximum, minimum, max, min, bitwise_{and,or,xor}, logical_{and,or}}, bitvec-vs-arithmetic split with `negate` arithmetic-only."** The `tensor_reduce` stub docstring confirms the two-category split and the `negate` restriction verbatim; the exact 11-name list is from the `sema.so` module string table (report grade). **CONFIRMED** for the category split; the precise name list is STRONG (carried from the absent decompile's string table, corroborated in spirit by the stub).

Failures fixed: every per-function `@0x…` / `sema.py:NNNN` is now explicitly marked *(offset unverified)* given the absent sidecar; the SBUF byte budget is given as the per-gen `statebuf_par_size_in_bytes` range from [1.05](../arch/sbuf-psum-geometry.md), not a flat "176 KiB", since sema reads it from `target`.

---

## Related Components

| Name | Relationship |
|---|---|
| [6.4.1 ISA Compute Intrinsics](isa-compute-intrinsics.md) | the `nc_matmul`/`activation`/elementwise ops whose operands these asserts gate |
| [6.4.2 ISA Reduce / DVE / DMA](isa-reduce-dve-dma.md) | `tensor_reduce` (op-code gate), indirect-DMA index-in-SBUF rule |
| [6.4.5 Diagnostic Catalog](diagnostic-catalog.md) | the `err_*()` message-template layer every assert dispatches to before `nki_assert` |
| [1.05 SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) | source of every numeric limit the `target` attrs carry (128 partitions, 2 KiB banks) |

## Cross-References

- [6.4.1 ISA Compute Intrinsics](isa-compute-intrinsics.md) — `nc_matmul` stationary/moving shape limits this page enforces
- [6.4.2 ISA Reduce / DVE / DMA](isa-reduce-dve-dma.md) — the reduce op-code set and indirect-DMA SBUF-index rule
- [6.4.5 Diagnostic Catalog](diagnostic-catalog.md) — the `err_num_partition_exceed_arch_limit` / `err_unsupported_memory` / `err_instruction_unsupported_op` message builders
- [1.05 SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — `statebuf_num_partitions`, `psum_num_banks`, and the per-gen byte budgets behind the `target` attribute contract
