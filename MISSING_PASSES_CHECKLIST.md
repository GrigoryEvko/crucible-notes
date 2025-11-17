# Missing CICC Optimization Passes - Documentation Checklist

**Total**: 34 passes
**Last Updated**: 2025-11-17

---

## CRITICAL Priority (7 passes) - IMMEDIATE ACTION

- [ ] **NVVMIntrRange** - NVVM intrinsic range optimization
- [ ] **NVPTXSetGlobalArrayAlignment** - Global memory array alignment
- [ ] **NVPTXSetLocalArrayAlignment** - Local memory array alignment
- [ ] **NVPTXImageOptimizer** - Texture/surface memory optimization
- [ ] **RegisterUsageInformationCollector** - Collect register usage data
- [ ] **RegisterUsageInformationPropagation** - Propagate register usage info
- [ ] **RegisterUsageInformationStorage** - Store register usage information

**All are NVIDIA-proprietary GPU optimizations**

---

## HIGH Priority (11 passes)

### LLVM Standard (8 passes)
- [ ] **BitTrackingDeadCodeElimination (BDCE)** - Bit-tracking dead code elimination
- [ ] **LoopUnrollAndJam** - Combined loop unrolling and fusion
- [ ] **LoopIdiomVectorize** - Vectorization of loop idioms
- [ ] **LoopSimplifyCFG** - CFG simplification within loops
- [ ] **SLPVectorizer** - Superword-Level Parallelism vectorizer
- [ ] **NewGVN** - Next-generation Global Value Numbering
- [ ] **GVNHoist** - Hoist redundant computations using GVN
- [ ] **AtomicExpand** - Atomic operation expansion

### NVIDIA-Specific (3 passes)
- [ ] **NVPTXCopyByValArgs** - Function argument passing optimization
- [ ] **NVPTXCtorDtorLowering** - Constructor/destructor lowering
- [ ] **NVPTXLowerArgs** - Argument lowering pass

---

## MEDIUM Priority (9 passes)

### Analysis Passes (3 passes)
- [ ] **AAManager** - Alias Analysis manager
- [ ] **RegisterPressureAnalysis** - Register pressure tracking
- [ ] **PhysicalRegisterUsageAnalysis** - Physical register usage analysis

### Attributor Framework (4 passes)
- [ ] **AttributorPass** - Main attributor framework
- [ ] **AttributorLightPass** - Lightweight attributor variant
- [ ] **AttributorCGSCCPass** - Call graph SCC attributor
- [ ] **AttributorLightCGSCCPass** - Lightweight CGSCC variant

### Code Gen & PGO (2 passes)
- [ ] **BypassSlowDivision** - Division bypass optimization
- [ ] **PGOForceFunctionAttrs** - Profile-guided function attributes

---

## LOW Priority (7 passes)

### Sanitizers (2 passes)
- [ ] **AddressSanitizer** - Address sanitizer instrumentation
- [ ] **BoundsChecking** - Bounds checking instrumentation

### Other Transformations (5 passes)
- [ ] **CFGuard** - Control Flow Guard (Windows)
- [ ] **CGProfile** - Call graph profiling
- [ ] **CanonicalizeAliases** - Alias canonicalization
- [ ] **CanonicalizeFreezeInLoops** - Freeze canonicalization in loops
- [ ] **OpenMPOptCGSCCPass** - OpenMP optimizations

---

## Agent Assignment Plan

### Phase 1: CRITICAL (Weeks 1-2)
- **Agent 1**: RegisterUsageInformation* (3 passes)
- **Agent 2**: NVPTX alignment & image (6 passes including 3 from HIGH)
- **Agent 3**: NVVMIntrRange (1 pass)

### Phase 2: HIGH (Weeks 3-4)
- **Agent 4**: Loop optimizations (3 passes)
- **Agent 5**: Vectorization & value numbering (3 passes)
- **Agent 7**: BDCE, AtomicExpand (2 passes)

### Phase 3: MEDIUM/LOW (Weeks 5-6)
- **Agent 6**: Attributor & Analysis (7 passes)
- **Agent 7**: Code Gen & Sanitizers (3 remaining passes)
- **Agent 8**: Other Transformations (6 passes)

---

## Files Available

1. **Full Report**: `/home/user/nvopen-tools/CICC_OPTIMIZATION_PASS_GAP_ANALYSIS.md`
2. **JSON Data**: `/home/user/nvopen-tools/gap_analysis_comprehensive_report.json`
3. **Summary**: `/home/user/nvopen-tools/gap_analysis_summary.txt`
4. **This Checklist**: `/home/user/nvopen-tools/MISSING_PASSES_CHECKLIST.md`

---

## Progress Tracking

- **Documentation Coverage**: 63.8% (60/94 passes)
- **Remaining**: 34 passes
- **Target**: 100% coverage
- **Timeline**: 6 weeks with 8 agents
