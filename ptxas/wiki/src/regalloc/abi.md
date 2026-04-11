# GPU ABI & Calling Convention

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas ABI engine implements the NVIDIA GPU calling convention for device-side function calls. It manages parameter register allocation, return address placement, scratch/preserved register classification, and per-function ABI lowering across the full range of SM architectures (sm\_35 through sm\_100+). The engine runs as a multi-pass pipeline invoked per-kernel from the per-function compilation driver (`sub_98F430`), positioned between optimization passes and the register allocator. It spans approximately 250 KB (276 functions) at address range `0x19C6230`--`0x1A00FFF`.

| | |
|---|---|
| **Master ABI setup** | `sub_19D1AF0` (5608 bytes) -- orchestrates full per-function ABI pipeline |
| **Per-pass lowering** | `sub_19DC4B0` (6459 bytes) -- 3-pass instruction transform driver |
| **Opcode-level dispatch** | `sub_19CFC30` -- routes 11 opcodes to ABI handlers |
| **Parameter allocator** | `sub_19CA730` (2277 bytes) -- 2048-bit free-list bitmap allocator |
| **Return address validator** | `sub_19CDFF0` (7.5 KB) -- 12 diagnostic strings, warnings 7001--7009 |
| **Return address setup** | `sub_19D1720` (4.8 KB) -- validates and assigns return address registers |
| **Register transfer lowering** | `sub_19CC1A0` (3873 bytes) -- generates MOV/STS/LDS/PRMT sequences |
| **gb10b WAR** | `sub_19D9E00` + `sub_19DA2A0` -- `__nv_reservedSMEM_gb10b_war_var` |
| **Convergent checker** | `sub_19D13F0` (4.3 KB) -- `allowConvAlloc` boundary validation |
| **Address range** | `0x19C6230`--`0x1A00FFF` (~250 KB, 276 functions) |

## Reserved Registers

Registers R0--R3 are reserved by the ABI and cannot be used for general allocation. The allocator enforces this with the diagnostic `"Registers 0-3 are reserved by ABI and cannot be used for %s"`. These four registers serve fixed ABI roles (stack pointer, thread parameters, etc.) and are excluded from both parameter passing and general register allocation.

The reservation is unconditional across all SM generations. Any `.maxreg` directive or ABI specification that attempts to assign these registers to parameter or return roles triggers a diagnostic.

## SM Generation Dispatch

The ABI engine determines the target SM generation by reading a field from the SM target descriptor:

```
generation = *(int*)(sm_target + 372) >> 12
```

| Generation value | SM targets | Key ABI differences |
|------------------|------------|---------------------|
| 3 | sm\_35, sm\_37 | Kepler ABI: no uniform registers, no convergent boundaries |
| 4 | sm\_50, sm\_52, sm\_53 | Maxwell ABI: 16-register minimum, label fixups, coroutine insertion |
| 5 | sm\_60--sm\_89 | Pascal through Ada ABI: 24-register minimum, cooperative launch support |
| 9 | sm\_90, sm\_90a | Hopper ABI: 24-register minimum, uniform return address support |
| >9 | sm\_100+ | Blackwell ABI: no minimum enforced (skips check), extended register reservation |

The minimum register count varies by generation. For generations 3--4 (sm\_35 through sm\_53), the ABI requires at least 16 registers per function. For generations 5--9 (sm\_60 through sm\_90a), the minimum is 24. Generations below 3 and above 9 skip the minimum check entirely. Violating these minimums emits warning 7016: `"regcount %d specified below abi_minimum of %d"`. The `abi_minimum` value is computed as `(generation - 5) < 5 ? 24 : 16`.

## Master ABI Setup: sub\_19D1AF0

The top-level ABI entry point (5608 bytes), called once per function by the per-function compilation driver `sub_98F430`. It orchestrates the full ABI pipeline in 10 steps:

```
function abi_master_setup(func, sm_target, abi_spec):
    // 1. Validate register count vs. ABI minimums
    generation = *(sm_target + 372) >> 12
    if generation in 3..4:  min_regs = 16    // sm_35-sm_53
    if generation in 5..9:  min_regs = 24    // sm_60-sm_90a
    if func.maxreg < min_regs:
        warn(7016, "regcount %d specified below abi_minimum of %d",
             func.maxreg, min_regs)

    // 2. Validate register reservation range
    if available_regs < requested_reservation:
        warn(7017, "register available %d for reservation is less "
             "than the requested number of registers %d",
             available_regs, requested_reservation)

    // 3. Validate coroutine SUSPEND semantics
    for each register in func.preserved_set:
        if register.is_scratch_at_suspend:
            warn(7011, "Register (%s%d)is defined as scratch on "
                 "SUSPEND but preserved for coroutine function",
                 register.class_name, register.index)

    // 4. Iterate callee list, mark ABI-callable entries
    for each callee in func.callees:
        callee.abi_flags |= ABI_CALLABLE
        propagate_abi_attributes(func, callee)

    // 5. Propagate register limits to callees
    abi_propagate_limits(func)               // sub_19CE590

    // 6. Check return-address / parameter overlap
    abi_overlap_precheck(func)               // sub_19CA3C0

    // 7. Allocate parameter registers
    abi_alloc_params(func)                   // sub_19CA730

    // 8. Validate return address assignment
    abi_return_addr_setup(func)              // sub_19D1720

    // 9. Detailed return address validation
    abi_return_addr_validate(func)           // sub_19CDFF0

    // 10. Adjust register file limits via vtable
    vtable[736](func, sm_target)
```

## Parameter Passing

Parameters are passed in consecutive R registers starting from a configurable base register. The ABI tracks `"number of registers used for parameter passing"` and `"first parameter register"` as per-function properties. The parameter register range begins after the reserved registers (R0--R3) and the return address register.

### Parameter Register Allocator: sub\_19CA730

The core parameter allocator (2277 bytes, 98% confidence). It uses a 256-byte free-list array (`v103[]`) where each byte represents one register slot: `0xFF` = free, `0x00` = occupied.

```
function abi_alloc_params(func):
    // Initialize 256-byte free-list (one byte per register slot)
    bitmap[256] = {0xFF...}                      // all slots free

    // Mark reserved registers as occupied
    clear_bits(bitmap, 0, 3)                     // R0-R3 always reserved

    // Mark already-allocated registers
    popcount = register_usage_popcount(func)     // sub_19C99B0

    // Allocate PARAMETER registers
    for each param in func.parameters:
        align = param_alignment(param.type_width) // 4/8/16 bytes
        slot = find_contiguous_free(bitmap, param.reg_count, align)
        if slot == -1:
            error("Function %s size requires more registers(%d) "
                  "than available(%d)", func.name, needed, available)
            return FAILURE
        assign_register(slot, param)             // sub_7FA420
        mark_allocated(bitmap, slot, param.reg_count)  // sub_BDBB80

    // Allocate RETURN registers (same algorithm, separate class)
    for each ret in func.return_values:
        slot = find_contiguous_free(bitmap, ret.reg_count, align)
        assign_register(slot, ret)
        mark_allocated(bitmap, slot, ret.reg_count)
```

#### find\_contiguous\_free: Inlined Bitmap Scan

The `find_contiguous_free` call in the pseudocode above is not a separate function -- it is inlined directly in `sub_19CA730`. The algorithm scans the 256-byte free-list for `count` contiguous free slots starting at an aligned offset. Each candidate start position is forced to the next alignment boundary using a negative-mask trick.

```
// Inlined in sub_19CA730 (lines ~395-421 of decompiled output).
// bitmap: byte[256], 0xFF = free, 0x00 = occupied.
// count:  registers needed = ceil(param_bytes / 4).
// align:  register alignment = min(type_width, 16) / 4.
//         1 for 4-byte, 2 for 8-byte, 4 for 16-byte params.
// base:   first parameter register (e.g. R4).
// avail:  total slots available for parameters.
//
// Returns offset from base (not absolute register number).
// Returns -1 on failure (falls through to non-register path).

function find_contiguous_free(bitmap, count, align, base, avail):
    neg_mask = -align                            // e.g. -2 = 0xFFFFFFFE
    offset   = (neg_mask & (align - 1 + base)) - base  // first aligned slot

    while offset < avail:
        // Phase 1: find an aligned slot whose byte is free (0xFF)
        if bitmap[offset] == 0x00:               // slot occupied
            next = offset + 1
            offset = (neg_mask & (align - 1 + base + next)) - base
            continue

        // Bounds check: would count registers fit?
        if offset + count > avail:
            return -1                            // no room

        // Phase 2: verify all count slots are contiguous-free
        if count == 1:
            return offset                        // single-reg, done

        ok = true
        for i in 1 .. count-1:
            if bitmap[offset + i] == 0x00:       // gap found
                // Jump past the gap to next aligned position
                next = offset + i + 1
                offset = (neg_mask & (align - 1 + base + next)) - base
                ok = false
                break
        if ok:
            return offset

    return -1                                    // exhausted
```

After a successful find the caller writes `abs_reg = base + offset`, calls `assign_register` (`sub_7FA420`) to record the physical register, calls `mark_allocated` (`sub_BDBB80`) for each register in the range, and zeroes the corresponding bytes in `bitmap[]` via an inlined `memset`.

The allocator processes parameters and return values as separate classes, each requiring contiguous register ranges with natural alignment. For 8-byte parameters, the base register must be even-aligned. For 16-byte parameters, the base must be 4-register-aligned.

The population count helper (`sub_19C99B0`, 2568 bytes) uses the `__popcountdi2` intrinsic to count live registers in the function's usage bitmap, determining how many slots remain available.

## Return Address Register

The return address occupies a dedicated register (or register pair) whose location is validated against parameter ranges. The diagnostic `"Parameter registers from R%d to R%d overlap with return address register R%d to R%d"` fires when parameter and return address ranges collide.

### Return Address Modes

The return address validator (`sub_19CDFF0`, 7.5 KB, 99% confidence) handles four modes, selected by the `v7` field in the ABI specification:

| Mode | `v7` | Behavior |
|------|------|----------|
| Fixed | 1 | Return address at register `4 + 2 = R6`. Fixed by architecture. |
| Regular | 2 | General-purpose register, validated `< max_reg`. |
| Uniform | 3 | Uniform register (UR) for return address. Requires SM support (sm\_75+). |
| Computed | 5 | Derived from parameter layout. Auto-aligned to even register number. |

### Return Address Validator: sub\_19CDFF0

The most thoroughly instrumented function in the ABI engine (7 distinct warning codes across two mode-specific paths). It performs these validations in sequence:

| Code | Condition | Message |
|------|-----------|---------|
| 7001 | `return_addr & 1 != 0` | `"ABI return address %d is unaligned"` |
| 7002 | `return_addr >= max_reg` | `"Return Address (%d) should be less than %d"` |
| 7003 | `stack_ptr` in `[return_addr, return_addr+1]` | `"Return address (%d) should not overlap with the stack pointer (%d)"` |
| 7004 | Return addr bit set in parameter bitmap | `"Return Address %d overlaps with parameters in range %d - %d"` |
| 7005 | `param_end + align > max_reg` (auto-placement) | `"With specified parameters, return address is %d registers and exceeds specified max reg (%d)"` |
| 7008 | `return_addr < lower_bound or return_addr > upper_bound` | `"Return address (%d) should be between %d and %d"` |
| 7009 | Mode 3 and `!(func+1408 byte & 0x02)` | `"SM does not support uniform registers for return address"` |

The checks are mode-dependent. Mode 2 (regular GPR) enters the 7002/7001/7003/7004 path. Modes 3 and 5 (uniform/computed) enter the 7009/7008/7001 path. Mode 1 and mode 5 share the auto-placement path where 7005 fires. Warning 7001 (unaligned) appears in both paths because 64-bit return address pairs always require even alignment.

### Return Address Setup: sub\_19D1720

The setup function (4.8 KB, 95% confidence) runs before the validator. It propagates ABI flag `0x04` to the function state (byte 1389), validates that the return address register (register 1) is not classified as scratch when it must be preserved (warning 7012: `"%d register should not be classified as scratch"`), sizes the preserved register set to 255 entries via `sub_BDBAD0`, and computes the effective register range as `return_size + param_size` for comparison against the maximum available. The 7012 check fires when `*(abi_spec+88) & 0x01` and `*(abi_spec+48) & 0x02` are both set, always with argument 1 (the return address register).

The function also enforces the mutual exclusion rule (warning 7006): `"ABI allows either specifying return address or return address before params"`. This fires when mode is 1 (fixed, "return address before params") but an explicit return address register is also assigned (`return_addr != -1`). You pick one strategy, not both.

## Scratch Data Registers

Registers not reserved by the ABI and not used for parameters or return values may be classified as **scratch** (callee-clobbered). The ABI engine tracks scratch classification per register and validates it against coroutine semantics. At SUSPEND points in coroutine functions, a register marked as scratch must not also appear in the preserved set. Violation triggers warning 7011.

The scratch/preserved classification feeds into the register allocator's spill decisions. Registers marked as scratch across a call boundary must be saved by the caller; preserved registers must be saved by the callee. The spill codegen (`sub_94F150`) adjusts per-vreg spill costs at every CALL instruction (opcode 97) based on this classification:

```
function adjust_spill_cost_for_abi(alloc, func, instr):
    // Called within sub_94F150's instruction walk when opcode == 97.
    // Epoch counter tracks call boundaries for liveness segmentation.
    if regclass_count(alloc) - 1 <= 2 or is_epoch_boundary(func, instr):
        epoch++                                        // sub_936CF0 check

    // For each vreg operand of this CALL instruction (reverse order):
    for each operand vreg of instr:
        if operand_type(vreg) != REGISTER: continue
        if is_pre_colored(alloc, instr, vreg.id): continue   // sub_9446D0

        // --- secondary cost: always accumulated at vreg+76 ---
        vreg.secondary_cost += block_freq
        if prev_use_chain[vreg] == NULL:               // first ref in block
            vreg.secondary_cost += block_freq          // double for kill point

        // --- caller-save multiplier ---
        // vtable+16 probes: live across call in both directions?
        if live_across(alloc, instr) and live_across(alloc, instr.next)
           and live_across(alloc, instr.prev):
            cost_mult = 2.0                            // save + restore
        else:
            cost_mult = 1.0                            // save OR restore

        // --- scratch-class gate (vreg+48 flags) ---
        flags = vreg.flags
        scratch_bitmask = spill_info[vreg.regclass]    // v84[3 * vreg.class]

        if vreg.pair_width == 1:                       // single-width register
            // nesting-depth cost only (no ABI penalty)
            goto simple_path

        if (flags & 0x200) != 0:                       // uniform register -> exempt
            continue

        if is_def(operand):
            if (alloc.force_caller_save or (scratch_bitmask & 2) == 0) \
               and (flags & 0x800) != 0                // scratch bit set
               and (flags & 0x400) == 0:               // preserved bit clear
                // Scratch across call: try short save chain first
                save_len = estimate_save_chain(alloc, vreg, instr)  // sub_944740
                if save_len <= alloc.save_limit:       // alloc+1544
                    vreg.spill_cost += (float)save_len * block_freq
                else:
                    vreg.spill_cost += cost_mult * (2 * base_weight) * block_freq
            else:
                // Preserved or unconditional scratch -> full double-weight
                vreg.spill_cost += cost_mult * (2 * base_weight) * block_freq
```

The `2 * base_weight` factor (30.0 default, 6.0 under pressure) reflects the paired save+restore cost: one store before the call, one load after. The `cost_mult` further doubles to `4 * base_weight` when the vreg is provably live in both directions across the call. Uniform registers (flag `0x200`) are exempt because they use a separate spill path through the UR file. The save chain estimate (`sub_944740`) provides a cheaper alternative when the caller-save sequence is short enough, bounded by `alloc+1544` (default 4.0, from knob 680).

## Per-Pass Instruction Lowering: sub\_19DC4B0

The instruction-level ABI transform driver (6459 bytes, 95% confidence). Called from both `sub_98F430` and `sub_A9DDD0`. It makes three passes over the instruction stream, each performing different transformations:

### Pass 1 -- Convergent Boundary Fixup

- Fixes convergent boundary annotations (`allowConvAlloc`).
- Handles `SHFL.NI` (shuffle, no-index) fixups for intra-warp communication.
- Propagates the `.uniform` bit on `CAL` (call) instructions.

### Pass 2 -- Instruction Lowering

Lowers high-level Ori opcodes into ABI-conforming SASS sequences. Entry is gated on a disjunction of six ABI capability flags read from `abi_spec+1096..1107` crossed against function state flags at `ctx+1368..1382`. If none of the flags are set, Pass 2 returns immediately.

| Ori opcode | Mnemonic | Gate flag (abi\_spec) | Handler |
|------------|----------|----------------------|---------|
| 109 | CALL | `+1105 & 0x01` | `sub_19D5680` (param fixup) then `sub_19D7160` (callee-save rewrite) |
| 16 | ST | `+1096 & 0x10` | `sub_19D5520` |
| 77 | LD | `+1105 & 0x08` | `sub_19D5850` (only if operand bit 7 set and mem-mode is shared/2 or indexed-shared) |
| 185 | ATOMG | `+1105 & 0x20` | `sub_19D5DD0` |
| 183 | (special) | `+1107 & 0x02` or `+1105 & 0x02` | `sub_7E2670` reclassification to mode 2/3 |

The instruction stream is scanned linearly. For CALL instructions (opcode 109), two sub-phases execute in sequence:

#### CALL lowering: parameter register fixup (`sub_19D5680`)

Runs when the callee has no explicit ABI spec (`*(callee_desc+24) == 0`) and `abi_spec+1105 & 0x01`. Rewrites operands referencing the ABI parameter base R41 (`0x29`) to point at a freshly allocated frame-local copy.

```
lower_call_params(ctx, call_insn):
    bitmap = alloc_bitmap(ctx, call_insn.operand_count)      // sub_BDBAD0
    for i = call_insn.operand_count-1 downto 0:
        op = call_insn.operand[i]                            // at +84 + 8*i
        if op < 0:  break                                    // sentinel
        if op.type != REG(1) or (op.byte7 & 1):  continue   // GPR, non-predicated
        if (op & 0xFFFFFF) != 0x29:  continue                // not param base R41
        callee = resolve_callee(ctx, call_insn)              // func_table[+392]
        if !needs_param_frame(callee, ctx, call_insn, bitmap):  continue  // sub_19DF900
        frame_reg = alloc_frame_reg(ctx, reg_file=6)         // sub_91BF30
        copy_size = compute_copy_size(ctx, call_insn, i)     // sub_91E610
        emit_before(ctx, call_insn, MOV/0x82, copy_size,
                    dst=frame_reg, src=op)                    // sub_92E720
        for j = i downto 0:                                  // rewrite bitmap-covered ops
            if bitmap.test(j):
                call_insn.operand[j] = frame_reg | (op[j] & 0xFF000000)
        return 1
    return 0
```

#### CALL lowering: callee-save operand rewrite (`sub_19D7160`)

Runs after parameter fixup when the CALL has operands with type 5 (callee-save reference) or the `0x1000000` needs-save flag. Walks operands in reverse and rewrites non-stack-save operands into frame MOVs.

```
lower_call_saves(ctx, call_insn):
    if (get_flags(call_insn) ^ 0x70000000) & 0x70000000 == 0:
        return 0                                     // no ABI operands
    count = 0;  i = call_insn.operand_count - 1
    // Phase A: skip trailing operands whose spill class has bit 10 (stack-save)
    while i >= 0:
        op = call_insn.operand[i]
        if op < 0:  break
        if (op.type == 5 or op.needs_save) and is_valid_operand(call_insn, i):
            if get_spill_class(lookup_reg(ctx, op)).bit10:  break
        i--
    // Phase B: rewrite remaining callee-save operands into frame MOVs
    while i >= 0:
        op = call_insn.operand[i]
        if op < 0:  break
        if op.type == 5:
            frame_reg = alloc_frame_reg(ctx, reg_file=6)
            emit_before(ctx, call_insn, MOV/0x82, 10,
                        dst=frame_reg, src=op)               // sub_934630
            count++
            call_insn.operand[i] = (op & 0x8F000000) | (frame_reg & 0xFFFFFF) | 0x10000000
        i--
    return count
```

These two sub-phases connect to the separate opcode-72 (CAL) lowering in `sub_19CFC30` ([below](#opcode-level-abi-dispatch-sub_19cfc30)). Opcode 109 is the Ori-level abstract call; opcode 72 is the SASS-level concrete call. Pass 2 rewrites CALL operands; `sub_19CFC30` later emits the pre-call save (`sub_19CB590`: STL/STS bracket) and post-call restore (`sub_19CB7E0`: LDL/LDS bracket) around the CAL.

### Pass 3 -- Architecture-Specific Fixups

Conditioned on SM generation:

**sm\_50 (generation == 4):** Label fixups, coroutine code insertion, shared memory WAR insertion, convergent boundary checks.

**sm\_60+ (generation == 5):** Additional register reservation for ABI conformance, cooperative launch handling, extended register file support.

**All architectures:** Per-block instruction scanning for opcode 195 (MOV) and opcode 205 reclassification. Register reservation range setup via `sub_7358F0` / `sub_7AC150`.

## Opcode-Level ABI Dispatch: sub\_19CFC30

A dispatcher called twice from `sub_98F430` that routes individual opcodes to specialized ABI handlers:

| Ori opcode | Handler | Transform |
|------------|---------|-----------|
| 9 | `sub_19CF9A0` | PRMT (permute) lowering |
| 54 | (inline) | Function parameter preallocation |
| 72 | `sub_19CDED0` + `sub_19CB590` + `sub_19CB7E0` | SMEM reservation + pre/post call register save/restore |
| 98 | `sub_19CBAC0` | Shared load (LD.S) ABI lowering |
| 159 | `sub_19CD0D0` | Barrier instruction lowering |
| 164 | `sub_19CC1A0` | Register load (transfer lowering) |
| 168 | `sub_19CC1A0` | Register store (transfer lowering) |
| 183 | `sub_19CBE00` | Special instruction fixup |
| 226 | `sub_19CD950` | Predicate lowering |
| 236 | `sub_19CD510` | Conversion instruction lowering |
| 335 | `sub_19CDED0` | SMEM reservation instruction handler |

## Register Transfer Lowering: sub\_19CC1A0

The register-to-register transfer lowering function (3873 bytes, 95% confidence). It converts abstract register load/store operations (opcodes 164 and 168) into concrete SASS instruction sequences. The lowering path depends on the ABI function properties:

**Direct copy path** (byte 12 == 0): Register-to-register MOV instructions.

| Data width | Generated sequence |
|------------|-------------------|
| 4 bytes (32-bit) | Single MOV-like (opcode 130 / `0x82`, `HSET2` in ROT13; actual SASS `MOV` is opcode 19) |
| 8 bytes (64-bit) | `STS` + `LDS` pair (opcodes 0x86/0x85) through shared memory |
| Permute | `PRMT` (opcode 0x120) for byte-lane rearrangement |

**Shared memory indirect path** (byte 13 == 1): All transfers go through shared memory via `STS`/`LDS` pairs, using a reserved shared memory region as a scratch buffer. This path is used when direct register-to-register transfer is not possible (e.g., cross-warp parameter passing on older architectures or when the register file is partitioned).

The function also generates opcode 0xB7 (special) for shared-memory-based transfers that require additional synchronization. It calls `sub_92E800` (instruction builder) for each generated SASS instruction.

## Convergent Boundary Enforcement

Two functions enforce convergent allocation boundaries for function calls annotated with `allowConvAlloc`:

**Convergent boundary checker** (`sub_19D13F0`, 4.3 KB):

```
check_convergent_boundaries(func, call_insn):
    num_words = (func.reg_count + 64) >> 6       // 64-bit bitmask words
    bitmask   = alloc(num_words * 8)              // one bit per register slot
    callback  = sub_19C6400                       // single-call-per-region checker

    start_bb = lookup_bb(func.bb_array, call_insn.dst_reg)   // +296 index
    end_bb   = idom_or_fallthrough(start_bb)      // opcode-97 edge or idom

    bb = start_bb
    while bb != end_bb:
        if not scan_convergent_boundary(bitmask, bb, func.bb_array)
           and (not bitmask.any_set or not bitmask.any_low):
            if call_insn.qword280 & 1 == 0:       // allowConvAlloc not self-guarded
                target = func.callee(call_insn)    // +344 -> +64 -> byte 40
                if not target or not target.has_conv_boundary:
                    emit_warning(7020,
                        "Missing proper convergent boundary around "
                        "func call annotated with allowConvAlloc")
        bb = bb.next                               // linked-list successor
    free(bitmask)
```

**CONV.ALLOC insertion** (`sub_19D7A70`, 3313 bytes):

```
insert_conv_alloc(func, allow_reorder) -> int:
    num_words  = (func.reg_count + 64) >> 6       // field +376
    live_mask  = alloc(num_words * 8)              // convergent-live bitmask
    inserted   = 0
    high_water = -1                                // highest word touched

    for block_id in 0 .. func.block_count-1:       // +792 count, +368 array
        block = func.blocks[block_id]
        if block.first_insn == null: continue
        if block.flags[281] & 0x08: continue       // skip non-schedulable

        bit_word = block_id >> 6
        bit_mask = 1 << (block_id & 63)
        start_bb = lookup_bb(func.bb_array, block.first_insn.dst_reg)

        for each bb from start_bb via successor chain:
            if allow_reorder and bb.byte245: break  // hit reorder barrier
            // -- scan for CONV boundary markers (opcode 27 after mask) --
            for insn in bb.insn_list:
                if (insn.opcode & ~0x3000) == 27:
                    if (high_water+1) << 6 > block_id:
                        already = (live_mask[bit_word] >> (block_id&63)) & 1
                        if block.qword280 & 1:     // guard bit -> clear
                            live_mask[bit_word] &= ~bit_mask
                        elif not already:           // first encounter -> set
                            zero_extend(live_mask, high_water+1, num_words)
                            live_mask[bit_word] |= bit_mask
                            high_water = num_words - 1
            // -- check reaching use across non-convergent path --
            use_bb = resolve_reaching_use(func, bb)
            if use_bb == null: continue
            if use_bb.first_def.opcode == 286: continue  // already CONV.ALLOC
            if needs_conv_alloc(use_bb.first_def, func):
                if bb.order > use_bb.order or not allow_reorder:
                    slot = alloc_virtual_reg(func, -1)
                    ops  = [use_reg & 0xFFFFFF | 0x90000000, 0, slot, 0]
                    new  = build_insn(func, 0x11E, size=12, nops=2, ops)
                    insert_before(use_bb.insn_list, new)
                    mark_convergent(use_bb.parent, flag=17)
                    inserted += 1

    free(live_mask)
    return inserted
```

The single-call checker (`sub_19C6400`) warns when a convergent region contains more than one call: `"Multiple functions calls within the allowConvAlloc convergent boundary"`.

## Coroutine Support

Functions with coroutine support (flag `0x01` at function byte `+1369`) receive special ABI handling. Registers that are live across SUSPEND points must be saved to and restored from the coroutine frame.

**Coroutine SUSPEND handler** (`sub_19D5F10`, 1568 bytes): Scans the instruction stream for suspend points. For each register defined before and used after a SUSPEND, inserts save/restore pairs to/from the coroutine frame.

**Coroutine frame builder** (`sub_19D4B80`, 1925 bytes): Constructs the frame layout for coroutine-style functions, allocating slots for each register that must survive a SUSPEND.

The ABI engine validates that the scratch/preserved classification is consistent with coroutine semantics. Warning 7011 fires when a register marked as scratch at a SUSPEND point is also required to be preserved for the coroutine function. Warning 7012 fires when the return address register itself is misclassified as scratch.

### handle\_suspend\_point (sub\_19D5F10)

```
handle_suspend_point(func_ctx):
    func = *func_ctx
    if !(func[+1369] & 0x01): return              // not a coroutine
    mode = get_coroutine_mode(func)                // sub_7DDB50

    if mode == 1:                                  // simple: per-block scan
        for bb_idx in 0 .. func[+304]:
            first = *func.bb_array[bb_idx][+8]
            if first[+72] & 0xFFFFCFFF == 0x7C and last_src_regclass(first) == 3:
                insert_save_restore(func, bb)      // sub_19D31D0
        return

    // full path: build liveness then walk flat instruction list
    build_liveness(func)                           // sub_781F80, sub_A0B310
    save_set = {};  restore_set = {};  seen_suspend = seen_resume = false
    for instr in func.instr_list(+272):
        opc = instr[+72] & 0xFFFFCFFF
        if opc in {SUSPEND(183), RESUME(288)}:
            if get_mem_space(instr.last_src) & ~2 == 1:  // local/spill
                seen_suspend |= (opc==183);  seen_resume |= (opc==288)
        elif opc == CALL(97):
            tgt = func.bb_array[instr.src0 & 0xFFFFFF]
            save_set = &tgt[+96];  restore_set = &tgt[+48];  continue
        for each vreg operand (tag bits[31:28]==1, file==6, index-41 > 3):
            if operand >= 0:    add(save, vr.hw_reg); remove(restore, vr.hw_reg)
            elif seen_suspend:  add(restore, vr.hw_reg)   // use after SUSPEND
            else:               add(save, vr.hw_reg); remove(restore, vr.hw_reg)
            if vr.is_64bit: same for hw_reg+1

    if !(seen_suspend and seen_resume): return
    recompute_liveness(func)                       // sub_A0BA40
    for each suspend_block in func.suspend_list(+512):
        bb = func.bb_array[suspend_block]
        n_live = bitset_count(bb[+96])
        if n_live > 9: insert_save_restore(func, bb)     // large path
        elif n_live > 0:                                  // small: groups of 3
            for reg in bitset_iter(bb[+96]):
                vr = alloc_vreg(func, file=6); vr.hw_slot = reg
                operands[slot++] = vr | 0x10000000
                if slot == 3: emit_group(); break
            if 0 < slot < 3: pad with SENTINEL(0x10000019)
            if ctx[+1044] & 0x10: emit(opc 0x121, 7 ops) // extended SM
            else:                 emit(opc 0x6E,  4 ops)  // base SM
```

### build\_coroutine\_frame (sub\_19D4B80)

```
build_coroutine_frame(func_ctx) -> int:
    frame_count = 0;  anchor = NULL
    warp_group = -1;  seen_local = false
    for instr in func.instr_list(+272):
        opc = instr[+72] & 0xFFFFCFFF
        if opc == BB_BOUNDARY(52):
            if !anchor or !validate_predecessor(anchor):
                reset(); continue
            // emit frame descriptor + retype surrounding blocks
            func.cursor = anchor
            emit(FRAME_MARK, opcode 0x94, 1 operand)
            backend.register_frame(func.cursor)    // vtable[408]
            anchor_bb = anchor[+40]
            anchor_bb[175] = (anchor_bb[175] & 0xE1) | 0x0A
            update_block_type(anchor_bb)           // 16/17->18; 1->2
            tag_block_as_split(func, instr, warp_group)
            frame_count++;  reset()
        elif opc == BRANCH(16) and get_branch_kind(instr)==4:
            anchor = instr
            info = instr[+40]
            warp_group = (info[175] & 0x02) ? ((info[175]>>2)&7) : -1
        elif opc == SUSPEND(183):
            if get_mem_space(instr.last_src)==4:    // local-frame
                if seen_local: reset(); continue
                seen_local = true
        if warp_group != -1:                       // barrier-group guard
            mask = (instr.bb[174] >> 1) & 0x3F
            if bit_test(mask, warp_group): reset(); continue
        if anchor and opc == RESUME(288):
            // validate: space==4 or (space==1 + vreg is non-live scratch)
            if valid: update anchor attrs, tag_block_as_split(...)
    return frame_count
```

## gb10b Hardware WAR

Two functions implement a shared-memory-based workaround for a hardware bug on the gb10b variant (SM 75, Turing). Both reference the reserved symbol `__nv_reservedSMEM_gb10b_war_var`.

**Entry block variant** (`sub_19D9E00`): Generates a complex instruction sequence using additional temp registers (opcodes ADD, MOV, BAR) for the function entry block.

**Body variant** (`sub_19DA2A0`, 95% confidence): Generates a 7-instruction SASS sequence:

```
1. MOV.C  temp_reg, <constant>           // opcode 195, class 3
2. LD.S   temp_reg, [__nv_reservedSMEM_gb10b_war_var]  // opcode 98
3. AND    temp_reg, temp_reg, 4           // opcode 214
4. SETP   P, temp_reg, 0x4000            // opcode 272
5. STS    [__nv_reservedSMEM_gb10b_war_var], temp_reg   // opcode 277
6. @P BRA target                          // opcode 18, predicated
7. MOV    result, 0                       // zero-initialization
```

The reserved SMEM checker (`sub_19DDEF0`, 1687 bytes) iterates instructions looking for opcode 335 (SMEM reservation). When found and the function is not allowed to use reserved shared memory, it emits warning 7801: `"Function '%s' uses reserved shared memory when not allowed."`.

## ABI Register Limit Propagation

The limit propagator (`sub_19CE590`) handles inter-procedural ABI attribute forwarding. For SM generations 4 and 5 (sm\_50, sm\_60 families), it iterates the call graph and copies the max-register limit from caller to callee (field `+264` to `+268`) unless the callee has an explicit ABI specification. This ensures that callees do not exceed the register budget established by their callers.

### propagate\_abi\_limits (sub\_19CE590)

```
propagate_abi_limits(compilation_ctx):
    target = compilation_ctx[+8]
    sm_gen = target[+896]
    if sm_gen < 4 or sm_gen > 5: return            // sm_50 / sm_60 only

    // Phase 1: seed effective limit per function
    func_array = target[+368];  n_funcs = target[+376]
    for i in 0 .. n_funcs:
        func = func_array[i]
        if func[+280] & 0x01:                      // explicit ABI spec
            func[+268] = func[+264]                // copy declared limit
        elif func[+216] >= 0                        // has a caller index
              and call_graph[func[+216]][+57]:       // caller propagatable
            func[+268] = func[+264]
        else:
            func[+268] = -1                        // unconstrained

    // Phase 2: reverse-order call-graph edge walk
    edge_list = target[+792]                       // {count, data[]}
    for edge_idx in (edge_list.count - 1) downto 0:
        caller = func_array[edge_list.data[edge_idx]]
        for callee in caller.callee_list(+136):    // linked list
            if callee[+216] >= 0 and call_graph[callee[+216]][+57]:
                callee[+268] = callee[+264]        // keep own limit
            else:
                callee[+268] = caller[+268]        // inherit caller budget
```

## Call Instruction ABI Lowering: sub\_19D41E0

The call lowering function (2247 bytes, 85% confidence) processes each call instruction (opcode 97; `STG` in the ROT13 name table, but used here as an internal CALL-like marker -- actual SASS CALL is opcode 71) in the function. For each call site it:

1. Sets up parameter passing registers according to the callee's ABI specification.
2. Inserts pre-call register save sequences for caller-saved registers.
3. Modifies the call target to use ABI-conforming register assignments.
4. Inserts post-call register restore sequences.

## Register File Types

The ABI handles three register file types, each with distinct allocation rules:

| Type | `v7` value | File | Range | SM requirement |
|------|------------|------|-------|----------------|
| GPR | 2 | General-purpose | R0--R255 | All architectures |
| Uniform | 3 | Uniform GPR | UR0--UR63 | sm\_75+ |
| Predicate | 5 | Predicate | P0--P7 | All architectures |

Uniform registers (type 3) are only available on sm\_75 and later. Attempting to use a uniform register for the return address on an older SM triggers warning 7009.

## Pipeline Integration

The ABI engine sits between the optimization passes and the register allocator in the ptxas pipeline:

```
... optimization passes ...
  Late Legalization / Expansion
  ABI Master Setup              <-- sub_19D1AF0 (per-function)
  ABI Pass 1 (convergent)       <-- sub_19DC4B0 (a2=1)
  ABI Pass 2 (lowering)         <-- sub_19DC4B0 (a2=2)
  ABI Opcode Dispatch           <-- sub_19CFC30 (2x)
  ABI Pass 3 (arch-specific)    <-- sub_19DC4B0 (a2=3)
  Register Allocation           <-- sub_9721C0
  Instruction Scheduling
  SASS Encoding
```

The ABI engine produces new SASS instructions via `sub_934630` / `sub_9314F0` (instruction builder/inserter) and uses `sub_91BF30` (temp register allocation) for scratch registers needed during lowering. During final emission, the encoding functions in Zone B (`0x1A01000`--`0x1A76F30`) convert the ABI-lowered instructions into binary SASS words.

## ABI State Object Layout

The ABI engine operates on three nested data structures: the **ABI engine context** (the `this` pointer passed as `a1` to all ABI functions), the **per-callee ABI specification** (one per callee in the call graph), and **parameter/return descriptor entries** (one per parameter or return value). All offsets are byte offsets from the structure base.

### ABI Engine Context

The top-level per-function ABI state, passed as `a1` to `sub_19D1AF0`, `sub_19CA730`, `sub_19CDFF0`, and `sub_19D1720`. Total size is at least 4672 bytes.

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| `+0` | 8 | `ptr` | `vtable` | Dispatch table; method at `+144` dispatches per-callee validation, `+152` selects register reservation strategy |
| `+8` | 8 | `ptr` | `func_ctx` | Pointer to per-function compilation context (1716+ bytes); accessed everywhere as `*(_QWORD *)(a1+8)` |
| `+16` | 1 | `byte` | `abi_mode_flags` | Master ABI mode selector; 0 = no ABI lowering, nonzero = full pipeline |
| `+64` | 4 | `int` | `max_param_offset` | Highest parameter register offset seen during callee iteration |
| `+76` | 4 | `int` | `preserved_param_start` | Start register for preserved parameter range |
| `+80` | 4 | `int` | `preserved_param_align` | Alignment requirement for preserved parameter range |
| `+88` | 8 | `ptr` | `current_callee_entry` | Pointer to the callee entry node being processed in the current iteration |
| `+97` | 1 | `byte` | `skip_popcount` | When set, skips the register usage population count (`sub_19C99B0`) |
| `+98` | 1 | `byte` | `has_return_addr_spec` | Set to 1 when any callee has a return address ABI specification |
| `+4428` | 4 | `int` | `cached_reg_R3` | Cached physical register ID for R3 (from `sub_7FA420(regfile, 6, 3)`) |
| `+4432` | 4 | `int` | `cached_reg_R2` | Cached physical register ID for R2 (from `sub_7FA420(regfile, 6, 2)`) |
| `+4449` | 1 | `byte` | `first_callee_seen` | Set after the first callee with an ABI spec is processed; controls whether per-class reservation bitmaps are populated or inherited |
| `+4456` | 16+ | `bitvec` | `param_alloc_bitmap` | Bitvector tracking which physical registers have been assigned to parameters; manipulated via `sub_BDBB80` (set bit), `sub_BDDCB0` (find highest), `sub_BDDD40` (popcount) |
| `+4472` | 4 | `int` | `param_alloc_count` | Number of registers allocated for parameter passing |
| `+4480` | 16+ | `bitvec` | `retval_alloc_bitmap` | Bitvector tracking which physical registers have been assigned to return values |
| `+4496` | 4 | `int` | `retval_alloc_count` | Number of registers allocated for return values |
| `+4528` | 144 | `bitvec[6]` | `per_class_reservation` | Per-register-class ABI reservation bitmaps; 6 entries (classes 1--6), 24 bytes each; the loop in `sub_19D1AF0` iterates `v148` from 1 to 6, incrementing the pointer by 3 qwords per iteration |

The `param_alloc_bitmap` and `retval_alloc_bitmap` are used after parameter/return allocation to compute the effective register file occupancy. The master setup reads the highest set bit in each (`sub_BDDCB0`) to determine `func_ctx+361` (total register demand) and compares against `func_ctx+367` (register file limit).

### Per-Callee ABI Specification

Pointed to by `*(callee_entry + 64)`. One instance per callee in the call graph. Describes how parameters are passed, return values are received, and the return address is placed. Accessed as `v3`/`v12`/`v14` (cast to `_DWORD *`) in the decompiled code, so integer-indexed fields are at 4-byte stride.

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| `+0` | 4 | `int` | `param_count` | Number of parameter descriptor entries |
| `+4` | 4 | `int` | `return_count` | Number of return value descriptor entries |
| `+8` | 8 | `ptr` | `param_descriptors` | Pointer to array of 32-byte parameter descriptor entries |
| `+16` | 8 | `ptr` | `return_descriptors` | Pointer to array of 32-byte return value descriptor entries |
| `+24` | 4 | `int` | `return_addr_register` | Explicit return address register number; -1 = unassigned |
| `+28` | 4 | `int` | `return_addr_mode` | Return address placement strategy (see table below) |
| `+32` | 4 | `int` | `first_param_register` | First register available for parameter passing; -1 = use default |
| `+36` | 4 | `int` | `available_reg_count` | Number of registers available; -1 = target default, -2 = computed from target descriptor |
| `+40` | 1 | `byte` | `ret_addr_before_params` | If set, return address is placed before the parameter range |
| `+44` | 4 | `int` | `preserved_reg_type` | Preserved register specification type; 1 triggers per-register scratch bitmap construction |
| `+48` | 8 | `uint64` | `scratch_gpr_bitmask` | Bit 1 (`& 2`) = scratch classification active for GPR return address register |
| `+57` | 1 | `byte` | `has_abi_spec` | Master enable: 0 = callee has no ABI specification, 1 = specification is active |
| `+58` | 1 | `byte` | `allocation_complete` | Set to 1 after parameter/return allocation finishes successfully |
| `+64` | 8 | `ptr` | `abi_detail_ptr` | Pointer to extended ABI detail sub-object (preserved bitmasks, scratch classification) |
| `+80` | 8 | `uint64` | `preserved_pred_bitmask` | Per-predicate-register preserved bitmask; bit N = predicate register N is preserved |
| `+88` | 4 | `uint32` | `preserved_class_flags` | Bit 0 (`& 1`) = GPR preserved set active; bit 1 (`& 2`) = scratch classification active |
| `+96` | 1 | `byte` | `return_addr_validated` | Set to 1 after `sub_19CDFF0` completes validation for this callee |

Return address mode values (field `+28`):

| Value | Mode | Behavior |
|-------|------|----------|
| 1 | Fixed | Return address at `first_param_register + 2` (e.g., R6 when base is R4) |
| 2 | Regular | General-purpose register, validated `< max_reg` |
| 3 | Uniform | Uniform register (UR), requires SM75+ (`func_ctx+1408 & 0x02`) |
| 5 | Computed | Derived from parameter layout, auto-aligned to even register boundary |

### Parameter/Return Descriptor Entry (32 bytes)

Each parameter or return value is described by a 32-byte entry. The allocator iterates the parameter array with stride 32 (`v34 += 32` per parameter) and the return array identically (`v43 += 32` per return value).

| Offset | Size | Type | Field | Notes |
|--------|------|------|-------|-------|
| `+0` | 4 | `int` | `element_count` | Number of elements (e.g., 4 for a `float4`) |
| `+4` | 4 | `int` | `element_size` | Size per element in bytes (e.g., 4 for `float`) |
| `+8` | 4 | `int` | `alignment_hint` | Alignment in bytes, clamped to `[4, 16]`; 8 = even-aligned, 16 = quad-aligned |
| `+12` | 1 | `byte` | `is_register_allocated` | 0 = stack-passed (fallback), 1 = register-allocated |
| `+16` | 4 | `int` | `assigned_register_id` | Physical register ID assigned by the allocator (from `sub_7FA420`) |

The total byte size is `element_count * element_size`. The register count is `ceil(total_bytes / 4)`, computed as `(total + 3) >> 2`. The alignment mask applied to register slot selection is `-(alignment_hint >> 2)`, producing a bitmask that enforces natural alignment: 8-byte parameters require even-aligned base registers, 16-byte parameters require 4-register-aligned bases.

### 2048-Bit Free-List Bitmap (Stack Local)

The parameter allocator (`sub_19CA730`) constructs a 2048-bit free-list bitmap as a **stack-local** variable (not stored in the engine context). It is declared as `v103[31]` (248 bytes of QWORD array) plus `v104` (4 bytes), `v105` (2 bytes), and `v106` (1 byte), totaling 255 bytes.

```
Initialization:
  memset(v103, 0xFF, 248)     // 248 bytes all-ones
  v104 = 0xFFFFFFFF           // 4 bytes
  v105 = 0xFFFF               // 2 bytes
  v106 = 0xFF                 // 1 byte
  Result: 2040 bits all-ones (255 bytes)
```

A bit value of **1** means the register slot is free; **0** means occupied. The bitmap is indexed relative to `first_param_register`, not absolute R0. When a contiguous run of free slots is found for a parameter, the allocator zeroes the corresponding bytes using a size-optimized zeroing sequence (special-cased for lengths < 4, == 4, and >= 8 bytes). After allocation, the assigned registers are also recorded in the persistent bitvectors at `+4456` (parameters) and `+4480` (return values) via `sub_BDBB80`.

The bitmap supports up to 2040 register slots, far exceeding the 255-register GPR limit. This over-provisioning accommodates the allocator's use for both parameter and return value allocation in a single bitmap, and provides headroom for potential multi-class allocation in future architectures.

### Target Descriptor Fields Referenced by ABI Engine

The ABI engine accesses the target descriptor (at `func_ctx+1584`) through these offsets during ABI setup:

| Offset | Type | Purpose |
|--------|------|---------|
| `+372` | `int` | SM generation index (value `>> 12`; 3=Kepler, 4=Maxwell, 5=Pascal+, 9=Hopper, >9=Blackwell) |
| `+452` | `int` | SM version number; `> 4` gates 64-bit return address pair semantics |
| `+616` | `int` | Available register count ceiling for the target |
| `+636` | `int` | Register count subtraction base (for computed available_reg_count) |
| `+896` | `vfunc` | Register range query; called with `(target, func_ctx, &query, 6)`, returns low/high range pair at `query+24` |
| `+2096` | `vfunc` | Register class capacity query; called with `(target, reg_class)` |
| `+3000` | `vfunc` | Validator callback; `nullsub_464` = no-op (validation skipped) |

The vtable call at `+896` takes a 32-byte query structure initialized to `{hi=-1 lo=0, 0, 0, 0, 0, 148, 148, -1}`. The result at query `+24` (as two 32-bit halves) returns the reserved register range boundaries. This is used by warnings 7014 (reserved range overlaps parameters) and 7017 (insufficient registers for reservation).

## ABI Validation Diagnostics

The ABI engine emits 15 distinct warning codes (7001--7017) from six functions. Two codes are unused in this binary version (7007, 7018). All codes share the contiguous hex ID range `0x1B59`--`0x1B69` and are emitted through two parallel paths: `sub_7EEFA0` (standalone diagnostic buffer) and `sub_895530` (context-attached diagnostic using the compilation context at `*(func+48)`).

### Complete Warning Catalog

| Code | Hex | Emitter | Message | Trigger |
|------|-----|---------|---------|---------|
| 7001 | `0x1B59` | `sub_19CDFF0` | `"ABI return address %d is unaligned"` | `return_addr & 1 != 0` (odd register for 64-bit pair) |
| 7002 | `0x1B5A` | `sub_19CDFF0` | `"Return Address (%d) should be less than %d"` | `return_addr >= max_reg` (exceeds register file) |
| 7003 | `0x1B5B` | `sub_19CDFF0` | `"Return address (%d) should not overlap with the stack pointer (%d)"` | Stack pointer falls within `[return_addr, return_addr+1]` |
| 7004 | `0x1B5C` | `sub_19CDFF0` | `"Return Address %d overlaps with parameters in range %d - %d"` | Return addr bit set in parameter allocation bitmap |
| 7005 | `0x1B5D` | `sub_19CDFF0` | `"With specified parameters, return address is %d registers and exceeds specified max reg (%d)"` | Auto-placed return addr pushed beyond register file limit |
| 7006 | `0x1B5E` | `sub_19D1720` | `"ABI allows either specifying return address or return address before params"` | Mode 1 (fixed) with explicit `return_addr != -1` |
| 7007 | `0x1B5F` | -- | -- | Unused/reserved in this binary version |
| 7008 | `0x1B60` | `sub_19CDFF0` | `"Return address (%d) should be between %d and %d"` | Return addr outside valid range from target vtable query |
| 7009 | `0x1B61` | `sub_19CDFF0` | `"SM does not support uniform registers for return address"` | Mode 3 (uniform) on target without UR support (`!(func+1408 & 0x02)`) |
| 7010 | `0x1B62` | `sub_13B6DF0` | `"Relative 32-bit return address requires a caller-save 64-bit scratch register pair"` | 32-bit relative call without available scratch pair |
| 7011 | `0x1B63` | `sub_19D1AF0` | `"Register (%s%d)is defined as scratch on SUSPEND but preserved for coroutine function"` | Register in preserved set is scratch in SUSPEND bitmap |
| 7012 | `0x1B64` | `sub_19D1720`, `sub_19D1AF0` | `"%d register should not be classified as scratch"` | Preserved ABI register (return addr) misclassified as scratch |
| 7013 | `0x1B65` | `sub_19CA730` | `"%d register used to return value cannot be classified as preserved"` | Return-value register appears in preserved bitmap |
| 7014 | `0x1B66` | `sub_19CA730` | `"Reserved register range %d - %d overlaps with parameters in range %d - %d"` | Explicit reserved range collides with parameter range |
| 7015 | `0x1B67` | `sub_19C69D0` | `"Reserved register range %d - %d overlaps with retAddr %d"` | Reserved range collides with return address register |
| 7016 | `0x1B68` | `sub_19D1AF0` | `"regcount %d specified below abi_minimum of %d"` | `func.maxreg` below generation minimum (16 or 24) |
| 7017 | `0x1B69` | `sub_19D1AF0` | `"register available %d for reservation is less than the requested number of registers %d "` | Available regs after reservation base < requested count |

### Diagnostic Emission Architecture

The ABI engine uses three diagnostic emitters:

**`sub_7EEFA0`** (standalone path): Takes a stack buffer, the decimal warning code, and a printf-format string. Used as the fallback when no compilation context is available (when `*(*(func)+48) == NULL`). This is the path that produces warnings visible in non-context mode (e.g., standalone ptxas invocations).

**`sub_895530`** (context-attached path): Takes the function object, the output context, flags (always 0), the hex warning code, and the format string. Used when the compilation context exists. This is the primary path during normal nvcc-driven compilation.

**`sub_7F7C10`** (conditional emitter): Returns a bool indicating whether the diagnostic was accepted (not suppressed by the diagnostic context at `func+1176`). Used exclusively for warning 7011 (SUSPEND). When it returns true, the caller additionally invokes `sub_8955D0` to attach the diagnostic to the compilation context.

### Validation Order

The ABI master setup (`sub_19D1AF0`) invokes validators in this order:

```
1. regcount vs. abi_minimum       -> 7016
2. register reservation overflow  -> 7017
3. return address setup           -> 7006, 7012  (sub_19D1720)
4. parameter allocation           -> 7013, 7014  (sub_19CA730)
5. reserved range vs. retAddr     -> 7015         (sub_19C69D0)
6. return address validation      -> 7001-7005, 7008, 7009  (sub_19CDFF0)
7. coroutine SUSPEND validation   -> 7011, 7012
```

### Unreferenced ABI Strings

Three ABI-related strings exist in `ptxas_strings.json` with no cross-references in the decompiled binary. They may be dead code, referenced via indirect dispatch, or used only in debug builds:

- `"Caller and callee expected to have different return address register but '%s' and '%s' both use R%d as return address register"`
- `"Function '%s' specifies register R%d as scratch register which is used as return address register"`
- `"Mismatch in return address abi when '%s' calls '%s'"`

## Function Map

| Address | Size | Confidence | Role |
|---------|------|------------|------|
| `sub_19C6400` | ~200 | 90% | Convergent boundary single-call checker |
| `sub_19C69D0` | ~600 | 90% | Reserved register overlap checker |
| `sub_19C7350` | ~900 | 80% | Register bitmap manipulation helper |
| `sub_19C7890` | ~600 | 80% | Register range validator |
| `sub_19C7B20` | ~600 | 80% | Register alignment checker |
| `sub_19C7D60` | ~700 | 80% | Register pair allocator helper |
| `sub_19C8040` | ~700 | 80% | Register contiguous-range finder |
| `sub_19C84A0` | 1927 | 85% | Multi-function register dispatcher |
| `sub_19C8D30` | ~600 | 80% | Register usage merger |
| `sub_19C9010` | ~700 | 85% | Per-function register limit setter |
| `sub_19C92F0` | ~1050 | 85% | Register bitmap AND/OR combiner |
| `sub_19C99B0` | 2568 | 90% | Register usage population counter |
| `sub_19CA3C0` | ~300 | 95% | Return address overlap pre-check |
| `sub_19CA730` | 2277 | 98% | Parameter register allocator |
| `sub_19CB020` | ~200 | 85% | Shared-mem base address calculator |
| `sub_19CB230` | ~200 | 85% | Shared-mem offset calculator |
| `sub_19CB590` | ~350 | 80% | Post-call register restore |
| `sub_19CB7E0` | ~350 | 80% | Pre-call register save |
| `sub_19CBAC0` | ~600 | 85% | Shared load (LD.S) ABI lowering |
| `sub_19CBE00` | ~600 | 85% | Special instruction ABI fixup |
| `sub_19CC1A0` | 3873 | 95% | Register transfer lowering (STS/LDS) |
| `sub_19CD0D0` | ~1050 | 85% | Barrier instruction ABI lowering |
| `sub_19CD510` | ~900 | 85% | Conversion instruction ABI lowering |
| `sub_19CD950` | ~700 | 85% | Predicate lowering |
| `sub_19CDDB0` | ~200 | 80% | Reserved SMEM helper |
| `sub_19CDED0` | ~200 | 85% | SMEM reservation instruction handler |
| `sub_19CDFF0` | ~7500 | 99% | Return address validator |
| `sub_19CE590` | ~300 | 90% | Register limit propagator |
| `sub_19CE6D0` | ~300 | 85% | ABI flag propagator |
| `sub_19CEEF0` | ~200 | 80% | ABI attribute copier |
| `sub_19CF030` | ~200 | 80% | Function entry ABI setup |
| `sub_19CF140` | ~700 | 85% | Register-save sequence builder |
| `sub_19CF530` | ~350 | 80% | Parameter setup helper |
| `sub_19CF9A0` | ~600 | 85% | PRMT instruction ABI lowering |
| `sub_19CFC30` | ~500 | 95% | Opcode-based ABI dispatch |
| `sub_19D01E0` | ~1200 | 85% | Multi-callee ABI propagation |
| `sub_19D0680` | ~300 | 80% | Iterator initialization |
| `sub_19D0A80` | ~200 | 80% | Iterator filter setup |
| `sub_19D0AF0` | ~100 | 95% | Iterator filter check |
| `sub_19D0BC0` | ~40 | 95% | Iterator advance (next instruction) |
| `sub_19D0C10` | ~40 | 95% | Iterator advance (next matching) |
| `sub_19D0C70` | ~40 | 95% | Iterator advance (skip non-matching) |
| `sub_19D0CE0` | ~40 | 95% | Iterator advance (reverse) |
| `sub_19D0EE0` | ~40 | 95% | Iterator reset |
| `sub_19D1030` | ~200 | 80% | Iterator state query |
| `sub_19D13F0` | ~4300 | 90% | Convergent boundary checker |
| `sub_19D1720` | ~4800 | 95% | ABI return address setup |
| `sub_19D1AF0` | 5608 | 98% | Master ABI setup |
| `sub_19D32C0` | 1902 | 85% | Per-block register reservation builder |
| `sub_19D41E0` | 2247 | 85% | CALL instruction ABI lowering |
| `sub_19D4B80` | 1925 | 85% | Coroutine frame builder |
| `sub_19D5850` | ~900 | 80% | Shared-mem instruction lowering |
| `sub_19D5F10` | 1568 | 85% | Coroutine SUSPEND handler |
| `sub_19D67B0` | ~800 | 80% | Function exit ABI lowering |
| `sub_19D7160` | ~600 | 85% | Sub-pass: scan for ABI-relevant ops |
| `sub_19D7470` | 1526 | 80% | Register classification propagator |
| `sub_19D7A70` | 3313 | 85% | CONV.ALLOC insertion (dead instruction insertion) |
| `sub_19D8CE0` | ~1100 | 80% | Register save/restore pair generator |
| `sub_19D9290` | ~1000 | 80% | Register live range computation |
| `sub_19D9710` | ~1000 | 80% | Register conflict detector |
| `sub_19D9E00` | ~700 | 95% | gb10b WAR code generator (entry) |
| `sub_19DA2A0` | ~500 | 95% | gb10b WAR code generator (body) |
| `sub_19DA8F0` | 1580 | 80% | SSA-form instruction rebuilder |
| `sub_19DAF20` | ~1300 | 80% | Multi-dest instruction splitter |
| `sub_19DB440` | ~700 | 80% | Additional register reservation pass |
| `sub_19DC070` | ~900 | 85% | Sub-pass dispatcher |
| `sub_19DC4B0` | 6459 | 95% | Per-pass instruction lowering |
| `sub_19DDEF0` | 1687 | 95% | Reserved SMEM checker |
| `sub_19DE8F0` | 1842 | 80% | Register renaming for ABI conformance |
| `sub_19DF170` | 1928 | 80% | Instruction list rewriter |
