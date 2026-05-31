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
```ptx
ld.volatile.b32 $0, [$1];
atom.add.volatile.u32 $0, [$1], $2;
```

**SM 70+** (explicit memory model):
```ptx
ld.acquire.gpu.b32 $0, [$1];
st.release.sys.b32 [$0], $1;
atom.add.acq_rel.cta.u32 $0, [$1], $2;
atom.cas.relaxed.gpu.b64 $0, [$1], $2, $3;
```

### The sub_12AE930 / sub_9502D0 Algorithm in Detail

Both the EDG-side handler (`sub_12AE930`, 0x12AE930) and its NVVM-side twin (`sub_9502D0`, 0x9502D0) follow identical logic. They accept five parameters: `(result, codegen_state, builtin_id, call_arg_list, type_info)`. The algorithm proceeds in six phases.

#### Phase 1: SM Version Check and Path Selection

```c
v186 = (unk_4D045E8 <= 0x45)    // SM <= 69 -> volatile mode
```

When `v186` is true, the handler enters the pre-SM 70 "volatile" path. All atomic operations receive a `.volatile` qualifier instead of explicit memory ordering and scope qualifiers. The 128-bit atomics emit diagnostic `0xEB6` (3766) and are rejected entirely.

When `v186` is false (SM 70+), the handler enters the memory model path, which constructs the full `{mnemonic}.{ordering}.{scope}.{type}` format.

#### Phase 2: Operand Extraction and Builtin ID Dispatch

The handler extracts between 2 and 5 operands from the call argument list (pointer, value, compare-value for CAS, plus the ordering and scope parameters encoded as compile-time constants). The builtin ID selects the PTX mnemonic via a switch:

```c
switch (builtin_id) {
    case 417..422:  mnemonic = "ld";          // atomic load
    case 423..428:  mnemonic = "st";          // atomic store
    case 429..434:  mnemonic = "atom.add";    // fetch-add (unsigned, signed, float)
    case 435..440:  mnemonic = "atom.add";    // fetch-sub (negated; see below)
    case 441..442:  mnemonic = "atom.and";    // fetch-and
    case 443..444:  mnemonic = "atom.or";     // fetch-or
    case 445..446:  mnemonic = "atom.xor";    // fetch-xor
    case 447..452:  mnemonic = "atom.max";    // fetch-max
    case 453..458:  mnemonic = "atom.min";    // fetch-min
    case 462..465:  mnemonic = "atom.exch";   // exchange
    case 469..473:  mnemonic = "atom.cas";    // compare-and-swap
    default:        fatal("unexpected atomic builtin function");
}
```

For IDs 435--440 (`fetch_sub`), the handler does not emit `atom.sub` (which does not exist in PTX). Instead, for integer types it negates the operand and emits `atom.add`; for float types it negates via `fneg` and emits `atom.add.f`.

For thread fence (ID 366), the handler branches to `sub_12AE0E0` (volatile fence, pre-SM 70) or `sub_12AE4B0` (explicit fence, SM 70+) and returns immediately, bypassing the rest of the atomic pipeline.

#### Phase 3: Memory Ordering Resolution

The ordering parameter is extracted from the first constant operand of the C++11 atomic call via `sub_620EE0`. The value (0--5) maps to a PTX qualifier string:

| Value | C++ Ordering | PTX Qualifier | Applies To |
|---|---|---|---|
| 0 | `relaxed` / monotonic | `relaxed` | All operations |
| 1 | `consume` (treated as acquire) | `acquire` | Loads, RMW |
| 2 | `acquire` | `acquire` | Loads, RMW |
| 3 | `release` | `release` | Stores, RMW |
| 4 | `acq_rel` | `acq_rel` | RMW operations |
| 5 | `seq_cst` | `acquire` (loads), `release` (stores) | All |

Sequential consistency (value 5) is *downgraded*: loads get `acquire`, stores get `release`, and RMW operations get `acq_rel`. True `seq_cst` semantics are achieved by inserting explicit fences around the operation (see "Fence Insertion for Seq_Cst" below).

**Store-specific validation.** For store builtins (IDs 423--428), only ordering values 0, 3, and 5 are legal. Any other value triggers `fatal("unexpected memory order.")`. Value 5 is treated as `relaxed` for the store instruction itself, with the seq_cst fence handling the ordering guarantee externally.

**Load-specific validation.** For load builtins (IDs 417--422), values 3 (release) and 4 (acq_rel) are illegal and trigger the same fatal error.

#### Phase 4: Scope Resolution

The scope parameter is extracted from the second constant operand via `sub_620EE0`. The value (0--4) maps to a PTX scope qualifier:

```c
switch (scope_value) {
    case 0:  // fall through
    case 1:  scope_str = "cta";      break;   // thread block
    case 2:
        if (unk_4D045E8 > 0x59)               // SM > 89
            scope_str = "cluster";             // SM 90+ (Hopper)
        else
            scope_str = "gpu";                 // SM <= 89: fallback
        break;
    case 3:  scope_str = "gpu";      break;   // device
    case 4:  scope_str = "sys";      break;   // system
    default: fatal("unexpected atomic operation scope.");
}
```

The cluster scope fallback is the critical SM gate at line 255 / 424 of `sub_12AE930` / `sub_9502D0`: when the SM version is 89 or below, scope value 2 ("cluster") silently degrades to `gpu`. No diagnostic is emitted; the scope is simply rewritten. On SM 90+ (Hopper and later), `cluster` passes through to the PTX output.

#### Phase 5: Type Suffix Construction

The type suffix is built from two components: a type-class letter and a byte-width number. The type-class lookup uses a 4-entry table stored in local variable `v196`:

```c
v196[0] = 'b'    // bitwise   (for exch, and, or, xor, cas)
v196[1] = 'u'    // unsigned  (for add, inc, dec, max, min on unsigned)
v196[2] = 's'    // signed    (for max, min on signed)
v196[3] = 'f'    // float     (for add on float/double)
```

The type-class index is derived from the LLVM type of the atomic operand:

- Integer type with unsigned semantics: index 1 (`u`)
- Integer type with signed semantics: index 2 (`s`)
- Floating-point type: index 3 (`f`)
- All other cases (exchange, CAS, bitwise): index 0 (`b`)

The byte-width is the size of the atomic operand in bytes. Valid sizes are validated against the bitmask `0x10116`:

```c
valid = ((1LL << byte_size) & 0x10116) != 0
```

This bitmask has bits set at positions 1, 2, 4, 8, and 16, accepting exactly the byte widths {1, 2, 4, 8, 16}. Any other size triggers `fatal("unexpected size1")`.

The resulting suffix is the letter concatenated with the bit width (byte_size * 8): `.u32`, `.s64`, `.f32`, `.b128`, etc.

#### Phase 6: Inline ASM String Assembly and Emission

The handler assembles the final PTX string by concatenating the components. Two string buffers are maintained throughout: `v190` (ordering string) and `v193` (scope string), set during phases 3 and 4.

**For SM 70+ (memory model mode):**
```c
// Loads:
sprintf(buf, "ld.%s.%s.%c%d $0, [$1];", v190, v193, type_letter, bit_width);
// Stores:
sprintf(buf, "st.%s.%s.%c%d [$0], $1;", v190, v193, type_letter, bit_width);
// RMW atomics:
sprintf(buf, "%s.%s.%s.%c%d $0, [$1], $2;", mnemonic, v190, v193, type_letter, bit_width);
// CAS:
sprintf(buf, "%s.%s.%s.%c%d $0, [$1], $2, $3;", mnemonic, v190, v193, type_letter, bit_width);
```

**For pre-SM 70 (volatile mode):**
```c
// Loads:
sprintf(buf, "ld.volatile.%c%d $0, [$1];", type_letter, bit_width);
// RMW atomics:
sprintf(buf, "%s.volatile.%c%d $0, [$1], $2;", mnemonic, type_letter, bit_width);
```

**Constraint string construction.** The LLVM inline ASM constraint string is built dynamically to match the operand pattern:

| Pattern | Constraint String | Meaning |
|---|---|---|
| Load (`ld`) | `"=r,l,~{memory}"` or `"=l,l,~{memory}"` | result in reg, address in 64-bit reg, memory clobber |
| Store (`st`) | `"l,r,~{memory}"` or `"l,l,~{memory}"` | address in 64-bit reg, value in reg, memory clobber |
| RMW (`atom.*`) | `"=r,l,r,~{memory}"` | result, address, operand, memory clobber |
| CAS (`atom.cas`) | `"=r,l,r,r,~{memory}"` | result, address, compare, swap, memory clobber |

The register class for result and value operands is `r` for 32-bit types and `l` for 64-bit types. 128-bit types use `l` with pair operands.

The assembled PTX string and constraint string are passed to `sub_B41A60` (NVVM side) or the equivalent EDG-side helper, which creates an LLVM `InlineAsm` node. The node is then emitted via `sub_921880` / `sub_1285290`.

### Fence Insertion for Seq_Cst

When the memory ordering is sequential consistency (value 5) and the SM version supports explicit fences (SM 70+), the handler does not simply emit `atom.sc.{scope}`. Instead, it implements seq_cst through a fence-bracketed pattern:

1. **Pre-fence:** If the operation is a store or RMW and ordering >= release, the handler calls `sub_94F9E0` (membar) or `sub_94FDF0` (fence) to emit a leading fence:
   - `sub_94F9E0` emits `membar.{scope};` as inline PTX
   - `sub_94FDF0` emits `fence.sc.{scope};` or `fence.acq_rel.{scope};`

2. **The atomic operation:** Emitted with downgraded ordering (`acquire` for loads, `release` for stores, `acq_rel` for RMW).

3. **Post-fence:** If the operation is a load or RMW and ordering >= acquire, a trailing fence is emitted.

The fence scope matches the atomic operation's scope. The decision to emit membar vs fence depends on the SM version and the specific ordering level: membar is used for the pre-SM 70 path (though that path should not reach this code), and fence.sc / fence.acq_rel for SM 70+.

The pre/post-fence logic is gated by two conditions in the NVVM-side handler:
```c
PRE-FENCE:  if (v186 && (v187 - 3) <= 2)    // v187 is ordering; range [3,5] = release, acq_rel, seq_cst
POST-FENCE: if (!v175 && v169 == 5)          // v175 = is_store; v169 = ordering = seq_cst
```

### The Volatile Fence Handler (sub_12AE0E0)

For thread fence on SM <= 69, `sub_12AE0E0` emits a volatile memory barrier. The function takes an ASM buffer and fence configuration parameters. It produces:

```ptx
membar.{scope};
```

where the scope is derived from the fence's scope parameter (cta / gl / sys). This is the pre-memory-model equivalent of the explicit fence path.

### The Explicit Fence Handler (sub_12AE4B0)

For thread fence on SM 70+, `sub_12AE4B0` constructs an explicit `fence.{ordering}.{scope};` instruction. The ordering for fences is a restricted set compared to atomics:

| Ordering Value | Fence Qualifier |
|---|---|
| 3 | `sc` (sequentially consistent) |
| 4 | `acq_rel` |
| 5 | `sc` (same as 3) |
| Other | `fatal("unexpected memory order.")` |

The scope string follows the same rules as atomics. The assembled string is emitted as LLVM inline ASM with a `~{memory}` clobber.

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

## The 13 Atomic Operations at PTX Emission

The PTX emission layer at `sub_21E5E70` (base) and `sub_21E6420` (L2-hinted) implements the final encoding from the NVPTX MachineInstr opcode to the PTX text. The instruction operand word at this stage encodes both scope and operation:

```text
bits[7:4]    — scope:  0 = gpu (default), 1 = cta, 2 = sys
bits[23:16]  — atomic operation opcode (BYTE2)
```

The 13-entry dispatch table:

| Opcode | PTX Suffix | L2-Hinted Suffix | Description |
|---|---|---|---|
| 0x00 | `.exch.b` | `.exch.L2::cache_hint.b` | Bitwise exchange |
| 0x01 | `.add.u` | `.add.L2::cache_hint.u` | Unsigned add |
| 0x02 | *(missing)* | *(missing)* | No `.add.s` in PTX ISA |
| 0x03 | `.and.b` | `.and.L2::cache_hint.b` | Bitwise AND |
| 0x04 | *(missing)* | *(missing)* | Unused slot |
| 0x05 | `.or.b` | `.or.L2::cache_hint.b` | Bitwise OR |
| 0x06 | `.xor.b` | `.xor.L2::cache_hint.b` | Bitwise XOR |
| 0x07 | `.max.s` | `.max.L2::cache_hint.s` | Signed max |
| 0x08 | `.min.s` | `.min.L2::cache_hint.s` | Signed min |
| 0x09 | `.max.u` | `.max.L2::cache_hint.u` | Unsigned max |
| 0x0A | `.min.u` | `.min.L2::cache_hint.u` | Unsigned min |
| 0x0B | `.add.f` | `.add.L2::cache_hint.f` | Float add |
| 0x0C | `.inc.u` | `.inc.L2::cache_hint.u` | Unsigned increment |
| 0x0D | `.dec.u` | `.dec.L2::cache_hint.u` | Unsigned decrement |
| 0x0E | `.cas.b` | `.cas.L2::cache_hint.b` | Compare-and-swap |

Opcodes 0x02 and 0x04 are unoccupied. There is no signed atomic add in PTX (signed add uses `.add.u` since two's-complement wrapping is identical). Slot 0x04 is simply skipped.

The scope prefix is emitted before the operation suffix:

```text
bits[7:4] & 0xF:
    0  ->  (nothing; implicit .gpu scope)
    1  ->  ".cta"
    2  ->  ".sys"
```

Full PTX emission format:

```ptx
atom[.scope].{op}.{type}{size}
```

Example: `atom.cta.add.u32`, `atom.sys.cas.b64`, `atom.exch.b32`.

## L2 Cache Hint System (SM 80+ / Ampere)

`sub_21E6420` (address `0x21E6420`) is a parallel version of the base atomic emitter `sub_21E5E70`. It inserts `.L2::cache_hint` between the operation and type suffix for all 13 atomic operations:

```ptx
atom[.scope].{op}.L2::cache_hint.{type}{size}
```

The L2 cache hint instructs the GPU's L2 cache to retain (or evict) the atomic target data after the operation completes. This is a PTX 7.3+ feature introduced with Ampere (SM 80+).

The L2-hinted path is selected when bit 0x400 is set in the instruction's encoding flags. The hint is applied at the MachineInstr level during instruction selection, not during the inline ASM generation phase of `sub_12AE930`. Both paths produce identical scope and type encoding; the L2 path adds exactly the `.L2::cache_hint` substring.

String emission uses SSE (`xmm`) register loads from precomputed constant data at addresses `xmmword_435F590` through `xmmword_435F620` to fast-copy the 16-byte prefix of each operation string, then patches the remaining bytes. This avoids branch-heavy string concatenation for the 13 cases.

## AtomicExpandPass: IR-Level Expansion (sub_20C9140)

Before `sub_12AE930` handles the C++11 atomics, and separately from the legacy builtin lowering, an LLVM `FunctionPass` named `"Expand Atomic instructions"` (pass ID `"atomic-expand"`, registered at `sub_20CA900`) runs on LLVM IR to decide which atomic operations the NVPTX target can handle natively and which must be expanded into CAS loops.

### Expansion Decision Tree

For each atomic instruction in the function:

1. **shouldExpandAtomicCmpXchgInIR** (vtable +0x258): Default expands all `cmpxchg` to LL/SC or CAS-based loops. The NVPTX override may keep native i32/i64 `cmpxchg` on SM 70+.

2. **shouldExpandAtomicRMWInIR** (vtable +0x280):
   - i32 `xchg`/`add`/`min`/`max`: kept native on all SM.
   - i64 `xchg`/`add`: kept native on SM 70+.
   - i32/i64 `sub`/`nand`: always expanded to CAS loop (no native PTX instruction).
   - i8/i16 (any operation): always expanded via partword masking.
   - Float `atomicAdd`: native on SM 70+ (fp32), SM 80+ (fp16/bf16).

3. **shouldExpandAtomicLoadInIR** (vtable +0x270): Native for aligned i32/i64. Expanded for i8/i16 (widen to i32 load + extract) and i128+ (decompose to multiple loads).

4. **shouldExpandAtomicStoreInIR** (vtable +0x278): Native for aligned i32/i64. Expanded for sub-word and >64-bit types.

### Sub-Word Atomic Expansion (sub_20CB200)

No NVIDIA GPU architecture through SM 120 supports native sub-word (i8/i16) atomics. The pass generates mask-and-shift wrappers around word-sized CAS loops. The mask generation function `sub_20CB200` (2,896 bytes native) produces a 6-field output struct:

| Field | Name | Purpose |
|---|---|---|
| +0x00 | `AlignedAddr` | Pointer masked to word boundary: `ptr & ~(word_size - 1)` |
| +0x08 | `AlignedType` | Always i32 |
| +0x10 | `PtrLSB` | Low address bits: `ptr & (word_size - 1)` |
| +0x18 | `ShiftAmt` | Bit position within the word: `PtrLSB * 8` (little-endian) |
| +0x20 | `Inv_Mask` | Inverted mask: `~(((1 << (type_size * 8)) - 1) << ShiftAmt)` |
| +0x28 | `Mask` | Mask: `(1 << (type_size * 8)) - 1` |

The CAS loop (`sub_20CBD50`, 1646 bytes) then:
1. Shifts the new value into position: `ValOperand_Shifted = new_val << ShiftAmt`.
2. Loops: loads the word, applies the RMW operation on the masked sub-word, attempts CAS on the full word.
3. On success: extracts the sub-word result via shift + mask.

### CAS Loop Generation (sub_20C96A0)

For operations that cannot be handled natively, the pass builds a compare-and-swap loop with three basic blocks:

```text
entry -> "atomicrmw.start" -> (CAS failure) -> "atomicrmw.start" (retry)
                           -> (CAS success) -> "atomicrmw.end"
```

Steps:
1. Load current value from pointer.
2. Compute new value using the RMW operation (dispatched through an 11-case switch at `sub_20CC690`: Xchg, Add, Sub, And, Or, Xor [implied], Nand, Max, Min, UMax, UMin, FMin, FMax).
3. Emit `cmpxchg` with packed success+failure orderings.
4. Branch back to start on failure, fall through to end on success.

### Ordering-to-Fence Table (address 0x428C1E0)

The pass uses a 7-entry fence decision table indexed by LLVM `AtomicOrdering` enum:

| Ordering | Index | Release Fence Before? | Acquire Fence After? |
|---|---|---|---|
| NotAtomic | 0 | No | No |
| Unordered | 1 | No | No |
| Monotonic | 2 | No | No |
| Acquire | 3 | No | Yes |
| Release | 4 | Yes | No |
| AcquireRelease | 5 | Yes | Yes |
| SequentiallyConsistent | 6 | Yes | Yes (+ barrier) |

Fence emission calls `sub_15F9C80` which creates an LLVM `fence` instruction with the specified ordering and sync scope.

## Memory Barrier and Fence Emission

The PTX emission layer has two dedicated handlers for barriers and fences, separate from the atomic operation emitters.

### Memory Barrier (sub_21E94F0)

Emits `membar` instructions based on a 4-bit operand encoding:

| Value | Instruction | Scope |
|---|---|---|
| 0 | `membar.gpu` | Device |
| 1 | `membar.cta` | Thread block |
| 2 | `membar.sys` | System |
| 4 | `fence.sc.cluster` | Cluster (SM 90+) |
| 3 | `fatal("Bad membar op")` | Invalid |

### NVVM-Side Membar (sub_94F9E0)

At the NVVM lowering level, `sub_94F9E0` handles membar emission with a different scope encoding:

| Scope Value | Scope String | PTX Instruction |
|---|---|---|
| 0, 1 | `cta` | `membar.cta;` |
| 2, 3 | `gl` | `membar.gl;` |
| 4 | `sys` | `membar.sys;` |
| Other | | `fatal("unexpected atomic operation scope.")` |

### NVVM-Side Fence (sub_94FDF0)

Constructs `fence.{ordering}.{scope};` from a state array. The ordering mapping is:

| Value | Ordering String |
|---|---|
| 3 | `sc` |
| 4 | `acq_rel` |
| 5 | `sc` |
| Other | `fatal("unexpected memory order.")` |

Both membar and fence are emitted as inline PTX assembly (not LLVM IR fence instructions) because PTX-level memory ordering semantics have no direct LLVM IR equivalent at the precision NVIDIA requires.

## Architecture Gates

| SM Threshold | Effect |
|---|---|
| SM <= 59 | Diagnostic `0xEB6` warning for certain atomic patterns |
| SM 60--69 | Diagnostic `0xEB2` (3762) for specific atomic patterns |
| SM <= 69 | Volatile mode; 128-bit atomics not supported (diagnostic `0xEB4`) |
| SM 70+ | Explicit ordering/scope in PTX output |
| SM <= 89 | Scope value 2 silently falls back from `cluster` to `gpu` |
| SM <= 89 | Half-precision (2-byte FP) atomics not supported |
| SM 90+ (Hopper) | Cluster scope (`.cluster`) becomes available |
| SM 90+ | `f16x2` packed atomic add (IDs 459--461) |
| SM 90+ | `fence.sc.cluster` becomes available |
| SM 100+ (Blackwell datacenter) | `f16x4` packed atomic add (IDs 466--468) |

## EDG Frontend Name Construction

The EDG atomic builtin generator `sub_6BBC40` (address `0x6BBC40`, 1251 lines) constructs internal function names from C++ `cuda::atomic_ref` calls. The algorithm uses a dispatch key `v165 = *(uint16_t*)(type_node + 176)`, the EDG "builtin kind" tag, to select the operation:

| v165 (hex) | v165 (dec) | Operation |
|---|---|---|
| 0x6241, 0x6242 | 25153, 25154 | compare_exchange |
| 0x6248, 0x6249 | 25160, 25161 | exchange |
| 0x624F, 0x6250 | 25167, 25168 | fetch_add |
| 0x6257, 0x6258 | 25175, 25176 | fetch_sub |
| 0x625F, 0x6260 | 25183, 25184 | fetch_and |
| 0x6263, 0x6264 | 25187, 25188 | fetch_xor |
| 0x6267, 0x6268 | 25191, 25192 | fetch_or |
| 0x626B, 0x626C | 25195, 25196 | fetch_max |
| 0x6273, 0x6274 | 25203, 25204 | fetch_min |
| 0x627B, 0x627C | 25211, 25212 | load |
| 0x6280, 0x6281 | 25216, 25217 | store |
| 0x6286 | 25222 | thread_fence |

Within each pair, the odd ID is the "generic" overload that enters the renaming path; the even ID has its base name string set explicitly via `strcpy`.

### Name Construction Algorithm (lines 877--996 of sub_6BBC40)

**Step 1 -- Base name.** Copy the EDG source name, then overwrite with the canonical base for the seven fetch-op builtins:

```text
v165     Base name
------   ---------------------------
0x6250   "__nv_atomic_fetch_add"
0x6258   "__nv_atomic_fetch_sub"
0x6260   "__nv_atomic_fetch_and"
0x6264   "__nv_atomic_fetch_xor"
0x6268   "__nv_atomic_fetch_or"
0x626C   "__nv_atomic_fetch_max"
0x6274   "__nv_atomic_fetch_min"
```

**Step 2 -- Width suffix.** Append `"_%u"` formatted with the type size in bytes from `*(uint32_t*)(type_node + 128)`. For fetch-op builtins, the size is validated as `(type_size - 4) <= 4`, accepting only 4 and 8 bytes.

**Step 3 -- Type suffix** (only for add/sub/max/min; lines 960--996). Reads `type_kind = *(uint8_t*)(type_node + 140)`:

| type_kind | Meaning | Suffix | Condition |
|---|---|---|---|
| 2 | integer | `_s` | `byte_4B6DF90[signedness_byte] != 0` (signed) |
| 2 | integer | `_u` | `byte_4B6DF90[signedness_byte] == 0` (unsigned) |
| 3 | float | `_f` | Always |
| 6 | unsigned explicit | `_u` | Always |

`byte_4B6DF90` is a 256-entry lookup table that maps the EDG "integer kind" sub-tag (at `type_node + 160`) to a boolean: 1 = signed, 0 = unsigned.

Bitwise operations (and/or/xor) omit the type suffix entirely.

### Naming Pattern Summary

```text
__nv_atomic_fetch_{op}_{width}[_{type}]

{op}    = add | sub | and | xor | or | max | min
{width} = 4 | 8  (bytes)
{type}  = _s (signed), _u (unsigned), _f (float), or omitted (bitwise)
```

For load/store/exchange/compare_exchange, only the width suffix is appended; no type suffix.

### Validation Diagnostics

| Diagnostic | Hex | Condition |
|---|---|---|
| 852 | `0x354` | Unsupported atomic operation for target |
| 1645 | `0x66D` | Wrong return type for builtin |
| 1646 | `0x66E` | Unsupported type size (not in {1,2,4,8,16}) |
| 3745 | `0xEA1` | Atomic not supported for given type |
| 3746 | `0xEA2` | First param scope exceeds range (>5) |
| 3747 | `0xEA3` | Return param scope exceeds range (>4) |
| 3748 | `0xEA4` | `fetch_op` type size not 4 or 8 bytes |
| 3749 | `0xEA5` | Store with type_size <= 1 (too small) |
| 3750 | `0xEA6` | Load with type_size > 3 (too large) |
| 3756 | `0xEAC` | CAS parameter type mismatch |
| 3757 | `0xEAD` | Exchange parameter type mismatch |
| 3759 | `0xEAF` | Float return not supported below SM 90 |
| 3762 | `0xEB2` | SM 60--69 atomic variant diagnostic |
| 3763 | `0xEB3` | Return type on store (SM <= 89) |
| 3764 | `0xEB4` | 128-bit store/load not supported on this SM |
| 3765 | `0xEB5` | 16-bit store not supported on SM <= 69 |
| 3766 | `0xEB6` | Generic warning for SM <= 59 |
| 3767 | `0xEB7` | Type size not in {1,2,4,8,16} bitmask |
| 3769 | `0xEB9` | Null argument list error |

## EDG Type Node Field Map

| Offset | Size | Field |
|---|---|---|
| +128 | 8 | `type_size` (byte count: 1, 2, 4, 8, 16) |
| +140 | 1 | `type_kind` (0=void, 2=integer, 3=float, 6=unsigned, 8=pointer, 12=typedef) |
| +160 | varies | For type_kind 12 (typedef): pointer to underlying type. For type_kind 2 (integer): uint8_t signedness sub-tag indexed into `byte_4B6DF90`. |
| +168 | 8 | Pointer chain (for struct/compound types) |
| +176 | 2 | `builtin_kind` (the v165 dispatch tag, uint16_t) |

## NVPTX MachineInstr Atomic Opcodes

At the SelectionDAG / MachineInstr level, atomic operations map to NVPTX-specific opcodes distinct from the inline ASM emission:

| MachineInstr Opcode | PTX Operation |
|---|---|
| 149 | `ATOMIC_LOAD` |
| 294--297 | `atom.add` (f32 / f64 / i32 / i64) |
| 302--305 | `atom.min` (s32 / s64 / u32 / u64) |
| 314--317 | `atom.max` (s32 / s64 / u32 / u64) |
| 462 | `atom.cas` (generic) |

These opcodes are emitted by the SelectionDAG lowering for native atomic operations that survive the AtomicExpandPass without expansion.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `sub_6BBC40` | `0x6BBC40` | ~1251 lines | EDG atomic builtin name generator |
| `sub_12AA280` | `0x12AA280` |  | Legacy CAS IR node builder |
| `sub_12AA9B0` | `0x12AA9B0` |  | Legacy atomic exchange handler |
| `sub_12ADE80` | `0x12ADE80` |  | Scoped atomic load/store/fetch handler |
| `sub_12AE010` | `0x12AE010` |  | Fence acquire/release emitter (EDG only; BUG on NVVM) |
| `sub_12AE0E0` | `0x12AE0E0` |  | Volatile fence emitter (pre-SM 70) |
| `sub_12AE4B0` | `0x12AE4B0` |  | Explicit fence emitter (SM 70+) |
| `sub_12AE930` | `0x12AE930` | 41KB | PTX inline ASM atomic codegen (EDG side) |
| `sub_12B3FD0` | `0x12B3FD0` | 103KB | Main builtin lowering mega-switch |
| `sub_20C7CE0` | `0x20C7CE0` | 1399 | AtomicExpandPass: recursive type walker |
| `sub_20C84C0` | `0x20C84C0` | 1656 | AtomicExpandPass: address space checker |
| `sub_20C9140` | `0x20C9140` | 1204 | AtomicExpandPass: runOnFunction |
| `sub_20C96A0` | `0x20C96A0` | 1814 | AtomicExpandPass: CAS loop generation |
| `sub_20CA900` | `0x20CA900` | 218 | AtomicExpandPass: registration |
| `sub_20CB200` | `0x20CB200` | 2896 | AtomicExpandPass: sub-word mask generation |
| `sub_20CBD50` | `0x20CBD50` | 1646 | AtomicExpandPass: partword RMW expansion |
| `sub_20CC690` | `0x20CC690` | 43 | AtomicExpandPass: 11-case operation dispatch |
| `sub_20CD3E0` | `0x20CD3E0` | 6030 | AtomicExpandPass: partword CmpXchg expansion |
| `sub_20CEB70` | `0x20CEB70` | 10640 | AtomicExpandPass: full CmpXchg LL/SC expansion |
| `sub_21E5E70` | `0x21E5E70` |  | PTX emission: base atomic opcode emitter |
| `sub_21E6420` | `0x21E6420` |  | PTX emission: L2-hinted atomic opcode emitter |
| `sub_21E8EA0` | `0x21E8EA0` |  | PTX emission: cluster barrier emitter |
| `sub_21E94F0` | `0x21E94F0` |  | PTX emission: membar/fence emitter |
| `sub_9502D0` | `0x9502D0` | 55KB | PTX inline ASM atomic codegen (NVVM side) |
| `sub_94F9E0` | `0x94F9E0` |  | NVVM membar emitter |
| `sub_94FDF0` | `0x94FDF0` |  | NVVM fence emitter |

## Cross-References

- [Builtin System Overview](./index.md) -- hash table infrastructure and ID dispatch
- [SM 70--89 Feature Gates](../targets/sm70-89.md) -- unk_4D045E8 thresholds
- [SM 90 Hopper Features](../targets/sm90-hopper.md) -- cluster scope, fence.sc.cluster
- [SM 100 Blackwell Features](../targets/sm100-blackwell.md) -- f16x4 atomics
- [PTX Emission](../pipeline/emission.md) -- instruction printer subsystem
- [NVPTX Opcodes Reference](../reference/nvptx-opcodes.md) -- MachineInstr opcode table
- [Inline Assembly Codegen](../pipeline/irgen-functions.md#inline-assembly-codegen) -- general inline ASM infrastructure at sub_1292420 (and Path-A duplicate sub_932270)
