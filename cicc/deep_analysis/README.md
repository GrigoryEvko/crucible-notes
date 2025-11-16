# CICC Deep Analysis (L2 Phase)

**Purpose**: Reverse engineering deep dive into CICC's internal algorithms, data structures, and runtime behavior.

**Phase**: L2 - Deep Inspection (follows L0/L1 foundation analysis)

---

## Directory Structure

### `/algorithms/`
**Purpose**: Reverse engineered algorithm implementations

**Contents**:
- `register_allocation.json` - Graph coloring algorithm details
- `optimization_passes/` - Individual optimization pass algorithms (94 passes)
- `instruction_selection.json` - Instruction selection heuristics
- `liveness_analysis.json` - Variable liveness computation
- `loop_analysis.json` - Loop detection and transformation algorithms

**Agent Focus**:
- Identify exact algorithms (graph coloring variant, SSA construction method)
- Map functions to algorithm steps
- Document complexity and data flow

---

### `/data_structures/`
**Purpose**: Internal data structure layouts reconstructed from binary

**Contents**:
- `ir_format.json` - Internal IR structure (SSA form)
- `symbol_table.json` - Symbol table organization
- `cfg_representation.json` - Control flow graph structure
- `interference_graph.json` - Register allocator data structures
- `type_system.json` - CUDA type representation

**Agent Focus**:
- Reverse engineer struct layouts
- Identify pointer relationships
- Document size, alignment, padding

---

### `/execution_traces/`
**Purpose**: Dynamic analysis and execution tracing results

**Contents**:
- `trace_sm_*.json` - Execution traces for different SM versions
- `memory_snapshots/` - Heap/stack snapshots during compilation
- `function_call_sequences.json` - Observed call patterns
- `optimization_decisions.json` - Which optimizations fired and why

**Agent Focus**:
- Use GDB/Frida/PIN to trace execution
- Capture decision points
- Map runtime behavior to static analysis

---

### `/symbol_recovery/`
**Purpose**: Recover function names from stripped binary

**Contents**:
- `recovered_functions.json` - Functions with recovered names
- `confidence_scores.json` - Confidence in each name (HIGH/MEDIUM/LOW)
- `name_mappings.csv` - Address → Name mappings
- `signature_database.json` - Known function signatures

**Agent Focus**:
- String reference analysis
- Call pattern matching
- Signature matching against known compilers (LLVM, GCC)
- Cross-reference with NVIDIA patents

---

### `/protocols/`
**Purpose**: Binary formats, ABIs, communication protocols

**Contents**:
- `ptx_abi.json` - PTX ABI implementation details
- `internal_formats.json` - Serialization formats for IR
- `metadata_format.json` - Debug/metadata encoding
- `plugin_interface.json` - Plugin/extension interfaces (if any)

**Agent Focus**:
- Document binary formats
- Identify versioning schemes
- Map ABI compatibility rules

---

### `/findings/`
**Purpose**: Consolidated reports and major discoveries

**Contents**:
- `MASTER_FINDINGS.md` - Top-level summary of all discoveries
- `algorithm_discoveries.json` - Key algorithm identifications
- `critical_insights.md` - Major breakthroughs
- `hypothesis_validation.json` - Tested and confirmed hypotheses
- `unknowns_remaining.json` - What we still don't know

**Agent Focus**:
- Aggregate discoveries from all agents
- Document confidence levels
- Prioritize remaining unknowns

---

### `/validation/`
**Purpose**: Verification of reverse engineering hypotheses

**Contents**:
- `algorithm_tests/` - Test cases to validate algorithm identification
- `structure_validation/` - Tests for data structure layouts
- `behavior_tests/` - Compare CICC behavior to RE models
- `regression_tests/` - Ensure RE understanding is correct

**Agent Focus**:
- Create test cases for each hypothesis
- Validate against real CICC behavior
- Document discrepancies

---

## Agent Output Guidelines

### File Naming Convention
```
<category>_<module>_<topic>.json
```

Examples:
- `algorithm_register_allocation_graph_coloring.json`
- `data_structure_optimization_framework_pass_manager.json`
- `trace_compilation_pipeline_sm90_tensor.json`

### JSON Structure Template
```json
{
  "metadata": {
    "phase": "L2",
    "agent": "agent-name",
    "date": "2025-11-16",
    "confidence": "HIGH|MEDIUM|LOW",
    "status": "CONFIRMED|HYPOTHESIS|SPECULATION"
  },
  "discovery": {
    "summary": "One sentence description",
    "details": "Detailed explanation",
    "evidence": ["Evidence source 1", "Evidence source 2"]
  },
  "functions": {
    "0xADDRESS": {
      "name": "proposed_function_name",
      "purpose": "what it does",
      "algorithm_step": "which step in algorithm"
    }
  },
  "validation": {
    "test_case": "path/to/test",
    "verified": true,
    "discrepancies": []
  }
}
```

---

## Workflow

### Phase Progression
1. **L0 (DONE)**: Foundation - Static analysis, callgraph extraction
2. **L1 (DONE)**: Programmatic analysis - 26 consolidated reports
3. **L2 (CURRENT)**: Deep inspection - Algorithm/structure RE
4. **L3 (FUTURE)**: Implementation - Recreate CICC components

### L2 Priorities (from `foundation/analyses/13_REVERSE_ENGINEERING_ROADMAP.json`)
1. **Pipeline Understanding** (320 hours)
   - Map compilation stages
   - Identify phase transitions
   - Output → `findings/pipeline_architecture.json`

2. **Algorithm Identification** (280 hours)
   - Register allocation algorithm
   - 82 unknown optimization passes
   - Output → `algorithms/`

3. **Data Structure Analysis** (240 hours)
   - IR format reconstruction
   - Symbol table layout
   - Output → `data_structures/`

4. **Symbol Recovery** (200 hours)
   - Top 500 critical functions
   - Output → `symbol_recovery/`

---

## Cross-References

**Foundation (L1) → Deep Analysis (L2) Mapping**:
- `foundation/analyses/02_MODULE_ANALYSIS.json` → `algorithms/` (identify algorithms per module)
- `foundation/analyses/06_CRITICAL_FUNCTIONS_CORRECTED.json` → `symbol_recovery/` (name top functions)
- `foundation/analyses/20_REGISTER_ALLOCATION_ALGORITHM.json` → `algorithms/register_allocation.json`
- `foundation/analyses/09_PATTERN_DISCOVERY.json` → `data_structures/` (structure hints)
- `foundation/analyses/14_KNOWLEDGE_GAPS.json` → `findings/unknowns_remaining.json`

---

## Agent Responsibilities (20 Agents)

When launching L2 agents, assign clear responsibilities:

**Algorithm Team (8 agents)**:
1. Register allocation algorithm
2. SSA construction/destruction
3. Loop optimization passes
4. Instruction selection
5. Peephole optimization
6. Dead code elimination
7. Constant propagation
8. Code motion algorithms

**Data Structure Team (4 agents)**:
9. IR format reconstruction
10. Symbol table layout
11. CFG representation
12. Type system

**Dynamic Analysis Team (4 agents)**:
13. Execution tracing (sm_70/sm_80)
14. Execution tracing (sm_90/sm_100)
15. Memory profiling
16. Decision point capture

**Symbol Recovery Team (3 agents)**:
17. Top 200 critical functions
18. Optimization framework functions
19. Register allocation functions

**Synthesis Agent (1 agent)**:
20. Consolidate all findings → `findings/MASTER_FINDINGS.md`

---

## Success Criteria

L2 phase is complete when:
- [ ] Register allocation algorithm identified (HIGH confidence)
- [ ] 50+ of 82 unknown optimization passes located
- [ ] IR format documented (struct layout, field purposes)
- [ ] Top 200 critical functions named
- [ ] 10+ execution traces captured
- [ ] MASTER_FINDINGS.md documents major breakthroughs
- [ ] All findings have confidence scores and validation tests

---

## Notes

- **Keep foundation/ intact** - L1 is the baseline, don't modify
- **All L2 findings go in deep_analysis/** - Clean separation
- **Cross-reference L1** - Link back to foundation analyses
- **Document confidence** - Every claim needs evidence level
- **Validate hypotheses** - Create tests in `validation/`
