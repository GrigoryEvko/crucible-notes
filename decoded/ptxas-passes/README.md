# ptxas v13.0.88 Optimization Phase Pipeline (binary-derived)

All facts recovered from the ptxas binary (CUDA 13.0.88), independently verified
via objdump/readelf/struct-unpack. On any mismatch with prior docs, the binary wins.

## phase_pipeline.tsv

Columns:
- `exec_order` — position in the DEFAULT execution sequence (0..156). `-` = not in
  default pipeline (reachable only via the recipe-string override, option 298).
- `bin_index` — index into the phase name table at `0x22BD0C0` and the factory
  switch / vtable array (0..158). This is the authoritative phase identity.
- `phase_name` — exact string from the name table (no decoding needed; plaintext).
- `name_string_va` — VA of the name string in `.rodata`.
- `category` — analytical classification (NOT in binary): Validation / Lowering /
  Optimization / Analysis / Reporting / Scheduling / RegAlloc / Encoding / Cleanup / Gate.
- `opt_gate` — layer-1 `execute()`-wrapper opt-level gate. Empty = always runs
  (Category A). `>1` = O2+. `>2`,`>3`,`>0`,`==0` as shown. Fine-grained level
  branching lives inside the implementation bodies, not the wrapper.
- `role` — one-line description with key impl function addresses.

## Key binary facts (independently confirmed)

| Fact | Value | Evidence |
|---|---|---|
| Phase name table | `0x22BD0C0`, 159 string pointers | first ptr -> `OriCheckInitialProgram` @0x22BC429 |
| Default-order table | `0x22BEEA0`, **identity [0..156]** | struct-unpack: vals[i]==i for 0..156; vals[157]=vals[158]=0 (padding) |
| Default phase count | **157** (0x9D) | `sub_C60D20`: `mov $0x22beea0,%eax; mov $0x9d,%edx; ret` |
| Phase factory | `sub_C60D30`, 159-case switch | `cmp $0x9e,%edx; ja; jmp *0x22BBEB8(,%rdx,8)` (jump table @0x22BBEB8) |
| Dispatch loop | `sub_C64F70` | snapshot -> getName(vt+8) -> isNoOp(vt+16) -> execute(vt+0) -> isNoOp(vt+16) -> timing(c64310 if +0x48) |
| Recipe driver | `sub_7FB6C0` -> `sub_9F63D0` | option 298 (config +0x53D0) selects recipe path vs default |
| Timing gate | option 17928 (config +0x4608) | `cmpb $0x0,0x4608(%rax)` in sub_C62720 |
| NvOptRecipe gate | option 391 (`mov $0x187,%esi`) | sub_C62720; nvopt-level<=5 check at config +0x6DF8 |

## Correction vs prior wiki

The wiki `passes/index.md` "Default Order / DUMPIR#" column presents the default
order as a non-trivial PERMUTATION of the 159 indices (e.g. bin 8 -> order 8 but
displaced, bin 9 -> order 10, etc.). The binary REFUTES this: `0x22BEEA0` is a
plain identity array `[0,1,2,...,156]`. The default pipeline executes name-table
indices 0..156 in their natural order. Indices 157 (`DebuggerBreak`) and 158
(`NOP`) are constructed by the factory but NOT in the default order — they run
only when a recipe string (option 298) places them, and 158 doubles as the
phase-lookup-failure sentinel and the recipe `order[]` fill value.

Confidence: HIGH for all rows' identity/name/VA; MEDIUM-HIGH for category and
opt_gate (category is analytical; gate values harvested from wrapper analysis).
