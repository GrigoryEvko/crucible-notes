# The Opt-Level Planes — `-O0..-O3` and the Three Hidden Dials

> *All symbols, offsets, and string IDs on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython modules under `neuronxcc/`, plus the `walrus_driver` backend ELF). Other wheels differ; treat every address as version-pinned. Cython interns each attribute name as `__pyx_n_s_<name>` / `__pyx_n_u_<name>` and each string literal as `__pyx_kp_u_<val>`; equality tests are `_Pyx_PyUnicode_Equals(attr, kp_u_<v>)`. Line numbers cited as `py L####` come from the `__Pyx_AddTraceback(...,<line>,...)` markers in the decompiled body.*

## Abstract

A reimplementer who reads "`-O2`" expects **one** optimization dial. There are **four**, and they live in four different binaries with four incompatible value spaces. Conflating them is the single most common error when reasoning about how aggressively this compiler optimizes, because three of the four are never written by the user and only one of them is the integer most people imagine.

The four planes are: **(1)** the user-facing `--optlevel` / `-O{0,1,2,3}` — a *string* attribute on the Python driver, default `"2"`, in which **`-O3` is a hard, unconditional alias of `-O2`**; **(2)** `--internal-tensorizer-opt-level` — despite the name, **not a number** but a *string mode selector* whose recognized values are backend names like `"nki"` and `"eager"`; **(3)** the penguin `ir.OptLevel` — a four-member `@total_ordering` *staged-skip enum* that decides which whole compilation **stages** run, carried per-function; and **(4)** the walrus backend opt-level — the only genuine integer dial, reaching **8**, with `--smt-allocation` forcing level **7**, selected by `cl::opt` flags entirely separate from the user `-O`.

The thread that ties them together is loose, not arithmetic. The user `-O` is read at exactly three sites in `buildPipeline` and drives only two binary decisions (clear DGE at `-O0`; collapse `-O3`→`-O2`). It is **never** forwarded downstream as a numeric `-O<n>`. The penguin enum and the walrus integer are set *by construction* inside their own flows — a lower user `-O` *tends* to skip more stages and pick a lighter pass-builder, but there is no `if optlevel >= N` literal anywhere mapping the user number onto the downstream level. This page reproduces each plane against its binary, then gives the cross-plane mapping table that is the whole point.

| | |
|---|---|
| **Plane 1 — user `-O`** | `optlevel` *string* attr; `CompileCommand.buildPipeline` @ `0x619d0`; choices `kp_u_0..kp_u_3`; default `"2"`; **`-O3 ≡ -O2`** |
| **Plane 2 — tensorizer mode** | `internal_tensorizer_opt_level` *string* attr; `=="nki"` @ `buildPipeline` py L1334; `=="eager"` @ `runPipeline` py L1548 |
| **Plane 3 — penguin enum** | `ir.OptLevel(Enum)` — 4 members, `@unique @total_ordering`; `OptLevel.cpython-310-*.so` |
| **Plane 4 — walrus integer** | backend opt-level dispatcher `sub_80D9D0`; `{0, 1/2/3, 6, 7, 8}`; `7 ⇐ --smt-allocation` (D-K19) |
| **Bridge (3)** | `penguin/Compile.get_optimization_functions(target, optlevel)` @ `0x4b0c0` → `extract_nki_opt_level` @ `0x3a4d0` (`Compile.py:315`) |
| **The three read-sites of `optlevel`** | `0x629a0` (`=="0"`), `0x717f2`/`0x7180f` (`=="3"`), write `0x71862` (`="2"`) |
| **No `-O4..-O9`** | only `kp_u_0..kp_u_3` exist in the Cython string table — higher values are rejected by argparse |

---

## 1. Why "opt level" is ambiguous

The confusion is structural, not accidental, and a reimplementer must internalize it before writing any mapping code:

- The word **"optlevel"** names two different attributes in the *same* Python class hierarchy — the user `optlevel` and the internal `internal_tensorizer_opt_level` — and a third `optlevel` *parameter* on a penguin function (`get_optimization_functions(..., optlevel)`). All three are strings; none is the integer the LLVM-style `-O` convention implies.
- The only thing in the system that is genuinely an integer "0..8 master dial" is the **walrus backend** opt-level, and the user has no direct way to set it to most of its values — it is computed inside the backend from separate `cl::opt` flags.
- The penguin `ir.OptLevel` enum is *inverted*: higher member value means **fewer** optimizations, the opposite of the `-O` convention.

So "the opt level" is never a single quantity. The rest of this page treats each plane as its own object with its own value space, then shows the (deliberately loose) coupling.

> **GOTCHA — `-O` changes no top-level jobs.** Unlike a classic compiler where `-O0` strips passes from a flat list, the user `-O` here is read in only three places and flips at most two flags. The pipeline *shape* (which Jobs run) is set by `collectFrontendPipeline` / `collectWalrusPipeline` / `collectBackendPipeline` and the `enable_internal_*` toggles — see [3.3](compilecommand-pipeline.md). `-O` perturbs that shape only indirectly.

---

## 2. Plane 1 — the user `-O{0,1,2,3}` string

**Binary:** `neuronxcc/driver/commands/CompileCommand.cpython-310-*.so`. **Body:** `buildPipeline` @ file-off `0x619d0` (16 616 decompiled lines; the optlevel logic is in the normalization prologue). **Confidence: CONFIRMED** — every claim below is read directly from the decompiled body.

### 2.1 The attribute and its value space

`--optlevel` (short alias `-O`, proven by `string_tab[111] = ("-O", &__pyx_kp_u_O)`) is an `enum`-typed argparse argument whose choices are the four *string* tokens `"0"`, `"1"`, `"2"`, `"3"` (`string_tab[1..4]`, the interned `kp_u_0..kp_u_3`). The default is `"2"`. There is **no `kp_u_4..kp_u_9`** constant anywhere in the binary; `-O4`..`-O9` are rejected at parse time by `choices=["0","1","2","3"]`.

The help banner (verbatim `.rodata` @ `0x857a0`) is the user-facing contract:

```text
Optimization level. The default setting -O2 provides the best balance between
performance and compile time. -O1 enables the core performance optimizations in
the compiler, while also aiming to minimize compile-time. -O3 can provide
additional performance in some cases but may incur higher compile time and memory
usage that exceeds the accelerator device limits.
```

### 2.2 The three read-sites — the entire footprint of `-O`

In the whole 16 616-line `buildPipeline` body, `optlevel` is touched at exactly three places. This is the complete list:

| Site | py line | Operation | Effect |
|---|---|---|---|
| `0x629a0` | L1192 | `_Pyx_PyUnicode_Equals(optlevel, kp_u_0)` (decompile L5519/5548) | the **DGE-clear** gate |
| `0x717f2` / `0x7180f` | L1485 | `_Pyx_PyUnicode_Equals(kp_u_3, optlevel)` (decompile L15251/15262) | the **`-O3`→`-O2`** test |
| `0x71862` | L1486 | `SetAttr optlevel = kp_u_2` (decompile L15279) | the `-O3` rewrite + default normalize |

Everything else `-O` "does" is mediated through booleans that *other* flags also flip, then handed to `HLOToTensorizer` as option lists. `-O` itself never travels downstream as a number.

### 2.3 The `-O0` DGE-clear gate (py L1186–1193)

The reconstructed prologue (string/opcode-anchored, decompile L5046–5559):

```c
// CompileCommand.buildPipeline, py L1186-1193
self.internal_enable_dge_levels.append("io");                              // L1186 (n_u_io)
self.dge_on_levels |= set(self.internal_enable_dge_levels);                 // L1188 (PyNumber_Or)
self.dge_on_levels -= set(self.internal_disable_dge_levels);               // L1189 (InPlaceSubtract)
if ( !self.enable_dge || self.optlevel == "0" )                            // L1192: GetAttr enable_dge; Equals(optlevel, kp_u_0)
    self.dge_on_levels.clear();                                            // L1193 (decompile L5559)
```

Decompiled branch logic: `test ebx,ebx / jz` on `enable_dge` FALSE → clear; `test ebx,ebx / jnz` on `optlevel=="0"` → clear; fall-through (`enable_dge` true **and** `optlevel != "0"`) keeps `dge_on_levels` = `{…,"io"}` minus the disable set.

**Effect:** at `-O0` (or whenever `--enable-dge` is off) the *Descriptor-Generation-Engine* per-level optimization set is emptied. This is `-O0`'s **only** directly `-O`-keyed effect in the driver.

> **CORRECTION — the `enable_internal_new_backend=False` you may have seen attributed to `-O0` is not gated on `optlevel`.** The block immediately after the DGE gate (py L1196–1199) sets `internal_run_standalone_legacy_compilation = True` and `enable_internal_new_backend = False` — but the guard is the **experimental-tensorized-scheduler** flag (`@0x629f2`), *not* `optlevel=="0"`. An earlier wave (D-A05) conflated the two adjacent prologue blocks. The single direct `-O0` effect is `dge_on_levels.clear()`. **(CONFIRMED.)**

### 2.4 The `-O3 ≡ -O2` collapse (py L1485–1490)

This is the most important and most surprising fact on the page. The collapse runs **unconditionally** on the flat normalization path — there is no guarding `if` other than the `optlevel=="3"` test itself (decompile L15251–15378):

```c
// CompileCommand.buildPipeline, py L1485-1490
if ( self.optlevel == "3" ) {                                  // L1485 (decompile L15262: Equals(kp_u_3, optlevel))
    self.optlevel = "2";                                       // L1486 (decompile L15279: SetAttr kp_u_2)
    self.layer_unroll_factor = 0;                              // L1487 (decompile L15288: SetAttr int_0)
    self.layer_unroll_factor_Used = True;                      // L1488 (PyObject_SetAttr layer_unroll_factor_Used)
    self.log(logging.USER,                                     // L1489 (decompile L15306 GetAttr log; L15347 logging.USER)
             "The -O3 setting is not optimized for compilation time. "
             "Please refer to the neuron documentation ...");  // L1490 (kp_u_The_O3_setting_is_not_optimized @ decompile L15375)
}
```

So in **this** build, `-O3`:

1. rewrites `optlevel` to `"2"` — there is **no** downstream branch that ever sees `optlevel=="3"`;
2. pins `layer_unroll_factor = 0`;
3. marks `layer_unroll_factor_Used = True`, which *suppresses* the fsdp unroll default (§2.5);
4. emits a `USER`-level advisory that `-O3` is not compile-time-optimized.

> **QUIRK — `-O3` is strictly *worse-behaved* than `-O2`, not better.** It produces the exact `-O2` optimization behavior *plus* a forced `layer_unroll_factor=0` *plus* a warning. The help text's promise of "additional performance" is not delivered by any code path in this build; the value `"3"` is consumed and discarded in the prologue. A reimplementer should model `-O3` as "`-O2` with unroll pinned to 0 and a printed advisory." **(CONFIRMED — the equality vs `kp_u_3` then triple-`SetAttr` is fully decompiled.)**

> **CORRECTION — the collapse is unconditional.** An earlier catalog (D-AF02) described `-O3` as falling back to `-O2` "when its preconditions are not met." There are no preconditions; the only guard is `optlevel=="3"`. The degrade always happens, and it additionally pins unroll to 0. **(CONFIRMED.)**

### 2.5 `layer_unroll_factor` — a distribution knob, not an `-O` knob

The only place `layer_unroll_factor` gets a *nonzero* default is the fsdp distribution-strategy arm (py L1406–1414, decompile L14029–14060):

```c
if ( self.distribution_strategy == "fsdp" )                    // L1406 (Equals n_u_fsdp)
    if ( !getattr(self, "layer_unroll_factor_Used", False) ) { // L1412 (GetAttr3, default Py_False)
        self.layer_unroll_factor = 4;                          // L1413 (decompile L14060: SetAttr int_4)
        self.layer_unroll_factor_Used = True;                  // L1414
    }
```

Because the fsdp arm only bumps unroll when `_Used` is still `False`, an explicit `-O3` (which sets `_Used = True` with factor 0 in §2.4) **suppresses** the fsdp unroll bump. `layer_unroll_factor > 0` is also the gate that makes `collectBackendPipeline` emit the Backend sub-list ([Part 8](../walrus/)). So unroll is primarily a *distribution-strategy* knob; `-O` touches it only to zero it at `-O3`.

### 2.6 Per-level summary (driver-prologue effects only)

| `-O` | Direct `buildPipeline` effect | Help (verbatim) |
|---|---|---|
| `-O0` | `dge_on_levels.clear()` (py L1193, gate `!enable_dge \|\| optlevel=="0"`) → DGE per-level opt OFF | (banner only) "minimal opt / fastest" |
| `-O1` | none keyed on `"1"` | "Enables the core performance optimizations …, while also aiming to minimize compile-time." |
| `-O2` | **default** (set when unset); none keyed on `"2"` beyond being the fallback target | "best balance between performance and compile time." |
| `-O3` | **hard alias → `optlevel="2"`** + `layer_unroll_factor=0` + `_Used=True` + USER advisory (py L1486–1490) | "additional performance … may incur higher compile time and memory …" |

---

## 3. Plane 2 — `--internal-tensorizer-opt-level` (a string mode, not a number)

**Binary:** `CompileCommand.cpython-310-*.so` (driver) + `starfish/penguin/Options.so`. **Confidence: CONFIRMED string + CONFIRMED single use; STRONG semantics.**

The name screams "numeric opt level." It is not. The attribute `internal_tensorizer_opt_level` (dest literal @ `0x868b0`, auto-INTERNAL via the `internal-` prefix rule; no help string) is read in only two places, and the only comparisons performed on it are against **mode-name strings**:

```c
// buildPipeline @ decompile L11940-11953, py L1334
v791 = GetAttrStr(self, "internal_tensorizer_opt_level");
v792 = _Pyx_PyUnicode_Equals(v791, n_u_nki);                   // == "nki"  (the ONLY comparison here)
if ( v792 /* and not the nki-validation-disable, py L1335 */ ) {
    self.tensorizer_options.append("run_simulator_after_BirCodeGenLoop"); // py L1337 (decompile ~L12006)
    /* pull in self.tolerance */                               // py L1338 (decompile L12034/12074)
}
```

```c
// runPipeline @ 0x7af50, py L1548
if ( self.internal_tensorizer_opt_level == "eager" )           // RichCompare vs n_u_eager @0x7af80
    InitGlobalState(...);                                       // py L1549
```

So the recognized values include at least `"nki"` and `"eager"`. The attribute is a **backend/path selector**, never an integer. When `== "nki"` (and NKI validation is not disabled) it routes onto the NKI codegen path and appends the BIR simulator stage; when `== "eager"` it routes `runPipeline` into `InitGlobalState`.

> **GOTCHA — `--internal-tensorizer-opt-level` does not numerically override `-O`.** It selects the *backend* (NKI vs the default tensorizer path). The numeric "which stages run" decision is the penguin `ir.OptLevel` enum (§4), set inside the penguin flow — not this driver attribute. A reimplementer modeling this as `opt_level = max(user_O, internal_tensorizer_opt_level)` would be wrong: the two are not on the same axis. **(CONFIRMED disasm.)**

> **CORRECTION — there is no numeric "internal level 7" on this attribute.** A wave-1 note ("optlevel 7 / smt-allocation") referred to the *walrus* backend opt-level (§5), an entirely different plane reached via `--smt-allocation`. This Python attribute carries mode *names*. **(CONFIRMED.)**

---

## 4. Plane 3 — the penguin `ir.OptLevel` staged-skip enum

**Binary:** `neuronxcc/starfish/penguin/ir/OptLevel.cpython-310-*.so`. **Confidence: CONFIRMED** (member set, order, docstring, `__gt__` all read from the decompile + string table).

### 4.1 The class

`OptLevel(Enum)` is decorated `@unique` and `@total_ordering` (interned `n_s_unique`, `n_s_total_ordering` both imported in `pymod_exec`; a custom `__gt__` is defined). The docstring is verbatim from `kp_s_The_optimization_level_of_an_pe`:

```text
The optimization level of an penguin function.

The optimization toward the end of this list will run less optimizations
```

(The grammatical slips — "an penguin", missing period — are byte-verbatim and serve as a fingerprint for the right symbol.)

### 4.2 The four members and their order

The class body defines exactly **four** members with `enum.auto()`. Confirmed from the Cython string table (`Pyx_CreateStringTabAndInitStrings`), where each name is interned with its `_mm_insert_epi64` literal:

| `auto()` value | Member | string_tab slot | Meaning |
|---|---|---|---|
| 1 | `default_level` | `[10]` `"default_level"` | FULL: middle-end + loop-level + ISel + allocators + peephole |
| 2 | `skip_loop_level_transformations` | `[32]` | drop loop-level transforms; keep rest |
| 3 | `skip_middle_end` | `[33]` | drop the whole middle-end opt block |
| 4 | `skip_allocators` | `[31]` | additionally skip the allocator stage (codegen-only / prepare-ish) |

Because of `@total_ordering` + `auto()`, **higher value ⇒ later in the list ⇒ fewer optimizations**: `default_level (1) < skip_loop_level_transformations (2) < skip_middle_end (3) < skip_allocators (4)`.

> **CORRECTION — exactly four members; no `prepare`, no `allow_none`.** An earlier roster (D-U01) listed `prepare` and `allow_none` as members. In this binary, `prepare` is the metaclass `__prepare__` hook (a single load in class-body construction), and there is no `allow_none` member at all — only the four `auto()` `SetItem`s above. **(CONFIRMED: the four member names are the only ones in the string table alongside the docstring.)**

### 4.3 `__gt__` — ordering on the underlying int

The comparison is the obvious one (full decompile, `OptLevel.py:34-36`):

```c
def __gt__(self, other):                  // OptLevel.py:34
    if not isinstance(other, OptLevel):   // L35
        return NotImplemented
    return self.value > other.value       // L36 (GetAttr "value" on both; RichCompare Py_GT)
```

`@total_ordering` synthesizes `<`, `<=`, `>=` from this `__gt__` plus `Enum`'s `__eq__`.

### 4.4 How the enum gates stages

The per-function attribute `function_opt_level` (an `OptLevel` instance) is compared against the named thresholds at *flow* level, dropping whole stages (anchors in the NKI codegen flow generator):

```c
// NKICodeGenFlow (gen-level)
if ( function_opt_level <= OptLevel.skip_loop_level_transformations )  // gen L382-423
    // fall through to shared_codegen_flow.peephole_optimizations() — loop-level stage dropped
...
if ( function_opt_level >= getattr(OptLevel, "skip_allocators") )      // gen L1304
    // skip StackAllocator / RelocateNKIStack / LegalizeInstSize
```

The resulting stage map (taxonomy CONFIRMED; the exact user-`-O`→member cutoff is INFERRED — it is set per-function from the tensorizer, not a static literal table):

```text
  default_level                    →  middle-end + loop-level + ISel + allocators + peephole   (FULL)
  skip_loop_level_transformations  →  drop loop-level transforms; keep the rest
  skip_middle_end                  →  drop the whole middle-end opt block
  skip_allocators                  →  additionally skip the allocator stage  (codegen-only)
```

### 4.5 The bridge: `penguin/Compile.get_optimization_functions`

**Binary:** `starfish/penguin/Compile.cpython-310-*.so`, `get_optimization_functions` @ `0x4b0c0`. This is where a *string* mode selects an optimization-function set and pulls in the per-function enum:

```c
// get_optimization_functions(target, optlevel), Compile.py
if ( optlevel == "eager" )                          // decompile L881 (Equals n_u_eager)
    return eager_optimization_function_set;
if ( optlevel == "nki" )                            // decompile L1066 (Equals n_u_nki)
    extract_nki_opt_level(cu); ... return nki_set;  // decompile L1290/1319 (GetBuiltin extract_nki_opt_level)
if ( optlevel == "codegen_only" )                   // decompile L1301 (Equals kp_u_codegen_only)
    extract_nki_opt_level(cu); ...                  // the codegen-only mode also reads the per-fn level
return default_optimization_function_set;
```

`extract_nki_opt_level(cu)` (@ `0x3a4d0`, `Compile.py:315`) reads the `opt_level` attribute off the compile-unit's per-function object (`GetAttr n_s_opt_level`, decompile L381) and asserts the cu type ("Unexpected cu type"). Its docstring: *"optlevel: The optimization level ('eager', 'nki', or other)."*

> **NOTE — a third recognized mode, `codegen_only`.** Besides `eager` and `nki`, `get_optimization_functions` also branches on `kp_u_codegen_only` (decompile L1301), which likewise calls `extract_nki_opt_level`. So at the penguin layer the `optlevel` *parameter* is a string mode over at least `{eager, nki, codegen_only, …default}`, and the numeric `OptLevel` enum is carried **per-function** — there is no global "user `-O2` == `OptLevel.default_level`" literal in the shipped binaries. The coupling is by construction. **(CONFIRMED dispatch; INFERRED exact user-`-O` ↔ enum mapping.)**

---

## 5. Plane 4 — the walrus backend opt-level (the real `0..8` integer)

**Binary:** the `walrus_driver` backend ELF (the codegen/allocator engine). The dispatcher is `sub_80D9D0`. **Confidence: CONFIRMED via the dedicated backend analysis (D-K19); this page owns only the *cross-plane* statement — Plane-4 mechanics are owned in depth by [Part 8](../walrus/).**

This is the one plane that is a genuine integer, and the only one that reaches 7 and 8. It is **not** a Python attribute and is **not** selected by `--internal-tensorizer-opt-level` or by the user `-O` number directly. It is normalized and switched inside the backend:

```c
// sub_80D9D0 prologue (normalization), then the dispatch switch
assert( allocator in {"", "coloring", "lsa"} );
if ( loopsInBackend )   optlevel = (optlevel == 2) ? 6 : 8;   // backend-loops upgrades 2→6, else →8
if ( SmtAllocation )    optlevel = 7;                          // --smt-allocation forces 7 (+ cl::opt callback)
assert( vncNcCount == 1 || (allocator != "lsa" && optlevel != 0 && optlevel != 8) );

switch ( optlevel ) {                                         // the pass-builder selector
  case 0:        sub_806F80;   break;   // minimal builder
  case 1: case 2: case 3:  sub_80A6E0; break;   // primary pre-codegen builder
  case 6:        sub_807EF0;   break;   // coloring_allocator WITH loop (loop-aware)
  case 7:        /* INLINE ~70 passes — the SmtAllocation arm: per-space coloring_allocator,
                    forbids partitioner + call-graph, runs Unroll::check_in_place. NO Z3/SMT solver. */
  case 8:        sub_809580;   break;   // linear_scan_allocator
}
```

Key cross-plane facts (all CONFIRMED in D-K19):

- The walrus level **reaches 8**. Level **7** is the `--smt-allocation` experimental arm; the `SmtAllocation` `cl::opt` is the *only* thing that forces it. The persisted `PassOptions` byte `+0x6D` carries the smt bit; `Unroll::run` invokes `check_in_place` only when that byte is set (opt7). In opt0/1-3/6/8 the byte is 0 and the in-place re-check is skipped.
- Despite the name, there is **no Z3 / SMT solver** in the opt7 path — `check_in_place` is an in-place unroll re-check, not a constraint solver.
- **No user `-O` level turns this on.** `-O{0,1,2,3}` never sets `SmtAllocation`. Walrus opt7 is orthogonal to the user `-O` dial.
- Allocator *class* = `--allocator` string (`coloring` vs `lsa`) × the walrus opt-level case (opt6 → coloring-with-loop; opt7 → plain per-space coloring; opt8 → linear-scan). Whether allocators run *at all* is the penguin `OptLevel.skip_allocators` gate (§4.4) — a different plane again.

> **GOTCHA — `--smt-allocation` is the only door to walrus opt7, and it is not an `-O` value.** Reimplementers who expect `-O3` to mean "maximum" will miss that the most aggressive *allocator* arm (7) and the linear-scan arm (8) are reached through backend `cl::opt` flags and the `loopsInBackend` upgrade, after the user `-O` has already been mapped into the forwarded `--walrus-passes` list. See [Part 8](../walrus/) and [3.7](walrus-driver-cli.md). **(CONFIRMED D-K19.)**

### 5.1 What `-O` does *not* gate in the backend

Two backend features are commonly but wrongly assumed to be `-O`-gated:

- **SMT allocator** ⇐ `--smt-allocation` only (walrus opt7). No `if user_O >= N` exists.
- **TritiumFusion autotuner** ⇐ the per-pass `cl::opt enable-tritium-loopfusion`, with extra-tripcount variants behind `internal-autotune-tritium-use-more-tripcounts`. Its only coupling to `-O` is *indirect*: at a low penguin `OptLevel` the middle-end stage that contains TritiumFusion can be skipped wholesale (§4.4, `skip_middle_end`), so the autotuner simply never runs.

---

## 6. The cross-plane mapping — tracing one `-O` through all four

This table is the centerpiece. It traces a single user `-O` value left-to-right through every plane, and marks where each plane's value is *set by something else* (the loose coupling).

| User `-O` (string) | Plane 1 — driver direct effect | Plane 2 — tensorizer mode | Plane 3 — penguin `ir.OptLevel` (per-fn) | Plane 4 — walrus integer |
|---|---|---|---|---|
| `-O0` | `dge_on_levels.clear()` (DGE off) | *unchanged* — set by `--internal-tensorizer-opt-level` (`nki`/`eager`/…), not by `-O` | *by construction* tends toward a higher member (more stages skipped) — no literal cutoff | *by construction* tends toward a lighter builder — but set by backend `cl::opt`/`loopsInBackend`, not `-O0` |
| `-O1` | none | *unchanged* | *by construction* | *unchanged* |
| `-O2` (default) | none (it is the default + fallback target) | *unchanged* | *by construction* `default_level`-ish full flow | normalized cases `1/2/3` → primary builder `sub_80A6E0` (unless upgraded) |
| `-O3` | **→ `-O2`** + `layer_unroll_factor=0` + `_Used=True` + USER advisory | *unchanged* | identical to `-O2` (value `"3"` never survives) | identical to `-O2` |
| (orthogonal) | — | — | — | `--smt-allocation` → opt **7**; `loopsInBackend` → 2 upgrades to **6**, else **8** |

Reading the table: a user `-O` only ever moves Plane 1 deterministically. Planes 2–4 have their own value spaces and their own inputs; the arrows from `-O` to them are dashed ("tends toward"), never solid. The single hard cross-plane rewrite in the whole system is **`-O3 → -O2`** inside Plane 1.

### 6.1 The mapping algorithm, as pseudocode

```python
# How ONE user -O value is processed. Real symbols / attrs named inline.
def normalize_optlevel(self):                      # inside CompileCommand.buildPipeline @ 0x619d0
    # --- Plane 1: the only place the user -O is consumed ---
    if not self.optlevel:                           # default normalizer
        self.optlevel = "2"                         # kp_u_2

    # DGE-clear gate (py L1192)
    if (not self.enable_dge) or (self.optlevel == "0"):   # Equals(optlevel, kp_u_0)
        self.dge_on_levels.clear()

    # -O3 hard alias (py L1485-1490) — UNCONDITIONAL
    if self.optlevel == "3":                        # Equals(kp_u_3, optlevel)
        self.optlevel = "2"                         # SetAttr kp_u_2  -> -O3 ≡ -O2
        self.layer_unroll_factor = 0
        self.layer_unroll_factor_Used = True
        self.log(logging.USER, "The -O3 setting is not optimized for compilation time. ...")

    # NOTE: self.optlevel is NEVER forwarded downstream as a number.
    # Planes 2-4 are selected independently:

    # --- Plane 2: a STRING backend mode, set by a separate flag ---
    if self.internal_tensorizer_opt_level == "nki":          # py L1334, NOT a number
        self.tensorizer_options.append("run_simulator_after_BirCodeGenLoop")
    # (runPipeline later: if internal_tensorizer_opt_level == "eager": InitGlobalState())

    # --- Plane 3: per-function enum, chosen inside the penguin flow ---
    # penguin/Compile.get_optimization_functions(target, optlevel_string):
    #   "eager" -> eager set ; "nki"/"codegen_only" -> extract_nki_opt_level(cu) ; else default
    #   extract_nki_opt_level reads cu.<fn>.opt_level : ir.OptLevel  (1..4, higher = fewer opts)

    # --- Plane 4: the real integer, normalized in the backend dispatcher sub_80D9D0 ---
    #   walrus_optlevel starts from the forwarded --walrus-passes config, THEN:
    #     if loopsInBackend: walrus_optlevel = 6 if walrus_optlevel == 2 else 8
    #     if SmtAllocation (--smt-allocation): walrus_optlevel = 7
    #   switch(walrus_optlevel): 0|1/2/3|6|7|8 -> distinct pass-builders
```

---

## 7. Reimplementation contract

To reproduce this build's opt-level behavior, a reimplementer must honor all of:

1. `--optlevel` / `-O` accepts **only** `{0,1,2,3}`, default `"2"`, as **strings**. Reject `-O4..-O9` at parse time.
2. `-O0` clears `dge_on_levels` (also cleared when `--enable-dge` is off). No other direct `-O0` effect.
3. `-O3` is an **unconditional** rewrite to `-O2`, additionally pinning `layer_unroll_factor=0`, setting `layer_unroll_factor_Used=True`, and logging the USER advisory. No code path sees `optlevel=="3"` afterward.
4. `--internal-tensorizer-opt-level` is a **string mode** (`nki`/`eager`/…), not a number; `nki` appends the BIR simulator stage and pulls `tolerance`; `eager` routes `runPipeline` to `InitGlobalState`.
5. `ir.OptLevel` has **four** members in `auto()` order `{default_level, skip_loop_level_transformations, skip_middle_end, skip_allocators}`, `@total_ordering` on `value`, inverted (higher = fewer opts), carried **per-function** as `function_opt_level`.
6. The penguin `optlevel` *parameter* (`get_optimization_functions`) dispatches on `{eager, nki, codegen_only, default}`; `extract_nki_opt_level` reads the per-function `opt_level` enum.
7. The walrus backend opt-level is an **integer** `{0, 1/2/3, 6, 7, 8}`, normalized in `sub_80D9D0`: `loopsInBackend` upgrades `2→6` else `→8`; `--smt-allocation` forces `7`. It reaches 8. No Z3/SMT solver in opt7.
8. **Do not** model any plane as a numeric override of another. The only hard cross-plane coupling is `-O3 → -O2`.

---

## 8. Confidence ledger

- **CONFIRMED** — `-O` choice-set `{0,1,2,3}` + default `"2"`; the three `optlevel` read-sites; the `-O0` DGE-clear gate and its branches; the `-O3`→`-O2` triple-`SetAttr` + advisory (full decompile L15262–15378); `internal_tensorizer_opt_level == "nki"` (decompile L11953) and `== "eager"` (runPipeline); the `ir.OptLevel` four-member `auto()` set + order + docstring + `__gt__` (string table + decompile); `get_optimization_functions` `eager`/`nki`/`codegen_only` dispatch (decompile L881/L1066/L1301) and `extract_nki_opt_level` @ `Compile.py:315`; the walrus dispatcher switch `{0,1/2/3,6,7,8}` and `--smt-allocation`→7 gate (D-K19).
- **STRONG** — the Cython→Python reconstruction of the `buildPipeline` prologue (control flow + string identities certain; exact py text is the conventional lift); the flow-level `OptLevel` comparison direction.
- **INFERRED** — the exact user-`-O` number → `ir.OptLevel` member numeric cutoff (set per-function from the tensorizer, not a static literal table); the precise penguin-`optlevel`-string ↔ user-`-O` coupling.
- **Plane-4 ownership** — the walrus opt-level mechanics are CONFIRMED in the dedicated backend analysis (D-K19); this page reproduces only the cross-plane statement and defers depth to [Part 8](../walrus/).

## See also

- [3.7 — walrus driver CLI](walrus-driver-cli.md) — the backend `cl::opt` surface, including `--smt-allocation`.
- [3.8 — CompileCommand flag catalog](flag-catalog.md) — the full `--optlevel` / `--internal-tensorizer-opt-level` registration.
- [3.3 — CompileCommand pipeline & job order](compilecommand-pipeline.md) — why `-O` does not add or remove top-level jobs.
- [Part 8 — the walrus backend](../walrus/) — the integer opt-level dispatcher and allocator selection in full.
