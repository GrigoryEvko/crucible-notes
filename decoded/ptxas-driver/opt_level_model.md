# ptxas optimization-level (-O) model (binary-derived, CUDA 13.0.88)

All addresses are VMA == file offset for `.text`/`.rodata`. The OCG context
(the ~2140-byte per-function struct built by `sub_7F7DC0`) holds the resolved
optimization level at **offset 0x838 (2104)** as a signed int32.

## 1. The external option

| Property | Value | Evidence |
|---|---|---|
| Long name | `--opt-level` | registration in `sub_703AB0`: `("opt-level","O",4,1,288,…,"3","<N>","Specify optimization level")` |
| Short alias | `-O` | same registration (2nd arg "O") |
| Help text | "Specify optimization level" | rodata 0x1ce39d3 |
| Metavar | `<N>` | rodata 0x1ce383b |
| Default | **3** (i.e. `-O3`) | default-arg literal "3" at rodata 0x22a1d75 in the registration call |
| Accepted external values | **0, 1, 2, 3** | help/usage strings `-opt-level=<0,2,3>` (0x1ce6f44) and `<1,2,3>` (0x1ce6f1d); parser writes raw value to parsed-opts+0x70 |
| Parsed-options field | `opts+0x70` (112) | becomes `a2+112` consumed by `sub_7F7DC0` |

`-O0` additionally auto-enables the stack-pointer bounds-check sequence
(option help 0x1ce52b8: "...turned on automatically when device-debug (-g) or
opt-level(-O) 0 is specified").

## 2. External -> internal normalization (in `sub_7F7DC0`)

`sub_7F7DC0` (the OCG-context constructor) copies `opts+0x70` into ctx+2104
(line ~577), then remaps:

```
7f8b9f: 8b 43 70             mov 0x70(%rbx),%eax     ; raw -O
7f8ba2: 85 c0                test %eax,%eax
7f8ba4: 0f 85 .. jne 7f9e70  ; nonzero -> O>=1 path
7f8baa: c7 85 38 08 00 00 01 movl $0x1,0x838(%rbp)   ; raw 0 (O0) -> internal 1
...
7f9e70: 3d 03 00 00 00       cmp $0x3,%eax           ; raw 3 (O3) ?
7f9e73: 0f 85 .. jne 7f8bb4  ; not 3 -> keep copied raw value (1 or 2)
7f9e79: c7 85 38 08 00 00 04 movl $0x4,0x838(%rbp)   ; raw 3 (O3) -> internal 4
```

| External `-O` | Internal level (ctx+0x838) |
|---|---|
| 0 | 1 |
| 1 | 1 |
| 2 | 2 |
| 3 | 4 |

So only **three effective global tiers** exist at runtime: **{1, 2, 4}**
(O0/O1 collapse to 1). This remap is byte-verified at `0x7f8baa` and
`0x7f9e79`. The internal-level *domain* is 0..5 (see below), but the global
`-O` tier only ever produces {1,2,4}.

### Global tier vs per-function nvopt level (two distinct fields)

There are **two** opt-level fields, and they must not be conflated:

- **Global tier** `ctx+0x838` (int32, values {1,2,4}). Set once by the OCG ctor
  `sub_7F7DC0` from the `-O` flag (remap above). This is the master gate read
  by the 164 accessor sites via `sub_7DDB50`.
- **Per-function / per-region nvopt level**, field `+0x158` (=344) on a
  per-function "nvopt region" descriptor (object `v90`/`r15` in `sub_C173E0`).
  This is the field that can carry a per-region override (e.g. from
  `ApplyNvOptRecipes`, phase ID 1) and is **bounds-checked 0..5**:
  ```
  c18147: mov  0x158(%r15),%ecx   ; per-region nvopt level
  c1814e: cmp  $0x5,%ecx
  c18151: ja   c1a3b0             ; > 5 (unsigned) -> "Invalid nvopt level : %d."
  ```
  The error string `0x22b4967` ("Invalid nvopt level : %d.") is then emitted
  from `0xc1a3b9` / `0xc1a3ea`, printing `*(int*)(v90+344)`. So the internal
  level domain is **0..5** at the per-function granularity; the global tier
  only ever takes {1,2,4}.

## 3. The accessor and how phases read the level

- `sub_7DDB50` (0x7ddb50) returns ctx+2104, with a knob-499 ("disable
  optimization") override branch. **164 functions** call this accessor; it is
  the canonical per-phase opt-level gate. A few phases read ctx+2104 raw
  (e.g. `sub_7DB610`, `sub_7E7FC0`).

  Exact body (decompiled): `v1 = *(ctx+1664)` (the options/knob object).
  It reads vtable slot `+152` (a higher-level bool accessor `sub_67EB60`) and
  slot `+72` (the leaf bool accessor `sub_6614A0`). The knob-499 lookup uses
  the OCG-knob arithmetic `config+0x48 + 499*72` (= `+35928`/`0x8c58`):
  ```c
  if (slot+72 == sub_6614A0)  v4 = *(BYTE*)(v1[9] + 35928) != 0;   // knob 499 set?
  else                        v4 = (slot+72)(*(ctx+1664), 499);
  if (!v4) return *(uint*)(ctx+2104);          // knob 499 clear -> return the real level
  // knob 499 set: a budget counter at +35936 (limit) / +35940 (current)
  if (limit > cur) { ++cur; return *(uint*)(ctx+2104); }  // still within budget -> real level
  return 1;                                      // budget exhausted -> force minimal level 1
  ```
  i.e. **knob 499 ("disable optimization") forces the returned level to 1**
  once a per-compile budget counter is exhausted, otherwise leaves the real
  level intact. (`499*72=0x8c58`, `+8`=limit, `+12`=current — all verified.)
- The dominant test is `(int)level > 1` ("optimize beyond the minimal O0/O1
  tier", true for O2=2 and O3=4). Across the 164 callers: `> 1` ~30 sites,
  `== 1` / `!= 1` ~12 sites, `<= 2` 2 sites, `<= 3` 2 sites, `> 2` exactly 1.

## 4. Opt-level gating sites

(`level` = value returned by `sub_7DDB50`, i.e. internal {1,2,4}.)

| Function | Test | Effect | Conf. |
|---|---|---|---|
| `sub_9FC860` | `a4==6 && level > 2` | **O3-only** path (the unique `>2` site; isolates internal 4) | high (test) |
| `sub_78DB70` | `level <= 2` | runs at O0/O1/O2, **off at O3** (SinkCodeIntoBlock-related) | high (test) |
| `sub_8FFDE0` | `level <= 2` | O0/O1/O2 path, off at O3 | high (test) |
| `sub_914B40`, `sub_98A320` | `level <= 3` | boolean "not O3" (internal 4 excluded) | high |
| `sub_735290`, `sub_793220`, `sub_869960`, `sub_8EF500`, `sub_947150`, `sub_96D940`, `sub_98F430`, `sub_9B7A80`, `sub_C5FB60/90/C0/D0` | `level > 1` | enable optimization (skipped at O0/O1) | high (test) |
| `sub_781F80` | `level > 1 && ctx+1552 <= 12` | opt on AND applied-recipe count <= 12 | high (test) |
| `sub_8EDF90`, `sub_9BE270` | `level > 1 && !ctx+1880` | opt on AND no active scratch object | high (test) |
| `sub_991790` | `level > 1 && (attr&1)==0` | opt on AND attribute bit clear | high (test) |
| `sub_9F66A0` | `level > 1 && (ctx+1415 & 0x40)==0` | opt on AND feature flag clear | high (test) |
| `sub_A9DDD0` | `size<=20479 && (ctx+1368&1) && level>1` | opt on, code-size + flag guarded | high (test) |
| `sub_7753F0`, `sub_94F150`, `sub_7AF960` | `level > 1 && <extra>` | opt on with secondary guard | high (test) |
| `sub_692200`, `sub_AB9770`, `sub_ADEB40`, `sub_A0F020`, `sub_A112C0` | `level != 1` | "any optimization on" (excludes only O0/O1) | high (test) |
| `sub_726E00`, `sub_773320`, `sub_A94270`, `sub_AA3150`, `sub_AED3C0`, `sub_C173E0` | `level == 1` | **O0/O1-only** (debug/minimal) behavior | high (test) |
| `sub_926A30` | `ctx+1552>12 \|\| level==1 \|\| a6` | bail when minimal-opt OR recipe budget exceeded | high (test) |
| `sub_7DB610` | `*(int*)(ctx+2104) <= 1` (raw) | O0/O1 -> skip a knob-499/686 block | high |
| `sub_7E7FC0` | `*(int*)(ctx+2104) == 1` (raw) | when ctx+1424==199, set a bool at O0/O1 | high |

Behavioral labels for individual passes are inferred from nearby names/strings
(confidence medium); the threshold tests themselves are read directly from the
decompiled comparisons (confidence high).

## 5. Secondary opt knobs (DISTINCT fields, only loosely coupled to -O)

| Knob | String / addr | Default coupling to -O | Stored where |
|---|---|---|---|
| `allow-expensive-optimizations` | 0x1ce3d7c | **default on for internal level >= 2** (external O2/O3); overridable | own boolean field |
| stack-pointer bounds-check | 0x1ce52b8 | **auto-on at `-O0` (raw 0) or `-g`** | own flag |
| fast-compile (`-Ofast-compile=<min,mid,max>` / `--fast-compile`) | 0x1ce35b8 / 0x1ce6efa | **orthogonal**; default disabled; levels max/mid/min/0 | separate field |
| `regAllocOptLevel` | 0x1cfdb69 | register-allocator-specific level; **independent** | separate field |
| `perf-per-watt-opt-level` | 0x201f6d1 (used in sub_609F60) | perf/watt tuning; **independent** | separate field |

Key point: the global `-O` (ctx+0x838) is the master gate read by the 164
accessor sites, but `allow-expensive-optimizations`, fast-compile,
`regAllocOptLevel`, and `perf-per-watt-opt-level` are separate options that are
NOT stored in ctx+0x838 and are gated by their own fields (some merely default
their initial value from the -O tier).
