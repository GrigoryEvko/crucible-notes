# The ISS Semantic-Model Synthesis

This is the **capstone of Part 14** — the one page that assembles every value claim of the GPSIMD
Vision-Q7 instruction-set simulator into a single coherent **semantic model**: the *value + decode
oracle*, the thing that answers **"what does instruction X compute"** down to the last clamped bit.
It is deliberately *not* a timing page — the *when* (latencies, the 32-deep ring, the scoreboard) is
its sibling [cas Timing Model](./cas-timing-model.md). Here we unify the *what*.

The fifteen committed sibling pages are the **substrate**; this page does not restate them, it
**welds** them: the cas decode/timing surface ([core surface](./cas-core-surface.md),
[arith](./cas-arith-sem.md), [MAC/FMAC](./cas-mac-fmac.md), [load/store](./cas-load-store.md),
[SuperGather](./cas-supergather.md), [predicate/boolean](./cas-predicate-boolean.md),
[convert/pack/fp](./cas-convert-pack-fp.md), [valign/shuffle/reduce](./cas-valign-shuffle-reduce.md));
the fiss value oracle ([surface/exceptions](./fiss-surface-exceptions.md),
[slotfill F0–F3](./fiss-slotfill-f0-f3.md), [slotfill F4–F11](./fiss-slotfill-f4-f11.md),
[slotfill N0–N2](./fiss-slotfill-n0-n2.md), [datapath oracle](./fiss-datapath-oracle.md)); and the
TIE/ctype provenance layer ([libtie core/msem](./libtie-core-msem.md),
[libctype c-stub](./libctype-cstub.md)). The decode/encode contract is anchored in
[the Formal ISA Model](../isa/semantics/formal-isa-model.md); the final introspection/SystemC
capstone is [the Oracle Synthesis](./iss-oracle-synthesis.md).

> **Keystone — the semantic model in one sentence.** A GPSIMD instruction's *meaning* is the pair
> **(operand decode, element value)**: `libfiss-base.so` decodes the FLIX bundle into operand
> indices via **12 569 `slotfill__` leaves**, fetches register values, and computes each lane's
> result with one of **864 `module__xdref_*` value primitives** — a **self-contained,
> in-process, integer-only reference interpreter** (`nm -D … | rg ' nx_.*_interface'` = **0**).
> `libcas-core.so` decodes the *same* encoding for timing but computes **zero** element values; it
> delegates each lane's `a OP b` to the host through **119 `nx_*_interface` TIE-port callbacks**.
> Both decode the roster `libisa-core.so` encodes, so **(format, slot, regfile, value)** closes
> end-to-end. The fiss leaves are therefore the **executable golden semantic model** — every value
> claim on every sibling page is certifiable by driving the real leaf **live via `ctypes`**.
> `[HIGH/OBSERVED]`

Confidence tags follow [the Confidence & Walls model](../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / proven by disassembly or a live `ctypes` call,
`[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` = re-used at a sibling's confidence (the
synthesis spine below is **re-grounded against the shipped binaries in this pass**, not merely
carried). All four ISS plugins live under
`extracted/.../gpsimd_tools_tgz/tools/ncore2gp/config/`; every count is grounded by
`nm -D <abs-path> | rg -c`, never the 884 k-file decompile.

| binary | size (B) | role in the semantic model |
|---|---:|---|
| `libcas-core.so` | 45 878 080 | decode + timing + register-hazard; **0 element values**; delegates math via **119** `nx_*_interface` |
| `libfiss-base.so` | 12 330 016 | the **value oracle**: 12 569 `slotfill__` decode + 864 `module__xdref_*` value leaves; **0** host callbacks |
| `libtie-core.so` | 51 098 208 | the **source-of-truth** TIE DB the `xdref`/`xdsem` leaves were generated from (1607 mnemonic / 12642 OPCODEDEF) |
| `libctype.so` | 388 648 | the host **value-marshalling** layer (64 ctypes, 6 dispatchers, 299 `Function_TIE_*`) — orthogonal to the oracle |

> **GOTCHA — VMA vs file offset is not uniform.** For all four plugins `.text` and `.rodata` are
> **VMA == file offset** (so the disassembly addresses below are raw VMA), but the writable sections
> carry a **`0x200000` delta** (`libfiss-base` `.data.rel.ro` VMA `0xc17e80` → file `0xa17e80`;
> `libctype` `.data` VMA `0x256040` → file `0x56040`). No value-leaf body lives in a writable
> section, so this caveat only bites if you `xxd` a struct table. `[HIGH/OBSERVED]`

---

## 1. The spine — cas decodes, fiss values, isa rosters

The ISS is a **two-library co-simulator** sitting on a **shared encode/decode roster**. The split is
not a layering accident — it is the whole architecture, and the semantic model lives entirely on the
`fiss` side. Stated as the three sentences each library answers for **one** instruction:

- **`libcas-core` answers *when*.** It decodes the bundle, schedules each operand's read/write on a
  32-deep pipeline-stage ring with a latency-aware bypass scoreboard, and at the execute stage
  **hands the element math to the host** through a TIE-port callback. It computes **zero** element
  values — verified two ways: **119** `nx_*_interface` imports (the value ports) and **0**
  packed-arithmetic SIMD instructions in 45 MB. `[HIGH/OBSERVED]`
- **`libfiss-base` answers *what*.** It decodes the bundle (`slotfill__`), gathers operand register
  *values* (`regload__`), computes each lane's result with its own integer-only primitives
  (`module__xdref_*`), and commits (`writeback__`). It is **self-contained** — `nm -D … | rg ' U '`
  = **0** undefined symbols beyond five weak libc/runtime clones, **0** `nx_*_interface`. `[HIGH/OBSERVED]`
- **`libisa-core` is the shared roster both decode against** — formats, slots, regfiles, the
  `(mnemonic × slot)` encode-placement matrix (see [the Formal ISA Model](../isa/semantics/formal-isa-model.md)).
  Because both halves decode the *same* `libisa-core`, their op-class → `(format, slot, regfile)`
  bindings **must** agree, and §5 shows they do. `[HIGH/CARRIED]`

The **design logic**: `cas` and `fiss` are the cycle-accurate and functional halves of the *same*
ISS; the `ncore2gp` harness pairs them — `cas` for cycles, `fiss` (or the host) for values. This is
why a *value*-bearing distinction (signed vs unsigned, saturating vs wrapping, ordered vs unordered,
predicated vs plain) is a **distinct decode bit / distinct opcode**, never a runtime mode toggle:
the difference shows up only in *which `xdref` leaf fires*, and never in `cas` timing.

```
                     ┌──────────────────────── libisa-core.so ────────────────────────┐
                     │   the SHARED roster:  (mnemonic × slot) encode placements,      │
                     │   14 formats · 46 slots · 8 regfiles  (Formal ISA Model)        │
                     └───────────────┬───────────────────────────────┬────────────────┘
                       both DECODE   │                               │   both DECODE
                                     ▼                               ▼
        ┌──────────── libcas-core.so (45.9 MB) ───────────┐   ┌────── libfiss-base.so (12.3 MB) ──────┐
        │  decode → schedule on 32-deep ring → at EXECUTE │   │  slotfill__ → regload__ → opcode__    │
        │  HAND OFF the value to the host:                │   │     __stage_{5,14} → writeback__       │
        │     119 × nx_*_interface  ───────────────┐      │   │  per-lane:  module__xdref_<op>_<W>    │
        │  computes 0 element values  (0 SIMD)     │      │   │     (864 leaves, integer-only)        │
        └──────────────────────────────────────────┼──────┘   └───────────────┬───────────────────────┘
                                                    │                          │
                         host system model ◄────────┘     in-process value ◄───┘
                         (the value, host-side)            (the value, fiss-side)
                                                    │                          │
                                                    └────── SAME value math ────┘
                                                       (cas=host, fiss=in-process;
                                                        the SEMANTICS are identical)
```

The per-op handoff is the universal pattern: cas's stage-0 wrapper lights **one**
`(format, slot, mnemonic)` decode bit; a shared per-slot executor tests it and at the execute stage
fires the host value port; the matching fiss value is `opcode__<m>__stage_{5|14}` driving the
matching `module__xdref_*`. Every op class follows it — established for arith on
[cas-arith-sem](./cas-arith-sem.md), confirmed per-class on MAC/FMAC, load/store, gather, predicate,
convert, valign through the eight cas siblings.

> **NOTE — the two `_stage` depths are *fiss internal*, not the cas pipeline.** fiss retires the
> **vector** path at `opcode__…__stage_5` and the **scalar base-ISA** path at `…__stage_14`. These
> are the fiss interpreter's own phase tags, distinct from the cas 32-deep ring whose execute fires
> at stage 10 (`esi=$0xa`). Do not conflate the two depth axes. `[HIGH/OBSERVED]`

---

## 2. The keystone census — re-grounded, every number byte-exact

These are the five numbers the entire semantic model rests on. Each was re-run against the shipped
binary in *this* pass (not carried from a report), and each reconciles to the unit.

| claim | command (abridged) | result | tag |
|---|---|---:|---|
| cas value-delegation callbacks | `nm -D libcas-core.so \| rg -c ' U nx_.*_interface'` | **119** | `[HIGH/OBSERVED]` |
| cas total UND imports | `nm -D libcas-core.so \| rg -c '^\s\+U '` | **120** (= 119 `nx_` + `memset`) | `[HIGH/OBSERVED]` |
| fiss host callbacks | `nm -D libfiss-base.so \| rg -c 'nx_.*_interface'` | **0** | `[HIGH/OBSERVED]` |
| fiss value leaves | `nm -D --defined-only … \| rg -c ' module__xdref_'` | **864** | `[HIGH/OBSERVED]` |
| fiss operand-decoders | `nm -D --defined-only … \| rg -c ' slotfill__'` | **12 569** | `[HIGH/OBSERVED]` |
| fiss dynsym exports | `nm -D --defined-only … \| wc -l` | **20 379** (20 384 incl. 5 weak clones) | `[HIGH/OBSERVED]` |
| cas per-instance state | `dll_get_data_size` body = `mov $0x4a09f0,%eax` | **0x4a09f0 = 4 852 208 B** | `[HIGH/OBSERVED]` |

### 2.1 The slotfill census reconciliation — 12 569, to the unit

The **12 569** operand-decoders partition cleanly across the **11 issue formats** plus the three
base-ISA pseudo-formats, and the three slices sum to the whole with no remainder:

| slice | formats | `slotfill__` count | sibling |
|---|---|---:|---|
| wide FLIX | F0, F1, F2, F3 | **6 288** | [slotfill F0–F3](./fiss-slotfill-f0-f3.md) |
| irregular FLIX | F4, F6, F7, F11 | **4 158** | [slotfill F4–F11](./fiss-slotfill-f4-f11.md) |
| narrow FLIX | N0, N1, N2 | **1 790** | [slotfill N0–N2](./fiss-slotfill-n0-n2.md) |
| base-ISA scalar | x24 (319) + x16a (4) + x16b (10) | **333** | — |
| | | **= 12 569** ✓ | |

```
6 288 (F0–F3) + 4 158 (F4/F6/F7/F11) + 1 790 (N0/N1/N2) + 333 (x24+x16a+x16b) = 12 569
```

> **CORRECTION — "12 569 = 6 288 + 4 158 + 1 790" omits the scalar tail.** The three FLIX slices sum
> to **12 236**, not 12 569; the missing **333** are the base-ISA codecs `slotfill__x24__` (319),
> `slotfill__x16a__` (4) and `slotfill__x16b__` (10) — verified `nm -D --defined-only … | rg -c
> ' slotfill__x24__'` = 319, etc. The capstone census is **6 288 + 4 158 + 1 790 + 333 = 12 569**.
> `[HIGH/OBSERVED]`

A `slotfill__` leaf is a **pure bitfield decoder**: given a pointer to a per-op context whose first
16 bytes (8 for narrow formats) hold the raw FLIX bundle as little-endian words, it `shr`/`and`/`or`
the slot's operand sub-fields into fixed index latches (`0x24`/`0x48`/`0x50` scalar;
`0x94`/`0x50`/`0xd8` ALU-canonical and `0x28`/`0x6c`/`0xb0` Mul/alt vector) and returns. It performs
**no** arithmetic on values — it computes operand **indices**, sign-extended **immediates**, and
sub-opcode **selectors**. The cross-validation is complete for all 11 issue formats: each cell's
recovered bit-positions match the `libisa-core` `(slot get_fn ∘ field_get)` composition, with the
field `_set` proven the exact inverse of `_get` — the **encode = decode⁻¹** closure (§5). `[HIGH/CARRIED]`

### 2.2 The five-phase value pipeline these leaves feed

`slotfill__` is the decode root; the value is computed by a five-phase quintuple keyed by the *same*
decoded indices, parallel across all 1 534 executable mnemonic-variants:

```
slotfill__   (12 569)  bundle  → operand INDEX latches              (pure DECODE, no math)
stateload__  ( 1 534)  arch/window state → context
regload__    ( 1 534)  index → 512-bit register VALUE blocks        (×16 gather per vreg)
opcode__     ( 1 925)  __stage_{5=vector,14=scalar}: per-lane loop  ─┐ the EXECUTE
                                                                      ├─► module__xdref_<op>_<W>   ← THE VALUE
writeback__  ( 1 534)  result lane → register file                  ─┘  (864 leaves)
```

The vector execute is the defining shape: `opcode__ivp_addnx16t__stage_5` calls
`module__xdref_add_16_16_16` **32 times** — once per NX16 lane of a 512-bit vector — plus
`module__xdref_bitkillt_16_2` per lane for the `_t` predicate guard. *Correctness over throughput*:
a reference interpreter, never packed SIMD. `[HIGH/OBSERVED]`

---

## 3. The value grammar — what a leaf name *means*

Every value leaf is named by a width-typed grammar that **is** its type signature. Reading the name
tells you the operation, the operand and result widths, and the wrap/saturate/signedness/round
behaviour; reading the (tiny) body confirms it bit-for-bit.

```
module__xdref_<op>[<post>]_<outW>_<inAW>[_<inBW>][_<suffix>]
   <op>     operation root          add sub adds neg abs abssub min max mul mula muls …
   widths   element bit-width        8 16 24 32 48 64 96      512 = full-vector bool/logical
   <…f>     IEEE float-element token  16f (binary16) · 32f (binary32) · 64f      → the FLOAT axis
   suffix   t = PREDICATED (_t merge) · u = UNSIGNED · s = SATURATING/signed-narrow
            c = COMPLEX (re/im in a 32-bit slot) · j = CONJUGATE · r = ROUNDING · packl/packm = pack
```

### 3.1 The float / integer split — 180 / 684, re-derived

Of the 864 leaves, the float axis is exactly those carrying a `_<digits>f` width token (`16f`,
`32f`, `64f`, and the two `divn_…_2f` float-divn variants). Re-derived from the binary in this pass:

```
nm -D --defined-only libfiss-base.so | rg -o 'module__xdref_\S+' | rg -c '[0-9]+f(_|$)'   →  180   (float)
                                                                       864 − 180          =  684   (integer / structural)
```

So **180 float-token leaves + 684 integer-structural leaves = 864**. The integer side is tiny scalar
bodies (5–50 B); the float side is large **integer-only soft-float** (fp16 ≈ 0x830 B, fp32 ≈ 0x890 B,
**zero** x87/SSE FP) implementing IEEE binary16/binary32 in GPR logic. `[HIGH/OBSERVED]`

> **CORRECTION — the float census is 180/684, not 184/680.** The fiss datapath report stated
> "184 float / 680 integer," counting only the `16f` token as the float marker and labelling the
> `32f`/`64f`/`2f` leaves "integer." The reproducible word-boundary census `rg '[0-9]+f(_|$)'` is
> **180 float / 684 integer**, which reconciles to 864; the distinct float-width tokens present are
> `16f`, `32f`, `64f`, `512f`, plus `2f`/`4f` modifiers — but the *count of names* carrying any
> float token is 180. This page uses **180 / 684**. `[HIGH/OBSERVED]`

### 3.2 Wrapping vs saturating — the two integer-arithmetic regimes

The arithmetic regime is a property of the **root**, decoded as a distinct opcode, and visible in the
leaf body as a literal:

- **Wrapping** (`add`/`sub`/`neg`/`abs`): `a OP b` then `and $((1<<w)-1)` — modular two's-complement.
  `add_16_16_16` is literally `add %esi,%edx ; and $0xffff,%edx ; mov %edx,(%rcx)` (32-bit `add` needs
  no mask). `abs`/`neg` therefore **wrap** on `INT_MIN` (`abs(0x8000)=0x8000`). `[HIGH/OBSERVED]`
- **Saturating** (`adds`/`subs`/`negs`/`abss`/`absssub`): sign-extend to a 17-bit intermediate,
  detect overflow as **bit 15 ≠ bit 16**, and clamp to **exactly `0x7fff` (+overflow) / `0x8000`
  (−overflow)**. The signed `|a−b|` (`abssub`) vs unsigned (`abssubu`) split is *only* the operand
  sign-extension (`movswl` vs raw). `[HIGH/OBSERVED]`

Floats route through the **FP32 hub**: only `fp16 ↔ fp32` convert is native; bf16/fp8 are not native
ops — they ride the FP32 hub (cast) or unpack+scale (MX dequant). Rounding is FSR-driven (UR-id
`0xe9`, `wur.fsr = 0x00f3e900`), default RNE; `trunc`/`utrunc` ignore the mode (round-toward-zero).
The soft-float path classifies NaN/Inf/denormal per IEEE and emits a **canonical qNaN**
(`0x7e00` fp16-class / `0x7fc00000` fp32) — see [convert/pack/fp](./cas-convert-pack-fp.md). `[HIGH/CARRIED]`

### 3.3 The predicate axis — `_t` is destination-MERGE, not zero-fill

A `_t` suffix is the per-lane `vbool` predicate. The merge primitive is `bitkillt`:
`bitkillt_16_2` = `(pred ? 0 : 0xFFFF)`, a select mask. Killed lanes **retain the destination
value** (merge), they are *not* zero-filled — the central semantic distinction for masked
vector ops. The compare → `vbool` generators implement full IEEE ordered/unordered classification
(ordered ⇒ FALSE on NaN, unordered ⇒ TRUE on NaN); see
[predicate/boolean](./cas-predicate-boolean.md). `[HIGH/CARRIED]`

---

## 4. The TIE provenance — where the oracle was *generated from*

The fiss value leaves are not hand-written; they were **generated** from a single source-of-truth:
the TIE descriptor DB carried inside `libtie-core.so`. This closes the question "is the oracle
faithful to the ISA?" — *the oracle is a compiled projection of the same DB the ISA roster is*.

- **`libtie-core.so`** (51 MB) is a **data container**, not a simulator: `.text` is 0x48 bytes (five
  `lea blob ; ret` getters + `interface_version`), and `.data` carries four `+13 (mod 256)`-ciphered
  TIE-XML blobs (the four TIE-compiler phases `post_parse` / `post_rewrite` / `compiler` / `xinfo`).
  The big `post_rewrite` blob is the folded TIE DB; its decoded census is **1607 distinct mnemonics
  / 12 642 OPCODEDEF placements** (the pre-fold authoring superset; the runtime opcode set folds to
  1534) and **19 476 `xdref_*` tokens**. `[HIGH/CARRIED]`
- **The link is name-exact.** The `xdref_*` symbols inside `libfiss-base.so` (e.g.
  `xdref_widestshift_512_64_6`, `xdref_zeroext_48_32`) appear **verbatim** inside the decoded
  `libtie-core` DB, and `libcas-core` carries the matching `xdsem` references. So the DB's
  `<REFERENCE>`/`<MODULE>`/`<SEMANTIC>` blocks are the source from which the fiss `xdref` and cas
  `xdsem` functions were emitted — see [libtie core/msem](./libtie-core-msem.md). The `msem`
  overlay (`libtie-Xtensa-msem.so`) carries the complementary **memory/control** semantics (645
  INTERFACE signals + 118 load/store `xtms_*` functions). `[HIGH/CARRIED]`
- **`libctype.so`** (388 648 B) is the host **value-marshalling** layer beneath the model: **64
  ctypes** realised as **6 dispatchers** (`cstub_set_base_address`, `cstub_{loadi,storei,rtor}_function`,
  `cstub_ctype_size_function`, `cstub_ctypes_init`) over **299 `Function_TIE_*`** custom-type stubs
  (**205 rtor / 47 loadi / 47 storei**, re-verified `nm --defined-only … | rg -c ' Function_TIE_'`
  = 299). It is **orthogonal** to the value oracle — `cas`/`fiss`/`isa` do **not** call it; only the
  native TIE simulator and `xt-gdb` do; see [libctype c-stub](./libctype-cstub.md). `[HIGH/CARRIED]`

> **NOTE — the four ciphered blobs reuse one scheme.** Each blob is an ASCII-hex checksum prefix +
> `|` + body, body = plaintext `+ 13 (mod 256)` with the `s..z` overflow remap (the same cipher the
> standalone `Xtensa.xml` uses). The `post_rewrite` ciphered body is byte-identical to the standalone
> DB; only the checksum prefix differs. This is the *provenance proof*, not a fresh crack. `[HIGH/CARRIED]`

---

## 5. The closure — (format, slot, regfile, value) is consistent end-to-end

The model is **closed** across all three libraries — the property that makes it a *model* and not a
heap of facts. Four edges, each independently verified:

1. **encode = decode⁻¹.** `libisa-core`'s 12 569 encode templates are the inverse of each slot's
   decode classifier; the fiss `slotfill__` operand codecs are inverse-proven for **all 11** issue
   formats (`field _set == _get⁻¹` over the full field range, no MISMATCH). `[HIGH/CARRIED]`
2. **cas and fiss decode the same grid.** Both key on the same `(format, slot, mnemonic)` and use
   the same `opnd_sem_<rf>_addr` resolvers on the same 46-slot / 8-regfile grid. The 8 register
   files agree byte-exact three ways (cas `dll_regfile_table` = `libisa` `regfiles[]` = fiss usage):
   `AR 64×32`, `BR 16×1`, `vec 32×512`, `vbool 16×64`, `valign 4×512`, `wvec 4×1536`, `b32_pr 16×64`,
   `gvr 8×512`. `[HIGH/CARRIED]`
3. **the values are byte-anchored.** The fiss `xdref` leaves confirmed the cas naming-grammar
   semantics bit-for-bit — saturation bounds, signed/unsigned, wrap/round all *executed*, several
   upgraded MED → HIGH, zero refuted. `[HIGH/CARRIED]`
4. **every op-class maps to a consistent (slot, regfile).** arith → `S3_ALU`(`/S4`); int MAC →
   `S2_Mul` (wide `wvec` accumulator); FP-FMA → `S3_ALU`; load/store → `S0_LdSt`/`S1_Ld`; gather post
   → `S0_LdSt`, drain → `S1_Ld`; bool → `S1_Ld`; compare/convert/shuffle/select/reduce → `S3_ALU`;
   valign → `S0_LdSt`/`S1_Ld`; acc-pack → `S1_Ld`. The binding is **identical** across all three
   libraries. `[HIGH/CARRIED]`

The only two structural gaps are reconciled: `gvr` is present in all three binaries but absent from
the shipped `tie.h` (the binary is authoritative — `gvr` is a real 8×512-bit file), and the slotfill
N0/N1/N2 cross-validation that closes the 11-format decode proof. Both resolve in favour of the
binary. The `(format, slot, regfile, value)` tuple is consistent **end-to-end**. `[HIGH/CARRIED]`

---

## 6. Methodology verdict — the ISS *is* the executable golden semantic model

This is the core methodological claim of Part 14, stated plainly: **you never have to
*infer* a GPSIMD value semantic — you can *execute* it.** Because `libfiss-base.so` is a
self-contained, in-process reference interpreter (0 host callbacks, 0 hardware FP), each
`module__xdref_*` leaf is a free-standing C-callable function; `dlopen`+`ctypes` drives it directly
and the answer it returns **is** the certified golden value. The fifteen siblings' worked examples —
`mula_48_48_16_16` (the 8/8 → widening MAC body), `adds_16_16_16` (the 0x7fff/0x8000 saturate),
`cvtf32_1_32f_16f` (the soft-float canonical-qNaN widen) — are all *executable*, not narrated.

### 6.1 A fresh confirmatory leaf — driven live to certify this page

To certify the oracle independently of the siblings, this page drives a **fresh** leaf —
`module__xdref_avgr_16_16_16` (rounding average, never driven live in any sibling) — plus the
wrap/saturate pair as a control. The leaf body (read first, then executed):

```asm
module__xdref_avgr_16_16_16  @0x81d1e0:
  movswl %si,%esi          ; sign-extend A   (esi = arg-A)
  movswl %dx,%edx          ; sign-extend B   (edx = arg-B)
  add    %esi,%edx         ; 17-bit signed sum (no overflow)
  mov    %edx,%eax ; and $0x1ffff,%eax ; shr $1,%eax   ; floor((a+b)/2)
  and    $0x1,%edx ; je +  ; add $0x1,%eax ; movzwl %ax,%eax  ; +1 if sum odd (round-half-up)
  mov    %eax,(%rcx)       ; result *= rcx
  ret
```

The ABI matters and is the one trap (§6.2): A → `%esi`, B → `%edx`, result-ptr → `%rcx` — these are
SysV integer args **2, 3, 4**, so the C-callable signature is **four** args with **arg 0 (`%rdi`)
ignored**. Driven via `ctypes`:

```python
import ctypes
lib = ctypes.CDLL(".../ncore2gp/config/libfiss-base.so")
f = lib.module__xdref_avgr_16_16_16
f.restype, f.argtypes = None, [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32,
                               ctypes.POINTER(ctypes.c_uint32)]
out = ctypes.c_uint32()
f(None, 7, 8, ctypes.byref(out))     # arg0 ignored; A=7, B=8, result ptr
# out.value & 0xffff == 8            # floor((7+8+1)/2) = 8  ✓
```

Live results, all **byte-exact** against the read-from-byte expectation (`floor((a+b+1)/2)`):

| `avgr(a, b)` | got | expect | | `adds`/`add`/`maxu` | got | expect |
|---|---:|---:|---|---|---:|---:|
| `avgr(3, 4)` | 4 | 4 | | `adds(0x7000,0x2000)` sat+ | `0x7fff` | `0x7fff` |
| `avgr(4, 4)` | 4 | 4 | | `adds(-0x7000,-0x2000)` sat− | `0x8000` | `0x8000` |
| `avgr(5, 2)` | 4 | 4 | | `add(0x7000,0x2000)` **wrap** | `0x9000` | `0x9000` |
| `avgr(−4, −3)` | −3 | −3 | | `add(0xffff,0x0001)` **wrap** | `0x0000` | `0x0000` |
| `avgr(7, 8)` | 8 | 8 | | `maxu(0x8000,0x0001)` unsigned | `0x8000` | `0x8000` |

`avgr` certified: it is `floor((a+b+1)/2)` over a signed 17-bit intermediate (round-half-up), and the
`add`/`adds` pair certifies the wrap-vs-saturate split *live*. The oracle is executable. `[HIGH/OBSERVED]`

### 6.2 The one ABI trap when driving leaves via `ctypes`

> **GOTCHA — the value-leaf ABI is 4-arg, with arg 0 a dead `%rdi`.** The leaf bodies read A from
> `%esi`, B from `%edx`, and write through `%rcx` — SysV integer registers **2, 3, 4**. A `ctypes`
> caller that declares the documented "3-operand" signature `(int32 a, int32 b, uint32* res)` passes
> A in `%rdi`, B in `%rsi`, ptr in `%rdx` — the leaf then reads garbage from `%esi`/`%edx` and
> writes through whatever is in `%rcx`, so your result slot stays **unwritten** (it keeps its
> sentinel; you see `0xbeef`, not the value). The correct signature is **four** args
> `(c_void_p ignored, int32 a, int32 b, uint32* res)`. This is the single difference between a
> certified oracle and a silent no-op. `[HIGH/OBSERVED]`

---

## 7. The semantic-model quick-reference (the one table)

The whole model on one line per op-class — decode slot, regfiles touched, and the fiss value
primitive that *is* the semantic. (Latencies are deliberately omitted; see
[cas Timing Model](./cas-timing-model.md).)

| class | rep. mnemonics | slot | regfiles | fiss value primitive |
|---|---|---|---|---|
| arith | ADD/SUB/MIN/MAX/ABS | S3_ALU | vec, vbool, b32_pr | `add`/`min`/`max`/`abs` (wrap \| sat) |
| int MAC | MULA/MULPA/MUL4TA | S2_Mul | wvec(acc), vec, b32_pr | `mula`/`mulp`/`mul4t` (widen 8→24/16→48/32→96) |
| FP FMA | MULAN\_2XF32/MULANXF16 | S3_ALU | vec | `hp_fma`/`spfma` (soft-float, single round) |
| load/store | LSNX16/SSNX16 (`_i/x/ip/xp`) | S0_LdSt/S1_Ld | vec, AR, valign | `wideldshift`/`widestshift` (512-bit line) |
| gather/scatter | GATHERAN\*/GATHERD\*/SCATTER\* | S0/S1 | AR, gvr, vbool, vec | `bitkillf` + base + per-lane Voff |
| compare → vbool | o/u{lt,le,eq}, lt/le/eq | S3_ALU | vec → vbool | `olt`/`ole`/`oeq` (IEEE classify) |
| bool | ANDB/ORB/XORB/NOTB | S1_Ld | vbool, BR | `andb`/`orb`/`xorb_64` |
| convert | FLOAT/TRUNC/CVTF16F32 | S3_ALU | vec, AR (FSR) | `float`/`trunc`/`cvtf` (canonical qNaN) |
| acc-pack | PACKVR2NX24 | S1_Ld | wvec → vec | `packvr` (arith-shift + round + sat) |
| valign | L/S/M/ZALIGN | S0/S1 | valign, vec, AR | `*align` + `wideldshift` funnel |
| shuffle/select | SHFL / SEL/DSEL | S3_ALU | vec, vbool | `shfl`/`sel` (log₂N idx; `_t` merge) |
| reduce | RADD/RMAX/RMIN/RANDB | S3_ALU | vec, vbool | `r{add,max,min,andb}` (N−1 fold) |

---

## 8. Confidence ledger & open items

**HIGH / OBSERVED (re-grounded against the shipped binaries this pass):** the 119 / 0 / 864 /
12 569 / 20 379 keystone counts; the slotfill census `6 288 + 4 158 + 1 790 + 333 = 12 569`; the
180 / 684 float / integer split; the `0x4a09f0 = 4,852,208 B` (≈4.63 MB) cas state size (read from
`dll_get_data_size`); the `avgr`/`adds`/`add`/`maxu` leaves driven **live** byte-exact; the 4-arg
leaf ABI; the 6 ctype dispatchers / 299 `Function_TIE_*`.

**HIGH / CARRIED (proven at the cited sibling, re-used here):** the two-library architecture and the
per-op-class decode → value mapping; the 8-regfile model; the FSR/RNE/integer-only soft-float fp
model and the canonical qNaN; the TIE provenance (1607/12642/19476, the `+13` cipher, name-exact
`xdref`/`xdsem` link); the libctype marshalling layer; the `(format, slot, regfile, value)` closure.

**MED / INFERRED:** the prod-vs-ref behavioural delta (the `ref` variants keep the *same* 864
`xdref` oracle, so this semantic model holds for both — interface identity HIGH, exact delta
INFERRED); the exact `msem`→ISS structural link (the `xtms_*` helpers are compiled into the pipeline,
not kept as named fiss/cas symbols).

**LOW / open (none material to the semantic model):** the full 4-mode FSR enum and any FTZ bit;
the Newton-seed (`recip0`/`rsqrt0`/`sqrt0`/`div0`) polynomial coefficients (named SEED leaves, bodies
not disassembled); the reduce hardware lane-pairing tree (the oracle uses a flat fold, value-equivalent
by associativity); device endianness (LE inferred from host-x86 mirroring).

> **CORRECTION — the cas per-instance state is 4,852,208 B (0x4a09f0, ≈4.63 MB).** An
> off-by-1,024 decimal carried across some early ISS reports was `0x4a05f0` (a 9↔5 nibble
> slip) — exactly **1,024 bytes** below the value `dll_get_data_size` actually loads. The
> disassembly is unambiguous (`1776570: b8 f0 09 4a 00  mov $0x4a09f0,%eax`), and
> `0x4a09f0 = 4,852,208`. The binary is authoritative: per-instance cas state is
> **4,852,208 B (0x4a09f0, ≈4.63 MB)**; the decimal-MB reading is what echoes the slipped
> figure, so prefer ≈4.63 MB. `[HIGH/OBSERVED]`

The remaining open work is the *introspection / single-step / fault-replay / SystemC* surface and the
final cross-layer roll-up — [the Oracle Synthesis](./iss-oracle-synthesis.md) — and the live
validation harness (Part 15, "Driving the ISS as a Golden Oracle"), referenced by title only. With
this page the **semantic** half of Part 14 is closed: the GPSIMD Vision-Q7 ISS is the executable
golden value-and-decode model, and every claim in it is certifiable by driving the real leaf live.
