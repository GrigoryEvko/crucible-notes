# Dispatch-Table Taxonomy

> *All addresses, section names, strides, and counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel: a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d` (the only unambiguous version anchor — pin to it). The package metadata reports version `0.0.40`; a `0.103` plugin version is carried by surrounding tooling but is **not** observable as a literal string in the binary, so it is not used as a static anchor here. Other wheels differ in every address.*

## Abstract

`libtpu.so` carries two structurally distinct populations of indirect-dispatch
data, and they are routinely conflated. The first is the **function-pointer
table** population: 40,313 tables holding 516,323 pointers, almost all of which
are relocated C++ vtables. The second is the **compiled switch jump-table**
population: 33,016 LLVM-lowered `switch` statements indirecting through 4,673,757
case targets in a separate read-only offset region. The famous "40,313 tables →
17 classes" headline conflates neither population correctly: the 40,313 figure is
the function-pointer count alone, and once thunk-, `std::`-, and
local-scope-mangled symbols are normalized, the taxonomy resolves to **19**
classes covering 99.6% of the tables, not 17.

This page is the census. It establishes the authoritative per-class counts, the
structural signature of each class — what a member table looks like in memory
(stride, entry kind, who indexes it, which section it lives in) — and a
representative address per class. The single most important structural fact is
that this is a PIE: in the file image the vtable slots are *zero*, and the real
targets are the addends of the `R_X86_64_RELATIVE` relocations the loader applies
— 924,033 of the binary's relocations (1,069,006 of them `RELATIVE`) land in
`.data.rel.ro`. IDA's table sidecar has already resolved each slot
through its relocation, so the recovered `target_func` names are post-load truth.

The frame of reference is the Itanium C++ ABI. Every "true" table is a vtable
laid out `[offset-to-top][typeinfo*][slot 0][slot 1]…`, addressed at its *address
point* (`_ZTV<X>+0x10`), 8 bytes per slot. The classes that are *not* vtables —
LLVM `UniqueFunctionBase` type-erasure pools, libpfm4 PMU C-tables, abseil
container policy thunks, AnyInvocable invokers — are called out explicitly, because
a reimplementer who treats them as vtables will mis-model both their mutability and
their indexing. Per-slot method labelling of the major hierarchies, and the deep
mechanics of the thunk and top-vtable classes, are deferred to sibling pages; this
page owns the population-level taxonomy.

For reimplementation, the contract is:

- The two-population split: 40,313 function-pointer tables (this page's subject)
  versus 33,016 switch jump tables, and why they must never be summed.
- The Itanium-ABI memory signature of a vtable table — stride 8, address point at
  `_ZTV+0x10`, slots zero-in-file and loader-filled — and the four non-vtable
  signatures that break that mold.
- The 19-class decomposition with its keying rule (the classifier keys on the
  *symbol*, e.g. the `RegisteredOperationName::Model` Op-Model marker, not on table
  size), and the section invariant (`.data.rel.ro` = const vtables,
  `.data` = runtime-mutable pools, `.rodata` = pure/member-pointer tables).

| | |
|---|---|
| **Function-pointer tables** | 40,313 (sidecar record count, exact) |
| **Total pointers across tables** | 516,323 |
| **Section split** | `.data.rel.ro` 38,664 · `.data` 1,442 · `.rodata` 207 |
| **Switch jump tables (separate)** | 33,016 / 4,673,757 case targets |
| **"vtable for" RTTI records** | 39,155 (of 160,351 RTTI records total) |
| **`R_X86_64_RELATIVE` relocations** | 1,069,006 (`DT_RELACOUNT`; 924,033 in `.data.rel.ro`) |
| **Largest table** | `0x223393a0` — 2,595 entries (`UniqueFunctionBase`, `.data`) |
| **Op-Model fingerprint** | size-23: 6,129 tables, 6,050 carry a `Model` entry |
| **Taxonomy classes** | 19 rows = 18 attributed classes (99.6%) + 1 residual (157 / 0.4%) |

---

## The Two Populations

Before the taxonomy, fix the distinction that the "40,313 → 17 classes" headline
blurs. There are two separate indirect-dispatch mechanisms in the binary, recorded
by two separate sidecars, and they share no entries.

```text
function-pointer tables (this page)        switch jump tables (separate)
  40,313 tables                              33,016 tables
  516,323 pointer slots                      4,673,757 case targets
  live in .data.rel.ro / .data / .rodata     indirect jmp through .lrodata
  8-byte stride, each slot a code pointer     N-byte case→offset, computed-goto
  almost all are C++ vtables                  LLVM-lowered C/C++ switch statements
  indexed by object vptr + slot index         indexed by (scrutinee - lo) → jmp
```

A function-pointer table is read *by an object*: the object stores a pointer to
the table's address point in its hidden vptr field, and a virtual call loads slot
`i` and jumps. A switch jump table is read *by a function body*: the lowered
`switch` subtracts the case minimum, bounds-checks, loads a relative offset from a
`.lrodata` array, and computes a goto. The two never overlap; summing them
(40,313 + 33,016) is meaningless.

> **GOTCHA —** the headline "40,313 tables" is the function-pointer count *only*.
> It is not the switch count (33,016), not the `R_X86_64_RELATIVE` relocation count
> (1,069,006), and not the "vtable for" RTTI-symbol count (39,155). A reimplementer who reads
> "40,313 dispatch tables" as "40,313 vtables" is close but wrong by ~1,158: the
> figure includes type-erasure pools and C-runtime tables that are not vtables.

### The PIE loader-fill invariant

Every count of "targets" on this page is post-relocation. The binary is a
position-independent executable whose vtable slots are **0x0 in the file image**;
the loader patches them at load time through `R_X86_64_RELATIVE` relocations whose
addend is the real target VA. The relocation distribution proves where the tables
live:

| Section | Relocs (all types) | of which `RELATIVE` | Role |
|---|---:|---:|---|
| `.data.rel.ro` | 924,033 | 924,015 | const-after-reloc vtables + RTTI thunks |
| `.data` | 131,596 | 131,590 | runtime-mutable pools (`UniqueFunctionBase`, libpfm4) |
| `.got` / `.got.plt` | 6,698 | 6,069 | GOT entries for cross-module indirection |
| all others | 7,332 | 7,332 | `.ldata` (3,507), `.init_array` (2,471), `.tbss`/misc |

`R_X86_64_RELATIVE` is the dominant relocation type but not the only one. The
binary's `.rela.dyn` + `.rela.plt` hold **1,069,659** relocation entries total, of
which **1,069,006** are `R_X86_64_RELATIVE` (the count `DT_RELACOUNT` reports;
relocation code `12` in `readelf -rW`) — the loader-fill addends that populate the
zero-in-file vtable slots. The remaining 653 are non-fill types (473 `JUMP_SLOT`,
99 `GLOB_DAT`, 57 `DTPMOD64`, 24 `R_X86_64_64`) and do not address dispatch-table
slots. The 924,033 / 131,596 section anchors are the *all-type* reloc counts (and
match `index.md`/`overview.md`); only 18 and 6 of those, respectively, are
non-`RELATIVE`. Every "target" count on this page is post-`RELATIVE`-relocation.
The Itanium-ABI vtable layout, per class `X`, is therefore:

```text
_ZTV<X> + 0x00   offset-to-top   (0 for a primary vtable)
_ZTV<X> + 0x08   &_ZTI<X>        (typeinfo — binds the table to its class)
_ZTV<X> + 0x10   slot 0          <-- ADDRESS POINT: what the object's vptr holds,
_ZTV<X> + 0x18   slot 1               and the key under which the table sidecar
_ZTV<X> + 0x10+8i slot i              records this table; entry count == slot count
```

The sidecar keys each table at the address point (`+0x10`) and reports its entry
count as the slot count. The detail of walking a slot back to its method name —
`readelf -r` at `+0x10+8i`, addend equals `&method_i`, covering symbol names the
method — is owned by the RTTI/vtable census (see Cross-References).

---

## Taxonomy at a Glance — Function-Pointer Tables

Nineteen classes cover 99.6% of the 40,313 tables. The classifier keys on the
recovered symbol of the table's contents (the namespace of its vtable owner, or
the presence of a marker symbol such as `RegisteredOperationName::Model`), **not**
on table size — size-23 coincidences are resolved by the marker, not the arity.
The column **Stride/Entry** gives the memory signature.

| ID | Class | Count | % | Med | Max | Section | Stride / entry kind |
|----|-------|------:|----:|----:|----:|---------|---------------------|
| E  | TPU ISA encoder vtables (`asic_sw`) | 9,932 | 24.6% | 6 | 674 | `.data.rel.ro` | 8 B vtable; per-gen per-lane-cluster encoder/clone |
| A  | MLIR Op-Model arrays | 6,085 | 15.1% | 23 | 113 | `.data.rel.ro` | 8 B; 23-slot `Model<Op>` interface ABI |
| F  | `mlir::` vtables (non-Op-Model) | 4,270 | 10.6% | 8 | 108 | `.data.rel.ro` | 8 B vtable; pass/dialect/interface objects |
| I  | `llvm::` vtables | 2,611 | 6.5% | 10 | 336 | `.data.rel.ro` | 8 B vtable; TargetLowering/ISel/passes |
| D  | dnnl / Xbyak JIT vtables | 2,289 | 5.7% | 10 | 29 | `.data.rel.ro` | 8 B vtable; JIT primitive + code-gen |
| G  | `xla::` / `stablehlo::` vtables | 2,154 | 5.3% | 6 | 266 | `.data.rel.ro` | 8 B vtable; incl. 6× 266-slot per-gen Target |
| H  | `tensorflow::` / `tsl::` vtables | 2,153 | 5.3% | 7 | 89 | `.data.rel.ro` | 8 B vtable; grappler/runtime objects |
| P  | abseil hash-container policy thunks | 2,066 | 5.1% | 7 | 447 | `.data.rel.ro` | 8 B; type-erased `flat/node_hash` policy |
| O  | long-tail named-namespace vtables | 1,866 | 4.6% | 6 | 111 | `.data.rel.ro` | 8 B vtable; ~150 small namespaces |
| K  | libc++ `std::` thunks | 1,802 | 4.5% | 5 | 345 | `.data.rel.ro` | 8 B; `shared_ptr_emplace`/`__policy_func` |
| N  | TPU runtime / profiler vtables | 1,130 | 2.8% | 6 | 63 | `.data.rel.ro` | 8 B vtable; `TpuHal`/`TpuCore`/`TpuCodec` |
| M  | gRPC / `grpc_core` vtables | 931 | 2.3% | 5 | 30 | `.data.rel.ro` | 8 B vtable; channel/filter/promise state |
| C  | libpfm4 PMU event tables | 833 | 2.1% | 5 | 10 | `.data` (mutable) | 8 B; **C struct**, not vtable |
| L  | protobuf message/descriptor vtables | 712 | 1.8% | 6 | 117 | `.data.rel.ro` | 8 B vtable; reflection/`MapEntry` |
| Z1 | anonymous-namespace static helpers | 698 | 1.7% | 10 | 165 | `.data.rel.ro` | 8 B vtable; `_GLOBAL__N_` TU-local |
| B  | LLVM `UniqueFunctionBase` pools | 589 | 1.5% | 9 | 2,595 | `.data` (mutable) | 8 B; **type-erasure pool**, not vtable |
| R  | C-runtime / Rust handler tables | 33 | 0.1% | 9 | 30 | `.data.rel.ro` | 8 B; cURL/BoringSSL/zstd/Rust C structs |
| Q  | abseil AnyInvocable invokers | 2 | 0.0% | 4 | 4 | `.rodata` | 8 B; `InvokeObject` type-erasure |
| Z  | unclassified (IDA auto-named) | 157 | 0.4% | 6 | 60 | `.data.rel.ro` | 8 B; pure-virtual-only / no owner symbol |

> **NOTE —** the per-class library counts (E, F, I, G, …) are HIGH but not
> CERTAIN: the boundary between a "thunk" table and the vtable it forwards into,
> and between sibling namespaces, depends on symbol normalization. The *totals*
> (40,313 tables, 516,323 entries, the section split, the size-23 Op-Model
> fingerprint) re-derive exactly from the sidecar and are CERTAIN. Treat the class
> rows as the authoritative shape of the space; re-derive a single class's exact
> count only if a downstream claim hinges on it.

> **Note:** the 19-class decomposition depends on symbol normalization. The
> classifier collapses thunk-prefix mangling (`_ZThn`/`_ZTv`), libc++ `std::`
> thunks, and local-scope (`_ZZ`) mangling onto their owning class before keying;
> without that step the abseil `raw_hash_set` policy thunks (Class P, ~2,000
> tables) fall into the long-tail and the residual unclassified bucket inflates
> from 157 (0.4%) to several thousand. dnnl and Xbyak JIT vtables (Class D) are one
> class, and the C-runtime/Rust handler tables (Class R) are genuine handler tables,
> not trampoline false positives.

### Structural decomposition (library-independent)

Cross-cutting the 19 library classes, the 40,313 tables decompose by *kind* as
follows. This is the decomposition a reimplementer cares about, because kind
dictates mutability and indexing:

```text
~39,155   true C++ vtables          .data.rel.ro, const-after-reloc, vptr-indexed
   6,070   contain an Op-Model entry  (subset of the vtables; Model<Op> arrays)
     589   UniqueFunctionBase pools  .data, runtime-mutable, NOT vtables (Class B)
     833   libpfm4 PMU C-tables      .data, C structs, NOT vtables       (Class C)
  ~1,158   other non-vtable dispatch abseil policy thunks, member-ptr,
                                     C-runtime handler tables (P/Q/R + tail)
```

The "39,155 vtables / 40,313 tables" relationship is the central anchor: nearly
every dispatch table is a relocated C++ vtable, and 39,155 is exactly the count of
"vtable for" RTTI records. The ~1,158-table gap is precisely the non-vtable
classes — and a reimplementer who models all 40,313 as const vtables will get the
mutability of 1,442 `.data` tables wrong.

---

## Class A — MLIR Op-Model Arrays

### Purpose

MLIR registers each operation through a `RegisteredOperationName`, and the
op-interface dispatch (verify, parse, print, fold, getCanonicalizationPatterns,
…) is carried by a `Model<ConcreteOp>` array installed per registered op. This is
the single largest *semantically uniform* class: 6,085 tables, one family per
registered MLIR op across ~50 dialects.

### Structural signature

A Class A table is a 23-slot const vtable in `.data.rel.ro`, stride 8, whose
contents are `RegisteredOperationName::Model<Op>` member functions. Size 23 is the
fingerprint of the Op-Model interface ABI in this build.

```text
size-23 tables in the binary ............ 6,129
  of which carry a Model<Op> entry ...... 6,050   <-- Class A core
  23-slot vtables that merely coincide .. 79      <-- e.g. PjRtDevice has 23 vmethods
tables with >=1 Model entry (any size) .. 6,070
```

> **QUIRK —** size-23 is a near-perfect but not perfect detector. 79 size-23
> tables are ordinary vtables of unrelated classes that happen to have exactly 23
> virtual methods (`xla::MegaScalePjRtDevice`, and the 23-slot `PjRtDevice`
> vtables documented on the PJRT side). The classifier keys on the
> `RegisteredOperationName::Model` symbol *in the table contents*, not on the
> arity, so it does not mis-bucket the 79. A reimplementer who keys on size alone
> will mislabel them.

### Representative addresses

| Address | Section | Entries | First symbol |
|---|---|---:|---|
| `0x219bfbe8` | `.data.rel.ro` | 23 | `mlir::RegisteredOperationName::Model<mlir::ROCDL::BlockIdXOp>` |
| `0x219d4e48` | `.data.rel.ro` | 23 | `mlir::RegisteredOperationName::Model<xla::PureCallOp>` |

> **Note:** a Class A anchor must be the *address point* of a confirmed
> `Model<Op>` vtable — i.e. `_ZTV…+0x10`, not the `_ZTV` symbol itself, and not a
> coincidental size-23 vtable. `0x215fca58` is the `_ZTV` for
> `xla::MegaScalePjRtDevice` (one of the 79 size-23 non-Model coincidences this
> page flags), and `0xa2c33e0` falls *inside* the `asic_sw::…profiler` PMU
> `kCmq_lookup` C-table (`.rodata`, base `0xa2c33c0`) — neither is a `Model<Op>`
> vtable. The correct anchors are `0x219bfbe8`
> (`_ZTVN4mlir23RegisteredOperationName5ModelINS_5ROCDL10BlockIdXOpEEE`, 23 slots,
> `.data.rel.ro`) and `0x219d4e48` (`Model<xla::PureCallOp>`, 23 slots). The binary
> carries exactly **6,050** `vtable for …RegisteredOperationName::Model<…>` symbols,
> confirming the size-23/with-Model count below.

The dialect distribution skews hard toward the TPU/sparse-core dialects — the
top contributors by Op-Model count are `sparse_core`, `TF`, `spirv`, `ROCDL`,
`llo`, `LLVM`, `transform`, `NVVM`, `vhlo`, `mhlo`, `stablehlo`. A reimplementer
sizing the op-registration table should expect ~6,000 registered ops, not the few
hundred a stock upstream MLIR build carries.

---

## Class E — TPU ISA Encoder Vtables (`asic_sw`)

### Purpose

The largest class by table count (9,932, 24.6%) is the per-instruction
instruction-encoder dispatch for the TPU ISA, under the `asic_sw::deepsea`
namespace. Each table is a small vtable for an encoder (or its clone) of one ISA
operation, and the population partitions cleanly by **silicon generation and lane
cluster** — this is the structural form that per-generation dispatch takes in this
binary (not a `TpuVersion` switch).

### Structural signature

A Class E table is a small const vtable (median 6 slots, max 674) in
`.data.rel.ro`. The defining feature is the namespace partition: tables are grouped
by `<gen>xc::<cluster>fc::isa`, and the counts are near-symmetric within a
generation, suggesting paired encode/clone vtables per opcode.

```text
lane-cluster partition of the 9,932 Class E tables:
  gxc/gfc  2,290     gxc/glc  2,270     <-- the dominant generation
  vxc/isa  1,328     vxc/vfc    427
  pxc/isa    592     pxc/pfc    241
  jxc/dfc      4     jxc/jfc      2     pxc/plc  2
```

> **QUIRK —** the gxc/gfc (2,290) vs gxc/glc (2,270) near-symmetry is the
> encode/clone-pair signature. Whether the pairing is exactly 1:1 with ISA opcodes
> (vs. helper duplicates) is not yet confirmed against the opcode census, so treat
> the encoder↔opcode ratio as MEDIUM.

### Representative addresses

| Address | Section | Entries | First symbol |
|---|---|---:|---|
| `0x21e0d0a0` | `.data.rel.ro` | 674 | `asic_sw::deepsea::gxc::gfc::isa::TensorCoreVectorAlu::Compact` (vtable `_ZTV…+0x10`) |
| `0x21e0d0a0…` | `.data.rel.ro` | 674/623/620 | per-lane-cluster `TensorCoreVectorAlu::Compact` encoders |

---

## Class B — LLVM `UniqueFunctionBase` Type-Erasure Pools

### Purpose

`llvm::detail::UniqueFunctionBase` is LLVM's move-only type-erased callable. The
589 Class B tables are **not vtables** — they are runtime-mutable pools of
`CallImpl` thunks, one pool per distinct callable signature. The single largest
table in the entire binary is one of these.

### Structural signature

A Class B table lives in **`.data` (mutable)**, not `.data.rel.ro`. Stride is 8,
but the entries are `CallImpl<Lambda>` thunks rather than a class's virtual
methods, and the pool is populated at module init and may be mutated at runtime —
the const-after-reloc invariant of the vtable classes does *not* hold here.

```text
.data table population (1,442 total, all runtime-mutable):
  833   libpfm4 PMU C-tables           (Class C)
  585   UniqueFunctionBase pools       (Class B core; 589 incl. variant mangling)
  ~24   misc gRPC / cURL handler tables
```

### Representative address

| Address | Section | Entries | First symbol |
|---|---|---:|---|
| `0x223393a0` | `.data` | 2,595 | `UniqueFunctionBase<LogicalResult(Operation*,ArrayRef<Attribute>,…)>::CallImpl<…>` |

The 2,595-entry table at `0x223393a0` is the **unified MLIR op verify/parse/print/
fold dispatch pool**: every registered op's fold-hook lambda is type-erased into a
single `UniqueFunctionBase<LogicalResult(Operation*, …)>` pool. It is the largest
function-pointer table in the binary and it is mutable.

> **GOTCHA —** treating Class B and C tables as const vtables is a correctness
> bug: they sit in `.data`, are written at init, and are not indexed by an object
> vptr. The 1,442 mutable tables are the exception to "almost every table is a
> const vtable" — model their mutability or lose it.

---

## Class C — libpfm4 PMU Event Tables

### Purpose

833 tables are libpfm4's per-microarchitecture performance-monitoring-unit event
lookup tables, enabling host-CPU PMU sampling (xprof) while the TPU kernel runs.
These are **C structs**, not C++ vtables.

### Structural signature

Small (median 5, max 10), in `.data` (mutable), populated at init. The entries are
C function pointers into the libpfm4 detect/encode routines, keyed by host
microarchitecture. The library covers the full modern x86 lineage — Intel
Core/Atom through Sapphire Rapids, AMD fam10h–fam19h, NetBurst, and `perf_raw`.

### Representative address

| Address | Section | First symbol |
|---|---|---|
| `0x222662f8` | `.data` | `pfm_perf_event_os_detect` |

---

## Class P / Q — abseil Type-Erasure Dispatch

### Purpose

abseil's hash containers (`flat_hash_set/map`, `node_hash_set/map`) are
type-erased through a per-instantiation policy. Class P (2,066 tables) is that
policy-thunk dispatch; Class Q (2 tables) is the `AnyInvocable`/`InvokeObject`
invoker thunks. These are dispatch *structures*, not class vtables.

### Structural signature

The defining member is a single global 447-entry policy table that fans out to
every hashmap instantiation in the binary — one global type-erasure point rather
than per-container vtables. The remaining ~2,000 are smaller per-policy thunk
tables. Class Q's two tables are tiny (4 slots) invokers in `.rodata`.

### Representative addresses

| Address | Section | Entries | First symbol |
|---|---|---:|---|
| `0x21c1d590` | `.data.rel.ro` | 447 | `absl::container_internal::GetRefForEmptyClass` |
| `0xa30c788` | `.rodata` | 4 | `absl::functional_internal::InvokeObject<…>` |

> **Note:** the bulk of the apparent "AnyInvocable" tables are actually
> `raw_hash_set` policy thunks — Class P is 2,066 tables and Class Q is only 2. The
> 447-entry global policy table sits at `0x21c1d590`.

---

## Class N / G — Per-Generation Dispatch Families

### Purpose

Per-silicon-generation dispatch is carried by **parallel vtable families**, not by
a `TpuVersion` switch. Class N (TPU runtime, 1,130) and Class G (xla/jellyfish,
2,154) hold the families that install per-generation behavior — cost models, ISA
codecs, and target descriptors — one vtable per generation.

### Structural signature

Per-generation families appear as runs of consecutive small vtables of identical
arity, one per generation/lane-cluster:

```text
5x *CycleTable vtables {Jf,Pf,Vf,Glc,Gfc}   0x21c1ffc8 .. 0x21c201d8, 5 slots each
   TpuCodec{Jellyfish..Ghostlite} 6-slot      0x21d35810 ..
6x jellyfish 266-slot Target vtables           0x21cc6358 .. 0x21cce6b0  (Class G)
   {Dragonfish,Jellyfish,Pufferfish,Viperfish,Ghostlite}Target + base Target
```

> **QUIRK —** only 12 literal `TpuVersion` switch jump tables exist in the entire
> binary (Class S-GEN below). Per-gen dispatch is overwhelmingly vtable-based: the
> generation is selected once at target construction, which installs the correct
> vtable family; thereafter dispatch is an ordinary virtual call. A reimplementer
> who models per-gen behavior as a giant `switch(version)` is modeling the wrong
> mechanism.

> **Note:** measuring slot counts directly off the symbol table (gap to the next
> `_ZTV`/`_ZTI` symbol, minus the 2-slot offset-to-top/typeinfo header) gives
> **6 `Target` vtables at exactly 266 slots** in `0x21cc6358…0x21cce6b0`: the five
> concrete generations
> (`xla::jellyfish::{Dragonfish,Jellyfish,Pufferfish,Viperfish,Ghostlite}Target`)
> plus the abstract base `xla::jellyfish::Target`. The two `*SparseCoreTarget`
> vtables in the same address band are **28**-slot, not 266, and are not part of
> this family. The per-slot method labelling of these families is owned by the
> top-vtable / per-gen sibling pages (see Cross-References).

Detail for these families — slot-level method names, the override matrix across
generations — belongs to the RTTI/vtable census and the per-gen dispatcher pages
and is not duplicated here.

---

## Long-Tail and Residual Classes

The remaining classes are structurally ordinary 8-byte const vtables, separated
only by owning namespace:

- **Class F / I / H / D / L / M** (`mlir`, `llvm`, `tensorflow`/`tsl`,
  dnnl/Xbyak, protobuf, gRPC) — standard library-object vtables. The largest
  arities live here: a 336-slot `llvm` vtable, a 345-slot `std::` thunk table, a
  117-slot protobuf descriptor vtable. These are the codegen/runtime object models
  of the embedded libraries.
- **Class O** — ~150 small namespaces (Eigen, OR-tools, RE2, riegeli, antlr, ICU,
  …), each contributing a handful of vtables; 1,866 total. MEDIUM confidence
  because the namespace boundary is fuzzy.
- **Class K** — libc++ `std::` thunks: `shared_ptr_emplace`,
  `__function::__policy_func`, sort-policy thunks. 1,802 tables; MEDIUM because
  these are the most heavily ICF-folded and thunk-prefixed symbols.
- **Class Z1** — anonymous-namespace (`_GLOBAL__N_`) TU-local pass/lambda
  dispatch, 698.
- **Class R** — 33 genuine C-runtime/Rust handler tables (cURL, BoringSSL
  connection filters, zstd, hwloc, Rust `_RNv` mangling). These are real handler
  tables, not trampoline false positives.
- **Class Z** — 157 tables (0.4%) IDA could not attribute: pure-virtual-only
  abstract-class vtables (`__cxa_pure_virtual`) or `sub_`/`nullsub_` auto-named
  tables with no recoverable owner symbol. Their owner could be recovered by
  matching slot addresses to `.text` function ranges, not from symbols. LOW.

---

## Switch Jump Tables — The Separate Population

The 33,016 compiled `switch` jump tables are reported here only to fix the
boundary; they are LLVM-lowered `switch` statements indirecting through `.lrodata`
offset arrays, structurally distinct from the function-pointer tables above. The
total case-target count is 4,673,757 (≈140× the function-pointer table count's
entry total), dominated by the TPU ISA encode/decode opcode switches.

| ID | Class | Count | % | Max cases | Representative |
|----|-------|------:|----:|----:|----------------|
| S-ISA | TPU ISA encode/decode opcode switch | 11,746 | 35.6% | 7,529 | `…gxc::glc::profiler::PerformanceCounterNameToString` |
| S-OTH | other named-namespace switch | 6,454 | 19.5% | 685 | `tcmalloc::FindExperimentByName` |
| S-LLVM | LLVM IR / codegen switch | 3,566 | 10.8% | 4,111 | `function_ref<…>::callback_fn` (MLIR walk) |
| S-XLA | XLA HLO opcode / shape switch | 2,985 | 9.0% | 5,549 | `xla::primitive_util::PrimitiveTypeSwitch` |
| S-Z | unclassified switch | 2,155 | 6.5% | 5,501 | `TF_TString_ResizeUninitialized` |
| S-DNNL | oneDNN primitive/isa switch | 2,002 | 6.1% | 765 | `memory_desc_wrapper::compute_blocking` |
| S-ANON | anonymous-namespace static switch | 1,651 | 5.0% | 2,594 | `(anonymous)::TpuToDmaCoreId` |
| S-MLIR | MLIR op/dialect/attr switch | 1,423 | 4.3% | 548 | `tf_device::ReplicateOp::getInherentAttr` |
| S-TF | TensorFlow op switch | 413 | 1.3% | 132 | `TPUPartitionedCallOp::SetDeviceOrdinal` |
| S-GRPC | gRPC state-machine switch | 336 | 1.0% | 94 | `channelz::BaseNode::KindToEntityType` |
| S-PROTO | protobuf field/wiretype switch | 200 | 0.6% | 90 | `TreeNode::MergeRepeatedField` |
| S-TG | LLVM TableGen instr-select/encode | 73 | 0.2% | 40,813 | `AMDGPUMCCodeEmitter::getBinaryCodeForInstr` |
| S-GEN | per-generation `TpuVersion` direct switch | 12 | 0.0% | 25 | `TpuCodec::Create(TpuVersion)` |

> **NOTE —** the single largest switch in the binary is
> `AMDGPUMCCodeEmitter::getBinaryCodeForInstr` at **40,813 cases** — a
> TableGen-generated instruction-encoder dispatch (Class S-TG). It is a
> *switch*, not a function-pointer table; it does not contribute to the 40,313
> figure despite the numerical coincidence. The asic_sw ISA switches (S-ISA)
> account for 11,746 tables and 3.94M of the 4.67M total cases, including 11×
> per-generation `PerformanceCounterNameToString` switches at 7,529 cases each.

---

## Verification Notes

The relocation figures on this page are taken directly from the binary with
`readelf -dW`/`-rW`, which are authoritative over any analysis-sidecar relocation
total (see the count-provenance note below). The confirmed figures:

| Quantity | Value | Status |
|---|---:|---|
| Function-pointer tables | 40,313 | CERTAIN |
| Total pointer entries | 516,323 | CERTAIN |
| Section split | 38,664 / 1,442 / 207 | CERTAIN |
| `.data` composition | 833 pfm + 585 UFB (+ ~24 misc) | CERTAIN |
| Switch jump tables | 33,016 | CERTAIN |
| Switch case targets | 4,673,757 | CERTAIN |
| Largest switch | 40,813 cases | CERTAIN |
| size-23 tables / with Model / without | 6,129 / 6,050 / 79 | CERTAIN |
| Tables with ≥1 Model entry | 6,070 | CERTAIN |
| "vtable for" RTTI records | 39,155 | CERTAIN |
| `R_X86_64_RELATIVE` relocations (`DT_RELACOUNT`) | 1,069,006 | CERTAIN |
| Total relocation entries (all types) | 1,069,659 | CERTAIN |
| `.data.rel.ro` relocations (all types / `RELATIVE`) | 924,033 / 924,015 | CERTAIN |
| `0x223393a0` / `0x21c1d590` / `0x21e0d0a0` entries | 2,595 / 447 / 674 | CERTAIN |

> **Note (count provenance):** relocation totals must come from `readelf -dW` /
> `readelf -rW` on the binary, not from an analysis sidecar. The authoritative
> counts are `DT_RELACOUNT` = **1,069,006** `R_X86_64_RELATIVE` relocations and
> **1,069,659** relocation entries across all types. A sidecar figure of 1,069,603
> over-counts the `RELATIVE` set by 597 and under-counts the all-types total by 56,
> matching neither. The two section anchors do reproduce from the binary —
> 924,033 relocations in `.data.rel.ro` and 131,596 in `.data` (all-type counts;
> 924,015 / 131,590 of those are `RELATIVE`). The vtable count (39,155) and every
> table figure on this page are derived from the deduped symbol table, not a
> decompile-tree grep.

Not yet resolved: per-table demangling of the 157 Class Z residual (recoverable by
`.text` address-band matching, not symbols); slot-level semantic labelling of
every vtable (done only for the major hierarchies on the sibling pages); and the
exact Class E encoder↔opcode ratio.

---

## Related Components

| Population | Relationship |
|---|---|
| 39,155 "vtable for" RTTI records | The C++ vtable backbone — ~97% of all 40,313 tables |
| 1,069,006 `R_X86_64_RELATIVE` relocations | The loader-fill mechanism that populates the zero-in-file slots |
| 33,016 switch jump tables | The separate computed-goto dispatch population |
| 6,070 Op-Model tables | The MLIR op-registration surface (Class A) |

## Cross-References

- [Binary Forensics Overview](overview.md) — the section map and the place of the table/switch populations in the whole binary.
- [ELF Anatomy](elf-anatomy.md) — `.data.rel.ro` / `.data` / `.rodata` / `.got` section bounds and the PIE relocation model that fills the vtable slots.
- [RTTI / Vtable Census](rtti-vtable-census.md) — owns the per-slot method labelling, the RTTI→vtable binding chain, and the 39,155 "vtable for" detail referenced by Classes A/E/N/G.
- [Polymorphic Entry Points](polymorphic-entry-points.md) — the thunk-table forwarding-stub class and how relocated slots point at trampolines.
- [Per-Generation Function Dispatcher](per-gen-function-dispatcher.md) — the vtable-family mechanism behind Classes N and G (cost model, codec, Target installation).
- [PJRT_Api Function-Pointer Table Reconstruction](../pjrt/api-vtable-reconstruction.md) — the 23-slot `PjRtDevice` vtables that are among the 79 non-Model size-23 tables.
- [Dispatch-Table Taxonomy — Full Catalog](../appendix/dispatch-table-taxonomy-full.md) — the exhaustive per-table listing behind this page's class summary.
