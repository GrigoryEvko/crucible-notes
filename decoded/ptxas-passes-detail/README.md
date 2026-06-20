# ptxas v13.0.88 — Optimization/Validation/Lowering Phase Detail (binary-derived)

Deep-dive reverse engineering of 14 high-leverage ptxas phases that previously had
no detail page. Every fact is recovered from the ptxas binary
(`ptxas/ptxas`, CUDA 13.0.88, ELF x86-64, stripped; `.text` VMA == file offset) via
`objdump` raw disassembly cross-checked against the per-function decompilation in
`ptxas/decompiled/`. Every fact is grounded in disassembly, control/data flow, vtable
layouts, and the binary's own embedded strings; where the binary and any prior doc
disagree, the **binary wins** and the divergence is flagged in-row. All output is
binary-derived.

## Files

- `passes_detail.tsv` — one row per phase. Columns:
  `phase | bin_index | vtable_va | execute_thunk_va | worker_or_slot | dispatch_kind | role | key_transform`.
  This is a superset of the requested `(phase, index, execute_fn, role, key_transform)`:
  `bin_index`=index, `worker_or_slot`=execute_fn, plus the vtable VA, the execute-thunk
  VA, and the dispatch kind for full traceability.
- `README.md` — this file (methodology, dispatch architecture, the gate primitive,
  per-phase confidence, and the wiki page proposal).

## Phases covered (authoritative bin_index)

| bin | phase | kind |
|----:|-------|------|
| 0 | OriCheckInitialProgram | Validation (debug-gated, read-only) |
| 2 | PromoteFP16 | Lowering |
| 3 | AnalyzeControlFlow | Analysis (CFG build) |
| 9 | OriCreateMacroInsts (TTU ray-tracing macro packer) | Lowering |
| 13 | OriSanitize (= compute-sanitizer injector) | Instrumentation |
| 39 | ExtractShaderConstsFirst | Optimization |
| 45 | ConvertVTGReadWrite | Lowering (profile virtual) |
| 46 | DoVirtualCTAExpansion | Lowering (profile virtual) |
| 49 | ForwardProgress | Lowering (profile virtual) |
| 55 | EnforceArgumentRestrictions | Validation/Legalization (profile virtual) |
| 59 | ExtractShaderConstsFinal | Optimization |
| 94 | ExpandJmxComputation | Lowering |
| 110 | FinalInspectionPass | Validation (shaderType virtual) |
| 116 | BackPropagateVEC2D | Analysis (post-schedule) |

> **Index reconciliation.** The task brief listed older display-order numbers
> (e.g. "OriCreateMacroInsts(8)", "FinalInspectionPass(94)"). The authoritative
> identity is the **bin_index** = the factory `sub_C60D30` switch case = the phase
> name-table index at `0x22BD0C0`. The brief's numbers are a stale ordering; the
> bin_index values above were re-derived directly from the binary and supersede them.

## Dispatch architecture (binary-confirmed)

Each phase is a 16-byte command object built by the factory `sub_C60D30` (159-case
switch, jump table `@0x22BBEB8`). Case *N* allocates the object and writes its vtable
pointer `off_22BDxxx`; the object's two qwords are `[vtable, Gb]`. The dispatch loop
`sub_C64F70` calls vtable **slot 0 = Execute(this, Gb)**. Slot 0 of each phase vtable
points at a small **execute thunk** in the `0xC5E000–0xC60Cxx` band that:

1. optionally calls the run-gate `sub_7DDB50(Gb)` and branches on its return, then
2. tail-jumps to the real **worker** (`sub_XXXXXX`) or **virtual-dispatches** through
   `theProfile` (Gb+0x630) or `shaderType` (Gb+0x640).

All 14 vtable VAs and execute-thunk VAs were verified against the factory switch and the
raw `.rodata` vtable bytes; all worker addresses were verified by disassembling the thunk.

### The gate primitive `sub_7DDB50` (the OptBudget oracle)

This single function backs the source `OPT` / `NOOPT` predicates and appears in most
thunks. It is **not** a raw OptLevel getter. It reads `oriKnobs` (`Gb+0x1664`) and
consults knob **499** (the optimization-budget knob) at the 72-byte knob-entry stride
(entry = `knobs[9] + 72*499 = +0x8C58`: byte+0 = active, dword+8 = limit, dword+12 =
running count). While under budget it returns the configured effective opt level
`*(Gb+0x838)` and increments the count; **once the budget is exhausted it returns the
literal `1`** — a forced-O0 sentinel. The per-thunk comparison therefore means:

- `cmp $1; je`   → return == 1 → **NOOPT** (OptLevel 0, or budget burned)
- `cmp $1; jg`   → return  > 1 → **optimizing** (OPT)
- `cmp $1; jle`  → skip phase unless OPT

The default knob accessor is `sub_6614A0` (`byte[knobs[9] + 72*idx] != 0`); thunks
compare the resolved slot against it before reading a knob byte directly (e.g.
AnalyzeControlFlow reads DISABLESOURCEORDER at `knobs+0x4218`).

### Three dispatch shapes among these 14

1. **Direct worker** (gate then `jmp sub_worker`): PromoteFP16, OriCheckInitialProgram,
   AnalyzeControlFlow, OriCreateMacroInsts, OriSanitize, ExtractShaderConsts{First,Final},
   ExpandJmxComputation, BackPropagateVEC2D.
2. **`theProfile` virtual** (per-architecture override; base impl is an empty
   `repz ret` stub at a known address): ConvertVTGReadWrite (slot +0xA8,
   base 0x7D6BB0), DoVirtualCTAExpansion (slot +0x1C0, base 0x7D6DD0),
   ForwardProgress (slot +0xB8, base 0x7D6BD0), EnforceArgumentRestrictions
   (slot +0xD0, base 0x7D6C00). Base profile vtable VMA `0x21CDCB8`.
3. **`shaderType` chained virtual**: FinalInspectionPass (guard slot +0x60 vs base
   `sub_661310`, then inner-object slots +0x118 and +0xC10).

## Key cross-cutting findings

- **OriCreateMacroInsts** is the ray-tracing **TTU macro packer** (the binary's public
  name does not surface the "TTU" string, but the body's logic is unambiguous). Worker
  `sub_19DFC20`; gate `Gb+0x564` bit7 (HasTTU); matches
  `(node+0x48 & 0xCF00) == 0xFC00` = OP_TTUOPEN. The neighboring thunk `0xC5F8E0`
  (a different phase) gates on `Gb+0x588` — the two were previously conflated.
- **OriSanitize** is **not** an IR validator; it is the **compute-sanitizer** device
  instrumentation injector (memcheck / threadsteer paths, `__cuda_sanitizer_*` runtime
  calls). All four mode/symbol strings are byte-verified in `.rodata`. The device
  instrumentation behavior behind this name is a **binary-only discovery**.
- **OriCheckInitialProgram** is a debug-gated (`Gb+0x566 & 0x401`) read-only DAG/
  consistency walk; it sits ahead of the index-0 FP16 promotion phase. Its public-name
  identity is uncertain (no other reference uses this name), so the name binding is
  MEDIUM confidence while the read-only-validation behavior is HIGH.
- **EnforceArgumentRestrictions (bin 55)** and **LateEnforceArgumentRestrictions (bin
  103, thunk `0xC5E880`)** call the **same** profile slot +0xD0 — bin 55 OPT-gated with
  `FinalCall=false`, bin 103 ungated with `FinalCall=true`. Binary-proven pair.
- **ExtractShaderConsts First/Final** share worker `sub_1C72640`; the boolean flag
  (a2) controls (i) skipping the `remainingConstBytes ≤ 3` early-out and (ii) the
  `sub_1C68760` (FinalizeSetupShader) commit. Final asserts per-operand loop-id (+66) /
  block (+164) consistency, `BUG()` on violation.

## Confidence summary

HIGH confidence on dispatch/gate/worker identity and the primary transform for **all 14**.
Reduced-confidence sub-items (flagged in-row): the loop-depth interval machinery inside
the CFG builder (MEDIUM); the exact public-name identity of OriCheckInitialProgram
(MEDIUM — name binding uncertain); the exact propagated sub-attribute flag inside
BackPropagateVEC2D (MEDIUM — the fine-grained predicate is inferred from the body alone).

## Proposed wiki enrichment (for `passes/`, do NOT edit `*/wiki/` from here)

Add one grouped detail page plus targeted per-phase sections, all under the existing
`passes/` family:

1. **`passes/validation-and-lowering.md`** — new grouped page "Validation & Lowering
   Passes". Sections:
   - *The OptBudget gate* (`sub_7DDB50`, knob 499, the O0-sentinel semantics) — shared
     infrastructure every phase page can link to.
   - *Profile-virtual dispatch* (base empty-stub pattern, slot table, override addresses).
   - *Per-phase*: OriCheckInitialProgram, PromoteFP16, AnalyzeControlFlow,
     OriCreateMacroInsts, OriSanitize, ConvertVTGReadWrite, DoVirtualCTAExpansion,
     ForwardProgress, EnforceArgumentRestrictions (+ Late pair), ExpandJmxComputation,
     FinalInspectionPass, BackPropagateVEC2D.
2. **`passes/shader-constants.md`** — ExtractShaderConsts{First,Final} (shared worker,
   extract-vs-finalize flag, setup-shader build, loop/block consistency BUG check).
3. Cross-links from `passes/index.md` rows (bin 0/2/3/9/13/39/45/46/49/55/59/94/110/116)
   to the new sections; add a one-line "compute-sanitizer, not a validator" caveat to the
   OriSanitize row.

Suggested page order in the grouped page follows pipeline position
(initial → optimization → final), matching `decoded/ptxas-passes/phase_pipeline.tsv`.
