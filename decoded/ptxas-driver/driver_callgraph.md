# ptxas compilation driver & pipeline orchestration (binary-derived, CUDA 13.0.88)

All addresses from the stripped ptxas binary. VMA == file offset for `.text`
and `.rodata`. Binary is ground truth; mismatches with any external reference
are flagged as drift.

## Full call chain: ELF entry -> phase dispatch

```
start (0x42333c)                              ; ELF entry
  main (0x409460)                             ; setvbuf(stdout/stderr); -> sub_446240
    sub_446240 (0x446240)                     ; THE DRIVER: CLI parse + module compile
      |   - parses argv into the "parsed-options" struct (the a2 read downstream)
      |       -O (raw 0..3)   -> parsed-opts+0x70 (112)
      |       target/arch     -> parsed-opts+704 / +352 / +360 ...
      |   - [PTX parse -> IR build]  (module-level front-end; virtual-dispatched callees)
      |
      +-- per-function backend  (C++ virtual dispatch; one target class per SM family)
          |   target backend class built by sub_607DB0 (0x607db0);
          |   its 24-entry per-target trampoline table refs wrappers
          |   sub_608F20 / sub_609B40 .. sub_609F30
          |
          v  (virtual "compile function" slot ->)
          one of 24 wrappers, e.g. sub_608F20 (0x608f20)  -> sub_663C30
            sub_663C30 (0x663c30)             ; per-function backend body
              +-- sub_662920 (0x662920)       ; BUILD per-function context (sole caller of the OCG ctor)
              |     sub_7F7DC0 (0x7f7dc0)      ; <<< OCG-CONTEXT CONSTRUCTOR
              |        reads parsed-options a2; writes the ~2140-byte ctx,
              |        including opt-level ctx+2104 (0x838) -- see opt_level_model.md
              +-- sub_7FBB70 (0x7fbb70)        ; backend entry (per-function ctx)
                    +-- sub_C173E0 (0xc173e0)  ; DAG / codegen setup (~3553 LOC); bail if returns 0
                    +-- if (ctx+1428 < 0): emit "\nFunction name: <name>\n" via sub_7FE930
                    +-- ctx+1416 |= 0x80
                    +-- return sub_7FB6C0 (0x7fb6c0)   ; <<< THE OPTIMIZATION DRIVER
```

Verified at the binary: `sub_7FBB70` calls `sub_C173E0`, conditionally emits
the "Function name: " banner when `ctx+1428 < 0`, ORs 0x80 into ctx+1416, then
tail-calls `sub_7FB6C0`.

## sub_7FB6C0 -- the optimization driver

```c
sub_7FB6C0(ctx):
    ocg = *(ctx + 0x680)                 // OCG knob/config object
    if knob(298) active:                 // checked via vtable slot +72; fast path reads ocg.cfg+0x53D0
        sub_9F63D0(ctx)                  // RECIPE/DEBUG pipeline (custom schedule, can reach 157/158)
    else:                                // NORMAL pipeline
        sub_C62720(&PM, ctx)             // 1. build PhaseManager -> registers 159 phase objects
        rax,rdx = sub_C60D20()           // 2. rax=&order_table(0x22BEEA0), rdx=count=157 (0x9D)
        sub_C64F70(&PM, rax, 157)        // 3. RUN: dispatch the 157 ordered phases (IDs 0..156)
        ... teardown ctx+1880 / +1872 / +1864 scratch objects ...
        sub_C61B20(&PM)                  // 4. destroy PhaseManager
    return 1
```

The 157-vs-159 crux is fully documented in `phase_dispatch_157_vs_159.md`.
Summary: **159 registered, 157 dispatched by default.**

### The OCG-knob mechanism (how every branch in the driver is gated)

The driver makes its control-flow decisions by reading **OCG knobs**. An OCG
knob is one **72-byte entry** in a flat array at `config+0x48`; the config
object is `ctx+0x680` (or equivalently the options view `ctx+0x1664`). The
config offset of knob N is exactly `N*72` (`0x48`-relative). Two inlined leaf
accessors recur, compared by address:

| Accessor | addr | semantics |
|---|---|---|
| bool | `sub_6614A0` | `knobs=obj+0x48; e=knobs+idx*72; return e[0]!=0` |
| value | `sub_6614C0` | same; `if e[0]==5 return e[8] else 0` |

When the live accessor object's vtable slot does NOT match these fast leaves,
the driver does a virtual call `(*slot)(obj, idx)` with the knob index. The
knobs the driver/optimizer actually consults:

| Knob | config off | read at | role |
|---|---|---|---|
| 249 | `0x4608` | `sub_C62720` 0xc62786 | phase-wise compile-stats -> PM+0x48 (timing report on/off) |
| 298 | `0x53D0` | `sub_7FB6C0` 0x7fb6ef | recipe/named-phases selector -> `sub_9F63D0` path |
| 298 | `0x53D8` | `sub_9F4040` 0x9f4258 | recipe STRING value (tag==5, ptr at entry+8) -> tokenizer `sub_798B60` |
| 391 | `0x6DF8` | `sub_C62720` 0xc6297d | NvOptRecipe present? -> wires the `ApplyNvOptRecipes` (phase ID 1) object |
| 391 | `0x6E00` | `sub_C62720` 0xc62ac9 | NvOptRecipe value -> recipe-apply object +312 |
| 499 | `0x8C58` | `sub_7DDB50` | "disable optimization" -> forces opt level 1 past a budget counter |

All offsets `id*72` verified exact. See `ocg_knobs.tsv` for the full table.

### The recipe / named-phases override path (`sub_9F63D0` -> `sub_9F4040`)

When knob 298 is set, `sub_9F63D0` builds the PhaseManager (same
`sub_C62720`), seeds a 256-int buffer with `[0]=158` (NOP), calls
`sub_9F4040` to compute a **custom schedule + length**, and feeds that to the
**same** `sub_C64F70` dispatch loop. `sub_9F4040` starts from the default
157-identity schedule (`sub_C60D20()`), tokenizes the recipe string
(`sub_798B60`), and applies a small DSL — `NamedPhases`, `p%d` index-select,
`shuffle`/`reps`, `swap1..6`, `dce1..3` (inject `OriPerformLiveDead`),
`cpy1..3` (inject `OriCopyProp`) — with explicit IDs **clamped to [0,159]**
and unknown names mapped to 158/NOP (`sub_C641D0`). This is the **only** way
phase IDs 157/158 enter a schedule. Full DSL in `recipe_override_dsl.tsv`.

## OCG-context construction (sub_7F7DC0) and option mapping

`sub_7F7DC0(ctx, parsed_opts, a3)` zero-inits the context, builds its
sub-allocators, then copies/normalizes option fields. The fields that matter
for the driver/opt model:

| parsed-opts off | meaning | -> context field | note |
|---|---|---|---|
| +112 (0x70) | raw `-O` (0..3) | ctx+2104 (0x838) | then remapped O0/1->1, O2->2, O3->4 |
| +704 | target/arch | ctx+1424 | |
| +352 / +360 | target params | ctx+1428 / ctx+1432 | ctx+1428 < 0 triggers "Function name:" banner |
| +116/120/124/128 | scheduling limits | ctx+1704/1708/1712/1716 | |
| +132 | reg/limit | ctx+1720 (default 512) | |
| many +88..+1808 | feature/knob flags | packed bits ctx+1396..+1421 | each `(opt != 0)` -> one bit |

## PhaseManager (sub_C62720) -- what it builds

- Reads the **timing knob 249** first: `PM+0x48 = (*(*(ctx+1664)+72) + 17928 != 0)`
  (`17928 = 249*72`) — the phase-wise compile-stats flag that turns the
  Before/After/Summary report on.
- Allocates a 1272-byte (159 * 8) `const char*` name table, copied from
  `off_22BD0C0`; writes name count **159** at PM+0x6c (`*((_DWORD*)v2+27)=159`),
  registered count `+= 159` at PM+0x68, reserves 159 queue slots.
- Build loop `for (id = 0; id != 159; ++id) sub_C60D30(&tmp, &PM, id)` calls
  the factory to construct one 16-byte phase object per ID (vtable per phase).
- The factory `sub_C60D30` is a 159-arm jump table (`cmp $0x9e` upper bound,
  valid 0..158; all 159 arms distinct); arm 157 -> DebuggerBreak ctor
  (0xc60d61), arm 158 -> NOP ctor (0xc61ae8).
- After the build loop, the ctor reads **NvOptRecipe knob 391**
  (`*(*(ctx+1664)+72) + 28152`, `28152 = 391*72`). When set, it reads the
  recipe value (knob 391 value form, `+28160 = 391*72+8`) and constructs a
  440-byte "recipe-apply" object, stashing the recipe pointer at its +312 —
  this is the data consumed by the `ApplyNvOptRecipes` phase (ID 1, the
  second phase in the default schedule).

## Phase-wise timing report (the "Before/After" / "All Phases Summary" output)

- `sub_C64F70` emits a `"Before <phase>"` line before, and `"After <phase>"`
  after, each dispatched phase, and an `"All Phases Summary"` at the end --
  but only when the byte at **PM+72 (0x48)** is set.
- That flag is set in the PhaseManager ctor:
  ```
  c62782: mov 0x48(%rax),%rax        ; rax = OCG knob-config object (from ctx+0x680 -> +0x48)
  c62786: cmpb $0x0,0x4608(%rax)     ; knobConfig+0x4608 != 0 ?
  c627ab: setne 0x48(%rbp)           ; PM.timing_enabled = result
  ```
  i.e. **PM+72 = (knobConfig+0x4608 != 0)**.
- knobConfig+0x4608 is the **phase-wise compile-stats** sub-flag, driven by the
  `phase-wise/p` selector of the compiler-statistics option (option help string:
  "... time/t : Prints compilation time. memory/m : Prints peak memory usage.
  phase-wise/p : Prints the above data for various compiler phases. ...").
  So enabling phase-wise compile-stats turns on the per-phase Before/After/Summary
  report.

## Driver stages (high level)

1. `main` / `sub_446240` -- startup, CLI option parsing -> parsed-options struct.
2. PTX parse -> IR build (module-level front-end).
3. Per-function backend (virtual dispatch -> 24 target trampolines -> `sub_663C30`):
   a. context build: `sub_662920` -> OCG ctor `sub_7F7DC0`
   b. DAG / codegen setup: `sub_C173E0`
   c. optimize: `sub_7FB6C0` -> build (`sub_C62720`, 159) -> dispatch
      (`sub_C64F70`, 157) -> destroy (`sub_C61B20`)
   d. SASS emit happens inside the dispatched tail phases
      (FormatCodeList=153, DumpNVuCodeText=155, DumpNVuCodeHex=156) and in
      `sub_663C30`'s post-pass.
4. ELF / cubin output -- module-level, after all functions, back in `sub_446240`.

## Known limits / drift notes

- The per-function virtual-dispatch site above `sub_663C30` is C++ vtable
  dispatch; the static call graph shows the 24 trampolines and the 5
  target-class builders as reachable only through data (`type 1`) xrefs, not
  direct `call` edges. The exact `call *vtable[slot]` lives in the
  module-compile loop reached from `sub_446240`.
- Phase *counts* differ from older toolkits (the dispatch *mechanism* is
  stable: a PhaseManager that registers every phase + a default order list
  that names the subset to run). For 13.0.88 the binary is authoritative:
  159 registered / 157 default.
