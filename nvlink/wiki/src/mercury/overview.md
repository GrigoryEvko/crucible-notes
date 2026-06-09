# Mercury Overview

Mercury is NVIDIA's internal codename for a new GPU ISA binary format that replaces the legacy SASS (Shader ASSembler) encoding for modern GPU architectures. The name is ROT13-obfuscated throughout the binary as "Zrephel" — applying ROT13 to "MERCURY" yields "ZREPHEL", the form seen in all instruction-level string tables. In nvlink v13.0.88, Mercury surfaces across four distinct subsystems: the MercExpand instruction expansion engine, the capsule mercury (capmerc) ELF format, the R_MERCURY relocation family, and the FNLZR (finalizer) that converts between SASS and Mercury representations.

## String Evidence Summary

| Type | Count | Address Range | Examples |
|---|---|---|---|
| `mercury` / `Mercury` | 82 | `0x1D35A17`--`0x245EF38` | `R_MERCURY_ABS64`, `EIATTR_MERCURY_ISA_VERSION`, `mercury,capmerc,sass` |
| `Zrephel` / `ZREPHEL` | 667 | `0x1D42C80`--`0x1D4DF80` | ROT13-encoded SASS builtins: `ZREPHEL_zoneevre_neevir` = `MERCURY_mbarrier_arrive` |
| `R_MERCURY_*` | 67 | `0x1D35A17`--`0x1D35F4C` | 65 unique relocation types plus `R_MERCURY_NONE` and `R_MERCURY_NONE_LAST` sentinels |
| `.nv.merc.*` | 20 | `0x24582E8`--`0x2458D00` | `.nv.merc.debug_info`, `.nv.merc.rela`, `.nv.merc.symtab_shndx` |
| `capmerc` | 7 | `0x1D33FA9`--`0x1D41EF8` | `capmerc.cubin`, `--binary-kind capmerc`, self-check strings |
| `FNLZR` | 17 | `0x1D32381`--`0x2458F10` | `FNLZR: Input ELF: %s`, `FNLZR: Pre-Link Mode`, `FNLZR: JIT Path` |

## Architecture Generation Mapping

Mercury is not a single monolithic format. It has two deployment tiers tied to GPU architecture:

| Architecture | SM Range | Mercury Role | Default `--binary-kind` |
|---|---|---|---|
| Hopper | SM90, SM90a | Mercury format available but not default. SASS remains the standard output. MercExpand runs in the backend pipeline | `sass` |
| Blackwell | SM100, SM100a, SM100f | Mercury is default. Capsule Mercury (capmerc) is the standard ELF output format | `capmerc` |
| Blackwell Ultra / Future | SM103, SM120, SM121 | Mercury-only. No legacy SASS path | `capmerc` |

The `--binary-kind` CLI flag at `0x1D41D94` (xref `0x4ACC47`) selects the output format:

```text
--binary-kind <mercury|capmerc|sass>

Specify the type of target ELF binary kind.
Default on sm100+ is capmerc.
```

The three valid values are parsed from the string `"mercury,capmerc,sass"` at `0x1D41D03` (xref `0x4AC55C`). The option description at `0x1D41E78` confirms the sm100+ default: `"Specify the type of target ELF binary kind. Default on sm100+ is capmerc"`.

## ROT13 Obfuscation Scheme

NVIDIA applies ROT13 encoding to Mercury-related instruction mnemonics throughout the binary. This is a trivial Caesar cipher (A-M swap with N-Z) applied character-by-character, preserving underscores and digits. The encoding is consistent across all 667 `ZREPHEL_*` strings.

Key decodings:

| ROT13 (in binary) | Decoded (real name) | Instruction Category |
|---|---|---|
| `ZREPHEL` | `MERCURY` | ISA prefix |
| `ZREPHEL_zoneevre_neevir` | `MERCURY_mbarrier_arrive` | Barrier ops (124 strings) |
| `ZREPHEL_oneevre_neevir_flap` | `MERCURY_barrier_arrive_sync` | Barrier ops (86 strings) |
| `ZREPHEL_jnectebhc_zzn_flap_0` | `MERCURY_warpgroup_mma_sync_0` | Warpgroup MMA (40 strings) |
| `ZREPHEL_ngbz_nqq_f32` | `MERCURY_atom_add_s32` | Atomics (36 strings) |
| `ZREPHEL_erqhk_f32_flap` | `MERCURY_redux_s32_sync` | Reductions (32 strings) |
| `ZREPHEL_srapr_zoneevref` | `MERCURY_fence_mbarriers` | Fences (32 strings) |
| `ZREPHEL_gptra05_yq` | `MERCURY_tcgen05_ld` | SM100 tensor ops (4 strings) |

The 667 ROT13-encoded entries represent the Mercury-ISA-specific builtin instruction templates. These are instruction descriptors used by the ISel pattern matchers and the MercExpand engine. Each string encodes the full instruction signature: opcode, operand types (e.g., `fepf` = `srcs` for source operands, `he4` = `ur4` for uniform register 4-wide), and variant index.

Instruction category distribution across the 667 ZREPHEL builtins:

| Category (decoded) | Count | Description |
|---|---|---|
| `mbarrier` | 124 | Async barrier operations (arrive, wait, test, try_wait, pend) |
| `barrier` | 86 | Classic warp-level barrier synchronization |
| `warpgroup` | 40 | Warpgroup MMA operations (fp16, fp8, int, sparse variants) |
| `atom` | 36 | Atomic memory operations |
| `redux` | 32 | Warp-level reductions (s32, u32) |
| `fence` | 32 | Memory fence operations |
| `max`, `min`, `addmin`, `addmax` | 78 | Integer min/max combinators |
| `elect` | 20 | Warp-level leader election |
| `match` | 16 | Warp match operations |
| `vabsdiff4` | 14 | SIMD 4-byte absolute difference |
| `mov`, `selmov` | 16 | Data movement |
| `vote` | 12 | Warp-level voting |
| `tcgen05` | 4 | SM100 Blackwell tensor core gen05 |
| Others | 57 | `cvt`, `cvta`, `mapa`, `createpolicy`, `shfl`, `ld`, `st`, `cp`, `red`, `fma`, `sad`, `predict`, `multimem`, `griddepcontrol` |

## Mercury Pipeline in the Backend Compiler

Mercury processing occurs in the backend scheduling/encoding pipeline within the embedded ptxas. Four named Mercury passes are identified from the pass table at `0x2443C00`:

| Pass Name | String Address | Xref Address | Stage |
|---|---|---|---|
| `MercEncodeAndDecode` | `0x2443CA2` | `0x24443F0` | Encode IR to Mercury binary, then decode for verification |
| `MercExpandInstructions` | `0x2443CB6` | `0x24443F8` | Expand pseudo-instructions into Mercury machine operations |
| `MercGenerateWARs1` | `0x2443CCD` | `0x2444400` | Generate write-after-read hazard barriers (first pass) |
| `MercGenerateOpex` | `0x2443CDF` | `0x2444408` | Generate operand extensions for wide encodings |
| `MercGenerateWARs2` | `0x2443CF0` | `0x2444410` | Generate WAR barriers (second pass, post-opex) |
| `MercGenerateSassUCode` | `0x2443D02` | `0x2444418` | Generate final SASS microcode from Mercury representation |
| `PostFixForMercTargets` | `0x2443C44` | `0x24443C0` | Target-specific fixups for Mercury architectures |

Additional Mercury-prefixed pass markers confirmed from logging strings:

| Marker String | Address | Xref | Engine |
|---|---|---|---|
| `"After MercExpand"` | `0x1DFE320` | `0x5FF15E` | MercExpand dispatch at `sub_5FDDB0` |
| `"After MercConverter"` | `0x241F913` | `0x19798F8` | MercConverter in scheduling pipeline |
| `"After MercWARs"` | `0x1D41C60` | `0x4A480A` | WAR hazard barrier insertion |
| `"After MercOpex"` | `0x1D41C6F` | `0x4ABC3E` | Operand extension generation |

## The MercExpand Engine

MercExpand is the instruction expansion pass that lowers IR-level pseudo-instructions into Mercury machine operations. It occupies the address range `0x5E4470`--`0x600260` (~112 KB, ~40 functions) in the binary.

### Mercury IR Before Expansion

Before MercExpand runs, each function's IR is a doubly-linked list of instruction nodes threaded through basic blocks. Every IR node has the following layout (all offsets from the node pointer):

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 8 | `prev` | Previous node in linked list |
| +8 | 8 | `next` | Next node in linked list |
| +16 | — | `ir_body` | Start of IR body (handlers receive `node+16`) |
| +28 | 2 | `opcode_tag` | IR opcode type tag (the dispatch key) |
| +32 | 4 | `data_type_id` | Data type identifier (used for surface ops check: 559-560) |
| +36 | 4 | `sub_type` | Sub-type or variant index |
| +48 | 8 | `operand_list` | Pointer to operand array base |
| +56 | 4 | `num_operands` | Operand count (signed, checked `> 0` for surface ops) |
| +112 | 8 | `metadata_ptr` | Pointer to metadata block (contains descriptor index at +20) |
| +120 | 8 | `cfg_group_ptr` | Pointer to CFG group node (basic block membership) |
| +128 | 8 | `extended_info` | Extended instruction info pointer |
| +148 | 4 | `flags` | Instruction flag bitvector (bit 0 = terminator, bit 18 = barrier flag) |
| +149 | 1 | `sched_hint` | Scheduling hint byte (bit 7 = needs ordering constraint) |
| +152 | 4 | `bb_id` | Basic block identifier |

The opcode tag at +28 is a 16-bit signed integer. Pre-expansion opcodes are abstract: they represent generic operations (e.g., "add", "load", "branch") that do not yet encode Mercury-specific encoding constraints. The tag value -1 (`0xFFFF`) marks basic block terminators; tag 120 marks special metadata nodes (PHI inputs, call descriptors, inline constants).

Each instruction also carries an attribute bag accessible via `sub_A49150(context, ir_body, attr_id)` and `sub_A49120(context, ir_body, attr_id, value)`. Key attribute IDs observed in the expansion pass:

| Attribute ID | Description | Observed Values |
|---|---|---|
| 5 | Data type class | 12 = predicate type |
| 88 | Post-expansion flag | 408 = "was MercExpand'd" marker |
| 118 | Return semantics | 519 = return/exit opcode marker |
| 199 | Operand mode | 1104 = special operand form |
| 200 | Instruction class | 1107 = MOV pseudo requiring expansion |
| 202 | Memory scope | 1111 = specific scope tag |
| 227 | Target opcode | 1233 = MOV target opcode marker |
| 257 | Comparison mode | 1332 = specific comparison |
| 295 | Memory operation class | 1495/1496/1499/1500 = different memory categories |
| 297 | Address space | 1505/1506 = global/shared address space |
| 315 | Barrier type | 1774 = specific barrier form |
| 319 | Register constraint | 1791 = register alignment constraint |
| 345 | Expansion directive | 1901 = standard expansion |
| 348 | MercExpand state | 1912/1913/1914/1915 = expansion lifecycle states |
| 359 | Return mode | 1960 = return instruction marker |
| 527, 534, 538 | Opex flags | Operand extension indicators (case 90) |

The attribute-348 field is particularly important — it tracks where each instruction is in the MercExpand lifecycle:

| Value | Meaning |
|---|---|
| 1912 | Needs expansion (pre-expand marker) |
| 1913 | Currently expanding (in-progress) |
| 1914 | Expansion complete, needs fixup |
| 1915 | Fully expanded and finalized |

### Core Architecture

The engine operates on a per-basic-block basis, iterating the IR instruction linked list. For each node, it dispatches to specialized handlers based on the IR opcode type (field at node offset +28).

**Main dispatch function**: `sub_5FDDB0` (MercExpand_Dispatch, ~25.5 KB)

#### Dispatch Initialization

Before entering the main loop, the dispatch function performs three setup steps:

1. **Mercury capability check**: Reads the target descriptor at `context+312` and queries capability flags via the vtable at +72. If the target's byte at descriptor offset +216 equals 1, reads a flag from offset +224. Alternatively checks byte at offset +864. These determine whether Mercury expansion is needed at all (`v85` = skip flag).

2. **CFG group allocation**: Allocates two CFG group objects, each 64 bytes, via the arena allocator at `***context+16`. Each CFG group has a doubly-linked list structure:

```text
CFG Group (64 bytes):
  +0:   prev pointer (or NULL for head)
  +8:   sentinel (points to self+16)
  +16:  self-pointer (list anchor)
  +24:  NULL (reserved)
  +32:  tail pointer (points to self)
  +40:  back-pointer (points to self+16)
  +48:  state word = 2 (initial state: "open")
  +56:  ref-count object pointer
```

   The two groups serve as: `v81` = "definition group" (tracks where new instructions are inserted) and `v11`/`v82` = "resource group" (tracks register resource accounting boundaries).

3. **State initialization**: Sets `v90 = -1` (last processed BB ID), `v86 = 0` (predication tracking), `v83 = 0` (expansion-needed flag), `v84 = 0` (secondary flag).

#### The Main Iteration Loop

```c
MercExpand_Dispatch(pass_state):
    context       = pass_state->context          // at +24
    func_body     = context->func_body           // at +312
    bb_list       = context->basic_blocks        // at +24 (linked list)
    
    // 1. Check if Mercury expansion is needed
    target_desc = func_body->target_descriptor   // at [func_body+72]
    mercury_ver = target_desc[9]->byte_216
    if mercury_ver == 1:
        skip_flag  = target_desc[9]->dword_224
        has_mercury = call_vtable(func_body, cap_12) | sub_A48AA0(context)
    else:
        has_mercury = sub_A48AA0(context) | (target_desc[9]->byte_864 != 0)
    
    // 2. Allocate two CFG group trackers
    def_group = alloc_cfg_group(context)         // 64 bytes, state=2
    res_group = alloc_cfg_group(context)         // 64 bytes, state=2
    
    // 3. Initialize tracking state
    last_bb_id = -1
    pred_state = 0
    expand_needed = 0
    secondary_flag = 0
    
    // 4. Walk basic block linked list
    node = bb_list->first_child                  // context+24 -> offset+24
    while node != bb_list_sentinel:
        opcode_tag = node->word_28
        
        // --- Handle BB terminator (opcode_tag == -1) ---
        if opcode_tag == -1:
            if node->flags & 1:                  // bit 0 of +148
                prev_pred = context->byte_237 ^ 1
                update predication tracking
                pred_state = 1
                pass_state->dword_704 = node->dword_152
        
        // --- Handle special node (opcode_tag == 120) ---
        elif opcode_tag == 120:
            goto instruction_processing           // skip to per-instruction
        
        // --- Handle normal instructions ---
        else:
            // Assign node to current def_group (if expansion active)
            if !has_mercury && def_group->state != 2:
                node->cfg_group_ptr = def_group
                // Allocate fresh def_group
                def_group = alloc_cfg_group(context)
            
            // Call resource accounting on current res_group
            if res_group->state != 2:
                sub_5F8B60(pass_state, node+16, res_group, &last_bb_id)
                // Allocate fresh res_group
                res_group = alloc_cfg_group(context)
        
        // --- Per-instruction processing ---
        instruction_processing:
        
        // Query target capabilities (vtable+1048 and +792)
        if expand_needed:
            if call_vtable(target, cap_1048, node+16):
                sub_5F8980(pass_state, node)     // reset expansion
                expand_needed = 0
                secondary_flag = 0
        
        // Check vtable+792 for secondary capability
        if call_vtable(target, cap_792, node+16):
            secondary_flag = result
        
        // Record current/previous instruction pointers
        pass_state->ptr_8  = node
        pass_state->ptr_16 = node
        
        // Check scheduling constraints from target descriptor
        if context->dword_208 != context->dword_212
            && node->byte_149 < 0:
            target_info = context->ptr_312 -> ptr_72
            if target_info->byte_3384 == 1 && target_info->dword_3392:
                sub_5F9B10(pass_state, node)     // apply scheduling hint
            elif !target_info->byte_3456
                || (target_info->byte_3456 == 1 && target_info->dword_3464):
                return 1                         // early exit: constraint met
        
        // MOV pseudo-instruction special case
        if sub_A497D0(node+16)                   // check instruction kind
            && sub_A49150(context, node+16, 200) == 1107:
            sub_5FC6B0(pass_state, node)         // ExpandMOV
        
        // Opcode dispatch switch
        switch opcode_tag:
            // ... (see dispatch table below)
        
        // Post-dispatch: check for abort
        if pass_state->byte_886:
            return 1                             // abort expansion
        
        // Advance to next node
        node = next_node
```

#### The Opcode Dispatch Table

The switch statement at the heart of `sub_5FDDB0` dispatches on 30 distinct opcode tag values. Each case either calls a direct handler function or dispatches through a vtable. The vtable is at `*pass_state` (the first pointer in the pass state object).

```c
switch (opcode_tag):  // node->word_28
    case 0:     vtable[48/8=6]  (pass_state, node)     // Generic expansion
    case 5:     register_width_clamp(node, max=15)      // Clamp register width
    case 8:     register_width_clamp(node, max=15)      // (same as 5)
    case 9:     register_width_clamp(node, max=15)      // (same as 5)
    case 11:    complex_3way_dispatch(pass_state, node)  // See below
    case 12:    vtable[136/8=17] (pass_state, node)     // Conversion expansion
    case 17:    debug_node_handler(pass_state, node)     // Debug info (if +1536 set)
    case 19:    vtable[40/8=5]   (pass_state, node)     // Pre-header ops
    case 22:    vtable[144/8=18] (pass_state, node)     // Texture ops (pair with 23)
    case 23:    vtable[144/8=18] (pass_state, node)     // Texture ops (pair with 22)
    case 27:    memory_op_dispatch(pass_state, node)     // Memory load/store (complex)
    case 34:    comparison_handler(pass_state, node)     // Compare-and-branch
    case 35:    sub_5F7D20 + vtable[32/8=4]             // Predicated ops
    case 50:    vtable[112/8=14] (pass_state, node)     // Miscellaneous
    case 56:    atomic_handler(pass_state, node)         // Atomic read-modify-write
    case 57:    vtable[72/8=9]   (pass_state, node)     // Arithmetic
    case 59:    vtable[160/8=20] (pass_state, node)     // Returns next_node
    case 65:    vtable[160/8=20] (pass_state, node)     // (same as 59)
    case 66:    vtable[152/8=19] (pass_state, node)     // Returns next_node
    case 67:    vtable[152/8=19] (pass_state, node)     // (same as 66)
    case 69:    vtable[168/8=21] (pass_state, node)     // Special form
    case 71:    vtable[16/8=2]   (pass_state, node)     // Branch + WAR check
    case 73:    call_handler(pass_state, node)           // Function calls
    case 77:    predicate_handler(pass_state, node)      // Predicate definition
    case 78:    predicate_handler(pass_state, node)      // (same as 77)
    case 90:    opex_check(pass_state, node)             // Operand extension check
    case 94:    comparison_fixup(pass_state, node)       // Compare post-fixup
    case 95:    sub_5F8380(pass_state, node)             // Immediate handling
    case 97:    vtable[176/8=22] (pass_state, node)     // Post-pass ops
    case 98:    conditional_rewrite(pass_state, node)    // Conditional vtable+536
    case 99:    vtable[128/8=16] (pass_state, node)     // Shuffle/permute
    case 120:   special_node_handler(pass_state, node)   // Metadata / inline const
    case 130:   barrier_handler(pass_state, node)        // Barrier ops (WAR flag)
    case 205:   sub_5F7A80(pass_state, node)             // Named sub-handler
    case 210:   vtable[24/8=3]   (pass_state, node)     // Late-stage ops
    default:    (no action, fall through to post-dispatch)
```

##### Case 11: The Three-Way Dispatch

Case 11 is the most complex case. It checks three capabilities to select a handler:

```c
case 11:
    target = context->ptr_416
    if call_vtable(target, cap_584, node+16):
        sub_5F80E0(pass_state, node)          // FNV-1a hash lookup path
    elif call_vtable(target, cap_1160, node+16):
        sub_5FAC90(pass_state, node)          // Shared memory expansion
    elif node->num_operands > 0
         && node->operand_list->byte_32 == 6
         && (node->operand_list->dword_36 - 559) <= 1:
        sub_5FC1B0(pass_state, node)          // Surface read/write
    else:
        vtable[88/8=11](pass_state, node)     // Generic fallback
```

##### Case 27: Memory Operation Dispatch

Case 27 handles load/store instructions with the richest attribute-based dispatch:

```c
case 27:
    // Check expansion state attribute (348)
    if has_attr(348):
        state = get_attr(348)
        if state == 1915: skip (already done)
        if state == 1913: sub_5FA360 -- in-progress expansion
        if state == 1912: sub_5F9FD0 -- needs expansion
    
    // Clear attribute 348 and mark as expanded
    clear_attr(348)
    set_attr(348, 1915)
    invalidate(node)
    
    // Classify by address space (attribute 297)
    addr_space = get_attr(297)
    if addr_space == 1506 && num_operands == 6:
        sub_5FB5B0(pass_state, node)         // HandleGlobalMem
    elif addr_space == 1506 && num_operands == 5
         || addr_space == 1505:
        // Check register constraint (attr 319)
        if get_attr(319) == -1:
            // Inject alignment constraint
            operands->dword_132 += 1
            operands->dword_148 = 1
            set_attr(319, 1791)
            invalidate(node)
    elif addr_space == 1500:
        sub_5FBC30(pass_state, node)         // HandleConstMem
    elif (addr_space - 1495) & ~4 == 0
         && get_attr(202) == 1111
         && get_attr(359) == 1960:
        sub_5FCE20(pass_state, node)         // ExpandRETURN
    elif addr_space == 1496:
        flag = sub_5FA920(pass_state, node, secondary_flag)
        expand_needed |= flag
        secondary_flag &= flag
    else:
        vtable[80/8=10](pass_state, node)    // Generic memory handler
        sub_5F7A00(pass_state)               // Post-expansion NOP removal
```

##### Case 120: Special Node Handler

Case 120 handles metadata nodes, particularly inline constants and PHI inputs:

```c
case 120:
    metadata = node->metadata_ptr              // at +112
    kind = *metadata                           // first dword
    
    if kind == 3:
        // Call descriptor -- skip, handled by call expansion
        pass
    elif kind == 0:
        // Inline constant definition
        if !has_mercury:
            def_5F8C7E(pass_state, node+16, def_group)
        sub_A48B20(context, node, 0)
    elif kind == 2:
        // PHI input from another basic block
        if context->word_1544 > 0x100 && metadata[2] != 52:
            // Insert into resource group's linked list
            alloc list_node from res_group freelist
            list_node->data = metadata
            append to res_group's doubly-linked list
            res_group->count++
        sub_A48B20(context, node, 0)
    else:
        sub_A48B20(context, node, 0)          // generic cleanup
```

#### Post-Loop Finalization

After processing all instructions, the dispatch function performs final bookkeeping:

```c
    // After all nodes processed:
    func_info = context->ptr_1280              // at context[160]
    if func_info:
        if func_info->byte_92:
            func_info->dword_88 += pass_state->dword_880
        else:
            func_info->byte_92 = 1
            func_info->oword_76 = 0            // zero 16 bytes
        func_info->dword_88 = accumulated_value
    
    context->dword_1540 = 1                    // mark expansion complete
    return 0                                   // success
```

### The Per-Instruction Handler (`sub_5F38E0`)

`sub_5F38E0` is the 35 KB per-instruction expansion function. It is called from `sub_5FDDB0` indirectly for most non-trivial opcode types. While the dispatch function selects *which* handler runs, this function performs the *actual expansion* — converting one IR instruction into potentially multiple Mercury machine operations with proper register constraints and scheduling parameters.

#### Initialization

The function initializes a 144-byte local state structure on the stack:

```c
HandleInstruction(state, context, bb_list, is_predicated, pass_number):
    // Zero the local state
    state->ptr_24   = &input_operands
    state->dword_48 = 0
    memset(state+48, 0, 0x10)                // clear 16 bytes
    state->ptr_72   = NULL
    state->byte_40  = 1                      // "first instruction" flag
    state->ptr_88   = NULL                   // last_scheduling_dep
    state->ptr_96   = NULL                   // last_texture_dep
    state->ptr_104  = NULL                   // last_shared_dep
    state->ptr_112  = NULL                   // last_global_dep
    state->ptr_128  = NULL                   // last_barrier_dep
    state->ptr_136  = NULL                   // last_const_dep
    state->ptr_120  = NULL                   // last_fence_dep
    state->byte_41  = 0                      // not in reorder zone
    state->byte_42  = 0                      // no opex needed
    state->byte_43  = is_predicated ^ 1      // inverted predication flag
    
    // Invalidate register state for this basic block
    sub_5EA4F0(context, is_predicated)
```

#### The Descriptor Lookup

For each IR instruction, the handler looks up the corresponding **target instruction descriptor** — a 184-byte structure that defines the Mercury encoding constraints:

```c
    // Get descriptor index from instruction metadata
    desc_index = *(inst->metadata_ptr + 20)    // metadata[5]
    
    // Lookup in descriptor table (indexed or hash-map backed)
    if desc_index <= target_state->max_direct_index:   // at target+840
        descriptor = target_state->desc_table + 184 * desc_index  // at target+832
    else:
        descriptor = hash_lookup(target_state->desc_hash, desc_index)  // at target+848
```

The descriptor at pointer `v19` has this layout:

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 4 | `desc_id` | Descriptor unique ID |
| +4 | 4 | `sched_class` | Scheduling class (stored as `v193`) |
| +8-103 | — | `constraint_bitvectors` | Register constraint data (6 pairs at +8, +40, +104, +120) |
| +104 | 16 | `src_reg_constraints` | Source register constraint bitvectors |
| +120 | 16 | `dst_reg_constraints` | Destination register constraint bitvectors |
| +152 | 2 | `reg_class_0` | Register class constraint word 0 |
| +154 | 2 | `reg_class_1` | Register class constraint word 1 |
| +156 | 2 | `reg_class_2` | Register class constraint word 2 |
| +158 | 2 | `reg_class_3` | Register class constraint word 3 |
| +160 | 2 | `reg_class_4` | Register class constraint word 4 |
| +162 | 2 | `reg_class_5` | Register class constraint word 5 |
| +164 | 1 | `has_src_constraint` | Source operand constraint flag |
| +165 | 1 | `has_dst_constraint` | Destination operand constraint flag |
| +166 | 1 | `has_src_bitvec` | Source bitvector constraint present |
| +167 | 1 | `has_dst_bitvec` | Destination bitvector constraint present |
| +176 | 1 | `is_pseudo` | Pseudo-instruction flag |
| +177 | 1 | `is_eliminated` | Eliminated during expansion |

#### Register Constraint Propagation

After descriptor lookup, the handler applies register constraints in a strict order. The constraint system has four layers, each with source (`operand_index=0`) and destination (`operand_index=4`) variants, plus bidirectional variants (`operand_index=2,3`):

```c
    // Layer 1: Simple operand constraints (sub_5F1C50)
    if descriptor->has_src_constraint:         // byte at +164
        sub_5F1C50(context, operands, &offset, node+16, 0, &descriptor[+8])
        sub_5F1C50(context, operands, &offset, node+16, 4, &descriptor[+8])
    
    if descriptor->has_dst_constraint:         // byte at +165
        sub_5F1C50(context, operands, &offset, node+16, 3, &descriptor[+40])
        sub_5F1C50(context, operands, &offset, node+16, 2, &descriptor[+40])
    
    // Layer 2: Bitvector constraints (sub_5F0180)
    if descriptor->has_src_bitvec:             // byte at +166
        sub_5F0180(context, operands, &offset, node+16, 0, &descriptor[+104], 0, 0, 0, 0)
        sub_5F0180(context, operands, &offset, node+16, 4, &descriptor[+104], 0, 0, 0, 0)
    
    if descriptor->has_dst_bitvec:             // byte at +167
        sub_5F0180(context, operands, &offset, node+16, 3, &descriptor[+120], 0, 0, 0, 0)
        sub_5F0180(context, operands, &offset, node+16, 2, &descriptor[+120], 0, 0, 0, 0)
    
    // Layer 3: Register class constraints (6 class words at +152..+162)
    for i in 0..5:
        class_word = descriptor->reg_class[i]  // at +152 + 2*i
        if class_word != 0:
            sub_5F0180(context, operands, &offset, node+16,
                       src_or_dst[i], 0, class_word, 0, 0, 0)
            sub_5F0180(context, operands, &offset, node+16,
                       complement[i], 0, class_word, 0, 0, 0)
```

The `sub_5F0180` function (14.2 KB) is the core register constraint propagation engine. It takes 10 parameters and operates on bitvector scan data:

- Parameter `a5` selects the operand direction: 0=source, 2=dst-as-src, 3=src-as-dst, 4=destination
- Parameters `a6`-`a10` provide constraint data from different layers of the descriptor
- The function maintains 6 local bitvector buffers (48 bytes each at `v41`-`v46`) for intermediate results
- It uses the state structure at `a1+168`, `a1+232`, `a1+328` to track accumulated constraints across operands

#### Scheduling Distance Computation

After register constraints, the handler computes scheduling distances — the minimum number of cycles between dependent instructions:

```c
    // Query scheduling distance from target capabilities
    target_caps = sub_4FBCF0(target->desc_312, node+16, 0)
    if target_caps:
        // Check capability 95 (write-after-read distance)
        if has_cap(target_caps, 95):
            war_distance = get_cap_value(target_caps, 95)
        // Check capability 96 (read-after-write distance)
        elif has_cap(target_caps, 96):
            raw_distance = get_cap_value(target_caps, 96)
        // Check capability 97 (barrier-to-instruction distance)
        // Check capability 98 (instruction size for opex)
    
    // Compute stall cycles for dependent pair
    if previous_instruction:
        desc_prev = previous_instruction->metadata_ptr
        has_reorder = sub_5EF3C0(context, prev_inst, node+16)
        offset_prev = desc_prev->dword_0
        
        new_start = offset_prev + (has_reorder ? 2 : 1)
        if new_start < state->current_offset:
            new_start = state->current_offset
        state->current_offset = new_start
    
    // Apply result to instruction metadata
    inst_meta->dword_0  = state->current_offset  // cycle start
    inst_meta->dword_4  = state->max_offset       // cycle end bound
```

#### Dependency Chain Tracking

The function tracks seven categories of instruction dependencies for scheduling:

```c
    // Track dependencies by memory space / operation type
    state->ptr_88   -- last instruction using scheduling class (general dep)
    state->ptr_96   -- last texture/sampler operation
    state->ptr_104  -- last fence/sync operation
    state->ptr_112  -- last global memory barrier
    state->ptr_120  -- last fence instruction
    state->ptr_128  -- last barrier instruction
    state->ptr_136  -- reserved for cross-BB tracking
    
    // For each instruction, compute distance to its dependency:
    if descriptor->sched_class == 12:           // barrier class
        state->ptr_112 = current_node
    
    // Scheduling class queries via sub_502210 and sub_5021F0:
    if sub_502210(target_table, descriptor, state->ptr_152):
        // This instruction depends on last in same class
        dep = state->ptr_88
        if dep:
            dep_desc = dep->metadata_ptr
            distance = dep_desc->dword_0 + sub_4B13F0(target, dep, descriptor)
            if distance < state->dword_24:
                distance = state->dword_24
            state->dword_24 = distance
        state->ptr_88 = current_node
    
    // Similar checks for texture (sub_5021F0 flag 32/31),
    // fence (flag 6/7), and barrier distances
```

#### Target Opcode Lookup (`sub_5EA930`)

After scheduling, `sub_5EA930` (12.1 KB) performs the final target instruction descriptor lookup that maps the expanded instruction to the encoder tables. This function:

1. Reads the descriptor index from `instruction->metadata_ptr->dword_20`
2. Looks up the 184-byte descriptor (same table as above)
3. Queries the target capabilities database via `sub_4FBCF0` for encoder-specific information
4. Checks capabilities 93-98 for instruction size, stall penalties, and encoding constraints
5. Sets bit flags on the metadata block:
   - `byte_50 |= 0x10` when the instruction exceeds the minimum encoding size
   - `byte_50 |= 0x08` when capability 51 value equals 4
   - `byte_51 |= 0x04` when instruction must use extended encoding
   - `byte_51 |= 0x08` when capability 51 value equals 2
   - `byte_51 |= 0x10` when capability 51 value equals 3

These bit flags directly control the Mercury encoder: they select between compact (64-bit) and extended (128-bit) instruction encodings, and determine whether operand extension (opex) words are needed.

#### Instruction Emission Calls

After constraints and scheduling, two final calls emit the expanded instruction:

```c
    // 1. Branch expansion
    sub_5EEB20(state, node+16)                // HandleBranch -- resolves branch targets
    
    // 2. Register constraint finalization
    sub_5EBA30(state, context, node+16)       // Finalize register allocations
    
    // 3. Post-expansion constraint writeback
    // Write back constraint results to instruction metadata
    if descriptor->has_src_constraint:
        sub_5EDEA0(context, node+16, &descriptor[+8], 1)
    if descriptor->has_dst_constraint:
        sub_5EDEA0(context, node+16, &descriptor[+40], 0)
    if descriptor->has_src_bitvec:
        sub_5EDA80(context, node+16, &descriptor[+104], 0, 0, 1, 0)
    if descriptor->has_dst_bitvec:
        sub_5EDA80(context, node+16, &descriptor[+120], 0, 0, 0, 0)
    // Write back each non-zero register class word
    for i in 0..5:
        if descriptor->reg_class[i]:
            sub_5EDA80(context, node+16, 0, class_word[i], 0, src_flag[i], extra[i])
```

#### Epilogue: Last Instruction Fixup

After the main loop exits, the handler performs a final pass on the last instruction in the block:

```c
    // Compute final scheduling distance for the block
    last_meta = last_instruction->metadata_ptr
    max_offset = state->dword_32
    if state->dword_24 >= max_offset:
        max_offset = state->dword_24
    
    // Account for trailing barriers/fences
    if state->ptr_112:                         // had global barrier
        trailing = *(state->ptr_112->metadata_ptr) + 6
        if max_offset < trailing: max_offset = trailing
    if state->ptr_104:                         // had fence
        trailing = *(state->ptr_104->metadata_ptr) + 6
        if max_offset < trailing: max_offset = trailing
    if state->ptr_88:                          // had scheduling dep
        trailing = sub_4B13F0(...) computation
        if max_offset < trailing: max_offset = trailing
    
    // Check capability 97 for minimum block distance
    target_caps = sub_4FBCF0(target->desc_312, last_instruction, 0)
    if target_caps && has_cap(target_caps, 97):
        cap_97_distance = get_cap_value(target_caps, 97)
        if max_offset < cap_97_distance: max_offset = cap_97_distance
    
    // Compute final stall count
    stall_count = max_offset - last_meta->dword_0
    if stall_count < last_meta->dword_12:
        stall_count = last_meta->dword_12
    
    // Check capability 93 for additional penalty
    if has_cap(target_caps, 93):
        stall_count += get_cap_value(target_caps, 93)
    
    // Minimum stall: 1, or 2 if instruction has opex/extension flags
    min_stall = 1 + ((last_meta->byte_48 & 0x11) != 0)
    if stall_count < min_stall:
        stall_count = min_stall
    
    // Final target lookup
    if sub_5EA930(state, last_instruction, stall_count, state->dword_36):
        last_meta->byte_50 |= 0x10            // needs extended encoding
    
    last_meta->dword_56 = stall_count
    
    // Write total cycle count to basic block terminator
    bb_terminator->metadata_ptr->dword_4 = stall_count + last_meta->dword_4
    
    // Apply target constraints for final pass
    if !pass_number:
        sub_5EB130(state, context, last_instruction, 1)
```

### The FNV-1a Hash Lookup (`sub_5F80E0`)

The instruction dispatch for case 11 / vtable+584 uses an FNV-1a hash map to find pre-computed expansion templates:

```c
sub_5F80E0(pass_state, node):
    context = pass_state[3]                    // ptr at index 3
    
    if context->byte_512:                      // hash map enabled
        assert context->dword_480 != 0         // hash table size > 0
        
        // FNV-1a hash of the data type ID
        data_type = node->dword_32             // 4-byte value
        hash = 0x811C9DC5                      // FNV offset basis
        hash = (hash ^ byte_0(data_type)) * 16777619   // FNV prime
        hash = (hash ^ byte_1(data_type)) * 16777619
        hash = (hash ^ byte_2(data_type)) * 16777619
        hash = (hash ^ byte_3(data_type)) * 16777619
        
        // Probe the hash table
        bucket = context->ptr_488 + 24 * (hash & (context->ptr_496 - 1))
        while bucket != NULL:
            bucket = *bucket                   // follow chain
            if bucket && bucket->dword_8 == data_type:
                break                          // found
        
        expansion_template = bucket->ptr_16
    else:
        expansion_template = NULL
    
    // Store IR body for expansion
    context->ptr_992 = node->ptr_16
    
    // Create expanded instruction via sub_A4CA70
    new_node = sub_A4CA70(context, node, pass_state+2, expansion_template)
    pass_state[1] = new_node
    
    // Call target-specific fixup (vtable+56 on target at context+416)
    call_vtable(target, fixup_56, node+16, new_node+16)
    
    // Transfer CFG group ownership
    new_node->ptr_120 = node->ptr_120
    node->ptr_120 = NULL
    
    // Invalidate and clean up original node
    sub_A49DF0(context, new_node+16, 0)
    sub_5F70E0(pass_state, node+16)
    sub_A48B20(context, node, new_node+16)
```

### Post-Expansion NOP Removal (`sub_5F7A00`)

After certain expansions, the engine runs a cleanup pass that removes unnecessary NOP-like instructions:

```c
sub_5F7A00(pass_state):
    target_info = *(context->ptr_312 + 72)
    if target_info->byte_1008 != 1:
        return
    if target_info->dword_1016 == 0:
        return
    
    // Scan from first_inserted to last_inserted
    node = pass_state->ptr_1                   // first inserted instruction
    sentinel = pass_state->ptr_2               // last original instruction
    
    while node != sentinel:
        opcode = node->word_28
        next = node->next
        
        // Remove opcode 162 (NOP) and opcode 349 (expansion placeholder)
        if opcode == 162 || opcode == 349:
            sub_A48D20(context, node, 0)       // delete node from list
        
        node = next
```

### Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `0x5FDDB0` | `MercExpand_Dispatch` | 25.5 KB | Main entry point, instruction dispatch loop |
| `0x5F38E0` | `MercExpand_HandleInstruction` | 35.0 KB | Per-instruction expansion, 2nd largest function in engine |
| `0x5F0180` | `MercExpand_PropagateRegConstraints` | 14.2 KB | Register constraint propagation (bitvector scanning, 10 params) |
| `0x5F1C50` | `MercExpand_ApplySimpleConstraints` | ~8 KB | Simple operand constraint application |
| `0x5F8B60` | `MercExpand_ApplyResourceConstraints` | 16.0 KB | Register resource accounting (52 register types) |
| `0x5EA930` | `MercExpand_LookupTargetOpInfo` | 12.1 KB | Target instruction descriptor lookup + encoder flag computation |
| `0x5EB130` | `MercExpand_ApplyRegConstraintsFromTarget` | 11.0 KB | Target-specific register constraints (capabilities 40-47) |
| `0x5EA4F0` | `MercExpand_InvalidateRegisterState` | 4.3 KB | Register cache invalidation (13 register slots, 15+ generation counters) |
| `0x5F60E0` | `IRTree_Walk` | 18.6 KB | Recursive tree walker (manually unrolled to 5 nesting levels) |
| `0x5F80E0` | `MercExpand_HashDispatch` | 2.8 KB | FNV-1a hash lookup for expansion templates |
| `0x5F7A00` | `MercExpand_PostCleanup` | 1.1 KB | Remove NOP/placeholder instructions (opcodes 162, 349) |
| `0x5F9C70` | `MercExpand_ReleaseCFGGroup` | 0.9 KB | Reference-counted CFG group deallocation |
| `0x5F9B10` | `MercExpand_ApplySchedHint` | ~2 KB | Apply scheduling hint from target descriptor (+3384/+3456) |
| `0x5FCE20` | `MercExpand_ExpandRETURN` | ~8 KB | Return/exit expansion |
| `0x5FC6B0` | `MercExpand_ExpandMOV` | ~4 KB | MOV pseudo-instruction expansion |
| `0x5EEB20` | `MercExpand_HandleBranch` | 7.7 KB | Branch target resolution |
| `0x5EBA30` | `MercExpand_FinalizeRegAlloc` | ~6 KB | Register constraint finalization |
| `0x5EF3C0` | `MercExpand_CheckReorderability` | ~2 KB | Check if two instructions can be reordered |
| `0x5F2BA0` | `MercExpand_InsertStallCycles` | ~4 KB | Insert explicit stall cycles between instructions |

### Register State Cache and Invalidation

`sub_5EA4F0` (MercExpand_InvalidateRegisterState, 4.3 KB) manages a generation-counter-based register cache. The cache tracks 13 physical register file partitions, each with a value slot and a generation counter:

```c
sub_5EA4F0(state, is_predicated):
    // Bump 15 generation counters (different register file partitions)
    state->gen_64++                            // general purpose registers (even)
    state->gen_96++                            // general purpose registers (odd)
    state->gen_128++                           // predicate registers
    state->gen_160++                           // special registers group A
    state->gen_192++                           // special registers group B
    state->gen_224++                           // uniform registers even
    state->gen_256++                           // uniform registers odd
    state->gen_288++                           // condition code registers
    state->gen_320++                           // barrier registers
    state->gen_352++                           // address registers
    state->gen_384++                           // texture sampler registers
    state->gen_416++                           // global version counter
    state->gen_448++                           // reserved / opex
    
    // Zero the 15 corresponding dirty flags
    state->dirty_68  = 0
    state->dirty_100 = 0
    state->dirty_132 = 0
    ... (all 15 cleared)
    
    // Invalidate the 13-slot register value cache
    // Cache is at state->ptr_400, each entry is 8 bytes (value + generation)
    cache = state->ptr_400
    gen   = ++state->dword_416                 // global generation
    
    // For each of the 13 cache slots:
    for slot in [0, 1, 4, 5, 10, 11, 12, 13, 18, 19, 22, 23, ...]:
        if cache[slot].generation != gen:
            state->dirty_420++                 // count invalidations
        cache[slot].value = -1                 // mark invalid
        cache[slot].generation = gen
    
    // Apply target-specific constraints (capabilities 39-40)
    target = context->ptr_312
    if has_cap(target, 40):
        // Walk linked list at target+2896 (sentinel at +2904)
        for entry in linked_list(target+2896):
            slot_idx = entry->dword_16
            new_val  = entry->dword_20
            cache_entry = &cache[slot_idx]
            if cache_entry.generation != gen:
                dirty_420++
            cache_entry.value = new_val
            cache_entry.generation = gen
    
    if has_cap(target, 39):
        // Walk linked list at target+2824 (sentinel at +2832)
        for entry in linked_list(target+2824):
            sub_5EA3A0(state, entry->dword_16, entry->dword_20)
    
    state->byte_34 = is_predicated
```

### Instruction Handlers

The dispatch loop delegates to specialized handlers per instruction category:

| Function | Handler | Notes |
|---|---|---|
| `0x5EC540` | HandlePredication | Predicate register setup |
| `0x5EC940` | HandleBarrier | Barrier synchronization |
| `0x5ECC60` | HandleSync | Warp synchronization |
| `0x5ED060` | HandleDepInfo | Dependency information |
| `0x5ED3A0` | HandleTexSampler | Texture/sampler operations |
| `0x5ED850` | HandleAtomicOp | Atomic memory operations |
| `0x5EDA80` | HandleMemOp | Memory load/store |
| `0x5EE030` | HandleConversion | Type conversion |
| `0x5EE750` | HandleCmp | Comparison operations |
| `0x5EE930` | HandleSelect | Select/conditional move |
| `0x5EEB20` | HandleBranch | Branch expansion (7.7 KB) |
| `0x5EF0E0` | HandleCall | Function call expansion |
| `0x5EF3C0` | CheckReorderability | Instruction reorder check |
| `0x5EF4D0` | HandleReturn | Return/exit expansion |
| `0x5EF760` | HandlePhi | PHI node expansion |
| `0x5FAC90` | HandleSharedMem | Shared memory access (9.6 KB) |
| `0x5FB5B0` | HandleGlobalMem | Global memory access |
| `0x5FBC30` | HandleConstMem | Constant memory access |
| `0x5FC1B0` | HandleSurfaceOp | Surface read/write |
| `0x5FC6B0` | ExpandMOV | MOV instruction (attribute 200==1107 special case) |
| `0x5FCE20` | ExpandRETURN | Return/exit (creates opcode 270, sets attribute 118=519) |

### How Expanded Instructions Feed Into Encoder Tables

The MercExpand output feeds directly into the downstream `MercEncodeAndDecode` pass. The connection points are:

1. **Descriptor index** (metadata +20): Each expanded instruction carries a descriptor index that maps 1:1 to an entry in the Mercury encoder table. The encoder table base is at `target_state+832`, with each entry being 184 bytes. This is the same table consulted during expansion — the encoder reads the same descriptor to determine the binary encoding format.

2. **Encoding size flags** (metadata byte +50 and +51): Set by `sub_5EA930` during expansion:
   - Bit 4 of byte +50 (`0x10`): instruction exceeds minimum encoding width, needs extended format
   - Bit 3 of byte +50 (`0x08`): capability-51 value 4 flag
   - Bit 2 of byte +51 (`0x04`): requires opex (operand extension) words
   - Bit 3 of byte +51 (`0x08`): capability-51 value 2 flag
   - Bit 4 of byte +51 (`0x10`): capability-51 value 3 flag

3. **Stall count** (metadata +56): Written by the per-instruction handler, this integer encodes the minimum stall cycles before the next instruction. The Mercury encoder embeds this in the instruction's yield/stall field.

4. **Scheduling offset** (metadata +0 and +4): The `dword_0` field gives the cycle at which this instruction can issue; `dword_4` gives the upper bound. The encoder uses these to compute the final instruction ordering in the `.nv.merc` section.

5. **Register class results** (metadata +36): The encoder reads the final register constraint resolution written back by `sub_5EDEA0` / `sub_5EDA80`. These determine which register encoding fields are used in the binary word.

6. **CFG group membership** (node +120): The CFG group pointer threads instructions into basic blocks. The encoder traverses these groups to produce per-block instruction sequences with proper entry/exit markers.

### Internal Data Structures

**Target instruction descriptor** (184 bytes per entry, base at state offset +832):
- Offset 0: descriptor index
- Offset 4: scheduling class
- Offsets 8-39: source operand simple constraints (4 pairs of 8-byte bitvectors)
- Offsets 40-103: destination operand simple constraints (4 pairs of 8-byte bitvectors)
- Offsets 104-119: source register bitvector constraints (16 bytes)
- Offsets 120-135: destination register bitvector constraints (16 bytes)
- Offsets 152-163: 6 register class constraint words (2 bytes each)
- Offsets 164-167: 4 constraint flag bytes (has_src_constraint, has_dst_constraint, has_src_bitvec, has_dst_bitvec)
- Offsets 176-177: pseudo/eliminated flags
- Offsets 2880-2904: register constraint linked lists (from target capabilities 39-40)
- Offsets 3384-3456: scheduling hints
- Offset 3672: capability flag 51

**Register state cache** (at MercExpand state offset +400):
- 13 register slots mapping to physical register file partitions
- Each slot: 8 bytes (4-byte value + 4-byte generation counter)
- Generation counters for cache invalidation (15 separate counters at offsets +64 through +448)
- Dirty counter at offset +420 tracks total invalidation events
- Slots cover: general purpose (R0-R255), predicates (P0-P6), uniform registers (UR0-UR63), condition codes (CC), barrier registers, address registers, texture sampler state, and reserved/opex slots

**CFG group node** (64 bytes, reference-counted):
- +0: prev pointer (doubly-linked list)
- +8: sentinel pointer (self+16)
- +16: self-pointer (list anchor)
- +32: tail pointer
- +48: state word (2=open/active, other values=closed/finalized)
- +56: ref-count object pointer (shared_ptr-like, released via `sub_5F9C70`)

**FNV-1a hash maps**: Used for IR node tracking and lookup tables throughout MercExpand. The hash map at state offset +480 uses FNV-1a (prime=16777619, basis=0x811C9DC5) with chained buckets of 24 bytes each. Node identification uses hash maps keyed on node metadata at offset +112 -> +20.

## Capsule Mercury (capmerc) Format

Capsule Mercury is the new ELF binary format for SM100+ targets. It wraps Mercury-encoded instructions in a specialized ELF layout with `.nv.merc.*` sections. The `capmerc.cubin` filename extension is used (string at `0x1D33FA9`, xrefs from `0x40A84F` and `0x42A26F`).

### ELF Sections

The 20 `.nv.merc.*` section names identified in the binary:

| Section Name | Description |
|---|---|
| `.nv.merc` | Main Mercury instruction section |
| `.nv.merc.rela` | Mercury relocation entries |
| `.nv.merc.symtab_shndx` | Extended section header index (for >65535 sections) |
| `.nv.merc.nv.shared.reserved.` | Reserved shared memory region |
| `.nv.merc.debug_abbrev` | DWARF abbreviation tables |
| `.nv.merc.debug_aranges` | DWARF address ranges |
| `.nv.merc.debug_frame` | DWARF call frame information |
| `.nv.merc.debug_info` | DWARF debug information entries |
| `.nv.merc.debug_line` | DWARF line number program |
| `.nv.merc.debug_loc` | DWARF location lists |
| `.nv.merc.debug_macinfo` | DWARF macro information |
| `.nv.merc.debug_pubnames` | DWARF public names |
| `.nv.merc.debug_pubtypes` | DWARF public types |
| `.nv.merc.debug_ranges` | DWARF address ranges |
| `.nv.merc.debug_str` | DWARF string table |
| `.nv.merc.nv_debug_ptx_txt` | Embedded PTX source text |
| `.nv.merc.nv_debug_line_sass` | NVIDIA SASS-level line tables |
| `.nv.merc.nv_debug_info_reg_sass` | NVIDIA SASS register debug info |
| `.nv.merc.nv_debug_info_reg_type` | NVIDIA register type debug info |

### Self-Check Mechanism

nvlink includes a self-check facility for capsule mercury output, enabled via the `--self-check` CLI flag (string at `0x1D41D3A`). The self-check description at `0x1D41EC8`: `"Self check for capsule mercury (capmerc)"`.

Self-check validates three sections independently:

| Check | Error String | Address |
|---|---|---|
| Text section | `"Self check for capsule mercury text section failed"` | `0x2458F38` |
| Debug section | `"Self check for capsule mercury debug section failed"` | `0x2458F70` |
| Relocation section | `"Self check for capsule mercury relocation section failed"` | `0x2458FA8` |

On failure, the error at `0x1F44288` references internal documentation: `"Failure of '%s' section in self-check for capsule mercury. See the Jira confluence page 'MERCSW-125' for more information that includes some debugging steps."` The `MERCSW` Jira project is NVIDIA's internal Mercury software tracker.

An additional option produces reconstituted SASS for debugging: `"Generate output of capmerc based reconstituted sass only through -self-check"` (string at `0x1D41EF8`).

## Mercury Uplift

The "mercury uplift" path converts legacy SASS ELF binaries into Mercury format. The error string at `0x2458FE8` (`"Invalid elf provided for mercury uplift."`, xref `0x24590B8`) confirms this conversion direction. A related skip path at `0x1D3BCB7` (`"skip mercury section %i"`, xref `0x45F624`) handles sections that should not be uplifted.

The uplift path coexists with the `"don't uplift %s"` diagnostic at `0x1D3410E` (xref `0x42BBDC`), indicating per-symbol or per-section uplift control.

## ELF Attributes for Mercury

Two EIATTR (ELF Info Attribute) types are Mercury-specific:

| Attribute | String Address |
|---|---|
| `EIATTR_MERCURY_ISA_VERSION` | `0x1D36F31` |
| `EIATTR_MERCURY_FINALIZER_OPTIONS` | `0x1D37170` |

Four EICOMPAT (ELF Info Compatibility) attributes relate to Mercury and finalization:

| Attribute | String Address | Description |
|---|---|---|
| `EICOMPAT_ATTR_MERCURY_ISA_MAJOR_MINOR_VERSION` | `0x245EF08` | Mercury ISA version (major.minor) |
| `EICOMPAT_ATTR_MERCURY_ISA_PATCH_VERSION` | `0x245EF38` | Mercury ISA patch version |
| `EICOMPAT_ATTR_ENABLE_OPPORTUNISTIC_FINALIZATION` | `0x245EED8` | Controls cross-family finalization |
| `EICOMPAT_ATTR_CAN_FASTPATH_FINALIZE` | `0x245EF88` | Fast-path finalization eligibility |

## Relationship to FNLZR (Finalizer)

The FNLZR (Finalizer) subsystem is the runtime component that converts between Mercury and SASS representations. It operates in two modes, logged via diagnostic strings:

| Mode | String | Address | Xref |
|---|---|---|---|
| Pre-Link | `"FNLZR: Pre-Link Mode"` | `0x1D323BD` | `0x427999` |
| Post-Link | `"FNLZR: Post-Link Mode"` | `0x1D32397` | `0x427951` |
| JIT | `"FNLZR: JIT Path"` | `0x1DF8C40` | `0x52DDE1` |

FNLZR logs its input: `"FNLZR: Input ELF: %s"` (`0x1D32381`), and tracks lifecycle: `"FNLZR: Starting %s"` / `"FNLZR: Ending %s"` / `"FNLZR: Flags [ %u | %u ]"`.

Finalization also appears in the capsule mercury code region with thread-level parallelism: `"Failed to create finalizer thread"` (`0x2458EC0`), suggesting that the finalizer runs as a separate thread during link.

The `--opportunistic-finalization-lvl` flag (string at `0x1D41F70`) controls cross-architecture finalization behavior:

```text
--opportunistic-finalization-lvl <0|1|2|3>

0 = default behavior
1 = no opportunistic finalization
2 = intra family finalization only
3 = intra and inter family finalization
```

Fast-path finalization is confirmed by the diagnostic at `0x1D40610`: `"[Finalizer] fastpath optimization applied for off-target %u -> %u finalization"`, indicating cross-SM finalization (e.g., compiling SM90 code for an SM100 target).

See [FNLZR (Finalizer)](fnlzr.md) for detailed analysis of the finalization subsystem.

## MercGenerateSassUCode

The final Mercury pipeline stage is `MercGenerateSassUCode` (`0x2443D02`, xref `0x2444418`), which converts the Mercury internal representation into SASS microcode — the actual GPU-executable instruction encoding. Related dump utilities exist:

| Function | String Address | Description |
|---|---|---|
| `DumpNVuCodeText` | `0x2443DA2` | Dump microcode in text format |
| `DumpNVuCodeHex` | `0x2443DB2` | Dump microcode in hex format |

The `.ucode` section name at `0x1EEC922` and `EIATTR_UCODE_SECTION_DATA` at `0x1D36D20` confirm that microcode is a distinct section in the output ELF.

## Cross-References

### nvlink Internal
- [Capsule Mercury Format](capmerc-format.md) — detailed capmerc ELF layout and encoding
- [R_MERCURY Relocations](r-mercury-relocations.md) — the 67 Mercury relocation types
- [Mercury ELF Sections](elf-sections.md) — the 20 `.nv.merc.*` sections
- [Mercury Compiler Passes](compiler-passes.md) — MercExpand, MercConverter, MercWARs, MercOpex
- [FNLZR (Finalizer)](fnlzr.md) — SASS-to-Mercury and Mercury-to-SASS conversion
- [Embedded ptxas Overview](../ptxas/overview.md) — MercExpand mega-hub at `0x5B1D80` in the address map
- [ISel Hubs](../ptxas/isel-hubs.md) — MercExpand is the 5th mega-hub dispatch function
- [SM100 Blackwell](../targets/sm100-blackwell.md) — Mercury is the default encoding for SM100+ targets
- [Output Phase](../pipeline/output.md) — Mercury output path in the linker pipeline

### Sibling Wikis
- [ptxas: Mercury Encoder Pipeline](../../ptxas/codegen/mercury.html) — standalone ptxas Mercury encoder (phases 113–122: encode/decode, MercExpand, WAR, opex, UCode emission)
- [ptxas: Capsule Mercury & Finalization](../../ptxas/codegen/capmerc.html) — standalone ptxas capmerc output format, Mercury section binary layouts, finalization pipeline
- [ptxas: SASS Encoding](../../ptxas/codegen/encoding.html) — SASS instruction encoding that Mercury wraps

## Confidence Assessment

| Claim | Rating | Evidence |
|---|---|---|
| Mercury = ROT13("Zrephel") obfuscation scheme | **HIGH** | 667 ZREPHEL strings verified in `nvlink_strings.json` (addr range `0x1D42C80`--`0x1D4DF80`). ROT13 decode confirmed character-by-character. |
| 667 ZREPHEL builtin instruction entries | **HIGH** | Exact count from `nvlink_strings.json` string scan. All entries begin with `ZREPHEL_` prefix. |
| ROT13 decoder at `sub_1A40AC0` (15,629 bytes, SIMD) | **MEDIUM** | Function exists at stated address in decompiled code. SIMD `_mm_load_si128` usage inferred from decompiler output. Byte count from function bounds. |
| `--binary-kind` flag parsed from `"mercury,capmerc,sass"` at `0x1D41D03` | **HIGH** | String verified at exact address in `nvlink_strings.json` with xref to `0x4AC55C`. |
| SM100+ default is capmerc (activation via `byte_2A5F222`) | **HIGH** | String `"Default on sm100+ is capmerc"` verified. Global `byte_2A5F222` confirmed in decompiled `sub_4AC380`. |
| Architecture compatibility: 104->120, 130->107, 101->110 remapping | **HIGH** | Verified from decompiled `sub_4709E0` and `sub_470DA0`. |
| Capability bitmask: sm100=1, sm103=8, sm110=2, sm121=64 | **HIGH** | Decompiled from `sub_470DA0`, verified from switch on architecture codes 'd','g','n','y'. |
| MercExpand engine spans `0x5E4470`--`0x600260` (~112 KB) | **MEDIUM** | Address range from sweep analysis. Entry `sub_5FF110` and dispatch `sub_5FDDB0` verified from decompiled code. Sub-function count (~40) is approximate. |
| 30 opcode dispatch cases in `sub_5FDDB0` | **HIGH** | Switch statement cases verified from decompiled `sub_5FDDB0` (25.5KB). |
| IR node layout (offsets +0/+8/+16/+28/+32/+48/+56/+112/+120/+148/+149/+152) | **MEDIUM** | Offsets derived from decompiled code field access patterns. Consistent across multiple functions. Field names are inferred from usage context. |
| 184-byte target instruction descriptor format | **MEDIUM** | Size inferred from `184 * desc_index` multiplication in decompiled code. Field offsets verified from constraint propagation code. |
| FNV-1a hash constants (basis=`0x811C9DC5`, prime=16777619) | **HIGH** | Standard FNV-1a constants, verified from decompiled `sub_5F80E0`. |
| Register state cache: 13 slots, 15 generation counters | **MEDIUM** | Slot count and counter count from decompiled `sub_5EA4F0`. Specific slot-to-register-file mapping is inferred. |
| String evidence: 82 `mercury`/`Mercury`, 7 `capmerc`, 17 `FNLZR`, 20 `.nv.merc.*` | **HIGH** | Counts verified by scanning `nvlink_strings.json`. Address ranges confirmed. |
| FNLZR diagnostic strings (`"FNLZR: Input ELF: %s"`, etc.) | **HIGH** | All 12 FNLZR strings verified at exact addresses with xrefs to `sub_4275C0`. |
| Self-check error strings and MERCSW-125 Jira reference | **HIGH** | 4 self-check strings verified. `MERCSW-125` string confirmed at `0x1F44288`. |
| `capmerc.cubin` output filename at `0x1D33FA9` | **HIGH** | Verified in `nvlink_strings.json` with xrefs from `0x40A84F` and `0x42A26F`. |
| Fastpath optimization log string at `0x1D40610` | **HIGH** | Exact string verified in `nvlink_strings.json`. |
| Mercury uplift error at `0x2458FE8` | **HIGH** | `"Invalid elf provided for mercury uplift."` verified at exact address. |
| `MercGenerateSassUCode` at `0x2443D02` (pipeline stage) | **HIGH** | String verified. Xref at `0x2444418` confirmed (master phase table entry). |
| Instruction category distribution (mbarrier=124, barrier=86, etc.) | **MEDIUM** | Counts from categorizing 667 ZREPHEL strings by decoded prefix. Manual categorization may have minor counting errors. |
| Hopper (SM90) uses Mercury but not as default | **MEDIUM** | Inferred from global flag thresholds (sm > 99 vs sm > 89). Not directly confirmed from string evidence. |
| `"don't uplift %s"` diagnostic at `0x1D3410E` | **HIGH** | Verified in `nvlink_strings.json` with xref at `0x42BBDC`. |
| EIATTR/EICOMPAT Mercury attributes at stated addresses | **HIGH** | All 6 attribute strings verified at exact addresses in `nvlink_strings.json`. |
