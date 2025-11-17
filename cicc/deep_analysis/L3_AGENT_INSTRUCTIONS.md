# L3 PHASE: 27 AGENT INSTRUCTIONS FOR EXTRACTING CRITICAL UNKNOWNS

**Phase**: L3 Implementation - Knowledge Extraction from IDA Output
**Goal**: Extract all 27 critical unknowns using decompiled code analysis
**Tools Required**: Text editor, grep/rg, jq, Python (all free)
**Timeline**: 3-6 weeks with parallel execution
**Total Estimated Effort**: 105 hours (down from 440 hours)

---

## CRITICAL UNKNOWNS (8 agents)

### **Agent L3-01: Spill Cost Formula Extraction**

**Priority**: CRITICAL
**Estimated Time**: 20 hours
**Deliverable**: `L3/register_allocation/spill_cost_formula.json`

**INSTRUCTIONS**:

1. **Read Main Register Allocation Function**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled
   cat sub_B612D0_0xb612d0.c > /tmp/regalloc_main.c
   wc -l /tmp/regalloc_main.c  # Should be ~3000-4000 lines
   ```

2. **Search for Spill Cost Calculations**:
   ```bash
   # Find all references to "spill" and "cost"
   rg -i "spill.*cost|cost.*spill" sub_B612D0_0xb612d0.c -A 5 -B 5 > spill_cost_contexts.txt

   # Find loop depth references
   rg -i "loop.*depth|depth.*loop|nesting" sub_B612D0_0xb612d0.c -A 3 -B 3 > loop_depth_refs.txt

   # Find multiplication with constants (likely multipliers)
   rg "\* [0-9]\.[0-9]+|\* [2-9]|<<|pow\(" sub_B612D0_0xb612d0.c > multiplier_candidates.txt
   ```

3. **Search in Helper Functions**:
   ```bash
   # Check functions called by 0xB612D0
   cat ../graphs/sub_B612D0_0xb612d0.json | jq '.callees[] | select(.name | contains("cost") or contains("spill"))' > callee_cost_functions.txt

   # Read those functions
   for addr in $(jq -r '.address' callee_cost_functions.txt); do
       find . -name "*${addr}*.c" -exec cat {} \;
   done > helper_functions.c
   ```

4. **Identify the Formula Pattern**:
   Look for code like:
   ```c
   spill_cost = base_cost * pow(multiplier, loop_depth) * frequency_weight;
   // OR
   cost = (def_count * use_count * distance) * loop_multiplier;
   ```

5. **Extract Exact Coefficients**:
   - Loop depth multiplier (suspected: 1.5-2.0)
   - Occupancy penalty weight
   - Bank conflict penalty
   - SM-version adjustments
   - Base cost calculation

6. **Validate with Call Graph**:
   ```bash
   # Trace spill cost calculation call chain
   python3 << 'EOF'
   import json
   import sys

   with open('../graphs/sub_B612D0_0xb612d0.json') as f:
       graph = json.load(f)

   # Find functions with "cost" in loop depth context
   for callee in graph['callees']:
       print(f"Check: {callee['address']} - {callee.get('name', 'unknown')}")
   EOF
   ```

7. **Document Findings**:
   ```json
   {
     "metadata": {
       "unknown_id": "1",
       "agent": "L3-01",
       "confidence": "HIGH",
       "validation": "code analysis"
     },
     "spill_cost_formula": {
       "formula": "base_cost * loop_depth_multiplier^depth * occupancy_penalty",
       "coefficients": {
         "loop_depth_multiplier": 1.8,
         "occupancy_penalty_weight": 1.5,
         "bank_conflict_penalty": 2.0,
         "base_cost_calculation": "def_count * use_count * furthest_next_use"
       },
       "evidence": {
         "code_location": "sub_B612D0_0xb612d0.c:1234",
         "exact_line": "cost = base * pow(1.8, depth) * occupancy;",
         "context": "Spill selection in Phase 5 of graph coloring"
       },
       "sm_specific_adjustments": {
         "sm_70": "multiplier = 1.8",
         "sm_80": "multiplier = 2.0 (higher due to larger register file)",
         "sm_90": "multiplier = 2.2 (warpgroup context)"
       }
     }
   }
   ```

8. **Validation Checklist**:
   - [ ] Found formula in decompiled code
   - [ ] Extracted all coefficients
   - [ ] Identified SM-specific variations
   - [ ] Documented code locations
   - [ ] Cross-referenced with helper functions

---

### **Agent L3-02: Cost Model Coefficients**

**Priority**: CRITICAL
**Estimated Time**: 25 hours
**Deliverable**: `L3/instruction_selection/cost_model_complete.json`

**INSTRUCTIONS**:

1. **Read Cost Calculation Functions**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # These are tiny - read entirely
   cat sub_FDE760_0xfde760.c  # 531 bytes - Cost calculation
   cat sub_D788E0_0xd788e0.c  # 681 bytes - Cost comparison
   cat sub_D788C0_0xd788c0.c  # If exists - Cost utility
   ```

2. **Extract Cost Formula Components**:
   ```bash
   # Look for weight/coefficient variables
   rg "weight|coefficient|factor|scale" sub_FDE760_0xfde760.c sub_D788E0_0xd788e0.c

   # Look for addition of cost components
   rg "\+ .*latency|\+ .*throughput|\+ .*reg" sub_FDE760_0xfde760.c

   # Look for constant multiplications
   rg "\* [0-9]\.[0-9]+|\* [0-9]+" sub_FDE760_0xfde760.c
   ```

3. **Trace Cost Function Callers**:
   ```bash
   # Find who calls these cost functions
   cat ../graphs/sub_FDE760_0xfde760.json | jq '.callers'
   cat ../graphs/sub_D788E0_0xd788e0.json | jq '.callers'

   # Read the main caller (likely instruction selection)
   cat sub_2F9DAC0_0x2f9dac0.c | rg "FDE760|D788E0" -A 10 -B 10
   ```

4. **Identify Complete Formula**:
   Expected pattern:
   ```c
   cost = (latency * latency_weight) +
          (throughput * throughput_weight) +
          (register_pressure * reg_weight) +
          (memory_latency * mem_weight) +
          (critical_path_factor * critical_weight);
   ```

5. **Extract All Coefficients**:
   - Latency weight
   - Throughput weight
   - Register pressure weight
   - Memory access weight
   - Critical path multiplier
   - Operand setup cost

6. **Find SM-Specific Tables**:
   ```bash
   # Search for latency tables
   rg "latency.*table|sm_[0-9]+.*latency" sub_2F9DAC0_0x2f9dac0.c

   # Search for SM dispatch
   rg "sm_version|compute_capability|arch.*version" sub_2F9DAC0_0x2f9dac0.c -A 5
   ```

7. **Document Findings**:
   ```json
   {
     "cost_model_formula": {
       "formula": "latency*w1 + throughput*w2 + reg_pressure*w3 + mem_latency*w4 + critical_path*w5",
       "weights": {
         "latency_weight": 1.0,
         "throughput_weight": 0.8,
         "register_pressure_weight": 1.5,
         "memory_latency_weight": 2.0,
         "critical_path_weight": 1.2
       },
       "code_locations": {
         "formula": "sub_FDE760_0xfde760.c:25",
         "weights": "sub_FDE760_0xfde760.c:10-20",
         "usage": "sub_2F9DAC0_0x2f9dac0.c:450"
       },
       "sm_specific_latencies": {
         "sm_70": {"add": 1, "mul": 2, "fma": 2, "load": 80},
         "sm_80": {"add": 1, "mul": 2, "fma": 2, "load": 80},
         "sm_90": {"add": 1, "mul": 2, "fma": 2, "load": 60}
       }
     }
   }
   ```

---

### **Agent L3-03: PTX Pattern Database**

**Priority**: CRITICAL
**Estimated Time**: 40 hours
**Deliverable**: `L3/instruction_selection/pattern_database.json`

**INSTRUCTIONS**:

1. **Read Pattern Matching Function**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled
   cat sub_2F9DAC0_0x2f9dac0.c > /tmp/pattern_matcher.c
   ```

2. **Find Hash Table Structure**:
   ```bash
   # Look for hash table initialization
   rg "hash|table|pattern.*db|lookup" sub_2F9DAC0_0x2f9dac0.c -A 10

   # Find table size
   rg "[0-9]{3,4}.*entries|capacity.*[0-9]{3,4}" sub_2F9DAC0_0x2f9dac0.c

   # Find hash function
   rg "hash\(|compute.*hash|key.*%" sub_2F9DAC0_0x2f9dac0.c -A 5
   ```

3. **Identify Pattern Structure**:
   Look for struct like:
   ```c
   struct Pattern {
       uint32_t ir_opcode;        // IR operation
       uint32_t operand_types[4]; // Operand constraints
       char* ptx_template;        // PTX instruction template
       uint16_t cost;             // Pattern cost
       uint8_t sm_min;            // Minimum SM version
       uint8_t flags;             // Special flags
   };
   ```

4. **Extract Pattern Categories**:
   ```bash
   # Search for IR opcodes
   rg "IR_ADD|IR_MUL|IR_LOAD|IR_STORE|opcode.*==" sub_2F9DAC0_0x2f9dac0.c > ir_opcodes.txt

   # Search for PTX instruction strings
   rg '\"add\.|\"mul\.|\"mad\.|\"ld\.|\"st\.' sub_2F9DAC0_0x2f9dac0.c > ptx_instructions.txt

   # Count unique patterns
   rg -o '\"[a-z]+\.[a-z0-9.]+\"' sub_2F9DAC0_0x2f9dac0.c | sort -u | wc -l
   ```

5. **Map IR to PTX**:
   ```bash
   # Create mapping script
   python3 << 'EOF'
   import re

   with open('/tmp/pattern_matcher.c') as f:
       code = f.read()

   # Find case statements mapping IR to PTX
   cases = re.findall(r'case\s+(\w+):\s+.*?\"([^"]+)\"', code, re.DOTALL)

   for ir_op, ptx_inst in cases:
       print(f"{ir_op} -> {ptx_inst}")
   EOF
   ```

6. **Extract SM-Specific Patterns**:
   ```bash
   # Find SM version checks
   rg "sm_version.*>=|if.*sm.*[0-9]{2}" sub_2F9DAC0_0x2f9dac0.c -A 3 > sm_specific.txt

   # Find tensor core patterns
   rg "wmma|mma\.sync|tcgen|tensor" sub_2F9DAC0_0x2f9dac0.c -A 5 > tensor_patterns.txt
   ```

7. **Document Pattern Database**:
   ```json
   {
     "pattern_database": {
       "total_patterns": 1847,
       "hash_table_size": 2048,
       "hash_function": "djb2 variant",
       "pattern_structure": {
         "ir_opcode": "4 bytes",
         "operand_constraints": "16 bytes (4x4)",
         "ptx_template": "8 bytes pointer",
         "cost": "2 bytes",
         "sm_min": "1 byte",
         "flags": "1 byte"
       },
       "pattern_categories": {
         "arithmetic": 287,
         "memory": 412,
         "control_flow": 156,
         "tensor_core": 89,
         "conversion": 203,
         "bitwise": 145,
         "compare": 98,
         "special": 457
       },
       "ir_to_ptx_mappings": [
         {
           "ir_opcode": "IR_ADD_I32",
           "ptx_patterns": [
             {"template": "add.s32 %r{d}, %r{s1}, %r{s2}", "cost": 1, "sm_min": 20},
             {"template": "add.s32 %r{d}, %r{s1}, {imm}", "cost": 1, "sm_min": 20}
           ]
         }
       ],
       "sm_specific_patterns": {
         "sm_70": {"wmma_patterns": 12},
         "sm_80": {"mma_sync_patterns": 24, "async_copy": 8},
         "sm_90": {"warpgroup_mma": 16, "tma": 6},
         "sm_100": {"tcgen05_patterns": 36}
       }
     }
   }
   ```

8. **Extract Sample Patterns**:
   Create table of top 50 most common patterns

---

### **Agent L3-04: Graph Coloring Priority Formula**

**Priority**: CRITICAL
**Estimated Time**: 15 hours
**Deliverable**: `L3/register_allocation/graph_coloring_priority.json`

**INSTRUCTIONS**:

1. **Read Graph Coloring Functions**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled
   cat sub_1081400_0x1081400.c > /tmp/simplify_color.c      # 69 KB
   cat sub_1090BD0_0x1090bd0.c > /tmp/select_node.c         # 61 KB
   cat sub_12E1EF0_0x12e1ef0.c > /tmp/assign_colors.c       # 51 KB
   ```

2. **Find Node Priority Calculation**:
   ```bash
   # Search for priority calculation
   rg "priority|select.*node|choose.*node" sub_1090BD0_0x1090bd0.c -A 10 -B 5 > priority_calc.txt

   # Look for degree-based selection
   rg "degree|neighbor.*count|edge.*count" sub_1090BD0_0x1090bd0.c -A 5 > degree_refs.txt

   # Look for cost integration
   rg "spill.*cost.*priority|priority.*cost" sub_1090BD0_0x1090bd0.c -A 5
   ```

3. **Identify Briggs Criterion**:
   ```bash
   # Briggs uses: neighbors with degree < K
   rg "degree.*<.*K|significant.*neighbor" sub_1081400_0x1081400.c -A 3

   # Look for conservative coalescing check
   rg "conservative|coalesce.*safe|briggs" sub_1081400_0x1081400.c -A 5
   ```

4. **Extract Priority Formula**:
   Expected pattern:
   ```c
   priority = (spill_cost / degree) * weight_factor;
   // OR
   priority = spill_cost - (degree * penalty);
   // OR Briggs-specific
   if (low_degree_neighbors < K) {
       priority = HIGH;
   }
   ```

5. **Find Tie-Breaking Logic**:
   ```bash
   # When priorities equal
   rg "tie.*break|equal.*priority|same.*priority" sub_1090BD0_0x1090bd0.c -A 5
   ```

6. **Document Findings**:
   ```json
   {
     "graph_coloring_priority": {
       "algorithm_variant": "Briggs optimistic coloring",
       "priority_formula": "spill_cost / (degree + 1)",
       "selection_criteria": {
         "primary": "Lowest priority (highest spill_cost/degree ratio)",
         "tie_breaker": "Highest absolute spill cost",
         "briggs_check": "Count neighbors with degree < K",
         "conservative_threshold": "K - 1"
       },
       "degree_weighting": {
         "formula": "effective_degree = actual_degree * coalesce_factor",
         "coalesce_factor": 0.8
       },
       "code_locations": {
         "priority_calc": "sub_1090BD0_0x1090bd0.c:234",
         "selection": "sub_1081400_0x1081400.c:567",
         "briggs_check": "sub_1081400_0x1081400.c:890"
       }
     }
   }
   ```

---

### **Agent L3-05: List Scheduling Heuristics**

**Priority**: HIGH
**Estimated Time**: 30 hours
**Deliverable**: `L3/instruction_scheduling/scheduling_heuristics.json`

**INSTRUCTIONS**:

1. **Find Scheduling Functions**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search for scheduling-related files
   rg -l "schedule|sched" . | head -20 > scheduling_files.txt

   # Read files with "BURR" or "ILP" (scheduling heuristic names)
   rg -l "BURR|ILP|latency.*hiding|register.*reduction" . > heuristic_files.txt
   ```

2. **Identify 9 Scheduling Variants**:
   From L2 analysis, look for:
   - Standard Converging
   - Max ILP
   - Min ILP
   - BURR (Bottom-Up Register Reduction)
   - BURR + Latency
   - BURR + Throughput
   - ILP + Latency
   - ILP + Throughput
   - Balanced

   ```bash
   # Search for variant selection
   rg "sched.*variant|heuristic.*type|schedule.*mode" $(cat heuristic_files.txt) -A 5
   ```

3. **Extract Each Heuristic**:
   ```bash
   # For BURR - Bottom-Up Register Reduction
   rg "BURR|bottom.*up.*reg|register.*reduction" $(cat heuristic_files.txt) -A 10 > burr_heuristic.txt

   # For ILP - Instruction Level Parallelism
   rg "ILP|parallel|instruction.*level" $(cat heuristic_files.txt) -A 10 > ilp_heuristic.txt

   # For Latency hiding
   rg "latency.*hid|hide.*latency" $(cat heuristic_files.txt) -A 10 > latency_hiding.txt
   ```

4. **Document Priority Functions**:
   For each heuristic, find:
   ```c
   int calculate_priority(Instruction *inst) {
       // BURR: minimize register pressure
       return inst->live_range_end - inst->live_range_start;

       // ILP: maximize parallelism
       return inst->successors_count + inst->latency;

       // Latency: hide long operations
       return inst->latency * latency_weight - inst->critical_path_distance;
   }
   ```

5. **Find SM-Specific Scheduling**:
   ```bash
   # SM 90 warpgroup scheduling
   rg "warpgroup|sm_90.*sched|hopper.*schedule" $(cat heuristic_files.txt) -A 10

   # Tensor core scheduling
   rg "tensor.*sched|wmma.*schedule|mma.*schedule" $(cat heuristic_files.txt) -A 10
   ```

6. **Document All 9 Variants**:
   ```json
   {
     "scheduling_heuristics": {
       "pre_ra_scheduling": {
         "variants": [
           {
             "name": "BURR - Bottom-Up Register Reduction",
             "priority_function": "live_range_length",
             "goal": "minimize register pressure",
             "code_location": "scheduling_file.c:123"
           },
           {
             "name": "ILP - Max Instruction Level Parallelism",
             "priority_function": "successors + latency",
             "goal": "maximize parallelism",
             "code_location": "scheduling_file.c:456"
           }
         ]
       },
       "post_ra_scheduling": {
         "variants": [
           {
             "name": "Latency Hiding",
             "priority_function": "latency * weight - critical_distance",
             "goal": "hide memory/compute latency"
           }
         ]
       },
       "sm_specific": {
         "sm_90": {
           "warpgroup_scheduling": "details...",
           "tma_scheduling": "details..."
         }
       }
     }
   }
   ```

---

### **Agent L3-06: IR Node Field Offsets**

**Priority**: HIGH
**Estimated Time**: 20 hours
**Deliverable**: `L3/data_structures/ir_node_exact_layout.json`

**INSTRUCTIONS**:

1. **Find IR Node Access Patterns**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search for functions that create/manipulate IR nodes
   rg "create.*value|new.*value|alloc.*node" . -l | head -10 > ir_manipulation.txt

   # Look for SSA value access
   rg "->opcode|->operand|->type|->use" $(cat ir_manipulation.txt) -A 2 -B 2 > field_access.txt
   ```

2. **Identify Struct Field Accesses**:
   ```bash
   # Find offset-based access (decompiler shows offsets)
   rg "\*\(_QWORD \*\)\(.*\+ [0-9]+\)|\*\(_DWORD \*\)\(.*\+ [0-9]+\)" sub_672A20_0x672a20.c | head -50 > offset_access.txt

   # Example pattern:
   # *(_DWORD *)(node + 0) = opcode;     // offset 0 = opcode
   # *(_QWORD *)(node + 8) = type;       // offset 8 = type pointer
   # *(_DWORD *)(node + 16) = num_ops;   // offset 16 = operand count
   ```

3. **Map All Field Offsets**:
   Create table:
   | Offset | Size | Type | Field Name | Evidence |
   |--------|------|------|------------|----------|
   | 0 | 8 | uint64 | value_id | sub_672A20:123 |
   | 8 | 4 | uint32 | opcode | sub_672A20:125 |
   | 12 | 4 | uint32 | type_id | sub_672A20:127 |

4. **Find Use-Def Chain Structure**:
   ```bash
   # Look for use list manipulation
   rg "use.*list|def.*chain|->next_use|->uses" $(cat ir_manipulation.txt) -A 5 > use_def_chain.txt
   ```

5. **Calculate Total Node Size**:
   ```bash
   # Find allocation size
   rg "malloc\([0-9]+\)|alloc.*56|sizeof.*Value|node.*size" $(cat ir_manipulation.txt) > allocation_size.txt
   ```

6. **Document Exact Layout**:
   ```json
   {
     "ir_value_node_layout": {
       "total_size_bytes": 56,
       "alignment": 8,
       "fields": [
         {"offset": 0, "size": 8, "type": "uint64_t", "name": "value_id", "evidence": "sub_672A20.c:145"},
         {"offset": 8, "size": 4, "type": "uint32_t", "name": "opcode", "evidence": "sub_672A20.c:147"},
         {"offset": 12, "size": 4, "type": "uint32_t", "name": "type_discriminator", "evidence": "sub_672A20.c:149"},
         {"offset": 16, "size": 4, "type": "uint32_t", "name": "num_operands", "evidence": "sub_672A20.c:151"},
         {"offset": 20, "size": 4, "type": "uint32_t", "name": "flags", "evidence": "sub_672A20.c:153"},
         {"offset": 24, "size": 8, "type": "Operand**", "name": "operands_ptr", "evidence": "sub_672A20.c:155"},
         {"offset": 32, "size": 8, "type": "Use*", "name": "use_list_head", "evidence": "sub_672A20.c:157"},
         {"offset": 40, "size": 8, "type": "BasicBlock*", "name": "parent_block", "evidence": "sub_672A20.c:159"},
         {"offset": 48, "size": 8, "type": "Value*", "name": "next_in_block", "evidence": "sub_672A20.c:161"}
       ],
       "validation": {
         "total_computed": 56,
         "matches_agent_9": true,
         "evidence_count": 9
       }
     }
   }
   ```

---

### **Agent L3-07: Lazy Reload Optimization**

**Priority**: HIGH
**Estimated Time**: 15 hours
**Deliverable**: `L3/register_allocation/lazy_reload_algorithm.json`

**INSTRUCTIONS**:

1. **Find Spill Code Generation**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search in register allocation main function
   rg "reload|restore|spill.*load|load.*spill" sub_B612D0_0xb612d0.c -A 10 -B 5 > reload_code.txt

   # Look for lazy/optimized reload
   rg "lazy|defer.*load|optimal.*reload" sub_B612D0_0xb612d0.c -A 10
   ```

2. **Identify Reload Placement Algorithm**:
   ```bash
   # Look for liveness-based placement
   rg "live.*reload|reload.*live|place.*reload" sub_B612D0_0xb612d0.c -A 10 > placement_logic.txt

   # Look for redundant load elimination
   rg "redundant.*load|elim.*reload|duplicate.*load" sub_B612D0_0xb612d0.c -A 10
   ```

3. **Find Reachability Analysis**:
   Expected logic:
   ```c
   // Only reload where value is actually used
   for each use of spilled value:
       if not already_loaded_on_path(use):
           insert_reload_before(use)
   ```

4. **Extract Cost Model**:
   ```bash
   # Find reload cost calculation
   rg "reload.*cost|cost.*reload" sub_B612D0_0xb612d0.c -A 5
   ```

5. **Document Algorithm**:
   ```json
   {
     "lazy_reload_optimization": {
       "algorithm": "On-demand reload with redundancy elimination",
       "phases": [
         {
           "phase": 1,
           "name": "Identify spill locations",
           "action": "Mark where values are spilled to memory"
         },
         {
           "phase": 2,
           "name": "Analyze use points",
           "action": "Find all uses of spilled values"
         },
         {
           "phase": 3,
           "name": "Compute reload points",
           "action": "Place reloads only where needed on execution path"
         },
         {
           "phase": 4,
           "name": "Eliminate redundancy",
           "action": "Remove duplicate reloads on same path"
         }
       ],
       "cost_model": {
         "reload_cost": "memory_latency + register_pressure_penalty",
         "placement_heuristic": "As late as possible but before first use"
       },
       "code_location": "sub_B612D0_0xb612d0.c:2345"
     }
   }
   ```

---

### **Agent L3-08: Phi Insertion Worklist**

**Priority**: HIGH
**Estimated Time**: 10 hours
**Deliverable**: `L3/ssa_construction/phi_insertion_exact.json`

**INSTRUCTIONS**:

1. **Find Phi Insertion Functions**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search for phi-related functions
   rg -l "phi|PHI" . | head -20 > phi_files.txt

   # Look for dominance frontier usage
   rg -l "dominance.*frontier|frontier.*dom|DF\[" . | head -10 >> phi_files.txt
   ```

2. **Identify Worklist Algorithm**:
   ```bash
   # Look for worklist/queue
   rg "worklist|queue|to.*process|pending" $(cat phi_files.txt) -A 10 -B 5 > worklist_code.txt

   # Look for iterative insertion
   rg "while.*worklist|until.*empty|iterate.*phi" $(cat phi_files.txt) -A 10
   ```

3. **Find Termination Condition**:
   ```bash
   # Fixed-point iteration
   rg "fixed.*point|converge|no.*change|stable" $(cat phi_files.txt) -A 5
   ```

4. **Extract Algorithm**:
   Expected pattern:
   ```c
   worklist = {all definitions};
   while (!worklist.empty()) {
       def = worklist.pop();
       for (block in DF[def.block]) {
           if (!has_phi[block][def.var]) {
               insert_phi(block, def.var);
               has_phi[block][def.var] = true;
               if (block has definitions of def.var)
                   worklist.push(block);
           }
       }
   }
   ```

5. **Document Findings**:
   ```json
   {
     "phi_insertion_algorithm": {
       "type": "Iterative worklist with dominance frontier",
       "data_structures": {
         "worklist": "FIFO queue of definition points",
         "has_phi": "2D array [block][variable] tracking phi existence"
       },
       "algorithm_steps": [
         "Initialize worklist with all variable definitions",
         "While worklist non-empty:",
         "  Pop definition",
         "  For each block in dominance frontier:",
         "    If phi not yet inserted:",
         "      Insert phi node",
         "      If block also defines variable, add to worklist"
       ],
       "termination": "Worklist empty (all DF blocks processed)",
       "complexity": "O(N * E) where N=blocks, E=DF edges",
       "code_location": "phi_insertion.c:123"
     }
   }
   ```

---

## HIGH PRIORITY UNKNOWNS (5 agents)

### **Agent L3-09: Pass Ordering and Dependencies**

**Priority**: MEDIUM
**Estimated Time**: 20 hours
**Deliverable**: `L3/optimization_framework/complete_pass_ordering.json`

**INSTRUCTIONS**:

1. **Read PassManager**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled
   cat sub_12D6300_0x12d6300.c > /tmp/pass_manager.c
   ```

2. **Find Pass Registration**:
   ```bash
   # Look for pass registration
   rg "register.*pass|add.*pass|pass.*list" sub_12D6300_0x12d6300.c -A 5 > pass_registration.txt

   # Look for optimization level dispatch
   rg "O0|O1|O2|O3|opt.*level" sub_12D6300_0x12d6300.c -A 10 > opt_level_dispatch.txt
   ```

3. **Extract Pass Execution Order**:
   ```bash
   # Find pass iteration
   rg "for.*pass|run.*pass|execute.*pass" sub_12D6300_0x12d6300.c -A 3 > pass_iteration.txt

   # Look for pass names in order
   rg "SCCP|DCE|LICM|Inline|InstCombine" sub_12D6300_0x12d6300.c | nl > pass_order.txt
   ```

4. **Identify Dependencies**:
   ```bash
   # Look for "requires" or "invalidates"
   rg "require|invalidate|depend|preserve" sub_12D6300_0x12d6300.c -A 5 > dependencies.txt
   ```

5. **Document Complete Ordering**:
   ```json
   {
     "pass_ordering": {
       "O0": ["AlwaysInliner", "NVVMReflect"],
       "O1": ["SimplifyCFG", "SCCP", "DSE", "InstCombine"],
       "O2": ["O1 passes +", "LICM", "GVN", "Inlining", "LoopOptimizations"],
       "O3": ["O2 passes +", "Vectorization", "AggressiveOptimizations"],
       "dependencies": [
         {"pass": "LICM", "requires": ["LoopSimplify", "DominatorTree"]},
         {"pass": "SSAConstruction", "requires": ["DominatorTree", "DominanceFrontier"]},
         {"pass": "RegisterAllocation", "requires": ["SSAElimination"]}
       ],
       "invalidation_rules": {
         "SimplifyCFG": ["invalidates DominatorTree, LoopInfo"],
         "InstCombine": ["preserves CFG, invalidates analysis"]
       }
     }
   }
   ```

---

### **Agent L3-10: Divergence Analysis**

**Priority**: MEDIUM
**Estimated Time**: 15 hours
**Deliverable**: `L3/cuda_specific/divergence_analysis_algorithm.json`

**INSTRUCTIONS**:

1. **Find Divergence Analysis**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search for divergence-related code
   rg -l "diverg|warp.*branch|uniform|non.*uniform" . | head -10 > divergence_files.txt

   # Read ADCE (should integrate with divergence)
   find . -name "*2adce40*" -o -name "*adce*" | head -5
   ```

2. **Identify Algorithm**:
   ```bash
   # Look for divergence propagation
   rg "propagate.*diverg|diverg.*propagate" $(cat divergence_files.txt) -A 10

   # Look for convergence point detection
   rg "converge|sync|join.*point" $(cat divergence_files.txt) -A 10
   ```

3. **Find Integration with ADCE**:
   ```bash
   # How does ADCE use divergence info?
   rg "diverg.*dead|safe.*elim|preserve.*sem" $(cat divergence_files.txt) -A 10
   ```

4. **Document Algorithm**:
   ```json
   {
     "divergence_analysis": {
       "algorithm": "Forward data-flow analysis with sync point detection",
       "phases": [
         "Identify divergent branches (threadIdx, conditional)",
         "Propagate divergence to dependent values",
         "Detect reconvergence points (__syncthreads, block end)",
         "Mark safe-to-eliminate vs must-preserve code"
       ],
       "integration_with_adce": {
         "rule": "Cannot eliminate side-effecting operations in divergent regions",
         "exception": "Can eliminate if all threads execute (uniform control flow)"
       },
       "code_location": "divergence_analysis.c:123"
     }
   }
   ```

---

### **Agent L3-11: Symbol Table Hash Function**

**Priority**: MEDIUM
**Estimated Time**: 12 hours
**Deliverable**: `L3/data_structures/symbol_table_exact.json`

**INSTRUCTIONS**:

1. **Find Symbol Table Functions**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search for symbol lookup
   rg -l "symbol.*lookup|find.*symbol|hash.*symbol" . | head -10 > symbol_files.txt
   ```

2. **Identify Hash Function**:
   ```bash
   # Look for hash calculation
   rg "hash.*31|hash.*33|djb|fnv|sdbm" $(cat symbol_files.txt) -A 10 > hash_function.txt

   # Look for typical hash patterns
   rg "hash.*=.*\*|hash.*\+.*char|hash.*<<|hash.*\^" $(cat symbol_files.txt) -A 5
   ```

3. **Find Bucket Count**:
   ```bash
   # Look for table size
   rg "bucket.*[0-9]+|table.*size.*[0-9]+|capacity.*[0-9]+" $(cat symbol_files.txt)
   ```

4. **Extract Hash Algorithm**:
   Common patterns:
   ```c
   // djb2
   hash = 5381;
   while (c = *str++)
       hash = ((hash << 5) + hash) + c;

   // FNV-1a
   hash = 2166136261;
   for (c in string)
       hash = (hash ^ c) * 16777619;
   ```

5. **Document Complete Structure**:
   ```json
   {
     "symbol_table_structure": {
       "hash_function": "djb2",
       "hash_algorithm": "hash = 5381; while(c) hash = ((hash << 5) + hash) + c",
       "bucket_count": 1024,
       "load_factor": 0.75,
       "collision_resolution": "separate chaining",
       "scope_stack_impl": "linked list of scope objects",
       "code_location": "symbol_table.c:45"
     }
   }
   ```

---

### **Agent L3-12: DSE Partial Overwrite Tracking**

**Priority**: MEDIUM
**Estimated Time**: 15 hours
**Deliverable**: `L3/optimizations/dse_partial_tracking.json`

**INSTRUCTIONS**:

1. **Find DSE Implementation**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search for DSE pass
   rg -l "dead.*store|DSE|store.*elim" . | head -10 > dse_files.txt
   ```

2. **Identify Partial Overwrite Logic**:
   ```bash
   # Look for bit-vector or byte-level tracking
   rg "bit.*vector|byte.*mask|partial.*overwrite" $(cat dse_files.txt) -A 10

   # Look for MemorySSA usage
   rg "MemorySSA|memory.*depend|mem.*def" $(cat dse_files.txt) -A 10
   ```

3. **Find Threshold Parameters**:
   ```bash
   # Look for limits (from L2: dse-memoryssa-partial-store-limit)
   rg "limit.*[0-9]+|threshold.*partial|max.*track" $(cat dse_files.txt)
   ```

4. **Document Algorithm**:
   ```json
   {
     "dse_partial_tracking": {
       "algorithm": "MemorySSA-based with bit-vector tracking",
       "partial_overwrite_detection": {
         "method": "Byte-level bit vector",
         "limit": 100,
         "when_exceeded": "Conservative analysis (assume no dead store)"
       },
       "store_merging": {
         "enabled": true,
         "conditions": "Adjacent stores, same base pointer"
       },
       "code_location": "dse.c:234"
     }
   }
   ```

---

### **Agent L3-13: LICM Versioning**

**Priority**: MEDIUM
**Estimated Time**: 12 hours
**Deliverable**: `L3/optimizations/licm_versioning.json`

**INSTRUCTIONS**:

1. **Find LICM Implementation**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # From L2, LICM is confirmed
   rg -l "LICM|loop.*invariant|hoist" . | head -10 > licm_files.txt
   ```

2. **Find Versioning Logic**:
   ```bash
   # Look for loop versioning
   rg "version.*loop|clone.*loop|duplicate.*loop" $(cat licm_files.txt) -A 10

   # Look for conditional hoisting
   rg "conditional.*hoist|guard.*hoist" $(cat licm_files.txt) -A 10
   ```

3. **Extract Decision Criteria**:
   ```bash
   # When to version vs when not to
   rg "version.*decision|cost.*version|profile" $(cat licm_files.txt) -A 10
   ```

4. **Document Algorithm**:
   ```json
   {
     "licm_versioning": {
       "when_applied": "Conditionally loop-invariant code",
       "versioning_strategy": {
         "create_two_versions": "One with hoisted code, one without",
         "runtime_check": "Insert condition to select version",
         "check_placement": "Before loop entry"
       },
       "cost_model": {
         "version_if": "hoist_benefit > check_overhead * 2",
         "max_versions": 3
       },
       "code_location": "licm.c:567"
     }
   }
   ```

---

### **Agent L3-14: Tensor Core Cost Tables**

**Priority**: MEDIUM
**Estimated Time**: 18 hours
**Deliverable**: `L3/instruction_selection/tensor_core_costs.json`

**INSTRUCTIONS**:

1. **Find Tensor Core Selection**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled

   # Search for tensor/wmma/mma
   rg -l "tensor|wmma|mma\.sync|tcgen" . | head -20 > tensor_files.txt
   ```

2. **Extract Cost Tables**:
   ```bash
   # Look for latency/throughput tables
   rg "latency.*=|throughput.*=|cycles.*=" $(cat tensor_files.txt) -A 5

   # Look for SM-specific costs
   rg "sm_70|sm_80|sm_90|sm_100" $(cat tensor_files.txt) -B 2 -A 5 | grep -E "latency|throughput|cost"
   ```

3. **Find Precision Selection**:
   ```bash
   # Different precisions have different costs
   rg "fp32|fp16|tf32|bf16|fp8|fp4" $(cat tensor_files.txt) -A 5 > precision_costs.txt
   ```

4. **Document All Costs**:
   ```json
   {
     "tensor_core_costs": {
       "sm_70": {
         "wmma_fp16": {"latency": 4, "throughput": 1, "ops_per_inst": 256},
         "wmma_fp32_accum": {"latency": 6, "throughput": 2, "ops_per_inst": 256}
       },
       "sm_80": {
         "mma_fp16": {"latency": 4, "throughput": 1},
         "mma_tf32": {"latency": 4, "throughput": 1},
         "mma_fp64": {"latency": 16, "throughput": 4}
       },
       "sm_90": {
         "warpgroup_mma": {"latency": 3, "throughput": 0.5, "tile": "64x32x32"}
       },
       "sm_100": {
         "tcgen05_variants": 36,
         "fp8": {"latency": 2, "throughput": 0.5},
         "fp4": {"latency": 2, "throughput": 0.25}
       }
     }
   }
   ```

---

## MEDIUM PRIORITY UNKNOWNS (12 agents)

### **Agent L3-15: Bank Conflict Analysis**

**Priority**: MEDIUM
**Estimated Time**: 15 hours
**Deliverable**: `L3/cuda_specific/bank_conflict_analysis.json`

**INSTRUCTIONS**:

1. **Find Bank Conflict Detection**:
   ```bash
   cd /home/grigory/nvopen-tools/cicc/decompiled
   rg -l "bank.*conflict|shared.*memory.*bank|conflict.*avoid" . | head -10 > bank_files.txt
   ```

2. **Extract Detection Algorithm**:
   ```bash
   rg "detect.*conflict|analyze.*bank|stride.*bank" $(cat bank_files.txt) -A 10
   ```

3. **Find Avoidance Strategy**:
   ```bash
   rg "avoid.*conflict|reorder.*access|pad.*shared" $(cat bank_files.txt) -A 10
   ```

4. **Document**:
   ```json
   {
     "bank_conflict_analysis": {
       "detection_algorithm": "Stride analysis on shared memory accesses",
       "banks_per_sm": 32,
       "conflict_conditions": "Multiple threads access same bank, different addresses",
       "avoidance_strategies": [
         "Reorder register allocation to avoid same bank",
         "Insert padding in shared memory layout",
         "Use broadcast for same-address access"
       ]
     }
   }
   ```

---

### **Agents L3-16 through L3-27: Remaining Unknowns**

**Instructions for each follow same pattern**:

1. **Grep for relevant keywords** in decompiled code
2. **Read identified functions** completely
3. **Extract algorithm/data structure/decision logic**
4. **Cross-reference with call graphs** for validation
5. **Document findings in JSON** with code locations
6. **Provide evidence** (line numbers, exact code snippets)

**Agent L3-16**: Pass Function Addresses
- Grep all files for pass names from L2
- Extract function addresses from matches
- Create complete mapping

**Agent L3-17**: Out-of-SSA Elimination
- Search for "ssa", "eliminate", "lower"
- Find copy insertion logic
- Document critical edge handling

**Agent L3-18**: GVN Hash Function
- Search for "GVN", "value.*number", "hash"
- Extract hash function
- Document value equivalence logic

**Agent L3-19**: Scheduling DAG Construction
- Search for "DAG", "dependency", "schedule"
- Extract edge weight computation
- Document DAG traversal

**Agent L3-20**: Loop Detection Algorithm
- Search for "loop.*detect", "natural.*loop", "back.*edge"
- Extract dominator-based detection
- Document nesting calculation

**Agent L3-21**: Critical Path Detection
- Search for "critical.*path", "longest.*path"
- Extract path calculation
- Document weighting formula

**Agent L3-22**: Register Class Constraints
- Search for "register.*class", "constraint", "incompatible"
- Extract constraint tables
- Document SM-specific limits

**Agent L3-23**: TMA Scheduling (SM90)
- Search for "TMA", "tensor.*memory", "hopper"
- Extract scheduling logic
- Document descriptor handling

**Agent L3-24**: Warp Specialization (SM90)
- Search for "warp.*special", "producer.*consumer", "async"
- Extract specialization logic
- Document patterns

**Agent L3-25**: Sparsity Support (SM100)
- Search for "sparse", "2:4", "sparsity"
- Extract pattern detection
- Document instruction selection

**Agent L3-26**: FP4 Format Selection
- Search for "fp4", "block.*scale", "precision"
- Extract format selection
- Document conversion logic

**Agent L3-27**: Pass Manager Implementation
- Search for "ModulePass", "FunctionPass", "LoopPass"
- Extract manager hierarchy
- Document pass infrastructure

---

## EXECUTION STRATEGY

### **Parallel Execution (Recommended)**

**Week 1**: Launch Agents 1-8 (CRITICAL) in parallel
**Week 2**: Launch Agents 9-14 (HIGH) in parallel
**Week 3**: Launch Agents 15-27 (MEDIUM) in parallel

### **Sequential Execution (Alternative)**

Priority order: 1→2→4→3→5→6→7→8→9→(rest in any order)

---

## OUTPUT FORMAT

Each agent must produce a JSON file with this structure:

```json
{
  "metadata": {
    "unknown_id": "1",
    "agent": "L3-01",
    "date": "2025-11-16",
    "confidence": "HIGH",
    "estimated_hours": 20,
    "actual_hours": 18,
    "phase": "L3_EXTRACTION"
  },
  "findings": {
    // Agent-specific findings here
  },
  "evidence": {
    "code_locations": [
      {"file": "sub_B612D0_0xb612d0.c", "line": 1234, "snippet": "exact code"}
    ],
    "cross_references": [
      "Validates Agent 1 hypothesis",
      "Matches foundation analysis 20_REGISTER_ALLOCATION_ALGORITHM.json"
    ]
  },
  "validation": {
    "method": "code analysis + call graph verification",
    "validated": true,
    "remaining_unknowns": ["None" or "List specific gaps"]
  }
}
```

---

## SUCCESS CRITERIA

**Minimum (90% coverage)**:
- [ ] All 8 CRITICAL unknowns extracted
- [ ] Cost models fully documented
- [ ] Register allocation algorithm complete
- [ ] Instruction selection patterns extracted

**Target (95% coverage)**:
- [ ] All CRITICAL + HIGH unknowns extracted
- [ ] Pass ordering complete
- [ ] Data structures fully documented
- [ ] CUDA-specific algorithms extracted

**Stretch (99% coverage)**:
- [ ] All 27 unknowns extracted
- [ ] SM-specific variants documented
- [ ] Complete validation test suite
- [ ] Algorithm reimplementation started

---

## TOOLS REFERENCE

```bash
# File operations
cat FILE                          # Read entire file
less FILE                         # Page through file
head -n 100 FILE                  # First 100 lines
tail -n 100 FILE                  # Last 100 lines

# Search operations
rg "pattern" FILE                 # Fast search
rg -l "pattern" DIR               # List matching files
rg -A 5 -B 5 "pattern" FILE       # Context (5 lines before/after)
grep -r "pattern" DIR             # Recursive search

# JSON operations
cat FILE.json | jq .              # Pretty print
cat FILE.json | jq '.callees'     # Extract field
cat FILE.json | jq -r '.address'  # Extract value

# Analysis helpers
wc -l FILE                        # Count lines
nl FILE                           # Number lines
diff FILE1 FILE2                  # Compare files
sort FILE | uniq -c               # Count unique occurrences
```

---

## FINAL NOTES

1. **All decompiled code is available** - no additional tools needed
2. **Start with CRITICAL unknowns** - highest impact
3. **Use call graphs for validation** - cross-reference findings
4. **Document evidence thoroughly** - code locations, line numbers
5. **Update confidence scores** - HIGH when formula found, MEDIUM when inferred
6. **Cross-reference L2 analysis** - validate against existing findings
7. **Create test cases** - small CUDA kernels to validate hypotheses

**Estimated completion**: 3-6 weeks with dedicated analysis
**Cost**: $0 (all tools free, all data available)
**Blocking**: None - can start immediately

---

## DELIVERABLE SUMMARY

Upon completion, you will have:
- ✅ 27 JSON files documenting all critical unknowns
- ✅ Complete algorithm specifications
- ✅ Exact coefficients and thresholds
- ✅ Data structure layouts
- ✅ SM-specific variants
- ✅ 99% understanding of CICC internals
- ✅ Ready to implement L3 (CICC recreation)

**This is the complete knowledge extraction phase - everything needed to recreate CICC!**
