# CICC Execution Tracing Methodology - L2 Agent 13

**Date**: 2025-11-16
**Agent**: agent_13 (Dynamic Analysis Team)
**Status**: DOCUMENTED_METHODOLOGY
**Confidence**: MEDIUM (requires hands-on execution to achieve HIGH)

---

## Executive Summary

This document provides a comprehensive methodology for dynamic analysis of the CICC compiler through execution tracing. Due to the binary being **stripped** (no debug symbols), this approach combines:

1. **System call tracing** (strace/ltrace) for I/O and library patterns
2. **Address-based breakpoints** (GDB with known addresses)
3. **Differential analysis** (comparing sm_70 vs sm_80 compilation outputs)
4. **Static binary analysis** (objdump, strings for pattern identification)

**Timeline**: Full execution traces can be captured in 50-100 hours with proper setup.

---

## Prerequisites

### System Requirements
- **OS**: Linux x86_64
- **Memory**: 32GB RAM (compilation may allocate 100-200MB)
- **Disk**: 100GB free (for intermediate files, traces, analysis)

### Software Requirements
```bash
# Required
gdb >= 12.0                          # Debugger for breakpoint-based tracing
strace                               # System call tracer
ltrace                               # Library call tracer
objdump                              # Binary disassembly
strings                              # String extraction
readelf                              # ELF section analysis

# CUDA Toolkit (version-specific)
nvcc (CUDA 11.x or earlier)          # For sm_70 support
nvcc >= 12.0                         # For sm_80+ support

# Optional but useful
perf                                 # Performance profiling
graphviz                             # Graph visualization
python3 + gdb-python                 # Custom GDB scripts
```

### Build Requirements
- CICC source code (to rebuild with -g debug symbols)
- CUDA development headers
- CMake or Make build system

---

## Phase 1: Environment Setup (8-10 hours)

### 1.1 Verify Tools
```bash
# Check all tracing tools available
which gdb strace ltrace objdump strings readelf
gdb --version  # Should be 12.0+
strace --version
```

### 1.2 Prepare Test Kernels

Create standardized test cases:

**simple_kernel.cu** - Minimal compilation path
```cuda
__global__ void vectorAdd(const float *A, const float *B, float *C, int n) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx < n) C[idx] = A[idx] + B[idx];
}
```

**tensor_kernel.cu** - Tests tensor core code generation
```cuda
// Matrix multiply using WMMA (triggers tensor core paths)
__global__ void matmul(float *A, float *B, float *C, int n) {
    // Warp matrix multiply-accumulate operations
    // Expected to trigger wmma instruction selection
}
```

**register_heavy.cu** - Tests register allocation with spilling
```cuda
__global__ void heavyRegs(float *data, int n) {
    // Uses 100+ registers intentionally
    // Will trigger spilling analysis
}
```

### 1.3 Compile Test Kernels

For **sm_70** (requires CUDA 11.x):
```bash
nvcc -ptx -arch=sm_70 -gencode=arch=compute_70,code=sm_70 \
     simple_kernel.cu -o simple_sm70.ptx
```

For **sm_80** (CUDA 12.0+):
```bash
nvcc -ptx -arch=sm_80 -gencode=arch=compute_80,code=sm_80 \
     simple_kernel.cu -o simple_sm80.ptx
```

### 1.4 Rebuild CICC with Debug Symbols (Optional but Recommended)

If source code available:
```bash
cmake -DCMAKE_BUILD_TYPE=Debug -g
make cicc
# Result: cicc binary with debug symbols
```

---

## Phase 2: System Call Tracing (4-6 hours)

### 2.1 Basic System Call Trace

Trace all syscalls during compilation:
```bash
strace -f -o cicc_trace.txt \
       /path/to/cicc input.i -arch=sm_70 -o output.ptx
```

**Filtered tracing** (more useful):
```bash
strace -f \
       -e trace=open,openat,read,write,mmap,mprotect,brk,madvise \
       -o cicc_filtered.txt \
       /path/to/cicc input.i -arch=sm_70 -o output.ptx
```

### 2.2 Analyze System Call Output

Key patterns to extract:
- **File I/O**: How often are reads/writes occurring?
- **Memory allocation**: mmap/brk patterns indicate allocation phases
- **Library loading**: Which libraries are loaded?
- **System call timing**: Relative ordering shows phase sequence

Example analysis script:
```python
import re
with open('cicc_filtered.txt') as f:
    for line in f:
        if 'mmap' in line:
            print(f"Memory allocation: {line}")
        elif 'open.*input' in line:
            print(f"Input file access: {line}")
        elif 'write.*output' in line:
            print(f"Output file write: {line}")
```

### 2.3 Library Call Tracing

```bash
ltrace -e '@*' -o cicc_ltrace.txt \
       /path/to/cicc input.i -arch=sm_70 -o output.ptx
```

Look for patterns:
- Function calls to memory allocators (malloc, calloc, realloc)
- Library function calls (can hint at algorithm usage)
- External dependency calls

---

## Phase 3: GDB-Based Breakpoint Tracing (20-30 hours)

Since the binary is stripped, use address-based breakpoints and disassembly.

### 3.1 Find Suspected Entry Points

Use static analysis to find entry point:
```bash
objdump -d /path/to/cicc | grep -i "^[0-9a-f]* <main>:" -A 20
```

### 3.2 Set Initial Breakpoint

```bash
gdb /path/to/cicc
(gdb) break main
(gdb) run input.i -arch=sm_70 -o output.ptx
(gdb) info registers  # Examine initial state
(gdb) backtrace       # View call stack
```

### 3.3 Trace Execution Phases

#### Phase 1: Initialization
```bash
(gdb) next           # Step through main function
(gdb) step           # Step into function calls
(gdb) info frame     # Current stack frame info
(gdb) print $rsp     # Stack pointer (may reveal allocations)
```

#### Phase 2: Input Parsing
Focus on functions that read input:
```bash
# Find likely parsing functions
objdump -t /path/to/cicc | grep -i 'parse\|read\|input'

# Set breakpoint at suspected parser
(gdb) break *0x<address>
(gdb) commands
# Add custom commands to print interesting data
end
(gdb) continue
```

#### Phase 3-7: Optimization Pipeline
Document each pass by setting breakpoints at suspected pass boundaries:
```bash
# Look for function names hinting at optimization
objdump -T /path/to/cicc | grep -i 'pass\|optimize'

# For each pass, set breakpoint
(gdb) break *0x<pass_address>
(gdb) continue
(gdb) backtrace  # See who called this pass
(gdb) info frame # Memory state
```

#### Phase 8: PTX Emission
Find PTX generation function:
```bash
objdump -t /path/to/cicc | grep -i 'ptx\|emit\|output'

(gdb) break *0x<ptx_emit_address>
(gdb) continue
(gdb) x/s $rsi  # If PTX string is in RSI register
```

### 3.4 Memory Inspection During Execution

At key breakpoints, dump memory regions:
```bash
(gdb) break *0x<address>
(gdb) commands
  silent
  dump binary memory /tmp/heap_dump.bin 0x<start> 0x<end>
  continue
end
```

Parse the memory dump:
```python
with open('/tmp/heap_dump.bin', 'rb') as f:
    data = f.read()
    # Analyze binary structure
    # Look for recognizable patterns (IR nodes, pass data, etc.)
```

---

## Phase 4: Differential Analysis (12-16 hours)

### 4.1 Compile Same Kernel for Multiple SM Versions

```bash
# sm_70 trace
strace -o trace_sm70.txt cicc input.i -arch=sm_70 -o sm70.ptx

# sm_80 trace
strace -o trace_sm80.txt cicc input.i -arch=sm_80 -o sm80.ptx

# sm_90 trace (if supported)
strace -o trace_sm90.txt cicc input.i -arch=sm_90 -o sm90.ptx
```

### 4.2 Identify Divergence Points

```python
import difflib

with open('trace_sm70.txt') as f:
    trace_sm70 = f.readlines()
with open('trace_sm80.txt') as f:
    trace_sm80 = f.readlines()

# Find differences
diff = list(difflib.unified_diff(trace_sm70, trace_sm80, lineterm=''))
for line in diff:
    if line.startswith('+') or line.startswith('-'):
        print(line)
```

### 4.3 Analyze PTX Output Differences

Compare generated PTX files:
```bash
diff -u sm70.ptx sm80.ptx | head -50
```

Expected differences:
- `.version 6.0` vs `.version 7.0`
- `.target sm_70` vs `.target sm_80`
- Instruction variants (tf32, ldmatrix, etc. on sm_80)
- Register count and allocation patterns

---

## Phase 5: Advanced Tracing with Custom Scripts (10-15 hours)

### 5.1 GDB Python Scripting

Create custom GDB commands to extract internal structures:

```python
import gdb

class ParseIRCommand(gdb.Command):
    """Parse IR module from memory at current breakpoint"""

    def invoke(self, arg, from_tty):
        # Get frame information
        frame = gdb.selected_frame()

        # Extract suspected IR module pointer from registers
        # This requires knowing the calling convention and ABI
        rsi = frame.read_register("rsi")

        # Parse suspected IR structure
        # Requires knowledge of internal struct layout
        ir_module = self.parse_ir_at_address(int(rsi))

        gdb.write(f"IR Module: {ir_module}\n")

    def parse_ir_at_address(self, addr):
        # TODO: Implement IR parsing
        pass

ParseIRCommand("parse_ir", gdb.COMMAND_DATA)
```

### 5.2 Automated Trace Collection

Script to collect comprehensive traces:

```bash
#!/bin/bash

KERNELS=("simple_kernel.cu" "tensor_kernel.cu" "register_heavy.cu")
TARGETS=("sm_70" "sm_80" "sm_90")

for kernel in "${KERNELS[@]}"; do
    for target in "${TARGETS[@]}"; do
        echo "Tracing $kernel for $target..."

        INPUT="${kernel%.cu}.i"
        OUTPUT="${kernel%.cu}_${target}.ptx"
        TRACE="${kernel%.cu}_${target}_trace.txt"

        strace -f -e trace=open,openat,read,write,mmap,madvise \
               -o "$TRACE" \
               /path/to/cicc "$INPUT" -arch="$target" -o "$OUTPUT"

        echo "Completed: $TRACE"
    done
done
```

---

## Phase 6: Data Analysis and Documentation (8-12 hours)

### 6.1 Extract Key Metrics

From trace files, compute:
- **Compilation time**: Total elapsed time
- **Phase duration**: Time spent in each phase
- **Memory peak**: Maximum memory usage
- **File I/O count**: Number of read/write operations
- **Library calls**: Function call frequencies

### 6.2 Create Execution Flow Diagram

Use call graph from GDB to create flow visualization:

```bash
# Generate call graph
gdb --batch -ex "run" -ex "bt" /path/to/cicc > callgraph.txt

# Format as DOT graph
# (Requires manual processing or script)
```

### 6.3 Document Findings

Create JSON output files in the format:

```json
{
  "metadata": {
    "phase": "L2_DEEP_ANALYSIS",
    "agent": "agent_13",
    "tracing_method": "strace+gdb",
    "kernel": "simple_vector_addition",
    "target_sm": "sm_70"
  },
  "compilation_phases": [
    {
      "phase": "initialization",
      "duration_ms": 15,
      "syscalls": ["brk", "mmap", "openat"],
      "key_actions": ["Parse arguments", "Load libraries"]
    },
    ...
  ],
  "total_compilation_time_ms": 1200,
  "peak_memory_mb": 150
}
```

---

## Expected Discoveries

### HIGH Confidence (Can be confirmed through tracing)
1. **Compilation pipeline sequence**: Exact phase order
2. **System call patterns**: Memory allocation strategies
3. **File I/O behavior**: Input/output operations
4. **SM version detection**: How `-arch=sm_XX` influences execution
5. **PTX output format**: Instruction emission order

### MEDIUM Confidence (Requires GDB + source knowledge)
1. **Pass execution order**: Optimization pass sequence
2. **Register allocation algorithm**: Chaitin vs Briggs
3. **Cost model evaluation**: How optimization decisions are made
4. **SM-specific optimizations**: Register limits, instruction selection
5. **Tensor core code generation**: wmma instruction insertion

### LOW Confidence (May require reverse engineering)
1. **Exact heuristic thresholds**: Loop unroll limits, spill thresholds
2. **Internal data structure layouts**: IR format, graph structures
3. **Algorithm implementation details**: Specific optimization algorithms
4. **Undocumented compiler features**: Hidden compiler flags/behaviors

---

## Challenges and Workarounds

### Challenge 1: Binary is Stripped
**Problem**: No symbols to set breakpoints by name
**Workaround**:
- Use address-based breakpoints
- Use binary analysis (objdump, strings) to find entry points
- Rebuild from source with `-g` if possible

### Challenge 2: Optimized Compilation
**Problem**: Compiler optimizations obscure original logic
**Workaround**:
- Rebuild CICC with `-O0` (no optimization)
- Use disassembly analysis
- Focus on functional behavior rather than implementation details

### Challenge 3: Large Data Structures
**Problem**: IR and optimization data structures are huge
**Workaround**:
- Use minimal test kernels
- Focus on specific fields of interest
- Parse memory dumps to extract relevant data

### Challenge 4: Non-Deterministic Behavior
**Problem**: Memory addresses vary between runs
**Workaround**:
- Document relative offsets
- Use pattern matching instead of absolute addresses
- Collect multiple traces to identify stable patterns

### Challenge 5: CUDA Version Compatibility
**Problem**: CUDA 13.0 doesn't support sm_70; earlier versions don't support sm_100
**Workaround**:
- Use CUDA 11.x for sm_70 tracing
- Use CUDA 12.x for sm_80 tracing
- Use CUDA 13.0+ for sm_90+ tracing
- May need multiple CUDA installations

---

## Success Criteria

### For Each SM Target (sm_70, sm_80, sm_90):

- [ ] Documented all 9 compilation phases with durations
- [ ] Identified function addresses for major entry points
- [ ] Captured system call sequence
- [ ] Extracted peak memory usage
- [ ] Generated sample PTX output
- [ ] Documented SM-specific code paths

### For Comparative Analysis (sm_70 vs sm_80):

- [ ] Identified divergence points in execution
- [ ] Documented new optimization passes (if any)
- [ ] Compared instruction selection differences
- [ ] Analyzed PTX output differences
- [ ] Confirmed SM version detection logic

### For Algorithm Identification:

- [ ] Register allocation algorithm classified (90%+ confidence)
- [ ] Pass execution order documented (95%+ confidence)
- [ ] Cost model behavior documented (70%+ confidence)
- [ ] SM-specific optimizations identified (80%+ confidence)

---

## Time Estimates

| Phase | Hours | Notes |
|-------|-------|-------|
| Setup | 10 | Tools, environment, kernels |
| System call tracing | 6 | strace analysis |
| GDB breakpoint tracing | 30 | Address-based, single-step |
| Differential analysis | 16 | sm_70 vs sm_80 vs sm_90 |
| Advanced scripting | 12 | Custom GDB commands |
| Data analysis | 10 | Extract metrics, create visualizations |
| Documentation | 10 | Consolidate findings |
| **Total** | **94** | ~2.5 working weeks full-time |

---

## Recommended Execution Plan

1. **Week 1**: Setup environment, collect basic strace traces
2. **Week 2**: GDB-based breakpoint tracing for sm_70
3. **Week 3**: GDB-based breakpoint tracing for sm_80
4. **Week 4**: Differential analysis, custom scripting, documentation

---

## Next Steps

After completing execution traces:

1. **Validate discoveries** against expected LLVM compiler behavior
2. **Cross-reference** with patent documents and NVIDIA documentation
3. **Implement test cases** to verify algorithm hypotheses
4. **Create modification toolkit** based on discovered structures
5. **Document undiscovered areas** for future agents

---

## Resources

### External References
- [GDB Manual](https://sourceware.org/gdb/documentation/)
- [strace Documentation](https://strace.io/)
- [LLVM Compiler Infrastructure](https://llvm.org/)
- [NVIDIA PTX ISA Documentation](https://docs.nvidia.com/cuda/parallel-thread-execution/)
- [CUDA Architecture Documentation](https://docs.nvidia.com/cuda/cuda-c-programming-guide/)

### Related L2 Analysis Documents
- `algorithms/register_allocation.json` - Register allocation algorithm details
- `algorithms/optimization_passes/` - Individual pass documentation
- `data_structures/ir_format.json` - IR format reconstruction
- `findings/MASTER_FINDINGS.md` - Consolidated L2 discoveries

---

**Document Status**: Complete methodology documented
**Execution Status**: Ready for hands-on tracing
**Next Agent**: Agent 14 (sm_90/sm_100 tracing) can follow same methodology
