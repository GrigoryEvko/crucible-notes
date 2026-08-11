# Profile-Guided Auto-Tuning (PGA): a Grid Search With No Feedback Path

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds). The native code lives in `neuronxcc/starfish/lib/libwalrus.so`; the Python gate lives in the Cython modules `neuronxcc/driver/commands/CompileCommand.cpython-310-…so` and `neuronxcc/driver/jobs/WalrusDriver.cpython-310-…so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `0x5e9020`–`0x62d650` is the `.plt` thunk band, so every `call …@plt` cited here targets a thunk whose real body lives elsewhere — the cited `0xd6xxxx`/`0xd7xxxx` addresses are the real bodies of the PGA functions themselves. `.data` carries a +0x400000 delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

`ProfileGuidedAutoTuning` (PGA) is a compiled-in, **fully disconnected** parameter sweep around the VN-Splitter. Its body answers one question: *which `(SB-size level, split threshold)` pair makes the VN-Splitter produce the cheapest schedule for this module?* — and it answers it by brute force. `runPGA` (`@0xd6e680`) enumerates an **8 × 8 = 64-candidate grid** of `(level, threshold)` cells, compiles and simulates each candidate independently, reads back a `(memeff, cycles)` score per candidate, and **logs** the results. There is no annealing, no SPSA, no greedy descent, and no temperature schedule.

There is also no feedback: nothing in this build calls `runPGA`, and nothing consumes its scores. The class's only virtual entry, `run(bir::Module&)` (`@0xd713f0`), is a 30-byte stub that zero-fills a result struct and returns without touching the module or the grid. The three claims that pin this down, each checked against the shipped binaries:

| Claim | Check | Result |
|---|---|---|
| `runPGA` has no call site | full `objdump -d libwalrus.so`, search for `d6e680` | one hit — the symbol header itself |
| `runPGA` is not taken by address | `readelf -r` + raw 8-byte scan for `0x00000000006de680`-style pointers | only its `.dynsym` entry; no reloc, no vtable slot, no GOT entry |
| Nothing outside `libwalrus.so` knows PGA | `grep -rl --binary-files=text ProfileGuidedAutoTuning neuronxcc/` over the unpacked wheel | one file — `libwalrus.so` |

The exported `runPGA` / `runVNSplitterOnce` symbols are reachable only by an out-of-tree caller that links `libwalrus.so` and names them; no shipped component does. This page therefore documents a **specified-but-unwired sweep**: the grid geometry, the candidate body, and the scoring struct are complete and citeable, and the loop that would close around them is absent.

Each grid cell is one call to `runVNSplitterOnce(50000, threshold, level)` (`@0xd6f440`), dispatched through `std::async` over an independent **clone** of the PGA object, so the 64 candidates run as 64 `std::future<std::tuple<float,int>>` in parallel and never share mutable module state. A candidate re-loads the baseline module from a serialized BIR snapshot, runs `VNSplitter::runTransform` with the cell's `(threshold, level)`, re-fuses with `VerticalFusion::runTransform`, then runs `PerformanceProfiler::runProfile` to get an in-process `(memeff, cycles)` estimate — the same perf-sim metric serialized elsewhere as `tensorizer_metric_store.json` (the `metricstore` page, in flight, covers the serialized form), read here straight out of the live profiler result struct rather than from a file.

The metric is the VN-Splitter knobs the search tunes; the [`vnsplitter-shrink`](vnsplitter-shrink.md) page documents the pass itself (pass 24, `vn_splitter`, whose standalone body runs **split-then-fold** — `VNSplitter::runTransform` first, then `VerticalFusion::runTransform`). The PGA candidate body runs the **same** two transforms in the **same** split-then-fold order, so there is no order difference between the standalone pass and the autotuner candidate (see [§2](#2-runvnsplitteronce--one-candidate)). PGA is a **grid search**, and it should not be confused with the separate penguin-frontend MCTS autotuner (the [`penguin-autotuner`](penguin-autotuner.md) page) which searches a tile/schedule space with Monte-Carlo tree search — a fundamentally different algorithm. The two share the word "autotuner" and nothing else.

> **GOTCHA — "profile-guided" names an intent, not a wired loop.** The class name, the `PerformanceProfiler` call inside each candidate, and the `(memeff, cycles)` return tuple all read like a closed autotuning loop, and the [`vnsplitter-shrink`](vnsplitter-shrink.md) knobs the sweep varies are real live pass options. But `runPGA` selects nothing (no `comiss`/`minss`, no scalar reward — the result vectors are freed in the epilogue), commits nothing, and is itself never called. Whatever tuning the VN-Splitter performs in a real compile comes from its own per-split accept rule, not from PGA.

## Reimplementation contract

Nothing here needs to be reimplemented for behavioural parity — an implementation that omits PGA entirely matches this build. To reproduce the sweep as specified: 

- **The grid**: an outer loop over 8 `level` values `{2560, 5120, …, 20480}` (step 2560) and an inner loop over 8 `threshold` steps `{105, 130, …, 280}` (step 25), the threshold being `step / 100.0f`. 64 cells, not annealing.
- **The fan-out**: each cell launches `runVNSplitterOnce(50000, threshold, level)` via `std::async` (default `launch::async|deferred`) on a **copy** of the PGA object; futures collected into a `vector<future<tuple<float,int>>>`.
- **The candidate body**: load baseline module → `VNSplitter::runTransform` → `VerticalFusion::runTransform` → `PerformanceProfiler::runProfile`; return `tuple<float memeff, int cycles>` with a `-1.0f` failure sentinel for `memeff`.
- **The join**: wait each future, read `(cycles@+0, memeff@+4)`, format `"(PGA) memeff = … , cycles = … --> (level, threshold)"`.
- **The entry point you must supply yourself**: there is no flag, no pass registration, and no caller for `runPGA` in this build. There is no `--enable-pga` / `enable_pga` string anywhere in the wheel, and no numeric `level` knob — the grid is hard-coded.

| | |
|---|---|
| **Component** | `neuronxcc::backend::ProfileGuidedAutoTuning` |
| **Grid driver** | `runPGA()` `@0xd6e680` (`_ZN9neuronxcc7backend23ProfileGuidedAutoTuning6runPGAEv`) |
| **Candidate** | `runVNSplitterOnce(int, float, int)` `@0xd6f440` → `std::tuple<float,int>` |
| **Pass shim** | `run(bir::Module&)` `@0xd713f0` — 30-byte stub (`0xd713f0`–`0xd7140d`), vtable slot `vt+0x20` |
| **Call sites of `runPGA`** | **none** in `libwalrus.so`; no reloc, no vtable slot, no other wheel file names the class |
| **Clone ctor** | copy-ctor `@0xd73060`, called from grid at `d6e88b` |
| **vtable / typeinfo** | `0x3d8f7b8` / `0x3d8f638` |
| **Grid size** | 8 levels × 8 thresholds = **64** candidates |
| **Budget literal** | `0xc350` = **50000** (constant, never swept) |
| **Metric source** | `PerformanceProfiler::runProfile` `@0xd6b080` (in-process; perf-sim) |
| **Gate** | none — no PGA flag exists; the VN-Splitter's own `enable_bir_vnsplitter` gates the *pass*, not this sweep |
| **Search class** | exhaustive **grid** — *not* annealing/SPSA/greedy/MCTS |
| **Status in this build** | unreachable: dead code with complete bodies |

---

## 1. `runPGA` — the grid-search driver

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

> **GOTCHA — the threshold sweep tops out at 2.80, not 3.05.** `0x131` (305) is the loop's *exit sentinel*, so the last value actually passed to `runVNSplitterOnce` is 280, i.e. `2.80f`. Reading the exit constant as a swept value inflates the grid to a threshold that never runs. The eight thresholds are `{1.05, 1.30, 1.55, 1.80, 2.05, 2.30, 2.55, 2.80}`, step `0.25`.

The divisor constant reads byte-exact: `.rodata @0x1dd8bf8` contains `… 0000c842 …`, so the dword at `0x1dd8bfc` is little-endian `0x42c80000` — exactly `100.0f`.

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

The copy at `d6e88b` is the entire reason the search is data-race-free: each candidate captures its **own** clone of the PGA object (which carries the serialized baseline module at field `+0x70`, see [§2](#2-runvnsplitteronce--one-candidate)), so the 64 threads each `load` and mutate a private module.

The launch policy is the default `std::launch::async|deferred`. The binary instantiates **both** state classes for the same invoker type:

```text
std::__future_base::_Async_state_impl< _Invoker<tuple< (PGA::*)(int,float,int),
                                       PGA, int, float, int >>, tuple<float,int> >   @0xd727c0 _M_run
std::__future_base::_Deferred_state<   …same invoker… , tuple<float,int> >            @0xd707d0 _M_is_deferred_future
```

Both `_Async_state_impl::_M_run` (`@0xd727c0`, a real `std::thread`) and `_Deferred_state` (lazy) are present, which is the signature of the default policy: the runtime picks a real thread when it can and falls back to lazy evaluation otherwise. Both instantiations are `nm`-visible.

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

The three format literals each appear **exactly once** in `.rodata` (`rg -a` count = 1 apiece), confirming this is the only emission site. There is **no** `comiss`/`minss` reducing the 64 outcomes to a winner — they are logged and the vectors freed.

> **QUIRK — `__libc_single_threaded` fast path.** Around `d6e946`/`d6e94d` the future-state refcount ops branch on `__libc_single_threaded`, toggling between locked and unlocked `shared_ptr` decrements. This is libstdc++ boilerplate, not PGA logic, but it shows up in the disassembly of the launch loop and can confuse a reimplementer expecting a single code path.

---

## 2. `runVNSplitterOnce` — one candidate

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

Every call above is a confirmed `…@plt` target: `bir::Module::Module(string)` (`d6f4ed`), `bir::Module::load(string)` (`d6f517`), `getArch`/`getArchModel` (`d6fa4c`/`d6fa61`), `system_clock::now` (`d6fabd`), `VNSplitter::runTransform` (`d6fad2`), `VerticalFusion::runTransform` (`d6febf`), `PerformanceProfiler::runProfile` (`d7013f`).

### Argument semantics

| Arg | Reg | Value | Meaning | Confidence |
|---|---|---|---|---|
| 1 `budget` | `edx` | `50000` (`0xc350`) | per-attempt size/cost cap; **never swept** | CERTAIN (value) / MEDIUM (semantic) |
| 2 `threshold` | `xmm0` | `1.05 … 2.80` | split threshold = duplication-factor tolerance (`maxDupFactorSBSplit`) | HIGH |
| 3 `level` | `ecx` | `2560 … 20480` | SB-size level = SBUF byte-budget granularity (`minEligibleSBSplitSize`) | HIGH |

The threshold being `> 1.0` and fed as the `float` arg to `VNSplitter::analyze(MemoryLocation*, float, int, int)` is what grounds the "duplication factor" reading; the VN-Splitter carries the strings `maxDupFactorSBSplit` and `min_split_size`. The same three-argument shape appears on the sibling page under a different naming — [`vnsplitter-shrink`](vnsplitter-shrink.md) calls it `runVNSplitterOnce(int vn_limit, float ratio, int perSplitLimit)`. The two namings agree on structure (int budget/limit, float ratio/threshold, int level/per-split cap); the exact field semantics behind `budget` are not pinned to a named struct field. [INFERRED]

### The scoring struct and the `-1.0f` sentinel

`runProfile` writes the result into the profiler at `+0x190` (`memeff`, float) and `+0x194` (`cycles`, int). Before the call, `memeff` is pre-set to `-1.0f` (`movl $0xbf800000,0x190(%rsp)` at `d7012c`). This is the **failure default**: a split that violates SB capacity or otherwise produces an infeasible module returns `memeff = -1`, which — under a "higher memeff is better" rule — loses to any feasible candidate automatically. The sentinel itself is pinned; the units of `memeff` are [INFERRED].

The returned tuple is GCC's reverse field order: `{int cycles; float memeff}` in memory, so `std::get<float>` is `memeff` and `std::get<int>` is `cycles`. The join loop in `runPGA` reads them back at `+0x10`/`+0x14` of the future result (see [§1](#the-join--log-loop)).

The candidate body and the standalone pass run the two transforms in the **same** order — split first, then fold. `VNSplitterPass::run` (`@0xd73890`, documented on [`vnsplitter-shrink`](vnsplitter-shrink.md)) calls `VNSplitter::runTransform` at `d73d6a` before `VerticalFusion::runTransform` at `d74162`; the PGA candidate calls them at `d6fad2` and `d6febf` respectively. A reimplementer can rely on one order throughout: split with the tuned knobs, then fold to measure the net footprint.

---

## 3. The metric: in-process perf-sim, not a file

The `(memeff, cycles)` score is read **directly** from `PerformanceProfiler::runProfile`'s result struct inside each async candidate. There is no JSON round-trip in this code path. `runProfile` (`@0xd6b080`) is the in-process perf-sim: it calls `bir::Module::getDMAProfile(int)` (`@0xd6b0b1`) and walks the module's `PhysicalAccessPattern`s to produce a latency estimate.

The serialized `tensorizer_metric_store.json` leg — `BackendMetricType` 42, `PostSchedEstLatency` — is the **on-disk form of this same `cycles` estimate**. The metric store serializes it for cross-stage hand-off; the PGA candidate reads the same quantity out of the live profiler struct. That is the whole of the relationship: one estimate, two readers. The PGA reading feeds a log line and nothing else.

There is **no reward arithmetic** in `runPGA`. Both `memeff` and `cycles` are kept as a raw pair and only logged — no weighting, no scalarization, no annealing temperature update, and no comparison across candidates. Whatever split selection happens in a real compile is internal to `VNSplitter::runTransform`'s own per-split accept rule (each committed split must satisfy `ApGroup::isValidForSB(SBModel)` and improve packing); PGA does not enumerate around it, because PGA does not run. The absence of reward arithmetic is read directly; the accept-rule reading comes from the VN-Splitter side.

---

## 4. There is no gate — `enable_bir_vnsplitter` gates the pass, not the sweep

There is **no** `--enable-pga`, `--run-pga`, or `enable_pga` string anywhere in `libwalrus.so` or the walrus driver (`rg -a` count = 0). PGA is not user-toggled by name, and — since nothing calls `runPGA` — it is not toggled indirectly either.

The neighbouring option that *is* real gates the VN-Splitter **pass**. It lives in the **Cython** front-end, not in `libwalrus.so`. The string pool of `CompileCommand.cpython-310-…so` carries both forms:

```text
__pyx_kp_u_enable_bir_vnsplitter  -> "--enable-bir-vnsplitter"   (CLI long-option)
__pyx_n_s_enable_bir_vnsplitter_2 -> "enable_bir_vnsplitter"     (Python option identifier)
```

Both appear in `CompileCommand.cpython-310-…so` and `WalrusDriver.cpython-310-…so` (and identically in the cp311/cp312 rebuilds), exposed as a Python pipeline identifier `enable_bir_vnsplitter` and as the CLI long-option `--enable-bir-vnsplitter` (`__pyx_kp_u_enable_bir_vnsplitter`).

> **GOTCHA — enabling the VN-Splitter does not enable the sweep.** `--enable-bir-vnsplitter` schedules `VNSplitterPass` (`@0xd73890`), which calls `VNSplitter::runTransform` **once** with the options it was configured with. It does not reach `ProfileGuidedAutoTuning`: the two classes share the transform but no call edge. Reading the absence of a PGA flag as "the sweep is enabled transitively" is the natural wrong inference and produces a 64× cost estimate for a pass that runs once.

---

## 5. Why there is no commit path: the sweep is never entered

`runPGA` **logs** 64 `(memeff, cycles) → (level, threshold)` lines and **frees** its result vectors. It performs no argmin, computes no reward, and does not re-invoke `VNSplitter` with a chosen winner. That alone leaves the question "who picks the winner?" open — and the answer is that nobody does, because nobody starts the search.

**The virtual entry is a stub.** `ProfileGuidedAutoTuning::run(bir::Module&)` occupies vtable slot `vt+0x20` (reloc `R_X86_64_64` at `0x3d8f7d8` → `0xd713f0`, with the dtors at `vt+0x10`/`vt+0x18`). Its body is 30 bytes, disassembled in full:

```asm
d713f0:  lea    rdx,[rdi+0x18]          ; rdi = hidden return slot (result struct)
d713f4:  mov    DWORD PTR [rdi],0x0     ; status/kind word = 0
d713fa:  mov    rax,rdi                 ; return the slot
d713fd:  mov    QWORD PTR [rdi+0x8],rdx ; string ptr → inline buffer at +0x18
d71401:  mov    QWORD PTR [rdi+0x10],0x0; length 0
d71409:  mov    BYTE PTR [rdi+0x18],0x0 ; NUL terminator  → empty message
d7140d:  ret
```

It never dereferences the `bir::Module&` and never calls `runPGA`. (`VNSplitter::run(bir::Module&)` at `0xd71410` has the same shape — for that class the live pass entry is `VNSplitterPass::run` @ `0xd73890`. PGA has no such second entry: `nm -DC` lists exactly `runPGA`, `runVNSplitterOnce`, `run`, the copy-ctor, and the dtors, with no `getName`, no registration helper, and no non-copy constructor.)

**The grid driver has no caller.** `0xd6e680` appears once in a full `objdump -d` of `libwalrus.so` — as its own symbol header. A raw scan of the file for the little-endian 8-byte value `0xd6e680`, plus `readelf -r`, finds it only in `.dynsym`: no direct `call`, no relocation, no vtable slot, no GOT entry, hence no indirect call either. By contrast `runVNSplitterOnce` *does* have a `R_X86_64_GLOB_DAT` at `0x3dc1ee8` — the pointer-to-member the `std::async` invoker inside `runPGA` needs — which is exactly what a live call edge looks like, and which `runPGA` itself does not have.

**Nothing outside the library knows the class.** `grep -rl --binary-files=text ProfileGuidedAutoTuning neuronxcc/` over the unpacked wheel matches only `libwalrus.so`; so do `runPGA` and the `"(PGA) memeff = "` log literal. `walrus_driver`, the Cython driver modules, and every other shipped ELF are clear.

The consequence for a reimplementer is simple: **there is no feedback loop to port.** The argmin, the re-apply, and the trigger must all be written from scratch if the sweep is wanted; skipping PGA entirely reproduces this build exactly.

Two further caveats about the sweep body itself, which stand independently of its reachability:

- The `budget = 50000` *semantic* (cost cap vs iteration count vs cycle ceiling) is inferred from it being the fixed first argument sitting alongside SBModel limit constants; it is not pinned to a named field. [INFERRED]
- `memeff` units (ratio `[0,1]` vs percentage) are not confirmed — only the `-1.0f` failure sentinel and the implied "higher is better" ordering are visible. [INFERRED]

---

## 6. Key addresses

| Address | Symbol / role |
|---|---|
| `0xd6e680` | `runPGA()` — grid driver; **zero call sites, zero relocations** |
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
| `0xd713f0` | `run(bir::Module&)` — 30-byte stub (`0xd713f0`–`0xd7140d`), empty result, no `runPGA` call |
| `0x3d8f7b8` / `0x3d8f638` | vtable / typeinfo for `ProfileGuidedAutoTuning` |
| `0x3d8f7c8` / `0x3d8f7d0` / `0x3d8f7d8` | vtable slots: dtor `0xd6e520`, dtor `0xd6e660`, `run` stub `0xd713f0` (`readelf -r`) |
| `0x3dc1ee8` | `R_X86_64_GLOB_DAT` → `runVNSplitterOnce` — the pointer-to-member the async invoker in `runPGA` takes |
| `0x1c79f54` / `0x1c79f64` / `0x1c79f70` | log fmts `"(PGA) memeff = "` / `", cycles = "` / `" --> ("` |
| Cython | `__pyx_kp_u_enable_bir_vnsplitter` (`--enable-bir-vnsplitter`) / `__pyx_n_s_enable_bir_vnsplitter_2` |
