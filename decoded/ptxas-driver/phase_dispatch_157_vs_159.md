# ptxas phase dispatch: 157 vs 159 — definitive resolution (CUDA 13.0.88)

**Authority:** the `ptxas` binary. Every claim is pinned to a binary address and checked at
the byte level. `.text` and `.rodata` use `VMA == file_offset + 0x400000` (rodata delta
`-0x400000`: VMA `0x1ce2e00` -> file `0x18e2e00`).

## Verdict

The default ptxas pipeline **dispatches exactly 157 phases** (phase IDs `0..156`).
The PhaseManager **registers 159 phase names** (IDs `0..158`). The two trailing registered
phases — ID 157 `DebuggerBreak` and ID 158 `NOP` — are **not in the default schedule** and
are never run on a default compile.

- `159` = size of the phase-*name registry* and the inclusive ceiling for explicit phase
  selection.
- `157` = length of the default phase-*schedule* that the dispatch loop actually walks.

Both numbers are correct for different objects; they do not conflict. phase_names.json
(`"count": 159`) is the registry, not the run count.

## The three objects and their counts

| Object | Symbol / builder | What it is | Count |
|---|---|---|---|
| Phase-name registry (rodata) | `off_22BD0C0` | `const char*[]` of phase names | 159 ptrs (1272 bytes) |
| PhaseManager name array | `sub_C62720` builds it | runtime copy of the registry | 159 (`size field = 159`) |
| Default schedule | `unk_22BEEA0` | `int32[]` of phase IDs to run; identity `0..156` | **157** (`0x9D`) |

## Evidence chain

### 1. The count rides in `edx`, lost by Hex-Rays

`sub_C60D20` (0xC60D20) — the default-schedule accessor. The Hex-Rays C view shows only:

```c
void *sub_C60D20() { return &unk_22BEEA0; }
```

The raw disassembly returns a *pair* (pointer in `eax`, count in `edx`):

```
0xc60d20: b8 a0 ee 2b 02    mov     eax, offset unk_22BEEA0   ; pointer to schedule
0xc60d25: ba 9d 00 00 00    mov     edx, 9Dh                  ; count = 0x9D = 157
0xc60d2a: c3                retn
```

Hex-Rays dropped `edx` because the inferred prototype returns a single pointer. `0x9D = 157`.

### 2. The driver passes `(ptr, count)` straight into the loop, untouched

Driver `sub_7FB6C0` (0x7FB6C0), default branch:

```
0x7fb707: call    sub_C62720        ; build PhaseManager   (rdi=&PM, rsi=ctx)
0x7fb70c: call    sub_C60D20        ; -> eax=&unk_22BEEA0, edx=157
0x7fb711: mov     rdi, rsp          ; arg1 = PhaseManager
0x7fb714: mov     rsi, rax          ; arg2 = &unk_22BEEA0  (schedule pointer)
0x7fb717: call    sub_C64F70        ; arg3 = rdx, UNCHANGED -> 157
```

Between the two calls **only `rdi`/`rsi` are reloaded**; `rdx`/`edx` is never touched, so
`edx = 157` flows into the third integer arg of `sub_C64F70` (SysV AMD64: rdi, rsi, **rdx**,
…). In the C view this third arg appears as the uninitialized-looking `v6` in
`sub_C64F70(v34, v5, v6)` — that is the lost `edx`.

### 3. The dispatch loop iterates `count`, not a fixed `0..158`

`sub_C64F70` (0xC64F70) loop setup:

```
0xc64f80: mov     rbx, rdx              ; rbx = a3 = count = 157
0xc64f9b: lea     rdx, ds:0[rbx*4]      ; rdx = count*4 = 628 (int32 stride)
0xc64fa3: lea     rax, [r12+rdx]        ; rax = &schedule[count] = end pointer
0xc64fa7: cmp     r12, rax              ; empty-schedule guard
0xc64faa: jz      loc_C650C3
0xc64fb8: movsxd  rax, dword ptr [r12]  ; load int32 phase ID from schedule[i]
          ... look the ID up in the name array, run the phase, r12 += 4 ...
```

The bound is `&schedule[a3]` with `a3 = 157`; entries `[0..156]` are consumed and the loop
stops. The C view agrees: `v5 = &a2[a3]; while (v3 != v50 /* &a2[a3] */)`. **No `0..158`
constant exists in the loop** — the bound is purely the runtime count.

### 4. The schedule array is the identity `0..156`, then ends

`.rodata` dump at file offset `0x1EBEEA0` (VMA `0x22BEEA0`), `int32` elements:

```
entry 0   = 0x00   entry 1 = 0x01   …   entry 155 = 0x9B
entry 156 = 0x9C   (= 156; the 157th and last element)
entry 157 = 0x00000000   <- NOT part of the array; unrelated rodata follows
```

Exactly 157 elements, value == index. The bytes after entry 156 (floats, a pointer
`0x22BC3DB`) belong to a different structure, confirming length 157.

### 5. The registry (159 names) is built and sized by the ctor

`sub_C62720` (0xC62720), the PhaseManager constructor:

- sets the name-array size field to **159**: `*((_DWORD *)v2 + 27) = 159;`
  then `*((_DWORD *)v2 + 26) += 159;`
- `qmemcpy(..., off_22BD0C0, 8 * (1272 >> 3))` copies **1272 bytes = 159 `const char*`**
  from the rodata registry `off_22BD0C0`.
- materializes entry 0 = `"OriCheckInitialProgram"` and entry 158 = `byte_21E6C80`
  (= `"NOP"`) as immediate relocations (redundant-with-memcpy compiler artifact).
- pre-sizes per-phase bookkeeping with `sub_C62640(v3, 159)` and loops to `159`.

### 6. The two excluded phases, confirmed by string dumps

From `off_22BD0C0`:

```
entry 156 -> 0x22BCEF2  "DumpNVuCodeHex"   (last phase in the default schedule)
entry 157 -> 0x22BCF01  "DebuggerBreak"    (registered, NOT scheduled by default)
entry 158 -> 0x21E6C80  "NOP"              (registered, NOT scheduled by default)
```

Both strings verified by direct byte dump at their VAs; they match phase_names.json IDs
156/157/158.

## Why the alternate (named-phases) path does not change the answer

When **knob 298** is set the driver takes `sub_7FB6C0` → `sub_9F63D0` → schedule builder
`sub_9F4040` → the same `sub_C64F70`:

- `sub_9F63D0` zeroes a 256-slot int buffer, seeds `[0]=158`, calls `sub_9F4040` to build a
  custom schedule, and passes its **computed length** to `sub_C64F70`.
- `sub_9F4040` starts from `sub_C60D20()` (the same 157 default), parses option tokens
  (`"NamedPhases"`, `"p%d"`), maps names to IDs via `sub_C641D0` (returns `158`=NOP for
  unknown names), and **clamps explicit IDs to `[0, 159]`** (`if (v121 > 159) v121 = 159;`).

This is the only way IDs 157/158 can enter a schedule, and only under explicit selection.
It confirms `159` as the inclusive selection ceiling and leaves the **default** count at 157.

## Structural corroboration (binary mechanism)

The ORI backend phase manager registers its full phase list once and builds the default
schedule from a subset that **omits the two trailing sentinel phases** `{ DebuggerBreak, NOP }`,
so registered-minus-scheduled is exactly **2**. The dispatch loop iterates a list of IDs,
looks up name+function per ID, and emits `"Before "`/`"After "` tracepoints and a final
`"All Phases Summary"` (all three strings present in `.rodata`). On a name miss the lookup
returns the NOP id — the binary's `sub_C641D0` returns `158` for unknown names. `DebuggerBreak`
is the explicit-break entry point; `NOP`'s phase body is an immediate `return`.

Across earlier toolchains the absolute counts were smaller (≈143 run / ≈145 registered);
CUDA 13.0.88 has grown to 157 run / 159 registered (≈14 added optimization phases). The
**mechanism and the identity of the two excluded sentinels are unchanged**.

## One-line summary

> `sub_C60D20` returns `(&unk_22BEEA0, 157)`; `157` rides in `edx` (lost by Hex-Rays), flows
> untouched through the driver into `sub_C64F70`'s third arg, and bounds the loop at
> `&schedule[157]`. Registry = 159 names; default schedule = 157 IDs (`0..156`);
> `DebuggerBreak` (157) and `NOP` (158) are registered but not dispatched by default.

**Confidence: very high.** Every link is pinned to a binary address and byte-checked, and the
structural mechanism (registry vs. schedule, two excluded sentinel phases) is consistent across
toolchain versions.
