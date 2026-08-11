# Iota / sequence-index generator

This page decodes the **Iota** kernel of the NeuronCore GPSIMD ISA — the **POOL-engine**
handler that *synthesizes* a strided arithmetic ramp `dst[element] = base + Σ_d index_d *
step_elem[d] (+ channel * channel_multiplier)` and writes it to SBUF or PSUM. It reads
**no input tensor**: the entire sequence is generated from the operand struct alone, which
is why the on-device function takes **zero runtime arguments**. It pins the opcode
(`0x7e`, `pool_iota`), the 64-byte `D4_IOTA` operand struct byte-for-byte from the shipped
ISA header (compile-verified `sizeof == 64` on all four generations), the
`movva32`/`addn_2x32`/`muln_2x32`/`packln_2x96` ramp datapath recovered from the carved
POOL firmware, the per-channel `channel_multiplier` stride that gives each partition a
distinct base, and the `<true>`=fp-output vs `<false>`=int-output **C++-template** split
(byte-OBSERVED: 3× `ivp_floatn_2x32` in the `<true>` body, 0 in `<false>`).

It also carries one standing **CORRECTION**: Iota is **not** a SUNDA-unique kernel. It is a
**universal POOL instruction** present from SUNDA (NC-v2) through MAVERICK (NC-v5). The
*only* cross-generation difference is the C++ symbol **name** — `iota_kernel` on SUNDA,
`iota_impl` on CAYMAN and later — for one and the same opcode, struct, template shape, and
datapath. See §7.

Iota runs on the **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW DSP**
(`ncore2gp` config, one per NeuronCore) — specifically on the **POOL** (Pooling)
sequencer. It is the standalone, first-class form of the position/index ramp that
[Sort](./sort.md), [NonzeroWithCount](./nonzero-with-count.md), and the gather/indirect
family ([indirection-gather](./indirection-gather.md)) otherwise inline as their index
payload; all of them compose the same `movva32`-broadcast + `addn_2x32`-accumulate primitive
documented in the [Vector Move / regfile bridge ISA batch](../../isa/ref/b09-vec-mov.md).
There is **no** single `ivp_seqn`/`iota` micro-op — the ramp is always *composed*.

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every host-ISA fact is read out of the public
`aws_neuron_isa_tpb_*.h` headers shipped in the customop-lib package and compile-verified;
every device fact is byte-pinned to a carve from the
shipped EXTISA firmware containers and disassembled with the native `xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`).

> **NOTE — the exact objects used.** Two shipped
> containers hold the Iota bodies. The SUNDA EXTISA device ELF is carved from
> `…/opt/aws/neuron/lib/libnrtucode_extisa.so` (the runtime-lib package,
> `sha256 dc00763d…`) at file offset `0x921660` / `0xd308`; the CAYMAN / MARIANA /
> MARIANA_PLUS EXTISA device ELFs are carved from
> `…/custom_op/c10/lib/libnrtucode_internal.so` (`sha256 b7c67e89…`). Each carve is a
> standalone `EM_XTENSA` `ELF32 EXEC` with its own `.text` (VMA `0x01000000` == file
> offset `0x100`) and per-function `.xt.prop` sections. The **SUNDA image decodes
> cleanest** under the FLIX disassembler and is the algorithm authority; the CAYMAN/MARIANA
> bodies are byte-near-identical to it (and to each other) but desync under a stock linear
> sweep (§8), so they are pinned by name+opcode+struct+trampoline, not re-listed op-by-op.
>
> | carve | container off / size | iota func-starts (`<true>`/`<false>`) | `.xt.prop` sizes | table idx |
> |---|---|---|---|---|
> | `SUNDA_0` (NC-v2) | `0x921660` / `0xd308` | `0x01005f40` / `0x01006100` | `0x108` / `0x0e4` | idx8 |
> | `CAYMAN_0` (NC-v3 MAIN) | `0x2ef7e0` / `0xa260` | `0x01000100` / `0x010002c0` | `0x108` / `0x0e4` | idx0 |
> | `CAYMAN_3` (NC-v3 CODEC) | `0x2fbf00` / `0x6974` | `0x01000100` / `0x010002c0` | `0x108` / `0x0e4` | idx0 |
> | `MARIANA_0` (NC-v4) | `0x5893c0` / `0xa280` | `0x01000100` / `0x010002c0` | `0x108` / `0x0e4` | idx0 |
> | `MARIANA_PLUS_0` | `0x855240` / `0xa280` | `0x01000100` / `0x010002c0` | `0x108` / `0x0e4` | idx0 |

---

## 1. The headline

Iota is a **first-class, named ISA instruction** — a sequence/index *generator*, not a
composed host helper. Six facts pin it:

1. **Opcode `= 0x7e`, struct `= D4_IOTA` (64 B).** From `common.h:214`
   (`NEURON_ISA_TPB_OPCODE_IOTA = 0x7e, // Y` — the `// Y` marks a *released* opcode), the
   `instruction_mapping.json` `struct2opcode` binding
   `NEURON_ISA_TPB_D4_IOTA_STRUCT → [NEURON_ISA_TPB_OPCODE_IOTA]`, and `debug_assert.h:219`
   (`case NEURON_ISA_TPB_OPCODE_IOTA: check = dbg_is_valid_iota(i);`). The struct
   compile-verifies `sizeof == 64` on sunda/cayman/mariana/maverick. The SUNDA container's
   baked opcode→name JSON lists `"pool_iota"` opcode `126`. `[HIGH/OBSERVED]`

2. **It computes a strided multi-dimensional ramp, not a memory read.** The source is a
   `DATA4D` *generator descriptor* (`base` + per-dim `step_elem[4]` + per-dim
   `num_elem[4]`), not a `TENSOR4D` *memory pattern*. There is no input tensor; Iota emits
   `base + Σ_d index_d * step_elem[d]` directly. A 1-D iota is the degenerate
   single-active-dim case; the general N-D iota is a strided multi-dim index ramp.
   `[HIGH struct; INFERRED arithmetic]`

3. **Each partition can start at a different base.** The `channel_multiplier` (`int32 @36`)
   adds `channel * channel_multiplier` to lane *channel*'s ramp, so each of the 1..128
   partitions carries a distinct phase/offset (a partition-index ramp, a tiled-position
   offset, etc.). `[HIGH field; INFERRED semantics]`

4. **The `<true>`/`<false>` template axis is the OUTPUT dtype CLASS (fp vs int) — nothing
   else.** `iota_kernel<true>`/`iota_impl<true>` (`ILb1E`) does the integer→float convert
   (3× `ivp_floatn_2x32` + `ivp_ufloat16nx16t`, byte-OBSERVED). `<false>` (`ILb0E`) emits
   the raw integer ramp (0× `ivp_floatn_2x32`). The N-dimensionality is the runtime
   `DATA4D`; the direction is the *sign* of `step_elem`; the start is `base`; the
   per-channel phase is `channel_multiplier` — none of those is the template axis.

5. **It dispatches through a single dtype-branch trampoline.** The POOL
   `kernel_info_table` row `{opcode 0x7e → trampoline VA}` lands on one `entry`-framed
   trampoline that tests `out_dtype` against the FP32 constant (`0xA`) and `const16`+`callx8`s
   the matching precompiled body.

6. **It is universal.** Opcode `0x7e`, the 64-byte struct, the validity predicate, and the
   1..128 channel rule are byte-identical across SUNDA/CAYMAN/MARIANA/MAVERICK; only the
   function name differs (`iota_kernel` vs `iota_impl`). Iota is the *oldest/most-universal*
   POOL index op, not a SUNDA addition (§7).

> **NOTE — Iota is the position PRODUCER.** Unlike Sort and NonzeroWithCount, which carry an
> index *payload* beside their values, Iota emits the index/sequence **as** the value — the
> ramp *is* the output. There is no separate index dtype; `out_dtype` is the ramp's dtype.
> The `addn_2x32`/`muln_2x32` ramp here is the same `0..N-1`-plus-offset position source that
> Sort rides through compare-exchange, that NonzeroWithCount compacts under a predicate, and
> that the gather index machinery feeds into address-gen.

---

## 2. The operand struct — `D4_IOTA` (64 B, compile-verified, all 4 gens)

The operand is `NEURON_ISA_TPB_D4_IOTA_STRUCT`, declared verbatim in
`aws_neuron_isa_tpb_d4_iota.h` (present under
`neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/`). `instruction_mapping.json` binds
the struct to `NEURON_ISA_TPB_OPCODE_IOTA`. A `gcc -I<hdr>` +
`sizeof`/`offsetof` compile-verify reproduces the layout for **all four** generations — **identical**:
`sz=64`, `src@12`, `out_dtype@33`, `nac@34`, `channel_multiplier@36`, `dst@44`,
`OPCODE_IOTA=0x7e`. `[HIGH/OBSERVED]`

| off | size | field | type | role |
|---|---|---|---|---|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{ opcode=0x7e, inst_word_len, debug_cmd, debug_hint }` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update semaphore sync `{wait_mode,wait_idx,update_mode,update_idx,sem_value}` |
| 12 | 20 | `src_data_pattern` | `NEURON_ISA_TPB_DATA4D` | **the iota geometry** (base + step + count) |
| 32 | 1 | `reserved0[1]` | `uint8_t` | MUST be 0 |
| 33 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | **OUTPUT dtype** (selects `<true>`/`<false>`) |
| 34 | 1 | `num_active_channels` | `uint8_t` | 1..128 partitions |
| 35 | 1 | `reserved1[1]` | `uint8_t` | MUST be 0 |
| 36 | 4 | `channel_multiplier` | `int32_t` | **PER-CHANNEL additive stride** |
| 40 | 4 | `reserved2[4]` | `uint8_t` | MUST be 0 |
| 44 | 20 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR4D` | OUTPUT (write; SBUF or PSUM) |

The `DATA4D` generator descriptor (`common.h:649`) is the ramp itself — and it is *not* a
`TENSOR4D`: it has no `start_addr`, because Iota reads nothing.

```c
typedef struct NEURON_ISA_TPB_DATA4D {   /* common.h:649 — 20 B, the iota ramp */
    int32_t  base;          /* @ 0  the iota START value                     */
    int16_t  step_elem[4];  /* @ 4  per-dimension STEP/stride (signed!)      */
    uint16_t num_elem[4];   /* @12  per-dimension COUNT/length               */
} NEURON_ISA_TPB_DATA4D;
```

Contrast the `dst_mem_pattern`, which *is* a `TENSOR4D` (`common.h:643`, with a
`start_addr` `ADDR4` plus matching `step_elem[4]`/`num_elem[4]`) — the write target in
SBUF/PSUM. The total element count is the product of the active dims:
`d4d_element_count = num_elem[0]*num_elem[1]*num_elem[2]*num_elem[3]`, and the validity
predicate enforces `d4d_element_count(src) == t4d_element_count(dst)` so the ramp exactly
fills the destination (no over/under-run).

> **GOTCHA — `step_elem` is signed `int16`, `num_elem` is unsigned `uint16`.** The ramp
> direction (ascending/descending) is the *sign of `step_elem[d]`*, a runtime operand field
> — **not** the `<true>`/`<false>` template (§6). A descending iota is `step_elem[d] < 0`.
> Because `step_elem` is 16-bit but the ramp accumulator is 32-bit (`*_2x32` ops, §3), the
> per-step increment is sign-extended before accumulation (`sext a15,a11,15` appears in the
> SUNDA body). `[HIGH header; INFERRED sign-extend]`

> **NOTE — zero runtime args is a structural consequence, not an accident.** Because the
> source is a `DATA4D` (base/step/count) rather than a tensor read, the *entire* sequence is
> struct-driven. The demangled symbol signature is `void iota_kernel<bool>()` /
> `void iota_impl<bool>()` — `()` , no parameters. The body recovers `base`/`step`/
> `channel_multiplier` by `l32i`-loading them out of the descriptor the trampoline passes
> (`l32i a3, a0, 124` in the SUNDA trampoline — the descriptor pointer).

### 2.1 The validity predicate

`debug_assert.h:219` binds `OPCODE_IOTA → dbg_is_valid_iota(i)` (decode-time validity,
all 4 gens). The header states the predicate verbatim (`is_valid_iota`, identical across
all four `d4_iota.h`):

```
has_valid_neuron_header(i) && has_valid_neuron_events(i)
  && has_iota_opcode(i)                                          /* header.opcode == 0x7e */
  && d4_iota_reserved_zero(i)                                    /* reserved0/1/2 all 0   */
  && is_valid_dtype(out_dtype, DtypeAllowFP32R::True)            /* ANY valid dtype, incl FP32R */
  && has_valid_active_channel_range(num_active_channels, POOLING_NUM_CHANNELS /* 128 */)
  && start_addr_active_channels(dst.start_addr, num_active_channels)
  && tensor4d_valid(dst, out_dtype, WriteTensor::True, AllowedInPSUM::True, AllowedInSBUF::True)
  && d4_iota_same_src_dst_count(i)                               /* d4d count == t4d count */
```

`is_valid_dtype(..., AllowFP32R::True)` (the `DTYPE_ALLOW_FP32R` enum is `{FALSE=0,TRUE=1}`,
`common.h:1137`) explicitly admits **FP32R (`0xB`)** in addition to plain FP32 — Iota is one
of the ops that may emit the rounded/partial FP32 type. `dst` may be written to **either**
SBUF **or** PSUM (`AllowedInPSUM::True && AllowedInSBUF::True`).

---

## 3. The algorithm — broadcast base + accumulate step + (fp) convert

The SUNDA bodies decode cleanly under the FLIX disassembler and are the algorithm
authority. The recovered datapath is a classic SIMD ramp: broadcast the scalar base and the
scalar per-step strides into vector lanes, accumulate one `addn_2x32` per dimension/tile,
scale by the step factor with one `muln_2x32`, pack to the output lane width, and — only in
the fp body — convert to float.

### 3.1 The dispatch trampoline (`SUNDA @0x01005e80`, byte-OBSERVED)

```text
1005e80:  entry  a1, 32
1005e9c:  l32i   a3, a0, 124                          ; a3 = operand descriptor pointer
1005ec3:  { bgeui.w15 a4, 16, 0x1005f09 ; slli a3,a3,4 }   ; dtype-class branch
1005ece:  { const16 a4, 0x100 ; movi a5, 10 }         ; movi a5,10 = FP32 dtype (0xA) discriminator
1005ed6:  const16 a4, 0x5f40                          ; a4 = 0x01005f40  (<true>  fp body)
1005f09:  const16 a4, 0x6100   (branch target)        ; a4 = 0x01006100  (<false> int body)
1005f02:  callx8 a4                                   ; call the selected template body
1005f07:  retw.n
```

The trampoline loads the descriptor (`l32i a3,a0,124`), tests `out_dtype` against the FP32
constant `0xA` (the `movi a5,10` discriminator), and `const16`-builds **either** `0x5f40`
(the `<true>` fp body) **or** `0x6100` (the `<false>` int body), then `callx8`s it. The
CAYMAN trampoline `@0x01000080` is the same shape (`entry a1,32`; `bgeui` dtype branch;
`const16 0x100` `<true>` / `const16 0x2c0` `<false>`; `callx8`) — and its FLIX bundle even
shows live IVP ops, confirming a real body lives there.
`[HIGH/OBSERVED; MED branch immediate]`

```c
/* The POOL trampoline for opcode 0x7e (real symbols: the kernel_info_table funcVA target).
 * SUNDA @0x01005e80 (idx8); CAYMAN/MARIANA/MARIANA_PLUS @0x01000080 (idx0). */
static void pool_iota_trampoline(const NEURON_ISA_TPB_D4_IOTA_STRUCT *op /* a0->...->a3 */)
{
    /* descriptor reached via l32i a3,a0,124; out_dtype @ offset 33 */
    if (dtype_is_floating(op->out_dtype))   /* tested vs FP32 const 0xA (movi a5,10) */
        iota_body_true(op);                 /* const16 0x5f40 ; callx8  — fp output  */
    else
        iota_body_false(op);                /* const16 0x6100 ; callx8  — int output */
}
```

### 3.2 The ramp body (`SUNDA iota_kernel<true> @0x01005f40`, byte-OBSERVED)

In binary-resync mode (`--adjust-vma=0x01000000`), the `<true>` body's
op census is exactly: **6× `ivp_movva32`, 7× `ivp_addn_2x32`, 1× `ivp_muln_2x32`, 1×
`ivp_packln_2x96`, 3× `ivp_floatn_2x32`, 1× `ivp_ufloat16nx16t`**. The representative
bundle trace:

```text
1005f6e: { beqz.w15 a5,… ; ivp_movva32 v31, a6 }                 ; BROADCAST a6 (=base/start) -> v31
1005faa: { mov.a a6,a3   ; ivp_movva32 v5,  a13 }                ; broadcast a13 (a step)   -> v5
1005fb2: { sext a15,a11,15 ; ivp_movva32 v1, a8 }                ; broadcast a8 (a step/stride) -> v1
1005fba: { ivp_movva32 v6, a14 }                                 ; broadcast a14 (a step)   -> v6
1005fc2: { ivp_movva32 v0, a12 ; ivp_addn_2x32 v30, v1, v0 }     ; RAMP add (step accumulate)
1005fd2: { ivp_muln_2x32 wv0, v30, v2 }                          ; STEP MULTIPLY (ramp * step factor)
1005fda: { ivp_packln_2x96 v2, wv0 }                             ; pack the wide product -> 32b lanes
1005fe2: { ivp_addn_2x32 v8, v2, v31 }                           ; + base (v31) -> base + index*step
1006015: { ivp_addn_2x32 v9, v9, v6 }                            ; += per-dim/per-channel step (v6)
1006040: { ivp_addn_2x32 v1,v1,v0 ; ivp_floatn_2x32 v2,v1,0 }    ; ramp + int32->FP32 CONVERT (<true>)
1006078: { ivp_floatn_2x32 v3, v1, 0 }                           ; (fp output only)
1006080: { loopnez.w15 a8,… ; ivp_addn_2x32 v2,v2,v0 ; ivp_floatn_2x32 v1,v2,0 } ; MULTI-TILE LOOP
10060c0: { brdec.p a7,0x1006030 ; ivp_addn_2x32 v10,v10,v5 }     ; per-channel ramp continuation
10060f8: retw.n
```

The ramp is constructed as:

1. **Broadcast** the scalar `base` into every lane (`ivp_movva32 v31, a6`) and broadcast each
   per-dim/per-channel step into its own vector (`ivp_movva32 v1/v5/v6`). This is the
   `AR→vec` 32→32 bridge `movva32` (opcode `F0_S1_Ld 0x00602007`) from the
   [vec-move batch](../../isa/ref/b09-vec-mov.md).
2. **Accumulate** the lane index into the ramp with `ivp_addn_2x32` — one add per dimension /
   per tile — building the running `index` value across lanes.
3. **Scale** by the step factor with `ivp_muln_2x32` (a 2×32-bit wide multiply, result in a
   `wv` wide accumulator), then **pack** the wide product back to 32-bit lanes with
   `ivp_packln_2x96`.
4. **Add the base** (`ivp_addn_2x32 v8, v2, v31`) → `base + index*step`.
5. **Add the per-channel offset** (`channel_multiplier`, riding as one of the broadcast step
   vectors `v5`/`v6`) in the per-channel continuation
   (`ivp_addn_2x32 v10,v10,v5`). `[HIGH field; INFERRED v5/v6 binding]`
6. **(fp only)** convert int32→fp32 with `ivp_floatn_2x32` (3 sites) or uint→fp16 with
   `ivp_ufloat16nx16t`.
7. **Multi-tile loop**: `loopnez.w15` + `brdec.p` extend the ramp past one vector width —
   each tile adds a full-width step offset (`ivp_addn_2x32 v2,v2,v0`) so element *k* of tile
   *t* is `base + (t*W + lane)*step`. `[HIGH loop bundles; MED loop-trip reg]`

```c
/* Recovered algorithm of iota_kernel<>/iota_impl<> (SUNDA bodies are the authority).
 * W = SIMD lane width; FP = template <true> (compile-time). Reads ONLY the operand. */
void iota_impl(const NEURON_ISA_TPB_D4_IOTA_STRUCT *op, bool FP /* template <true>/<false> */)
{
    const NEURON_ISA_TPB_DATA4D *src = &op->src_data_pattern;
    /* broadcast scalars into lanes (ivp_movva32) */
    vec base = movva32(src->base);                 /* v31 */
    vec lane = lane_index_ramp();                  /* 0,1,2,..,W-1  (the in-register 0..W-1) */

    for (uint32_t c = 0; c < op->num_active_channels; ++c) {        /* per partition lane */
        int32_t chan_off = (int32_t)c * op->channel_multiplier;     /* per-channel additive stride */
        vec cbase = addn_2x32(base, movva32(chan_off));             /* each channel its own base */

        for (int d = 0; d < 4; ++d) {                               /* per active dimension/tile */
            for (uint16_t t = 0; t * W < src->num_elem[d]; ++t) {   /* multi-tile loopnez/brdec */
                vec idx  = addn_2x32(lane, movva32(t * W));         /* index within the dim       */
                wvec wp  = muln_2x32(idx, movva32(src->step_elem[d]));/* index * step (wide)       */
                vec  ramp = packln_2x96(wp);                        /* pack wide -> 32b lanes      */
                vec  out  = addn_2x32(ramp, cbase);                 /* base + chan + index*step    */
                if (FP) out = floatn_2x32(out, /*scale*/0);         /* int32 -> fp32 (<true> only) */
                write_tensor4d(&op->dst_mem_pattern, out);          /* SBUF or PSUM, op->out_dtype */
            }
        }
    }
}
```

> **NOTE — the in-register `0..W-1` lane ramp.** The per-lane base index (`lane` above) is
> the per-element position within the vector; the body builds it once and reuses it across
> tiles. This is the same lane-id source the gather/sort kernels consume — the standalone
> Iota op just exposes it as the output. `[INFERRED — lane-id reg not byte-pinned]`

---

## 4. The `<true>` vs `<false>` template split (fp vs int) — the byte proof

The two template instantiations differ on **one compile-time axis only: the output dtype
class**. This is decisively determined from the bodies, not inferred from the mangling.

| | `<true>` (`ILb1E`) | `<false>` (`ILb0E`) |
|---|---|---|
| meaning | **floating-point** output | **integer** output |
| SUNDA body VMA | `0x01005f40` | `0x01006100` |
| CAYMAN+ body VMA | `0x01000100` | `0x010002c0` |
| `.xt.prop` record size | `0x108` (larger — convert tail) | `0x0e4` (smaller) |
| `ivp_floatn_2x32` count | **3** | **0** |
| extra fp ops | `ivp_ufloat16nx16t` (uint→fp16) | — |
| int-narrow op | — | `ivp_bminunx16` (lane narrowing) |
| selected when `out_dtype` is | FP32 / FP32R / FP16 / BF16 | INT8/16/32, UINT8/16/32 |

The proof is threefold:

* **The float-convert op count is 3 vs 0.** In the SUNDA bodies,
  `iota_kernel<true>@0x5f40` has **3** `ivp_floatn_2x32` (plus one `ivp_ufloat16nx16t`);
  `iota_kernel<false>@0x6100` has **0**. The `<false>` body shares the *identical* ramp core
  (6× `movva32`, `muln_2x32`, `packln_2x96`) but never converts. `[HIGH/OBSERVED]`
* **The `.xt.prop` record-size delta.** `<true>` = `0x108` > `<false>` = `0x0e4`, byte-read
  from `readelf -SW` on every carve — consistent with the extra fp-convert tail.
* **The trampoline FP32 discriminator.** The trampoline loads `0xA` (FP32) as the dtype
  constant (`movi a5,10`) and branches on it (§3.1).

> **CORRECTION — `<true>`/`<false>` is the fp/int CLASS, not dimensionality or direction.**
> The anchor survey wrote `iota_kernel<0>`/`<1>`; the mangling is `ILb0E`/`ILb1E` = the
> *bool* template `false`/`true`. `<1>`=`<true>`=fp, `<0>`=`<false>`=int. It is **not**
> 1-D-vs-N-D (that is the runtime `DATA4D` `num_elem`/`step_elem`), **not** ascending-vs-
> descending (that is the *sign* of `step_elem`), and **not** a base/offset variant (that is
> `base`/`channel_multiplier`). The only compile-time axis is the output dtype class — the
> 3-vs-0 `ivp_floatn_2x32` count is the byte-proof.

---

## 5. The dtype matrix

`out_dtype` is validated by `is_valid_dtype(out_dtype, AllowFP32R::True)` — i.e. *any* valid
NEURON dtype, **including FP32R (`0xB`)**. The template body is selected by the fp-vs-int
*class* of `out_dtype` (the `DTYPE` enum is `common.h:704`).

| `out_dtype` | hex | class | template | firmware leg | tag |
|---|---|---|---|---|---|
| FP32 | `0xA` | fp | `<true>` | `ivp_floatn_2x32` int32→fp32 | `[HIGH/OBS]` (trampoline const) |
| FP32R | `0xB` | fp | `<true>` | fp convert; FP32R explicitly allowed | `[HIGH/OBS]` (`AllowFP32R=True`) |
| FP16 | `0x7` | fp | `<true>` | `ivp_ufloat16nx16t` int→fp16 | `[HIGH/OBS]` (op present) |
| BFLOAT16 | `0x6` | fp | `<true>` | fp16/bf16 convert path | `[MED]` (via the fp leg) |
| INT32 | `0x8` | int | `<false>` | raw int32 ramp (no convert) | `[HIGH/OBS]` (0× floatn) |
| UINT32 | `0x9` | int | `<false>` | raw uint32 ramp | `[HIGH/OBS]` |
| INT16 \| UINT16 | `0x4` \| `0x5` | int | `<false>` | int ramp → `packln` to 16b lane | `[MED]` (lane-pack) |
| INT8 \| UINT8 | `0x2` \| `0x3` | int | `<false>` | int ramp → `packln` to 8b lane | `[MED]` |

`INVALID(0x0)` and any dtype failing `is_valid_dtype` are gate-rejected at decode. The
64-bit `INT64`/`UINT64` (`0xC`/`0x1`) cases are not in the observed `packln` lane path
(Iota's dst is a `TENSOR4D` SBUF/PSUM lane vector) and are `[LOW / not claimed]`.

> **NOTE — fp8 dtypes (`FP8_EXP3/4/5` = `0xD/0xE/0xF`).** These are valid `DTYPE` enum
> members and would route through the `<true>` (fp) leg, but no fp8-specific convert was
> byte-pinned in the recovered bodies; treat as `[LOW / INFERRED]`. The bf16/narrow-int
> lane-width selection is the standard GPSIMD `packln`/`floatn` model — the ops are
> OBSERVED, the per-dtype lane-width *branch* is INFERRED. `[MED]`

---

## 6. The load / dispatch path (end to end)

```text
(1) HOST: an Iota instruction (opcode 0x7e, D4_IOTA struct) is decode-validated by
    dbg_is_valid_iota — header/events/reserved-zero/out_dtype valid/channels 1..128/
    dst writable in SBUF|PSUM/same_element_count.
(2) The POOL engine's kernel_info_table linear-scans its packed (spec<<16 | opcode<<24)
    key for opcode 0x7e -> the iota TRAMPOLINE funcVA.
       record = { u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA }
       SUNDA   table @0x02000760 (18 entries): idx8 { spec 0, 0x7e -> 0x01005e80 }
       CAYMAN_0 @0x02000380 (17): idx0 { spec 0, 0x7e -> 0x01000080 }
       CAYMAN_3 @0x020008c8 ( 9): idx0 { spec 0, 0x7e -> 0x01000080 }  (codec image too)
       MARIANA(_PLUS) @0x02000380 (17): idx0 { spec 0, 0x7e -> 0x01000080 }
    ALL spec=0 (no 0xf0 ExtendedInst escape for iota).
(3) The trampoline (entry a1,32) reads the descriptor, tests out_dtype vs the FP32
    constant (0xA), and const16+callx8s the matching body:
       <true>  (fp output, +int->fp32 convert)  OR  <false> (int output).
(4) The body broadcasts base+step into lanes (ivp_movva32), accumulates the ramp
    (ivp_addn_2x32) with the step factor (ivp_muln_2x32), adds the per-channel
    channel_multiplier offset, packs to the out_dtype lane width (ivp_packln_2x96),
    (fp: ivp_floatn_2x32 convert), writes dst_mem_pattern, and a multi-tile loop
    (loopnez/brdec) extends sequences past one vector width.
```

The trampoline funcVA (`0x...e80` / `0x...080`) is **distinct** from the two body
func-starts (`0x...5f40`/`0x...6100` on SUNDA, `0x...0100`/`0x...02c0` on CAYMAN+) — the
dispatch is *trampoline → dtype-branch → body*, one indirection deep. The
[decode-pool dispatch hub](./decode-pool.md) is the generic version of this
kernel_info_table scan.

---

## 7. Per-gen presence — UNIVERSAL (the SUNDA reconciliation)

| GEN | opcode 0x7e | `d4_iota.h` | struct (compile) | `.xt.prop` symbol | firmware body |
|---|---|---|---|---|---|
| SUNDA (v2) | present | present (NC-v2) | sz=64 identical | `iota_kernel<true/false>` (`_Z11iota_kernel…`) | PRESENT (idx8 → `0x5e80` → `0x5f40`/`0x6100`; decodes CLEAN) |
| CAYMAN (v3) | present | present | sz=64 identical | `iota_impl<true/false>` (`_Z9iota_impl…`) | PRESENT (idx0 → `0x0080` → `0x0100`/`0x02c0`; MAIN + CODEC images) |
| MARIANA (v4) | present | present | sz=64 identical | `iota_impl<true/false>` | PRESENT (idx0; byte-near-id to CAYMAN, 49/448) |
| MARIANA_PLUS | present | (uses mariana hdr) | sz=64 identical | `iota_impl<true/false>` | PRESENT (idx0; == MARIANA, byte-identical carve) |
| MAVERICK (v5) | present | present (NC-v5) | sz=64 identical | (carve stripped — n/a) | header-grounded; body NOT byte-confirmed (§8) |

> **CORRECTION — Iota is NOT a SUNDA-unique kernel.** An earlier survey flagged
> `iota_kernel<true>/<false>` as a "SUNDA-new kernel … not in CAYMAN", clustering it with
> the genuinely SUNDA-only gather/embedding_update POOL kernels. **That framing is wrong.**
> In fact: (a) the `iota_impl<true>`/`<false>` mangled symbols (`_Z9iota_implILb1EEvv`,
> `_Z9iota_implILb0EEvv`) are byte-present in `libnrtucode_internal.so` (9 raw hits) and in
> the `.xt.prop` sections of the CAYMAN/MARIANA/MARIANA_PLUS carves; (b) the
> `D4_IOTA` struct compile-verifies `sz=64` with identical offsets and `OPCODE_IOTA=0x7e` in
> **all four** arch-isa dirs (sunda/cayman/mariana/maverick). The **only** real cross-gen
> difference is the C++ symbol **name**: SUNDA calls the function `iota_kernel`
> (`_Z11iota_kernelILbXEEvv`), CAYMAN/MARIANA/MARIANA_PLUS call it `iota_impl`
> (`_Z9iota_implILbXEEvv`) — same template-on-bool, same zero args, same `.xt.prop` record
> sizes (`0x108`/`<true>`, `0x0e4`/`<false>`), same trampoline shape, same opcode/struct.
> This is a build/source-naming delta, **not** a functional difference. Iota is the
> oldest/most-universal POOL index op, present SUNDA(v2)..MAVERICK(v5). Do **not** state
> SUNDA-only. `[HIGH/OBSERVED]`

> **NOTE — distinguish from the truly SUNDA-only POOL kernels.** The
> gather/indirect_copy/dma_memcopy_indirect/embedding_update *bodies* **are** SUNDA-only as
> `kernel_info_table` kernels (CAYMAN serves those via the DGE descriptor layer). Iota is
> **not** in that set — it is universal. Only Iota's per-gen status is corrected here; the
> SUNDA-only cluster stands.

---

## 8. FLIX-desync / honesty flags

* The **SUNDA bodies** (`iota_kernel<true>@0x5f40`, `<false>@0x6100`) + the trampoline
  `@0x5e80` decode cleanly enough to read the ramp primitives — the SUNDA image is the
  "cleanest" decoder. The `.w15` branch forms
  (`bgeui.w15`/`beqz.w15`/`loopnez.w15`/`brdec.p`) are standard FLIX-VLIW + literal-pool
  desync artifacts: the IVP slots *within* each bundle are real (the §3 census reproduces),
  but the surrounding control-flow branch *targets* in a linear sweep are not fully
  trustworthy and are reported structurally. The two `const16` body-build sites + the
  `callx8` + the float-op presence/count **are** byte-clean. `[flagged]`
* The **CAYMAN/MARIANA/MARIANA_PLUS** iota bodies are *more* desynced under a stock linear
  sweep (`0x0100..0x02c0` yields raw bytes only, 0 decoded IVP
  ops). They are pinned by: the iota `.xt.prop` func-starts (`0x100`/`0x2c0`, byte-read from
  the records — flags `0x2804` = func-start), the identical trampoline, the byte-near-identity
  to each other (CAYMAN vs MARIANA: 49/448 bytes differ in `<true>`, 33/312 in `<false>` —
  micro-schedule/literal-VA deltas only), and the compile-verified struct. The CAYMAN
  per-bundle IVP census is **not** independently re-listed; the SUNDA body is the algorithm
  authority. `[MED CAYMAN IVP slots; HIGH identity]`
* `channel_multiplier` "= per-channel additive stride" and the register→struct-field binding
  (which `a`-reg holds base vs step vs `channel_multiplier`) are **INFERRED-HIGH** from the
  field names + the broadcast/`addn` pattern, not a fully-resynced data-flow trace. `[flagged]`
* **MAVERICK firmware body NOT byte-confirmed** — the package's MAVERICK EXTISA carves are
  stripped (no `.xt.prop` names). MAVERICK iota is **header-HIGH** (opcode `0x7e` + struct
  compile-verified) and rests on the cross-gen invariance; the firmware *interior* is
  INFERRED. `[flagged — v5 header-OBSERVED only]`
* The per-narrow-dtype (int8/16, uint) lane-pack arm of `<false>` and the bf16 path of
  `<true>` are MED (the `packln`/`floatn`/`bminunx16` ops are OBSERVED; the per-dtype
  lane-width branch is INFERRED from the standard GPSIMD pack model). `[flagged]`

---

## 9. Relationship to the index-consuming kernels

Iota is the standalone, first-class form (opcode `0x7e`) of the position/index ramp that
several POOL kernels inline:

* **[Sort](./sort.md)** — the index/position payload (`dst[X=1,Y=k]` = original indices)
  originates as an iota (positions `0..N-1`, `+ index_offset`) carried through the
  compare-exchange sorting network.
* **[NonzeroWithCount](./nonzero-with-count.md)** — the emitted nonzero *positions* are an
  iota-derived flat-position ramp, compacted by the predicate.
* **gather / indirect_copy ([indirection-gather](./indirection-gather.md))** — the gather
  index iota (the lane positions) feeds the address generation; Iota's
  `addn_2x32`/`muln_2x32` ramp **is** the address/index ramp those kernels' 32-bit index
  machinery extends.

The shared ramp primitive — `movva32` broadcast + `addn_2x32` accumulate + `muln_2x32` step
— is the GPSIMD index-generation idiom; there is **no** dedicated `ivp_seqn` opcode (the
ramp is always composed). See the [Vector Move / regfile bridge batch](../../isa/ref/b09-vec-mov.md)
for `movva32` (opcode `F0_S1_Ld 0x00602007`, 1072 hits).

The **same base+stride accumulate** reappears in the DMA address-generation math (the
descriptor `start_addr + Σ step` walk); see [the DMA / descriptor model](../../dma/descriptor-model.md)
*(planned)* — Iota's ramp and the DMA address walk are the same arithmetic, one targeting
lane values and the other targeting memory addresses.

> **NOTE — the iota-predicate sibling.** [AffineSelect](./affineselect.md) *(planned, #740)*
> reuses precisely this ramp to build a comparison **mask**: it generates the same affine
> `base + index*step` ramp and compares it against a scalar to produce a per-lane boolean
> select, rather than emitting the ramp as the output. Iota emits the ramp; AffineSelect
> *thresholds* it. `[CARRIED — pending #740.]`

---

## 10. Reproduction

```sh
export XTENSA_SYSTEM=…/gpsimd_tools_tgz/tools/XtensaTools/config XTENSA_CORE=ncore2gp
# Tools: xtensa-elf-{objdump,readelf,objcopy,c++filt} (Binutils 2.34, Xtensa 14.09); host gcc.

# Carves (Python dd; identity .rodata map; .text VMA 0x01000000 == file off 0x100):
#   SUNDA_0      = libnrtucode_extisa.so   [0x921660 : +0xd308]   (runtime-lib)
#   CAYMAN_0     = libnrtucode_internal.so [0x2ef7e0 : +0xa260]   (customop-lib)
#   CAYMAN_3     = libnrtucode_internal.so [0x2fbf00 : +0x6974]
#   MARIANA_0    = libnrtucode_internal.so [0x5893c0 : +0xa280]
#   MARIANA_PLUS = libnrtucode_internal.so [0x855240 : +0xa280]

# iota .xt.prop symbols + sizes:
xtensa-elf-readelf -SW SUNDA_0.so   | rg iota   # _Z11iota_kernelILb1EEvv 0x108 / ILb0E 0x0e4
xtensa-elf-readelf -SW CAYMAN_0.so  | rg iota   # _Z9iota_implILb1EEvv    0x108 / ILb0E 0x0e4
xtensa-elf-c++filt _Z9iota_implILb1EEvv         # -> void iota_impl<true>()

# func-start VMAs (first LE32 of each .xt.prop record; flags 0x2804 = func-start):
xtensa-elf-objdump -s -j '.xt.prop._Z9iota_implILb1EEvv' CAYMAN_0.so   # 0x01000100  (<true>)
xtensa-elf-objdump -s -j '.xt.prop._Z9iota_implILb0EEvv' CAYMAN_0.so   # 0x010002c0  (<false>)

# THE fp/int split — byte-prove 3-vs-0 (SUNDA decodes clean; binary-resync mode):
xtensa-elf-objcopy -O binary --only-section=.text SUNDA_0.so sunda_text.bin
xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x01000000 \
    --start-address=0x01005f40 --stop-address=0x01006100 sunda_text.bin | rg -c ivp_floatn_2x32  # 3
xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x01000000 \
    --start-address=0x01006100 --stop-address=0x010061e4 sunda_text.bin | rg -c ivp_floatn_2x32  # 0

# trampoline (entry; l32i a3,a0,124; bgeui dtype branch; const16 0x5f40/0x6100; movi a5,10; callx8):
xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x01000000 \
    --start-address=0x01005e80 --stop-address=0x01005f10 sunda_text.bin

# struct compile-verify (all 4 gens IDENTICAL: sz=64 src@12 out_dtype@33 nac@34 chmul@36 dst@44 OPC=0x7e):
gcc -I…/neuron_<gen>_arch_isa/tpb v_<gen>.c -o v_<gen> && ./v_<gen>
```

---

## 11. Confidence ledger

**HIGH / OBSERVED** (direct disasm / byte / symtab / header / compile):

* Opcode `IOTA=0x7e`; struct `D4_IOTA` (64 B) compile-verified all 4 gens (`src@12`,
  `out_dtype@33`, `num_active_channels@34`, `channel_multiplier@36`, `dst@44`);
  `instruction_mapping.json` binds `D4_IOTA → OPCODE_IOTA`; `debug_assert.h:219` binds
  `OPCODE_IOTA → dbg_is_valid_iota`. `DATA4D = {int32 base; int16 step_elem[4]; uint16
  num_elem[4]}`.
* The iota `.xt.prop` symbols + func-start VMAs, all gens: SUNDA `iota_kernel<true>@0x5f40` /
  `<false>@0x6100`; CAYMAN/MARIANA/MARIANA_PLUS `iota_impl<true>@0x100` / `<false>@0x2c0`.
  Same `.xt.prop` record sizes (`0x108`/`0x0e4`) across all gens.
* `kernel_info_table` dispatch byte-exact: SUNDA idx8 `{0x7e→0x05e80}`; CAYMAN/MARIANA/
  MARIANA_PLUS idx0 `{0x7e→0x00080}`. SUNDA JSON `"pool_iota"` opcode 126.
* The trampoline (`entry a1,32`; `l32i a3,a0,124`; `bgeui` dtype branch; `const16`
  `<true>`/`<false>`; `movi a5,10` FP32 const; `callx8`) byte-decoded for SUNDA.
* The ramp algorithm (SUNDA `<true>` census): 6× `ivp_movva32`, 7× `ivp_addn_2x32`, 1×
  `ivp_muln_2x32`, 1× `ivp_packln_2x96` + the multi-tile `loopnez`/`brdec` continuation.
* The `<true>`=fp / `<false>`=int split: **3× `ivp_floatn_2x32` in `<true>` vs 0 in
  `<false>`** + the `.xt.prop` size diff + the trampoline FP32 const.
* Per-gen: UNIVERSAL (SUNDA..MARIANA_PLUS OBSERVED; MAVERICK header-compile-verified). The
  earlier "SUNDA-unique" framing CORRECTED. `iota_impl` mangled symbols byte-present in
  `libnrtucode_internal.so`.

**MED / INFERRED:**

* The register→struct-field binding (which `a`-reg = base/step/`channel_multiplier`) + the
  precise `base + Σ index*step + channel*channel_multiplier` arithmetic — INFERRED-HIGH from
  the `DATA4D` fields + the broadcast/`addn`/`muln` firmware chain.
* The per-narrow-dtype lane-pack arm + the bf16 fp path — INFERRED from the standard
  `packln`/`floatn`/`bminunx16` model (ops OBSERVED; per-dtype branch not byte-pinned).
* The CAYMAN/MARIANA body IVP slots (desync; pinned by name+opcode+struct+trampoline, not
  independently re-disassembled).

**LOW / NOT CLAIMED:**

* The MAVERICK firmware body bytes (stripped carve; header-grounded only).
* The exact multi-dim ordering (which `DATA4D` dim is inner/outer) at the byte level + the
  precise loop-trip register (desync).
* Any host/NKI API surface for Iota (the package surfaces only the ISA header + the firmware
  kernel; no `at::Tensor`/NKI iota op traced).
* 64-bit (`INT64`/`UINT64`) and fp8 (`FP8_EXP3/4/5`) emit paths.
