# NCFW CUST3 DMA Doorbell Thunks

This page decodes, byte-exact, the **NCFW "CUST3" DMA doorbell-thunk machinery** —
the small fan of management-core functions that ring a DMA engine by writing its
per-queue **tail-pointer-increment** doorbell, fanned through the per-engine
**APB-broadcast** window. It is the *who-pulls-the-trigger* companion to the
[DMA reprogram + APB broadcast](dma-reprogram-apb-bcast.md) page (which decodes the
window the doorbell writes land in) and to the
[al_udma HW engine](../../dma/udma-hw-engine.md) /
[descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md) pages
(the per-queue CSR map and the `+0x1038` tail-pointer-increment register this page
writes). The thunks are reached from the [NCFW main dispatch loop](main-dispatch-loop.md)
via the FW_IO command / exception-dispatch path.

The single most important distinction this page establishes is the **two-plane
doorbell split**:

* **DATA-plane doorbell** — the uDMA per-queue **`TDRTP_INC`/`RDRTP_INC`** at
  `queue_base + 0x38` (absolute `+0x1038` at queue 0), fanned via the APB-broadcast
  window. **Value written = N descriptors.** This is what the CUST3 thunks ring.
* **CONTROL-plane doorbell** — the TOP_SP `LOCAL_REG` **value-`1`** host-trigger at
  `0x615a0` (cayman/mariana; `0x60848` sunda), a *separate* register block reached
  through the `kaena_khal` HAL vtable. The CUST3 DMA thunks do **not** touch it.
  See [TOP_SP lowering](../ops/top-sp-lowering.md).

> **GOTCHA — two Xtensa cores, do not cross the wires.** The thunk *bodies* run on
> the **scalar Xtensa-LX** management core (NCFW), not the Vision-Q7 "Cairo" FLIX
> datapath. The only registered Xtensa config is `ncore2gp` (the Q7 NX core), which
> greedily reads `op0=0xe → 16-byte` / `op0=0xf → 8-byte` Vision bundles — **wrong**
> for the scalar LX core. The correct LX length rule is `op0` nibble `e`/`f` ⇒
> 3-byte instruction, else 2-byte, resyncing at `retw.n` (`1d f0`). A Vision-bundle
> decode of NCFW code is an artifact; do not trust it. The *one* intra-NCFW field
> recoverable without the LX `.tie` is the standard 3-byte Xtensa `call8` word
> (`op0=5`, `n=2`, `target = ((callPC & ~3) + 4 + (imm18<<2))`), so **every thunk
> call target on this page was decoded with that scalar rule**, not from a FLIX
> emit. The doorbell *mechanics* (address arithmetic + value = N) are recovered from
> the host `libnrt.so` DWARF/getters, which are fully x86-decodable. See the
> [scalar-LX core page](../../uarch/ncfw-lx-core.md) and
> [LX-ISA naming / arch_id synthesis](lx-isa-naming-archid-synthesis.md).

**Provenance & confidence.** Every fact below is read **this session** from two
shipped host libraries with stock binutils (`readelf -SW`/`-n`, `nm`,
`objdump -d -M intel`, `sha256sum`, `strings -t x`) plus a Python `struct`/`call8`
byte reader:

* **`libncfw.so`** (NCFW image accessor + `ctx_log` JSON pretty-printer; carries
  the firmware blobs as `.rodata`) — sha256 `598920d743…`, BuildID `a98f8e1c…`,
  `.rodata` VMA == file-offset `0x65000` *(re-verified)*.
* **`libnrt.so.2.31.24.0`** (host RunTime — the **encoder**; the authoritative
  struct/getter source) — BuildID `8bb57aba…`, `.text`/`.rodata` VMA == file-offset
  *(re-verified)*.
* **The carved NCFW blobs** (MD5 re-verified this session): `v4_iram` @`0x83260`
  `1f3d74d1…`, `v3_iram` @`0x79860` `d4d5b0d8…`, `v4_plus_iram` @`0x8ccc0`
  `eec31c54…`, `v4_dram == v4_plus_dram` @`0x7e440`/`0x87ea0` `7ff55158…`,
  `v3_dram` @`0x74a40` `c0a240cf…`.

> **GOTCHA — the `.data` offset delta does NOT apply here.** In `libncfw.so`/
> `libnrt.so` the `.text`/`.rodata` sections are VMA == file-offset (confirmed
> per-section with `readelf -SW`); the carved firmware blobs are read straight from
> `.rodata`. The ncore2gp config-DLL `.data` delta (`0x200000`) and the `libtpu`
> `0x400000` delta are **other binaries** — they do not apply to any address on this
> page. Likewise `libncfw.so` fn-ptr tables are plain **C arrays** (slot N = symbol
> + 8·N); the C++ `_ZTV+0x10` vtable rule is irrelevant here.

Tags: **HIGH/MED/LOW** × **OBSERVED** (bytes/disasm/DWARF read this session) /
**INFERRED** (deduced from structure/siblings) / **CARRIED** (from a cited sibling
report). `v2`/SUNDA is the addressing outlier; `v5`/MAVERICK NCFW is **FILE-ABSENT**
(every v5 claim is INFERRED/ABSENT).

---

## 1. What "CUST3" is

> **NOTE — "CUST3" is a disassembler mnemonic, not a vendor name.** A corpus census
> this session (`rg -i 'CUST3|cust3|custom3'` over the shipped `extracted/` tree and
> `strings` over the two binaries) returns **zero** occurrences of any
> `CUST3`/`cust3`/`custom3` opcode name. The one hit in the whole corpus is
> *unrelated*: `custom32` in the bundled LLVM `MIRYamlMapping.h` /
> `MachineJumpTableInfo.h` (a `MachineJumpTableInfo::EntryKind`, not an Xtensa op).
> "CUST3" is the **stock-xtensa disassembler mnemonic** for the *third*
> designer-/TIE-defined (CUSTomized-core) opcode class — the LX core reserves an
> opcode space for designer instructions, and the disassembler labels the classes
> `CUST0..CUSTn`. The DMA doorbell op falls in the CUST3 class. *(census OBSERVED
> HIGH; "third CUSTOM-class mnemonic" INFERRED-STRONG from the mnemonic family + the
> absence of the name anywhere in the shipped corpus.)*

**The concrete op word — OBSERVED HIGH.** The NCFW DMA doorbell instruction is a
single 3-byte TIE word `0x0d0ca0` — LE bytes **`a0 0c 0d`** on Mariana, **`80 0c 0d`**
on Cayman. Byte-census of the carved blobs (my own `xxd`/grep, this session):

| gen | codename / arch_id | CUST3 op bytes | word | t-field nibble | markers |
|-----|--------------------|----------------|------|----------------|---------|
| v3 | CAYMAN / NC-v3 (0x0c) | `80 0c 0d` | `0x0d0c80` | 8 | **19** |
| v4 | MARIANA / NC-v4 (0x14) | `a0 0c 0d` | `0x0d0ca0` | a | **19** |
| v4+ | MARIANA_PLUS / NC-v4+ (0x1c) | `a0 0c 0d` | `0x0d0ca0` | a | **19** |
| v2 | SUNDA / NC-v2 (0x05) | *(no `XX 0c 0d`)* | — | — | **0** |

The 19-marker count is generation-stable for v3/v4/v4+ with no cross-contamination
(`a0 0c 0d` is **0** in v3; `80 0c 0d` is **0** in v4/v4+). In a thunk body the op
is preceded by `7a` (e.g. family-B carries `7a a0 0c 0d`). The differing nibble
between gens (`8` vs `a`) is the op's **t-field** — the per-silicon doorbell-
addressing **MODE** of the op.

> **GOTCHA — v2/SUNDA carries no `XX 0c 0d` CUST3 word.** A byte-census of the
> carved `v2_iram` finds **zero** `00 0c 0d`/`20`/`40`/`60`/`80`/`a0`/`c0`/`e0 0c 0d`
> words. SUNDA predates this doorbell-op encoding (older `0x0fff` fabric, §8); the
> v2 doorbell path is **not** the CUST3-word machinery and any v2 claim here is
> INFERRED/outlier. v5/MAVERICK NCFW is **FILE-ABSENT** entirely.

**Scope.** CUST3 is the NCFW management core's TIE-accelerated **DMA doorbell-ring /
engine-command** instruction. The thunk functions (§2) wrap it: a thunk loads an
engine/channel **selector** immediate + (for family D) an off-image descriptor
pointer, executes the CUST3 op, and tail-calls one of two **direction-coded
helpers** (§3) that drive the engine-command core. The doorbell it ultimately rings
is the uDMA per-queue tail-pointer-increment (§4), fanned via the APB-broadcast
window. The **symbolic** TIE mnemonic and *which aperture bits the op sets* are
`.tie`-bound and do **not** ship — that is the standing residual (§8); the op word,
direction nibble, selector immediates, target region, and value=N are all proven.

---

## 2. The doorbell thunk array (`v4_iram` `0x3680..0x3a5c`)

The thunk array is 27 functions, each `entry a1,32` (`36 41 00`) → `retw.n`
(`1d f0`); **14** carry the CUST3 `a0 0c 0d` op. They split into four sub-families.
All bytes below were **dumped this session** from `v4_iram` (`1f3d74d1…`); all
`call8` targets were **decoded with the scalar LX rule**, not a FLIX emit.

### 2.1 Family A — SETUP thunks (no CUST3)

`0x3688` / `0x36b0` / `0x36d8` — length 38, **no** CUST3 op; each does an
`l32r`-loaded descriptor + `call8 0x3648` (the setup callee, which itself tail-calls
the engine-command core `0x490c`). These prime engine state *before* the per-channel
rings. *(structure CARRIED; OBSERVED that they carry no CUST3 word.)*

### 2.2 Family B — CUST3 GROUP-selector thunks (`ae bX 0Y`) — OBSERVED HIGH

Selector `ae bX 0Y` at thunk `+0x03`; CUST3 word `a0 0c 0d` at thunk `+0x0b`; the
`call8` word follows at thunk `+0x0e`. Bytes + decoded targets this session:

```
0x3700: 36 41 00 ae b1 01 80 01 8c 01 7a a0 0c 0d  a5 98 ff  → call8 0x3098 (IN)
0x3724: 36 41 00 ae b1 02 80 01 8c 01 7a a0 0c 0d  65 96 ff  → call8 0x3098 (IN)
0x3798: 36 41 00 ae b3 01 …             7a a0 0c 0d  25 8f ff  → call8 0x3098 (IN)
0x37bc: 36 41 00 ae b3 02 …             7a a0 0c 0d  e5 8c ff  → call8 0x3098 (IN)
0x3830: 36 41 00 ae b5 01 …             7a a0 0c 0d  a5 85 ff  → call8 0x3098 (IN)
0x3854: 36 41 00 ae b5 02 …             7a a0 0c 0d  65 83 ff  → call8 0x3098 (IN)
0x3a48: 36 41 00 ae b7 03 …             7a a0 0c 0d  25 64 ff  → call8 0x3098 (IN)  …1d f0
```

* **`X` ∈ {1,3,5,7}** (the `bX` nibble) = engine-pair selector (odd).
* **`Y` ∈ {1,2,3}** (the `0Y` byte) = sub-index; sibling pairs differ by **one byte**
  (`ae b1 01` vs `ae b1 02`, etc.).

All seven `call8` words (`callPC = thunk+0x0e`) decode to **`0x3098`** (the IN
helper) in their first leg.

### 2.3 Family C — CUST3 BANK-selector thunks (`d2 04 ZZ 0Y`) — OBSERVED HIGH

Selector `d2 04 ZZ 0Y` at thunk `+0x0d`. Bytes this session:

```
0x3748: 36 41 00 be c0 00 80 01 0c 45 fa ab 2f  d2 04 40 02  cc 07 ad 02  a5 93 ff  be 10 20 98
0x3770: 36 41 00 …                               d2 04 40 04  …            25 91 ff  …
0x37e0: 36 41 00 …                               d2 04 80 02  …            25 8a ff  …
0x3808: 36 41 00 …                               d2 04 80 04  …            a5 87 ff  …
```

* **`ZZ` ∈ {0x40, 0x80}** = the NC bank selector — `0x40 = NC0`, `0x80 = NC1`. This
  is *exactly* the die/NC `0x80` bit of the reg-addr table (§5): the bank-selector
  thunks address the **same** engine apertures, choosing the NC via the immediate.
* **`Y` ∈ {02, 04}** = the engine field within the bank (a 2-step index).

These carry the `be 10 20 98 41 9f 01 5a … a0 0c 0d` CUST3 tail, then `call8 0x30bc`
(the OUT helper).

### 2.4 Family D — PER-CHANNEL IN/OUT thunks (`0x3948..0x3a28`) — OBSERVED HIGH

Eight **byte-identical** thunks differing only in the channel nibble (`byte+9`) and
the IN/OUT call displacement. Template (32 B):

```
36 41 00  3e 1c 4f 98 67 ff  Xd  5b a0  3e c2 02 a0 1d ac 01  7a a0 a0 22 11 fa a2  [65|a5] [d] ff  1d f0
                            └ byte+9 = channel nibble (high nibble 0/2/4/6)
        └ 3e 1c 4f 98 67 ff = the per-channel signature (8× in v4, 0× in v4+/v3)
                                                              └ call8 word @ thunk+0x1a
```

All 8 `call8` targets **decoded myself** this session (`callPC = thunk+0x1a`,
scalar call8):

| thunk | `byte+9` | channel | call word | target | helper |
|-------|----------|---------|-----------|--------|--------|
| `0x3948` | `0x0d` | ch0 | `65 73 ff` | `0x3098` | **IN** (read) |
| `0x3968` | `0x0d` | ch0 | `a5 73 ff` | `0x30bc` | **OUT** (write) |
| `0x3988` | `0x2d` | ch2 | `65 6f ff` | `0x3098` | IN |
| `0x39a8` | `0x2d` | ch2 | `a5 6f ff` | `0x30bc` | OUT |
| `0x39c8` | `0x4d` | ch4 | `65 6b ff` | `0x3098` | IN |
| `0x39e8` | `0x4d` | ch4 | `a5 6b ff` | `0x30bc` | OUT |
| `0x3a08` | `0x6d` | ch6 | `65 67 ff` | `0x3098` | IN |
| `0x3a28` | `0x6d` | ch6 | `a5 67 ff` | `0x30bc` | OUT |

A textbook per-channel ring-doorbell table indexed by an **even** channel number
(0/2/4/6), each with a read (IN) + write (OUT) variant. The `l32r`-loaded descriptor
pointer is at thunk `+0x10..0x12` (`a0 1d ac 01` — op0-nibble-1 `l32r`, scalar) and
resolves to an **off-image** descriptor (§7).

### 2.5 Thunk selection per engine class — selector → aperture binding

```c
/* Family-D channel thunks are the CLEANEST 1:1 binding (OBSERVED HIGH at the
   selector level; the engine-CLASS label is INFERRED/CARRIED, §5). */
static doorbell_target_t ncfw_select_doorbell(int family, sel_t sel) {
    switch (family) {
    case FAM_D_CHANNEL:                 /* 0x3948..0x3a28 */
        /* even channel index 0/2/4/6 (byte+9 high nibble) →
           one of the 4 per-NC engine apertures (reg-addr idx0..3, §5);
           IN(0x3098 = head-read) / OUT(0x30bc = tail-doorbell) pair each. */
        return engine_aperture_for_channel(sel.channel /* 0,2,4,6 */);

    case FAM_B_GROUP:                   /* ae bX 0Y */
        /* odd engine-pair X∈{1,3,5,7} + sub-index Y; rings the engine's
           EVENT/sema slot directly via the CUST3 operand (no per-channel
           descriptor). All seven call8 → 0x3098 (IN) first leg. */
        return engine_pair_aperture(sel.X, sel.Y);

    case FAM_C_BANK:                    /* d2 04 ZZ 0Y */
        /* NC-bank ZZ (0x40=NC0 / 0x80=NC1 = die bit) + engine field Y;
           same engine apertures, NC chosen by the operand; → call8 0x30bc (OUT). */
        return engine_aperture_for_bank(sel.ZZ /* NC */, sel.Y /* eng */);
    }
}
```

The family-D channel index maps 1:1 to the four per-NC engine apertures; the
group/bank families (B/C) address the **same** apertures but select the NC-bank and
an engine sub-index via the CUST3 operand directly. The engine **CLASS** labels
(PE/ACT/POOL/DVE) per aperture remain the inferential dispatch-slot map
(CARRIED, §5 / §8), **not** proven from the binary.

---

## 3. The two shared direction-coded helpers

Both ring thunks tail-call one of two helpers that differ **only** in a transfer-
direction field. Byte-diff over 0x24 B (dumped + diffed this session):

```
0x3098 (IN/read) : 36 41 00 6e 1f 30 18 7c ff 0A 50 a0 6e 1f 4f f8 9f DF EE 50 ab 6f 12 52 b8 bd bf 05 7a a2 A5 9A 01 1d f0
0x30bc (OUT/write): 36 41 00 6e 1f 30 18 7c ff 2A 50 a0 6e 1f 4f f8 9f 1F EF 50 ab 6f 12 52 b8 bd bf 05 7a a2 65 98 01 1d f0
```

The **only** differing bytes over 0x24 B (re-verified):

| offset | IN (`0x3098`) | OUT (`0x30bc`) | meaning |
|--------|---------------|----------------|---------|
| `+0x09` | `0x0a` | `0x2a` | **transfer-DIRECTION nibble** inside the op (high nibble 0 vs 2) |
| `+0x11` | `df` | `1f` | dependent TIE operand |
| `+0x12` | `ee` | `ef` | dependent TIE operand |
| `+0x1e` | `a5` | `65` | `call8` displacement low byte (same callee, different call PC) |
| `+0x1f` | `9a` | `98` | `call8` displacement |

Both helpers' `call8` word at `+0x1e` decodes to the **same** callee **`0x4a60`**
(decoded this session). The high nibble of `byte+9` (`0` vs `2`) is the DMA
transfer-direction selector inside the op: the IN/read leg is the head-pointer read
(`DRHP`), the OUT/write leg is the tail-pointer doorbell (`TDRTP_INC`/`RDRTP_INC`).
*(the IN/OUT → DRHP/DRTP_INC mapping is INFERRED-STRONG from the helper byte-diff +
the host getter pair, §4; the TIE bit semantics are the residual.)*

**Tail chain (call targets decoded myself).** Both helpers reach the engine-command
core through two more LX functions:

```
0x3098 / 0x30bc
   └─ call8 0x4a60        (entry a1,32; at 0x4a69 the call word `05 00 00` →)
        └─ 0x4a6c         (entry a1,32; body: `20 15 f3` engine-status-read TIE,
                           then `2f 32` TIE; call word `e5 e7 ff` @ 0x4a7f →)
             └─ call8 0x48fc   (the engine-command core)
```

The actual engine MMIO write is the TIE op inside `0x4a6c`/`0x48fc` — undecodable
**symbolically** because the LX `.tie` does not ship; the *target register* and the
*value* are recovered instead from the host encoder (§4).

> **NOTE — caller graph.** The group/bank thunks `0x3700..0x3854` are invoked from
> the FW_IO command / exception-dispatch path (`cmd region 0x1690..0x1c41`), 1–2
> callers each. The family-D ch0/2/4/6 IN/OUT thunks (`0x3948..0x3a28`) have **zero**
> direct callers — they are reached via computed-goto / `callx` from a runtime-
> indexed per-channel dispatch table (consistent with the byte-identical, channel-
> indexed template). The helpers `0x3098`/`0x30bc` have 23–24 callers each — every
> ring thunk. *(structure CARRIED; entry sites re-verified.)*

---

## 4. The write mechanics — value = N, target = uDMA tail-ptr-INC, APB-broadcast fan

The host RunTime resolves the **same** doorbell address + value the CUST3 thunks
ring. Recovered byte-exact this session from `libnrt.so` (the authoritative encoder).

### 4.1 The full doorbell address — OBSERVED HIGH

```c
/* doorbell_addr = the uDMA per-queue tail-pointer-increment register, fanned
   through the per-engine APB-broadcast window. Recovered from libnrt getters. */
uint64_t doorbell_addr =
      cayman_bcast_region_table[engine_field]      /* per-engine APB bcast base */
    + aws_hal_udma_get_m2s_offset()                /* M2S sub-block base, khal vtable +0x578 */
    + ((queue_id + 1) << 12)                       /* per-queue base */
    + 0x38;                                         /* TDRTP_INC / RDRTP_INC reg (the DOORBELL) */

uint32_t value = N;   /* descriptor count to arm: write N ⇒ advance tail by N descriptors */
```

Resolver chain (every getter disassembled this session):

| symbol | addr | does |
|--------|------|------|
| `add_dma_tail_inc_bcast` | `0x273480` | top-level: issue the tail-inc bcast write |
| `get_dma_queue_tail_inc_bcast_offset` | `0x318830` | `= bcast_offset(eng) + (M2S\|S2M)_queue_tail_ptr_inc_offset` |
| `tdrv_arch_get_dma_queue_regs_bcast_offset_cayman` | `0x30c1e0` | `= m2s_offset() + (q+1)<<12 + bcast_region_table[eng]` |
| `aws_hal_udma_get_m2s_offset` | `0x45f1b0` | reads `[khal_vtable + 0x578]` (M2S sub-block) |
| `aws_hal_udma_get_m2s_queue_offset_cayman` | `0x473a90` | `lea eax,[rdi+1]; shl eax,0xc` = `(q+1)<<12` |
| `aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset_cayman` | `0x473bf0` | `0x1038 − queue_offset(q=0)` = **`0x38`** |
| `get_dma_queue_data_tail_inc_bcast_offset` | `0x318870` | the `+0xe0` enhanced-prefetch variant, gated by `tdrv_arch_supports_data_tail_ptr_inc` |
| `get_dma_queue_sw_ctrl_bcast_offset` | `0x3188b0` | the `+0xb0`/`+0x64` re-arm variant |

Disassembly of the cayman address-assembler (`0x30c1e0`, this session):

```asm
30c1ee:  call  aws_hal_udma_get_m2s_offset      ; M2S sub-block base ([khal+0x578])
30c1f9:  call  aws_hal_udma_get_m2s_queue_offset ; (q+1)<<12
30c1fe:  add   rax, rbx
30c201:  mov   edx, [rbp+0x4]                    ; engine_field
30c209:  lea   rcx, [rip+0x6d2d30]               ; 9def40  cayman_bcast_region_table
30c212:  add   rax, [rcx + rdx*8]                ; + bcast_region_table[engine_field]
```

> **GOTCHA — the getter returns the WITHIN-BLOCK `+0x38`, the absolute is `+0x1038`.**
> `aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset_cayman` (`0x473bf0`) computes
> `xor edi,edi; call queue_offset(q=0)` → `0x1000`, then `mov edx,0x1038; sub rdx,rax`
> = **`0x38`**. So the getter hands back `+0x38` (the register offset *within* the
> per-queue block), and the full address adds back `(q+1)<<12` (which is `0x1000` at
> `q=0`). The shared-fact anchor **`+0x1038`** is the `q=0` absolute; both readings
> are consistent — see [descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md)
> and [DMA reprogram + APB broadcast](dma-reprogram-apb-bcast.md), which use the same
> `+0x1038` tail-pointer-increment.

`cayman_get_apb_bcast_m2s_offsets` (`0x25ced0`) walks the `dma_alloc_bitmap` (the
engines participating in the step) and emits **one** tail-inc bcast offset per masked
engine — i.e. one MMIO write to the broadcast window reaches all masked peers (the
APB-broadcast fan-out). Its S2M sibling is `cayman_get_apb_bcast_s2m_offsets`
(`0x25af40`).

### 4.2 The per-queue CSR register block (`+0x1028..+0x10e0`) — OBSERVED HIGH

`queue_base = (q+1) << 12` (`aws_hal_udma_get_m2s_queue_offset_cayman` @`0x473a90`:
`lea eax,[rdi+0x1]; shl eax,0xc; ret`; S2M identical at `0x473aa0`). Each cayman
getter's `mov edx,0x10XX` immediate was read this session:

| register | abs offset (q=0) | M2S getter | S2M getter | meaning |
|----------|------------------|------------|------------|---------|
| `base_ptr_lo` | `+0x1028` | `0x473ab0` | `0x473ad0` | ring base PA [31:0] |
| `base_ptr_hi` | `+0x102c` | `0x473af0` | `0x473b10` | ring base PA [63:32] |
| `len` | `+0x1030` | `0x473b30` | `0x473b50` | ring length (descriptors) |
| `head_ptr` (`DRHP`) | `+0x1034` | `0x473b70` | `0x473b90` | head pointer (engine-advanced) |
| **`tail_ptr_INC`** (`TDRTP_INC`/`RDRTP_INC`) | **`+0x1038`** | `0x473bf0` | `0x473c10` | **the DOORBELL — write N ⇒ arm N descriptors** |
| `tail_ptr` (`DRTP`) | `+0x103c` | `0x473bb0` | `0x473bd0` | tail pointer (RO position) |
| `s2m sw_ctrl` | `+0x1064` | — | `0x473c70` | S2M `rst_tail_ptr` (re-arm) |
| `m2s sw_ctrl` | `+0x10b0` | `0x473c50` | — | M2S `rst_tail_ptr` (re-arm) |
| `data_tail_ptr_INC` | `+0x10e0` | `0x473c30` | — | M2S-only enhanced-prefetch independent tail |

> **GOTCHA — `tail_ptr` and `tail_ptr_INC` are different registers, adjacent.**
> `+0x103c` (`DRTP`) is the read-only *position* of the tail; `+0x1038`
> (`TDRTP_INC`/`RDRTP_INC`) is the **doorbell** you *write N* into to advance it.
> Do not write the doorbell to `+0x103c`. The same TX(M2S)/RX(S2M) `+0x1038`
> tail-increment is documented in the [al_udma HW engine](../../dma/udma-hw-engine.md)
> page — both directions share the within-block `+0x38`.

> **NOTE — `data_tail_ptr_INC` (`+0xe0`) is arch-gated.** `get_dma_queue_data_tail_inc_bcast_offset`
> (`0x318870`) is gated by `tdrv_arch_supports_data_tail_ptr_inc` — the enhanced-
> prefetch independent tail is M2S-only and **absent on SUNDA** (the sunda getter
> set lacks the data-tail-inc pair); cayman/mariana have it.

### 4.3 Value, ordering, fence

The **value written = N = the descriptor count** to arm (write N ⇒ the engine
advances its tail by N descriptors and begins fetching). Ordering: the device-side
completion-semaphore write is `memw`-fenced, and the host marks `ring_send_complete`
/ the next step waits on the completion semaphore (the send/wait protocol; the
`DMA_COMPLETION_MARKER 0xabcdef01` completion descriptor). *(value/ordering OBSERVED
HIGH via the send/wait protocol; the exact runtime `N` is runtime-resident — MED.)*
See [ring protocol / config command](ring-protocol-config-command.md).

---

## 5. The register-address TABLE (`v4_dram +0x010`, 20× u64)

The NCFW firmware's own engine/event base table, dumped byte-exact this session from
`v4_dram` (`7ff55158…` == `v4_plus_dram`), reconciled against the libnrt host tables.

| idx | off | u64 (LE) | role |
|-----|-----|----------|------|
| 0 | `+0x010` | `0x002802700000` | NC0 engine[nibble 2] EVENT/sema base aperture |
| 1 | `+0x018` | `0x003802700000` | NC0 engine[nibble 3] |
| 2 | `+0x020` | `0x006802700000` | NC0 engine[nibble 6] |
| 3 | `+0x028` | `0x007802700000` | NC0 engine[nibble 7] |
| 4 | `+0x030` | `0x802802700000` | NC1 engine[nibble 2] (die bit47) |
| 5 | `+0x038` | `0x803802700000` | NC1 engine[nibble 3] |
| 6 | `+0x040` | `0x806802700000` | NC1 engine[nibble 6] |
| 7 | `+0x048` | `0x807802700000` | NC1 engine[nibble 7] |
| 8 | `+0x050` | `0x001002000000` | NC0 APB-bcast ctl `0x02` |
| 9 | `+0x058` | `0x001003000000` | NC0 ctl `0x03` |
| 10 | `+0x060` | `0x005004000000` | NC0 ctl `0x04` |
| 11 | `+0x068` | `0x005005000000` | NC0 ctl `0x05` |
| 12 | `+0x070` | `0x801002000000` | NC1 ctl `0x02` (die bit47) |
| 13 | `+0x078` | `0x801003000000` | NC1 ctl `0x03` |
| 14 | `+0x080` | `0x805004000000` | NC1 ctl `0x04` |
| 15 | `+0x088` | `0x805005000000` | NC1 ctl `0x05` |
| 16 | `+0x090` | `0x008006800000` | NC0 SDMA D2H host-transfer engine (`APB_IO_0`) |
| 17 | `+0x098` | `0x008006900000` | NC0 SDMA H2D host-transfer engine |
| 18 | `+0x0a0` | `0x808006800000` | NC1 SDMA D2H (die bit47) |
| 19 | `+0x0a8` | `0x808006900000` | NC1 SDMA H2D |

Three row-bands:

* **idx[0..7] — per-(NC × engine) EVENT/sema apertures** (all end `0x...02700000` =
  the per-NC EVENT/sema region, `V4_MMAP_NC_EVENT_OFFSET 0x2700000`). The 4 distinct
  engines per NC are selected by the address **nibble at bit36** (2/3/6/7); the die
  by **bit47** (`0x80…`). These are the engine **RELEASE / completion-sema** half —
  the apertures the CUST3 group/bank selectors target.
* **idx[8..15] — per-(NC × engine) APB-broadcast CTL-block bases** (`0x...02000000` /
  `03` / `04` / `05`, die-banked). These are **byte-identical** to the libnrt
  `cayman_bcast_region_table` (§5.1) modulo a `+0x80000` M2S/S2M sub-block offset —
  the DATA-plane DMA-doorbell region the family-D channel thunks fan their
  tail-pointer-increment writes through.
* **idx[16..19] — the 4 host-transfer SDMA engines** (D2H `0x...06800000` / H2D
  `0x...06900000`, die-banked) — the device↔host top-level DMA, distinct from the
  per-NC compute-engine doorbells.

The table ends at `+0xb0`, where the **12-entry `algo_type` engine-dispatch table**
begins (`{0x3c38, 0x3c35, 0x3c2e, 0x48f0, 0x3c27, 0x3c20, 0x3c20, 0x3c20, 0x3e9c,
0x3e32, 0x3e76, 0x3e80}`; idx 3 `0x48f0` is the error/default outlier). The exc-cause
table at `+0x000` is `{0x1399, 0x13b1, 0x13c5, 0x1399}`. *(all tables OBSERVED HIGH
this session.)*

> **CORRECTION — `+0xb0` is ONE 12-entry table, not an 8-slot table + a 4-entry
> `+0xd0` secondary.** An earlier framing here (and in
> [`ncfw-dram-ctx-log`](ncfw-dram-ctx-log.md) §2.3, since corrected) split the
> `0xb0..0xe0` window into an 8-slot table at `+0xb0` plus a separate 4-entry table at
> `+0xd0`. The dispatch instruction at v3 IRAM `0x3bf8` (`const16 a2,0xB0; addx4
> a2,a3,a2; l32i.n a5,a2,0` — a single base literal, one ×4 index, one load; no second
> `const16 0xD0`) reads it as **one contiguous 12-entry `algo_type` jump table** with
> `a3 ∈ 0..11`; entries 8..11 fall at `0xB0 + 8·4 = 0xD0`. See the capstone
> [`lx-isa-naming-archid-synthesis`](lx-isa-naming-archid-synthesis.md) §4.3 and the
> [`main-dispatch-loop`](main-dispatch-loop.md) §4. *(the dispatch read is the decider —
> OBSERVED HIGH.)*

### 5.1 `cayman_bcast_region_table` — byte-identity with idx[8..15] + 0x80000

Dumped this session from `libnrt.so` `.rodata` @`0x9def40` (the symbol objdump even
resolves at the `lea` in `0x30c1e0`):

```
[0] 0x001002080000   [1] 0x001003080000   [2] 0x005004080000   [3] 0x005005080000
[4] 0x801002080000   [5] 0x801003080000   [6] 0x805004080000   [7] 0x805005080000
```

These are **byte-identical** to the NCFW reg-addr ctl-blocks idx[8..15]
(`0x001002000000` etc.) **except** for a `+0x80000` offset — the **M2S/S2M qbundle
sub-block base** within the engine APB window (`0x001002080000 − 0x001002000000 =
0x80000`). So the NCFW firmware's ctl-block region **IS** the libnrt per-engine
APB-broadcast doorbell region; the `+0x80000` is the M2S/S2M sub-window, and the
die-1 variants (`[4..7]`) set **bit47** (`0x800000000000`) — the same die selector
as the NC1 reg-addr entries.

> **CORRECTION — "engine A/B/C/D = EVENT-slot 0x28/0x38/0x68/0x78" is an address
> field, not an event-file sub-offset.** An earlier reading framed idx[0..3] as four
> "engine apertures at EVENT-file offset 0x28/0x38/0x68/0x78 = event index
> 10/14/26/30." Bit-decode this session settles it: each byte `0x28`/`0x38`/`0x68`/
> `0x78` is the **byte at bits[39:32] of the 48-bit device address**, i.e.
> `(engine_nibble << 4) | 0x8` — the **high nibble** (2/3/6/7, at bit36) is the
> engine/TPB selector and the **low nibble** (`0x8`) is a constant region selector.
> They are **not** offsets into an EVENT file; they are four distinct device base
> addresses each ending `0x...02700000`. idx[0..7] are the 8 full per-(NC × engine)
> EVENT/sema base apertures (4 engines × 2 dies), not 4 event-slot offsets.
> *(bit-decode + the idx[8..15] ↔ `cayman_bcast_region_table` equality OBSERVED HIGH.)*

---

## 6. CONTROL-plane vs DATA-plane doorbell — the key distinction

### 6.1 DATA-plane (the CUST3 thunks' target) — OBSERVED HIGH

The uDMA per-queue **`TDRTP_INC`/`RDRTP_INC`** at `queue_base + 0x38` (abs `+0x1038`
at `q=0`), fanned via the per-engine APB-broadcast window (`bcast_region_table`,
idx[8..15], §5). The `libncfw.so` `ctx_log` key roster (the firmware's own DMA
vocabulary, `strings -t x` this session) is purely data-plane:

| key | `libncfw.so` offset | meaning |
|-----|---------------------|---------|
| `m2s_tail_ptr` | `0x65150` | TX (M2S) tail pointer |
| `s2m_tail_ptr` | `0x6515d` | RX (S2M) tail pointer |
| `dma_apb_bcast` | `0x651e2` | the APB-broadcast doorbell window |
| `dma_apb_bcast_%d` | `0x65246` | per-engine bcast format |
| `dma_apb_rsvd_addr` | `0x652cd` | reserved bcast address |
| `dma_engines_bitmap` | `0x6535c` | the participating-engines mask |
| `tdrbp_low` | `0x65407` | TX descriptor-ring base PA low |
| `tdrbp_high` | `0x65413` | TX descriptor-ring base PA high |

*(keys repeat once per generation in the blob; all offsets cited are the first
instance.)* The CUST3 family-D/B/C thunks ring **this** doorbell. The descriptor-ring
naming (`tdrbp`/`m2s`/`s2m`) is shared with the
[descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md).

### 6.2 CONTROL-plane (NOT a CUST3 thunk target) — OBSERVED HIGH

The TOP_SP `LOCAL_REG` quintet, through the `kaena_khal` HAL vtable, re-verified
byte-exact this session:

| signal | getter | offset |
|--------|--------|--------|
| INIT | `aws_hal_sp_topsp_set_init_signal_cayman` @`0x471ae0` | `lea rdi,[rax+0x1540]` |
| TSYNC | `aws_hal_sp_topsp_set_tsync_signal_cayman` @`0x471b10` | `lea rdi,[rax+0x1560]` |
| **host_trigger (START)** | `aws_reg_cayman_get_top_sp_nx_local_reg_host_trigger_offset` @`0x47b080` | `mov eax,0x615a0` |
| STOP | (`0x47b090`) | `mov eax,0x615c0` |
| SWITCH (bbswitch) | (`0x47b0a0`) | `mov eax,0x615e0` |

The SUNDA host_trigger is **`0x60848`** (`aws_reg_sunda_…_host_trigger_offset`
@`0x479120`: `mov eax,0x60848`; STOP `0x6084c`). These are the management-core
start/stop/step signals — a **separate** register block from the uDMA tail-pointer
doorbell. The CUST3 DMA thunks do **not** use them. See
[TOP_SP lowering](../ops/top-sp-lowering.md).

### 6.3 Why the distinction matters

Both planes are real and both are referenced by the reg-addr table:

* **idx[0..7]** = the per-engine EVENT/sema apertures (`0x...02700000`) — the engine
  **release / completion-sema** half, the CUST3 group/bank selectors.
* **idx[8..15]** = the APB-broadcast DMA **tail-pointer doorbell** region
  (`0x...02000000…05`, == `cayman_bcast_region_table − 0x80000`) — the family-D
  channel-ring half.

The CUST3 op carries a **DIRECTION nibble** (`0x0a` IN / `0x2a` OUT, §3) and a
**t-field MODE** (§1): the IN/read leg is the head-pointer read (`DRHP` `+0x1034`),
the OUT/write leg is the tail-pointer doorbell (`TDRTP_INC`/`RDRTP_INC` `+0x1038`).
The DATA-plane doorbell is a *DMA-ring* write (advance the tail by N); the
CONTROL-plane `0x615a0` is a *management-core* value-`1` host trigger — orthogonal
register blocks, orthogonal value semantics. *(both planes OBSERVED HIGH; the precise
CUST3-op-bit mapping of IN/OUT onto DRHP/DRTP_INC is INFERRED-STRONG from the helper
byte-diff + the host getter set; the TIE bit semantics are the residual.)*

---

## 7. The off-image per-channel descriptor pointers (honest limit)

The family-D thunks load a descriptor pointer via an `l32r` at thunk `+0x10`
(`a0 1d ac 01`, scalar op0-nibble-1; **OBSERVED HIGH** the instruction is present).

> **GOTCHA — the `0xfffdXXXX` descriptor values are off-image, not blob-stored.**
> An earlier reading reported the loaded *values* as `0xfffd307c..` (stride `0x30`
> setup / `0x20` ring). Those `0xfffdXXXX` figures are the disassembler's PC-relative
> `l32r`-**target** computation (the literal's load *address* relative to the
> firmware load VMA); a byte-scan of the carved `v4_iram` finds **zero**
> `0xfffdXXXX` literals stored in the blob, because the descriptor literal pool + the
> descriptor **contents** live **off-image** in the mask-ROM (`0xfffdXXXX` band), not
> in the shipped payload. So: the `l32r` load is OBSERVED; the pointed-to per-channel
> ring base/size/doorbell-offset **values** are off-image and UNREADABLE from
> `libncfw.so`. *(the `l32r` presence OBSERVED HIGH; the target values are off-image — LOW.)*

By correlation with the uDMA per-queue ring CSR block (§4.2), the descriptor is a
`{base_lo +0x1028, base_hi +0x102c, len +0x1030, head +0x1034, tail +0x103c,
tail_inc +0x1038}` control record; the IN/OUT split = head-read vs tail-doorbell.
The structure is recoverable; the values are not. See
[pring descriptors](pring-descriptors.md) for the standing ring template the firmware
re-arms.

---

## 8. Per-generation stability + the residual

### 8.1 Per-generation stability — OBSERVED HIGH

| gen | CUST3 op | CUST3 markers | family-D sig `3e 1c 4f 98 67 ff` | group skeleton `ae b1 01` |
|-----|----------|---------------|----------------------------------|---------------------------|
| v3 cayman | `80 0c 0d` (t=8) | 19 | 0 (group sels at `0x36ef`, shifted) | present (2×) |
| v4 mariana | `a0 0c 0d` (t=a) | 19 | **8** (the per-channel block) | present (2×) |
| v4_plus | `a0 0c 0d` (t=a) | 19 | **0** (handler REWRITE) | **absent** |

* The reg-addr engine apertures are byte-identical v3 == v4 (idx0..3 =
  `0x002802700000` / `0x003802700000` / `0x006802700000` / `0x007802700000`).
* `v4_dram == v4_plus_dram` (reg-addr + dispatch tables byte-identical):
  **v4_plus rewrites the family-D thunk code while preserving the SAME reg-addr
  TARGET table** — the aperture↔engine binding is the invariant, the consuming code
  is revised.
* The CSR offsets (`+0x1028..+0x10e0`, the `+0x38` doorbell, `(q+1)<<12`) are
  identical across the cayman/mariana/sunda libnrt getters (sunda lacks the
  `data_tail_inc` pair).
* The **only** per-gen change in the doorbell mechanism is the CUST3 op t-field byte
  (Cayman `8` / Mariana `a`) — the per-silicon doorbell-addressing **MODE** of the op.
  The thunk → reg-addr binding **scheme** is generation-invariant.

> **GOTCHA — v2/SUNDA is the addressing outlier; v5/MAVERICK is FILE-ABSENT.**
> SUNDA's `v2_dram` reg-addr table has only **4** valid entries with `0x0fff_xxxx`
> older-fabric addressing (idx0 = `0x0fffc2700000`, idx1 = `0x0fffc6700000`,
> idx2 = `0x0ffff0600000`, idx3 = `0x0ffff0d00000`; idx4 = `0` is the sentinel
> terminator), and `v2_iram` carries **no** `XX 0c 0d` CUST3 word at all — the
> doorbell-op encoding postdates SUNDA. **MAVERICK / NC-v5 NCFW does not ship in this
> corpus** (no v5 IRAM/DRAM blob exists), so every v5 statement is **INFERRED /
> ABSENT**: by carried structure it would extend the `a0 0c 0d`-class doorbell op
> with a new t-field mode and the same `(q+1)<<12` + `+0x38` tail-inc target, but
> none of that is byte-grounded here.

### 8.2 The residual (`.tie`-bound) — LOW / OPEN

* The **symbolic CUST3 TIE mnemonic** + *which aperture bits the op writes* (the
  op-word bit-fields, the t-field `8→a` mode meaning) — needs the LX `.tie` /
  `core-isa.h`, which does **not** ship (decisive negative). The op **word**, the
  direction nibble (`0x0a`/`0x2a`), the selector immediates (`ae bX 0Y` /
  `d2 04 ZZ 0Y`), the target region, and the value = N are all proven; the TIE op's
  internal bit semantics are not.
* The **off-image per-channel ring-descriptor contents** (the `l32r` literal pool,
  mask-ROM `0xfffdXXXX` band) — base/size/doorbell-offset per channel (§7).
* The compute-engine **CLASS** (PE/ACT/POOL/DVE) per reg-addr idx[0..7] aperture —
  the inferential dispatch-slot map, not hardware-proven (would need a NEFF / compiler
  engine-id→aperture trace).
* The full fabric-side **APB-broadcast write data-flow** (fabric → firewall →
  UDMA CSR) and the on-device LX schedule that orders the per-step doorbell/fence —
  the [DMA reprogram + APB broadcast](dma-reprogram-apb-bcast.md) scope; the *address
  placement* is HIGH here, the device write path itself lives on that sibling page.

---

## 9. Reimplementation checklist

To ring a CUST3-equivalent DMA doorbell from a reimplemented NCFW management core:

1. **Pick the plane.** A *DMA ring advance* is a DATA-plane write; a *management-core
   start/stop* is the CONTROL-plane `0x615a0` value-`1` (do not conflate them).
2. **Resolve the engine aperture.** From the reg-addr table: idx[8..15] for the
   APB-broadcast CTL region (data-plane doorbell), or idx[0..7] for the EVENT/sema
   aperture (engine release). Select NC via bit47 (`0x80…`) and the engine via the
   bit36 nibble (2/3/6/7).
3. **Compute the address.**
   `bcast_region_table[engine] + m2s_sub_block + ((q+1)<<12) + 0x38` for the
   TX(M2S) tail-inc; use the `+0x38` S2M getter for RX. (`+0xe0` for the
   enhanced-prefetch data-tail-inc on cayman/mariana; absent on sunda.)
4. **Write the value.** `value = N` = the descriptor count to arm. One write to the
   broadcast window reaches every engine in the `dma_alloc_bitmap` mask.
5. **Fence + wait.** `memw`-fence the completion-semaphore write; the next step waits
   on the completion semaphore (`DMA_COMPLETION_MARKER 0xabcdef01`).
6. **Direction.** IN = head-pointer read (`DRHP` `+0x1034`); OUT = tail-pointer
   doorbell (`TDRTP_INC`/`RDRTP_INC` `+0x1038`). The CUST3 op encodes this in the
   `0x0a`(IN)/`0x2a`(OUT) direction nibble.

---

### Cross-references

* [NCFW DMA reprogram + APB broadcast](dma-reprogram-apb-bcast.md) — the broadcast
  window the doorbell writes land in (closest sibling).
* [NCFW main dispatch loop](main-dispatch-loop.md) — the FW_IO command path that
  invokes the thunks.
* [al_udma HW engine](../../dma/udma-hw-engine.md) — the per-queue CSR map.
* [Descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md) — the
  M2S/S2M `+0x1038` `TDRTP`/`RDRTP` ring layout.
* [TOP_SP lowering](../ops/top-sp-lowering.md) — the CONTROL-plane `0x615a0` host
  trigger.
* [pring descriptors](pring-descriptors.md) — the standing ring template re-armed
  between steps.
* [scalar-LX core](../../uarch/ncfw-lx-core.md) /
  [LX-ISA naming & arch_id synthesis](lx-isa-naming-archid-synthesis.md) — the ISA
  decode evidence behind the `call8` rule.
