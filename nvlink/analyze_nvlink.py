"""
IDA Pro COMPLETE database extraction for nvlink binary (CUDA Intermediate Code Compiler)
Exports: strings, functions, graphs, decompilation, xrefs, structures, everything
"""
import idautils
import idc
import idaapi
import ida_bytes
import ida_funcs
import ida_segment
import ida_nalt
import ida_gdl
import ida_graph
import ida_hexrays
import json
import os

OUTPUT_DIR = "/home/grigory/crucible-notes/nvlink"
GRAPH_DIR = os.path.join(OUTPUT_DIR, "graphs")
DISASM_DIR = os.path.join(OUTPUT_DIR, "disasm")
DECOMP_DIR = os.path.join(OUTPUT_DIR, "decompiled")

# Create output directories
for d in [GRAPH_DIR, DISASM_DIR, DECOMP_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

def export_all_strings():
    """Export all strings with addresses and xrefs"""
    print("[*] Exporting strings...")
    strings = []
    for s in idautils.Strings():
        string_ea = s.ea
        string_val = str(s)
        string_type = s.strtype

        xrefs = []
        for xref in idautils.XrefsTo(string_ea):
            func = idaapi.get_func(xref.frm)
            func_name = idc.get_func_name(xref.frm) if func else "unknown"
            xrefs.append({
                'from': hex(xref.frm),
                'func': func_name,
                'type': xref.type
            })

        strings.append({
            'addr': hex(string_ea),
            'value': string_val,
            'type': string_type,
            'xrefs': xrefs
        })

    with open(f"{OUTPUT_DIR}/nvlink_strings.json", "w") as f:
        json.dump(strings, f, indent=2)
    print(f"  Exported {len(strings)} strings")
    return strings

def export_all_functions():
    """Export all functions with detailed metadata"""
    print("[*] Exporting functions...")
    functions = []

    for func_ea in idautils.Functions():
        func = idaapi.get_func(func_ea)
        if not func:
            continue

        func_name = idc.get_func_name(func_ea)
        func_start = func.start_ea
        func_end = func.end_ea
        func_size = func_end - func_start

        flags = idc.get_func_attr(func_ea, FUNCATTR_FLAGS)
        is_library = (flags & FUNC_LIB) != 0
        is_thunk = (flags & FUNC_THUNK) != 0

        insn_count = 0
        ea = func_start
        while ea < func_end:
            if idc.is_code(idc.get_full_flags(ea)):
                insn_count += 1
            ea = idc.next_head(ea, func_end)

        callers = []
        for xref in idautils.XrefsTo(func_start):
            caller_func = idaapi.get_func(xref.frm)
            if caller_func:
                callers.append({
                    'addr': hex(xref.frm),
                    'func': idc.get_func_name(caller_func.start_ea)
                })

        callees = []
        for head in idautils.Heads(func_start, func_end):
            for xref in idautils.XrefsFrom(head, 0):
                if xref.type in [idc.fl_CN, idc.fl_CF]:
                    callee_func = idaapi.get_func(xref.to)
                    if callee_func:
                        callees.append({
                            'addr': hex(xref.to),
                            'func': idc.get_func_name(callee_func.start_ea)
                        })

        functions.append({
            'addr': hex(func_start),
            'end': hex(func_end),
            'name': func_name,
            'size': func_size,
            'insn_count': insn_count,
            'is_library': is_library,
            'is_thunk': is_thunk,
            'callers': callers,
            'callees': callees
        })

    with open(f"{OUTPUT_DIR}/nvlink_functions.json", "w") as f:
        json.dump(functions, f, indent=2)
    print(f"  Exported {len(functions)} functions")
    return functions

def export_complete_disassembly():
    """Export complete disassembly of all functions"""
    print("[*] Exporting complete disassembly...")

    total = 0
    for func_ea in idautils.Functions():
        func = idaapi.get_func(func_ea)
        if not func:
            continue

        func_name = idc.get_func_name(func_ea).replace('/', '_')
        asm_lines = []

        ea = func.start_ea
        while ea < func.end_ea:
            disasm = idc.generate_disasm_line(ea, 0)
            bytes_hex = ' '.join([f'{idc.get_wide_byte(ea + i):02x}' for i in range(idc.get_item_size(ea))])

            asm_lines.append(f"{hex(ea)}: {bytes_hex:40s} {disasm}")
            ea = idc.next_head(ea, func.end_ea)

        # Save to individual file
        with open(f"{DISASM_DIR}/{func_name}_{hex(func_ea)}.asm", "w") as f:
            f.write(f"; Function: {func_name}\n")
            f.write(f"; Address: {hex(func.start_ea)} - {hex(func.end_ea)}\n")
            f.write(f"; Size: {func.end_ea - func.start_ea} bytes\n")
            f.write(";\n")
            f.write('\n'.join(asm_lines))

        total += 1
        if total % 100 == 0:
            print(f"  Disassembled {total} functions...")

    print(f"  Exported {total} function disassemblies")

def export_function_graphs():
    """Export function control flow graphs"""
    print("[*] Exporting function graphs...")

    total = 0
    for func_ea in idautils.Functions():
        func = idaapi.get_func(func_ea)
        if not func:
            continue

        func_name = idc.get_func_name(func_ea).replace('/', '_')

        # Get flowchart (basic blocks)
        flowchart = idaapi.FlowChart(func)

        blocks = []
        edges = []

        for block in flowchart:
            block_data = {
                'id': block.id,
                'start': hex(block.start_ea),
                'end': hex(block.end_ea),
                'size': block.end_ea - block.start_ea,
                'instructions': []
            }

            # Get instructions in block
            ea = block.start_ea
            while ea < block.end_ea:
                disasm = idc.generate_disasm_line(ea, 0)
                block_data['instructions'].append({
                    'addr': hex(ea),
                    'asm': disasm
                })
                ea = idc.next_head(ea, block.end_ea)

            blocks.append(block_data)

            # Get edges
            for succ in block.succs():
                edges.append({
                    'from': block.id,
                    'to': succ.id
                })

        graph_data = {
            'function': func_name,
            'addr': hex(func_ea),
            'blocks': blocks,
            'edges': edges
        }

        with open(f"{GRAPH_DIR}/{func_name}_{hex(func_ea)}.json", "w") as f:
            json.dump(graph_data, f, indent=2)

        # Also export as DOT format for visualization
        with open(f"{GRAPH_DIR}/{func_name}_{hex(func_ea)}.dot", "w") as f:
            f.write(f"digraph {func_name} {{\n")
            f.write(f"  label=\"{func_name} @ {hex(func_ea)}\";\n")
            f.write("  node [shape=box];\n")

            for block in blocks:
                label = f"{block['start']}\\n{len(block['instructions'])} instructions"
                f.write(f"  block_{block['id']} [label=\"{label}\"];\n")

            for edge in edges:
                f.write(f"  block_{edge['from']} -> block_{edge['to']};\n")

            f.write("}\n")

        total += 1
        if total % 50 == 0:
            print(f"  Exported {total} graphs...")

    print(f"  Exported {total} function graphs")

def export_decompilation():
    """Export Hex-Rays decompilation if available"""
    print("[*] Attempting decompilation export...")

    if not idaapi.init_hexrays_plugin():
        print("  Hex-Rays decompiler not available")
        return

    total = 0
    success = 0

    for func_ea in idautils.Functions():
        func = idaapi.get_func(func_ea)
        if not func:
            continue

        func_name = idc.get_func_name(func_ea).replace('/', '_')

        try:
            cfunc = idaapi.decompile(func_ea)
            if cfunc:
                pseudocode = str(cfunc)

                with open(f"{DECOMP_DIR}/{func_name}_{hex(func_ea)}.c", "w") as f:
                    f.write(f"// Function: {func_name}\n")
                    f.write(f"// Address: {hex(func.start_ea)}\n")
                    f.write("//\n")
                    f.write(pseudocode)

                success += 1
        except Exception as e:
            pass  # Decompilation failed for this function

        total += 1
        if total % 50 == 0:
            print(f"  Decompiled {success}/{total} functions...")

    print(f"  Successfully decompiled {success}/{total} functions")

def export_imports():
    """Export imported functions"""
    print("[*] Exporting imports...")
    imports = []

    nimps = idaapi.get_import_module_qty()
    for i in range(nimps):
        name = idaapi.get_import_module_name(i)
        if not name:
            continue

        def imp_cb(ea, name, ordinal):
            imports.append({
                'module': idaapi.get_import_module_name(i),
                'name': name,
                'addr': hex(ea),
                'ordinal': ordinal
            })
            return True

        idaapi.enum_import_names(i, imp_cb)

    with open(f"{OUTPUT_DIR}/nvlink_imports.json", "w") as f:
        json.dump(imports, f, indent=2)
    print(f"  Exported {len(imports)} imports")
    return imports

def export_segments():
    """Export segment information"""
    print("[*] Exporting segments...")
    segments = []

    for seg_ea in idautils.Segments():
        seg = idaapi.getseg(seg_ea)
        if not seg:
            continue

        segments.append({
            'name': idc.get_segm_name(seg_ea),
            'start': hex(seg.start_ea),
            'end': hex(seg.end_ea),
            'size': seg.end_ea - seg.start_ea,
            'type': seg.type,
            'perm': seg.perm
        })

    with open(f"{OUTPUT_DIR}/nvlink_segments.json", "w") as f:
        json.dump(segments, f, indent=2)
    print(f"  Exported {len(segments)} segments")
    return segments

def export_structures():
    """Export all structure definitions"""
    print("[*] Exporting structures...")
    structures = []

    for idx in range(ida_struct.get_struc_qty()):
        tid = ida_struct.get_struc_by_idx(idx)
        sptr = ida_struct.get_struc(tid)
        if not sptr:
            continue

        struct_name = ida_struct.get_struc_name(tid)
        struct_size = ida_struct.get_struc_size(sptr)

        members = []
        for mptr in sptr.members:
            member_name = ida_struct.get_member_name(mptr.id)
            member_offset = mptr.soff
            member_size = ida_struct.get_member_size(mptr)

            members.append({
                'name': member_name,
                'offset': member_offset,
                'size': member_size
            })

        structures.append({
            'name': struct_name,
            'size': struct_size,
            'members': members
        })

    with open(f"{OUTPUT_DIR}/nvlink_structures.json", "w") as f:
        json.dump(structures, f, indent=2)
    print(f"  Exported {len(structures)} structures")

def export_xrefs():
    """Export all cross-references"""
    print("[*] Exporting cross-references...")
    xrefs = []

    for func_ea in idautils.Functions():
        func = idaapi.get_func(func_ea)
        if not func:
            continue

        for head in idautils.Heads(func.start_ea, func.end_ea):
            for xref in idautils.XrefsFrom(head, 0):
                xrefs.append({
                    'from': hex(head),
                    'from_func': idc.get_func_name(func_ea),
                    'to': hex(xref.to),
                    'to_func': idc.get_func_name(xref.to) if idaapi.get_func(xref.to) else None,
                    'type': xref.type
                })

    with open(f"{OUTPUT_DIR}/nvlink_xrefs.json", "w") as f:
        json.dump(xrefs, f, indent=2)
    print(f"  Exported {len(xrefs)} cross-references")

def export_comments():
    """Export all comments"""
    print("[*] Exporting comments...")
    comments = []

    for ea in idautils.Heads():
        # Regular comments
        cmt = idc.get_cmt(ea, 0)
        if cmt:
            comments.append({
                'addr': hex(ea),
                'type': 'regular',
                'text': cmt
            })

        # Repeatable comments
        rpt = idc.get_cmt(ea, 1)
        if rpt:
            comments.append({
                'addr': hex(ea),
                'type': 'repeatable',
                'text': rpt
            })

    with open(f"{OUTPUT_DIR}/nvlink_comments.json", "w") as f:
        json.dump(comments, f, indent=2)
    print(f"  Exported {len(comments)} comments")

def export_names():
    """Export all named locations"""
    print("[*] Exporting names...")
    names = []

    for ea, name in idautils.Names():
        names.append({
            'addr': hex(ea),
            'name': name
        })

    with open(f"{OUTPUT_DIR}/nvlink_names.json", "w") as f:
        json.dump(names, f, indent=2)
    print(f"  Exported {len(names)} named locations")

def extract_rodata():
    """Extract .rodata section data"""
    print("[*] Extracting .rodata section...")

    rodata_seg = idaapi.get_segm_by_name(".rodata")
    if not rodata_seg:
        print("  .rodata segment not found")
        return

    # Extract raw bytes
    size = rodata_seg.end_ea - rodata_seg.start_ea
    data = ida_bytes.get_bytes(rodata_seg.start_ea, size)

    with open(f"{OUTPUT_DIR}/nvlink_rodata.bin", "wb") as f:
        f.write(data)

    print(f"  Extracted {size} bytes from .rodata")

def export_callgraph():
    """Export complete call graph"""
    print("[*] Exporting call graph...")

    edges = []
    for func_ea in idautils.Functions():
        func = idaapi.get_func(func_ea)
        if not func:
            continue

        caller = idc.get_func_name(func_ea)

        for head in idautils.Heads(func.start_ea, func.end_ea):
            for xref in idautils.XrefsFrom(head, 0):
                if xref.type in [idc.fl_CN, idc.fl_CF]:
                    callee_func = idaapi.get_func(xref.to)
                    if callee_func:
                        callee = idc.get_func_name(callee_func.start_ea)
                        edges.append({
                            'from': caller,
                            'from_addr': hex(func_ea),
                            'to': callee,
                            'to_addr': hex(callee_func.start_ea)
                        })

    with open(f"{OUTPUT_DIR}/nvlink_callgraph.json", "w") as f:
        json.dump(edges, f, indent=2)

    # Also export as DOT
    with open(f"{OUTPUT_DIR}/nvlink_callgraph.dot", "w") as f:
        f.write("digraph callgraph {\n")
        f.write("  node [shape=box];\n")

        for edge in edges:
            f.write(f'  "{edge["from"]}" -> "{edge["to"]}";\n')

        f.write("}\n")

    print(f"  Exported {len(edges)} call edges")

def main():
    print("=" * 80)
    print("CICC COMPLETE DATABASE EXTRACTION (CUDA Intermediate Code Compiler)")
    print("=" * 80)

    print("[*] Waiting for auto-analysis...")
    idaapi.auto_wait()

    # Export all data
    strings_data = export_all_strings()
    funcs_data = export_all_functions()
    imports_data = export_imports()
    segments_data = export_segments()

    export_xrefs()
    export_comments()
    export_names()
    extract_rodata()
    export_callgraph()

    # Export complete disassembly
    export_complete_disassembly()

    # Export function graphs
    export_function_graphs()

    # Export decompilation
    export_decompilation()

    # Summary
    print("\n" + "=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total strings: {len(strings_data)}")
    print(f"Total functions: {len(funcs_data)}")
    print(f"Total imports: {len(imports_data)}")
    print(f"Total segments: {len(segments_data)}")
    print("\nOutput directories:")
    print(f"  {OUTPUT_DIR}/")
    print(f"  {GRAPH_DIR}/")
    print(f"  {DISASM_DIR}/")
    print(f"  {DECOMP_DIR}/")

    # Don't call qexit when run from IDAPython_ExecScript

# Execute main directly (not using __name__ guard since we're called via IDAPython_ExecScript)
main()
