# Fat-Point Allocation Algorithm

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas register allocator uses a fat-point algorithm with a Chaitin-Briggs-style simplify-select ordering. Before the fat-point scan runs, the ordering function (`sub_93FBE0`) classifies vregs into six membership lists by interference degree, then drains them in simplify order (unconstrained and low-interference first, high-interference last) onto an assignment stack. The core allocator (`sub_957160`) pops this stack, scanning a per-physical-register pressure array for each vreg and picking the slot with the lowest interference count. High-interference vregs deferred during simplify are colored optimistically — if a slot exists below the discard threshold, assignment succeeds without spilling. This page documents the algorithm in full detail: the six-list ordering (see [overview.md](./overview.md#six-list-classification-sub_93fbe0) for the classification table), pressure array construction, constraint evaluation, register selection, assignment propagation, the retry loop, and the supporting knobs.

| | |
|---|---|
| **Core allocator** | `sub_957160` (1658 lines) — fat-point coloring engine |
| **Occupancy bitvector** | `sub_957020` — resizes bitvector; `sub_94C9E0` — marks slot ranges |
| **Interference builder** | `sub_926A30` (4005 lines) — constraint solver |
| **Assignment** | `sub_94FDD0` (155 lines) — write physical reg, propagate aliases |
| **Pre-allocation** | `sub_94A020` (331 lines) — pre-assign high-priority operands |
| **Retry driver** | `sub_971A90` (355 lines) — NOSPILL then SPILL retry loop |
| **Best result recorder** | `sub_93D070` (155 lines) — compare and keep best attempt |
| **Entry point** | `sub_9721C0` (1086 lines) — per-function allocation driver |

## Pressure Array Construction

The core allocator (`sub_957160`) allocates two pressure arrays at the start of each allocation round. They are **arena-allocated through the OCG slab allocator (vtable slot +24)**, not stack frames: at `ptxas_full.c:995796` the allocator invokes `(*(void**)(*(a2+16) + 24))(*(a2+16), 2056)` to obtain a 2056-byte block — 512 DWORDs (2048 bytes) of pressure data plus a 2-DWORD sentinel — and immediately a second matching call for the secondary array. The 2056-byte size is hard-coded; the allocator object handle is the OCG context's `a2+16` slot, which is reused across the entire compilation. See the [OCG slab allocator vtable+24 idiom sidebar in encoding-tables.md](../codegen/encoding-tables.md#sidebar--the-ocg-slab-allocator-vtable-24-idiom).

| Array | Variable | Role |
|-------|----------|------|
| Primary | `v12` | Per-physical-register interference count. Lower is better. |
| Secondary | `v225` | Per-physical-register secondary cost. Breaks ties when primary values are equal. |

Both arrays are zeroed using SSE2 vectorized `_mm_store_si128` loops aligned to 16-byte boundaries. The zeroing loop processes 128 bits per iteration, covering 512 DWORDs in approximately 128 iterations.

For each virtual register in the allocation worklist (linked list at `alloc+744`), the allocator zeroes the pressure arrays and then walks the VR's constraint list (`vreg+144`). For each constraint, it increments the appropriate pressure array entries at the physical register slots that conflict with the current virtual register. The result is a histogram: `primary[slot]` holds the total interference weight for physical register `slot`, accumulated over all constraints of all previously-assigned virtual registers that conflict with the current one. The full per-VR algorithm is documented in the Pressure Computation Algorithm section below.

The secondary array accumulates a separate cost metric used for tie-breaking. It captures weaker interference signals — preferences and soft constraints that do not represent hard conflicts but indicate suboptimal placement.

### Budget Computation

Before the pressure scan begins, the allocator computes the maximum physical register count for the current class:

```c
v231 = hardware_limit + 7                   // alloc+756, with headroom
if allocation_mode == 6 (CSSA paired):
    v231 *= 4                               // quad range for paired allocation
elif allocation_mode == 3 (CSSA):
    v231 *= 2                               // doubled range
alloc.budget = v231                         // stored at alloc+60
```

The hardware limit comes from the target descriptor and reflects the physical register file size for the current class (e.g. 255 for GPRs, 7 for predicates). The `+7` headroom allows the allocator to explore slightly beyond the architectural limit before triggering a hard failure — this is clamped during assignment by the register budget check in `sub_94FDD0`.

The register budget at `alloc+1524` interacts with `--maxrregcount` and `--register-usage-level` (values 0–10). The CLI-specified maximum register count is stored in the compilation context and propagated to the allocator as the hard ceiling. The `register-usage-level` option modulates the target: level 0 means no restriction, level 10 means minimize register usage as aggressively as possible. The per-class register budget stored at `alloc+32*class+884` reflects this interaction.

### Occupancy Bitvector

After computing the budget, the allocator initializes an occupancy bitvector (`sub_957020` + `sub_94C9E0`) that tracks which physical register slots are already assigned. The bitvector is sized to `ceil(budget / 64)` 64-bit words. For each VR being allocated, `sub_94C9E0` sets bits covering the VR's footprint in the bitvector using a word-level OR with computed masks:

```c
function mark_occupancy(bitvec, range, alignment):
    lo_word = alignment >> 6
    hi_word = min(alignment + 64, range) >> 6
    hi_mask = ~(0xFFFFFFFFFFFFFFFF >> (64 - (range & 63)))
    lo_mask = 0xFFFFFFFFFFFFFFFF >> (~alignment & 63)

    for word_idx in lo_word .. lo_word + (lo_word - hi_word):
        mask = 0xFFFFFFFFFFFFFFFF
        if word_idx == hi_word:  mask &= hi_mask
        if word_idx == last:     mask &= lo_mask
        bitvec[word_idx] |= mask
```

During the fatpoint scan, a set bit means "slot occupied — skip it." This prevents the allocator from considering slots already committed to other VRs in the current round.

## Pressure Computation Algorithm

The per-VR pressure computation is the core of the fat-point allocator. For each unassigned virtual register, the allocator builds a fresh pressure histogram, selects the minimum-cost physical register slot, and commits the assignment. The algorithm has seven steps, all executed inside the main loop of `sub_957160` (lines 493–1590 of the decompiled output).

### Step 1: VR Geometry

For each VR, the allocator computes the physical register footprint via `sub_7DAFD0`:

```c
function aligned_width(vreg):
    stride = 1 << vreg.alignment                // vreg+72, uint8
    size   = vreg.width                          // vreg+74, uint16
    return (-stride) & (stride + size - 1)       // = ceil(size / stride) * stride
```

| stride | size | result | Meaning |
|--------|------|--------|---------|
| 1 | 1 | 1 | Single register |
| 1 | 2 | 2 | Unaligned pair |
| 2 | 2 | 2 | Aligned pair |
| 2 | 3 | 4 | Aligned quad (rounded up) |

The pair mode is extracted from `(vreg+48 >> 20) & 3`:

| pair_mode | step_size | Behavior |
|-----------|-----------|----------|
| 0 | `stride` | Normal single-width scan |
| 1 | `stride` | Paired mode — `is_paired = 1`, scan ceiling doubled |
| 3 | `2 * stride` | Double-width — aligned width doubled, step by 2x stride |

### Step 2: Scan Range

The scan range defines which physical register slots are candidates:

```c
function compute_scan_range(alloc, vreg):
    max_slot = alloc.budget                          // alloc+1524
    if vreg.flags & 0x400000:                        // per-class ceiling override
        class_limit = alloc.class_limits[alloc.current_class]
        if max_slot > class_limit:
            max_slot = class_limit

    ceiling = ((max_slot + 1) << is_paired) - 1
    start   = vtable[320](alloc, vreg, bitvec)       // class-specific start offset
    alignment = alloc.scan_alignment << is_paired     // alloc+1556
    scan_width = alloc.slot_count + 4                 // alloc+1584 + 4

    return (start, ceiling, scan_width, alignment)
```

The `+4` on scan_width provides padding beyond the register file limit. For pair modes, the ceiling is shifted left: double for `is_paired`, quad for pair_mode 3 with the `0x40` flag at `ctx+1369`.

### Step 3: Zero Pressure Arrays

Before accumulating interference for this VR, both arrays are zeroed over `scan_width` DWORDs:

```c
function zero_pressure(primary[], secondary[], scan_width):
    if scan_width > 14 and arrays_dont_overlap:
        // SSE2 vectorized path: zero 4 DWORDs per iteration
        for i in 0 .. scan_width/4:
            _mm_store_si128(&primary[4*i],  zero_128)
            _mm_store_si128(&secondary[4*i], zero_128)
        // scalar cleanup for remainder
    else:
        // scalar path
        for i in 0 .. scan_width:
            primary[i]   = 0
            secondary[i] = 0
```

The SSE2 path has a non-overlap guard (`secondary >= primary + 16 || primary >= secondary + 16`) to ensure the vectorized stores do not alias. The scalar path is used for narrow scan ranges (width <= 14).

### Step 3b: Constraint List Construction

Before the pressure walk, the constraint list at `vreg+144` must already be populated. Construction happens during the liveness analysis and interference graph build phases that precede the per-VR allocation loop. Each instruction's operand descriptors are scanned and decomposed into 24-byte constraint nodes appended to the relevant VR's linked list.

#### Operand Descriptor Bit-Field Layout

Each operand in the Ori IR is an 8-byte (two-DWORD) value. The low DWORD encodes the operand kind and register identity:

```text
Low DWORD (operand[0]):
  bits 31       sign/direction flag (1 = negated or descending)
  bits 28–30   operand_type (3 bits, 0–7)
  bits 24–27   modifier / pair extension (bit 24 = pair half selector)
  bits  0–23   reg_id (24-bit virtual register index)

High DWORD (operand[1]):
  bits 29       NOT modifier flag
  bits 26       high-half selector (for 16-bit packed access)
  bits 25       low-half selector
  bits 22–24   shift/subfield control
  bits  0–21   constraint flags / immediate tag
```

The extraction idiom recurring throughout `sub_926A30`:

```c
operand_type = (operand[0] >> 28) & 7     // 3-bit kind
reg_id       =  operand[0] & 0xFFFFFF     // 24-bit VR index
is_register  = (operand_type - 2) <= 1    // types 2,3 = VR operands
is_special   = (operand_type == 5)        // type 5 = physical/fixed
pair_bit     = (operand[0] >> 24) & 1     // pair half selector
```

Operand type values observed in the decompiled code (cross-referenced with `isel_operand_constraints.json` accessor stubs at `sub_10B69E0`..`sub_10BE640` which use `(qword >> 25) & 7` on the 8-byte descriptor at offset +48):

| operand_type | Meaning | Generates constraint? |
|--------------|---------|----------------------|
| 0 | Unused / padding | No |
| 1 | Physical register (pre-assigned) | Yes — exclude-one or exclude-all-but |
| 2 | Virtual register (def) | Yes — point interference at def point |
| 3 | Virtual register (use) | Yes — point interference at use point |
| 4 | Immediate value | No — no register needed |
| 5 | Fixed/special register | Yes — hard constraint to specific slot |
| 6 | Constant pool reference | No |
| 7 | Sentinel (end of operand list) | No — terminates scan |

#### build_constraints Pseudocode

The full constraint builder is `sub_926A30` (4005 lines). Lines 521–803 implement a per-instruction operand normalization pre-pass; lines 803–1058 perform opcode-specific rewriting (merged pairs, constant materialization, dead-operand elimination); constraint extraction proper begins at line 1058. The pseudocode below covers the core three-phase structure.

```c
function build_constraints(ctx, opcode, isel_desc, num_ops, operands):
    // ---- Phase 0: operand normalization (lines 521–803) ----
    // Strip the 0x1000 flag: base_opcode = opcode & 0xFFFFCFFF
    // For base_opcode in {0x3E, 0x4E} or isel_desc == 20:
    //   set needs_rewrite = true
    // Walk operands 0..num_ops, re-classify via sub_91A0F0, and
    // invoke sub_7DB1E0 (operand materializer) when the operand's
    // constant value needs promotion to a different size.
    //
    // For opcodes 183/185/16/288/329: merge the last N operands.
    // When the penultimate operand is type 5 (fixed) and the last
    // is type 1 (physical), fold them:
    //   operand[k]   = (dw0 & 0x8F000000) | 0x10000000 | next_reg_id
    //   operand[k+1] = (dw1 & 0xFEC00000) | 0x1000000  | orig_reg_id
    // Propagates NOT/ABS/NEG/shift bits from the adjacent descriptor.
    // The sentinel (0x70000000) overwrites the consumed slot.

    // ---- Phase 1: gate check + per-operand ctype scan ----
    // Gate: vtable[152] for knob 87; if ctx.sm_target > 12 or
    //   per-function disable flag at ctx+1396 is set: return early.
    // Quick scan for width-8 constraint (vtable+904 returning 8):
    //   for each operand, call sub_91A0F0 to get ctype, then
    //   query_class_size(ctx, ctype); if result == 8: set has_wide.

    // ---- Phase 2: per-operand constraint extraction ----
    for i in 0 .. num_ops:
        dw0 = operands[2*i]
        dw1 = operands[2*i + 1]

        op_type = (dw0 >> 28) & 7
        reg_id  = dw0 & 0xFFFFFF

        if op_type == 7: break                     // sentinel
        if op_type in {0, 4, 6}: continue          // non-register

        // --- Constraint type classification (sub_91A0F0) ---
        // Two-level dispatch:
        //   L1: pair_align = (dw0 >> 26) & 3
        //       pair_align==1 -> return 20 (paired-low)
        //       pair_align>=2 -> return 26 (paired-high/both)
        //   L2: 150+ case opcode switch on base_opcode
        ctype = classify_constraint(base_opcode, isel_desc,
                                    operands, num_ops, i)

        // --- VR operand (types 2, 3) ---
        if (op_type - 2) <= 1:
            // Constant-fold path (sub_922210, 254 lines).
            // Fires when modifier bits dw1[26:25] are nonzero and
            // ctype is not 26 (paired-both cannot fold).
            if ctype != 26 and (dw1 & 0x6000000) != 0:
                shift = 0
                if ctype in {11, 12}:              // parity constraints
                    if (dw1 & 0x4000000): shift = 32
                elif ctype == 13:
                    ctype = 11; shift = (dw1 >> 22) & 0x10
                elif ctype == 14:
                    ctype = 12; shift = (dw1 >> 22) & 0x10

                value = *(ctx.const_table + 4 * reg_id)
                w = query_class_size(ctx, ctype)   // vtable+904
                if w <= 7:
                    if is_unsigned(ctype):
                        value = (value >> shift) & ((1 << 8*w) - 1)
                    else:
                        value = sign_extend(value >> shift, 8*w)

                if (dw1 & 0x20000000): value = ~value  // NOT
                if (dw1 & 0x40000000) and value < 0:
                    value = -value                      // ABS
                if dw1 < 0:            value = -value   // NEG

                // Rewrite operand to immediate form
                if ctype == 20:                    // predicate -> bool
                    imm = alloc_const(ctx, -(value != 0))
                    dw0 = (imm & 0xFFFFFF) | 0x24000000
                elif ctype in {9, 10}:             // 64-bit
                    imm = alloc_const64(ctx, value, value >> 32)
                    dw0 = (imm & 0xFFFFFF) | 0x30000000
                else:                              // 32-bit
                    imm = alloc_const(ctx, value)
                    dw0 = (imm & 0xFFFFFF) | 0x20000000
                operands[2*i] = dw0; operands[2*i+1] = 0
                continue                           // folded -> no constraint

            // Not foldable: sub_91BA40 handles the non-modifier path.
            // If needs_rewrite: sub_91B730 (width-aware const promotion)
            // else: sub_7DB1E0 (plain operand materialization).

        // --- Physical register (type 1) ---
        elif op_type == 1 and (dw0 >> 56) & 1 == 0:
            // sub_9267C0: fetches the 44-byte conflict record from
            // ctx+152, indexed by (reg_id & 0xFFFFF).  The record
            // has 10 DWORDs + 2 xmmwords encoding the physical
            // register's conflict set.  query_class_size via
            // vtable+904 determines width.
            emit_physical_constraint(ctx, &operands[2*i], ctype,
                                     base_opcode, operands, num_ops)
            continue

        // --- Fixed/special (type 5): merged in Phase 0 ---
        elif op_type == 5: continue

        // --- Other with byte-7 bit clear: skip ---
        elif (op_type - 2) > 1 and op_type != 5:
            if (dw0 >> 56) & 1 == 0: continue
```

The `classify_constraint` function (`sub_91A0F0`, ~500 lines) returns one of approximately 20 unique type IDs. Representative opcode-to-ctype mappings from the decompiled switch:

| Opcode(s) | Operand index condition | Returned ctype |
|-----------|------------------------|----------------|
| 0x3C, 0x3E, 0x4E, 0x4F (MOV/SHFL) | `i <= 1` and `num_ops > 2` | `(dw_last & (31 << (5*i+13))) >> (5*i+13)` — packed 5-bit |
| 0x10 (CALL) | `i == num_ops-4` or `-3` | `(dw_last & 0x400) ? 10 : 12` |
| 0x3D (IADD3) | `i==0`, src2 non-VR, v25==3 | 12 |
| 0x3F (ISETP) | `i == 3` | `(dw_last & 8)==0 ? 12 : 11`; `i==0` -> 12; else 6 |
| 0xC8..0xDB (barrier/sync) | `i == 0` | 6; else passthrough |
| 0xFA (REDUX) | `i == 2` | 14; else 10 |
| 0xFD (VOTE) | `i == 0` | 14; else 10 |
| 0x16, 0x32 | `i == num_ops-2` | 11 |
| 0x33 (I2I) | `i in {1,2}` | `(dw_aux & (1<<i))>>i != 1 ? 12 : 11` |
| default | any | passthrough = isel_desc |

The constraint type IDs 0–15 assigned above correspond to the 15 types documented in the [Constraint Types](#constraint-types) section below. The `isel_operand_constraints.json` records (39 records at `0x22B8E00`, 256-byte stride) define per-opcode constraint profiles: each record's `type_ids` array lists which of the 26 unique operand type IDs (0–25) are valid for that instruction class. The `register_class_constraints.json` records (72 records per SM generation at `0x21FDC00`, 64-byte stride) map each constraint index to `(class_id, sub_a, sub_b)` triplets that route constraints to the correct register class allocator.

### Step 4: Constraint Walk

The allocator iterates the constraint list at `vreg+144`. For VRs with alias chains (coalesced registers via `vreg+32`), the walk processes constraints for the entire chain, accumulating pressure from all aliases into the same arrays. Each constraint node is a 24-byte structure:

| Offset | Type | Field |
|--------|------|-------|
| +0 | pointer | Next constraint (linked list) |
| +8 | int32 | Constraint type (0–15) |
| +12 | int32 | Target VR index or physical register |
| +16 | int32 | Weight (interference cost) |
| +20 | uint8 | Soft flag (skip in later iterations) |

The constraint type dispatches to different accumulation patterns:

```c
function accumulate_pressure(primary[], secondary[], constraint_list, scan_width,
                             base_offset, pair_mode, half_width_mode, iteration):
    soft_count = 0
    for node in constraint_list:
        // --- Soft constraint relaxation (iteration > 0) ---
        if node.soft_flag and iteration > 0:
            soft_count++
            skip_threshold = iteration * knob_weight    // OCG knob at +46232
            if soft_count <= skip_threshold:
                continue                                // relax this constraint
            if bank_aware and soft_count > relaxation_ceiling:
                continue

        type   = node.type
        target = node.target
        weight = node.weight

        switch type:
            case 0:  // Point interference
                phys = lookup_vreg(target).physical_reg
                if phys < 0: continue                   // target unassigned
                offset = phys - base_offset
                if offset < 0 or offset >= scan_width: continue
                if half_width_mode:
                    offset = 2 * offset + hi_half_bit(target)
                primary[offset] += weight

            case 1:  // Exclude-one
                if half_width_mode:
                    offset = 2 * offset + hi_half_bit(target)
                for slot in 0 .. scan_width:
                    if slot != offset:
                        primary[slot] += weight

            case 2:  // Exclude-all-but (target is the only allowed slot)
                for slot in 0 .. scan_width:
                    if slot != target:
                        primary[slot] += weight

            case 3:  // Below-point (same as exclude-all-but, downward liveness)
                for slot in 0 .. scan_width:
                    if target > slot:
                        primary[slot] += weight

            case 5:  // Paired-low (even slots only)
                primary[offset] += weight
                // For pair_mode 3: also primary[offset+1] += weight

            case 6:  // Paired-high (odd slots only)
                primary[offset + 1] += weight

            case 7:  // Aligned-pair (both halves)
                primary[offset] += weight
                primary[offset + 1] += weight

            case 8:  // Phi-related (parity-strided accumulation)
                parity = compute_phi_parity(target, vreg)
                for slot in parity .. scan_width step 2:
                    primary[slot] += weight

            case 11: // Paired-even-parity
                for slot in 0 .. scan_width:
                    if slot != offset:
                        primary[slot] += weight

            case 12: // Paired-odd-parity
                inverse = compute_odd_inverse(offset, pair_mode)
                for slot in 0 .. scan_width:
                    if slot != inverse:
                        primary[slot] += weight

            case 13: // Paired-parity-group (even-only exclusion)
                if offset & 1: continue
                for slot in 0 .. scan_width:
                    if slot != offset + 1:
                        primary[slot] += weight

            case 14: // Paired-parity-extended (odd-only exclusion)
                if !(offset & 1): continue
                for slot in 0 .. scan_width:
                    if slot != offset - 1:
                        primary[slot] += weight

            case 15: // Range (SECONDARY array)
                range_end = min(offset, scan_width)
                // SSE2 vectorized: broadcast weight, add to secondary[0..range_end]
                for slot in 0 .. range_end:                 // vectorized
                    secondary[slot] += weight
                // Tail: slots beyond (range_end + pair_width)
                for slot in range_end .. scan_width:
                    if slot >= offset + pair_width:
                        secondary[slot] += weight

            default: // Types 4, 9, 10 and custom extensions
                vtable[240](alloc, primary, 514, node, scan_width, offset, pair_flag)
```

Type 15 (range) is the only constraint type that writes to the secondary array. All others write to primary. This is the architectural decision that makes secondary a pure tie-breaker: it captures long-range preference signals while primary captures hard interference.

#### SSE2 Vectorization in Constraint Walk

Three inner loops use SSE2 intrinsics:

1. **Type 0 (point) with large width:** `_mm_add_epi32` adds the broadcast weight to 4 primary slots per iteration. Alignment pre-loop handles the first 1–3 slots to reach 16-byte alignment.

2. **Type 15 (range) secondary accumulation:** `_mm_shuffle_epi32(_mm_cvtsi32_si128(weight), 0)` broadcasts the weight to all 4 lanes. The vectorized loop processes 4 secondary slots per iteration with `_mm_add_epi32(_mm_load_si128(...), broadcast)`.

3. **Type 8 (phi) stride-2 accumulation:** Uses `_mm_shuffle_ps` with mask 136 (0b10001000) to extract every-other element, then `_mm_add_epi32` to accumulate. This implements stride-2 addition across the primary array.

### Step 5: Iteration-Dependent Constraint Relaxation

On retry iterations (iteration > 0), the allocator progressively relaxes soft constraints to reduce pressure:

```c
function should_skip_soft_constraint(soft_count, iteration, knob_weight,
                                     knob_ceiling, bank_aware):
    threshold = iteration * knob_weight           // more skipped each retry
    if soft_count <= threshold:
        return true                               // skip (relax)
    if !bank_aware:
        return true
    if soft_count > (total_soft - iteration * knob_ceiling):
        return true                               // beyond ceiling
    return false
```

The relaxation formula means: on iteration N, the first `N * knob_weight` soft constraints are ignored. The `knob_ceiling` parameter (OCG knob at offset +46304) controls how aggressively the tail is also relaxed. This trades bank-conflict quality for register pressure reduction, allowing the allocator to find assignments that fit within the budget on later retries.

### Step 6: Fatpoint Selection (Minimum Scan)

After pressure accumulation, the allocator scans for the physical register slot with the lowest cost:

```c
function select_fatpoint(primary[], secondary[], start, ceiling, budget,
                         step_size, shift, occupancy_bv, threshold,
                         first_pass, bank_mode, bank_mask, prev_assignment):
    best_slot     = start
    best_primary  = 0
    best_secondary = 0

    // --- Pre-scan threshold check (first pass only) ---
    if first_pass:
        for slot in start .. ceiling step step_size:
            if occupancy_bv[slot]: continue        // already occupied
            if primary[slot >> shift] > threshold:  // knob 684, default 50
                first_pass = false                  // congestion detected
                break

    // --- Main scan ---
    slot = start
    while slot < budget and slot < alloc.slot_count << is_paired:
        // Occupancy filter
        if slot < bv_size and occupancy_bv[slot]:
            slot += step_size; continue

        p = primary[slot >> shift]
        s = secondary[slot >> shift]

        if prev_assignment >= 0:                    // not first VR
            if first_pass:
                if s >= best_secondary:
                    slot += step_size; continue     // secondary-only comparison
            else:
                if p > best_primary:
                    slot += step_size; continue
                if p == best_primary and s >= best_secondary:
                    slot += step_size; continue

        // Bank conflict filter
        if bank_mode and ((slot ^ prev_assignment) & bank_mask) == 0:
            slot += step_size; continue             // same bank → skip

        // Ceiling check
        if slot > ceiling:
            best_slot = slot; break                 // accept (over ceiling)

        // Accept this slot
        if slot < scan_width_shifted:
            best_secondary = secondary[slot >> shift]
            best_primary   = primary[slot >> shift]
            if best_secondary == 0 and (best_primary == 0 or first_pass):
                best_slot = slot; break             // zero-cost → immediate accept
            best_slot = slot
            slot += step_size; continue

        best_slot = slot
        best_primary = 0
        break

    return best_slot
```

Key design decisions in the fatpoint scan:

**Two-mode comparison.** On the first pass (`first_pass = true`, iteration 0), the scan uses secondary cost as the sole criterion, ignoring primary. This makes the first attempt pure-affinity-driven: it places VRs at their preferred locations based on copy/phi hints in the secondary array. On subsequent passes, primary cost dominates and secondary breaks ties.

**Immediate zero-cost accept.** When a slot has both `primary == 0` and `secondary == 0` (or just `primary == 0` on first pass), the scan terminates immediately. This means the first zero-interference slot wins — no further searching. Combined with the priority ordering of VRs, this produces a fast, greedy assignment.

**Bank-conflict avoidance.** The bank mask (`-8` for pair mode 1, `-4` otherwise) partitions the register file into banks. The filter `((slot ^ prev_assignment) & mask) == 0` ensures consecutive assignments land in different banks, reducing bank conflicts in the SASS execution units.

**Occupancy bitvector filtering.** The bitvector provides O(1) per-slot filtering of already-assigned registers. Bits are set by `sub_94C9E0` for each committed assignment, preventing the scan from considering occupied slots.

### Step 7: Commit and Advance

The selected slot is committed via `sub_94FDD0`:

```c
alloc.cumulative_pressure += best_primary       // alloc+788
sub_94FDD0(alloc, ctx, iteration, vreg, &local_state, best_slot, best_primary)
vreg = vreg.next                                 // vreg+128, advance worklist
```

The cumulative pressure counter at `alloc+788` tracks the total interference weight across all VR assignments in this attempt. The retry driver uses this to compare attempts.

### End-of-Round Result

After all VRs are processed, the allocator computes the result (lines 1594–1641):

```c
peak_usage = alloc+1580                          // max physical register used
class_slot = ctx+1584 + 4 * mode + 384
*class_slot = peak_usage

if peak_usage > 0x989677 + 6:                    // sanity threshold (~10M)
    emit_error("Register allocation failed with register count of '%d'."
               " Compile the program with a higher register target",
               alloc+1524 + 1)

return peak_usage + 1                            // number of registers used
```

The return value feeds into the retry driver's comparison: `target >= result` means success (the allocation fits within the register budget).

## Constraint Types

The fat-point interference builder (`sub_926A30`, 4005 lines) processes constraints attached to each virtual register. Constraints are extracted from instruction operand descriptors encoded as 32-bit values: bits 28–30 encode the operand type, bits 0–23 encode the register index, bit 24 is the pair extension bit, and bit 31 is a sign/direction flag.

The builder recognizes 15 constraint types. Each constraint type adds interference weight to specific physical register slots in the pressure arrays:

| Type | Name | Pressure effect |
|------|------|-----------------|
| 0 | Point interference | Adds weight to specific physical register slots that are live at the same program point as this VR. The most common constraint — represents a simple "these two VRs cannot share a physical register because both are live at instruction I." |
| 1 | Exclude-one | Adds weight to exactly one physical register slot, excluding it from consideration. Used when a specific physical register is reserved (e.g. for ABI constraints or hardware requirements). |
| 2 | Exclude-all-but | Adds weight to all slots *except* one. Forces the VR into a single permitted physical register. Used for fixed-register operands (e.g. `R0` for return values). |
| 3 | Below-point | Adds interference weight for registers live below (after) the current program point. Captures downward-exposed liveness — the VR must avoid physical registers that are used by later instructions. |
| 4 | (reserved) | Not observed in common paths. |
| 5 | Paired-low | Constrains the VR to an even-numbered physical register. Used for the low half of a 64-bit register pair. The pressure builder increments only even-indexed slots. |
| 6 | Paired-high | Constrains the VR to an odd-numbered physical register (the slot immediately after its paired-low partner). Increments only odd-indexed slots. |
| 7 | Aligned-pair | Constrains a pair of VRs to consecutive even/odd physical registers simultaneously. Combines the effects of types 5 and 6. |
| 8 | Phi-related | Marks interference from CSSA phi instructions (opcode 195). Phi constraints are softer — they add lower weight because the phi can potentially be eliminated by the coalescing pass. |
| 9 | (reserved) | Not observed in common paths. |
| 10 | (reserved) | Not observed in common paths. |
| 11 | Paired-even-parity | Constrains the VR to a physical register whose index has even parity with respect to a bank partition. Used for bank-conflict avoidance on architectures where register bank is determined by `reg_index % N`. |
| 12 | Paired-odd-parity | Constrains to odd parity within the bank partition. |
| 13 | Paired-parity-group | Constrains a group of VRs to compatible parity assignments across a bank. |
| 14 | Paired-parity-extended | Extended variant of parity constraints for wider register groups (quads). |
| 15 | Range | Adds interference over an interval of program points rather than a single point. Represents a VR whose live range spans multiple instructions and conflicts with another VR whose live range overlaps. The weight is proportional to the overlap length. |

The builder uses FNV-1a hashing (seed `0x811C9DC5`, prime `16777619`) for hash-table lookups into the pre-allocation candidate table. It contains SSE2-vectorized inner loops (`_mm_add_epi64`) for bulk interference weight accumulation when processing large constraint lists. The builder dispatches through 7+ vtable entries for OCG knob queries that modulate constraint weights.

### Constraint List Structure

Each virtual register carries a constraint list at `vreg+144`. The list is a linked chain of constraint nodes, each containing:

- Constraint type (one of the 15 types above)
- Target VR or physical register index
- Weight (integer, typically 1 for hard constraints, lower for soft)
- Program point or interval (for types 0, 3, 15)
- Pair/alignment specification (for types 5–7, 11–14)

The interference builder iterates this list for every VR being assigned, accumulating weights into the pressure arrays. The total cost of assignment to slot `S` is the sum of all constraint weights that map to `S`.

#### Program Point Computation

Program points are assigned per-block during the fatpoint scan (`sub_925BB0`). Each block maintains a monotonic counter at `block+160`, incremented once per instruction. The resulting index is stored into the instruction node at `instr+68`:

```c
function assign_program_points(block):
    counter = *(block + 160)            // current point, initially -1
    for instr in block.instruction_list:
        counter += 1
        *(block + 160) = counter
        *(instr + 68)  = counter        // instruction's program point
        block.point_table[counter] = &instr.descriptor
    *(block + 192) += 1                 // total instruction count
```

Constraint types 0 (point interference) and 3 (below-point) reference program points directly. Type 15 (range) stores an interval `[start_point, end_point]`. The point values are block-local indices, not global addresses.

#### Pair/Alignment Extraction

The pair alignment mode is encoded in two places: bits 26–27 of the operand descriptor dword\[0\], and bits 20–21 of the VR flags at `vreg+48`. The per-operand field is checked first by `sub_91A0F0` to determine the constraint type:

```c
function compute_constraint_type(opcode, operand_desc, operand_index):
    desc = operand_desc[2 * operand_index]      // dword[0] of 8-byte slot
    op_type = (desc >> 28) & 7                   // operand type field
    if op_type not in {2, 3}:                    // not a register operand
        return opcode_specific_default(opcode)

    pair_align = (desc >> 26) & 3
    if pair_align != 0:
        if pair_align == 1:  return 20           // paired-low constraint
        else:                return 26           // paired-both constraint
    else:
        return opcode_dispatch_table(opcode, operand_index)
```

The pair_align field in the operand descriptor (bits 26–27 of dword\[0\]) encodes:

| pair_align | Meaning | Constraint type returned |
|------------|---------|------------------------|
| 0 | Single register | Determined by opcode dispatch |
| 1 | Low half of pair | 20 (paired-low) |
| 2 | High half of pair | 26 (paired-high/both) |
| 3 | Both halves | 26 (paired-both) |

This feeds constraint types 5–7 (paired-low, paired-high, aligned-pair) during pressure accumulation. The bit-24 flag (`0x1000000`) in the descriptor marks a merged pair operand created when `sub_926A30` folds an address-type operand (type 5) with the adjacent register operand (type 1), setting `desc = (desc & 0x8F000000) | 0x10000000 | next_desc & 0xFFFFFF`. The VR-level pair_mode at `(vreg+48 >> 20) & 3` controls the physical register scan stride independently (documented in Step 1 above).

## Register Selection

After the pressure arrays are populated for a given VR, the allocator scans physical register candidates and selects the one with minimum cost:

```c
function select_register(primary[], secondary[], maxRegs, threshold, pair_mode):
    best_slot = -1
    best_primary_cost = MAX_INT
    best_secondary_cost = MAX_INT

    stride = 1
    if pair_mode != 0:
        stride = 2 << shift              // 2 for pairs, 4 for quads

    for slot in range(0, maxRegs, stride):
        if primary[slot] > threshold:     // knob 684, default 50
            continue                      // skip congested slots

        p = primary[slot]
        s = secondary[slot]

        if p < best_primary_cost:
            best_slot = slot
            best_primary_cost = p
            best_secondary_cost = s
        elif p == best_primary_cost and s < best_secondary_cost:
            best_slot = slot
            best_secondary_cost = s

    return best_slot                      // -1 if nothing found
```

Key design decisions in the selection loop:

**Threshold filtering.** The interference threshold (OCG knob 684, default 50) acts as a congestion cutoff. Any physical register slot with total interference weight above this value is immediately skipped. This prevents the allocator from assigning a VR to a slot that would cause excessive register pressure, even if that slot happens to be the global minimum. The threshold trades a small increase in the number of spills for a significant improvement in allocation quality — high-interference slots tend to require cascading reassignments.

**Alignment stride.** For paired registers (pair mode 1 or 3 in `vreg+48` bits 20–21), the scan steps by 2 instead of 1, ensuring the VR lands on an even-numbered slot. For quad-width registers, the stride is 4. The shift amount comes from the register class descriptor and varies by allocation mode.

**Two-level tie-breaking.** When two candidates have equal primary cost, the secondary array breaks the tie. This provides a smooth gradient for the allocator to follow when the primary interference picture is flat. The secondary array typically captures weaker signals like register preference hints, pre-allocation suggestions, and copy-related affinities.

**No backtracking.** The selection is final once made. There is no local search, no Kempe-chain swapping, and no reassignment of previously-colored VRs. If the selection leads to a spill later, the retry loop (see below) handles it by rerunning the entire allocation with updated spill guidance.

## Assignment: sub\_94FDD0

Once a physical register slot is selected, `sub_94FDD0` (155 lines) commits the assignment. This function handles four cases:

### Case 1: Normal Assignment

The physical register number is written to `vreg+68`. The register consumption counter (`sub_939CE0`, 23 lines) computes how many physical slots this VR occupies:

```c
consumption = slot + (1 << (pair_mode == 3)) - 1
```

For single registers, this is just `slot`. For double-width pairs (pair_mode 3), it is `slot + 1`, consuming two consecutive physical registers. The peak usage trackers at `alloc+1528` and `alloc+1564` are updated if `consumption` exceeds the current maximum.

### Case 2: Predicate Half-Width

For predicate registers (class 2, type 3), the allocator performs a half-width division. The physical slot is divided by 2, and the odd/even bit is stored at `vreg+48` bit 23 (the `0x800000` flag):

```c
physical_reg = slot / 2
if slot is odd:
    vreg.flags |= 0x800000    // hi-half of pair
else:
    vreg.flags &= ~0x800000   // lo-half of pair
```

This maps two virtual predicate registers to one physical predicate register, since NVIDIA's predicate register file supports sub-register addressing (each physical predicate holds two 1-bit values).

### Case 3: Over-Budget / Spill Trigger

If `slot >= regclass_info.max_regs` and the VR is not already marked as spilled (flag `0x4000` at `vreg+48`), the allocator sets the needs-spill flag:

```c
vreg.flags |= 0x40000         // needs-spill flag (bit 18)
```

When the needs-spill flag is later detected, the allocator calls:
1. `sub_939BD0` — spill allocator setup (selects bucket size, alignment, max based on knob 623 and cost threshold at `alloc+776`)
2. `sub_94F150` — spill code generation (561 lines, emits spill/reload instructions)

The spill cost is accumulated:
```c
alloc.total_spill_cost += vreg.spill_cost     // double at alloc+1568
alloc.secondary_cost   += vreg.secondary_cost  // float at alloc+1576
```

### Case 4: Alias Chain Propagation

After writing the physical register, the function follows the alias chain at `vreg+36` (coalesced parent pointer). Every VR in the chain receives the same physical assignment:

```c
alias = vreg.alias_parent                    // vreg+36
while alias != NULL:
    alias.physical_register = slot           // alias+68
    alias = alias.alias_parent               // alias+36
```

This propagation ensures that coalesced registers (merged by the coalescing pass at `sub_9B1200`) share a single physical register without requiring the allocator to re-derive the relationship.

### Pre-Allocated Candidate Check

Before committing a normal assignment, `sub_94FDD0` calls `sub_950100` (205 lines) to check if the VR has a pre-allocated candidate in the hash table at `alloc+248`. If a candidate exists (FNV-1a keyed lookup), the pre-assigned physical register is used instead of the one selected by the pressure scan. For paired registers, the pre-assigned slot is doubled (`type 1 -> slot * 2`) to account for pair stride.

## Pre-Allocation Pass: sub\_94A020

Before the core allocator runs, the pre-allocation pass (331 lines) optionally assigns physical registers to high-priority operands. This pass is gated by three knobs:

| Knob | Role |
|------|------|
| 628 | Enable pre-allocation pass |
| 629 | Enable coalescing-aware pre-allocation |
| 618 | Enable uniform register pre-allocation |

When enabled and the allocation mode is 3, 5, or 6, the pass:

1. Clears the pre-allocation candidate hash tables at `alloc+240..336` (six tables covering candidates, results, and overflow).
2. Iterates basic blocks calling `sub_9499E0` (per-block scanner, 304 lines) to identify pre-assignment opportunities.
3. For each eligible instruction, calls `sub_93ECB0` (194 lines) to pre-assign operands.

### Per-Block Scanner: sub\_9499E0

The scanner checks each instruction for pair-preassignment opportunities. It runs under two independent flags: `alloc+441` (coalescing-aware scan, gated by knob 569) and `alloc+440` (uniform-MAC scan). The core logic finds the destination operand, then walks subsequent operands looking for pair linkage:

```c
function scan_block_for_prealloc(alloc, insn):
    ctx = alloc->code_obj->ctx
    coalesce_scan = *(alloc + 441) AND knob_match(ctx, 569, insn)
    if NOT coalesce_scan AND NOT *(alloc + 440):
        return

    opcode = insn->opcode & 0xFFFFCFFF            // mask modifier bits 12-13
    if NOT is_prealloc_eligible_wide(opcode):      // bitmask + switch test
        goto tail_checks
    if NOT get_knob_bool(ctx, 617, 1): return

    if *(alloc + 440):                             // uniform-MAC path
        if reg_desc[insn->bb_idx]->field_148 == 0: return
        if NOT is_prealloc_eligible_narrow(opcode): return
    if knob_match(ctx, 444, insn): return          // exclusion filter

    dest_idx = find_dest_operand(insn, ctx)        // sub_938350
    if dest_idx == -1: goto tail_checks
    operands = &insn->operand[0]                   // base at insn+84
    if operands[dest_idx] >= 0: goto tail_checks   // must have sign bit set

    // walk operands from dest_idx forward, pair-linking via opcode switch
    for i = dest_idx .. insn->num_operands-1:
        if operands[i] >= 0: break                 // stop at non-register
        op_type = (operands[i] >> 28) & 7
        if op_type != 1: continue
        boundary = compute_boundary_index(insn)    // opcode switch below
        target   = &operands[2 * (boundary + (i - dest_idx))]
        try_pair_preassign(alloc, &operands[i], target)  // sub_9498C0

tail_checks:
    if NOT coalesce_scan: return
    if opcode == 130:                              // HSET2
        if NOT get_knob_bool(ctx, 617, 1): return
        // pair-link operand[0] -> operand[2] if pair-width permits
        try_pair_preassign(alloc, &operands[0], &operands[2])
    if opcode in {272, 273}:                       // TCSTSWS, QFMA4
        if NOT get_knob_bool(ctx, 617, 1): return
        // link dest with two source operands via sub_9496B0
        link_prealloc_pair(alloc, vreg[op[0]], vreg[op[2]], dir=0)
        link_prealloc_pair(alloc, vreg[op[0]], vreg[op[4]], dir=1)
```

`find_dest_operand` (`sub_938350`) scans operands left-to-right for the first type-1 register whose `vreg->reg_class` is 3 (UR) or 6 (Tensor/Acc), skipping pair-extension operands (bit 24 clear). Returns index or -1. `try_pair_preassign` (`sub_9498C0`) verifies both operands are type-1 registers outside 41–44, same `reg_class` matching `alloc+1504`, compares pair widths via `(vreg->flags >> 20) & 3`, and calls `link_prealloc_pair` (`sub_9496B0`) with direction 0/1/2 (wider-to-narrower / narrower-to-wider / equal).

### Coalescing Model — Conservative, Interference-Checked, Preference-Biased

The coalescing is not the classic Briggs degree-count test, nor a pure preference merge. It is **conservative, interference-checked, preference/bias-driven optimistic coloring**:

- **Coalesce candidates are *linked* and colored first.** A merge candidate is pushed on top of the coloring stack (via the pre-allocation pair links above) so the fat-point allocator colors it before any independent vreg.
- **The merge is committed at color time only if it passes a point-interference check.** When the linked candidate is popped, a single point-interference bitvector test (`sub_956130` builds the masks; the per-neighbor interference walk in the merge engine `sub_9AD220` validates legality) gates the merge — plus alignment and "don't split a vector word" checks. If the target physical register fails the test, the candidate is colored independently, not merged.
- **Preference/bias drives the *order* and the *target*, not the legality.** The pair-preassign hints bias which physical register the candidate prefers; interference decides whether it gets it.
- **Coalescing is restricted to class 3 (GPR/UR) and class 6 (UR/Tensor-Acc).** `find_dest_operand` only triggers for `reg_class ∈ {3, 6}`; other classes never enter the coalescing path. Gated by knob 618 (`RegAllocCoalescing`); the MAC/MMA accumulator coalescing is a separate enable.

### Class-6 Sub-Register Selector 5 / 6 / 7

Register **class 6 = Tensor/Accumulator** (MMA/WGMMA). When the accumulator handler `sub_9539C0` computes a sub-register selector value of `5`, `6`, or `7`, these are **computed accumulator operand-vector indices**, not static "paired low/high/both" tags:

- the accumulator base operand is `operand[numOps − pairAdj − 5]`; `+1` / `+2` then walk the consecutive 32-bit accumulator lanes (hence selectors 5/6/7);
- the partition *span* comes from a separate 3-bit field `(desc & 7) + 1`, scaled by instruction subtype (`×2` for subtype 19, `÷2` for subtype 13);
- the static "paired-low / paired-high / aligned-pair" meaning belongs to a *different* constraint-type switch (ctype 5/6/7), driven by pair-align bits 26–27, **not** to the class-6 selector value itself.

So the same numbers 5/6/7 mean "accumulator lane index" in the class-6 selector and "pair-align constraint kind" in the constraint-type switch — two unrelated uses that must not be conflated.

### Per-Operand Pre-Assigner: sub\_93ECB0

Iterates operands finding register-typed candidates, uses the opcode switch to classify each as read-side or write-side:

```c
function pre_assign_operands(alloc, insn, block_idx):
    if insn == NULL OR insn->num_operands == 0: return
    operands = &insn->operand[0]
    op_idx = 0
    while op_idx < insn->num_operands:
        desc = operands[2 * op_idx]
        if ((desc >> 28) & 7) != 1: op_idx++; continue  // not a register
        if (desc & 0xFFFFFF) - 41 <= 3: op_idx++; continue  // skip P0-P3
        vreg = lookup_vreg(ctx, desc & 0xFFFFFF)
        if desc < 0:                               // sign bit = output operand
            if NOT arch_preassign_disabled():       // field_46584/46592 gate
                pre_assign_one(alloc, vreg, PRIORITY_READ, block_idx, op_idx)
            goto advance
        boundary = compute_boundary_index(insn)    // opcode switch below
        if op_idx < boundary:                      // read-side
            if knob_match(ctx, 646, insn) AND get_knob_value(ctx, 646, insn) == 2:
                pre_assign_one(alloc, vreg, PRIORITY_READ, block_idx, op_idx)
            else:
                pre_assign_one(alloc, vreg, PRIORITY_WRITE, block_idx, op_idx)
        else:                                      // write-side
            pre_assign_one(alloc, vreg, PRIORITY_BOTH, block_idx, op_idx)
    advance:
        op_idx = scan_for_next_register(operands, n_ops, op_idx + 1)
```

The **opcode boundary switch** is identical in both functions. Maps masked opcode to the index separating read (below) from write (at or above) operands:

| Masked opcode | SASS | Boundary | Source |
|---------------|------|----------|--------|
| 22 | R2P | `sub_7E40E0(insn, 3)` | Per-instruction query |
| 50 | ATOM | `LUT[5 * ((last_op >> 2) & 3)]` | Packed table at `xmmword_21B2EC0` |
| 51, 110–111, 113–114, 289 | AL2P..SUATOM, UISETP | 3 | Fixed |
| 77 | EXIT | `sub_7E36C0(2, ...)` | Modifier-dependent |
| 83 | TEX | `sub_7E3640(insn, 3)` | Texture descriptor query |
| 112 | SULD | 4 | Fixed |
| 279 | FENCE\_T | 6 | Fixed |
| 297 | UFMUL | `sub_7E3790(insn, 3)` | Per-instruction query |
| 352 | SEL | `sub_7E3800(insn, 3)` | Per-instruction query |
| (other) | — | -1 | No pre-assignment |

| Priority | Meaning |
|----------|---------|
| 1 (READ) | Pre-assign read operands only |
| 2 (WRITE) | Pre-assign write operands only |
| 3 (BOTH) | Pre-assign both read and write operands |

`sub_93E9D0` (125 lines) creates a spill candidate node via `sub_93E290` (allocates 192-byte structures from the arena freelist at `alloc+232`), marks the live range via `sub_93DBD0` (356 lines), and recursively processes dependent operands via `sub_93EC50`.

## Retry Loop: sub\_971A90

The per-class allocation driver (355 lines) wraps the core allocator in a two-phase retry loop.

### Phase 1: NOSPILL

The first attempt runs the core allocator without spill permission. The debug log emits:

```text
"-CLASS NOSPILL REGALLOC: attemp N, used M, target T"
```

(Note: "attemp" is a typo present in the binary.)

The call sequence for each NOSPILL attempt:

```c
sub_93FBE0(alloc, ctx, iteration)       // reset state for attempt
if iteration == 0:
    sub_956130(alloc, class)            // build interference masks (first attempt only)
result = sub_957160(alloc, ctx, iteration)  // core fat-point allocator
sub_93D070(&best, class, iteration,         // record best result
           result, pressure, alloc, cost)
```

The NOSPILL loop runs up to `v102` attempts. Retry mode selection (from `sub_971A90` lines 199–240):

| Condition | v102 (max attempts) | Behavior |
|-----------|---------------------|----------|
| Knob 638 enabled + special mode | 0 | No allocation at all |
| Knob 638 enabled, knob 639 set | knob 639 value | Custom iteration count |
| Knob 638 enabled, knob 639 unset | 1 | Single attempt |
| Knob 638 disabled, pressure low | 2 | Standard 2-attempt retry |
| Knob 638 disabled, pressure high | 0 | Skip to spill |

Exit conditions within the NOSPILL loop:
- `target >= adjusted_result`: allocation fits within budget (success)
- `target >= result`: no improvement possible between iterations (give up)
- The best-result recorder (`sub_93D070`) compares the current attempt against the best seen so far using a multi-criterion ranking: register count first, then cost (double at `best+56`), then spill count, then class width. It uses `128 / register_count` as an inverse density metric.

### Phase 2: SPILL

If all NOSPILL attempts fail, the driver records the last NOSPILL result (`sub_93D070` with attempt=99), then enters the spill phase via `sub_9714E0` (312 lines). This is a feedback loop between three components: a guidance engine that selects spill victims, a state rebuilder that resets interference, and the core allocator running in spill-aware mode.

#### Step 1: Spill codegen setup

If spill instructions have not been generated for this class (flag at `alloc+865`), `sub_9714E0` calls `sub_939BD0` (spill strategy init) and `sub_94F150` (spill codegen to LMEM), then records the starting register count:

```c
function spill_phase(alloc, max_attempts, class, best):
    if alloc.prev_reg_count + 1 >= max_attempts:
        return {spilled=false}
    if not alloc.spill_triggered:                  // alloc+865
        sub_939BD0(alloc)                          // spill lookup strategy
        sub_94F150(alloc, ctx, 1)                  // emit spill/reload to LMEM
    alloc.spill_start = alloc.prev_reg_count + 1   // alloc+1516
```

#### Step 2: Guidance-driven VR spill marking

The target vtable (slot 848) determines how many extra registers the architecture offers. With a nonzero spill budget, VRs are marked with the spill sentinel:

```c
    spill_budget = vtable[856](target, alloc.spill_start)
    alloc.spill_count = spill_budget               // alloc+1520
    if spill_budget > 0:
        vtable[216](alloc)                         // pre-spill hook
        for vreg in alloc.class_list:              // alloc+736
            entry = alloc.guidance_bitmask[vreg.register_class]
            slot  = entry.base + entry.count
            entry.count += 1
            alloc.guidance_slots[slot] = 163       // sentinel: spill this VR
```

The validation gate (vtable slot 280) checks viability. On rejection, markings are unwound:

```c
    if spill_budget > 0 and not vtable[280](alloc):
        alloc.spill_count = 0
        for vreg in alloc.class_list:
            alloc.guidance_bitmask[vreg.register_class].count -= 1
        sub_940EF0(alloc, ctx)                     // rebuild clean state
```

#### Step 3: Interference rebuild (`sub_940EF0`)

`sub_940EF0` (503 lines) resets allocation state for the current class. It walks six priority buckets at `alloc+72..184`, clearing `vreg+104` for every VR, then re-triages each:

```c
function rebuild_interference(alloc, ctx):
    alloc.cost_array = arena_alloc(num_regs + 1)   // alloc+856, zeroed
    alloc.available  = spill_start - class_base     // alloc+56
    for bucket in {alloc+72, +96, +120, +144, +168}:
        for vreg in bucket: vreg.back_ptr = NULL
        bucket = empty
    alloc.best_candidate = -1                       // alloc+1532
    for vreg in alloc.class_list:
        vreg.back_ptr = NULL; vreg.intf_sum = 0     // +104, +92
        vreg.flags &= ~0x1                          // clear bit 0 of +48
        if vreg.is_preassigned: continue            // bit 5 of +48
        vreg.phys_reg = -1                          // +68
        cost = sub_938EA0(alloc, vreg)              // spill cost
        vreg.intf_sum = cost
        if cost > alloc.high_threshold:             // alloc+768
            insert(bucket_HIGH, vreg)               // alloc+120
        elif vreg.has_constraint and (vreg.flags & 0x80000):
            insert(bucket_HIGH, vreg)
        elif sub_939220(alloc, vreg, available):    // trivially colorable?
            insert(bucket_LOW, vreg)                // alloc+72
        else:
            insert(bucket_NORMAL, vreg)             // alloc+96
```

#### Step 4: Spill-aware core allocation

The core allocator runs via `sub_957160` with attempt=99 (spill-mode sentinel). The architecture vtable (slot 168) adjusts the result:

```c
    result = sub_957160(alloc, ctx, 99)
    result = vtable[168](alloc, class, result, alloc.prev_reg_count)
    if alloc.prev_reg_count + 1 >= result:          // fits budget
        alloc.spill_count = 0; return success
    elif result == 0x989677+8:                      // hard failure
        // "Register allocation failed with register count of '%d'."
```

#### Step 5: Guidance engine invocation

If still over budget, the driver calls `sub_96D940` (2983 lines) for refined spill candidates:

```c
    if result > budget and spill_count == 0:
        sub_93D070(&best, class, 99, result, pressure, alloc, cost)
        if not best.has_valid:
            result = sub_936FD0(&best, result)      // fallback reduction
    alloc.final_count = result - 1                  // alloc+1560
    sub_96D940(alloc, ctx, class)                   // guidance engine
    target.spill_result[class] = alloc.spill_count
```

The guidance engine allocates an 11112-byte structure with 7 priority queues (qword offsets 0, 166, 188, 210, 232), each backed by `ceil(max_regs/64)` qword bitmask arrays. Per basic block it: (1) populates live-range bitmasks via `sub_95BC90`, (2) coalesces split ranges via `sub_93CD20` when not pre-split, (3) walks instructions dispatching on opcode (52=entry, 54=restore, 72=call, 97=exit, 269=special), (4) invokes `sub_9680F0` in guidance mode or `sub_966490` for pair/quad VRs, (5) updates live bitmasks via `sub_BDBB80` and rebuilds pressure via `sub_952AE0`. After the walk, `sub_93C0B0` consumes the queues to mark spill victims. For class 6, `sub_9539C0` handles accumulator-specific logic.

#### Step 6: VR flag feedback

On success (high byte == 0), VRs with bit 18 of `vreg+48` (`0x40000`, "must-spill") lacking bit 9 (`0x200`, "materialized") are counted at `ctx+1584 + 4*class + 412`. On failure (high byte != 0), physical assignments are cleared:

```c
    if failed:
        for vreg in alloc.class_list:
            if not (vreg.flags & 0x20) and vreg.reg_type != 8:
                vreg.phys_reg = -1
    // Debug: "{class}-CLASS SPILLING REGALLOC ({spill|no-spill}), N used, M allocated"
```

The outer loop in `sub_9721C0` retries `sub_971A90` while nonzero, with arena cleanup (`sub_8E3A80`) between attempts.

### SMEM Spill Activation

For allocation modes 3 or 6 when the compilation target is device type 5, shared-memory spilling is activated before the retry loop:

```c
if (class == 3 || class == 6) and device_type == 5:
    if num_variables > 0:
        sub_939BD0(alloc)                  // spill allocator setup
        sub_94F150(alloc, ctx, 1)          // spill codegen to SMEM
    alloc.spill_triggered = 1              // flag at alloc+865
```

This path generates spill/reload instructions targeting shared memory instead of local memory, which is faster but limited in size and shared across the CTA.

## Per-Class Iteration

The top-level entry point (`sub_9721C0`, 1086 lines) drives allocation for all register classes sequentially:

```c
for class_id in 1..6:
    if class_list[class_id] is empty:
        continue
    alloc.current_class = class_id          // alloc+376
    while sub_971A90(alloc, ctx, class_id) != 0:
        sub_8E3A80(alloc+2)                 // arena cleanup between attempts
```

Classes 1–6 are initialized via the target descriptor vtable at offset `+896`. The vtable call `vtable[896](alloc_state, class_id)` populates per-class register file descriptors at `alloc[114..156]` (four 8-byte entries per class). The class IDs correspond to `reg_type` values (1 = R, 2 = R alt, 3 = UR, 4 = UR ext, 5 = P/UP, 6 = Tensor/Acc). Barrier registers (`reg_type = 9`) are above the `<= 6` cutoff and handled separately.

| Class ID | Name | Width | HW Limit | Description |
|----------|------|-------|----------|-------------|
| 1 | R | 32-bit | 255 | General-purpose registers (R0–R254) |
| 2 | R (alt) | 32-bit | 255 | GPR variant (RZ sentinel, stat collector alternate) |
| 3 | UR | 32-bit | 63 | Uniform general-purpose registers (UR0–UR62) |
| 4 | UR (ext) | 32-bit | 63 | Uniform GPR variant (triggers flag update at +1369 in constructor) |
| 5 | P / UP | 1-bit | 7 | Predicate registers (P0–P6, UP0–UP6) |
| 6 | Tensor/Acc | 32-bit | varies | Tensor/accumulator registers for MMA/WGMMA operations |

Class 0 (unified/cross-class) is skipped in the main loop. It is used for cross-class constraint propagation during the interference building phase. Classes 3 (UR) and 6 (Tensor/Acc) have early-out conditions: if `alloc+348 == 2` (class 3) or `alloc+332 == 2` (class 6), allocation is skipped because no VRs of that class exist. Barrier registers (B, UB) have `reg_type = 9`, which is above the `<= 6` allocator cutoff and are handled by a separate mechanism outside the 7-class system.

Before the per-class loop, virtual registers are distributed into class-specific linked lists (lines 520–549 of `sub_9721C0`):

```c
for each vreg in function_vreg_list:       // from ctx+104
    if vreg.id in {41, 42, 43, 44}:        // skip architectural predicates
        continue
    class = vreg.register_class             // vreg+12
    if class >= 1 and class <= 6 and vreg.type != 0:
        insert(class_lists[class], vreg)
```

The VR list is sorted by priority (`sub_9375C0`) before distribution. Priority ordering ensures that VRs with more constraints and higher spill costs are allocated first, giving them first pick of the register file.

## Fast Register Allocation: Knob 638

Knob 638 (`register pressure analysis enable` / fast allocation mode) controls a single-pass no-retry allocation path. When enabled with the special mode flag set, the allocator sets `v102 = 0`, meaning the NOSPILL retry loop body never executes. Allocation proceeds directly to spill handling without iterating.

When knob 638 is enabled without the special mode flag:
- The iteration count is set to 1 (or the value of knob 639 if set)
- This creates a limited-retry mode where the allocator makes at most `knob_639` attempts
- Each attempt still uses the full fat-point algorithm but with no fallback to the multi-attempt guidance-driven loop

This mode is intended for fast compilation (`--fast-compile`) where compilation time matters more than register allocation quality. The allocator accepts the first viable assignment rather than searching for an optimal one.

## Interference Builder: sub\_926A30

The interference builder (4005 lines) is the largest single function in the allocator. It combines instruction rewriting (constant folding, operand normalization, pair merging) with constraint extraction in a single pass over each basic block's instruction list.

### Operand Descriptor Bit-Field Layout (low DWORD of 8-byte operand)

```text
  31  30 29 28  27 26 25 24  23 ............ 0
 +---+--------+--+--+--+---+------------------+
 |DEF| op_type|  modifiers |      reg_id      |
 +---+--------+--+--+--+---+------------------+
   1    3 bits    4 bits        24 bits

Extraction idiom (appears 100+ times in the decompiled source):
  op_type  = (dword0 >> 28) & 7        // bits 30:28
  reg_id   = dword0 & 0xFFFFFF         // bits 23:0
  pair_ext = (dword0 >> 24) & 1        // bit 24
  is_def   = dword0 >> 31              // bit 31 (DEF marker: 1 = written, 0 = read)

Register operand test:  (op_type - 2) <= 1   // types 2 (def), 3 (use)
Special operand test:   op_type == 5
Sentinel test:          op_type == 7          // end of operand list
```

### Main Loop Pseudocode

```c
function sub_926A30(ctx, opcode, op_category, operand_count, operands, flags, ...):
    // --- Phase 0: early exit & opcode canonicalization ---
    if opcode == 109:  return opcode              // NOP, nothing to process
    masked_opcode = opcode & 0xFFFFCFFF           // clear bits 12-13
    is_eligible = 0
    if masked_opcode == 0x3E or masked_opcode == 78:
        is_eligible = 1
    else if *op_category == 20 and (ctx.flags1377 & 2) != 0:
        is_eligible = 1
    else:
        is_eligible = sub_7D66E0(*op_category)    // query: category needs rewrite?

    // --- Phase 1: forward operand walk with constraint type resolution ---
    for i = 0 to operand_count - 1:
        op_ptr  = operands + 8 * i
        dword0  = *(u32*)op_ptr
        op_type = (dword0 >> 28) & 7
        kind    = op_type - 2                     // 0=def, 1=use for VR operands

        // Eligible-operand rewriting: fold address/pair operands
        if is_eligible and kind <= 1:
            adjusted_count = *operand_count - ((opcode >> 11) & 2)
            if i < adjusted_count:
                new_cat = sub_91A0F0(masked_opcode, *op_category,
                                     operands, adjusted_count, i)
                if sub_7D66E0(new_cat):
                    sub_7DB1E0(op_ptr, ctx, new_cat)   // rewrite operand
            op_type = (*(u32*)op_ptr >> 28) & 7        // re-extract after rewrite
            kind    = op_type - 2

        // Skip non-register, non-special operands
        if kind > 1 and op_type != 5 and (*(u8*)(op_ptr+7) & 1) == 0:
            continue

        // Resolve constraint category for this operand position
        constraint_cat = sub_91A0F0(masked_opcode, *op_category,
                                    operands, operand_count, i)

        // Dispatch: VR operands -> full constraint builder,
        //           fixed/special  -> lightweight path
        if kind <= 1:                                  // type 2 or 3
            sub_922210(ctx, masked_opcode, op_ptr, constraint_cat,
                       operands, operand_count, weight)
        else:
            sub_9267C0(ctx, op_ptr, constraint_cat, masked_opcode,
                       operands, operand_count)

    // --- Phase 2: pair-merge for 0x1000-flagged instructions ---
    if (opcode & 0x1000) != 0:
        penult = operands + 8*(operand_count - 2)
        last   = operands + 8*(operand_count - 1)
        if (*(u32*)(last+4) & 0x20000000) != 0:       // pair-merge flag
            *(u32*)(last+4) ^= 0x20000000
            resolved = backend_vtable[79](backend, *(u32*)last & 0xFFFFFF)
            *(u32*)(last+4) = 0
            *(u32*)last = (resolved & 0xFFFFFF) | 0x60000000

    // --- Phase 3: address-operand folding (opcodes 183/185/16/288/329) ---
    if masked_opcode in {183, 185, 16, 288, 329}:
        skip = select_addr_skip(masked_opcode)        // 4 or 5
        slot = operand_count - ((opcode >> 11) & 2) - skip
        addr_op = operands + 8 * slot
        if (*(u32*)addr_op >> 28 & 7) == 5:           // type 5 = address
            next_op = operands + 8 * (slot + 1)
            if (*(u32*)next_op >> 28 & 7) == 1        // type 1 = reg
               and (*(u8*)(next_op+7) & 1) == 0:
                // Fold: reg_id from next, mark type-1 with pair bit
                *(u32*)addr_op = (*(u32*)addr_op & 0x8F000000)
                               | 0x10000000
                               | (*(u32*)next_op & 0xFFFFFF)
                propagate_modifier_flags(addr_op, next_op)
                *(u64*)next_op = 0x70000000            // sentinel (type 7)

    // --- Phase 4: constant-folding gate ---
    if not ocg_knob_query(ctx, 87, 1):  return opcode
    if ctx.opt_level > 12 or sub_7DDB50(ctx) == 1:
        return opcode

    // Determine if first operand is a foldable constant
    has_const_src = 0
    if operand_count > 0 and *(i32*)operands < 0:     // sign bit = const
        fmt = (*(u16*)(operands+6)) & 3
        if   fmt == 1: has_const_src = 1               // integer
        elif fmt == 2: has_const_src = 0               // float, skip
        else: has_const_src = backend_vtable[8](backend, opcode, *op_category)

    // --- Phase 5: constant expression evaluation (30+ opcodes) ---
    // Extract constant values:  const_val = *(u64*)(ctx.const_table + 4*reg_id)
    //
    // Opcode dispatch (selected from the binary):
    //   2  (ADD):  result = const_a + const_b; clamp on signed overflow
    //   35 (SEL):  result = const_pred ? const_a : const_b
    //   79 (MUL):  result = const_a * const_b
    //  110 (MAD):  fold to ADD+MUL pair
    //  137 (IMAD): delegate to sub_9216C0 for 3-operand fold
    //  139 (IMUL): 64-bit product
    //  149 (ISHR): result = const_a >> const_b
    //  201 (SHR):  byte-width from vtable[113]
    //  213 (IMNMX):min/max per sign mode
    //  272 (PRMT): byte permutation
    //
    // After folding, instruction becomes:
    //   opcode = 130 (MOV), operand_count = 2
    //   operands[1] = sub_91CDD0(ctx, folded_value) | (type << 28)

    // --- Phase 6: multi-operand normalization ---
    // sub_91EFC0: emit constraint records into VR constraint lists
    // sub_9229F0: MAD -> ADD+shift strength reduction
    // sub_91FCB0: MUFU path rewriting
    // sub_924120: bank-conflict pair splitting
    // sub_9203A0: three-operand instruction merging
    // sub_91ED40: write rewritten instruction back
    return opcode
```

The builder queries multiple OCG knobs via vtable dispatches at offsets +72, +120, +152, +224, +256, +272, and +320. These knobs modulate constraint weights and enable/disable specific constraint categories (e.g. bank-conflict-aware constraints are gated by knob 641).

Special register IDs 41–44 (PT, P0–P3) and 39 are always skipped. The skip predicate (`sub_9446D0`, 29 lines) additionally checks for CSSA phi instructions (opcode 195 with type 9 = barrier) and performs hash-table lookups in the exclusion set at `alloc+360`.

## Best Result Recorder: sub\_93D070

The best-result recorder (155 lines) compares the current allocation result against the best seen across all retry attempts. It maintains state at offsets `best[10..20]`:

```c
best[10] = register_count                   // best count so far
best[13] = 128 / register_count             // inverse density metric
best[16] = max_pressure                     // peak live registers
best[17] = spill_score
*(double*)(best + 56) = cost                // floating-point cost metric
best[18] = arch_peak_1                      // from architecture state +408
best[20] = arch_peak_2                      // from architecture state +400
```

Comparison uses lexicographic ordering:
1. Lower register count wins
2. On tie: lower cost (double) wins
3. On tie: lower spill count wins
4. On tie: lower class width wins

When the current attempt improves over the best, the recorder allocates a per-register assignment array and copies the full VR-to-physical-register mapping for later restoration.

## Per-Instruction Assignment: sub\_9680F0

The per-instruction assignment core loop (3722 lines, the largest function in part 2 of the allocator) handles the actual instruction-by-instruction walk during allocation.

### Initialization

The function begins by computing the register pair stride from the register class descriptor. The `pair_mode` field at `reg_class+48` bits 20–21 selects the stride:

| pair\_mode | stride | operand divisor | pair width |
|-----------|--------|-----------------|------------|
| 0 (single) | 2 | /2 | 4 |
| 1 (half-pair) | 1 | /1 | 2 |
| 3 (quad) | 4 | /4 | 8 |

The function then builds the **assigned** bitvector (`alloc+1342..1345`, 256 bits) by enumerating physical registers belonging to the target class. It walks the register-file linked list from `backend[11]`, setting one bit per physical register slot. For register pairs, each physical register occupies two slots (`slot * 2`).

### Operand Pre-Scan and Bank Conflict Detection

Before entering the main loop, the function scans operands of the block's header instruction in reverse order (last to first). For each operand of type 1 (register) whose physical index is not 41–44 (architectural predicates):

```c
for operand_idx = num_operands-1 downto 0:
    op = insn.operands[operand_idx]
    if op.type != REG or (op.phys_index - 41) <= 3:
        continue
    slot = find_slot(op.phys_index)       // lookup in alloc+36 table
    used[slot >> 6] |= (1 << slot)       // mark in used bitvector (alloc+1350)
    if insn.live_out[operand_idx] == NULL:
        must_not_spill[slot >> 6] |= (1 << slot)  // (alloc+1346)
    if op.is_write and slot <= 255:
        bank_result = sub_9364B0(insn, operand_idx, op.phys_index)
        if bank_result != 1:              // conflict detected
            assigned[slot >> 6] &= ~(1 << slot)   // unmark from assigned
```

### Rematerialization Flag

A vtable call on the strategy object (`alloc[1]`) detects rematerialization. When the vtable entry is `sub_9360F0` (default), the flag falls back to `insn.opcode == 187`. This `remat_flag` (`v570`) gates later calls to `sub_93AC90`.

### Main Instruction Loop

```c
pressure = 0; assign_count = 0        // v86, v558
first_spill = NULL                     // alloc+1355: set when assign_count > 3
fallback_spill = NULL                  // alloc+1356: set when assign_count > 30
spill_point = NULL                     // alloc+1354
insn = block.first_insn                // v87: linked-list head
while insn != NULL:
    opcode = insn.next.opcode          // look-ahead to successor
    // --- pressure: count live registers from operands (reverse, skip idx 41..44) ---
    for each register operand:
        vr = backend.vreg_table[op.phys_index]
        if vr.class == alloc.class and vr.priority > alloc.threshold:
            pressure += is_pair(vr) ? 2 : 1
    if sub_7DF3A0(insn, backend) & 1:  // pinned -- skip assignment
        goto advance
    // --- attempt register assignment ---
    ok = sub_961A60(alloc, insn, &scratch, &remat_state,
                    half_count, reg_class_desc, allow_spill_flag)
    if !ok: goto ASSIGNMENT_FAILED
    assign_count++
    // --- BB boundary (opcode 52): reset spill tracking ---
    if insn reached new BB:
        first_spill = fallback_spill = spill_point = NULL
    else:
        // --- opcode 97 (STG): early spill point if class 1-3 or sub_936CF0 ---
        // --- opcode 236 (UBLKPF/CALL): exit to finalization immediately ---
        if !spill_point and sub_7E0F50(insn, backend):
            if !first_spill and assign_count > 3 and opcode != 187:
                first_spill = insn; snapshot pressure to alloc+1357
            if !fallback_spill and assign_count > 30 and alloc.flag_804:
                fallback_spill = insn
    if opcode != 187 and !spill_point:  // 187 = IMMA_16832 (LOAD) -> remat source
        remat_flag = check_next_is_load(insn)
    sub_93B760(alloc, interference_set, insn)
  advance:
    insn = insn.next                   // v87 = *v87
```

### Post-Loop: Spill Decision and Recursive Fallback

After the loop exits, the function selects a spill point and attempts to commit:

```c
if remat_candidate:       spill_insn = remat_candidate
elif spill_point:         spill_insn = spill_point
else:                     spill_insn = first_spill

result = sub_964130(alloc, block, reg_class_desc, half_count,
                    ..., spill_insn, 0)
if result >= 0:           // success: tail-call self for remaining instructions
    sub_9680F0(alloc, ctx, block, class_idx, budget, mode, 0, flags)
    return
// commit failed -- recursive spill fallback
sub_96CE90(alloc, block, class_idx, budget, &mode, ctx)
```

`sub_96CE90` re-invokes `sub_9680F0` with modified spill-permission flags, allowing registers previously protected by `must_not_spill` to be spilled. This is the escalation mechanism from pressure-only allocation to actual spill insertion.

If allocation still fails (`v122 != 0` at LABEL\_160), the function emits: `"Register allocation failed with register count of '%d'. Compile the program with a higher register target"`. The count is `alloc.threshold + 1`, or for class 6, `sub_9372B0(alloc)` (accounting for uniform register reservations).

## Post-Allocation Verification

After register allocation completes, a verification pass called "memcheck" (NVIDIA's internal name, unrelated to Valgrind) compares reaching definitions before and after allocation. Every instruction's operands are checked: the set of definitions that could reach each use must be preserved, or any change must be explained by a known-safe transformation (spill/refill, rematerialization, predicate packing). Unexplained changes indicate an allocator bug.

The verification runs inside the post-regalloc scheduling pass (`sub_A8B680`), after all register assignments are finalized and spill/reload instructions have been inserted.

### Verification Call Flow

```text
sub_A8B680 (PostAllocPass::run)
  +-- sub_A5B1C0   build pre/post def-use chains (48KB, all instructions)
  +-- sub_A76030   MemcheckPass::run -- entry point
        |
        for each instruction in function:
        |   +-- sub_A56790  fast per-instruction check (returns bool)
        |   |     true  -> skip (defs match)
        |   |     false -> deep verify
        |   |
        |   +-- sub_A54140  look up pre-regalloc def set
        |   +-- sub_A54140  look up post-regalloc def set
        |   +-- sub_A75CC0  deep single-instruction verification
        |         +-- sub_A56400  build Additions list (new defs)
        |         +-- sub_A56400  build Removals list (lost defs)
        |         +-- sub_A55D80  diagnostic reporter (10 error codes)
        |               +-- sub_A55D20  print uninitialized-def detail
        |
        printf("TOTAL MISMATCH %d   MISMATCH ON OLD %d\n", ...)
```

### Fast Check vs Deep Verify

The fast check (`sub_A56790`) performs a lightweight comparison per instruction. It returns true when pre-regalloc and post-regalloc reaching definitions match exactly. Only on failure does the verifier invoke the expensive deep path (`sub_A75CC0`), which:

1. Builds two difference lists — "Additions" (defs present after but not before) and "Removals" (defs present before but not after).
2. Classifies each difference as either `BENIGN (explainable)` or `POTENTIAL PROBLEM` by pattern-matching against known-safe transformations: spill-store/refill-load pairs, P2R/R2P predicate packing, bit-spill patterns, and rematerialized instructions.
3. For each unexplained difference, creates an error record with a category code (1–10), pointers to the offending instructions, and operand type flags.

### Error Categories

The reporter (`sub_A55D80`) dispatches on the error code at `record+24`:

| Code | Name | Message | Trigger |
|------|------|---------|---------|
| 1 | Spill-refill mismatch | `Failed to find matching spill for refilling load that is involved in this operand computation` | A post-regalloc refill load has no corresponding spill store. The verifier walks the spill-refill chain and cannot find the matching pair. |
| 2 | Refill reads uninitialized | `This operand was fully defined before register allocation, however refill that is involved in this operand computation reads potentially uninitialized memory` | The refill reads from a stack slot that was never written — the spill store was optimized away or placed on a non-executing path. |
| 3 | P2R-R2P pattern failure | `Failed to establish match for P2R-R2P pattern involved in this operand computation` | Predicate-to-register / register-to-predicate instruction pairs used to spill predicate registers through GPRs have a broken chain — the matching counterpart is missing. |
| 4 | Bit-spill-refill failure | `Failed to establish match for bit-spill-refill pattern involved in this operand computation` | The bit-packing variant of predicate spilling (multiple predicates packed into GPR bits) failed pattern matching. Same root cause as code 3 but for the packed representation. |
| 5 | Uninitialized value introduced | `Before register allocation this operand was fully defined, now an uninitialized value can reach it` | Pre-regalloc: all paths to this use had a definition. Post-regalloc: at least one path has no definition. The register holds a stale value from a prior computation. Typically caused by a spill placed on the wrong path or a definition eliminated during allocation. |
| 6 | Extra defs introduced | `After reg-alloc there are some extra def(s) that participate in this operand computation. They were not used this way before the allocation.` | The post-regalloc definition set is a strict superset of the pre-regalloc set. New definitions were introduced through register reuse or aliasing. When the extra def involves a short/byte type in a wider register, the reporter prints: `Does this def potentially clobber upper bits of a register that is supposed to hold unsigned short or unsigned byte?` and suggests `-knob IgnorePotentialMixedSizeProblems`. |
| 7 | Rematerialization mismatch | `Rematerialization problem: Old instruction: [%d] New instruction: [%d]` | A rematerialized instruction does not match the original. The new instruction created to recompute a value differs from the original in an unexpected way. |
| 8 | P2R-R2P base destroyed | `Some instruction(s) are destroying the base of P2R-R2P pattern involved in this operand computation` | The GPR holding packed predicate bits is overwritten between the P2R store and R2P restore by another instruction that defs the same physical register. |
| 9 | Bit-spill base destroyed | `Some instruction(s) are destroying the base of bit-spill-refill pattern involved in this operand computation` | Same as code 8 but for the bit-packing spill variant. The base register holding packed predicate bits is overwritten between store and restore. |
| 10 | Definitions disappeared | `During register allocation we did not add any new definitions for this operand and yet some original old ones disappeared` | The post-regalloc definition set is a strict subset of the pre-regalloc set. Definitions were removed without replacement by spill/refill or rematerialization. |

### Reporter Output Format

When `DUMPIR=AllocateRegisters` is enabled (knob ID 266), the reporter (`sub_A55D80`) prints a structured diagnostic per mismatch:

```text
=== ... (110 '=' banner) ===
REMATERIALIZATION PROBLEM. New Instruction [N] Old Instruction [M]   // only if instruction changed
INSTRUCTION: [N]
=== ... ===

Operand # K
Producers for operand K of instruction [N] before register allocation:
  [42] def opr # 0 for src opr # 2
  [38] def opr # 1 for src opr # 2

Producers for operand # K of instruction [N] after register allocation:
  [42] def opr # 0 for src opr # 2
  [55] def opr # 0 for src opr # 2          // <-- new, from refill

Additions
  [55] def opr # 0 src opr # 2  BENIGN (explainable)

Removals
  [38] def opr # 1 src opr # 2  POTENTIAL PROBLEM

<error-category-specific message from the table above>
```

If `DUMPIR=AllocateRegisters` is not enabled and mismatches exist, the verifier prints a one-shot suggestion:

```text
Please use -knob DUMPIR=AllocateRegisters for debugging
```

The one-shot flag at `verifier+1234` ensures this message appears at most once per allocation attempt.

### Mismatch Counters

The verifier tracks two counters reported at the end of the pass:

| Offset | Counter | Meaning |
|--------|---------|---------|
| `verifier+1236` | Total mismatches | Instructions where post-regalloc defs differ from pre-regalloc defs in an unexplained way. |
| `verifier+1240` | Old mismatches | Subset of total mismatches that represent pre-existing issues — the pre-regalloc def chain was already empty (no reaching definitions before allocation either). These are not regressions from the current allocation attempt. |

### Knob Interactions

| Knob | Effect |
|------|--------|
| `DUMPIR=AllocateRegisters` (ID 266) | Enables verbose per-mismatch diagnostic output. Without this, only the summary line and suggestion are printed. |
| `IgnorePotentialMixedSizeProblems` | Suppresses the mixed-size aliasing warning in error code 6 (extra defs involving short/byte types in wider registers). |
| `memcheck` flag at `function+1384` | Gates whether verification runs at all. When zero, `sub_A76030` is not called. |

### Verification Function Map

| Address | Size | Role | Confidence |
|---------|------|------|---|
| `sub_A54140` | — | Def-use chain lookup (hash table query into pre/post maps) | HIGH |
| `sub_A55D20` | ~100B | Print uninitialized-def warning helper | HIGH |
| `sub_A55D80` | 1454B | Diagnostic reporter — 10 error categories, structured output | HIGH |
| `sub_A56400` | — | Build additions/removals lists for deep comparison | MEDIUM |
| `sub_A56560` | 698B | Verify single operand's reaching definitions | HIGH |
| `sub_A56790` | ~250B | Per-instruction fast check (returns bool pass/fail) | HIGH |
| `sub_A5B1C0` | 8802B | Full-function def-use chain builder (pre and post regalloc) | HIGH |
| `sub_A60B60` | 4560B | Pre/post chain comparison engine | HIGH |
| `sub_A62480` | — | Reset scratch arrays between operand checks | MEDIUM |
| `sub_A75220` | 2640B | Compare reaching definitions (builds diff lists) | HIGH |
| `sub_A75CC0` | 866B | Deep single-instruction verifier (classifies diffs) | HIGH |
| `sub_A76030` | 770B | MemcheckPass::run — verification entry point | HIGH |

## Occupancy-Aware Budget Model

The allocator maintains a 144-byte budget pressure model at `alloc+1600`--`alloc+1744` that adjusts the effective register budget based on thread occupancy. The model is initialized by `sub_947150` (the allocator constructor) and consumed by the spill guidance function `sub_96D940`. The goal: kernels that need high occupancy get tighter register budgets (more aggressive spilling), while kernels that can tolerate low occupancy get relaxed budgets (more registers, fewer spills).

### Coefficient Initialization

Three knob-overridable coefficients control the interpolation:

| Field | Offset | Knob | Knob Name | Type | Default | Meaning |
|-------|--------|------|-----------|------|---------|---------|
| coeffA | +1632 | 664 | `RegAllocSpillBitLowRegScale` | DBL | 0.2 | Scale at low register counts (piecewise default value) |
| coeffB | +1640 | 661 | `RegAllocSpillBitHighRegScale` | DBL | 1.0 | Scale at high register counts (linear model y\_max) |
| coeffC | +1648 | 665 | `RegAllocSpillBitMediumRegScale` | DBL | 0.3 | Scale at medium register counts (linear model y\_min) |

Two integer knobs set the tier boundaries:

| Field | Offset | Knob | Knob Name | Type | Default | Meaning |
|-------|--------|------|-----------|------|---------|---------|
| maxThreads | +1624 | 663 | `RegAllocSpillBitLowRegCountHeur` | INT | 119 | Low register count tier boundary |
| pressureThresh | +1628 | 660 | `RegAllocSpillBitHighRegCountHeur` | INT | 160 | High register count tier boundary |

All five knobs use the standard OCG type-check pattern: byte at `knobArray + 72*index` encodes 0 (use default), 1 (use INT value at +8), or 3 (use DBL value at +8).

### Piecewise Interpolation Table

After reading the knobs, `sub_947150` queries the target descriptor for the hardware maximum thread count (vtable slot 90, offset +720). This value is clamped to `maxThreads - 1` if a knob override is active. The result becomes `totalThreads` — the kernel's maximum achievable occupancy.

An optional override through the function-object vtable at `context+1584` (vtable slot 118, offset +944) can adjust the architectural register limit. When the override is active, it calls `override_fn(totalThreads, param, 255.0)` and sets the adjusted limit to `255 - result`. When inactive, the limit stays at 255.

The piecewise array stores 7 `(value, x-coordinate)` pairs that define a step function mapping register counts to scale factors:

```c
interpTable[0] = coeffA    interpTable[1] = maxThreads
interpTable[2] = coeffA    interpTable[3] = pressureThresh
interpTable[4] = coeffA    interpTable[5] = adjustedLimit     // 255 or (255 - override)
interpTable[6] = coeffB
```

This means: for register counts up to `maxThreads` (default 119), the budget scale is coeffA (0.2); from `maxThreads` to `pressureThresh` (160), still coeffA; from `pressureThresh` to the adjusted limit (255), still coeffA; and beyond that boundary, coeffB (1.0). In practice the piecewise table establishes a two-tier system: a low scale for most of the register range, jumping to the high scale only at the top.

### Linear Interpolation Model

A separate linear model provides continuous interpolation for the spill guidance decision. Two more vtable queries establish the domain:

```c
x_min = target->getMaxOccupancy()    // vtable slot 90 on target descriptor via context+1584
x_max = target->getMinOccupancy()    // vtable slot 96 (offset +768) on function-object via context+1584
y_min = coeffC                       // 0.3
y_max = coeffB                       // 1.0
slope = (coeffB - coeffC) / (x_max - x_min)
```

The slope is stored at `alloc+1736`. Since `x_max` (minimum occupancy, meaning fewest concurrent threads = most registers allowed) is typically greater than `x_min` (maximum occupancy, meaning most concurrent threads = fewest registers), the slope is positive: as the function moves toward allowing more registers (fewer threads), the budget fraction increases.

### Spill Guidance Consumption

The spill guidance function `sub_96D940` (line 1520 in the decompiled output) uses the linear model to compute a dynamic spill threshold:

```c
budget_fraction = (current_reg_count - x_min) * slope + y_min
spill_threshold = budget_fraction * (class_budget - class_floor + 1)
```

Where:
- `current_reg_count`: the current register allocation count for this class (from `alloc+884` indexed by class)
- `class_budget` and `class_floor`: per-class allocation bounds at `alloc + 32*class + 884` and `alloc + 32*class + 880`
- For paired registers, `current_reg_count` is halved: `(count + 1) >> 1`

The comparison at line 1527:

```c
if (spill_count + unspilled_need_spill + current_reg_count) > spill_threshold:
    trigger_spill(sub_948B80)
```

If the total pressure (live registers needing spill + those already marked for spill + current allocation count) exceeds the occupancy-adjusted threshold, the allocator triggers a spill. The `sub_948B80` call adds the current VR to the spill candidate queue.

### Worked Example

For a Blackwell SM100 kernel with default knobs:

| Parameter | Value | Source |
|-----------|-------|--------|
| coeffA | 0.2 | Knob 664 default |
| coeffB | 1.0 | Knob 661 default |
| coeffC | 0.3 | Knob 665 default |
| maxOccupancy (x\_min) | 240 | SM100 target vtable slot 90 |
| minOccupancy (x\_max) | 480 | SM100 target vtable slot 96 |
| slope | (1.0 - 0.3) / (480 - 240) = 0.00292 | Computed |

If the current GPR class has a budget of 128 and floor of 0 (range = 129), and the function currently uses 300 registers:

```c
budget_fraction = (300 - 240) * 0.00292 + 0.3 = 0.475
spill_threshold = 0.475 * 129 = 61.3
```

If more than 61 VRs are pending spill or already allocated, the allocator triggers a spill rather than attempting to fit another register. With fewer registers in play (say 250), the fraction drops to 0.329 and the threshold tightens to 42 — more aggressive spilling at higher occupancy targets.

### Budget Model Field Summary

| Offset | Size | Type | Init | Field |
|--------|------|------|------|-------|
| +1600 | 8 | ptr | `ctx[2]->+208` | Function object pair pointer |
| +1608 | 8 | ptr | 0 | Auxiliary pointer (unused at init) |
| +1616 | 8 | QWORD | `0xFFFFFFFF` | Occupancy upper bound |
| +1624 | 4 | DWORD | 119 / knob 663 | maxThreads (low reg count tier boundary) |
| +1628 | 4 | DWORD | 160 / knob 660 | pressureThresh (high reg count tier boundary) |
| +1632 | 8 | double | 0.2 / knob 664 | coeffA (low-register scale) |
| +1640 | 8 | double | 1.0 / knob 661 | coeffB (high-register scale / y\_max) |
| +1648 | 8 | double | 0.3 / knob 665 | coeffC (medium-register scale / y\_min) |
| +1656 | 8 | double | (computed) | totalThreads as double |
| +1664 | 8 | double | = coeffA | interpTable[0]: value for tier 0 |
| +1672 | 8 | double | = maxThreads | interpTable[1]: x-boundary for tier 0 |
| +1680 | 8 | double | = coeffA | interpTable[2]: value for tier 1 |
| +1688 | 8 | double | = pressureThresh | interpTable[3]: x-boundary for tier 1 |
| +1696 | 8 | double | = coeffA | interpTable[4]: value for tier 2 |
| +1704 | 8 | double | = adjustedLimit | interpTable[5]: x-boundary for tier 2 |
| +1712 | 8 | double | = coeffB | interpTable[6]: value for tier 3 (final) |
| +1720 | 8 | double | (computed) | x\_min: max occupancy thread count |
| +1728 | 8 | double | = coeffC | y\_min (linear model intercept at x\_min) |
| +1736 | 8 | double | (computed) | slope: (coeffB - coeffC) / (minOcc - maxOcc) |
| +1744 | 8 | ptr | 0 | Tail sentinel |

### Post-Init: sub\_939BD0

Immediately after building the interpolation tables, `sub_947150` calls `sub_939BD0` which configures the spill guidance lookup strategy at `alloc+1784`. This function queries knob 623 (`RegAllocEstimatedLoopIterations`) through the function-object vtable:

- **If knob 623 is set**: the spill guidance uses the knob's value to estimate loop iteration counts, passed to the lookup strategy via vtable slot 3 (offset +24).
- **If knob 623 is unset**: the lookup strategy is initialized with default parameters. When the budget model's auxiliary weight at `alloc+776` is zero, the strategy uses `(8, 4, 0x100000)`; otherwise `(16, 16, 0x100000)`.

## Function Map

| Address | Lines | Role | Confidence |
|---------|-------|------|---|
| `sub_926A30` | 4005 | Fat-point interference builder / constraint solver | HIGH |
| `sub_93D070` | 155 | Best result recorder (multi-criterion comparison) | HIGH |
| `sub_93E290` | 397 | Spill candidate node creator (192-byte arena alloc) | HIGH |
| `sub_93E9D0` | 125 | Pre-assign individual operand | HIGH |
| `sub_93ECB0` | 194 | Pre-assign registers (per-instruction dispatcher) | HIGH |
| `sub_93FBE0` | 940 | Per-iteration allocation state reset | HIGH |
| `sub_939BD0` | 63 | Spill guidance strategy initializer (knob 623 query) | HIGH |
| `sub_939CE0` | 23 | Register consumption counter (pair-aware) | HIGH |
| `sub_9446D0` | 29 | Register skip predicate (special regs, exclusion set) | HIGH |
| `sub_947150` | ~700 | Allocator constructor (budget model + interpolation init) | HIGH |
| `sub_94A020` | 331 | Pre-allocation pass (knobs 628/629/618) | HIGH |
| `sub_94FDD0` | 155 | Register assignment + alias propagation | HIGH |
| `sub_950100` | 205 | Pre-allocated candidate applier (FNV-1a lookup) | HIGH |
| `sub_956130` | 873 | Register class interference mask builder (SSE2) | HIGH |
| `sub_957020` | 24 | Occupancy bitvector resizer (arena-backed realloc + memset) | HIGH |
| `sub_94C9E0` | 59 | Occupancy bitmask range setter (word-level OR with masks) | HIGH |
| `sub_7DAFD0` | 7 | VR aligned width computation (`ceil(size/stride)*stride`) | CERTAIN |
| `sub_957160` | 1658 | Core fat-point allocator (coloring engine) | HIGH |
| `sub_9680F0` | 3722 | Per-instruction assignment core loop | HIGH |
| `sub_96D940` | 2983 | Spill guidance (7-class priority queues) | HIGH |
| `sub_971A90` | 355 | NOSPILL / SPILL retry driver | HIGH |
| `sub_9714E0` | — | Post-allocation finalization | MEDIUM |
| `sub_9721C0` | 1086 | Register allocation entry point | HIGH |
| `sub_936FD0` | — | Final fallback allocation | MEDIUM |
| `sub_9375C0` | — | VR priority sort | MEDIUM |
