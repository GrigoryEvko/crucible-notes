# Data Structure Layouts

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

This page documents the key internal data structures in ptxas v13.0.88: the compilation context ("god object"), the Ori Code Object, symbol tables, constant/shared memory descriptors, the pool allocator's object model, and the generic container types (hash maps, linked lists, growable arrays) that underpin nearly every subsystem.

All offsets are byte offsets from the structure base unless otherwise noted. Types are inferred from decompiled access patterns. Field names are reverse-engineered -- the binary is stripped.

## Compilation Context (the "God Object")

The compilation context is the central state object passed to every phase in the pipeline. It is not the Code Object (which is per-function); it is the per-compilation-unit container that owns the Code Object, the knob system, the output stream, the function list, and all per-pass configuration. The `sub_7FBB70` (PerKernelEntry) function receives this as `a1`, and every phase's `execute()` receives it as the second argument.

The context is a polymorphic C++ object with a vtable at offset +0. It is allocated by the compilation driver and persists for the lifetime of a single compilation unit. Key observations:

- The vtable at `+0` provides 263+ virtual methods (vtable spans to offset 2104+)
- The object is at least 1928 bytes based on the highest confirmed field access (`+1928 = codegen_ctx`)
- The knob/options system is accessed through an indirection at `+1664` (pointer to knob container object)
- The output stream lives at `+1440`

### Compilation Context Field Map

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +0 | `vtable*` | vtable | `*(_QWORD *)a1` in every virtual dispatch |
| +8 | `ptr` | `parent` / `driver_ctx` | Back-pointer; `sub_A3A7E0` reads `v2 = *(a1+8)` then `v2[198]` for Code Object |
| +80 | `u32` | `last_exit_code` | `sub_663C30`: `*(a1+80) = v2[462]` |
| +96 | `u32` | `compile_unit_index` | `sub_663C30`: `*(a1+96) = 1` on first call |
| +139 | `u8` | `multi_function_flag` | `sub_663C30`: `if (!*(a1+139))` |
| +144 | `ptr` | `name_table` (via vtable+144) | `sub_7FBB70`: `*(*a1 + 144)` -> name lookup vtable |
| +296 | `ptr` | `current_function` | `sub_7FBB70`: `*(*(a1+296) + 164)` = function index |
| +368 | `ptr` | `function_name_array` | `sub_7FBB70`: `*(a1+368 + 8*func_id)` -> name object |
| +1144 | `ptr` | `function_list_head` | `sub_663C30`: linked list of function descriptors |
| +1160 | `ptr` | `entry_list_head` | `sub_663C30`: linked list of kernel entry descriptors |
| +1376 | `u32` | `scheduling_mode_flags` | Bit 0x08 = forward, bit 0x10 = bidirectional |
| +1412 | `i8` | `compilation_flags_byte` | `sub_A3B080`: `*(char*)(a2+1412) < 0` |
| +1416 | `u8` | `output_detail_flags` | `sub_7FBB70`: `*(a1+1416) \|= 0x80`; bits 4-5 control latency reporting mode |
| +1418 | `u8` | `codegen_mode_flags` | `sub_A3B080`: `*(a2+1418) & 4` |
| +1428 | `i32` | `function_index` | `sub_7FBB70`: `*(a1+1428) < 0` means first invocation |
| +1440 | `stream*` | `output_stream` | `sub_7FBB70`: `sub_7FE930(a1+1440, "\nFunction name: ")` |
| +1560 | `ptr` | `timing_records` | Growable array of 32-byte timing entries |
| +1576 | `u32` | `timing_count` | `sub_C62720`: `cu->timing_count++` |
| +1552 | `i32` | `pipeline_progress` | Pipeline progress counter (0--21), monotonically increases; see [known values](#pipeline-progress-counter) |
| +1584 | `ptr` | `sm_backend` | SM-specific architecture backend object (polymorphic, 1712--1992B depending on SM target); provides vtable dispatch for legalization, optimization, scheduling, and codegen; see note below |
| +1664 | `ptr` | `knob_container` | `sub_7FB6C0`, `sub_A3B080`: options/knob dispatch object |
| +1864 | `ptr` | `bb_structure` | `sub_7FB6C0`: destroyed via `sub_77F880` |
| +1872 | `ptr` | `per_func_data` | `sub_7FB6C0`: destroyed via `sub_7937D0` |
| +1880 | `ptr` | `function_context` | `sub_7FB6C0`: 17 analysis-result pairs at qword offsets |
| +1928 | `ptr` | `codegen_ctx` | Confirmed in overview.md Code Object table |

### SM Backend Object at +1584

The pointer at `context+0x630` (decimal 1584) is the single most confusing field in the compilation context, because it serves multiple roles through a single polymorphic C++ object. Different wiki pages historically called it different names depending on which role they observed:

- **Legalization pages** see it dispatching `MidExpansion`, `LateExpansionUnsupportedOps`, etc., and call it "SM backend" or "arch_backend"
- **Scheduling pages** see it providing hardware latency profiles at `*(sm_backend+372)` and call it "scheduler context" or "hw_profile"
- **Optimization pages** see it dispatching `GvnCse` (vtable[23]) and `OriReassociateAndCommon` (vtable[44]) and call it "optimizer state" or "function manager"
- **Codegen/template pages** see it holding register file capacity at `+372` and hardware capability flags at `+1037`

**It is one object.** The canonical name is **`sm_backend`**. It is constructed per-compilation-unit in `sub_662920` with a switch on SM version bits (`v3 >> 12`). Each SM generation gets a different-sized allocation and a different vtable:

| SM Case | Size | Base Constructor | Vtable | SM Generations |
|---------|------|------------------|--------|----------------|
| 3 | 1712B | `sub_A99A30` | `off_2029DD0` | sm_30 (Kepler) |
| 4 | 1712B | `sub_A99A30` | `off_21B4A50` | sm_50 (Maxwell) |
| 5 | 1888B | `sub_A99A30` | `off_22B2A58` | sm_60 (Pascal) |
| 6 | 1912B | `sub_A99A30` | `off_21D82B0` | sm_70 (Volta) |
| 7 | 1928B | `sub_ACDE20` | `off_21B2D30` | sm_80 (Ampere) |
| 8 | 1992B | `sub_662220` | `off_21C0C68` | sm_89 (Ada) |
| 9 | 1992B | `sub_662220` | `off_21D6860` | sm_90+ (Hopper/Blackwell) |

Key sub-fields on the SM backend:
- `+372` (`i32`): codegen factory value / encoded SM architecture version (e.g., 28673 = sm_80)
- `+1037` (`u8`): hardware capability flags (bit 0 = has high-precision FP64 MUFU seeds)
- Vtable slots provide architecture-specific dispatch for 50+ operations

### Pipeline Progress Counter at +1552

The field at `context+1552` is a monotonically increasing `int32` that tracks how far the compilation has progressed through the 159-phase pipeline. It is **not** a legalization-only counter -- it is incremented by phases across all categories (legalization, optimization, scheduling, regalloc). Each increment is performed by a small thunk function whose sole body is `*(ctx + 1552) = N`.

Known values and their associated phases:

| Value | Thunk Address | Phase / Context |
|-------|---------------|-----------------|
| 0 | (init) | `sub_7F7DC0` -- compilation context constructor |
| 1 | `sub_C5F620` | Early pipeline (before ConvertUnsupportedOps) |
| 2 | `sub_C5F5A0` | After ConvertUnsupportedOps (phase 5) |
| 3 | `sub_C5EF80` | After MidExpansion (phase 45) |
| 4 | `sub_C5EF30` | After OriDoRematEarly (phase 54) -- signals remat mode active |
| 5 | `sub_1233D70` | Mid-pipeline scheduling/ISel context |
| 7 | `sub_6612E0` / `sub_C60AA0` | After LateExpansion (phase 55) |
| 8 | `sub_849C60` | Post-optimization context |
| 9 | `sub_C5EB80` | After OriBackCopyPropagate (phase 83) |
| 10 | `sub_88E9D0` | Late optimization |
| 11 | `sub_C5EA80` | After SetAfterLegalization (phase 95) region |
| 12 | `sub_C5E980` | Post-legalization |
| 13 | `sub_13B5C80` | ISel/scheduling |
| 14 | `sub_C5E830` | Post-scheduling |
| 15 | `sub_C5E7C0` | Register allocation phase |
| 16 | `sub_C5E6E0` | Post-regalloc |
| 17 | `sub_C5E5A0` | Mercury/codegen |
| 18 | `sub_C5E4D0` | Post-Mercury |
| 19 | `sub_C5E440` | Late codegen |
| 20 | `sub_C5E390` | Post-RA cleanup |
| 21 | `sub_C5E0B0` | Final pipeline stage |

Readers of downstream passes use `*(ctx+1552) > N` to gate behavior that should only run after a certain pipeline point. For example, the rematerialization cross-block pass checks `*(ctx+1552) > 4` to enable its second-pass mode.

### Knob Container Access Pattern

The knob container at `+1664` is accessed through a two-level virtual dispatch pattern that appears at 100+ call sites:

```c
// Fast path: known vtable -> direct array read
_QWORD *v2 = *(_QWORD **)(ctx + 1664);
bool (*query)(__int64, int) = *(bool (**)(...))(*v2 + 72);
if (query == sub_6614A0)
    result = *(u8*)(v2[9] + knob_index * 72 + offset) != 0;
else
    result = query((int64)v2, knob_index);  // slow path
```

The fast path reads directly from the knob value array at `v2[9]` (offset +72 of the knob state object), where each knob value occupies 72 bytes. The slow path invokes the virtual method for derived knob containers.

### Function Context (at +1880)

When a function is under compilation, `+1880` points to a large context object containing 17 pairs of analysis-result data structures. Each pair consists of a sorted container and a hash map, holding results such as live ranges, register maps, and scheduling data. The cleanup code in `sub_7FB6C0` destroys pairs at qword offsets `[102, 97, 92, 87, 82, 77, 72, 67, 62, 57, 52, 47, 42, 36, 31, 26, 21]` from the context base, then handles reference-counted objects at offsets `[10]` and `[2]`.

## Ori Code Object (~1136 bytes)

The Code Object is the per-function container for all IR data. One instance exists for each function under compilation. Constructor is at `sub_A3B080`, vtable at `0x21EE238`.

### Constructor Analysis

The constructor (`sub_A3B080`) takes two arguments: `a1` (the Code Object to initialize) and `a2` (the compilation context). It:

1. Sets `+8 = a2` (back-pointer to compilation context)
2. Sets `+0 = &unk_21EE238` (vtable)
3. Zeroes approximately 250 distinct fields across the 1136-byte range
4. Loads two SSE constants from `xmmword_2027600` and `xmmword_21EFAE0` into offsets +96 and +112 (likely default register file descriptors or encoding parameters)
5. Reads `a2+1412` and `a2+1418` to set mode flags at `+1101` and `+1008`
6. Accesses the knob container at `a2+1664` to query knob 367 for initial configuration
7. Sets `+1008 = 0x300000050` (default) or `0x400000080` (if `a2+1418 & 4`)

### Code Object Field Map

| Offset | Type | Field | Evidence / Notes |
|--------|------|-------|------------------|
| +0 | `vtable*` | vtable | `0x21EE238`, 263+ virtual methods |
| +8 | `ptr` | `compilation_ctx` | Back-pointer to owning compilation context |
| +16 | `u128` | (zeroed) | SSE zero-store in constructor |
| +24 | `u32` | `sm_version` | Encoded SM target (12288=sm30, 20481=sm50, 36865=sm90) |
| +32 | `u128` | (zeroed) | SSE zero-store |
| +48 | `u128` | (zeroed) | SSE zero-store |
| +64 | `u32` | `init_flags` | Zeroed in constructor |
| +72 | `ptr` | `code_buf` | Output code buffer |
| +80 | `u128` | (zeroed) | |
| +88 | `ptr` | `reg_file` | Register descriptor array: `*(ctx+88) + 8*regId` |
| +96 | `u128` | `reg_defaults_1` | Loaded from `xmmword_2027600` |
| +99 | `u32` | `ur_count` | Uniform register (UR) count |
| +102 | `u32` | `r_alloc` | R-register allocated count |
| +112 | `u128` | `reg_defaults_2` | Loaded from `xmmword_21EFAE0` |
| +128--175 | `u128[3]` | (zeroed) | SSE zero-stores |
| +152 | `ptr` | `sym_table` | Symbol/constant lookup array |
| +159 | `u32` | `r_reserved` | R-register reserved count |
| +176 | `ptr` | (zeroed) | |
| +184 | `u32` | (zeroed) | |
| +192 | `ptr` | (zeroed) | |
| +200 | `u128` | (zeroed) | |
| +216 | `u128` | (zeroed) | |
| +232 | `u32` | (zeroed) | |
| +236 | `u32` | (zeroed) | |
| +240 | `ptr` | (zeroed) | |
| +248 | `u128` | (zeroed) | |
| +264 | `u128` | (zeroed) | |
| +272 | `ptr` | `instr_head` | Instruction linked-list head |
| +280 | `u32` | (zeroed) | |
| +288 | `ptr` | (zeroed) | |
| +296 | `ptr` | `bb_array` | Basic block array pointer (40 bytes per entry) |
| +304 | `u32` | `bb_index` | Current basic block count |
| +312 | `ptr` | `options` | `OptionsManager*` for knob queries |
| +320--359 | `u128[3]` | (zeroed) | |
| +335 | `u32` | `instr_hi` | Instruction count upper bound |
| +336 | `u32` | `tex_inst_count` | Texture instruction count (stats emitter) |
| +338 | `u32` | `fp16_vect_inst` | FP16 vectorized instruction count |
| +340 | `u32` | `inst_pairs` | Instruction pair count |
| +341 | `u32` | `instr_lo` | Instruction count lower bound |
| +342 | `u32` | `tepid_inst` | Tepid instruction count |
| +360 | `ptr` | (zeroed) | |
| +368 | `u32` | `sub_block_flags` | |
| +372 | `u32` | `instr_total` | Total instruction count (triggers chunked scheduling at > 0x3FFF) |
| +376 | `u32` | (zeroed) | |
| +384--416 | `ptr[5]` | (zeroed) | |
| +424 | `u32` | (zeroed) | |
| +432 | `ptr` | (zeroed) | |
| +440 | `u32` | (zeroed) | |
| +448 | `ptr` | (zeroed) | |
| +464 | `ptr` | (zeroed) | |
| +472 | `u8` | (zeroed) | |
| +473 | `u8` | (zeroed) | |
| +536 | `u32` | (zeroed) | |
| +540 | `u32` | (zeroed) | |
| +648 | `ptr` | `succ_map` | CFG successor edge hash table |
| +680 | `ptr` | `backedge_map` | CFG backedge hash table |
| +720 | `ptr` | `rpo_array` | Reverse post-order array (`int*`) |
| +728 | `ptr` | `bitmask_array` | Grow-on-demand bitmask array for scheduling |
| +740 | `u32` | `bitmask_capacity` | Capacity of bitmask array |
| +752 | `ptr` | (zeroed) | |
| +760 | `u32` | (zeroed) | |
| +764 | `u32` | (zeroed) | |
| +768 | `ptr` | `const_sections` | Constant memory section array |
| +772 | `u8` | (zeroed) | |
| +776 | `ptr` | `smem_sections` | Shared memory section array |
| +976 | `ptr` | `block_info` | Block info array (40 bytes per entry, contiguous) |
| +984 | `i32` | `num_blocks` | Number of basic blocks |
| +996 | `u32` | `annotation_offset` | Current offset into annotation buffer (`sub_A4B8F0`) |
| +1000 | `ptr` | `annotation_buffer` | Annotation data buffer (`sub_A4B8F0`) |
| +1008 | `u64` | `encoding_params` | Default `0x300000050` or `0x400000080` |
| +1016 | `ptr` | (zeroed) | |
| +1024 | `u32` | (zeroed) | |
| +1032 | `ptr` | (zeroed) | |
| +1040 | `ptr` | (zeroed) | |
| +1064 | `ptr` | (zeroed) | |
| +1080 | `u128` | (zeroed) | |
| +1096 | `u32` | (zeroed) | |
| +1100 | `u8` | (zeroed) | |
| +1101 | `u8` | `optimization_mode` | Set from knob 367 and `compilation_ctx+1412` |
| +1102 | `u8` | (zeroed) | |
| +1104 | `ptr` | (zeroed) | |
| +1120 | `u128` | (zeroed) | |

### Register Count Formula

From the stats emitter at `sub_A3A7E0` and the register count function at `sub_A4B8F0` (which both use vtable+2104 dispatch with `sub_859FC0` as the fast path):

```c
total_R_regs      = code_obj[159] + code_obj[102]   // reserved + allocated
instruction_count = code_obj[335] - code_obj[341]   // upper - lower
```

### Stats Emitter Field Map

The stats emitter (`sub_A3A7E0`) accesses a per-function stats record through the SM backend: `v3 = *(compilation_ctx+8)[198]` (offset +1584 from the outer compilation context points to the SM backend object; the emitter then reads per-function stats fields within it). It uses DWORD indexing (4-byte), and reveals these additional fields:

| DWORD Index | Byte Offset | Field | Stat String |
|-------------|-------------|-------|-------------|
| 8 | +32 | `est_latency` | `[est latency = %d]` |
| 10 | +40 | `worst_case_lat` | `[worstcaseLat=%f]` |
| 11 | +44 | `avg_case_lat` | `[avgcaseLat=%f]` |
| 12 | +48 | `spill_bytes` | `[LSpillB=%d]` |
| 13 | +52 | `refill_bytes` | `[LRefillB=%d]` |
| 14 | +56 | `s_refill_bytes` | `[SRefillB=%d]` |
| 15 | +60 | `s_spill_bytes` | `[SSpillB=%d]` |
| 16 | +64 | `low_lmem_spill` | `[LowLmemSpillSize=%d]` |
| 17 | +68 | `frame_lmem_spill` | `[FrameLmemSpillSize=%d]` |
| 18 | +72 | `non_spill_bytes` | `[LNonSpillB=%d]` |
| 19 | +76 | `non_refill_bytes` | `[LNonRefillB=%d]` |
| 20 | +80 | `non_spill_size` | `[NonSpillSize=%d]` |
| 26 | +104 | `occupancy` (float) | `[Occupancy = %f]` |
| 27 | +108 | `div_branches` | `[est numDivergentBranches=%d]` |
| 28 | +112 | `attr_mem_usage` | `[attributeMemUsage=%d]` |
| 29 | +116 | `program_size` | `[programSize=%d]` |
| 42 | +168 | `precise_inst` | `[Precise inst=%d]` |
| 44 | +176 | `udp_inst` | `[UDP inst=%d]` |
| 45 | +180 | `vec_to_ur` | `[numVecToURConverts inst=%d]` |
| 49 | +196 | `max_live_suspend` | `[maxNumLiveValuesAtSuspend=%d]` |
| 87 | +348 | `partial_unroll` | `[partially unrolled loops=%d]` |
| 88 | +352 | `non_unrolled` | `[non-unrolled loops=%d]` |
| 89 | +356 | `cb_bound_tex` | `[CB-Bound Tex=%d]` |
| 90 | +360 | `partial_bound_tex` | `[Partially Bound Tex=%d]` |
| 91 | +364 | `bindless_tex` | `[Bindless Tex=%d]` |
| 92 | +368 | `ur_bound_tex` | `[UR-Bound Tex=%d]` |
| 93 | +372 | `sm_version_check` | `> 24575` triggers UR reporting |
| 99 | +396 | `ur_count_stats` | `[urregs=%d]` |
| 102 | +408 | `r_alloc` | R-register allocated count |
| 159 | +636 | `r_reserved` | R-register reserved count |
| 303 | +1212 | `est_fp` | `[est fp=%d]` |
| 306 | +1224 | `est_half` | `[est half=%d]` |
| 307 | +1228 | `est_transcendental` | `[est trancedental=%d]` |
| 308 | +1232 | `est_ipa` | `[est ipa=%d]` |
| 310 | +1240 | `est_shared` | `[est shared=%d]` |
| 311 | +1244 | `est_control_flow` | `[est controlFlow=%d]` |
| 315 | +1260 | `est_load_store` | `[est loadStore=%d]` |
| 316 | +1264 | `est_tex` | `[est tex=%d]` |
| 334 | +1336 | `inst_pairs` | `[instPairs=%d]` |
| 335 | +1340 | `instr_hi` | Instruction count upper bound |
| 336 | +1344 | `tex_inst_count` | `[texInst=%d]` |
| 337 | +1348 | `fp16_inst` | `[FP16 inst=%d]` |
| 338 | +1352 | `fp16_vect_inst` | `[FP16 VectInst=%d]` |
| 339 | +1356 | `inst_hint` | `[instHint=%d]` |
| 340 | +1360 | `inst_pairs_2` | checked for non-zero to print instHint line |
| 341 | +1364 | `instr_lo` | Instruction count lower bound |
| 342 | +1368 | `tepid_inst` | `[tepid=%d]` |

Note: The stats emitter accesses the Code Object through a float pointer (`v3`), so DWORD indices map to byte offsets via `index * 4` for integers and `index * 4` for floats. Float fields at indices 9, 26, 50, 54, 57, 58, 59, 61, 62, 65, 84, 85, 86 hold throughput and occupancy metrics. A linked list at qword index 55 (byte +440) holds additional string annotations.

## Basic Block Entry (40 bytes)

Basic blocks are stored in a contiguous array at Code Object +976, with count at +984.

```
BasicBlock (40 bytes)
  +0    ptr      instr_head     // first instruction in this BB
  +8    ptr      instr_tail     // last instruction (or list link)
  +16   ptr      (reserved)
  +24   u32      (reserved)
  +28   i32      bix            // block index (unique ID for CFG ops)
  +32   u64      flags          // scheduling/analysis flags
```

The scheduling pass (`sub_8D0640`) initializes per-block scheduling state by iterating the block list and zeroing qword offsets `[7]`, `[13]`, `[19]`, and setting `[21] = -1` on each block.

## Instruction Layout

Instructions are polymorphic C++ objects linked into per-BB doubly-linked lists. The instruction format is detailed in [Instructions](./instructions.md); this section covers only the structural linkage.

Each instruction carries a unique integer ID at `+16`, an opcode at `+72` (the peephole optimizer masks with `& 0xCF` on byte 1 to strip modifier bits), and a packed operand array starting at `+84`. The operand count is at `+80`. Operands are 8 bytes each.

### Packed Operand Format

```
 31  30  29  28  27       24  23  22  21  20  19                  0
+---+---+---+---+-----------+---+---+---+---+---------------------+
|     type      |  modifier bits (8 bits)    |  index (20 bits)    |
+---+---+---+---+-----------+---+---+---+---+---------------------+

Extraction (50+ confirmed sites):
  uint32_t operand = *(uint32_t*)(instr + 84 + 8 * i);
  int type    = (operand >> 28) & 7;     // bits 28-30
  int index   = operand & 0xFFFFF;       // bits 0-19
  int mods    = (operand >> 20) & 0xFF;  // bits 20-27
```

| Type Value | Meaning | Resolution |
|------------|---------|------------|
| 1 | Register operand | Index into `*(code_obj+88)` register file |
| 5 | Symbol/constant operand | Index into `*(code_obj+152)` symbol table |

The operand classifier functions at `0xB28E00`--`0xB28E90` provide predicate checks:

| Function | Predicate |
|----------|-----------|
| `sub_B28E00` | `getRegClass` (1023 = wildcard, 1 = GPR) |
| `sub_B28E10` | `isRegOperand` |
| `sub_B28E20` | `isPredOperand` |
| `sub_B28E40` | `isImmOperand` |
| `sub_B28E80` | `isConstOperand` |
| `sub_B28E90` | `isUReg` |

## Symbol Table

The symbol table is accessed through Code Object `+152`. Based on the symbol table builder at `sub_621480` (21KB, references `a1+30016` for the symbol table base), symbols are stored in a hash-map-backed structure where each symbol has a name and associated properties (address, type, section binding).

### Internal Symbol Names

The following internal symbol names appear in decompiled code, indicating the kinds of entities tracked:

| Symbol | Purpose |
|--------|---------|
| `__ocg_const` | OCG-generated constant data |
| `__shared_scratch` | Shared memory scratch space |
| `__funcAddrTab_g` | Global indirect function call table |
| `__funcAddrTab_c` | Constant indirect function call table |
| `_global_ptr_%s` | Global pointer for named variable |
| `$funcID$name` | Function-local relocation symbol |
| `__cuda_dummy_entry__` | Dummy entry generated by `--compile-only` |
| `__cuda_sanitizer` | CUDA sanitizer instrumentation symbol |

### Symbol Resolution Flow

Symbol resolution (`sub_625800`, 27KB) traverses the symbol table to resolve references during the PTX-to-Ori lowering and subsequent optimization phases. The format `%s[%d]` (from `sub_6200A0`) is used for array-subscripted symbol references, and `__$endLabel$__%s` markers delimit function boundaries.

## Constant Buffer Layout

Constant memory is organized into banks (c[0], c[1], ...) corresponding to the CUDA `.nv.constant0`, `.nv.constant2`, etc. ELF sections. The constant section array at Code Object `+768` tracks all constant banks for the current function.

### Constant Bank Handling

The constant bank handler at `sub_6BC560` (4.9KB) manages references to constant memory using the `c[%d]` (integer bank) and `c[%s]` (named bank, `sw-compiler-bank`) notation. It enforces:

- A maximum constant register count (error: "Constant register limit exceeded; more than %d constant registers")
- LDC (Load Constant) requires a constant or immediate bank number

### ELF Constant Symbols

The ELF symbol emitter (`sub_7FD6C0`) creates symbols for constant bank metadata:

| Symbol Name | Purpose |
|-------------|---------|
| `.nv.ptx.const0.size` | Size of constant bank 0 (kernel parameters) |

The constant emission function (`sub_7D14C0`, 5.6KB) iterates the constant section array and copies bank data into the output ELF sections.

## Shared Memory Layout

Shared memory (`.nv.shared`) allocations are tracked through the shared memory section array at Code Object `+776`. Reserved shared memory regions are managed by `sub_6294E0` (12.1KB) and `sub_629E40` (6.1KB).

### Reserved Shared Memory Symbols

The ELF emitter recognizes these special symbols for shared memory layout:

| Symbol Name | Purpose |
|-------------|---------|
| `.nv.reservedSmem.begin` | Start of reserved shared memory region |
| `.nv.reservedSmem.cap` | Capacity of reserved shared memory |
| `.nv.reservedSmem.end` | End of reserved shared memory region |
| `.nv.reservedSmem.offset0` | First reserved offset within shared memory |
| `.nv.reservedSmem.offset1` | Second reserved offset within shared memory |

The `--disable-smem-reservation` CLI option disables the reservation mechanism. Shared memory intrinsic lowering (`sub_6C4DA0`, 15KB) validates that shared memory operations use types `{b32, b64}`.

### Descriptor Size Symbols

Additional ELF symbols track texture/surface descriptor sizes in shared memory:

| Symbol Name | Purpose |
|-------------|---------|
| `.nv.unified.texrefDescSize` | Unified texture reference descriptor size |
| `.nv.independent.texrefDescSize` | Independent texture reference descriptor size |
| `.nv.independent.samplerrefDescSize` | Independent sampler reference descriptor size |
| `.nv.surfrefDescSize` | Surface reference descriptor size |

## Pool Allocator

The pool allocator (`sub_424070`, 3,809 callers) is the single most heavily used allocation function. Every dynamic data structure in ptxas is allocated through pools.

### Pool Object Layout

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| +0 | `ptr` | `large_block_list` | Singly-linked list of large (>4999 byte) blocks |
| +32 | `u32` | `min_slab_size` | Minimum slab allocation size |
| +44 | `u32` | `slab_count` | Number of slabs allocated |
| +48 | `ptr` | `large_free_list` | Free list for large blocks (boundary-tag managed) |
| +56 | `u32` | `fragmentation_count` | Fragmentation counter (decremented on split) |
| +60 | `u32` | `max_order` | Maximum power-of-2 order for large blocks |
| +64 | ... | (large block free lists) | `a1 + 32*(order+2)` = per-order free list head |
| +2112 | `ptr` | `tracking_map` | Hash map for allocation metadata tracking |
| +2128 | `ptr[N]` | `small_free_lists` | Size-binned free lists: `*(pool + 8*(size>>3) + 2128)` = head |
| +7128 | `mutex*` | `pool_mutex` | `pthread_mutex_t*` for thread safety |

### Allocation Paths

**Small path** (size <= 4999 bytes = `0x1387`):
1. Round size up to 8-byte alignment: `aligned = (size + 7) & ~7`
2. Minimum 16 bytes
3. Compute bin: `bin = pool + 8 * (aligned >> 3) + 2128`
4. If bin has a free block: pop from free list, decrement slab available bytes
5. If bin is empty: allocate a new slab from the parent (size = `aligned * ceil(min_slab_size / aligned)`), carve into free-list nodes

**Large path** (size > 4999 bytes):
1. Add 32 bytes for boundary tags
2. Search power-of-2 order free lists starting from `log2(size+32)`
3. If found: split block if remainder > 39 bytes, return payload
4. If not found: call `sub_423B60` to grow the pool, allocate new slab from parent

### Boundary Tag Format (Large Blocks)

Large blocks use boundary tags for coalescing on free:

```
Block Header (32 bytes):
  +0    i64      sentinel      // -1 = allocated, else -> next free
  +8    ptr      prev_free     // previous in free list (or 0)
  +16   u64      tag_offset    // 32 (header size)
  +24   u64      payload_size  // user-requested allocation size

Block Footer (32 bytes at end):
  +0    i64      sentinel
  +8    ptr      prev_free
  +16   u64      footer_tag    // 32
  +24   u64      block_size    // total size including headers
```

### Slab Descriptor (56 bytes)

Each slab is tracked by a 56-byte descriptor:

| Offset | Type | Field |
|--------|------|-------|
| +0 | `ptr` | `chain_link` | Link to next slab descriptor |
| +8 | `u64` | `total_size` | Total bytes in this slab |
| +16 | `u64` | `available_size` | Bytes remaining (decremented on alloc) |
| +24 | `ptr` | `owning_pool` | Back-pointer to pool |
| +32 | `ptr` | `memory_base` | Base address of slab memory |
| +40 | `u8` | `is_small_slab` | 1 = small-alloc slab, 0 = large-alloc slab |
| +44 | `u32` | `slab_id` | Global slab sequence number (atomic counter) |
| +48 | `u32` | `bin_size` | Size class for small slabs |

### Hierarchical Pools

Pools are hierarchical. When `sub_424070` is called with `a1 = NULL`, it falls back to a global allocator (`sub_427A10`) that uses `malloc` directly. Non-null `a1` values are pool objects that allocate from their own slabs, which are themselves allocated from a parent pool (the TLS context at offset +24 holds the per-thread pool pointer). The top-level pool is named `"Top level ptxas memory pool"` and is created in the compilation driver.

## Hash Map

The hash map (`sub_426150` insert / `sub_426D60` lookup, 2,800+ and 422+ callers respectively) is the primary associative container in ptxas.

### Hash Map Object Layout (~112 bytes)

| Offset | Type | Field | Notes |
|--------|------|-------|-------|
| +0 | `fptr` | `hash_func` | Custom hash function pointer |
| +8 | `fptr` | `compare_func` | Custom compare function pointer |
| +16 | `fptr` | `hash_func_2` | Secondary hash (or NULL) |
| +24 | `fptr` | `compare_func_2` | Secondary compare (or NULL) |
| +32 | `u32` | `has_custom_compare` | Flag |
| +40 | `u64` | `bucket_mask` | `capacity - 1` for power-of-2 masking |
| +48 | `u64` | `entry_count` | Number of stored entries |
| +64 | `u64` | `load_factor_threshold` | Resize when `entry_count` exceeds this |
| +72 | `u32` | `first_free_slot` | Tracking for bitmap-based slot allocation |
| +76 | `u32` | `entries_capacity` | Capacity of entries array |
| +80 | `u32` | `bitmap_capacity` | Capacity of used-bits bitmap |
| +84 | `u32` | `flags` | Hash mode in bits 4-7 |
| +88 | `ptr` | `entries` | Array of 16-byte `{key, value}` pairs |
| +96 | `ptr` | `used_bitmap` | Bitmap tracking occupied slots |
| +104 | `ptr` | `buckets` | Array of pointers to chained index lists |

### Hash Modes

The hash mode is encoded in bits 4-7 of the flags field at offset +84:

| Mode | Flag Bits | Hash Function | Use Case |
|------|-----------|---------------|----------|
| 0 | `0x00` | Custom (`+0` function pointer) | User-defined hash/compare |
| 1 | `0x10` | Pointer hash: `(key>>11) ^ (key>>8) ^ (key>>5)` | Pointer-keyed maps |
| 2 | `0x20` | Identity: key used directly | Integer-keyed maps |

Mode selection happens automatically in the constructor (`sub_425CA0`): if the hash/compare pair matches `(sub_427750, sub_427760)`, mode 2 is set; if `(sub_4277F0, sub_427810)`, mode 1.

### Lookup Algorithm

```c
// Mode 1 (pointer hash) example:
uint64_t hash = (key >> 11) ^ (key >> 8) ^ (key >> 5);
uint64_t bucket_idx = hash & map->bucket_mask;
int32_t* chain = map->buckets[bucket_idx];
while (*++chain != -1) {
    entry_t* e = map->entries + 16 * (*chain);
    if (key == e->key)
        return e->value;  // found
}
return 0;  // not found
```

Growth policy: the map doubles capacity and rehashes when `entry_count > load_factor_threshold`.

### String-Keyed Maps

String-keyed maps use MurmurHash3 (`sub_427630`, 73 callers) as the hash function. The implementation uses the standard MurmurHash3_x86_32 constants:

| Constant | Value | Standard Name |
|----------|-------|---------------|
| c1 | `0xCC9E2D51` (-862048943) | MurmurHash3 c1 |
| c2 | `0x1B873593` (461845907) | MurmurHash3 c2 |
| fmix1 | `0x85EBCA6B` (-2048144789) | MurmurHash3 fmix |
| fmix2 | `0xC2B2AE35` (-1028477387) | MurmurHash3 fmix |

## CFG Hash Map (FNV-1a)

The control flow graph uses a separate hash map implementation based on FNV-1a hashing, distinct from the general-purpose hash map above. Two instances exist per Code Object at offsets +648 (successor edges) and +680 (backedge info).

| Parameter | Value |
|-----------|-------|
| Initial hash | `0x811C9DC5` (-2128831035) |
| Prime | `16777619` (`0x01000193`) |
| Input | 4-byte block index, hashed byte-by-byte |

Bucket entry: 24 bytes `{head, tail, count}`. Node: 64 bytes with chain link, key, values, sub-hash data, and cached hash. See [CFG](./cfg.md) for the full CFG hash map specification.

## Linked List

The linked list (`sub_42CA60` prepend, 298 callers; `sub_42CC30` length, 48 callers) is a singly-linked list of 16-byte nodes:

```
ListNode (16 bytes, pool-allocated)
  +0    ptr      next        // pointer to next node (NULL = end)
  +8    ptr      data        // pointer to payload object
```

Prepend allocates a 16-byte node from the pool, sets `node->data = payload`, and links it at the list head. This is used for function lists, relocation lists, annotation chains, and many intermediate pass-local collections.

## Growable Array (Pool Vector)

Growable arrays appear throughout the PhaseManager and elsewhere. The layout is a triple of `{data_ptr, count, capacity}`:

```
PoolVector (24 bytes inline, or embedded in parent struct)
  +0    ptr      data         // pointer to element array
  +8    i32      count        // current element count
  +12   i32      capacity     // allocated capacity
```

Growth strategy (confirmed in the PhaseManager timing records): `new_capacity = max(old + old/2 + 1, requested)` (1.5x growth factor). Elements are typically 8 bytes (pointers) or 16 bytes (pointer pairs). Reallocation uses `sub_424C50` (pool realloc, 27 callers).

The PhaseManager uses this pattern for the phase list (16-byte `{phase_ptr, pool_ptr}` pairs), the name table (8-byte string pointers), and the timing records (32-byte entries).

## Knob Value Array

Knob values are stored in a contiguous array of 72-byte slots, accessed at `knob_state[9] + 72 * knob_index` (where `knob_state[9]` is offset +72 of the knob state object).

### Knob Value Slot (72 bytes)

| Offset | Type | Field |
|--------|------|-------|
| +0 | `u8` | Type tag (0=unset, 1=bool, 2=int, ..., 12=opcode list) |
| +8 | `i64` | Integer value / pointer to string / linked list head |
| +16 | `i64` | Secondary value (range max, list count, etc.) |
| +24 | `i64` | Tertiary value |
| +64 | `ptr` | Allocator reference |

Supported types:

| Type | Tag | Storage |
|------|-----|---------|
| Boolean | 1 | Flag at +0 |
| Integer | 2 | Value at +8 |
| Integer+extra | 3 | Value at +8, extra at +12 |
| Integer range | 4 | Min at +8, max at +16 |
| Integer list | 5 | Growable array of ints |
| Float | 6 | `float` at +8 |
| Double | 7 | `double` at +8 |
| String | 8/11 | Pointer at +8 |
| When-string | 9 | Linked list of 24-byte condition+value nodes |
| Value-pair list | 10 | Opcode:integer pairs via vtable |
| Opcode list | 12 | Opcode names resolved through vtable |

### Knob Descriptor (64 bytes)

Knob descriptors are stored in a table at `knob_state+16`, with count at `knob_state+24`:

| Offset | Type | Field |
|--------|------|-------|
| +0 | `ptr` | Primary name (ROT13-encoded) |
| +8 | `u64` | Primary name length |
| +16 | `u32` | Type tag |
| +24 | ... | (reserved) |
| +40 | `ptr` | Alias name (ROT13-encoded) |
| +48 | `u64` | Alias name length |

## Stream Object

The output stream used for diagnostics and stats reporting (e.g., at compilation context +1440) is a C++ iostream-like object with operator overloads. Field layout (from `sub_7FE5D0` and `sub_7FECA0`):

| Offset | Type | Field |
|--------|------|-------|
| +0 | `vtable*` | vtable (dispatch for actual I/O) |
| +8 | `u32` | `width` |
| +12 | `u32` | `precision` |
| +16 | `u64` | `char_count` |
| +24 | `ptr` | `format_buffer` |
| +56 | `u32` | `flags` (bit 0=hex, bit 1=oct, bit 2=left-align, bit 3=uppercase, bits 7-8=sign) |

## ORI Record Serializer (`sub_A50650`)

The ORI Record Serializer (`sub_A50650`, 74 KB, 2,728 decompiled lines) is the central function that takes a Code Object's in-memory state and flattens it into a linear output buffer organized as a table of typed section records. It is the serialization backbone for both the DUMPIR diagnostic subsystem and the compilation output path. Despite the `_ORI_` string it contains, it is not an optimization pass -- it is infrastructure.

| | |
|---|---|
| **Address** | `0xA50650` |
| **Size** | ~74 KB |
| **Identity** | `CodeObject::EmitRecords` |
| **Confidence** | 0.90 |
| **Called from** | `sub_A53840` (wrapper), `sub_AACBF0` / `sub_AAD2A0` (DUMPIR diagnostic path) |
| **Calls** | `sub_A4BC60` (register serializer, new format), `sub_A4D3F0` (legacy format), `sub_A4B8F0` (register count annotation), `sub_A47330` + `sub_A474F0` (multi-section finalization), `sub_1730890` / `sub_17308C0` / `sub_17309A0` (scheduling serializers), `sub_1730FE0` (register file map) |

### Parameters

`a1` is a serialization state object ("OriRecordContext") that carries the section table, compilation context back-pointer, and per-subsection index/size pairs. `a2` is the output buffer write cursor, advanced as data is emitted.

Key fields on `a1`:

| Offset | Type | Field | Evidence |
|--------|------|-------|----------|
| +8 | `ptr` | `compilation_ctx` | Dereferenced to reach sm_backend at `+1584` |
| +24 | `i32` | `header_section_idx` | `v5 + 32 * (*(a1+24) + 1)` |
| +72 | `ptr` | `section_table` | Array of 32-byte section entries |
| +180 | `u32` | `instr_counter_1` | Reset to 0 at entry |
| +472 | `u8` | `has_debug_info` | Gates debug section emission |
| +916 | `i32` | `multi_section_count` | `> 0` triggers link-record emission and tail call to `sub_A47330` |
| +1102 | `u8` | `multi_section_enabled` | Master flag for multi-section mode |
| +1120 | `ptr` | `scheduling_ctx` | Scheduling context for barrier/scope serialization |

### Section Record Format

Each section occupies a 32-byte entry in the table at `*(a1+72) + 32 * section_index`:

```
Offset  Type   Field
+0      u16    type_tag           section type identifier
+4      u32    data_size          byte size of data payload
+8      ptr    data_ptr           pointer to data in output buffer
+16     u32    element_count      number of elements (or auxiliary metadata)
+20     u32    aux_field          additional per-type context
+24     u32    aux_field_2        secondary per-type context
```

Data payloads are 16-byte aligned: `cursor += (size + 15) & ~0xF`.

### Section Type Tag Catalog

The serializer emits up to 56 unique section types across three tag ranges.

**Base types (0x01--0x58):**

| Tag | Hex | Content | Evidence |
|-----|-----|---------|----------|
| 1 | 0x01 | Instruction stream (register-allocated code body) | Emitted via `sub_A4BC60` or `sub_A4D3F0` |
| 3 | 0x03 | Virtual-dispatch section (vtable+48 on state obj) | Conditional on `*(a1+64) > 0` |
| 16 | 0x10 | Source operand bank (v7[199] entries at v7+97) | `*(entry+48) = v7[199]` |
| 17 | 0x11 | Destination operand bank (bit-packed from v7+203) | Conditional on `!v7[1414]` |
| 19 | 0x13 | Annotation stream | `*(a1+232)` counter |
| 34 | 0x22 | Original-definition name table (`_ORI_` prefixed) | `strcpy(v50, "_ORI_")` at line 1762 |
| 35 | 0x23 | Instruction info snapshot (340 bytes from v7+4) | `qmemcpy` of 340 bytes |
| 46 | 0x2E | Texture/surface binding table | `v7[248]` entries, 16 bytes each |
| 50 | 0x32 | Live range interval table (spill map) | From compilation context +984 |
| 51 | 0x33 | Register file occupancy table | `*(ctx+1424) & 4` |
| 53 | 0x35 | Source operand type bitmap (4-bit per operand) | v7[131] operands, 20-byte stride |
| 54 | 0x36 | Destination operand type bitmap | v7[134] operands, 20-byte stride |
| 55 | 0x37 | Scheduling barrier data | via `sub_1730890` |
| 56 | 0x38 | Register file mapping | via `sub_1730FE0` |
| 58 | 0x3A | Scheduling dependency graph | via `sub_17309A0` |
| 59 | 0x3B | Multi-section link record | Conditional on `*(a1+1102)` |
| 64 | 0x40 | External reference (from ctx+2120) | Pointer stored, no data copy |
| 68 | 0x44 | Performance counter section | `*(a1+932)` counter |
| 70 | 0x46 | Spill/fill metadata | `v7[408]` |
| 71 | 0x47 | Call graph edge table | From v7+61, linked list traversal |
| 73 | 0x49 | Codegen context snapshot | From ctx+932 register allocation state |
| 80 | 0x50 | Hash table section | v7+207/208, hash bucket traversal |
| 81 | 0x51 | Extended call info | From v7+84 |
| 83 | 0x53 | Convergence scope data | via `sub_17308C0` |
| 85 | 0x55 | Register geometry record (banks, warps, lanes) | From ctx+1600, writes bank/warp/lane counts |
| 88 | 0x58 | Extended scheduling annotations | Conditional on `*(a1+1088) > 0` |

**Extended types (0x1208--0x1221):** Emitted only when `*(char*)(ctx+1412) < 0`, which enables the full post-register-allocation diagnostic mode. These 16 types carry per-register-class live range and operand definition data:

| Tag | Hex | Content |
|-----|-----|---------|
| 4616 | 0x1208 | Extended operand class 0 |
| 4617--4623 | 0x1209--0x120F | Extended operand classes 1--7 |
| 4624 | 0x1210 | Block-level operand summary |
| 4625 | 0x1211 | Live-in vector (12 bytes/element, count at `*(a1+668)`) |
| 4626 | 0x1212 | Live-out vector (12 bytes/element) |
| 4627 | 0x1213 | Extended operand class 8 |
| 4628--4629 | 0x1214--0x1215 | Extended operand classes 9--10 |
| 4630 | 0x1216 | Memory space descriptor (SM arch > 0x4FFF) |
| 4631 | 0x1217 | Extended scheduling flag (SM arch > 0x4FFF) |
| 4632 | 0x1218 | Instruction hash (ctx+1386 bit 3) |
| 4633 | 0x1219 | Annotation metadata |
| 4640 | 0x1220 | Extended section metadata |
| 4641 | 0x1221 | Optimization level record (from knob system, knob 988) |

### The `_ORI_` Name Prefix

The `_ORI_` string is not a pass name. At line 1762 the serializer iterates the linked list at `v7+55` (the original-definition chain maintained for rematerialization debugging) and for each entry creates a string `"_ORI_<original_name>"`:

```c
// Line 1748-1770 (simplified)
for (def = v7->original_defs; def; def = def->next) {
    entry = &section_table[16 * (state->instr_offset + idx)];
    entry->type_tag = 34;      // original-definition name
    entry->data_ptr = cursor;
    strcpy(cursor, "_ORI_");
    strcpy(cursor + 5, def->name);
    cursor += align16(strlen(def->name) + 21);
}
```

These names are consumed by the register allocation verifier (`sub_A55D80`) when it compares pre- and post-allocation reaching definitions. A mismatch triggers the "REMATERIALIZATION PROBLEM" diagnostic (string at `0xa55dd8`), which lists original definitions under their `_ORI_` names alongside the post-allocation state.

### Wrapper: `sub_A53840`

`sub_A53840` (48 lines) is a thin wrapper that:
1. Emits a type-44 header record if `*(ctx+1600)[1193]` is set (scheduling metadata header)
2. Calls `sub_A50650` with the output buffer
3. Optionally emits a type-62 trailer record if `*(ctx+1600)[48]` is set

This wrapper is the typical entry point reached through vtable dispatch.

## Function Map

| Address | Size | Callers | Identity |
|---------|------|---------|----------|
| `sub_A3B080` | ~700 B | multiple | Code Object constructor |
| `sub_A3A7E0` | ~700 B | 1 | Stats emitter (per-function profile) |
| `sub_A4B8F0` | ~250 B | 1 | Register count / annotation writer |
| `sub_A50650` | ~74 KB | 8 | ORI Record Serializer (`CodeObject::EmitRecords`) |
| `sub_A53840` | ~400 B | 1 | EmitRecords wrapper (adds type-44 header) |
| `sub_424070` | 2,098 B | 3,809 | Pool allocator (`alloc`) |
| `sub_4248B0` | 923 B | 1,215 | Pool deallocator (`free`) |
| `sub_424C50` | 488 B | 27 | Pool reallocator (`realloc`) |
| `sub_426150` | ~1.2 KB | 2,800 | Hash map insert |
| `sub_426D60` | 345 B | 422 | Hash map lookup |
| `sub_426EC0` | 349 B | 29 | Hash map contains |
| `sub_425CA0` | 114 B | 127 | Hash map constructor |
| `sub_425D20` | 121 B | 63 | Hash map destructor |
| `sub_42CA60` | 81 B | 298 | Linked list prepend |
| `sub_42CC30` | 34 B | 48 | Linked list length |
| `sub_427630` | 273 B | 73 | MurmurHash3 string hash |
| `sub_621480` | 21 KB | low | Symbol table builder |
| `sub_625800` | 27 KB | low | Symbol resolution |
| `sub_6BC560` | 4.9 KB | low | Constant bank handler |
| `sub_6294E0` | 12.1 KB | low | Reserved shared memory management |
| `sub_6C4DA0` | 15 KB | low | Shared memory intrinsic lowering |
| `sub_7FD6C0` | ~800 B | 3 | ELF symbol emitter |
| `sub_7FB6C0` | ~800 B | 1 | Pipeline orchestrator (context cleanup) |
| `sub_7FBB70` | ~100 B | 1 | Per-kernel entry point |
| `sub_663C30` | ~300 B | 1 | Compilation loop body |
| `sub_662920` | varies | 1 | Global initialization (calls KnobsInit) |

## Related Pages

- [Ori IR Overview](./overview.md) -- top-level IR design, Code Object field summary
- [Instructions](./instructions.md) -- detailed instruction format and encoding
- [CFG](./cfg.md) -- FNV-1a hash map CFG implementation
- [Registers](./registers.md) -- register descriptor layout
- [Phase Manager](../passes/phase-manager.md) -- PhaseManager object layout, phase dispatch
- [Memory Pool Allocator](../infra/memory-pools.md) -- full allocator internals
- [Hash Tables & Bitvectors](../infra/hash-bitvector.md) -- hash map and bitvector details
- [Knobs System](../config/knobs.md) -- knob descriptors, value types, ROT13 encoding
- [Entry Point & CLI](../pipeline/entry.md) -- compilation driver, options block
