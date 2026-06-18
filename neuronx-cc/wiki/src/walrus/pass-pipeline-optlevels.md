# The Walrus Pass Pipeline & the Optlevel Planes

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, binary `neuronxcc/starfish/lib/libwalrus.so` (the cp310 wheel; the cp311/cp312 images carry the same backend code, re-linked at different addresses). `.text` is VA==fileoffset for the backend (`>=0x62d660`); `.data.rel.ro` (`0x3d74780+`) is VMA==fileoffset; plain `.data` carries a `+0x400000` delta. Treat every address as version-pinned.*

## Abstract

The libwalrus backend has its own optimization dial, entirely separate from the user-facing `-O{0,1,2,3}` string (see [3.10](../frontend/opt-level-planes.md)). It is a genuine integer that reaches **8**, and a single `walrus_driver` function — `sub_80D9D0` (`0x80d9d0`, `walrus/driver/src/walrus_driver.cpp`) — reads that integer and *dispatches to one of five pipeline builders*. Each builder calls `BackendPassManager::addPass("<name>")` in a fixed order to assemble a list of `BackendPass` objects out of the pass-generator registry documented in [8.1](backendpass-registry.md). This page is the reimplementation spec for that dispatch: which builder runs at which level, the exact pass sequence each assembles, and the two backend quirks a naive reading gets wrong.

The first quirk is **`arch_legalize`** — registered at pipeline order 93 in three of the builders, named after per-architecture BIR legalization, and a **30-byte no-op stub** that ignores its module argument. It is gated default-off and does nothing even when enabled; the real per-arch work lives elsewhere. The second is the headline: **optlevel 7 is named `"smt"` but invokes no SMT solver.** There is no Z3, no GMP arithmetic, no constraint encoding anywhere in the binary. `--smt-allocation` is a `cl::opt` flag that forces optlevel→7, whose pipeline is the ordinary graph-coloring family of passes *reordered*, plus a hard in-place re-verification (`Unroll::check_in_place`). The "smt" name is vestigial nomenclature for a defunct solver-based placer. Both quirks are detailed in §4 and §5.

| | |
|---|---|
| **Dispatcher** | `sub_80D9D0` @ `0x80d9d0` — optlevel `switch`, NOT an "SMT builder" |
| **PassOptions packer** | `run_backend_driver` @ `0x8113b0` — packs each `cl::opt` byte into `PassOptions` |
| **`cl::opt` setup** | `sub_7C2890` @ `0x7c2890` — `walrus_driver` main; registers `smt-allocation`, `enable-arch-legalize`, … |
| **Builders** | opt0 `sub_806F80` · opt1/2/3 `sub_80A6E0` · opt6 `sub_807EF0` · opt7 *inline* · opt8 `sub_809580` |
| **Always-run tail** | `sub_805870` (low-level backend: `coloring_allocator_reg`, lowering, codegen) |
| **`arch_legalize`** | `ArchLegalize::run(bir::Module&)` @ `0x1733910` — 30-byte no-op; gated `enableArchLegalize` @ `0x3df6340` (default false) |
| **`smt` flag** | `SmtAllocation` `cl::opt<bool>` @ `0x3dfa180` (BSS); forces optlevel→7 |
| **`smt` re-check** | `Unroll::check_in_place(bir::Function&)` @ `0xb361f0` (gate = `PassOptions+0x6D`) |

---

## 1. The dispatcher — `sub_80D9D0`

`run_backend_driver` (`0x8113b0`) builds a `PassOptions` object from the LLVM `cl::opt` globals (packing each flag's SSO value byte `[15]` into a `PassOptions` field), constructs the `BackendPassManager`, then calls the dispatcher `sub_80D9D0`. The dispatcher's source is `walrus/driver/src/walrus_driver.cpp` (the path string survives in the body). It is a flag-normalization prologue followed by a `switch` on the normalized optlevel — **not** a solver, not an "SMT builder" despite the task name that motivated this page. **CONFIRMED** — the dispatcher tail-jumps to each builder; the `jmp` (not `call`) targets are read directly from the disassembly below.

### 1.1 Flag normalization prologue

Before the switch, `sub_80D9D0` rewrites the requested optlevel from three other backend flags. Order matters: the loop flag is applied first, then `smt` overrides it.

```c
// sub_80D9D0 prologue — walrus_driver.cpp normalization (CONFIRMED, decompile)
assert( allocator.empty() || allocator == "coloring" || allocator == "lsa" );  // (a)

if ( loopsInBackend[15] )           // "Keep loops in Backend" cl::opt          // (b)
    optlevel = (optlevel == 2) ? 6 : 8 ,  auxflag = 4 ;

if ( SmtAllocation[15] )            // "Doing smt allocation" cl::opt           // (c)
    optlevel = 7 ;                  //   → also fires the optlevel cl::opt callback

// (d) LSA / opt0 / opt8 are SINGLE-vNC-core ONLY:
assert( vncNcCount == 1 || (allocator != "lsa" && optlevel != 0 && optlevel != 8) );
```

`SmtAllocation` (the `cl::opt<bool>` object @ `0x3dfa180` in BSS) is read at `0x80de76` inside the dispatcher (`mov 0x35b6a5b(%rip),%rax  # SmtAllocation`). Setting `--smt-allocation` therefore does exactly one thing here: it forces `optlevel = 7`. Constraint (d) is the reason opt0, opt8, and the linear-scan allocator are rejected when more than one virtual-NeuronCore is present — they have no multi-core split path.

### 1.2 The switch

```c
switch ( optlevel ) {                                  // CONFIRMED — jmp targets disasm'd
  case 0:        tail-jmp sub_806F80(a1,a2,…);          //  minimal builder (~28 passes)
  case 1: case 2: case 3:
                 tail-jmp sub_80A6E0(a1,a2,…);          //  primary pre-codegen (~131 passes)
  case 6:        tail-jmp sub_807EF0(a1,a2,v339);       //  coloring_allocator_with_loop (8.1/D-K01)
  case 7:        /* INLINE pipeline — no builder fn */  //  the "smt" path (§5); ends → sub_805870
                 …~70 addPass(...) calls inline…
  case 8:        tail-jmp sub_809580(a1,a2,…);          //  linear_scan_allocator (~35 passes; 8.21)
}
// every level then runs the always-run low-level backend:
sub_805870(...)   // coloring_allocator_reg @ order 33, lowering, codegen
```

Disassembly of `sub_80D9D0`'s body confirms five distinct `jmp` targets — `806f80`, `80a6e0`, `807ef0`, `809580`, and the always-run `805870` — each a real function (every one opens with a `push %r15` / `push %rbp` prologue). Case 7 has **no** builder `jmp`: it constructs its entire ~70-pass pipeline inline in the dispatcher body, then converges on `sub_805870` like every other arm. **CONFIRMED.**

### 1.3 The always-run low-level tail — `sub_805870`

After whichever optlevel pipeline runs, the driver *always* invokes `sub_805870` (`add_lowlevel_backend_passes`). This is where machine-level lowering and the scalar register allocator live, because scalar SP-engine registers only exist after the per-engine machine-instruction expansion. The relevant tail order:

```text
… lower_branch → synchronizer → alloc_semaphores → … lower_sync → lower_act → lower_dve →
   lower_ap → coloring_allocator_reg (order 33) → vnc_remote_addr_map → vnc_link →
   branch_hint → … → codegen (order 40)
```

`coloring_allocator_reg` (the `REG_Allocator`, a Chaitin–Briggs colorer of `bir::Register*`) is optlevel-**independent** — it always runs here. Its algorithm is out of scope for this page; see [8.21](alt-allocators.md). The point for the pipeline reader is that the always-run tail makes every optlevel pipeline a *prefix*: the level only chooses the SBUF/PSUM/DRAM allocation strategy and the surrounding optimization passes; register allocation and codegen are constant across all levels.

---

## 2. The optlevel → builder map

| `--optlevel` / forcing flag | walrus optlevel | builder | SBUF/PSUM allocator | DRAM allocator | REG (always) |
|---|---|---|---|---|---|
| `--optlevel 0` | 0 | `sub_806F80` | (minimal; mem reservation) | `coloring_allocator_*` | `coloring_allocator_reg` |
| `--optlevel 1/2/3` | 1/2/3 | `sub_80A6E0` | `coloring_allocator_{psum,sb}` | `coloring_allocator_*` | `coloring_allocator_reg` |
| `--optlevel 2` + `--keep-loops` | 6 | `sub_807EF0` | `coloring_allocator_with_loop` | `coloring_allocator_*` | `coloring_allocator_reg` |
| `--optlevel 6` | 6 | `sub_807EF0` | `coloring_allocator_with_loop` | `coloring_allocator_*` | `coloring_allocator_reg` |
| opt6 + `--allocator=lsa` | 6 | `sub_807EF0` | `linear_scan_allocator` (+`coloring_allocator_sb` keyed) | `coloring_allocator_*` | `coloring_allocator_reg` |
| `--smt-allocation` | 7 | *inline* | `coloring_allocator_sb` + dma-opt/rotation | `coloring_allocator_{dram,dram_shared,dram_debug}` | `coloring_allocator_reg` |
| `--optlevel 8` / `--keep-loops`(≠2) | 8 | `sub_809580` | `linear_scan_allocator` (sole) | `coloring_allocator_dram_debug` | `coloring_allocator_reg` |

The map's controlling insight is that **the optlevel selects the SBUF/PSUM allocation strategy, not the depth of optimization** in the usual `-O` sense. opt6 swaps in the loop-aware coloring allocator; opt8 swaps in linear-scan; opt7 reorders the standard coloring family. The general-purpose optimization passes (DCE, peephole, reordering, constant propagation) are present in some form at every nonzero level. **CONFIRMED** (selection map; allocator identities cross-checked against the pass-name string sets each builder constructs).

---

## 3. The per-optlevel pipelines

Each builder adds passes by **name** via `BackendPassManager::add{Mod,Subgraph,Core}ParallelPass(name,…)`; the name string is assembled by `sub_7FAE40` and resolved in the `GeneratorRegistration` registry (the singleton hashtable of `name → function<unique_ptr<BackendPass>(PassOptions const&)>`, [8.1](backendpass-registry.md)). The `memory_analysis_after_<X>` snapshot passes are inserted conditionally when `<X>` is a member of the `--memory-analysis-after=` set (membership test `sub_7FC770` over `byte_3DFD800`). The orderings below are transcribed in emission order from the builder bodies; bracketed `[name]` entries are flag-gated.

### 3.1 opt0 — `sub_806F80` (minimal, ~28 passes)

The opt0 builder is the bare-survival pipeline. It does enough to produce a legal NEFF without quality optimization: memory reservation, the unrollers needed for static allocation, a single coloring allocation pass per space, and the schedule/legalize tail. It is single-vNC only (constraint (d) above). **CONFIRMED** (28-pass count read from the builder; this is the minimal-builder arm of the `switch`).

### 3.2 opt1/2/3 — `sub_80A6E0` (primary pre-codegen, ~131 passes)

This is the default production pipeline (`-O2`, and `-O1`/`-O3` map here too). At ~131 passes it is the densest builder. Internally `optlevel` 1, 2, and 3 produce the **same** builder — the dispatcher's `case 1: case 2: case 3:` fall through to one `sub_80A6E0` call. Differentiation between 1/2/3 is by individual flag toggles (`dead_code_elim_o0` vs `_o1`, peephole aggressiveness), not by a different pass list. The `arch_legalize` gate read for this builder is at `0x80bac6` (§4). **CONFIRMED.**

### 3.3 opt6 — `sub_807EF0` (`coloring_allocator_with_loop`)

opt6 is the loop-aware allocation pipeline, selected by `--optlevel 6` or by `--keep-loops` when the user level is 2. Its defining trait is the SBUF/PSUM allocator: `coloring_allocator_with_loop` (a `ColoringAllocatorWithLoop` that flattens PSUM→SB onto one `LinearizedFunction` and runs Chaitin–Briggs over the loop-aware live ranges). With `--allocator=lsa` the same builder substitutes `linear_scan_allocator` for the with-loop colorer. The opt6 order (for the opt7 diff in §5) is:

```text
[birverifier, lnc_verifier] [bir_sim] → dynamic_dma_cleanup → [expand_replication] → lower_ac →
flatten_small_loops → [loop_optimization] → sb_size_legalization → full_unroll_some →
heuristic_unroll → pre_sched → pre_sched (+memory_analysis_after_pre_sched) →
{ (--allocator=="lsa") ? linear_scan_allocator : coloring_allocator_with_loop }      ← THE allocator
(+memory_analysis_after_coloring_allocator_sb)  →  address_tensor_insertion →
address_tensor_content_dump → lower_symbolic_inst → peephole_opts → lower_select →
build_fdeps → remove_redundancies → anti_dependency_analyzer → [post_sched ×2] →
legalize_mm_accumulation_groups → [full_unroll_all] → [bir_sim] → anti_dependency_analyzer →
dep_opt → post_sched (+memory_analysis_after_post_sched) → [bir_sim] → [debug_onchip] →
report_stats → [debug_init_mem] → assign_trigger_engine → lower_local_collectives →
[insert_ptcom_flat] → extend_shared_lifetimes → dead_code_elim_o1 → localize_shared_memory →
[birverifier, lnc_verifier] → [sync_before_global_cc] → expand_device_print →
coloring_allocator_dram_debug → assign_hwdge_engine → alloc_queues → chain_dma_transposes →
[lnc_barriercheck]
```

> **GOTCHA — the `memory_analysis_after_coloring_allocator_sb` snapshot has no matching `coloring_allocator_sb` pass in opt6.** The mem-analysis pass is keyed on the *name* `"coloring_allocator_sb"` only for the set-membership test, but **no** `coloring_allocator_sb` pass is added in opt6 — the SB+PSUM coloring **is** the `coloring_allocator_with_loop` pass. A reimplementer keying the snapshot on the presence of an SB-colorer pass would misplace it.

opt6's DRAM allocator is **only** `coloring_allocator_dram_debug`. **CONFIRMED.**

### 3.4 opt8 — `sub_809580` (`linear_scan_allocator`, ~35 passes)

opt8 is the linear-scan pipeline — leaner than opt6/opt7, single-vNC only. `linear_scan_allocator` (the `LinearScanAllocator`, a `MemoryLocationSet` interval allocator backed by a best-fit 2-D placement manager) is the **sole** SBUF/PSUM allocator; DRAM still uses `coloring_allocator_dram_debug`. Full order:

```text
dynamic_dma_cleanup → expand_replication → lower_ac → loop_optimization → sb_size_legalization →
full_unroll_some → address_tensor_insertion → pre_sched → linear_scan_allocator → lower_select →
address_tensor_content_dump → lower_symbolic_inst → post_sched → legalize_mm_accumulation_groups →
full_unroll_all → bir_sim → dep_opt → debug_onchip → report_stats → debug_init_mem →
assign_trigger_engine → lower_local_collectives → insert_ptcom_flat → extend_shared_lifetimes →
dead_code_elim_o1 → localize_shared_memory → sync_before_global_cc → expand_device_print →
coloring_allocator_dram_debug → assign_hwdge_engine → alloc_queues → chain_dma_transposes →
lnc_barriercheck
```

The allocator and pipeline mechanics are out of scope here (see [8.21](alt-allocators.md)). **CONFIRMED** (pass ordering from `sub_809580`).

---

## 4. CORRECTION — `arch_legalize` is a dormant no-op stub

`arch_legalize` registers the C++ pass class `ArchLegalize` and sits at pipeline **order 93** (after `post_sched`=92, before `legalize_mm_accumulation_groups`=94) in three builders. Its name and `cl::opt` help text — *"Enable legalization pass that transforms BIR to be compatible with the latest architecture version being tested"* — promise per-architecture BIR legalization right before codegen. **In 2.24 it does nothing.**

### 4.1 The stub body — verbatim

`ArchLegalize::run(bir::Module&)` @ `0x1733910` is a 30-byte function. Disassembled directly from the binary:

```text
1733910:  48 8d 57 18         lea    0x18(%rdi),%rdx    ; rdx = &inline SSO buffer (rdi+0x18)
1733914:  c7 07 00 00 00 00   movl   $0x0,(%rdi)        ; result.status = 0  (Success)
173391a:  48 89 f8            mov    %rdi,%rax           ; return the RVO'd result slot
173391d:  48 89 57 08         mov    %rdx,0x8(%rdi)      ; result.msg.ptr = SSO buffer
1733921:  48 c7 47 10 00…     movq   $0x0,0x10(%rdi)     ; result.msg.len = 0
1733929:  c6 47 18 00         movb   $0x0,0x18(%rdi)     ; result.msg[0] = '\0'  (empty string)
173392d:  c3                  ret
```

```c
// ArchLegalize::run — the Module argument (%rsi) is NEVER touched   [CONFIRMED, disasm+decompile]
PassResult ArchLegalize::run(bir::Module& m /* UNUSED */) {
    return PassResult{ .status = 0, .message = std::string("") };
}
```

The `%rsi` register (the `bir::Module&`) is never read. The body does not walk instructions, does not read the `ArchLevel`, carries no per-arch rewrite tables. It constructs the same empty `PassResult` (`{i32 status; char* ptr → SSO; size_t len; char[] SSO}`) that every other `BackendPass` returns on its no-error path. The `ArchLegalize` vtable @ `0x3d97f90` overrides exactly the destructors and `run(bir::Module&)`; `run(vector&)`, `dry_run`, and `isRealPass` are **inherited** unchanged from `BackendPass`. The pass adds no virtual methods. **CONFIRMED** (disassembly + nm `_ZN…ArchLegalize…` symbol set + vtable relocs).

### 4.2 The gate — default off, arch-independent

The pass is added only when the `cl::opt<bool>` `enableArchLegalize` (@ `0x3df6340`) is set:

```c
// identical gate in every builder, e.g. opt1-3 sub_80A6E0 @0x80bac6:
if ( enableArchLegalize.value )                 // value byte at Option+0x78
    addModParallelPass("arch_legalize");        // order 93 — added ONLY if flag set
```

The flag's arg string is `"enable-arch-legalize"`, registered **hidden** (a dev option), with the value byte at `Option+0x78` initialized to 0 in the ctor — **default false**. The gate is a `cmpb $0x0,0x78(%rax)` *read* (never a write) at exactly four sites, confirmed by the rip-relative `enableArchLegalize` annotation in the disassembly:

| site | address | builder / role |
|---|---|---|
| `sub_806F80` | `0x8072eb` | opt0 / opt6 / opt8 path |
| `sub_80A6E0` | `0x80bac6` | opt1 / opt2 / opt3 builder |
| `sub_80D9D0` | `0x80f2af` | opt7 inline path |
| `run_backend_driver` | `0x8113ed` | top-level driver (dev-only log path) |

(plus the `cl::opt` registrar at `0x7c7e14`). No arch-detection codepath ever writes `Option+0x78` to true — the byte is written only by the generic `cl` parser when the user passes `--enable-arch-legalize` on the command line. The gate is purely the `cl::opt` bool, **independent of `ArchLevel`**: the pass is skipped on all architectures by default, and if enabled runs (as a no-op) on all architectures uniformly. **CONFIRMED.**

### 4.3 Where the real per-arch legalization lives

Because order 93 is empty, the per-arch BIR legalization a reimplementer expects is **distributed** across other machinery the dormant slot is positioned to one day subsume:

- The **`ArchLevel → CoreV{2,3,4}Gen` projection** at codegen (`initCodegen` @ `0xfc5d00` reads `Module+0xAC`: `0x14`→V2/sunda, `0x1E`→V3/gen3, `0x28`→V4/core_v4). This selects the per-generation encoder downstream of order 93.
- The **per-op selective overrides** in the GenImpl encoders — `CoreV3GenImpl` overrides 13 ops, `CoreV4GenImpl` overrides 5 (e.g. `visitInstMatmultMx` decomposes one BIR `MatmultMx` into two TPB bundles `LdWeightMx`+`MatmulMx`). This is per-gen op rewrite applied at *emit* time, not as a BIR pass.
- The **op-specific legalize passes** that run *before* order 93 and do read the arch model: `legalize_strided_dma` (order 18), `legalize_cce_dma` (order 19), `lower_generic_indirect` (order 10).
- The **legality gates** (L1 `birverifier` `checkArchLevel` @ `0xfd2540`; L2 wire validator) which **reject** arch-illegal BIR rather than rewrite it.

So `arch_legalize` is a reserved forward-compat umbrella slot for a future *unified* per-arch BIR rewrite — dormant in 2.24, with the real burden carried by the codegen encoders and the upstream legalize passes. **CONFIRMED** (slot empty; distribution targets cross-referenced).

---

## 5. The headline QUIRK — optlevel 7 is named `"smt"` but is NOT an SMT solver

The task that motivated this page assumed optlevel 7 invokes Z3 to find an *optimal* allocation, with an UNSAT/timeout fallback. **That premise is false for this build.** There is no SMT solver, no constraint encoding, no objective, and no fallback path anywhere in libwalrus. `--smt-allocation` is a flag; opt7 is a reordered graph-coloring pipeline with a hard in-place certifier.

### 5.1 The falsification — no solver in the binary

| probe | result |
|---|---|
| `nm -D libwalrus.so \| rg 'Z3_mk\|Z3_solver\|Z3_check\|Z3_ast\|__gmpz\|__gmpq\|mpz_init\|CVC4\|cvc5\|Yices'` | **EMPTY** (zero matches) |
| `objdump -p` DT_NEEDED | lists `libgmp.so.10`, but it is a **transitive** dependency (pulled by archive/xml2/crypto) with **zero** call sites in libwalrus's own `.text` |
| `rg -a -o 'smt…'` (all "smt" artifacts) | exactly four: the flag arg `"smt-allocation"`, the description `"Doing smt allocation"`, the BSS `cl::opt<bool>` `SmtAllocation`, and the assert literal `"smt allocation fails in-place write checker"` |
| runtime strings `unsat` / `solver` / `objective` / `constraint encoding` / `theorem` | **none present** |

The `z3`/`Z3` raw-string hits that exist are binary noise (substrings like `"3A3F3K3P3U3Z3"`, `"5BZ3"`), not API symbols. **CONFIRMED** — re-verified directly here: `nm -D` returns empty for all solver/GMP symbol patterns; the only solver-shaped DT_NEEDED entry (`libgmp`) has no call sites.

> **The verdict.** `--smt-allocation` does exactly one thing in the dispatcher: force `optlevel → 7` (the `SmtAllocation` read at `0x80de76` in `sub_80D9D0`). opt7 is then a *normal* `BackendPassManager` pipeline built from the family-(a) graph-coloring passes `coloring_allocator_{dram,dram_shared,sb}` (the same Chaitin–Briggs allocators every other level uses) — *reordered* relative to the standard pipelines — plus the post-allocation in-place re-verification `Unroll::check_in_place`. No encoding, no objective, no UNSAT/timeout, no fallback.

### 5.2 What opt7 actually does — the inline pipeline

opt7 is the only optlevel whose pipeline is *inlined* in `sub_80D9D0` (no separate builder fn). It opens with two hard entry guards, then constructs ~70 passes:

```c
// case 7 entry guards (walrus_driver.cpp:1451/1452)   [CONFIRMED — strings present]
assert( !enablePartitioner );   // smt path forbids the partitioner   (NeuronAssertion → SIGABRT)
assert( !enableCallGraph  );    // and forbids call-graph mode
```

i.e. opt7 is an experimental single-graph, no-partition, no-callgraph mode. The pass sequence (allocator-relevant structure; bracketed entries are flag-gated):

```text
[birverifier, lnc_verifier]
lower_ac → unroll → lower_generic_indirect → dead_code_elim_o1 → localize_shared_memory
[birverifier, lnc_verifier, oom_checker]
unroll (+memory_analysis_after_unroll)            ← 2nd unroll: Unroll::check_in_place fires here (§5.3)
instruction_reorder → constant_propagate → psum_legalization
[dynamic_dma_setup] → runtime_memory_reservation
┌─ if !disable-dram-allocation:
│   dma_optimization_psum → address_rotation_psum
│   [dma_optimization_sb (+memory_analysis_after_dma_optimization_sb) → address_rotation_sb]
│   address_rotation_psum (+memory_analysis_after_address_rotation_psum)
│   coloring_allocator_sb (+memory_analysis_after_coloring_allocator_sb)   ← THE SB COLORER (family-(a))
│   address_rotation_sb
└─ (DRAM block):
    [coloring_allocator_dram] → [address_rotation_dram] → tensorcopy_accel → [peephole_opts] →
    inline_bir_kernel → inline_nki_kernel → lower_select → dynamic_dma_cleanup → build_fdeps →
    remove_redundancies → anti_dependency_analyzer → dead_code_elim_o0 → localize_shared_memory →
    [lower_local_collectives → extend_shared_lifetimes → coloring_allocator_dram_shared →
     sync_shared_allocations → anti_dependency_analyzer_post_shared_dram →
     coloring_allocator_dram_shared (+memory_analysis)]   (lnc > 1)
    perf_sim → post_sched ×2 → arch_legalize → legalize_mm_accumulation_groups →
    [insert_ptcom_flat] → expand_scheduling_units → … → coloring_allocator_dram_debug →
    assign_hwdge_engine → alloc_queues → chain_dma_transposes → [lnc_barriercheck]
→ sub_805870   // always-run low-level tail (REG_Allocator, codegen)
```

The four distinctive opt7 traits:

| trait | opt7 | contrast |
|---|---|---|
| SB/PSUM colorer | `coloring_allocator_sb` (plain family-(a) `ColoringAllocator`, Type SB) | opt6 = `coloring_allocator_with_loop`; opt8 = `linear_scan_allocator` |
| DMA-opt / addr-rotation | run **before** `coloring_allocator_sb` (psum & sb) | standard pipelines rotate **after** each colorer — this is the literal "reorder" |
| PSUM coloring | **none** — no `coloring_allocator_psum` pass; PSUM handled via dma-opt + rotation + the in-place check | opt1-3 add a standalone PSUM colorer |
| DRAM allocator | first-class: `coloring_allocator_dram` + `coloring_allocator_dram_shared`(×2) + `_dram_debug` | opt6/opt8 have only `coloring_allocator_dram_debug` |

So opt7 is **not** "the same passes reordered." It swaps the loop-aware colorer for the plain SB colorer, drops standalone PSUM coloring, promotes DRAM coloring to first-class, and front-loads DMA-opt + address-rotation so the colorer runs on already-rotated (multi-buffered) addresses — then depends on `check_in_place` to prove the resulting in-place writes are still legal at the fixed physical slots. **CONFIRMED** (pipeline order; "STRONG" on the *why* of the reorder).

### 5.3 `Unroll::check_in_place` — the "smt" re-verification

The only "smt"-named runtime behavior is `Unroll::check_in_place(bir::Function&)` @ `0xb361f0`, whose sole caller is `Unroll::run` @ `0xb42f30` (the `unroll` pass body). It walks `bir::Function::allocs()` (a transformed range over the function's `MemoryLocationSet` list, projecting each to its representative `bir::MemoryLocation&`) and, for every in-place location, re-runs `MemoryLocation::allocate` at its **exact** recorded physical tuple:

```c
// Unroll::check_in_place — per in-place MemoryLocation   [CONFIRMED, full body read]
for ( bir::MemoryLocation& mem : fn.allocs() ) {
    if ( *(uint8_t*)(&mem + 0xA8) ) {                // +0xA8 = the in-place / materialized flag
        bool ok = mem.allocate( mem.getType(), mem.getAddrSpace(), mem.getAddress(),
                                mem.getBankId(), mem.getBasePartition(), mem.isPinned(),
                                &accessPatternVec );  // 7th arg = the overlap/conflict collector
        // allocate() returns FALSE iff that physical slot is already claimed by another
        // live tensor under the post-coloring assignment (an alias / overlap collision):
        assert( ok && "smt allocation fails in-place write checker" );   // unroll.cpp:0x3AE
    }
}
```

This is a **dynamic in-place-write safety check**: after the opt7 colorer + address-rotation have committed physical addresses, it proves every in-place (read-modify-write) tensor still owns its slot without aliasing — the runtime twin of the libBIR structural verifier `checkWriteInPlaceValidity` @ `0x204290`. It is **not** constraint solving.

The check is gated: `Unroll::run` reads `PassOptions` byte `+0x6D` (109) — the persisted `--smt-allocation` boolean (the same bit packed at `run_backend_driver` `0x8113b0`) — and only then calls `check_in_place`. In opt0/1-3/6/8 the gate byte is 0 and the call is skipped entirely. Because `unroll` appears **twice** in the opt7 pipeline, the re-check runs after the address-rotated / pre-coloring layout is in place (the 2nd `unroll`). **CONFIRMED** (sole call site; gate byte; assert literal verified in the binary).

> **No fallback on failure.** A collision is a libc `__assert_fail` (not a recoverable `NeuronAssertion`) → SIGABRT / `std::terminate`. There is no try/catch, no spill-and-retry, no alternate allocator. This is consistent with the smt assignment being treated as **pre-determined**: a collision is a developer/invariant bug, not a register-pressure condition to be spilled. Contrast the coloring allocators' soft failure surface ("couldn't allocate every tensor in SB" / spill-fixpoint), which *recovers* by spilling.

### 5.4 Why "smt" — the vestige verdict

There is no live solver, no dead solver stub, and no removed-Z3 symbol residue (no orphan `solve`/`encode`/`unsat` function with no callers). The strongest reading: `"smt allocation"` is a developer/experimental mode in which the physical allocation is treated as **fixed and pre-determined** — each `MemoryLocation` already carries its target type/addr/bank/partition/pinned tuple — and the backend's job is to **reproduce** it in place and **prove** it consistent (`check_in_place`), not to *search* for it. The pipeline shape supports this: it drops the search-oriented loop-aware colorer, front-loads address-rotation, runs the plain SB colorer, and hard-asserts the in-place result; and it forbids the partitioner/call-graph (a hand-/pre-placed graph). Since no external-allocation-file string survives in this build, the pre-solved addresses must arrive embedded in the input BIR (upstream Python/NKI emitting `MemoryLocation`s already carrying physical tuples) — **INFERRED**. The name "smt" plausibly refers to an earlier design where the fixed assignment was produced by an SMT/ILP placer; in 2.24 that producer is gone from libwalrus, leaving **vestigial nomenclature**. **STRONG** (verdict) / **INFERRED** (source of the fixed assignment).

---

## 6. Function & symbol map

| identity | address | role |
|---|---|---|
| `sub_80D9D0` | `0x80d9d0` | optlevel dispatcher (`switch`); case 7 inlined |
| `run_backend_driver` | `0x8113b0` | builds `PassOptions` from `cl::opt` globals; packs smt bit at `+0x6D` |
| `sub_7C2890` | `0x7c2890` | `walrus_driver` main; `cl::opt` setup (`smt-allocation`, `enable-arch-legalize`) |
| `sub_806F80` | `0x806f80` | opt0 builder (~28 passes) |
| `sub_80A6E0` | `0x80a6e0` | opt1/2/3 builder (~131 passes) |
| `sub_807EF0` | `0x807ef0` | opt6 builder (`coloring_allocator_with_loop`) |
| `sub_809580` | `0x809580` | opt8 builder (`linear_scan_allocator`, ~35 passes) |
| `sub_805870` | `0x805870` | always-run low-level tail (`coloring_allocator_reg` @ order 33, codegen) |
| `ArchLegalize::run(bir::Module&)` | `0x1733910` | 30-byte no-op stub (order 93, gated) |
| vtable for `ArchLegalize` | `0x3d97f90` | overrides only dtors + `run`; rest inherited from `BackendPass` |
| `enableArchLegalize` (`cl::opt<bool>`) | `0x3df6340` | `"enable-arch-legalize"`, hidden, default false; value byte `+0x78` |
| `SmtAllocation` (`cl::opt<bool>`) | `0x3dfa180` (BSS) | `"smt-allocation"` / `"Doing smt allocation"`; forces optlevel→7 |
| `Unroll::check_in_place(bir::Function&)` | `0xb361f0` | smt in-place re-verifier; assert `unroll.cpp:0x3AE` |
| `Unroll::run(bir::Module&)` | `0xb42f30` | the `unroll` pass; sole caller of `check_in_place`, gate `PassOptions+0x6D` |
| `coloring_allocator_sb` ctor | `0x9870c0` | opt7's SB colorer (`ColoringAllocator`, Type SB) |
| `initCodegen` (ArchLevel→GenImpl) | `0xfc5d00` | the real per-arch projection arch_legalize precedes |

## Cross-References

- [3.10 The Opt-Level Planes](../frontend/opt-level-planes.md) — the four incompatible "opt level" dials; the user `-O` string vs this walrus integer (plane 4).
- [8.1 The Backend Pass Registry](backendpass-registry.md) — `BackendPass` / `ContainerPass` / `*ForkPass`, `GeneratorRegistration`, and the 150 register-name → 121 class map these pipelines pull from.
- [8.21 The Alternative Allocators](alt-allocators.md) — `REG_Allocator`, `LinearScanAllocator`, `BestFitMemoryManager`, and the coloring family the optlevels select *(planned)*.
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where `WalrusDriver` (one Job) and `--walrus-passes` sit in the driver job schedule.
