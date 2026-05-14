# NVVM mbarrier Ops

## Abstract

`nvvm.mbarrier.*` covers the sm_80+ mbarrier (memory barrier) state machine — a 64-bit shared-memory slot that counts arrivals, tracks an expected-transaction byte count, advances a phase parity, and lets warps wait for the slot to flip. The 21 ops in this family each implement one transition of that state machine and emit the matching `mbarrier.*` PTX instruction.

Two slot variants exist for almost every op: a generic-pointer form for completeness and a `.shared` form for the common case where the slot lives in shared memory. Lowering picks the `.shared` form whenever the operand address space is 3; the generic form remains so kernels that explicitly cast through `__cvta_to_shared` round-trip.

## State Machine

Each mbarrier slot carries four fields packed into a 64-bit word:

| Field | Bits | Role |
|---|---|---|
| `participant_count` | low 20 | total arrivals that complete one phase |
| `pending_count` | mid 20 | arrivals remaining before the phase completes |
| `tx_count` | next 20 | bytes still expected (for TMA expect-tx variant) |
| `phase` | high 1 | toggles each time the phase completes |

The state transitions are:

| Op | Transition |
|---|---|
| `init` | `participant_count := N`, `pending_count := N`, `tx_count := 0`, `phase := 0` |
| `arrive` | `pending_count -= 1`; if zero, complete the phase: flip `phase`, reset `pending_count := participant_count` |
| `arrive.nocomplete` | `pending_count -= 1` but suppress completion |
| `arrive.expect_tx` | `arrive` plus `tx_count += k` (for the TMA producer side) |
| `try_wait.parity` | non-blocking: return `true` if `phase == expected_phase` |
| `test.wait` | blocking: spin until `phase` matches the token |
| `inval` | mark the slot uninitialised |

The `expect_tx` op is the producer-side handshake for TMA tile loads: the consumer initialises the slot with the participant count, the TMA load issues `arrive.expect_tx` once the bytes are committed, and the consumer waits on the phase flip.

## Op Roster

| Op | Variants |
|---|---|
| `nvvm.mbarrier.init` | generic + `.shared` |
| `nvvm.mbarrier.inval` | generic + `.shared` |
| `nvvm.mbarrier.arrive` | generic + `.shared` |
| `nvvm.mbarrier.arrive.nocomplete` | generic + `.shared` |
| `nvvm.mbarrier.arrive.expect_tx` | generic + `.shared` |
| `nvvm.mbarrier.arrive.drop` | generic + `.shared` |
| `nvvm.mbarrier.test.wait` | generic + `.shared` |
| `nvvm.mbarrier.try_wait` | generic + `.shared` |
| `nvvm.mbarrier.try_wait.parity` | generic + `.shared` |
| `nvvm.fence.mbarrier.init` | (one op) — proxy fence before `init` |
| `nvvm.mbarrier.complete_tx` | (one op) — explicit tx-count bump |

Eleven ops × the two address-space variants account for the 21 entries in the dialect roster.

## Operand Tables

### `nvvm.mbarrier.init[.shared]`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` (`.shared`) or generic `ptr` | mbarrier slot |
| operand 1 | `count` | `i32` | participant count |

### `nvvm.mbarrier.inval[.shared]`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` or generic | mbarrier slot |

### `nvvm.mbarrier.arrive[.shared]` / `.arrive.drop[.shared]`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` or generic | mbarrier slot |
| operand 1 | `count` | optional `i32` | arrival weight (default 1) |
| result 0 | `token` | `i64` | phase token consumed by `test.wait` |

### `nvvm.mbarrier.arrive.expect_tx[.shared]`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` or generic | mbarrier slot |
| operand 1 | `txCount` | `i32` | expect-tx byte count |
| result 0 | `token` | `i64` | phase token |

### `nvvm.mbarrier.test.wait[.shared]`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` or generic | mbarrier slot |
| operand 1 | `token` | `i64` | from `arrive` |
| result 0 | `complete` | `i1` | phase-match outcome |

### `nvvm.mbarrier.try_wait.parity[.shared]`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` or generic | mbarrier slot |
| operand 1 | `phase` | `i32` | parity (0 or 1) |
| operand 2 | `ticks` | `i32` | retry budget |
| result 0 | `complete` | `i1` | phase-match outcome |

### `nvvm.fence.mbarrier.init`

| Position | Name | Type | Notes |
|---|---|---|---|
| (no operands) | — | — | proxy-acquire fence emitted before `mbarrier.init` |

## LLVM Intrinsic Mapping

| Op | LLVM intrinsic |
|---|---|
| `nvvm.mbarrier.init.shared` | `llvm.nvvm.mbarrier.init.shared.b64` |
| `nvvm.mbarrier.init` | `llvm.nvvm.mbarrier.init.b64` |
| `nvvm.mbarrier.inval.shared` | `llvm.nvvm.mbarrier.inval.shared.b64` |
| `nvvm.mbarrier.arrive.shared` | `llvm.nvvm.mbarrier.arrive.shared.b64` |
| `nvvm.mbarrier.arrive` | `llvm.nvvm.mbarrier.arrive.b64` |
| `nvvm.mbarrier.arrive.nocomplete.shared` | `llvm.nvvm.mbarrier.arrive.noComplete.shared.b64` |
| `nvvm.mbarrier.arrive.expect_tx.shared` | `llvm.nvvm.mbarrier.arrive.expect_tx.shared.b64` |
| `nvvm.mbarrier.test.wait.shared` | `llvm.nvvm.mbarrier.test.wait.shared.b64` |
| `nvvm.mbarrier.try_wait.parity.shared` | `llvm.nvvm.mbarrier.try.wait.parity.shared.b64` |
| `nvvm.fence.mbarrier.init` | `llvm.nvvm.fence.mbarrier.init.release.cluster` |

The intrinsic ID is selected at TableGen registration time; lowering does not re-derive it from operand types.

## PTX Templates

```text
mbarrier.init.shared.b64 [%mbar], %count;
mbarrier.inval.shared.b64 [%mbar];
mbarrier.arrive.shared.b64 %tok, [%mbar];
mbarrier.arrive.noComplete.shared.b64 %tok, [%mbar], %count;
mbarrier.arrive.expect_tx.shared.b64 %tok, [%mbar], %tx;
mbarrier.test_wait.shared.b64 %p, [%mbar], %tok;
mbarrier.try_wait.parity.shared.b64 %p, [%mbar], %ph, %ns;
fence.mbarrier_init.release.cluster;
```

The non-`.shared` forms drop the address-space token: `mbarrier.init.b64 [%mbar], %count;` and so on. The verifier rejects mixing — a `.shared` op with a generic pointer operand, or a generic op with a shared-pointer operand.

## Per-Arch Availability

| Op | SM floor | `ptx_min` |
|---|---|---|
| `init`, `arrive`, `arrive.nocomplete`, `inval`, `test.wait`, `try_wait` | sm_80 | 7.0 |
| `try_wait.parity` | sm_80 | 7.8 |
| `arrive.expect_tx` | sm_90 | 7.8 |
| `arrive.drop` | sm_80 | 7.0 |
| `fence.mbarrier.init` | sm_90 | 8.0 |
| Cluster-aware variants (`.cluster`, `.release.cluster`) | sm_90 | 8.0 |

The `expect_tx` form is the TMA producer-side handshake; it is the only op in this family that requires sm_90.

## Verifier Invariants

- `.shared` ops require operand 0 in addr-space 3.
- `count` and `txCount` are 32-bit unsigned; values larger than 20 bits are rejected.
- `test.wait` and `try_wait.parity` require an `i1` result type.
- `arrive.expect_tx` is rejected on sm_80; it requires sm_90 or later.
- `fence.mbarrier.init` carries a `release.cluster` scope; rewriting it to `acquire` is rejected.
