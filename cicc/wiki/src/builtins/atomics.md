# Atomic Operations Builtins

Atomic builtins constitute the largest and most complex category in the NVVM builtin system, spanning over 130 IDs across two distinct subsystems: the legacy NVVM intrinsic atomics (IDs 207--275, 370--379) and the C++11-model atomics (IDs 366, 417--473). Both families converge in the lowering layer at `sub_12AE930` (EDG) / `sub_9502D0` (NVVM), a 1495-line handler that generates inline PTX assembly with explicit memory ordering and scope annotations.

## Two Atomic Subsystems

The compiler maintains two parallel atomic APIs that reflect CUDA's historical evolution. The legacy NVVM atomics (`__nvvm_atom_*`) predate the C++ memory model and encode scope directly in the builtin name (e.g., `__nvvm_atom_cta_add_gen_i` for block-scoped integer add). The C++11 atomics (`__nv_atomic_*`) accept ordering and scope as runtime parameters, matching the `cuda::atomic_ref` interface.

Both subsystems lower to identical PTX instructions. The distinction matters only during the EDG frontend phase, where `sub_6BBC40` generates the mangled `__nv_atomic_*` names from C++ source, and the NVVM lowering layer `sub_12B3FD0` dispatches them by ID.

## Legacy NVVM Atomics (IDs 207--275)

These 69 builtins encode the operation, scope, and type directly in the name. The lowering dispatches through `sub_12AA9B0` for exchange-style operations and `sub_12ADE80` for load/store/fetch operations. Each operation exists in three scope variants: default (device), `_cta_` (block), and `_sys_` (system).

| ID Range | Operation | Builtin Pattern | PTX Mnemonic |
|---|---|---|---|
| 207--218 | Add | `__nvvm_atom_{,cta_,sys_}add_gen_{i,ll,f,d}` | `atom.add` |
| 219--227 | Exchange | `__nvvm_atom_{,cta_,sys_}xchg_gen_{i,ll,128}` | `atom.exch` |
| 228--251 | Min/Max | `__nvvm_atom_{,cta_,sys_}{min,max}_gen_{i,ll,ui,ull}` | `atom.min` / `atom.max` |
| 252--257 | Inc/Dec | `__nvvm_atom_{,cta_,sys_}{inc,dec}_gen_ui` | `atom.inc` / `atom.dec` |
| 258--275 | Bitwise | `__nvvm_atom_{,cta_,sys_}{and,or,xor}_gen_{i,ll}` | `atom.and` / `atom.or` / `atom.xor` |

### Legacy CAS (IDs 370--379)

Compare-and-swap builtins include 128-bit variants for SM 70+ targets. The handler `sub_12AA280` builds an `AtomicCmpXchg` IR node with acquire ordering on both success and failure paths and weak exchange semantics.

| ID Range | Operation | Builtin Pattern |
|---|---|---|
| 370--379 | CAS | `__nvvm_atom_{,cta_,sys_}cas_gen_{i,ll,us,128}` |

### Half-Precision Atomics (IDs 459--468)

Added for SM 90+ (Hopper), these support `f16x2` and `f16x4` packed atomic adds:

| ID Range | Operation | Builtin Pattern | SM Gate |
|---|---|---|---|
| 459--461 | f16x2 add | `__nvvm_atom_{,cta_,sys_}add_gen_f2` | SM 90+ |
| 466--468 | f16x4 add | `__nvvm_atom_{,cta_,sys_}add_gen_f4` | SM 100+ (Blackwell) |

## C++11 Atomics (IDs 366, 417--473)

These 57 builtins implement the CUDA C++ atomic model with explicit memory ordering and scope parameters. The EDG frontend generator at `sub_6BBC40` constructs the mangled names using a `__nv_atomic_fetch_{op}_{width}_{type}` pattern, where width is the byte count (1, 2, 4, 8, or 16) and the type suffix is `_u` (unsigned), `_s` (signed), or `_f` (float).

### Thread Fence (ID 366)

`__nv_atomic_thread_fence` emits either a volatile fence (SM <= 69) or an explicit `fence.{ordering}.{scope};` PTX instruction (SM 70+). Ordering and scope are extracted from constant operand parameters at compile time.

### Load/Store (IDs 417--428)

| ID | Builtin | Width | PTX |
|---|---|---|---|
| 417 | `__nv_atomic_load` | generic | `ld.{ordering}.{scope}.{type}` |
| 418--422 | `__nv_atomic_load_{1,2,4,8,16}` | 1--16 bytes | same |
| 423 | `__nv_atomic_store` | generic | `st.{ordering}.{scope}.{type}` |
| 424--428 | `__nv_atomic_store_{1,2,4,8,16}` | 1--16 bytes | same |

### Fetch-Op (IDs 429--458)

Arithmetic and bitwise fetch operations are registered with width and type suffixes. Bitwise operations (and, or, xor) omit the type suffix since signedness is irrelevant for bitwise logic.

| ID Range | Operation | Builtin Pattern |
|---|---|---|
| 429--434 | fetch_add | `__nv_atomic_fetch_add_{4,8}_{u,s,f}` |
| 435--440 | fetch_sub | `__nv_atomic_fetch_sub_{4,8}_{u,s,f}` |
| 441--446 | fetch_and/or/xor | `__nv_atomic_fetch_{and,or,xor}_{4,8}` |
| 447--452 | fetch_max | `__nv_atomic_fetch_max_{4,8}_{u,s,f}` |
| 453--458 | fetch_min | `__nv_atomic_fetch_min_{4,8}_{u,s,f}` |

For `fetch_sub` with floating-point types (IDs 437, 440), the lowering negates the operand and emits `atom.add` rather than a dedicated subtraction instruction.

### Exchange and CAS (IDs 462--473)

| ID Range | Operation | Builtin Pattern |
|---|---|---|
| 462--465 | Exchange | `__nv_atomic_exchange{,_4,_8,_16}` |
| 469--473 | CAS | `__nv_atomic_compare_exchange{,_2,_4,_8,_16}` |

## PTX Inline Assembly Generation

The atomic codegen handler at `sub_12AE930` (address `0x12AE930`, 41KB) generates PTX inline assembly strings at compile time. The generated instruction format depends on the target SM:

**Pre-SM 70** (volatile mode, `unk_4D045E8 <= 0x45`):
```
ld.volatile.b32 $0, [$1];
atom.add.volatile.u32 $0, [$1], $2;
```

**SM 70+** (explicit memory model):
```
ld.acquire.gpu.b32 $0, [$1];
st.release.sys.b32 [$0], $1;
atom.add.acq_rel.cta.u32 $0, [$1], $2;
atom.cas.relaxed.gpu.b64 $0, [$1], $2, $3;
```

### Memory Ordering Encoding

The ordering parameter (values 0--5) maps to PTX qualifiers:

| Value | Ordering | Used For |
|---|---|---|
| 0 | `relaxed` | Default / monotonic |
| 1, 2 | `acquire` | Loads, RMW |
| 3 | `release` | Stores |
| 4 | `acq_rel` | RMW operations |
| 5 | `acquire` | Sequential consistency (downgraded) |

### Scope Encoding

The scope parameter (values 0--4) maps to PTX scope qualifiers:

| Value | Scope | PTX | SM Requirement |
|---|---|---|---|
| 0, 1 | Block | `.cta` | All |
| 2 | Cluster | `.cluster` | SM 90+ (Hopper); falls back to `.gpu` on SM <= 89 |
| 3 | Device | `.gpu` | All |
| 4 | System | `.sys` | All |

### Type Suffix Construction

The type suffix is built from a 4-entry table: `b` (bitwise), `u` (unsigned), `s` (signed), `f` (float). Combined with the byte size, this produces suffixes like `.u32`, `.f64`, `.b128`. Valid sizes are validated against the bitmask `0x10116` (bits for 1, 2, 4, 8, and 16 bytes).

## Architecture Gates

| SM Threshold | Effect |
|---|---|
| SM <= 59 | Diagnostic `0xEB6` warning for certain atomic patterns |
| SM <= 69 | Volatile mode; 128-bit atomics not supported (diagnostic `0xEB4`) |
| SM 70+ | Explicit ordering/scope in PTX output |
| SM <= 89 | Half-precision (2-byte FP) atomics not supported |
| SM 90+ (Hopper) | Cluster scope (`.cluster`) becomes available |
| SM 100+ (Blackwell datacenter) | `f16x4` packed atomic add (IDs 466--468) |

## EDG Frontend Name Construction

The EDG atomic builtin generator `sub_6BBC40` (address `0x6BBC40`, 1251 lines) constructs internal function names from C++ `cuda::atomic_ref` calls. The algorithm appends a width suffix (`_%u` from the type size at `type_node+128`) and, for arithmetic operations, a type suffix (`_s`, `_u`, or `_f`) determined by the type kind at `type_node+140` and a 256-entry signedness lookup table at `byte_4B6DF90`.

Key validation diagnostics emitted during name construction:

| Diagnostic | Hex | Condition |
|---|---|---|
| 3748 | `0xEA4` | `fetch_op` type size not 4 or 8 bytes |
| 3764 | `0xEB4` | 128-bit load/store on unsupported SM |
| 3765 | `0xEB5` | 16-bit store on SM <= 69 |
| 3767 | `0xEB7` | Type size not in {1, 2, 4, 8, 16} |
