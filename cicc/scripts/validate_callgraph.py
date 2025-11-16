#!/usr/bin/env python3
"""
Comprehensive callgraph validation and verification system.

Validates claimed metrics:
1. Cross-module call matrices
2. Integration hotspots
3. Entry points
4. Reachability analysis
5. Critical paths
6. Module dependency cycles
7. Bridge functions
8. Dead code detection
"""

import json
import sys
from pathlib import Path
from collections import defaultdict, deque
from typing import Dict, Set, List, Tuple, Optional
import random
from statistics import mean, stdev

# Paths
CICC_DIR = Path("/home/grigory/nvopen-tools/cicc")
CALLGRAPH_FILE = CICC_DIR / "databases" / "cicc_callgraph.json"
ANALYSES_DIR = CICC_DIR / "foundation" / "analyses"
OUTPUT_DIR = ANALYSES_DIR

# Module definitions based on existing data
KNOWN_MODULES = {
    "optimization_framework",
    "register_allocation",
    "compilation_pipeline",
    "ptx_emission",
    "instruction_selection",
    "error_handling",
    "tensor_core_codegen",
    "architecture_detection",
    "external",
    "main",
    "unknown",
}


class CallgraphValidator:
    """Comprehensive callgraph validation system."""

    def __init__(self, callgraph_file: Path):
        """Initialize validator with callgraph data."""
        print(f"Loading callgraph from {callgraph_file}...")
        with open(callgraph_file, "r") as f:
            self.callgraph = json.load(f)

        print(f"Loaded {len(self.callgraph)} call records")

        # Build indices
        self.build_indices()
        self.classify_functions()

    def build_indices(self) -> None:
        """Build various indices for analysis."""
        print("Building call graph indices...")

        # Caller and callee mappings
        self.callers: Dict[str, Set[str]] = defaultdict(set)  # func -> set of callers
        self.callees: Dict[str, Set[str]] = defaultdict(set)  # func -> set of callees
        self.call_counts: Dict[Tuple[str, str], int] = defaultdict(int)  # (from, to) -> count

        # Address mappings
        self.addr_to_name: Dict[str, str] = {}  # addr -> name
        self.name_to_addrs: Dict[str, Set[str]] = defaultdict(set)  # name -> set of addrs

        # Module tracking
        self.func_modules: Dict[str, str] = {}  # func -> module
        self.module_functions: Dict[str, Set[str]] = defaultdict(set)  # module -> set of funcs

        # Cross-module calls
        self.cross_module_calls: Dict[Tuple[str, str], int] = defaultdict(int)

        for call in self.callgraph:
            from_func = call["from"]
            to_func = call["to"]
            from_addr = call["from_addr"]
            to_addr = call["to_addr"]

            self.callers[to_func].add(from_func)
            self.callees[from_func].add(to_func)
            self.call_counts[(from_func, to_func)] += 1

            self.addr_to_name[from_addr] = from_func
            self.addr_to_name[to_addr] = to_func
            self.name_to_addrs[from_func].add(from_addr)
            self.name_to_addrs[to_func].add(to_addr)

        print(f"Built indices: {len(self.callers)} unique callees")

    def classify_functions(self) -> None:
        """Classify functions into modules."""
        print("Classifying functions into modules...")

        # Build reverse mapping: address (normalized) -> function name
        addr_to_func = {}
        for func_name in self.callers.keys() | set(self.callees.keys()):
            for addr in self.name_to_addrs.get(func_name, set()):
                # Normalize address (remove 0x, convert to lowercase)
                norm_addr = addr.lower().replace("0x", "")
                addr_to_func[norm_addr] = func_name

        # Load real module classification from function_to_module_map
        try:
            module_map_file = Path("/home/grigory/nvopen-tools/cicc/foundation/taxonomy/modules/function_to_module_map.json")
            if module_map_file.exists():
                print(f"Loading module classification from {module_map_file}...")
                with open(module_map_file, "r") as f:
                    addr_to_module = json.load(f)

                # Map functions to modules using address
                matched = 0
                for addr_hex, module in addr_to_module.items():
                    # Normalize the address from module map
                    norm_addr = addr_hex.lower().replace("0x", "")
                    if norm_addr in addr_to_func:
                        func_name = addr_to_func[norm_addr]
                        self.func_modules[func_name] = module
                        self.module_functions[module].add(func_name)
                        matched += 1

                print(f"Classified {matched} functions from module map")
            else:
                print("Module map file not found!")
        except Exception as e:
            print(f"ERROR loading module classification: {e}")
            import traceback
            traceback.print_exc()

        # Fallback: classify remaining functions based on patterns
        unclassified = 0
        for func in self.callers.keys() | set(self.callees.keys()):
            if func not in self.func_modules:
                # Try to determine module from name patterns
                if func.startswith("."):
                    self.func_modules[func] = "external"
                elif func.startswith("sub_"):
                    self.func_modules[func] = "unknown"
                else:
                    self.func_modules[func] = "unknown"

                module = self.func_modules[func]
                self.module_functions[module].add(func)
                unclassified += 1

        print(f"Unclassified (fallback): {unclassified} functions")

    def verify_cross_module_calls(self) -> Dict:
        """Verify cross-module call matrices."""
        print("\n=== VERIFYING CROSS-MODULE CALLS ===")

        results = {
            "total_cross_module_calls": 0,
            "by_module_pair": {},
            "sample_verification": {}
        }

        # Calculate cross-module calls
        for (from_func, to_func), count in self.call_counts.items():
            from_module = self.func_modules.get(from_func, "unknown")
            to_module = self.func_modules.get(to_func, "unknown")

            if from_module != to_module:
                key = f"{from_module} -> {to_module}"
                if key not in self.cross_module_calls:
                    self.cross_module_calls[(from_module, to_module)] = 0
                self.cross_module_calls[(from_module, to_module)] += count
                results["total_cross_module_calls"] += count

        # Format results
        for (from_mod, to_mod), count in sorted(
            self.cross_module_calls.items(), key=lambda x: x[1], reverse=True
        ):
            key = f"{from_mod} -> {to_mod}"
            results["by_module_pair"][key] = count

        print(f"Total cross-module calls: {results['total_cross_module_calls']}")

        # Sample verification
        if self.cross_module_calls:
            top_pair = max(self.cross_module_calls.items(), key=lambda x: x[1])
            from_mod, to_mod = top_pair[0]
            claimed_count = top_pair[1]

            # Find sample functions
            sample_calls = [
                (from_func, to_func, count)
                for (from_func, to_func), count in self.call_counts.items()
                if self.func_modules.get(from_func) == from_mod
                and self.func_modules.get(to_func) == to_mod
            ]

            sample_count = min(10, len(sample_calls))
            samples = random.sample(sample_calls, sample_count)

            results["sample_verification"] = {
                "top_module_pair": f"{from_mod} -> {to_mod}",
                "claimed_total": claimed_count,
                "sample_size": sample_count,
                "sample_calls": [
                    {"from": f, "to": t, "count": c} for f, t, c in samples
                ]
            }

        return results

    def validate_entry_points(self) -> Dict:
        """Validate entry points."""
        print("\n=== VALIDATING ENTRY POINTS ===")

        results = {
            "total_claimed": 0,
            "verified_entry_points": [],
            "false_positives": [],
            "confidence_score": 0.0
        }

        # Load claimed entry points
        try:
            entry_points_file = ANALYSES_DIR / "entry_points_global.json"
            with open(entry_points_file, "r") as f:
                claimed = json.load(f)
                results["total_claimed"] = len(claimed)
        except:
            claimed = []

        # Verify each claimed entry point
        verified = 0
        false_positives = 0

        for entry in claimed[:1000]:  # Sample 1000
            func_name = entry.get("name") or entry.get("addr")
            callers_count = len(self.callers.get(func_name, set()))

            if callers_count == 0:
                verified += 1
                results["verified_entry_points"].append(func_name)
            else:
                false_positives += 1
                results["false_positives"].append({
                    "name": func_name,
                    "actual_callers": callers_count,
                    "callers": list(self.callers.get(func_name, set()))[:5]
                })

        results["confidence_score"] = (
            verified / (verified + false_positives)
            if (verified + false_positives) > 0
            else 0
        )

        print(f"Entry Points: {verified} verified, {false_positives} false positives")
        print(f"Confidence: {results['confidence_score']:.1%}")

        return results

    def analyze_reachability(self) -> Dict:
        """Analyze reachability from main() entry points."""
        print("\n=== ANALYZING REACHABILITY ===")

        results = {
            "reachable_functions": set(),
            "unreachable_functions": set(),
            "reachability_percentage": 0.0,
            "dead_code_stats": {}
        }

        # Find main entry points
        main_entries = [
            f for f, module in self.func_modules.items()
            if module == "main" and len(self.callers.get(f, set())) == 0
        ]

        if not main_entries:
            print("No main() entry points found, trying external entries...")
            main_entries = [
                f for f in self.callers.keys()
                if len(self.callers.get(f, set())) == 0
            ][:10]

        print(f"Found {len(main_entries)} potential main entry points")

        # BFS from main
        visited = set()
        queue = deque(main_entries)

        while queue:
            func = queue.popleft()
            if func in visited:
                continue
            visited.add(func)

            for callee in self.callees.get(func, set()):
                if callee not in visited:
                    queue.append(callee)

        results["reachable_functions"] = visited

        # Find unreachable
        all_functions = set(self.callers.keys()) | set(self.callees.keys())
        unreachable = all_functions - visited
        results["unreachable_functions"] = unreachable

        if all_functions:
            results["reachability_percentage"] = len(visited) / len(all_functions)

        print(f"Reachable: {len(visited)} functions")
        print(f"Unreachable: {len(unreachable)} functions")
        print(f"Reachability: {results['reachability_percentage']:.1%}")

        return results

    def find_dead_code(self) -> Dict:
        """Identify dead code (unreachable with 0 callers)."""
        print("\n=== FINDING DEAD CODE ===")

        reachability = self.analyze_reachability()

        # Dead code: unreachable AND has 0 callers
        dead_code = []
        total_size = 0

        for func in reachability["unreachable_functions"]:
            if len(self.callers.get(func, set())) == 0:
                dead_code.append(func)

        print(f"Dead code functions: {len(dead_code)}")

        return {
            "dead_code_count": len(dead_code),
            "sample_dead_functions": dead_code[:50],
            "dead_code_modules": self._count_by_module(dead_code)
        }

    def find_module_cycles(self) -> Dict:
        """Find module dependency cycles."""
        print("\n=== FINDING MODULE CYCLES ===")

        # Build module dependency graph
        module_deps: Dict[str, Set[str]] = defaultdict(set)

        for (from_func, to_func) in self.call_counts.keys():
            from_mod = self.func_modules.get(from_func, "unknown")
            to_mod = self.func_modules.get(to_func, "unknown")

            if from_mod != to_mod:
                module_deps[from_mod].add(to_mod)

        # Find cycles
        cycles = []
        visited = set()
        rec_stack = set()

        def dfs_cycle(node, path):
            visited.add(node)
            rec_stack.add(node)

            for neighbor in module_deps.get(node, set()):
                if neighbor not in visited:
                    dfs_cycle(neighbor, path + [neighbor])
                elif neighbor in rec_stack:
                    cycle = path[path.index(neighbor):] + [neighbor]
                    cycles.append(cycle)

            rec_stack.remove(node)

        for module in module_deps.keys():
            if module not in visited:
                dfs_cycle(module, [module])

        print(f"Module dependency cycles: {len(cycles)}")

        return {
            "cycle_count": len(cycles),
            "cycles": cycles,
            "module_dependencies": dict(module_deps)
        }

    def verify_hotspots(self) -> Dict:
        """Verify integration hotspots."""
        print("\n=== VERIFYING INTEGRATION HOTSPOTS ===")

        # Find functions called by all modules
        hotspots = []

        for func in self.callers.keys():
            calling_modules = set()
            for caller in self.callers.get(func, set()):
                module = self.func_modules.get(caller, "unknown")
                calling_modules.add(module)

            if len(calling_modules) >= 8:  # Called by all 8 modules
                hotspots.append({
                    "name": func,
                    "called_by_modules": len(calling_modules),
                    "total_calls": sum(
                        self.call_counts[(c, func)]
                        for c in self.callers.get(func, set())
                    ),
                    "modules": list(calling_modules)
                })

        hotspots.sort(key=lambda x: x["total_calls"], reverse=True)

        print(f"Universal hotspots (called by all 8 modules): {len(hotspots)}")

        return {
            "hotspot_count": len(hotspots),
            "top_hotspots": hotspots[:20]
        }

    def identify_bridge_functions(self) -> Dict:
        """Identify bridge functions between modules."""
        print("\n=== IDENTIFYING BRIDGE FUNCTIONS ===")

        bridges = []

        for func in self.callees.keys():
            from_module = self.func_modules.get(func, "unknown")

            # Check if this function calls multiple other modules
            called_modules = set()
            for callee in self.callees.get(func, set()):
                to_module = self.func_modules.get(callee, "unknown")
                if to_module != from_module:
                    called_modules.add(to_module)

            # And is called by multiple modules
            calling_modules = set()
            for caller in self.callers.get(func, set()):
                caller_module = self.func_modules.get(caller, "unknown")
                if caller_module != from_module:
                    calling_modules.add(caller_module)

            if len(called_modules) >= 2 and len(calling_modules) >= 2:
                bridges.append({
                    "name": func,
                    "module": from_module,
                    "called_modules": list(called_modules),
                    "calling_modules": list(calling_modules),
                    "call_degree": len(called_modules),
                    "caller_degree": len(calling_modules)
                })

        bridges.sort(key=lambda x: x["call_degree"] * x["caller_degree"], reverse=True)

        print(f"Bridge functions: {len(bridges)}")

        return {
            "bridge_count": len(bridges),
            "top_bridges": bridges[:50]
        }

    def _count_by_module(self, functions: List[str]) -> Dict[str, int]:
        """Count functions by module."""
        counts = defaultdict(int)
        for func in functions:
            module = self.func_modules.get(func, "unknown")
            counts[module] += 1
        return dict(counts)

    def generate_report(self) -> Dict:
        """Generate comprehensive validation report."""
        report = {
            "timestamp": None,
            "metrics": {},
            "validations": {},
            "data_quality": {},
            "corrections": []
        }

        # Run all validations
        report["validations"]["cross_module_calls"] = self.verify_cross_module_calls()
        report["validations"]["entry_points"] = self.validate_entry_points()
        report["validations"]["hotspots"] = self.verify_hotspots()
        report["validations"]["cycles"] = self.find_module_cycles()
        report["validations"]["bridges"] = self.identify_bridge_functions()
        report["validations"]["dead_code"] = self.find_dead_code()

        # Calculate data quality
        entry_point_confidence = report["validations"]["entry_points"]["confidence_score"]
        report["data_quality"]["entry_point_accuracy"] = entry_point_confidence
        report["data_quality"]["overall_score"] = entry_point_confidence

        # Identify corrections
        if entry_point_confidence < 0.9:
            report["corrections"].append(
                f"Entry point accuracy only {entry_point_confidence:.1%}, "
                f"review false positives"
            )

        return report


def main():
    """Run validation."""
    print("CALLGRAPH VALIDATION SYSTEM")
    print("=" * 60)

    # Initialize validator
    validator = CallgraphValidator(CALLGRAPH_FILE)

    # Generate report
    print("\nGenerating comprehensive validation report...")
    report = validator.generate_report()

    # Save results
    output_file = OUTPUT_DIR / "CALLGRAPH_VALIDATION_REPORT.json"
    print(f"\nSaving report to {output_file}")
    with open(output_file, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # Save individual analyses
    analyses = {
        "cross_module_call_matrix_verified": report["validations"]["cross_module_calls"],
        "integration_hotspots_verified": report["validations"]["hotspots"],
        "entry_points_verified": report["validations"]["entry_points"],
        "module_dependency_cycles_detailed": report["validations"]["cycles"],
        "bridge_functions_verified": report["validations"]["bridges"],
        "dead_code_detection": report["validations"]["dead_code"],
    }

    for name, data in analyses.items():
        filepath = OUTPUT_DIR / f"{name}.json"
        print(f"Saving {name}...")
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2, default=str)

    # Print summary
    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY")
    print("=" * 60)

    for key, val in report["data_quality"].items():
        if isinstance(val, float):
            print(f"{key}: {val:.1%}")
        else:
            print(f"{key}: {val}")

    print(f"\nTotal corrections needed: {len(report['corrections'])}")
    for correction in report["corrections"]:
        print(f"  - {correction}")

    print("\nValidation complete!")


if __name__ == "__main__":
    main()
