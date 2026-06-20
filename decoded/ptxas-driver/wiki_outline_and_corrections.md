# Wiki outline + corrections for the driver / pipeline pages (CUDA 13.0.88)

Binary-derived. These are outlines and CORRECTIONS for the existing wiki pages.
Do NOT confuse "registered" (159) with "dispatched by default" (157).

## Hard corrections needed in the CURRENT wiki

The current `src/pipeline/optimizer.md` and `src/passes/*` pages contain
drift versus the binary. The following are wrong and must be fixed:

1. **"159-phase pipeline" / "runs every phase unconditionally in order."**
   The PhaseManager *registers* 159 phase objects (IDs 0..158), but the
   default driver dispatches **157** (IDs 0..156). The order accessor
   `sub_C60D20` returns count **0x9D = 157**, and the dispatch loop
   `sub_C64F70` walks `base .. base+157*4`. State it as: 159 registered, 157
   dispatched by default.

2. **Phase numbering is shifted.** The page lists DebuggerBreak=131,
   DumpNVuCodeText/Hex=129/130, and claims "phases 139..158 are 20 dynamic
   phases not in the name table." The binary's 159-entry name table
   (`off_22BD0C0`, every index 0..158 named) and `phase_names.json` give the
   true tail:
   - 153 = FormatCodeList
   - 155 = DumpNVuCodeText
   - 156 = DumpNVuCodeHex   (LAST default-dispatched phase)
   - 157 = DebuggerBreak     (registered, NOT in default order)
   - 158 = NOP               (registered, NOT in default order)
   There is no "139 named + 20 unnamed" split: all 159 are named in the table.

3. **"isNoOp() returning true causes the dispatch loop to skip the phase."**
   FALSE. In `sub_C64F70`, the phase's `Execute` (vtable slot +0,
   `call *(%rax)` at 0xc6508d) is invoked on EVERY iteration. The `isNoOp()`
   virtual (slot +16, `call *0x10(%rax)` at 0xc65078 / 0xc65095) gates ONLY
   whether the "Before <name>" / "After <name>" timing banner is built and
   printed. Phases that should do nothing return early INSIDE their own
   Execute body (opt-level / knob / predicate checks). The dispatch loop is
   unconditional over the 157 entries.

4. **"DebuggerBreak (157) and NOP (158) run in the default pipeline."**
   They do not. They are the two debugging-only "extra dispatch" phases,
   reachable only via the debug pipeline `sub_9F63D0` (OCG knob 298).

## Outline: `src/pipeline/overview.md`

- Driver stages: `start (0x42333c)` -> `main (0x409460)` -> `sub_446240`
  (CLI parse + module compile) -> PTX parse/IR build -> per-function backend
  (virtual dispatch) -> ELF/cubin output.
- The per-function backend chain (one ASCII diagram):
  `sub_446240 -> [24 target trampolines] -> sub_663C30 -> sub_662920
   (-> OCG ctor sub_7F7DC0) + sub_7FBB70 (-> sub_C173E0 + sub_7FB6C0)`.
- Where the OCG context is built (`sub_7F7DC0`) and what it carries
  (opt-level at +0x838, knob object at +0x680, options object at +1664).
- Pointer to the dedicated optimizer page for phase dispatch.

## Outline: `src/pipeline/entry.md`

- `start`/`main`: setvbuf, hand off to `sub_446240`.
- `sub_446240`: argv -> parsed-options struct (the `a2` consumed by the OCG
  ctor); -O at +0x70, target/arch at +704/+352/+360.
- Per-function backend entry `sub_7FBB70`: calls `sub_C173E0` (DAG/codegen
  setup; bails if 0), conditionally prints "Function name:" when ctx+1428<0,
  sets ctx+1416 |= 0x80, tail-calls `sub_7FB6C0`.
- Note the C++ virtual dispatch for target selection (24 trampolines, 5
  target-class builders via `sub_607DB0`).

## Outline: `src/pipeline/optimizer.md` (rewrite the counts)

- Lead: "159 phases registered, 157 dispatched by default." Explain WHY both
  numbers appear (constructor universe vs default order list).
- `sub_7FB6C0`: normal vs debug (knob 298) split.
- PhaseManager build (`sub_C62720`): 159*8 name table, build loop to 159,
  factory `sub_C60D30` 159-arm jump table.
- Order accessor `sub_C60D20`: returns (`0x22BEEA0`, 157). Order table is
  identity[0..156] + 2 zero sentinels.
- Dispatch loop `sub_C64F70`: count-bounded; per phase calls GetPhaseId
  (+8), Execute (+0, ALWAYS), isNoOp (+16, gates banner only); emits
  Before/After/Summary when PM+72 set.
- The two extra phases (157 DebuggerBreak, 158 NOP) and the debug path
  `sub_9F63D0`.
- Per-phase opt-level self-gating (link to the O-level page); correct the
  scheduler note: forward-pass at opt-level <= 2, reverse/latency at > 2
  (the unique `> 2` site is `sub_9FC860`).

## Outline: NEW or expanded `src/passes/index.md`

- Table of all 159 phase IDs/names from `phase_names.json` (mark 0..156 as
  default, 157..158 as debug-only).
- Group boundaries by name prefix (Ori*/General*/Advanced*/Merc*/Dump*).
- Per-phase vtable address range `off_22BD5C8 .. off_22BEE78`.

## Corrections to `src/passes/phase-manager.md`

- Change "owns the entire 159-phase optimization and code generation
  pipeline" -> "registers 159 phase objects; the default driver dispatches
  157 of them in order." Keep the Strategy + Abstract Factory description.
- "Total phases | 159 (139 explicitly named + 20 arch-specific)" -> "159
  registered (all named in `off_22BD0C0`); 157 dispatched by default; 2
  (DebuggerBreak, NOP) are debug-only extra-dispatch phases."
- "Default phase table at 0x22BEEA0 (returned by sub_C60D20)" -> add: returns
  pointer in rax AND count 157 in rdx; table is identity[0..156] + 2 zeros.
- "phase_list_count // always 159 after construction" is correct for the
  PhaseManager's registered list (PM+0x68/+0x6c hold 159); but add that the
  *dispatch* count is the separate 157 from the order accessor.

## Outline: NEW `src/pipeline/opt-levels.md` (or a section in optimizer.md)

- External `-O`/`--opt-level`: default 3, range 0..3.
- Internal remap in `sub_7F7DC0`: O0/O1 -> 1, O2 -> 2, O3 -> 4 (stored at
  **ctx+0x838**, global tier). Three effective global tiers {1,2,4}.
- TWO opt-level fields (do not conflate): global tier `ctx+0x838` {1,2,4} vs
  per-function/per-region nvopt level (field **+0x158** on the nvopt-region
  descriptor), bounds-checked **0..5** in `sub_C173E0`
  (`cmp $0x5; ja -> "Invalid nvopt level : %d."`).
- Accessor `sub_7DDB50` (164 callers); dominant gate `level > 1`; unique
  `> 2` (O3-only) site `sub_9FC860`; `<= 2`/`<= 3` "not-O3" sites. The
  knob-499 ("disable optimization") override forces level 1 past a budget
  counter (entry+12 < entry+8 at `499*72=0x8c58`).
- Distinct secondary knobs: `allow-expensive-optimizations` (default on at
  internal >= 2), stack-bounds (auto-on at -O0/-g), fast-compile,
  `regAllocOptLevel`, `perf-per-watt-opt-level`.

## Outline: NEW `src/pipeline/recipe-override.md` (or a section in optimizer.md)

- The OCG-knob mechanism: 72-byte entries at `config+0x48`, config offset =
  `id*72`; leaf accessors `sub_6614A0` (bool, `e[0]!=0`) and `sub_6614C0`
  (value, `e[0]==5 -> e[8]`). Table: knobs 249/297/298/391/499.
- The recipe path: knob 298 set -> `sub_7FB6C0` -> `sub_9F63D0` -> `sub_9F4040`
  builds a custom schedule from the recipe string (tokenized by `sub_798B60`),
  fed to the same `sub_C64F70`.
- The DSL: `NamedPhases`, `p%d`, `shuffle`/`reps`, `swap1..6`, `dce1..3`
  (inject `OriPerformLiveDead`), `cpy1..3` (inject `OriCopyProp`); IDs clamped
  to [0,159]; unknown names -> 158/NOP. THIS is the only path that schedules
  IDs 157/158.
- NvOptRecipe (knob 391) is the user-facing counterpart: when present, the
  PhaseManager ctor wires a recipe-apply object consumed by phase ID 1
  `ApplyNvOptRecipes`.
