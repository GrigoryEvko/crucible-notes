# Profile-Guided Auto-Tuning (PGA) Feedback Path

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds). The native code lives in `neuronxcc/starfish/lib/libwalrus.so`; the Python gate lives in the Cython modules `neuronxcc/driver/commands/CompileCommand.cpython-310-…so` and `neuronxcc/driver/jobs/WalrusDriver.cpython-310-…so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `0x5e9020`–`0x62d650` is the `.plt` thunk band, so every `call …@plt` cited here targets a thunk whose real body lives elsewhere — the cited `0xd6xxxx`/`0xd7xxxx` addresses are the real bodies of the PGA functions themselves. `.data` carries a +0x400000 delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

`ProfileGuidedAutoTuning` (PGA) is the walrus backend's parameter search around the VN-Splitter. It exists to answer one question: *which `(SB-size level, split threshold)` pair makes the VN-Splitter produce the cheapest schedule for this module?* It answers it the simplest way a search can — by **brute force**. `runPGA` (`@0xd6e680`) enumerates an **8 × 8 = 64-candidate grid** of `(level, threshold)` cells, compiles and simulates each candidate independently, reads back a `(memeff, cycles)` score per candidate, and logs the results. There is no annealing, no SPSA, no greedy descent, and no temperature schedule — the search space is small enough to evaluate exhaustively, and that is exactly what it does.

Each grid cell is one call to `runVNSplitterOnce(50000, threshold, level)` (`@0xd6f440`), dispatched through `std::async` over an independent **clone** of the PGA object, so the 64 candidates run as 64 `std::future<std::tuple<float,int>>` in parallel and never share mutable module state. A candidate re-loads the baseline module from a serialized BIR snapshot, runs `VNSplitter::runTransform` with the cell's `(threshold, level)`, re-fuses with `VerticalFusion::runTransform`, then runs `PerformanceProfiler::runProfile` to get an in-process `(memeff, cycles)` estimate — the same perf-sim metric serialized elsewhere as `tensorizer_metric_store.json` (the `metricstore` page, in flight, covers the serialized form), read here straight out of the live profiler result struct rather than from a file.

The metric is the VN-Splitter knobs the search tunes; the [`vnsplitter-shrink`](vnsplitter-shrink.md) page documents the pass itself (pass 24, `vn_splitter`, whose standalone body runs **split-then-fold** — `VNSplitter::runTransform` first, then `VerticalFusion::runTransform`). The PGA candidate body runs the **same** two transforms in the **same** split-then-fold order, so there is no order difference between the standalone pass and the autotuner candidate (see the [§2 QUIRK](#2-runvnsplitteronce--one-candidate-confirmed)). PGA is a **grid search**, and it should not be confused with the separate penguin-frontend MCTS autotuner (the [`penguin-autotuner`](penguin-autotuner.md) page) which searches a tile/schedule space with Monte-Carlo tree search — a fundamentally different algorithm. The two share the word "autotuner" and nothing else.

> **CRITICAL — the winning-config commit path is UNRESOLVED.** `runPGA` only **logs** each candidate's `(memeff, cycles) → (level, threshold)`. It performs **no** `comiss`/`minss` argmin over the 64 results, computes **no** scalar reward, and the result vectors are simply freed in the epilogue. *Whether and how the best configuration is selected and re-applied to the real module is not proven in this binary.* `run(bir::Module&)` (`@0xd713f0`) — the `Pass`-interface entry — is a 14-byte stub that returns an empty result. Do not assume PGA commits anything; treat it, on the evidence here, as a **dispatcher + telemetry logger** whose optimization effect (if any) is a side effect inside `VNSplitter::runTransform`'s own per-split accept rule. See [§5](#5-the-commit-path-unresolved).

## Reimplementation contract

To rebuild the PGA feedback path you must reproduce:

- **The grid**: an outer loop over 8 `level` values `{2560, 5120, …, 20480}` (step 2560) and an inner loop over 8 `threshold` steps `{105, 130, …, 280}` (step 25), the threshold being `step / 100.0f`. 64 cells, not annealing.
- **The fan-out**: each cell launches `runVNSplitterOnce(50000, threshold, level)` via `std::async` (default `launch::async|deferred`) on a **copy** of the PGA object; futures collected into a `vector<future<tuple<float,int>>>`.
- **The candidate body**: load baseline module → `VNSplitter::runTransform` → `VerticalFusion::runTransform` → `PerformanceProfiler::runProfile`; return `tuple<float memeff, int cycles>` with a `-1.0f` failure sentinel for `memeff`.
- **The join**: wait each future, read `(cycles@+0, memeff@+4)`, format `"(PGA) memeff = … , cycles = … --> (level, threshold)"`.
- **The gate**: the Python `enable_bir_vnsplitter` option (CLI `--enable-bir-vnsplitter`). There is **no** `--enable-pga` flag and **no** numeric `level` flag — the grid is hard-coded.

| | |
|---|---|
| **Component** | `neuronxcc::backend::ProfileGuidedAutoTuning` |
| **Grid driver** | `runPGA()` `@0xd6e680` (`_ZN9neuronxcc7backend23ProfileGuidedAutoTuning6runPGAEv`) |
| **Candidate** | `runVNSplitterOnce(int, float, int)` `@0xd6f440` → `std::tuple<float,int>` |
| **Pass shim** | `run(bir::Module&)` `@0xd713f0` (14-byte stub) |
| **Clone ctor** | copy-ctor `@0xd73060`, called from grid at `d6e88b` |
| **vtable / typeinfo** | `0x3d8f7b8` / `0x3d8f638` |
| **Grid size** | 8 levels × 8 thresholds = **64** candidates |
| **Budget literal** | `0xc350` = **50000** (constant, never swept) |
| **Metric source** | `PerformanceProfiler::runProfile` `@0xd6b080` (in-process; perf-sim) |
| **Gate** | Python `enable_bir_vnsplitter` / CLI `--enable-bir-vnsplitter` (Cython pipeline) |
| **Search class** | exhaustive **grid** — *not* annealing/SPSA/greedy/MCTS |

---

## 1. `runPGA` — the grid-search driver `[CONFIRMED]`

`runPGA` is two nested loops that launch 64 async candidates, followed by a join loop that logs each candidate's score. The loop bounds are immediate operands and are directly visible in the prologue.

### The grid axes

The **outer** loop walks the SB-size `level` in `r13d`, and the **inner** loop walks the threshold step in `ebx`:

```c
// runPGA @0xd6e680 — grid bounds are immediate operands.
// OUTER: level r13d := 0xa00 (2560), step +0xa00, exit when == 0x5a00 (23040)
//   d6e68a  mov  $0xa00,%r13d        // init  2560
//   d6ea68  add  $0xa00,%r13d        // step +2560
//   d6ea6f  cmp  $0x5a00,%r13d       // exit  23040  -> 8 iterations {2560..20480}
// INNER: threshold step ebx := 0x69 (105), step +0x19 (25), exit when == 0x131 (305)
//   d6e6e1  mov  $0x69,%ebx          // init  105
//   d6e798  add  $0x19,%ebx          // step +25
//   d6e7bf  cmp  $0x131,%ebx         // exit  305    -> 8 iterations {105..280}
// threshold = ebx / 100.0f:
//   d6e7d4  cvtsi2ss %ebx,%xmm0
//   d6e7d8  divss  0x1dd8bfc(%rip),%xmm0   // .rodata 0x1dd8bfc = 0x42c80000 = 100.0f
```

So the grid is:

| Axis | Operand | Set | Count |
|---|---|---|---|
| `level` (SB-size, `r13d`) | init `0xa00`, step `0xa00`, exit `0x5a00` | `{2560, 5120, 7680, 10240, 12800, 15360, 17920, 20480}` | 8 |
| `threshold` (`ebx/100.0`) | init `0x69`, step `0x19`, exit `0x131` | `{1.05, 1.30, 1.55, 1.80, 2.05, 2.30, 2.55, 2.80}` | 8 |
| `budget` (1st arg) | `movl $0xc350` (`d6e768`/`d6e792`/`d6e871`) | `50000` — fixed | 1 |

> **CORRECTION — threshold tops out at 2.80, not 3.05.** The backing report's 5-line index says the threshold sweep is `1.05..3.05`; that is wrong. The inner counter exits at `ebx == 0x131` (305), so the **last value used is 280**, i.e. `2.80f`. `0x131/100 = 3.05` is the *exit sentinel*, never passed to `runVNSplitterOnce`. (The report's body §(a) already states `2.80` correctly — the index line is the slip.) The 8 thresholds are `{1.05, 1.30, 1.55, 1.80, 2.05, 2.30, 2.55, 2.80}`, step `0.25`.

The divisor constant is byte-confirmed: `.rodata @0x1dd8bf8` reads `… 0000c842 …`, so the dword at `0x1dd8bfc` is little-endian `0x42c80000`, which is exactly `100.0f`. `[CONFIRMED]`

### The async fan-out

Per `(level, threshold)` cell the driver allocates an async control block, **copies** the PGA object, bakes the cell parameters into the copy, and starts the candidate:

```c
// per-cell launch in runPGA
void *cb = operator new(0xf8);                  // d6e8ba  _Znwm(0xf8) — _Async_state_impl block
ProfileGuidedAutoTuning clone(*this);           // d6e88b  copy-ctor @0xd73060 — CLONE the object
clone.threshold = threshold;                    //  (movss %xmm3,0x4c(%rbp))   field +0x4c
clone.level     = level;                        //  (mov   %r13d,0x50(%rbp))   field +0x50
clone.budget    = 0xc350;                       //  d6e871 movl $0xc350,0x48(%rbp)  field +0x48
std::thread::_M_start_thread(...);              // d6e8f8  LAUNCH candidate
futures._M_realloc_insert(future);              // d6e9c5  vector<future<tuple<float,int>>> push
```

The copy at `d6e88b` is the entire reason the search is data-race-free: each candidate captures its **own** clone of the PGA object (which carries the serialized baseline module at field `+0x70`, see [§2](#2-runvnsplitteronce--one-candidate-confirmed)), so the 64 threads each `load` and mutate a private module. `[CONFIRMED]`

The launch policy is the default `std::launch::async|deferred`. The binary instantiates **both** state classes for the same invoker type:

```text
std::__future_base::_Async_state_impl< _Invoker<tuple< (PGA::*)(int,float,int),
                                       PGA, int, float, int >>, tuple<float,int> >   @0xd727c0 _M_run
std::__future_base::_Deferred_state<   …same invoker… , tuple<float,int> >            @0xd707d0 _M_is_deferred_future
```

Both `_Async_state_impl::_M_run` (`@0xd727c0`, a real `std::thread`) and `_Deferred_state` (lazy) are present, which is the signature of the default policy: the runtime picks a real thread when it can and falls back to lazy evaluation otherwise. `[CONFIRMED — both instantiations nm-visible]`

### The join + log loop

After 64 launches the driver waits each future and logs its score. No comparison is performed:

```c
for (auto &f : futures) {
    auto r = f.get();                           // d6eb0b __atomic_futex_unsigned_base::_M_futex_wait_until
    int   cycles = *(int   *)((char*)&r + 0x10);// result tuple: int   @+0x10
    float memeff = *(float *)((char*)&r + 0x14);//              float @+0x14
    // build ostringstream and emit:
    //   "(PGA) memeff = " << (double)memeff
    //   << ", cycles = "  << cycles
    //   << " --> ("       << level << ", " << threshold << ")"
    // format literals: 0x1c79f54 "(PGA) memeff = "
    //                  0x1c79f64 ", cycles = "
    //                  0x1c79f70 " --> ("
}
// epilogue d6f0ec..d6f14a: operator delete on the config vec and the result vec. runPGA returns void.
```

The three format literals each appear **exactly once** in `.rodata` (`rg -a` count = 1 apiece), confirming this is the only emission site. There is **no** `comiss`/`minss` reducing the 64 outcomes to a winner — they are logged and the vectors freed. `[CONFIRMED]`

> **QUIRK — `__libc_single_threaded` fast path.** Around `d6e946`/`d6e94d` the future-state refcount ops branch on `__libc_single_threaded`, toggling between locked and unlocked `shared_ptr` decrements. This is libstdc++ boilerplate, not PGA logic, but it shows up in the disassembly of the launch loop and can confuse a reimplementer expecting a single code path.

---

## 2. `runVNSplitterOnce` — one candidate `[CONFIRMED]`

One grid cell is one call to `runVNSplitterOnce(int budget, float threshold, int level)` returning `std::tuple<float memeff, int cycles>` by value. The SysV ABI for a member function returning a struct by value places the hidden return pointer in `rdi`, `this` in `rsi`, and the three scalar args in `edx`/`xmm0`/`ecx`:

```c
// @0xd6f440 — member fn, struct return.  rdi=retptr, rsi=this, edx=budget, xmm0=threshold, ecx=level
std::tuple<float,int> runVNSplitterOnce(int budget /*=50000*/, float threshold, int level) {
    bir::Module module("module");               // d6f4ed  fresh empty module named "module"
    module.load(this->serialized /*+0x70*/);    // d6f517  *** RE-LOAD baseline BIR snapshot ***
                                                //  -> every candidate starts from the SAME baseline
    // build pass-options in-frame: SBModel/profiler defaults (1000, 0x4c4b40=5000000, 0x40, …)
    // with threshold and the packed (level,budget) baked in.
    auto t0 = system_clock::now();              // d6fabd
    VNSplitter::runTransform();                 // d6fad2  *** SPLIT using (threshold, level) ***
    auto t1 = system_clock::now();              // d6fad7   logs "INFO (VNSplitter) Time: <t1-t0> seconds"
    auto t2 = system_clock::now();              // d6feb7
    VerticalFusion::runTransform();             // d6febf  *** re-FUSE after split ***
    auto t3 = system_clock::now();              // d6fec4   logs "INFO (VerticalFusion) Time: …"
    PerformanceProfiler profiler(module, …);    // inline ctor
    float *r_memeff = retptr+0x190; *r_memeff = -1.0f; // d7012c  movl $0xbf800000  failure sentinel
    profiler.runProfile();                      // d7013f  *** SIMULATE — overwrites @0x190/@0x194 ***
    // return tuple (GCC reverse layout {int cycles; float memeff}):
    *(int   *)(retptr+0) = *(int   *)(profiler+0x194); // d7015e  cycles
    *(float *)(retptr+4) = *(float *)(profiler+0x190); // d70160  memeff
    return /*tuple<float,int>*/;                 // destroy profiler/VerticalFusion/VNSplitter/module
}
```

Every call above is a confirmed `…@plt` target: `bir::Module::Module(string)` (`d6f4ed`), `bir::Module::load(string)` (`d6f517`), `getArch`/`getArchModel` (`d6fa4c`/`d6fa61`), `system_clock::now` (`d6fabd`), `VNSplitter::runTransform` (`d6fad2`), `VerticalFusion::runTransform` (`d6febf`), `PerformanceProfiler::runProfile` (`d7013f`). `[CONFIRMED — all seven]`

### Argument semantics

| Arg | Reg | Value | Meaning | Confidence |
|---|---|---|---|---|
| 1 `budget` | `edx` | `50000` (`0xc350`) | per-attempt size/cost cap; **never swept** | `[CONFIRMED const]` / `[INFERRED semantic]` |
| 2 `threshold` | `xmm0` | `1.05 … 2.80` | split threshold = duplication-factor tolerance (`maxDupFactorSBSplit`) | `[STRONG]` |
| 3 `level` | `ecx` | `2560 … 20480` | SB-size level = SBUF byte-budget granularity (`minEligibleSBSplitSize`) | `[STRONG]` |

The threshold being `> 1.0` and fed as the `float` arg to `VNSplitter::analyze(MemoryLocation*, float, int, int)` is what grounds the "duplication factor" reading; the VN-Splitter carries the strings `maxDupFactorSBSplit` and `min_split_size`. The same three-argument shape appears on the sibling page under a different naming — [`vnsplitter-shrink`](vnsplitter-shrink.md) calls it `runVNSplitterOnce(int vn_limit, float ratio, int perSplitLimit)`. The two namings agree on structure (int budget/limit, float ratio/threshold, int level/per-split cap); the exact field semantics behind `budget` are not byte-confirmed against a named struct field. `[INFERRED]`

### The scoring struct and the `-1.0f` sentinel

`runProfile` writes the result into the profiler at `+0x190` (`memeff`, float) and `+0x194` (`cycles`, int). Before the call, `memeff` is pre-set to `-1.0f` (`movl $0xbf800000,0x190(%rsp)` at `d7012c`). This is the **failure default**: a split that violates SB capacity or otherwise produces an infeasible module returns `memeff = -1`, which — under a "higher memeff is better" rule — loses to any feasible candidate automatically. `[CONFIRMED sentinel; INFERRED units]`

The returned tuple is GCC's reverse field order: `{int cycles; float memeff}` in memory, so `std::get<float>` is `memeff` and `std::get<int>` is `cycles`. The join loop in `runPGA` reads them back at `+0x10`/`+0x14` of the future result (see [§1](#the-join--log-loop)). `[CONFIRMED]`

> **CORRECTION — both drivers are split-then-fold; there is NO order difference.** An earlier revision of this page claimed the standalone `VNSplitterPass::run` runs *fold-then-split* while the PGA candidate runs the opposite — that contrast is **false**. Both run split first, then fold. The standalone `VNSplitterPass::run` (`@0xd73890`, documented on [`vnsplitter-shrink`](vnsplitter-shrink.md)) calls `VNSplitter::runTransform` (SPLIT) at `d73d6a` **before** `VerticalFusion::runTransform` (FOLD) at `d74162`. The PGA candidate body runs the identical order: `VNSplitter::runTransform` (`d6fad2`) *before* `VerticalFusion::runTransform` (`d6febf`). A reimplementer can rely on the two sharing one order — split with the tuned knobs first, then fold to measure the net footprint. `[CONFIRMED — call order directly disassembled in both bodies]`

---

## 3. The metric: in-process perf-sim, not a file `[CONFIRMED / STRONG]`

The `(memeff, cycles)` score is read **directly** from `PerformanceProfiler::runProfile`'s result struct inside each async candidate. There is no JSON round-trip in this code path. `runProfile` (`@0xd6b080`) is the in-process perf-sim: it calls `bir::Module::getDMAProfile(int)` (`@0xd6b0b1`) and walks the module's `PhysicalAccessPattern`s to produce a latency estimate.

The serialized `tensorizer_metric_store.json` leg — `BackendMetricType` 42, `PostSchedEstLatency` — is the **on-disk form of this same `cycles` estimate**. PGA consumes it in-memory; the `metricstore` page (in flight) documents the serialized store. The relationship is: same metric, two consumers — the metric store serializes it for cross-stage hand-off, PGA reads it live for the inner search. `[STRONG]`

There is **no reward arithmetic** in `runPGA`. Both `memeff` and `cycles` are kept as a raw pair and only logged — no weighting, no scalarization, no annealing temperature update. The "reward" is the `(memeff, cycles)` pair itself; any selection of the winning split is internal to `VNSplitter::runTransform`'s own per-split accept rule (each committed split must satisfy `ApGroup::isValidForSB(SBModel)` and improve packing), with PGA enumerating the grid around it. `[CONFIRMED no-reward-math; STRONG on the accept-rule reading]`

---

## 4. The gate: `enable_bir_vnsplitter`, not `--enable-pga` `[CONFIRMED]`

There is **no** `--enable-pga`, `--run-pga`, or `enable_pga` string anywhere in `libwalrus.so` or the walrus driver (`rg -a` count = 0). PGA is not user-toggled by name. `[CONFIRMED absent]`

The path is gated by the VN-Splitter pipeline option, which lives in the **Cython** front-end, not in `libwalrus.so`. The string pool of `CompileCommand.cpython-310-…so` carries both forms:

```text
__pyx_kp_u_enable_bir_vnsplitter  -> "--enable-bir-vnsplitter"   (CLI long-option)
__pyx_n_s_enable_bir_vnsplitter_2 -> "enable_bir_vnsplitter"     (Python option identifier)
```

Both appear in `CompileCommand.cpython-310-…so` and `WalrusDriver.cpython-310-…so` (and identically in the cp311/cp312 rebuilds). When the VN-Splitter pass is scheduled, PGA runs programmatically — the grid is hard-coded, so there is no user-facing numeric `level` knob. `[CONFIRMED string; STRONG on "PGA runs when the pass is scheduled"]`

> **CORRECTION — there *is* a CLI flag, just not `--enable-pga`.** The backing report frames the gate as "the Python pipeline option `enable_bir_vnsplitter`" and emphasises the *absence* of `--enable-pga`. Both are true, but the gate is **also** exposed as a CLI long-option `--enable-bir-vnsplitter` (`__pyx_kp_u_enable_bir_vnsplitter`), not only a Python identifier. A user enabling the VN-Splitter from the command line therefore enables the PGA sweep transitively — there is simply no separate PGA flag.

---

## 5. The commit path `[UNRESOLVED]`

This is the honest ceiling of the analysis, and it is stated here in full so no reader over-reads the search.

`runPGA` **logs** 64 `(memeff, cycles) → (level, threshold)` lines and **frees** its result vectors. It performs no argmin, computes no reward, and does not re-invoke `VNSplitter` with a chosen winner. The `Pass`-interface entry `run(bir::Module&)` (`@0xd713f0`) is a **14-byte stub**:

```c
// run(bir::Module&) @0xd713f0 — disassembled in full
//   movl $0x0, (%rdi)          // zero-init an 0x18-byte result/StringRef-like struct
//   …                          // (mov, fill)
//   movb $0x0, 0x18(%rdi)      // d71409
//   ret                        // d7140d
```

It returns an empty result and drives nothing. So **from this binary alone, the outer orchestration that picks the best `(level, threshold)` and re-applies it to the real module is not recovered.** Two possibilities are consistent with the evidence and *neither* is proven:

1. **No commit** — PGA is pure telemetry; the real optimization happens inside `VNSplitter::runTransform`'s own per-split accept rule (which `runVNSplitterOnce` invokes 64 times on clones, discarding all 64). On this reading the grid measures, a human/log reads the result, and the "feedback" is offline. `[INFERRED]`
2. **External commit** — a caller (not `runPGA`, not `run`) parses the logged results or a result vector handed back through a different path and re-runs the splitter with the winner. No such caller was located from these symbols. `[SPECULATIVE]`

A reimplementer building a *closing* feedback loop must supply the argmin + re-apply themselves; this binary's `runPGA` does not contain it. **Do not fabricate a commit path.** `[UNRESOLVED]`

Two further honest caveats, carried from the analysis:

- The `budget = 50000` *semantic* (cost cap vs iteration count vs cycle ceiling) is inferred from it being the fixed first argument sitting alongside SBModel limit constants; it is not byte-confirmed against a named field. `[INFERRED]`
- `memeff` units (ratio `[0,1]` vs percentage) are not confirmed — only the `-1.0f` failure sentinel and the implied "higher is better" ordering are visible. `[INFERRED]`

---

## 6. Key addresses

| Address | Symbol / role |
|---|---|
| `0xd6e680` | `runPGA()` — grid driver |
| `0xd6e68a` / `0xd6ea68` / `0xd6ea6f` | level init `0xa00` / step `+0xa00` / exit `0x5a00` |
| `0xd6e6e1` / `0xd6e798` / `0xd6e7bf` | threshold init `0x69` / step `+0x19` / exit `0x131` |
| `0x1dd8bfc` | `.rodata` threshold divisor `0x42c80000` = `100.0f` |
| `0xd6e88b` | copy-ctor call (clone PGA per cell) → body `@0xd73060` |
| `0xd6e8f8` | `std::thread::_M_start_thread` (launch candidate) |
| `0xd6e9c5` | `vector<future<tuple<float,int>>>::_M_realloc_insert` |
| `0xd6eb0b` | `__atomic_futex_unsigned_base::_M_futex_wait_until` (future::get) |
| `0xd6f440` | `runVNSplitterOnce(int, float, int)` → `tuple<float,int>` |
| `0xd6f517` | `bir::Module::load` (re-load baseline snapshot) |
| `0xd6fad2` | `VNSplitter::runTransform` (split) |
| `0xd6febf` | `VerticalFusion::runTransform` (re-fuse) |
| `0xd7012c` | `memeff = -1.0f` failure sentinel (`movl $0xbf800000`) |
| `0xd7013f` | `PerformanceProfiler::runProfile` (perf-sim) |
| `0xd7015e` / `0xd70160` | tuple write: `cycles` / `memeff` |
| `0xd6b080` / `0xd6b0b1` | `PerformanceProfiler::runProfile` / `getDMAProfile` |
| `0xd713f0` | `run(bir::Module&)` — 14-byte stub |
| `0x3d8f7b8` / `0x3d8f638` | vtable / typeinfo for `ProfileGuidedAutoTuning` |
| `0x1c79f54` / `0x1c79f64` / `0x1c79f70` | log fmts `"(PGA) memeff = "` / `", cycles = "` / `" --> ("` |
| Cython | `__pyx_kp_u_enable_bir_vnsplitter` (`--enable-bir-vnsplitter`) / `__pyx_n_s_enable_bir_vnsplitter_2` |
