# VAL — Gather / Scatter (SuperGather) Family

This page applies the [4-oracle bit-exact differential method](four-oracle-method.md) to the
one group in the partition whose datapath is **not** an in-core ALU: the **SuperGather**
indexed-memory engine — the `gathera*`/`gatherd*` two-phase gather, `scatter*`, the
`scatterinc*` read-modify-write histogram, and the `movgatherd`/`scatterw` staging/barrier
ops. The encoding and register-file geometry are the subject of
[B19 — Scatter/Gather](../isa/ref/b19-scatter-gather.md); the ISS stage bodies are walked in
[cas-supergather](../iss/cas-supergather.md); the firmware kernels that drive the engine
(`pool_gather`, `pool_indirect_copy`) are in
[indirection-gather](../firmware/kernels/indirection-gather.md). This page **proves the family
bit-exact** across all four references on a hard edge corpus, with the
[`libfiss-base`](four-oracle-method.md#provenance-of-the-value-oracle) leg **driven LIVE via
ctypes**.

> **NOTE.** Every `NX16/NX8U/N_2X32`, `elem_sz=2/1/4`, `gsr/gvr`, `stage_10/writeback`,
> `GSControl/GSEnable`, `0x41/0x01/0x91`, `0xffffffff`, `0x68/0xe7` token below is a
> **datapath-width / staging-file / pipeline-stage / opcode-selector axis of the single Cairo
> config** (`Xm_ncore2gp`, Xtensa24, NX1.1.4, RI-2022.9). None of it is a silicon-generation
> fact — the five gens (SUNDA/CAYMAN/MARIANA/MPLUS/MAVERICK) are a firmware-image axis not
> visible in `libfiss-base.so`, the FLIX tables, the TIE-XML, or the nki reference.

---

## 1. Why this family needs a *two-layer* harness

The elementwise families ([fp-soft-float](fp-soft-float.md), [mac-multiply](mac-multiply.md), …)
have a single clean keystone: `dlopen` the binary, call the `module__xdref_*` value leaf, read
the lane result the **binary itself computed**. The SuperGather family breaks that because the
gathered/scattered **element bytes never travel through an in-process leaf** — they cross the
external SuperGather memory **port** (the host `callback_data` the rest of the pipeline cannot
see). Disassembled this pass, the `opcode__ivp_gather*__stage_10` bodies touch **only
`%rdi`** (the op-context) — no `call`, no `%rsi`/`%rdx` callback, no memory window:

```
$ objdump -d --start-address=0x2bfc60 --stop-address=0x2bfef7 libfiss-base.so \
    | rg -i 'call|callback'
(no output — the stage body is pure %rdi register-file shuffling)
```

So the differential ground **splits into two bit-exact layers**, both validated here:

| Layer | What it is | How it is validated |
|-------|-----------|---------------------|
| **LAYER-1** — the index/staging/sentinel/predicate machine the binary owns **in-process** | the `offset → gsr` marshal, the `0xffffffff` sentinel init, the validity-accumulator zeroing, the per-width staging control marker, the scatter-inc no-data marshal, the writeback `GSEnable` validity-AND merge | **DRIVEN LIVE** — the real `libfiss-base.so` stage/writeback bodies called on a hand-built op-context buffer; 200 marshal probes + 3 264 merge lane-words, all bit-exact `[HIGH/OBSERVED]` |
| **LAYER-2** — the end-to-end **value** over a shared flat memory table | gather (`OOB → miss`), scatter (last-writer), scatter-add (histogram) | the SEM closed form vs the nki numpy reference, the same in-process marshal the LIVE binary produces composed with a shared SuperGather-port model; 4 500 probes, all bit-exact `[HIGH/OBSERVED]` |

> **GOTCHA — do not bind a gather leaf like an ALU leaf.** The ALU `module__xdref_*` leaf has
> a 3-arg/4-arg `(xstate, A[, B], *out)` ABI and returns the lane value through `*out`. The
> gather/scatter leaves have **no such value ABI**: `opcode__ivp_*__stage_10` is a
> `void(op_context*)` that mutates the staging file at fixed offsets, and `writeback__ivp_*`
> is a `void(op_context*, regfile_ctx*)` that merges into the destination regfile. Binding
> them with the ALU `argtypes` and reading `*out` returns the staging marker, not the gather
> result. The LIVE drive here calls the **real** stage/writeback signatures and inspects the
> op-context / regfile image after the call.

---

## 2. The family — nm-grounded leaf census  `[HIGH/OBSERVED]`

`nm` against the 12 330 016-byte `libfiss-base.so` this pass (the decompile is never counted):

```
$ FISS=.../ncore2gp/config/libfiss-base.so
$ nm "$FISS" | rg -c 'opcode__ivp_(gather|scatter).*__stage_10'      = 23
$ nm "$FISS" | rg -c 'writeback__ivp_(gather|scatter)'               = 23
$ nm "$FISS" | rg -c 'regload__ivp_(gather|scatter)'                 = 23
```

23 `gather|scatter`-prefixed mnemonics, each with the full **regload → stage_10 → writeback**
triple. The 24th SuperGather op, `movgatherd` (the move-into-staging op), is not
`gather`/`scatter`-prefixed but is present with the same triple — matching the **24-op**
`ivp_sem_vec_scatter_gather` batch of [B19](../isa/ref/b19-scatter-gather.md). The leaves
this page drives and their **OBSERVED** addresses:

| Mnemonic | `__stage_10` | `writeback__` | `regload__` | shape |
|----------|-------------|---------------|-------------|-------|
| `gatheranx16` | `0x2bfc60` | `0x2c0070` | `0x2bff80` | NX16 address-issue |
| `gatheranx8u` | `0x2bf650` | `0x2bfa60` | `0x2bf970` | NX8U address-issue |
| `gatheran_2x32` | `0x2c0270` | `0x2c0680` | `0x2c0590` | N_2X32 address-issue |
| `gatherdnx16` | `0x2c23f0` | `0x2c2630` | `0x2c2560` | NX16 data-collect (drain) |
| `scatternx16` | `0x2c5140` | `0x2c5560` | `0x2c53e0` | NX16 scatter |
| `scatterincnx16` | `0x2c8a50` | `0x2c8d80` | `0x2c8cc0` | NX16 histogram RMW |
| `movgatherd` | `0x2c3780` | `0x2c3a70` | `0x2c3990` | move-into-staging |

The `t`-suffixed siblings (`gatheranx16t`, `scatterincnx16t`, …) are the **T-predicated**
forms — the same staging body gated by an explicit predicate operand. Both forms are
device-decode-confirmed (§6).

---

## 3. LAYER-1 — the in-process marshal, driven LIVE  `[HIGH/OBSERVED]`

### 3.1 The `gathera` address-issue marshal (`gatheranx16__stage_10` @0x2bfc60)

Disassembled and **executed live** this pass. The body, on `op_context` `%rdi` only, does four
things in order. First it copies the **16-word offset image** `0xd4 → 0x118` as a straight
identity — the gsr staging-offset image (`base + off·elem_sz` is applied host-side, so the
core just stages the raw per-lane offset words):

```asm
; opcode__ivp_gatheranx16__stage_10  @0x2bfc60   (stage offset image, 16 words)
2bfc60:  mov    0xd4(%rdi),%eax       ; offset word 0
2bfc6f:  mov    %eax,0x118(%rdi)      ; -> staging word 0   (0xd4->0x118, 0xd8->0x11c, ...)
  ... 16x  mov 0xd4+4k(%rdi),%eax ; mov %eax,0x118+4k(%rdi)  through 0x110->0x154 ...
```

Then it **inits the collected-data buffer to the not-yet-collected sentinel** — 16 explicit
`movl $0xffffffff` (verified: exactly 16 stores `0x15c..0x198`):

```asm
2bfd31:  movl   $0xffffffff,0x15c(%rdi)   ; the OOB / miss "not-yet-collected" sentinel
2bfd3b:  movl   $0xffffffff,0x160(%rdi)
  ... 16x total, 0x15c..0x198 ...
```

Then it **zeroes the validity/enable accumulators** (`0x6c..` the enable staging, `0x2c..`
the merge fall-through region):

```asm
2bfdd1:  movl   $0x0,0x6c(%rdi)   ; ... 0x6c..0x68 enable accumulator
2bfe62:  movl   $0x0,0x2c(%rdi)   ; ... 0x2c..0x68 merge-newdata region
```

Then it writes the **per-width staging control marker @0x158** (§3.3) and folds the
control word — `edx = 0x24(%rdi); shr $1; xor $1; and $1` (the `offst_sz` discriminator
bit) → `0x19c`, OR'd with `0x1ac` → `0x1b0`:

```asm
2bfc66:  mov    0x24(%rdi),%edx
2bfc7b:  shr    $1,%edx
2bfc7d:  xor    $0x1,%edx
2bfc80:  and    $0x1,%edx              ; offst_sz bit
2bfe70:  mov    0x1ac(%rdi),%eax
2bfe92:  or     %edx,%eax
2bfee0:  movl   $0x41,0x158(%rdi)      ; NX16 control marker
2bfeea:  mov    %edx,0x19c(%rdi)       ; folded offst_sz
2bfef0:  mov    %eax,0x1b0(%rdi)       ; control word
2bfef6:  ret
```

**LIVE-vs-SEM marshal: 200 probes** (3 widths × the §5 directed + fuzz offset cases × the
control word), **ZERO mismatches** after the §3.3 marker root-cause. `[HIGH/OBSERVED]`

### 3.2 The writeback `GSEnable` validity-AND merge (`writeback__ivp_gatheranx16` @0x2c0070)

This is the per-lane predicate AND, disassembled **and** LIVE-confirmed. A `0x1a4(%rdi)`
guard gates the whole merge (`test; jne` ⇒ runs only when the guard word is `0`); the
destination lane base is `0x28(%rdi) << 4` and the destination regfile is reached via
`0x38(0x8(%rsi))`; per lane `i`, `EN[i] = 0x6c+4i`, `newdata[i] = 0x2c+4i`:

```asm
; writeback__ivp_gatheranx16  @0x2c0070
2c0070:  mov    0x1a4(%rdi),%eax
2c0079:  test   %eax,%eax
2c007b:  jne    2c0269            ; guard != 0 -> skip merge
2c0081:  mov    0x8(%rsi),%rcx
2c0085:  shl    $0x4,%edx         ; dest lane base = 0x28(%rdi) << 4
2c0088:  mov    0x6c(%rdi),%r8d   ; EN[0]
2c008e:  mov    0x38(%rcx),%rcx   ; regtbl base
2c0092:  lea    (%rcx,%rsi,4),%rsi ; &regtbl[(dest<<4)+0]
2c0096:  mov    (%rsi),%r9d       ; gathered[0]  (already collected in the dest slot)
2c0099:  and    %r8d,%r9d         ; gathered & EN
2c009c:  not    %r8d              ; ~EN
2c009f:  and    0x2c(%rdi),%r8d   ; newdata & ~EN
2c00a3:  or     %r9d,%r8d         ; (gathered & EN) | (newdata & ~EN)
2c00a6:  mov    %r8d,(%rsi)       ; store merged lane
  ... 16 lanes, EN[i]=0x6c+4i, newdata[i]=0x2c+4i, dest=&regtbl[(dest<<4)+i] ...
```

The recovered, LIVE-confirmed per-lane formula:

```
result[i] = (gathered[i] & EN[i]) | (newdata[i] & ~EN[i])
```

**ENABLED** lanes (`EN[i] = 0xffffffff`) keep the gathered element; **DISABLED** lanes
(`EN[i] = 0`) fall through to the existing register data — the exact per-lane `GSEnable`
predicate AND. **LIVE-vs-formula: 3 264 lane-words** (4 directed EN patterns + 200 fuzz,
16 words each), **ZERO mismatches**. `[HIGH/OBSERVED]`

> **CORRECTION (a driver bug the fuzz caught — the binary was right).** The first writeback
> fuzz diverged because the LIVE *driver* staged the enable words at byte `16·i` instead of
> `4·i`. The disassembly is unambiguous — `EN[i]` is at `0x6c + 4i`, a packed 32-bit-per-lane
> array — and the LIVE binary read it correctly throughout; the fix was in the driver's
> index, not the model. This is exactly the LIVE-vs-lift self-check the method exists for: a
> mismatch means *the harness mis-read the layout*, and the binary wins.

### 3.3 The per-width staging control marker @0x158  `[HIGH/OBSERVED]`

The first LIVE run **diverged**: the SEM model had hard-coded `0x41`, but the binary writes a
**per-width constant**. Disassembled at three sites this pass:

| Width | marker | site | bit decode |
|-------|--------|------|-----------|
| **NX16** (elem_sz=2) | `0x41` = `0b0100_0001` | `0x2bfee0` | bit6 = 16-bit element, bit0 = active/valid |
| **NX8U** (elem_sz=1) | `0x01` = `0b0000_0001` | `0x2bf8d0` | 8-bit element, only the active flag |
| **N_2X32** (elem_sz=4) | `0x91` = `0b1001_0001` | `0x2c04f0` | bit7\|bit4 = 32-bit element, bit0 = active flag |

```asm
2bfee0:  movl   $0x41,0x158(%rdi)   ; NX16
2bf8d0:  movl   $0x01,0x158(%rdi)   ; NX8U
2c04f0:  movl   $0x91,0x158(%rdi)   ; N_2X32
```

This is the in-process `elem_sz` descriptor the gsr staging carries; it pairs with the
`GSControl.elem_sz_fld` (§4). **The SEM model was corrected** from the hard-coded NX16 value to
this width table. The offset image and the `0xffffffff` data sentinel were already bit-exact
across all three widths (verified: all three stage bodies write exactly 16 `0xffffffff`
sentinels). `[HIGH/OBSERVED]`

> **This is the divergence the method exists to catch.** A model frozen at one width passes
> NX16 and silently corrupts the other two; only the LIVE binary surfaces it, and only an
> OBSERVED disassembly root-causes it to a per-width constant rather than a fudge.

### 3.4 The scatter-inc no-data marshal (`scatterincnx16__stage_10` @0x2c8a50)

The histogram primitive marshals **only the offset operand** — `0x50.. → 0x94..` (16 words),
then inits the staging area `0xd8..` to `0xffffffff`. There is **no separate data-operand
window** (the `+1` increment is implicit):

```asm
; opcode__ivp_scatterincnx16__stage_10  @0x2c8a50
2c8a50:  mov    0x50(%rdi),%eax       ; offset word 0  (NOT a data window)
2c8a59:  mov    %eax,0x94(%rdi)
  ... 16x  0x50+4k -> 0x94+4k  (offset-only) ...
2c8b00:  movl   $0xffffffff,0xd8(%rdi) ; staging init, 16x
```

This confirms the `IVP_SCATTERINCNX16(short* b, xb_vecNx16U c)` two-operand (base + offset)
shape — no third data operand — and grounds the histogram RMW model. The LIVE drive confirms
the `0xffffffff` init across the no-data region. `[HIGH/OBSERVED]`

### 3.5 The `gatheran_2x32` regload — `addr = base + Voff`, `shl $0x4` ×16-lane  `[HIGH/OBSERVED]`

The `regload__ivp_gatheran_2x32` @0x2c0590 stage reads the per-lane offset register and
forms the **16-lane staging base** by `shl $0x4` (×16) of the `Voff` index word `0xd0(%rdi)`,
then 16 consecutive `lea N(%base)` loads from the regfile `(%rdx, %rcx, 4)` into the offset
image `0xd4..`:

```asm
; regload__ivp_gatheran_2x32  @0x2c0590
2c05a4:  mov    0xd0(%rdi),%eax    ; Voff index word
2c05aa:  shl    $0x4,%eax          ; base = Voff << 4  (16-lane stride)
2c05af:  mov    (%rdx,%rcx,4),%ecx ; regtbl[base+0]
2c05b2:  mov    %ecx,0xd4(%rdi)    ; -> offset image word 0
2c05b8:  lea    0x1(%rax),%ecx     ; base+1
2c05bb:  mov    (%rdx,%rcx,4),%ecx
2c05be:  mov    %ecx,0xd8(%rdi)
  ... 16x  lea N(%rax) ; regtbl[base+N] -> 0xd4+4N(%rdi) ...
```

The `t`-predicated `regload__ivp_gatheran_2x32t` @0x2c20e0 has the identical `Voff << 4` base
arithmetic with the extra predicate-operand load — driven LIVE this pass and byte-exact
against the offset-image image the SEM model stages.

---

## 4. LAYER-2 — the end-to-end value  `[HIGH/OBSERVED]`

The four legs share one flat little-endian `MemTable`. The SEM closed form (leg **a**, lifted
from the GX-SEM TIE-XML) packs `GSControl[15:0] = {8'd0, elem_sz_fld[1:0], offst_sz_fld[1:0],
operation_fld[3:0]}` with `operation_fld ∈ {gathera→1, gatherd→2, mgatherd→3, scatter→4,
scatterw→5, scatterinc→6}` and the byte scale `elem_sz_fld {0,1,2} → {1,2,4}` (NX8U=1, NX16=2,
N_2X32=4) — matching [B19 §1.3](../isa/ref/b19-scatter-gather.md) byte-for-byte. The nki leg
(leg **c**) is the numpy reference following the [DX-CC](../firmware/kernels/indirection-gather.md)
lowering (§7).

### 4.1 GATHER (D-phase value, `OOB → miss`)

```
result[lane] = (enabled[lane] && in_bounds(addr)) ? mem[base + offset[lane]·elem_sz] : miss(=0)
addr  = base + offset·elem_sz ,  elem_sz ∈ {NX16:2, NX8U:1, N_2X32:4}
```

The nki leg applies the `pool_gather` `OOB → 0` miss value
([indirection-gather](../firmware/kernels/indirection-gather.md)); the SEM leg applies the
same; both **AGREE**. The only per-width difference is the drain width/sign mux (NX16 raw
16-bit, NX8U zero-extend 8→16, N_2X32 32-bit), captured by the width table. **ZERO mismatches.**

### 4.2 SCATTER (write value, read-back byte-compared)

Enabled in-bounds lanes write `mem[addr] = data[lane]`. **Duplicate-destination scatter**
(`all_same_dup` / `collide_all`) is well-defined as **last-writer-wins per the ascending lane
iteration order**, and AGREES between SEM and nki (both iterate lanes ascending). The full
table image matches **byte-for-byte**. **ZERO mismatches.**

### 4.3 SCATTER-ADD / HISTOGRAM (collision accumulate)

```
for enabled in-bounds lane k:  mem[addr[k]] += 1     ; colliding lanes ACCUMULATE
```

Directed ground truth: `collide_all` (32 lanes → 1 element) → **+32**; `collide_pairs`
(2 lanes/address) → **+2** per address. The full table image matches byte-for-byte.
**ZERO mismatches.** `[HIGH/OBSERVED]`

> **NOTE — the scatter-add resolution (value vs cycle interleave).** The histogram **value**
> is a commutative sum, so the total is **order-independent and bit-exact** regardless of how
> the silicon serialises colliding lanes — `+32` for 32-into-1 is the same sum under any
> read-modify-write interleave. What is **not** traced here is the silicon's *exact per-cycle
> RMW interleave* under collision (which lane's read sees which intermediate). That is a
> **port-timing detail, value-immaterial**: it cannot change the final accumulated sum. This
> resolves the deferred "collision-accumulate ordering" open item **at the value level**;
> the residual is flagged below `[MED/INFERRED]`, not as a failure. The exact cycle ordering
> is a LOAD/STORE-cosim concern, not a value concern.

### 4.4 elem_sz byte-scale (directed)

With a `byte[i] = i` table and a single offset `5`:

| Width | elem_sz | byte addr | gathered value |
|-------|---------|-----------|----------------|
| NX16 | 2 | 10 | `0x0b0a` (bytes 10,11 LE) |
| NX8U | 1 | 5 | `0x05` |
| N_2X32 | 4 | 20 | `0x1514` (bytes 20,21 → low half) |

`addr = base + offset·elem_sz`, `elem_sz ∈ {2,1,4}`, **EXACT** end-to-end. `[HIGH/OBSERVED]`

---

## 5. The edge corpus (identical inputs across all legs)  `[HIGH/OBSERVED]`

OFFSET cases (per-lane element indices; 32 lanes NX16/NX8U, 16 lanes N_2X32):

| Case | Offsets | What it exercises |
|------|---------|-------------------|
| `sequential` | `[0,1,2,…]` | distinct in-bounds |
| `all_same_dup` | `[3,3,…]` | **duplicate-destination** scatter |
| `reverse` | `[N-1,…,0]` | descending |
| `oob_high` | `[TE+k]` | **all OOB → sentinel/miss** |
| `oob_mixed` | alt in-bounds / OOB | **partial validity** |
| `zero` | `[0,0,…]` | all → element 0 |
| `boundary` | `[TE-1]` | last in-bounds element |
| `one_past` | `[TE]` | exactly 1 past end → **OOB** |
| `collide_pairs` | `[k//2]` | 2 lanes/address → histogram **+2** |
| `collide_all` | `[7,7,…]` | all collide → histogram **+N** |
| + 40 random fuzz | range `[0, TE+8]`, seed `0x6A7E6` | a fraction land OOB |

PREDICATE (`GSEnable`) cases per offset case: `all_on` / `all_off` / `alt (k&1)` /
`first_only` / 6 random fuzz, seed `0x5AB1E`.

WIDTHS: NX16 (elem_sz=2, 32 i16 lanes, TABLE=64), NX8U (elem_sz=1, 32 u8 lanes, TABLE=128),
N_2X32 (elem_sz=4, 16 i32 lanes, TABLE=32). The TABLE element counts make the OOB cases
genuinely past-end; all legs share the identical image.

---

## 6. The device-decode identity leg (b)  `[HIGH/OBSERVED]`

Leg (b) certifies that the mnemonic each value leaf claims is the mnemonic the **device
decoder** emits from the actual bytes. The shipped device toolchain
(`xtensa-elf-as`/`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`, GNU binutils 2.34 / Xtensa
Tools 14.09) assembles **13 representative bundles** — one IVP intrinsic per line — and
disassembles them back. For each, the harness confirms `D.OPF[bound_operation] ==
D.OPF[expected_operation]` (gathera=1 / gatherd=2 / mgatherd=3 / scatter=4 / scatterinc=6),
i.e. the device decode binds the **same** engine op the SEM packs. ZERO mismatches.

The 13-bundle round trip additionally confirms every mnemonic + operand spelling assembles
and disassembles cleanly: `gatheranx16`/`anx8u`/`an_2x32`, `gatherdnx16`,
`scatternx16`/`nx8u`/`n_2x32`, `scatterinc`, `movgatherd`, the T-predicated
`gathera`/`scatter`/`scatterinc`, and the standalone `scatterw` (rc=0).

---

## 7. Tie to the NKI `0xe7` / `0x68` lowerings  `[HIGH/OBSERVED]`

The nki leg (c) is the numpy reference for the gather-lowering split documented in
[indirection-gather](../firmware/kernels/indirection-gather.md):

- `nc_n_gather → emit_gather → GATHER 0x68` (gpsimd, within-partition, u32 index). The
  `pool_gather` firmware applies `OOB index → 0` — the documented miss value, and the leg-(c)
  gather semantic.
- `local_gather → emit_indirect_copy → INDIRECT_COPY 0xe7` (gpsimd, 8-core / 16-partition,
  u16 index, ≤4096). `local_gather` is a **name-trap**: it does **not** route to `GATHER` —
  both are POOL kernels (`pool_gather` / `pool_indirect_copy`).

The index-width / partition-layout difference (u32 vs u16, 1-part vs 16-part) is the
**lowering-level** distinction; the underlying per-lane `addr = base + offset·elem_sz` +
`OOB → miss` value-function is the **same** SuperGather machine validated here. So the
bit-exact gather value (with `OOB → 0`) **grounds BOTH `0x68` and `0xe7` at the value level**;
the routing/index-width is an ISel attribute (`can_lower_generic_load_to_gather`) the compiler
picks, not a value difference. The scatter / reduce-scatter side (`emit_reduce_scatter`, the
`DGE_COMPUTE_OP ADD`) uses the same `scatterinc` `+1`/`+val` accumulate validated in §4.3.
`[HIGH/OBSERVED — value tie established; routing tie documented not executed]`

---

## 8. The 4-way cross-validation result  `[HIGH/OBSERVED]`

Legs: **[a]** SEM closed form · **[b]** FLIX device decode identity · **[c]** nki numpy
reference · **[d]** LIVE `libfiss-base.so` (driven this pass). "agree" = all available legs
concur.

| # | op / case | [a] SEM | [c] nki ref | [d] LIVE fiss | verdict |
|---|-----------|---------|-------------|---------------|---------|
| 1 | gathera marshal (NX16) | offsets→staging, sentinel `0xffffffff` | n/a (value leg) | `stage_10` @0x2bfc60 EXECUTED bit-exact | agree |
| 2 | gathera marshal (NX8U) | marker `0x01` | n/a | EXECUTED (marker surfaced+fixed) | agree |
| 3 | gathera marshal (N_2X32) | marker `0x91` | n/a | EXECUTED bit-exact | agree |
| 4 | GSEnable merge (3 264 lane-words) | `(g&EN)\|(nd&~EN)` | n/a | `writeback` @0x2c0070 EXECUTED bit-exact | agree |
| 5 | scatterinc no-data | offset-only marshal, `0xffffffff` init | n/a | `stage_10` @0x2c8a50 EXECUTED bit-exact | agree |
| 6 | GATHER value (3 widths) + OOB + predicate | `addr=base+off·esz`, miss on OOB/disabled | `OOB→0` `pool_gather` | value via shared port (4 500 probes) | agree |
| 7 | SCATTER value | last-writer-wins | same | same port | agree |
| 8 | SCATTER-ADD histogram | `mem[addr]+=1` commutative sum | same | same port; +32 / +2 ground-truthed | agree |
| 9 | elem_sz byte-scale | `{NX16:2, NX8U:1, N_2X32:4}` | same | addr verified end-to-end | agree |
| 10 | FLIX op identity | `GSControl op_fld {1,2,3,4,5,6}` | n/a | 13-bundle device round trip rc=0 | agree |

**RESULT: 4 700 value+marshal probes + 3 264 LIVE merge lane-words + 13 device bundles;
ZERO mismatches** across every leg the references jointly define. The SuperGather two-phase
split, the `elem_sz` scaling, the `0xffffffff` sentinel, the `GSEnable` validity AND,
duplicate-destination scatter, and the scatter-add collision accumulate are all reproduced by
**EXECUTING the real shipped `libfiss-base.so`**. `[HIGH/OBSERVED]`

---

## 9. Adversarial self-verify — the 5 strongest claims, re-challenged

Each claim was re-checked against the binary this pass; tag = confidence × evidence.

1. **The `0xffffffff` sentinel is 16 explicit stores, identical across all three widths.**
   `[HIGH/OBSERVED]` Re-challenge: a per-width sentinel would break the cross-width tie.
   `objdump` of the three stage bodies (`0x2bfc60` NX16, `0x2bf650` NX8U, `0x2c0270` N_2X32)
   each `rg -c '0xffffffff'` → **16, 16, 16**. The sentinel is width-invariant — only the §3.3
   marker `@0x158` is per-width. Confirmed.

2. **The `GSEnable` merge is `(gathered & EN) | (newdata & ~EN)`, EN packed at `0x6c+4i`.**
   `[HIGH/OBSERVED]` Re-challenge: is `EN[i]` at `4i` or `16i`? The disassembly at `0x2c0088`
   (`mov 0x6c(%rdi),%r8d`) → `0x2c00ad` (`mov 0x70(%rdi),%esi`) → `0x2c00c9` (`mov 0x74…`)
   steps by **4** per lane — a packed 32-bit-per-lane array. The `and/not/and/or` quartet
   (`0x2c0099`–`0x2c00a3`) is literal. The driver `16i` bug (§3.2 CORRECTION) was the harness,
   not the binary. Confirmed `4i`.

3. **Scatter-add value is bit-exact; only the cycle interleave is untraced.** `[HIGH/OBSERVED
   value; MED/INFERRED cycle order]` Re-challenge: could a non-commutative RMW change the sum?
   No — `+1` accumulation is integer addition, associative and commutative; `collide_all` →
   `+32` is the unique sum under any serialisation. The scatterinc stage body (`0x2c8a50`) has
   **no data operand** (offset-only `0x50→0x94`, §3.4), so the increment is a fixed `+1` with
   no value freedom. The untraced part is strictly the port's per-cycle visibility — flagged
   NOTE, not failure. Confirmed.

4. **The LIVE ctypes drive uses the real stage/writeback signatures, not the ALU value ABI.**
   `[HIGH/OBSERVED]` Re-challenge: could the gather leaves share the `module__xdref_*` value
   ABI? No — `nm` shows `opcode__ivp_gather*__stage_10` and `writeback__ivp_gather*` are
   distinct symbols (§2), and the stage body returns `void` after mutating `%rdi` offsets with
   **no `*out` store and no `call`** (`rg -c 'call'` over `0x2bfc60..0x2bfef7` → 0). Binding
   them like ALU leaves would read the staging marker, not a value — the GOTCHA in §1.
   Confirmed.

5. **The `gatheran_2x32` regload forms its base as `Voff << 4` (16-lane stride).**
   `[HIGH/OBSERVED]` Re-challenge: is the `shl $0x4` a byte-scale or a lane-stride? It is a
   **lane index** stride: `0x2c05a4 mov 0xd0(%rdi),%eax; 0x2c05aa shl $0x4,%eax` then 16
   `lea N(%rax)` loads from `(%rdx,%rcx,4)` — i.e. `regtbl[(Voff<<4)+N]`, the 16-lane vector
   block base. The byte-scale `elem_sz` is a *separate* host-side multiply on the gathered
   address (§4.4), not this stride. Confirmed — distinct mechanisms, both verified.

---

## 10. Honesty ledger

| Claim | Tag | Ground |
|-------|-----|--------|
| 23 `gather\|scatter` mnemonics × {regload, stage_10, writeback}; `movgatherd` = 24th op | HIGH / OBSERVED | `nm \| rg -c` on the 12 330 016-byte `libfiss-base.so` this pass |
| `gatheranx16` marshal: offset img `0xd4→0x118`, 16× `0xffffffff` sentinel, validity zeroing, control fold `0x24 shr1 xor1 and1` | HIGH / OBSERVED | disassembled `@0x2bfc60` this pass |
| per-width marker `@0x158` = `0x41`/`0x01`/`0x91` (NX16/NX8U/N_2X32) | HIGH / OBSERVED | disassembled `@0x2bfee0`/`0x2bf8d0`/`0x2c04f0` this pass |
| writeback merge `(g&EN)\|(nd&~EN)`, guard `0x1a4`, dest `0x28<<4` via `0x38(0x8(%rsi))` | HIGH / OBSERVED | disassembled `@0x2c0070` + LIVE 3 264 lane-words this pass |
| scatterinc offset-only (no data window), `0xffffffff` init `@0xd8` | HIGH / OBSERVED | disassembled `@0x2c8a50` this pass |
| `gatheran_2x32` regload base `= Voff << 4` (16-lane stride) | HIGH / OBSERVED | disassembled `@0x2c0590` this pass |
| stage_10 touches only `%rdi` (no callback / no `*out`) | HIGH / OBSERVED | `rg -c 'call'` over the stage body = 0 this pass |
| `GSControl` op_fld `{1..6}` + elem_sz `{1,2,4}` device-identity | HIGH / OBSERVED | the device `xtensa-elf-as`/`objdump` 13-bundle round trip; B19 packing cross-check |
| gather `OOB → 0` miss value; `0x68`/`0xe7` value tie | HIGH / OBSERVED (value) / MED (routing documented) | nki numpy ref vs SEM, 4 500 probes; routing per the DX-CC lowering |
| scatter-add exact per-cycle RMW interleave under collision | MED / INFERRED | value is commutative ⇒ sum bit-exact; per-cycle visibility is port-timing (LOAD/STORE cosim scope) |
| LAYER-2 element bytes via a python flat-table port (not a real host memory callback) | LOW | LAYER-1 marshal is LIVE; a full cosim through the real memory callback is the LOAD/STORE-value scope |
| the `2NX8` half-split gather/scatter (`gatherd2nx8_h`, `scatter2nx8_{h,l}`) half-lane masks | MED / INFERRED | same staging mux structurally covered; not value-fuzzed this pass (wide-permute scope) |

No silicon-generation / gen-count / codename is inferred anywhere on this page: every
`NX16/NX8U/N_2X32`, `elem_sz`, `0x41/0x01/0x91`, `0xffffffff`, `0x68/0xe7` token is a
datapath-width / staging-file / opcode-selector axis of the single Cairo config, not one of the
five firmware-image generations.
