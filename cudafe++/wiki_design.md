# cudafe++ Documentation Wiki Structure

> **Status:** Historical design sketch (MED confidence). This was the original MkDocs/Material wiki blueprint. The wiki was later rebuilt as an mdBook tree at [`wiki/src/`](./wiki/src/index.md); the canonical table of contents is [`wiki/src/SUMMARY.md`](./wiki/src/SUMMARY.md). Section names, page counts, and templates here no longer match the live tree. Retained for reference on the initial scoping; do not edit -- update the live wiki instead.

Technical documentation for NVIDIA's cudafe++ CUDA frontend compiler (reverse engineered).

## Organization

Pages organized by: topic, architecture version, user role.
Navigation: sidebar hierarchy, tag-based search, cross-references.
Content requirements: specification first, examples second.

## Page Structure

### 1. HOME

`Home.md` - Statistics (29 CUDA attributes, 20 undocumented; 114 __nv_* intrinsics; 515 compiler flags), navigation by role (developer/compiler engineer/researcher), quick links to major topics.

`About-This-Project.md` - RE methodology (IDA Pro, Hex-Rays, Python), legal basis (interoperability, fair use), citation format.

`Glossary.md` - CUDA terms (kernel, grid, block, warp, SM), compiler terms (frontend, IL, AST), EDG terms, acronyms (RDC, PTX, SASS).

### 2. GETTING STARTED

`For-CUDA-Developers.md` - Undocumented attribute usage, optimization tips, common error fixes, architecture-specific tuning.

`For-Compiler-Engineers.md` - Compilation pipeline, EDG IL format, device/host separation, lambda transformation, RDC mechanics, Clang CUDA comparison.

`For-Researchers.md` - Dataset structure (functions, strings, xrefs), analysis examples, data mining methods, extension techniques.

`Installation-and-Setup.md` - Prerequisites, tool installation, extractor execution, dataset access.

### 3. ARCHITECTURE

`Compilation-Pipeline.md` - Seven stages: EDG parsing, device/host separation, attribute propagation, template instantiation, lambda transformation, EDG IL generation, host C++ generation. Data flow, error handling per stage.

`EDG-Frontend.md` - Edison Design Group v6.6, NVIDIA's rationale vs Clang, licensing, C++ standard compliance, configuration.

`EDG-IL-Format.md` - Binary format, type encoding, symbol table, attribute preservation, template metadata, cross-unit references, cicc consumption.

`Device-Host-Separation.md` - Dual compilation tracks, __device__/__host__ tracking, shared code (__host__ __device__), template instantiation differences, symbol resolution.

`Lambda-Transformation.md` - __nv_dl_wrapper_t mechanics, __nv_hdl_create_wrapper_t, capture handling, return deduction, extended lambda support, transformation examples.

`RDC-Mode.md` - Relocatable Device Code (-rdc=true), use cases (dynamic parallelism, separate compilation), device stub generation, nvlink integration, restrictions, performance.

`Template-Instantiation.md` - Device/host separation, generic kernels, specialization, SFINAE in CUDA, metaprogramming limits.

### 4. FEATURES

`CUDA-Attributes-Reference.md` - Documented: __device__, __global__, __host__, __shared__, __constant__, __forceinline__, __noinline__, __launch_bounds__, __restrict__. Undocumented: __grid_constant__, __nv_register_params__, __nv_pure__, __nv_managed_data__. Format: syntax, SM version, examples, caveats.

`Internal-Attributes.md` - 40+ lambda attributes, 30+ atomic variants, memory intrinsics (cvta, memcpy_async), cluster ops (SM_90+), runtime init. Complete reference table.

`Compiler-Flags-Reference.md` - 515 flags by category, -Xcudafe usage, 171 diagnostic flags, EDG options, optimization controls, warnings/errors.

`Architecture-Feature-Matrix.md` - SM_30 to SM_121 feature table: grid constants (SM_70+), clusters (SM_90+), async ops (SM_80+), managed memory (SM_30+), atomics, tensor cores (SM_70+).

`Optimization-Attributes.md` - __launch_bounds__ (register pressure, occupancy, PTX analysis), __grid_constant__ (bandwidth, measurement), __restrict__ (aliasing, vectorization), inline controls.

`Memory-Attributes.md` - __shared__, __constant__, __managed__, __device__, address space qualifiers, hierarchy optimization, __nv_managed_data__.

### 5. GUIDES

`Optimization-Cookbook.md` - Recipes: grid constants for kernel args, launch bounds for register spilling, restrict for vectorization, shared memory access, async copy, clusters (SM_90+). Format: problem/solution/measurement/example.

`Debugging-Guide.md` - Error messages: "undefined in device code", "too many resources", "cannot take address", "copy constructor not callable". Root cause analysis, workarounds.

`Performance-Tuning-Guide.md` - Profiling (Nsight Compute), bottleneck identification, attribute application, arch-specific tuning, measurement, case studies.

`Migration-Guide.md` - SM_70→SM_80 (Ampere), SM_80→SM_90 (Hopper clusters), deprecations, new attributes, regression debugging.

`Lambda-Usage-Guide.md` - Device lambdas, host-device lambdas, extended lambdas, capture semantics, pitfalls, transformation internals, performance.

`RDC-Development-Guide.md` - RDC project structure, CMake config, compilation flags, nvlink, debugging, dynamic parallelism.

### 6. REFERENCE

`Function-Database.md` - 6,483 functions, categories, top 100 by calls, critical function explanations, call graphs, search interface.

`String-Database.md` - 9.5MB strings: errors, warnings, config, feature flags, template instantiation. Search interface.

`Cross-Reference-Database.md` - xref usage, attribute tracking, data flow tracing, call chain analysis, query examples.

`Symbol-Reference.md` - Function symbols, globals, imports, section analysis (.text, .rodata, .data).

`Binary-Format.md` - ELF sections, import/export tables, segment layout, relocations, debug symbols.

### 7. RESEARCH

`Reverse-Engineering-Methodology.md` - IDA Pro config, Hex-Rays workflow, Python extraction, pattern recognition, validation, limitations.

`Dataset-Description.md` - JSON structure, schemas, query methods, Python mining examples, statistics.

`Analysis-Examples.md` - Finding attribute uses, tracing calls, architecture checks, extracting errors, building matrices, notebooks.

`Research-Questions.md` - Open problems, open-source alternatives, optimization opportunities, security, unexplained behavior.

`Comparison-with-Clang-CUDA.md` - Architecture diff, feature parity, performance, attribute mapping, migration.

`Commercial-Compiler-Analysis.md` - EDG licensing, customers (Intel, Microsoft, NVIDIA), commercial vs OSS, technical advantages.

### 8. TOOLS

`Analysis-Tools-Overview.md` - IDA extraction, attribute extractor, flags extractor, matrix generator, error decoder, pattern finder. Format: usage/output.

`Attribute-Extractor.md` - attribute_extractor.py: install, options, inputs, output formats (JSON/Markdown), pattern extension.

`Compiler-Flags-Extractor.md` - compiler_flags_extractor.py: install, examples, categorization, custom patterns.

`Architecture-Matrix-Generator.md` - architecture_features.py: install, usage, feature addition, confidence scoring.

`IDA-Analysis-Script.md` - analyze_cudafe++.py: requirements, execution, output, customization, troubleshooting.

`Data-Mining-Examples.md` - Python/jq examples, SQL import, visualization, custom tool building.

### 9. SM ARCHITECTURES

`SM-30-Kepler.md` - CC 3.0/3.5, managed memory, dynamic parallelism (3.5).

`SM-50-Maxwell.md` - CC 5.0/5.2, shared memory improvements.

`SM-60-Pascal.md` - CC 6.0/6.1, unified memory, cooperative groups, FP16 (6.1).

`SM-70-Volta.md` - CC 7.0/7.2, tensor cores, independent thread scheduling, __grid_constant__, cooperative groups.

`SM-75-Turing.md` - CC 7.5, tensor core improvements.

`SM-80-Ampere.md` - CC 8.0/8.6, async copy (memcpy_async), TF32, FP64 tensor cores (A100).

`SM-90-Hopper.md` - CC 9.0, thread block clusters, cluster barriers, DPX, TMA, __nv_cluster* intrinsics.

`SM-100+-Future.md` - Speculative analysis, binary trends, SM_121 references.

### 10. ADVANCED

`Atomic-Operations-Deep-Dive.md` - 30+ __nv_atomic_* variants, signed/unsigned/float, 2/4/8/16-byte, CAS, fetch ops, performance.

`Address-Space-Conversion.md` - __nv_cvta_* intrinsics: generic↔global/shared/constant/local, use cases, performance, PTX.

`Cluster-Programming.md` - Clusters (SM_90+), __nv_clusterDim/__nv_clusterIdx, barriers, cross-block shared memory, intrinsic reference.

`Async-Memory-Operations.md` - __nv_memcpy_async_shared_global_*, pipeline model, alignment, performance, pitfalls.

`Custom-Compiler-Passes.md` - Pipeline interposition, custom attribute processors, code generation hooks.

`Security-Analysis.md` - Attack surface, input validation, code injection, sanitization, fuzzing.

### 11. DATASETS

`Download-Instructions.md` - Git LFS, download links, checksums, size, license.

`JSON-Schemas.md` - Schemas for: cudafe++_functions.json, cudafe++_strings.json, cudafe++_xrefs.json, cuda_attributes.json, compiler_flags.json, architecture_features.json.

`Binary-Files.md` - IDA database (.i64), disassembly, pseudocode, CFGs, callgraph DOT.

### 12. CONTRIBUTING

`How-to-Contribute.md` - Contribution types, issue templates, PR process, attribution.

`Adding-Attributes.md` - Documentation template, testing, validation, submission.

`Extending-Analysis.md` - Python tool structure, data sources, output standards, testing.

`Improving-Documentation.md` - Writing style, example quality, cross-references, review.

## Navigation

Top bar: Home | Getting Started | Architecture | Features | Guides | Reference | Research | Tools | Architectures | Contributing

Sidebar: Dynamic per section, tree structure (e.g., Architecture → Compilation Pipeline, EDG Frontend, EDG IL Format, Device-Host Separation, Lambda Transformation, RDC Mode, Template Instantiation).

Footer: Quick links (Attribute Reference, Compiler Flags, Architecture Matrix, Optimization Guide, Error Decoder), external (GitHub, issues, citation).

## Search

Full-text across all pages. Specialized: attribute name, function (6,483), string (9.5MB), error message, tag-based (SM arch, category).

Tags per page: audience (developer/compiler-engineer/researcher), level (beginner/intermediate/advanced), SM version (30/50/60/70/75/80/90/100+), category (optimization/debugging/reference/tutorial), status (complete/in-progress/stub).

Related pages: "see also", prerequisites, next steps, external links (NVIDIA docs, papers).

## Templates

### Reference Page
```
# [Feature Name]
Category: [Optimization/Memory/Debugging]
SM Version: SM_XX+
Status: [Documented/Undocumented]
Confidence: [High/Medium/Low]

DESCRIPTION
One paragraph.

SYNTAX
<code block>

PARAMETERS
Table: name, type, description.

REQUIREMENTS
SM version, compiler flags, dependencies.

EXAMPLES
Basic and advanced usage.

PERFORMANCE
Benchmark data, when to use/avoid.

PITFALLS
Common mistakes.

SEE ALSO
Related features, evidence from binary.
```

### Guide Page
```
# [Guide Title]
Audience: [Developer/Compiler Engineer/Researcher]
Level: [Beginner/Intermediate/Advanced]

OVERVIEW
What this covers.

PREREQUISITES
Required knowledge, tools.

STEPS
1. Action with explanation and code
2. Action with explanation and code

COMPLETE EXAMPLE
Full working code.

TROUBLESHOOTING
Common issues, solutions.
```

## Implementation

Platform: MkDocs with Material theme.

Install: `pip install mkdocs-material mkdocs-minify-plugin mkdocs-git-revision-date-localized-plugin`

Configuration: See mkdocs.yml for nav structure, search config, theme settings.

Deployment: GitHub Pages or static hosting.

## Maintenance

Updates: Weekly (new discoveries), monthly (statistics), quarterly (top page review).

Version control: Tag releases, maintain changelog.

Community: GitHub Discussions, PR process, attribution.

## Future

Interactive tools: attribute explorer, call graph visualizer, architecture comparator, code optimizer, error decoder.

Automation: Auto-generate from JSON, nightly CUDA version analysis, diff reports, CI/CD.

Integrations: VS Code extension, nvcc wrapper, Clang plugin, web API.
