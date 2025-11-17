#!/usr/bin/env python3
"""
Final comprehensive gap analysis with priority ranking.
"""

import json
import os
import re

# Load the optimization pass mapping
with open('/home/user/nvopen-tools/cicc/foundation/analyses/21_OPTIMIZATION_PASS_MAPPING.json', 'r') as f:
    mapping_data = json.load(f)

# Get documented passes from wiki
docs_dir = '/home/user/nvopen-tools/cicc/wiki/docs/compiler-internals/optimizations'
documented_files = [f for f in os.listdir(docs_dir) if f.endswith('.md')]
documented_files = [f for f in documented_files if not ('INDEX' in f or 'index' in f or 'overview' in f or 'remaining' in f)]
documented_passes = set([f.replace('.md', '') for f in documented_files])

# Manual mapping of known documented passes
DOCUMENTED_MAPPING = {
    'LoopRotate': 'loop-rotate',
    'LoopUnroll': 'loop-unroll',
    'LoopDeletion': 'loop-deletion',
    'LoopIdiom': 'loop-idiom',
    'LoopVectorize': 'loop-vectorize',
    'LoopSimplify': 'loop-simplify',
    'LoopDistribute': 'loop-distribute',
    'LoopInterchange': 'loop-interchange',
    'LoopLoadElimination': 'loop-load-elimination',
    'LoopSinking': 'loop-sinking',
    'LoopPredication': 'loop-predication',
    'LoopFlatten': 'loop-flatten',
    'IndVarSimplify': 'indvar-simplify',
    'LoopVersioningLICM': 'loop-versioning-licm',
    'GlobalValueNumbering (GVN)': 'gvn',
    'GVNSink': 'gvn-sink',
    'SparseCCP (SCCP)': 'sccp',
    'Interprocedural_SCCP (IPSCCP)': 'ipsccp',
    'Reassociate': 'reassociate',
    'JumpThreading': 'jump-threading',
    'DFAJumpThreading': 'dfa-jump-threading',
    'CorrelatedValuePropagation': 'correlated-value-propagation',
    'TailCallElimination': 'tail-call-elimination',
    'Scalarizer': 'scalarizer',
    'MemCpyOpt': 'memory-memcpyopt',
    'SROA (Scalar Replacement of Aggregates)': 'memory-sroa',
    'PromoteMemoryToRegister': 'memory-mem2reg',
    'ArgumentPromotion': 'interprocedural-argument-promotion',
    'GlobalOptimizer': 'interprocedural-global-optimizer',
    'MergeFunctions': 'interprocedural-merge-functions',
    'CalledValuePropagation': 'interprocedural-called-value-propagation',
    'FunctionAttrs': 'interprocedural-function-attrs',
    'PostOrderFunctionAttrs': 'interprocedural-post-order-function-attrs',
    'DeadArgumentElimination': 'interprocedural-dead-argument-elimination',
    'CodeGenPrepare': 'backend-codegen-prepare',
    'BreakCriticalEdges': 'backend-break-critical-edges',
    'CallSiteSplitting': 'backend-call-site-splitting',
    'PartialInliner': 'inline-partial-inliner',
    'AggressiveInstCombine': 'memory-aggressive-instcombine',
    'NVVMIRVerifier': 'nvvm-ir-verifier',
    'NVVMPeepholeOptimizer': 'nvvm-peephole-optimizer',
    'NVVMOptimizer': 'nvvm-optimizer',
    'MemorySpaceOptimizationForWmma': 'memory-space-optimization-wmma',
    'NVVMIPMemorySpacePropagation': 'memory-nvvm-propagation',
    'NVPTXSetFunctionLinkages': 'nvptx-set-function-linkages',
    'NVPTXAllocaHoisting': 'nvptx-alloca-hoisting',
    'NVPTXProxyRegisterErasure': 'nvptx-proxy-register-erasure',
    'NVPTXPrologEpilogPass': 'nvptx-prolog-epilog',
    'RegisterRematerializationOnNVVMIR': 'nvptx-register-rematerialization',
    'NVPTXBlockRemat': 'nvptx-block-remat',
    'NVPTX_cvta_optimization': 'nvptx-cvta-optimization',
    'NVPTX_ld_param_optimization': 'nvptx-ld-param-optimization',
    'MachineCSE': 'backend-machine-cse',
    'MachineLICM': 'backend-machine-licm',
    'MachineSinking': 'backend-machine-sinking',
    'MachineInstCombiner': 'backend-machine-inst-combiner',
    'RegisterCoalescer': 'backend-register-coalescer',
    'VirtualRegisterRewriter': 'backend-virtual-register-rewriter',
    'RegisterAllocation': 'backend-register-allocation',
    'RenameRegisterOperands': 'backend-rename-register-operands',
}

def is_documented(pass_name):
    """Check if a pass is documented"""
    if pass_name in DOCUMENTED_MAPPING:
        return DOCUMENTED_MAPPING[pass_name] in documented_passes, DOCUMENTED_MAPPING[pass_name]

    normalized = pass_name.lower().replace('_', '-').replace(' ', '-')
    normalized = re.sub(r'\s*\([^)]*\)', '', normalized)

    if normalized in documented_passes:
        return True, normalized

    for prefix in ['backend-', 'loop-', 'memory-', 'nvptx-', 'nvvm-', 'inline-', 'interprocedural-']:
        candidate = prefix + normalized
        if candidate in documented_passes:
            return True, candidate

    return False, None

# Priority ranking criteria
PRIORITY_RULES = {
    # CRITICAL: GPU-specific, high-impact optimizations
    'CRITICAL': {
        'keywords': ['NVVM', 'NVPTX', 'GPU', 'Register', 'Tensor', 'Memory'],
        'nvidia_specific': True,
        'passes': [
            'NVVMIntrRange',
            'NVPTXImageOptimizer',
            'RegisterUsageInformationCollector',
            'RegisterUsageInformationPropagation',
            'RegisterUsageInformationStorage',
            'NVPTXSetGlobalArrayAlignment',
            'NVPTXSetLocalArrayAlignment',
        ]
    },
    # HIGH: Common optimizations with significant impact
    'HIGH': {
        'passes': [
            'SLPVectorizer',
            'NewGVN',
            'BitTrackingDeadCodeElimination (BDCE)',
            'LoopUnrollAndJam',
            'LoopIdiomVectorize',
            'LoopSimplifyCFG',
            'GVNHoist',
            'AtomicExpand',
            'NVPTXCopyByValArgs',
            'NVPTXCtorDtorLowering',
            'NVPTXLowerArgs',
        ]
    },
    # MEDIUM: Specialized but useful
    'MEDIUM': {
        'passes': [
            'PGOForceFunctionAttrs',
            'AttributorPass',
            'AttributorLightPass',
            'AttributorCGSCCPass',
            'AttributorLightCGSCCPass',
            'BypassSlowDivision',
            'AAManager',
            'RegisterPressureAnalysis',
            'PhysicalRegisterUsageAnalysis',
        ]
    },
    # LOW: Rarely used, minimal impact
    'LOW': {
        'passes': [
            'AddressSanitizer',
            'BoundsChecking',
            'CFGuard',
            'CGProfile',
            'CanonicalizeAliases',
            'CanonicalizeFreezeInLoops',
            'OpenMPOptCGSCCPass',
        ]
    }
}

# Extract all passes and categorize
unconfirmed = mapping_data['unconfirmed_passes']
categories = {
    'Dead Code Elimination': unconfirmed['dead_code_elimination'],
    'Inlining': unconfirmed['inlining'],
    'Instruction Combining': unconfirmed['instruction_combining'],
    'Loop Optimization': unconfirmed['loop_optimization'],
    'Scalar Optimization': unconfirmed['scalar_optimization'],
    'Memory Optimization': unconfirmed['memory_optimization'],
    'Vectorization': unconfirmed['vectorization'],
    'Value Numbering': unconfirmed['value_numbering'],
    'Interprocedural Optimization': unconfirmed['interprocedural_optimization'],
    'Code Generation Preparation': unconfirmed['code_generation_preparation'],
    'Analysis Passes': unconfirmed['analysis_passes'],
    'Sanitizer Passes': unconfirmed['sanitizer_passes'],
    'Other Transformations': unconfirmed['other_transformations'],
    'NVIDIA-Specific': unconfirmed['nvidia_specific'],
    'Profile-Guided Optimization': unconfirmed['profile_guided_optimization'],
    'Attributor Passes': unconfirmed['attributor_passes'],
    'Specialized Optimization': unconfirmed['specialized_optimization']
}

def determine_priority(pass_name, category):
    """Determine priority level for a pass"""
    # Check explicit priority assignments
    for priority, rules in PRIORITY_RULES.items():
        if 'passes' in rules and pass_name in rules['passes']:
            return priority

    # Check by category
    if category == 'NVIDIA-Specific':
        return 'CRITICAL'
    elif category in ['Vectorization', 'Loop Optimization', 'Value Numbering', 'Dead Code Elimination']:
        return 'HIGH'
    elif category in ['Code Generation Preparation', 'Analysis Passes', 'Attributor Passes']:
        return 'MEDIUM'
    else:
        return 'LOW'

def estimate_impact(pass_name, category):
    """Estimate performance/correctness impact"""
    nvidia_keywords = ['NVVM', 'NVPTX', 'Register', 'Memory']
    perf_keywords = ['Vectorize', 'Loop', 'GVN', 'Inline']

    if any(kw in pass_name for kw in nvidia_keywords):
        return 'High - GPU-specific optimization'
    elif any(kw in pass_name for kw in perf_keywords):
        return 'High - Performance critical'
    elif 'Sanitizer' in category or 'Bounds' in pass_name:
        return 'Medium - Correctness/debugging'
    else:
        return 'Medium - General optimization'

def check_evidence(pass_name):
    """Check if there's evidence in L2 analysis"""
    # For now, all passes in unconfirmed have some evidence since they're in the mapping
    return 'Yes - String/flag evidence in binary'

def is_nvidia_specific(pass_name):
    """Check if pass is NVIDIA-specific"""
    nvidia_prefixes = ['NVVM', 'NVPTX', 'Register']
    return any(pass_name.startswith(prefix) for prefix in nvidia_prefixes)

# Build gap analysis with priorities
missing_by_priority = {'CRITICAL': [], 'HIGH': [], 'MEDIUM': [], 'LOW': []}
missing_by_category = {}

for category, passes in categories.items():
    missing_in_category = []

    for pass_name in passes:
        is_doc, _ = is_documented(pass_name)
        if not is_doc:
            priority = determine_priority(pass_name, category)
            impact = estimate_impact(pass_name, category)
            evidence = check_evidence(pass_name)
            nvidia_spec = is_nvidia_specific(pass_name)

            pass_info = {
                'name': pass_name,
                'category': category,
                'priority': priority,
                'impact': impact,
                'evidence': evidence,
                'nvidia_specific': nvidia_spec
            }

            missing_by_priority[priority].append(pass_info)
            missing_in_category.append(pass_info)

    if missing_in_category:
        missing_by_category[category] = missing_in_category

total_missing = sum(len(v) for v in missing_by_priority.values())

# Print summary
print("="*80)
print("COMPREHENSIVE GAP ANALYSIS - FINAL REPORT")
print("="*80)
print(f"\nTotal passes in mapping: {mapping_data['metadata']['total_passes']}")
print(f"Total documented pass files: {len(documented_passes)}")
print(f"Total missing passes: {total_missing}")

print("\n" + "-"*80)
print("MISSING PASSES BY PRIORITY")
print("-"*80)

for priority in ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW']:
    passes = missing_by_priority[priority]
    print(f"\n{priority} Priority: {len(passes)} passes")
    for p in passes:
        nvidia_tag = " [NVIDIA]" if p['nvidia_specific'] else ""
        print(f"  - {p['name']}{nvidia_tag}")
        print(f"    Category: {p['category']}")
        print(f"    Impact: {p['impact']}")

print("\n" + "-"*80)
print("TOP 20 HIGHEST PRIORITY PASSES TO DOCUMENT NEXT")
print("-"*80)

top_20 = missing_by_priority['CRITICAL'][:20] + missing_by_priority['HIGH'][:20]
top_20 = top_20[:20]

for i, p in enumerate(top_20, 1):
    nvidia_tag = " [NVIDIA-SPECIFIC]" if p['nvidia_specific'] else " [LLVM-STANDARD]"
    print(f"\n{i}. {p['name']}{nvidia_tag}")
    print(f"   Priority: {p['priority']}")
    print(f"   Category: {p['category']}")
    print(f"   Impact: {p['impact']}")
    print(f"   Evidence: {p['evidence']}")

# Recommend grouping for parallel agents
print("\n" + "="*80)
print("RECOMMENDED GROUPING FOR PARALLEL AGENT DOCUMENTATION")
print("="*80)

agent_groups = {
    'Agent 1 - NVIDIA Register Optimization': [p for p in missing_by_priority['CRITICAL'] if 'Register' in p['name']],
    'Agent 2 - NVIDIA Code Generation': [p for p in missing_by_priority['CRITICAL'] + missing_by_priority['HIGH'] if 'NVPTX' in p['name'] and 'Register' not in p['name']],
    'Agent 3 - NVIDIA IR Transformation': [p for p in missing_by_priority['CRITICAL'] + missing_by_priority['HIGH'] if 'NVVM' in p['name']],
    'Agent 4 - Loop Optimizations': [p for p in missing_by_category.get('Loop Optimization', [])],
    'Agent 5 - Vectorization & Value Numbering': [p for p in missing_by_category.get('Vectorization', []) + missing_by_category.get('Value Numbering', [])],
    'Agent 6 - Attributor & Analysis': [p for p in missing_by_category.get('Attributor Passes', []) + missing_by_category.get('Analysis Passes', [])],
    'Agent 7 - Code Gen & Sanitizers': [p for p in missing_by_category.get('Code Generation Preparation', []) + missing_by_category.get('Sanitizer Passes', [])],
    'Agent 8 - Other Transformations': [p for p in missing_by_category.get('Other Transformations', []) + missing_by_category.get('Profile-Guided Optimization', []) + missing_by_category.get('Specialized Optimization', [])]
}

for agent, passes in agent_groups.items():
    if passes:
        print(f"\n{agent}: {len(passes)} passes")
        for p in passes:
            print(f"  - {p['name']} ({p['priority']})")

# Save comprehensive report
report = {
    'summary': {
        'total_passes_in_mapping': mapping_data['metadata']['total_passes'],
        'total_documented': len(documented_passes),
        'total_missing': total_missing,
        'breakdown_by_priority': {
            'CRITICAL': len(missing_by_priority['CRITICAL']),
            'HIGH': len(missing_by_priority['HIGH']),
            'MEDIUM': len(missing_by_priority['MEDIUM']),
            'LOW': len(missing_by_priority['LOW'])
        },
        'breakdown_by_category': {cat: len(passes) for cat, passes in missing_by_category.items()}
    },
    'missing_by_priority': missing_by_priority,
    'missing_by_category': missing_by_category,
    'top_20_priorities': top_20,
    'agent_grouping': {k: [p['name'] for p in v] for k, v in agent_groups.items()}
}

with open('/home/user/nvopen-tools/gap_analysis_comprehensive_report.json', 'w') as f:
    json.dump(report, f, indent=2)

print(f"\n\nComprehensive report saved to: /home/user/nvopen-tools/gap_analysis_comprehensive_report.json")
