# Local-Memory / System-Bus / LSU Model

> **Scope.** The complete **data side** of the Vision-Q7 NX "Cairo" core (config `ncore2gp`,
> `uarchName="Cairo"`, `XCHAL_HW_VERSION_NAME="NX1.1.4"`) as a single memory model a
> reimplementer can build against. Four things, in order: **(1)** the Q7's own flat 32-bit
> local-memory address map (IRAM @`0x0`, the 4-bank DataRAM @`0x80000`, the 1-GiB system-RAM
> AXI window @`0x100000`, the per-core DRAM aperture, and the off-core SBUF/HBM NX windows);
> **(2)** the **dual-LSU** datapath — two load ports, one store port, the S0/S1 FLIX
> memory-slot split, the 64-byte access granule, and the `valign`-register accumulator that
> makes unaligned vector L/S work on a core whose *scalar* unaligned policy is "trap"; **(3)**
> the AXI/ACE-Lite master interface — 64-byte data bus, the `axi2sram` bridge to SBUF, the
> 8-entry write buffer, the outstanding-transaction bounds; **(4)** the load/store→pipeline
> **timing** — the stage-9 vector memory port, the stage-10 vec-dest, the stage-11 store
> commit, the scalar load-use.
>
> **Companion pages.** The per-opcode load/store operand catalogues are
> [B06 Vector Loads + valign](../isa/ref/b06-loads.md) and
> [B07 Vector Stores](../isa/ref/b07-stores.md); the FLIX slot bitfield decode is in
> [FLIX Co-Issue Matrix](./co-issue-matrix.md); the multi-ported file read/write-port pressure
> is in [Register-File Port Model](./regfile-ports.md); the full cycle-latency table is in
> [Pipeline Timing Model](./pipeline-timing.md). The SBUF/PSUM bank model and the off-core DMA
> contention belong to the DMA Part (link forward → `dma/sbuf-psum-banks.md`, Part 9). This
> page owns the **address map**, the **LSU/valign datapath**, the **AXI/write-buffer model**,
> and the **L/S→pipeline timing**.

Every claim is tagged `[CONF × PROV]`: confidence `HIGH/MED/LOW`; provenance `OBSERVED`
(read byte-exact out of a shipped config header / unstripped config DLL / device disassembly
here), `INFERRED` (derived from observed structure), `CARRIED` (from a prior in-corpus
report, re-verified this pass). Binary paths are relative to the extracted tree; the config
DLLs live under `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/`, the config headers
under `…/ncore2gp/xtensa-elf/arch/include/xtensa/config/`, the device toolchain under
`extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/`.

---

## 1. The Q7 local-memory + NX-local address map

The Q7 is an **identity-mapped** core: `XCHAL_HAVE_TLBS = 0`, `XCHAL_HAVE_PTP_MMU = 0`,
`XCHAL_MMU_ASID_BITS = 0` (`core-isa.h:781,787,793`). There is no address translation —
`vaddr == paddr` for every access — so the 32-bit number the program dereferences *is* the
NX-local physical address. Region protection is by a 16-entry MPU (`XCHAL_HAVE_MPU = 1`,
`XCHAL_MPU_ENTRIES = 16`, `core-isa.h:800-801`), not by a page table. Reaching the wider SoC
physical space is done by **hardware window registers** the device runtime programs (a
software-managed TLB; carried from the security Part). `[HIGH × OBSERVED]` (no-MMU / MPU);
`[HIGH × CARRIED]` (window mechanism).

### 1.1 The on-core local-memory map (directly dereferenceable)

Read byte-exact from `core-isa.h` (the local RAMs) and `system.h` (the system-RAM / I/O
windows), both for config `ncore2gp`:

| NX range | size | what it is | const & backing |
| --- | --- | --- | --- |
| `[0x00000000,0x00010000)` | 64 KiB | **INSTRAM0 (IRAM)** — code + reset/exception vectors. VECBASE reset @`0x0`. L/S-able. | `XCHAL_INSTRAM0_VADDR=0x0`, `_SIZE=65536`, `_ECC_PARITY=0`, `_HAVE_IDMA=0`; `XCHAL_HAVE_IMEM_LOADSTORE=1` (`core-isa.h:410-426`) |
| `[0x00010000,0x00080000)` | 448 KiB | **gap / device-trap hole** — no local RAM; MPU-walled | `[HIGH × CARRIED]` (MPU hole @`0x10000`) |
| `[0x00080000,0x00090000)` | 64 KiB | **DATARAM0** — the data side; **4 banks**; iDMA target | `XCHAL_DATARAM0_VADDR=0x00080000`, `_SIZE=65536`, `_ECC_PARITY=0`, `_BANKS=4`, `_HAVE_IDMA=1` (`core-isa.h:418-424`) |
| `[0x00090000,0x00100000)` | 448 KiB | **gap / device-trap hole** — MPU-walled | `[HIGH × CARRIED]` |
| `[0x00100000,0x40100000)` | **1 GiB** | **SYSTEM-RAM AXI window** (`XSHAL_RAM`) — outbound AXI/ACE-Lite to SoC mem; cached writeback. VECTOR1 @`0x100000`. | `XSHAL_RAM_VADDR=0x00100000`, `_VSIZE=0x40000000` (`system.h:86-89`); `XCHAL_RESET_VECTOR1_VADDR=0x00100000` (`core-isa.h:742`) |
| `0x70000000` | 224 MiB | **IOBLOCK cached** (devices) — AXI | `XSHAL_IOBLOCK_CACHED_VADDR=0x70000000`, `_SIZE=0x0E000000` (`system.h:75-77`) |
| `0x90000000` | 224 MiB | **IOBLOCK bypass** (devices) — AXI, cache-bypass | `XSHAL_IOBLOCK_BYPASS_VADDR=0x90000000`, `_SIZE=0x0E000000` (`system.h:79-81`) |
| `0xA0000000` | 512 MiB | **RAM bypass alias** — bypass static-map alias of `XSHAL_RAM` | `XSHAL_RAM_BYPASS_VADDR=0xA0000000`, `_PSIZE=0x20000000` (`system.h:104-106`) |
| `0xC0000000` | 512 MiB | **SimIO** — sim/host I/O | `XSHAL_SIMIO_PADDR=0xC0000000`, `_SIZE=0x20000000` (`system.h:111-114`) |

`[HIGH × OBSERVED]` — every base/size above is a literal `#define` re-read this pass. The two
IOBLOCK sizes are `0x0E000000 = 224 MiB` exactly. There is **no D-cache**
(`XCHAL_DCACHE_SIZE = 0`, `core-isa.h:298`), so the system-RAM window is *not* fronted by a
data cache — it is reached by raw AXI master transactions (§4). The cache attribute for the
system-RAM 512 MiB region is writeback (`XSHAL_ALLVALID_CACHEATTR_WRITEBACK = 0x22222111`,
`system.h:150`); but with `DCACHE_SIZE = 0` that "cached" attribute selects only the
write-buffer behaviour, never a data-cache line fill.

> **NOTE — "cached" with no D-cache.** The `XSHAL_RAM` window is labelled cacheable
> (`CACHEATTR` writeback) yet `XCHAL_DCACHE_SIZE = 0`. On this config the cacheable attribute
> governs **write-combining / write-buffer** behaviour and ordering, not allocation: a store
> to system RAM funnels through the 8-entry write buffer (§4.2); a load is a fresh AXI read.
> A reimplementer must not provision a D-cache for the data side. `[HIGH × OBSERVED]`

### 1.2 The reset/stack boundary anchors

`XCHAL_HAVE_VECBASE = 1` (relocatable vectors), `XCHAL_VECBASE_RESET_VADDR = 0x0`
(`core-isa.h:735-736`). The two reset vectors anchor the IRAM and the system-RAM windows:
`XCHAL_RESET_VECTOR0_VADDR = 0x00000000` (in IRAM) and `XCHAL_RESET_VECTOR1_VADDR =
0x00100000` (in system RAM) (`core-isa.h:740-742`). The interrupt-stack base sits at the high
end of the 64-KiB DataRAM (`ISB_VADDR = 0x0008FED0`, the top of `[0x80000,0x90000)`); a deep
kernel switches to an HBM-backed window. `[HIGH × OBSERVED]` (vectors); `[HIGH × CARRIED]`
(stack base).

### 1.3 The off-core NX windows + per-core DRAM aperture

Beyond the on-core RAMs and the system-RAM AXI window, the device runtime overlays an NX MEM
REG CSR block and the SBUF/HBM apertures via the 5-region software TLB (carried from the
security Part):

* **NX `0x80000000`** — a 64-MiB **pinned** window → SoC `STATE_BUF [0x2000000000,0x2004000000)`
  (32 MiB SBUF + 32 MiB reserved pad). This is the Q7's view of SBUF (§4.3).
* **NX `0x84000000`** — a 64-MiB **pinned** window → an HBM-resident scratch heap (`hbm_scratch`).
* **NX `0x07000000` / `0x09000000` / `0x0a000000`** — three 16-MiB **dynamic** windows
  (round-robin %3) → any other 16-MiB SoC region (HBM tensors, the HBM stack, EVT_SEM, remote
  die/chip). The op's pointer working set is therefore 3-deep — a transient-reference hazard.

`[HIGH × CARRIED]` (NX windows). The **per-core DRAM aperture** rebases the *same* local
DataRAM range `[0x80000,0x90000)` into a disjoint SoC slice per core via `dram_addr_to_soc_addr`:

```c
// Per-core DRAM-address translation (carried from the security Part, re-stated here as the
// one place the 8 SPMD pool cores hold HARDWARE-disjoint memory at a shared NX address).
// guard:  the low 16 bits of the "DRAM address" select the byte; the high bits MUST be 0x80000.
uint64_t dram_addr_to_soc_addr(uint32_t dram) {
    assert((dram & 0xFFFF0000u) == 0x00080000u);      // else _Assert / FATAL
    uint32_t cpu_id = read_PRID() & 0x7;               // {0..7}; bgeui 8 guards the range
    uint32_t idx    = 9u + 2u * cpu_id;                // per-core stride, step 0x10000
    uint32_t off    = dram & 0x0000FFFFu;              // byte offset inside the 64-KiB window
    return per_core_base + ((uint64_t)idx << 16) + off;
}
```

Directly dereferencing `[0x80000,0x90000)` on-core hits the **64-KiB SRAM**; the
`dram_addr_to_soc_addr` path is the **SoC-address translation** a DMA/AXI access uses. The
distinction (local SRAM vs. SoC rebase at the same NX address) is the synthesis tie.
`[HIGH × CARRIED]`

---

## 2. The dual-LSU model

### 2.1 Two load ports, one store port

The config declares **two** load/store units and a **unified** L/S pipe:

```
XCHAL_NUM_LOADSTORE_UNITS = 2     // core-isa.h:226
XCHAL_UNIFIED_LOADSTORE   = 1     // core-isa.h:240  (both units share one data path)
```

`[HIGH × OBSERVED]`. The *asymmetry* of the two units — which one carries stores — is not in
`core-isa.h`; it is read directly out of the unstripped ISS scheduling DLL `libcas-core.so`.
That DLL imports its memory-port hazard interfaces by name, and exactly **three** exist:

```
$ nm libcas-core.so | rg 'nx_(Load|Store)_[0-9]_interface'
                 U nx_Load_0_interface
                 U nx_Load_1_interface
                 U nx_Store_0_interface          # <- there is NO nx_Store_1_interface
$ nm libcas-core.so | rg -c 'nx_Store_1_interface'
0
```

Two **load** ports (`Load_0`, `Load_1`), one **store** port (`Store_0`). The same three are
imported by `libcas-ref-core.so` (`nm -D`). `[HIGH × OBSERVED]` — this is the cleanest
statement of the dual-LSU shape: **a bundle may issue two loads, but at most one store.**

### 2.2 The S0/S1 FLIX memory-slot split

In a FLIX bundle the two units are realized as two memory slots, **S0** and **S1**. Every
`(format, slot, opcode)` scheduling triple has a function in `libcas-core.so` named
`<FMT>_<fmt>_<slot>_<class>_<width>_inst_<OPCODE>_{issue,stall}`. Enumerating the slot/class
tokens over those names gives the op-class assignment:

```
$ nm libcas-core.so | rg -o 'S[0-9]_[A-Za-z]+_[0-9]+_inst' | sort -u
  S0_Ld_4_inst   S0_LdSt_4_inst   S0_LdStALU_4_inst      # slot 0: load, load/store, load/store+ALU
  S1_ALU_16_inst S1_Ld_16_inst                            # slot 1: ALU, load-only
  S2_Mul_16/27/28/41   S3_ALU_*   S4_ALU_24               # slots 2..4: multiply / ALU (no memory)
```

So the two **memory** slots are **S0 = full Load+Store** (classes `LdSt`/`LdStALU`, and a
bare `Ld` in the dual-load format) and **S1 = Load-only** (class `Ld`, or `ALU` where S1
carries no load). The eleven FLIX formats (live `Format_<F>_encode` symbols in
`libisa-core.so`) split as:

| format | S0 | S1 | memory ops / bundle |
| --- | --- | --- | --- |
| F0, F2, F3, F6, F7 | `LdSt` | `Ld` | 2 (one may store) |
| F1 | `LdStALU` | `Ld` | 2 (one may store) |
| **F4** | `Ld` | `Ld` | **2 loads** (no store slot) |
| F11 | `Ld` | `ALU` | 1 load |
| N0, N1 | `LdSt` | — | 1 (one narrow memory op) |
| N2 | `LdSt` | `Ld` | 2 (one may store) |

`[HIGH × OBSERVED]`. The structural ceiling: **≤ 2 memory ops per bundle, of which ≤ 1 is a
store** (because store opcodes live only in S0). The dual-LOAD format **F4** carries `Ld` in
*both* memory slots (and `Mul`+`ALU` in S2/S3); the same load opcode is instantiated in both,
e.g. `F4_F4_S0_Ld_4_inst_IVP_LA2NX8_IP_issue` and `F4_F4_S1_Ld_16_inst_IVP_LA2NX8_IP_issue`.

#### Stores are S0-only — verified four ways

| evidence | result |
| --- | --- |
| `nm libcas-core.so \| rg 'S1_(Ld\|ALU)_.*inst_IVP_(SV\|SA2\|SANX\|SVNX\|SAPOS\|SAV\|SCATTER\|SBN\|SRAINX)' \| …sort -u \| wc -l` | **0** distinct stores in S1 (CAS-instantiated) |
| same sweep over the full `libisa-core.so` opcode dictionary | **0 / 66** stores in S1 |
| real data store `IVP_SV2NX8_IP` slot census (`S0_LdSt`×8, `S0_LdStALU`×1) | **S0 only** |
| scatter store `IVP_SCATTERNX8U` slot census | **S0 only** (`S0_LdSt`/`S0_LdStALU`) |

`[HIGH × OBSERVED]`. Conversely loads populate **both** slots: `IVP_LV2NX8_I` (plain
full-vector load) instantiates `…S0_LdSt…` and `…S1_Ld…` across F0–F7/N2 (18 distinct load
opcodes reachable in S1; 19 distinct real-store opcodes reachable in S0).

> **GOTCHA — S-prefixed opcodes that are NOT stores.** Two families *look* like stores by
> mnemonic but are ALU ops and must be excluded from any store census: `IVP_SRAINX16`
> (shift-right-arith-immediate — an ALU op that the scheduler places in `S0_LdSt`/`S3_ALU`
> alike) and the `IVP_SEQ*` set-equal compares (which legitimately appear in `S1`). A naive
> `rg 'IVP_S'` over slot names yields false stores in S1; the real store roster is
> `SV*/SA*/SAPOS*/SAV*/SB*/SCATTER*` only. `[HIGH × OBSERVED]`

> **NOTE — valign seed/flush ops can ride a load slot.** `IVP_SALIGN_*` and `IVP_SAPOS_*`
> are *valign* prime/flush ops (§3) with **no vector data operand**. `IVP_SAPOS_FP` is even
> instantiated in `F4_F4_S0_Ld_4` (a pure load slot): its `_issue` body reads only AR @1 and
> the valign register @9/@10 — it commits the residual through the valign accumulator, not a
> fresh vector store, so the slot model permits it where a load could go. The *data*-store
> opcodes remain S0-`LdSt`/`LdStALU` only. `[HIGH × OBSERVED]`

#### Scatter/gather slot split

The two-phase SuperGather datapath also lives in the memory slots, and splits cleanly:
the **address phase** (`IVP_GATHERA*`, `IVP_MOVGATHERD`) lands in **S0**; the **data-drain
phase** (`IVP_GATHERD*`, e.g. `gatherdnx16`) is the only "gather" family in **S1** (`S1_Ld`);
every **scatter** (`IVP_SCATTER*`) is **S0-only** — reinforcing the stores-only-in-S0 rule.
`[HIGH × OBSERVED]` (11 distinct gather + 13 distinct scatter opcodes in the ISA dictionary).

### 2.3 The access granule — 64-byte natural, 8-byte SBUF word, distinct DMA beats

Three granules govern a Q7 access, and conflating them is the classic error. Pinned this pass:

| granule | value | source const | role |
| --- | --- | --- | --- |
| **data bus / natural access** | **64 B (512 b)** | `XCHAL_DATA_WIDTH = 64` (`core-isa.h:229`) | the on-core compute access quantum; an aligning store funnels to `eff & ~0x3f` (§3.3) |
| vector register / transfer | 64 B (512 b) | vec file 512-bit; `MemDataIs512Bits` | the L/S datapath width |
| scalar L/S | 1 / 2 / 4 B | `L8UI` / `L16UI` / `L32I` (must be aligned) | the scalar pipe |
| **SBUF SRAM word (ECC unit)** | **8 B (64 b)** | `STATE_BUF_WORD_SIZE = 8` (sunda arch-isa hdr `:40`) | the SBUF byte-offset quantum; one 64-B bus access spans 8 SBUF words |
| on-core iDMA beat | 16 B (128 b) | `XCHAL_IDMA_DATA_WIDTH = 128` (`core-isa.h:436`) | the on-core iDMA port (§4.4) |
| SBUF DMA-engine beat | 32 B (256 b) | `DMA_ENGINE_DATA_WIDTH_BYTES` | the SBUF-side DMA beat (carried) |
| I-cache access / i-fetch | 32 B (256 b) | `XCHAL_ICACHE_ACCESS_SIZE = 32`, `XCHAL_INST_FETCH_WIDTH = 32` (`core-isa.h:383,228`) | instruction side |
| I-cache line | 64 B | `XCHAL_ICACHE_LINESIZE = 64` (`core-isa.h:291`) | instruction side |

`[HIGH × OBSERVED]` (every value above is a literal const re-read this pass, except the SBUF
DMA-engine beat which is `[HIGH × CARRIED]`). One sentence: **the Q7 compute data port is 64
bytes wide; it sits on SBUF's 8-byte words through the `axi2sram` bridge; the DMA paths move
16-/32-byte beats; the i-fetch moves 32-byte granules.**

### 2.4 The unaligned-access policy — scalar TRAP, vector VALIGN

```
XCHAL_UNALIGNED_LOAD_EXCEPTION  = 1     // core-isa.h:235  scalar misaligned load -> exception
XCHAL_UNALIGNED_STORE_EXCEPTION = 1     // core-isa.h:236
XCHAL_UNALIGNED_LOAD_HW         = 0     // core-isa.h:237  no hardware unaligned scalar load
XCHAL_UNALIGNED_STORE_HW        = 0     // core-isa.h:238
```

`[HIGH × OBSERVED]`. This is the central asymmetry of the data side:

* A **scalar** unaligned access (`L32I`/`S32I` to a non-naturally-aligned address) **traps** —
  a `LoadStoreAlignment` / ADDRESS exception (`EXCCAUSE 9`). Scalar code must keep pointers
  aligned.
* A **vector** unaligned stream is handled **entirely in software** by the `valign` register
  file (§3): the aligning `LA*` loads and `SA*` stores read/write a 512-bit *aligned* window
  and merge across the misalignment boundary using a valign register, so the **hardware only
  ever issues naturally-aligned 64-byte accesses.**

There is **no hardware unaligned unit on either path.** The valign idiom is therefore not an
optimization but a **correctness requirement** for any unaligned vector stream. `[HIGH × OBSERVED]`

---

## 3. The valign accumulator mechanism — unaligned vector L/S

The `valign` register file (file idx 4, **512-bit × 4**, registers `u0..u3`; the assembler
rejects `u4`) is the software-managed cross-boundary byte funnel that turns a stream of
naturally-aligned 64-byte hardware accesses into a logically-unaligned vector stream. The
register-index field is 2 bits (`opnd_sem_valign_addr` returns `esi & 0x3`,
`libcas-core.so @0x17aa2c0`) — exactly 4 values. `[HIGH × OBSERVED]`

### 3.1 The load side — `uul` prime → iterate

A valign register holds the **carry-over bytes** of the previous partial row. The
aligning-load idiom is **prime → iterate**, observed verbatim in device code (the
`memcpy_data_transfer_impl` aligning copy in `libneuroncustomop.a`, disassembled with the
native `xtensa-elf-objdump XTENSA_CORE=ncore2gp`):

```asm
; PRIME — seed valign u0 from the first aligned row @a6; produces NO vec result
    { blti.w15 a8,128,…; ivp_la_pp   u0, a6 }
; ITERATE — v = align(u0 ++ mem[a3]); u0 = new residue; a3 += 32 (one 256-bit row)
    { ivp_la2nx8_ip  v31, u0, a3; nop; mov.a a2, a9 }
```

```c
// Annotated load funnel.  An aligning load fetches the NEXT 256-bit aligned row, concatenates
// it with the residue held in the valign register, extracts the in-flight 512-bit vector, and
// updates the residue.  The funnel byte-shift is a rotate-merge over {carry_row || new_row}.
typedef struct { uint8_t bytes[64]; } valign_t;   // a u0..u3 register (512 bits)

// PRIME: ivp_la_pp u, a   — fetch first aligned row, seed residue, no vec out.
void la_prime(valign_t *u, const uint8_t *aligned_row /* = a & ~0x3f */) {
    load_aligned_512(u->bytes, aligned_row);      // hardware issues ONE naturally-aligned 64-B read
}
// Alternatives, NO memory access (pure valign synthesis):
//   ivp_zalign u        -> memset(u,0,64)                 (zero-prime)
//   ivp_malign u, u'     -> *u = *u'  (valign->valign derive; two u-regs, no AR operand)

// ITERATE: ivp_la2nx8_ip v, u, a  — align(u ++ mem[a]); update u; a += 32.
void la_iter(uint8_t out[64], valign_t *u, const uint8_t **a, unsigned misalign /* a0 & 0x3f */) {
    uint8_t next[64];
    load_aligned_512(next, (const uint8_t *)((uintptr_t)*a & ~0x3fu));  // aligned hw read
    funnel_512(out, u->bytes, next, misalign);    // rotate-merge: {residue || next} >> misalign
    memcpy(u->bytes, next, 64);                   // new residue = this row
    *a += 32;                                      // post-increment (the *_ip variant)
}
```

Variants: `LAV*` = byte-count-bounded tail (`ivp_lav2nx8_xp v,u,a,len`); `LAT2NX8` =
predicated aligning (a `vbool` mask gates per-lane write); `L2A*`/`L2AU2*` = dual-port paired
aligning (uses **both** memory ports, 2×512 b per instruction). `[HIGH × CARRIED]`
(operand semantics, b06); `[HIGH × OBSERVED]` (prime/iterate device disassembly).

### 3.2 The store side — `uus` accumulator → flush

The store-side mirror uses the valign register as a **write accumulator**, observed in the
same device function:

```asm
; ITERATE — store v31 INTO u1 accumulator, commit the now-complete 64-B aligned window;
;            CO-ISSUED with a load iterate (S0 store + S1 load in ONE bundle):
    { ivp_sa2nx8_ip  v31, u1, a2;  ivp_la2nx8_ip  v0, u0, a3 }
; FLUSH — commit the residual bytes still held in u1 (no vec operand):
    { ivp_sapos_fp   u1, a2; nop; nop }
```

```c
// SEED:  ivp_salign_i u, a, 0   — prime the store accumulator @a (store-side MALIGN).
// ITERATE: ivp_sa2nx8_ip v, u, a — rotate v's bytes INTO u, commit the complete aligned window.
void sa_iter(valign_t *u, const uint8_t v[64], const uint8_t **a, unsigned misalign) {
    uint8_t commit[64];
    rotate_in(commit, u->bytes, v, misalign);     // {accumulator || v} >> misalign -> 64-B window
    store_aligned_masked((uintptr_t)*a & ~0x3fu,  // 64-B align-down (§3.3)
                         commit, head_tail_mask(misalign));
    memcpy(u->bytes, v, 64);                       // carry the tail into the accumulator
    *a += 32;
}
// FLUSH: ivp_sapos_fp u, a — commit the residual; SAPOS has NO vec data operand.
void sa_flush(valign_t *u, const uint8_t *a) {
    store_aligned_masked((uintptr_t)a & ~0x3fu, u->bytes, tail_only_mask(a));
}
```

The accumulator `uus` is, for `SA*`/`SAPOS`, a **read-modify-write** operand (USE/DEF @10,
§5.3): it is not in the iclass `ARG_LIST`, but the device assembler **requires** the u-register
to be spelled (the 2-operand form is rejected). `[HIGH × CARRIED]` (b07);
`[HIGH × OBSERVED]` (device disassembly).

> **NOTE — drop the flush and you corrupt the tail.** Because each `SA*` commits the window
> for the *previous* logical position and carries the new tail into the accumulator, the final
> partial bytes never reach memory unless `SAPOS_FP` flushes the accumulator. A reimplementer
> that emits the iterate loop without the flush silently loses the last `<64 - misalign` bytes
> of every unaligned vector store. `[HIGH × CARRIED]`

### 3.3 The 64-byte align-down + byte-disables (the masked-store mechanic)

Every aligning store commits a **64-byte aligned window** even when the logical store is
misaligned. The mechanic (carried, re-stated):

```c
uintptr_t aligned  = eff_addr & ~0x3fu;          // 64-B align-down (DATA_WIDTH = 64)
unsigned  addrsel  = eff_addr &  0x3fu;           // the misalignment offset
uint64_t  disables = is_T_store ? per_lane(vbool)               // predicated store
                   : is_SA      ? misalign_mask(addrsel)         // aligning store head/tail
                   :              all_enable;                    // plain aligned store
// PRE-VALIDATE (watchpoint / MPU) WITHOUT committing — returns a merged kill word:
uint64_t kill = aligned_store_check(ctx, aligned, 0x40, ring);          // store-check callback
// COMMIT the masked 64-byte window:
aligned_store(ctx, aligned, 0x40, src, disables, ring, &kill);          // store callback
```

The byte-`disables` (`VectorStoreByteDisable`) mask the head/tail bytes the 64-byte window
covers but the logical store does not. So an unaligned vector store **never** issues a
sub-64-byte or unaligned hardware access — it always commits a full aligned 64-byte window
with the unwanted bytes disabled. This is the store-side complement of the §3.1 load funnel,
and the reason §2.4's "no unaligned store hardware" is not a limitation. `[HIGH × CARRIED]` (b07).

---

## 4. The AXI / ACE-Lite master interface + write buffer + iDMA

### 4.1 The outbound bus

```
XCHAL_HAVE_PIF        = 1     // system.h:353  any outbound bus present
XCHAL_HAVE_AXI        = 1     // system.h:355  AXI bus master
XCHAL_HAVE_AXI_ECC    = 1     // system.h:356  ECC on the AXI bus
XCHAL_HAVE_ACELITE    = 1     // system.h:357  ACE-Lite (coherent-lite) master
XCHAL_HAVE_PIF_WR_RESP = 1    // system.h:359  PIF/AXI write response (writes acknowledged)
XCHAL_HAVE_PIF_REQ_ATTR= 0    // system.h:360  no PIF request-attribute extension
```

`[HIGH × OBSERVED]`. The Q7 is an **AXI/ACE-Lite bus master with ECC, write-response-enabled.**
This is the **only** data path off-core — there is no second compute port. Every access to
system RAM, the I/O blocks, SBUF, or HBM is an AXI master transaction. The data bus is 64
bytes wide (`XCHAL_DATA_WIDTH = 64`), matching the vector access granule.

### 4.2 The write buffer (store decoupling)

```
XCHAL_NUM_WRITEBUFFER_ENTRIES = 8     // core-isa.h:227
XCHAL_DCACHE_SIZE             = 0      // core-isa.h:298  no D-cache
```

`[HIGH × OBSERVED]`. The data side is **local DataRAM + an 8-entry write buffer** — nothing
else. An outbound store retires into the write buffer and **drains asynchronously to AXI**, so
the pipe does not stall on AXI write latency: a store retires at @5 (scalar) / @11 (vector)
into the buffer (§5). The 8 entries bound the in-flight write depth. The decoupling reading is
the standard write-buffer role, `[HIGH × INFERRED]` operationally.

### 4.3 The `axi2sram` bridge — how the Q7 reaches SBUF

The Q7 has **no dedicated SBUF compute port.** It reaches the 32-MiB SBUF as memory-mapped AXI
through the `axi2sram` bridge:

* SBUF appears at an AXI aperture (`aws_hal_stpb_get_axi_offset(tpb_idx, 0)`, size `0x2000000`
  = 32 MiB). On-core the Q7 sees it through the pinned 64-MiB NX window @ `0x80000000` (§1.3)
  → SoC `STATE_BUF [0x2000000000,0x2004000000)`.
* A GPSIMD load/store to SBUF is an AXI transaction that enters SBUF through the `axi2sram`
  bridge and arbitrates as the **TDM DMA slot** (1/8 of the SBUF port, behind the 2-stage SBUF
  arbiter) — it is the DMA share, **not** a dedicated compute-client port.

The SBUF linear addressing the Q7 uses (read byte-exact this pass from the shipped `sunda`
arch-isa header `aws_neuron_isa_tpb_common.h`):

```c
// sunda (NC-v2) TPB geometry — the shipped customop arch-isa header.
STATE_BUF_OFFSET            = 0x00000000;   PSUM_BUF_OFFSET            = 0x02000000;  // :68-69
STATE_BUF_NUM_PARTITIONS    = 128;          PSUM_BUF_NUM_PARTITIONS    = 128;          // :39,41
STATE_BUF_WORD_SIZE         = 8;                                                       // :40
STATE_BUF_PARTITION_SIZE    = 256 * 1024;   PSUM_BUF_PARTITION_SIZE    = 32 * 1024;    // :42,44
STATE_BUF_PARTITION_ACTIVE_SIZE = 192*1024; PSUM_BUF_PARTITION_ACTIVE_SIZE = 16*1024;  // :43,45
TPB_PARTITION_ADDR_MASK     = 0x1fffffff;                                              // :48 (29-bit)

bool addr_in_sbuf(uint32_t a) { return (a & TPB_PARTITION_ADDR_MASK) < PSUM_BUF_OFFSET; }
uint32_t sbuf_partition(uint32_t a) { return (a - STATE_BUF_OFFSET) / STATE_BUF_PARTITION_SIZE; }
uint32_t sbuf_byte_off (uint32_t a) { return (a - STATE_BUF_OFFSET) % STATE_BUF_PARTITION_SIZE; }
```

A GPSIMD core owns 16 contiguous partitions (`16c .. 16c+16`). **PSUM** (`[0x2000000,…)`) has
**no AXI aperture and no NX window** → it is **structurally unreachable** by the Q7. `[HIGH ×
OBSERVED]` (the SBUF/PSUM constants, sunda header); `[HIGH × CARRIED]` (aperture/bridge/TDM
join); `[MED × CARRIED]` (the 1/8 TDM-slot arbitration detail).

> **CORRECTION — the SBUF 8-byte word is OBSERVED here, not merely carried.** Earlier reports
> carried `STATE_BUF_WORD_SIZE = 8` from the DMA Part. This pass reads it byte-exact out of the
> shipped `sunda` arch-isa header (`aws_neuron_isa_tpb_common.h:40`), alongside the 29-bit
> `TPB_PARTITION_ADDR_MASK = 0x1fffffff` and `PSUM_BUF_OFFSET = 0x2000000`. Provenance upgraded
> `CARRIED → OBSERVED`.

### 4.4 The iDMA engine — a distinct 128-bit port

```
XCHAL_HAVE_IDMA             = 1     // core-isa.h:433
XCHAL_IDMA_NUM_CHANNELS     = 1     // core-isa.h:434
XCHAL_IDMA_ADDR_WIDTH       = 32    // core-isa.h:435
XCHAL_IDMA_DATA_WIDTH       = 128   // core-isa.h:436  16 bytes/beat
XCHAL_IDMA_DESC_SIZE        = 64    // core-isa.h:437  max descriptor 64 bytes
XCHAL_IDMA_MAX_OUTSTANDING_REQ = 32 // core-isa.h:438  up to 32 outstanding requests
XCHAL_IDMA_HAVE_REORDERBUF  = 0     // core-isa.h:439
XCHAL_IDMA_HAVE_TRANSPOSE   = 0     // core-isa.h:440
XCHAL_IDMA_NUM_AXI2AXI_CHAN = 0     // core-isa.h:442
// done/err interrupts:
XCHAL_IDMA_CH0_DONE_INTERRUPT = 35  // core-isa.h:445
XCHAL_IDMA_CH0_ERR_INTERRUPT  = 36  // core-isa.h:446
```

`[HIGH × OBSERVED]`. The on-core iDMA is a **separate 128-bit-datapath block** that targets
the **DataRAM** (`XCHAL_DATARAM0_HAVE_IDMA = 1`; the **IRAM is not an iDMA target**,
`XCHAL_INSTRAM0_HAVE_IDMA = 0`) with up to 32 outstanding requests. It is distinct from the
SBUF-side DMA engines (which stage into SBUF, 32-byte beats): the on-core iDMA stages into the
64-KiB DataRAM. The iDMA's 32-outstanding is the *on-core block*; the **AXI/PIF master's own
outstanding-read depth is not a named config constant** — it is SoC-fabric-dependent
(the AXI slave / fabric sets it). The write side is bounded by the 8-entry write buffer.

### 4.5 The instruction side (for completeness, not the data path)

```
I-CACHE: 16384 B = 64 sets × 4 ways × 64-B line   (SETWIDTH=6, WAYS=4, LINESIZE=64; core-isa.h:365,369,291)
         ECC_WIDTH = 4 (core-isa.h:379), ACCESS_SIZE = 32 (core-isa.h:383), dyn-enable via MEMCTL.
         INST_FETCH_WIDTH = 32 B feeds the 256-bit instbuf.  PREFETCH_ENTRIES = 8 (core-isa.h:307).
NO D-CACHE (DCACHE_SIZE = 0).
```

A miss refills a 64-byte line as two 32-byte AXI beats + the fabric round-trip — a front-end
stall, **not** in the register-latency schedule (§5). `[HIGH × OBSERVED]` geometry; the miss
penalty is `[MED × INFERRED]`.

#### Outstanding-transaction summary

| path | outstanding | provenance |
| --- | --- | --- |
| on-core iDMA | 32 requests | `XCHAL_IDMA_MAX_OUTSTANDING_REQ = 32` `[HIGH × OBSERVED]` |
| AXI-master writes | bounded by 8-entry write buffer | `XCHAL_NUM_WRITEBUFFER_ENTRIES = 8` `[HIGH × OBSERVED]` |
| AXI-master read-outstanding depth | **not a config constant** → SoC-fabric dependent | `[MED]` — not derivable from the config |

---

## 5. Load/store → pipeline timing

The data-side pipe is the deep vector memory port. The cycle stages are read **directly** out
of `libcas-core.so`: each `_issue`/`_stall` function calls `opnd_sem_<file>_addr(<reg field>)`
to get the operand's register index, then `mov $0x<stage>,%esi; call *%r12` to register a
use/def hazard at that **stage**. The immediate `$0x<stage>` is the pipeline stage. The load
and store pipes are `stage0..stage11` (12 stages; the max `…_inst_stage11` symbol exists for
both `IVP_LV2NX8_I` and `IVP_SV2NX8_I` across every format). `[HIGH × OBSERVED]`

### 5.1 Scalar load/store (the core 5-stage pipe)

The scalar d-side is a 5-stage pipe (`XCHAL_DATA_PIPE_DELAY = 1`, `core-isa.h:230`, comment
"1 = 5-stage"; ISS params `nBStage=3 nEStage=4 nMStage=5 nWStage=6`):

```
L32I / L16UI / L8UI / L32R:  ars (AGU base) USE@1; ScalarMemDataIn32 USE@5; art (result) DEF@5.
   => scalar LOAD result @stage 5 (the M-stage data return).  Dependent ALU reads @4 -> 1-cycle load-use.
S32I:                        VAddrBase DEF@1; art (store data) USE@5; ScalarMemDataOut32 DEF@5.
   => scalar STORE egresses @5 into the 8-entry write buffer.
L32EX / S32EX (exclusive):   + XTSYNC barrier @6 (the exclusive-monitor sync).
```

`[HIGH × CARRIED]` (DX-ISA ld_st). A scalar misaligned `L32I`/`S32I` traps before any of this
(§2.4).

### 5.2 Vector load — port @9, vec dest @10, valign @9

Read out of `F0_F0_S0_LdSt_4_inst_IVP_LA2NX8_IP_{issue,stall}` (`@0x118a360` / `@0x11b32d0`)
and the plain-load `…IVP_LSNX16_IP_issue` (`@0x11892c0`):

```
nx_Load_0_interface          // the load memory-port hazard (vs nx_Store_0_interface for stores)
ars (AR base)        USE@1, DEF@1   // opnd_sem_AR_addr -> mov $0x1   (post-increment lands EARLY)
opnd_..._uul (valign)USE@9, DEF@9   // opnd_sem_valign_addr -> mov $0x9   <-- valign @9 (load side)
opnd_..._vr (vec)    DEF@10         // opnd_sem_vec_addr   -> mov $0xa   <-- loaded vector @10
CPENABLE             USE@3          // my_CPENABLE_stall -> mov $0x3   (cp gate; squashes BEFORE @9)
```

```c
// The hazard model literally encodes the stage as the call argument.  Reproduced from the
// LA2NX8_IP stall body (libcas-core.so @0x11b3340):
int la2nx8_stall(iss_t *s, decoded_t *d) {
    if (nx_Load_0_interface(s->port, /*slot*/1)) return 1;           // load-port occupancy
    if (read_stall(s->tbl, /*stage*/1,  opnd_sem_AR_addr(d->ar)))     return 1;  // AR base @1
    if (read_stall(s->tbl, /*stage*/9,  opnd_sem_valign_addr(d->uul)))return 1;  // uul @9
    if (read_stall(s->tbl, /*stage*/3,  /*CPENABLE*/0))               return 1;  // cp1 gate @3
    /* REV8AR, WB_P, WB_N, InOCDMode, MS_DISPST, WB_S writeback interlocks ... */
    return 0;
}
```

`[HIGH × OBSERVED]`. The architectural vec result lands **@10** — exactly the vec-regfile read
port — so a dependent vector op in the **next** bundle forwards with **0 bubble** (same-cycle
is a true RAW dependency). The valign `uul` update @9 lands **one stage before** the vec data,
so an aligning-load chain (§3.1) feeds the next `LA*`'s `uul` input with no extra bubble. AR
post-increment @1 lets a tight load loop advance its pointer every cycle without stalling.

### 5.3 Vector store — data @10, valign @10, commit @11

Read out of `…IVP_SA2NX8_IP_{issue,stall}` (`@0x11894f0` / `@0x11b33f0`):

```
nx_Store_0_interface         // the SINGLE store memory-port hazard (no nx_Store_1_interface)
ars (AR base)        USE@1, DEF@1   // mov $0x1   (post-update EARLY)
vr / vbr (store data)USE@10         // opnd_sem_vec_addr  -> mov $0xa   <-- store data sampled @10
uus (valign accum)   USE@10, DEF@10 // opnd_sem_valign_addr -> mov $0xa  <-- uus RMW @10 (NOT @9!)
CPENABLE             USE@3          // mov $0x3
VectorMemDataOut512  DEF@11         // commit driven inside nx_Store_0_interface  <-- @11
VectorStoreByteDisable DEF@11       // byte-mask resolved @11
```

`[HIGH × OBSERVED]`. The store **commits @stage 11** — one stage later than the load's
result-land @10 — because the data operand is sampled @10 and the masked 64-byte window is
driven out @11. The valign accumulator `uus` is read-modify-written **@10** (one stage *later*
than the load-side `uul` @9 — the load-vs-store asymmetry is real and OBSERVED), so an
aligning-store chain (§3.2) feeds the next `SA*`'s accumulator with no bubble. AR
post-increment @1 → bubble-free store loop.

> **NOTE — two consistent stage views.** The **hazard/dependency** model above exposes the
> load-side valign at **@9** (the forwarding-availability stage). The **file-port** model in
> [regfile-ports.md](./regfile-ports.md) exposes the valign file as **read @10 / write @12**
> (the architectural register-file port). These are not in conflict: `@9` is the stage at which
> the new `uul` residue is *available to forward* to the next `LA*`, one stage ahead of the
> @10 file read. Both are read out of `libcas-core.so`; cite the **@9** hazard stage when
> reasoning about back-to-back aligning-load chains and the **@10/@12** file ports when
> reasoning about read/write-port pressure. `[HIGH × OBSERVED]` (both)

### 5.4 The timing summary (one line per access type)

| access | result / commit | consequence |
| --- | --- | --- |
| scalar load | result @5 | 1-cycle load-use (dependent ALU @4) |
| scalar store | egress @5 | into the 8-entry write buffer |
| **vector load** | port hazard @9, **vec result @10** | 0-bubble forward to next bundle |
| **vector store** | src @10, **commit @11** | commit-late-by-one vs the load |
| valign `uul` / `uus` | load @9 / store @10 | bubble-free aligning chains |
| AR post-increment | @1 (both) | bubble-free pointer loops |

The SBUF/HBM AXI round-trip **adds** to these on-core stages (SoC-fabric-dependent + the SBUF
arbiter stall, §4.3) — the on-core schedule models only the pipe stages. `[HIGH × OBSERVED]`
on-core; `[MED × CARRIED]` the off-core add.

---

## 6. The consolidated access-granularity table (the deliverable)

### 6.1 Granules by path

| path / structure | granule | source const | axis |
| --- | --- | --- | --- |
| Q7 compute data bus | 64 B (512 b) | `XCHAL_DATA_WIDTH = 64` | on-core L/S |
| vector register / transfer | 64 B (512 b) | vec 512-b; `MemDataIs512Bits` | datapath |
| natural-access / align-down | 64 B | `eff_addr & ~0x3f` | aligning L/S |
| scalar load/store | 1 / 2 / 4 B | `L8/L16/L32` (must be aligned) | scalar pipe |
| SBUF SRAM word (ECC unit) | 8 B (64 b) | `STATE_BUF_WORD_SIZE = 8` | SBUF SRAM |
| on-core iDMA beat | 16 B (128 b) | `XCHAL_IDMA_DATA_WIDTH = 128` | iDMA port |
| SBUF DMA-engine beat | 32 B (256 b) | `DMA_ENGINE_DATA_WIDTH = 32` | SBUF DMA |
| I-cache access / i-fetch | 32 B (256 b) | `ICACHE_ACCESS_SIZE = 32` | i-side |
| I-cache line | 64 B | `ICACHE_LINESIZE = 64` | i-side |
| AXI write-buffer depth | 8 entries | `WRITEBUFFER_ENTRIES = 8` | AXI master |

### 6.2 Transfer widths a single vector L/S can move (`MemDataIs<N>Bits`)

| width | used by | lanes (this 512-b config) |
| --- | --- | --- |
| 8 b | element splat/select | 1 element → 64 lanes (splat) |
| 16 b | `LSNX16` / `LB*` bool / `SBN_2` | 1 i16 / packed pred 16 b |
| 32 b | `LSN_2X32` tile / `LBN` / `SBN` | packed pred 32 b |
| 64 b | `LSN_4X64` tile / `LB2N` / `SB2N` | packed pred 64 b |
| 128 b | `LSN_8X128` tile (16-byte row) | 8 i16 |
| 256 b | `LVNX8` / `LSN_16X256` half / `SVNX8` half | half vector |
| 512 b | `LV2NX8` / `LA*`/`SA*` aligning / `L2A*` paired | full 64-byte vector |

`[HIGH × CARRIED]` (b06/b07). The immediate is pre-scaled by the granule, so `imm=1` steps one
row.

### 6.3 Ports-per-bundle (the dual-LSU issue ceiling)

**≤ 2 memory ops/bundle (S0 + S1); of those ≤ 1 STORE (S0 only).** F4 = 2 LOADS (S0_Ld +
S1_Ld). N0/N1 = 1 memory op. `L2A*` paired = 2×512 b/instr using both ports in one op. The
canonical co-issue, observed in device code:

```asm
; S0 store + S1 load in ONE FLIX bundle (encoding 02ac6052...5f, libneuroncustomop.a memcpy):
    { ivp_sa2nx8_ip  v31, u1, a2;  ivp_la2nx8_ip  v0, u0, a3 }
; aligning store+load (variable-bound) in one bundle:
    { ivp_sav2nx8_xp v0, u1, a2, a10;  ivp_lav2nx8_xp v31, u0, a3, a11 }
```

`[HIGH × OBSERVED]`.

---

## 7. Cross-check / reconciliation

| claim | this page (header / DLL) | prior source | verdict |
| --- | --- | --- | --- |
| 2 LSU, unified | `NUM_LOADSTORE_UNITS=2`, `UNIFIED=1` | DX-HW-01 §4 | **CONFIRMED** |
| 2 load ports, 1 store port | `nx_Load_{0,1}_interface` + `nx_Store_0_interface`; no `Store_1` | DX-HW-03 §3.1 | **CONFIRMED (stronger)** |
| stores S0-only | 0 distinct stores in S1 (0/18 CAS, 0/66 ISA) | DX-HW-03 §3.1 | **CONFIRMED** |
| F4 = dual-load | `F4 S0_Ld + S1_Ld` | DX-HW-03 §4.2 | **CONFIRMED** |
| 64-byte data bus | `DATA_WIDTH=64` | DX-HW-01 §2 | **CONFIRMED** |
| 8-byte SBUF word | `STATE_BUF_WORD_SIZE=8` (sunda hdr) | DX-DMA-06 §1.3 | **CONFIRMED (now OBSERVED)** |
| scalar unaligned traps | `UNALIGNED_*_EXC=1, HW=0` | DX-HW-01 §4 | **CONFIRMED** |
| valign = unaligned vector mech | `uul`/`uus` prime/iterate/flush (device disasm) | DX-ISA-06/07 | **CONSOLIDATED** |
| 64-B align-down + disables | `eff & ~0x3f` + `VectorStoreByteDisable` | DX-ISA-07 §4 | **CONSOLIDATED** |
| IRAM 0x0 / DataRAM 0x80000 / 4 banks | `INSTRAM0/DATARAM0`, `BANKS=4` | DX-HW-01 §2 | **CONFIRMED** |
| system RAM 1 GiB @0x100000 | `XSHAL_RAM` | DX-HW-01 §2 | **CONFIRMED** |
| IOBLOCK 224 MiB each | `0x0E000000` | DX-HW-01 §2 | **CONFIRMED exact** |
| AXI/ACE-Lite ECC master | `AXI/ACELITE/AXI_ECC=1` | DX-HW-01 §2 | **CONFIRMED** |
| write buffer 8 entries | `WRITEBUFFER=8` | DX-HW-01 §2 | **CONFIRMED** |
| iDMA 128 b / desc 64 / outstanding 32 | `IDMA_*` | DX-HW-01 §3 | **CONFIRMED** |
| PSUM unreachable | no AXI aperture / no NX window | DX-DMA-06/SEC-06 | **CONSISTENT** |
| load port@9 / vec@10 / store commit@11 | `mov $0x9/$0xa/$0xb` in `libcas-core.so` | DX-HW-03 §5 / [pipeline-timing](./pipeline-timing.md) | **CONFIRMED** |

**Open reconciliation (logged for the per-Part reconcile pass):** the load-side valign stage
is exposed as **@9** by the hazard model (this page, observed in `LA2NX8_IP`) and as **@10
read / @12 write** by the file-port model ([regfile-ports.md §3](./regfile-ports.md), and b06
"valign read @10/write @12"). These are the forwarding-availability stage vs. the file read
port; both OBSERVED, reconciled in §5.3's NOTE. The only **not-derivable** item is the
AXI-master read-outstanding depth (SoC-fabric dependent), flagged `[MED]`.

> **NOTE — 5-stage scalar vs 12-stage vector pipe.** `XCHAL_DATA_PIPE_DELAY = 1` selects the
> 5-stage scalar d-side (B3/E4/M5/W6 in the ISS params), while the vector memory port runs the
> deep `stage0..stage11` schedule (load result @10, store commit @11). The "7-stage" framing in
> some prior notes counts the FLIX front-end stages; the **register-latency** schedule that
> matters for software is the 5-stage scalar / 12-stage vector split above. `[HIGH × OBSERVED]`

---

## 8. Provenance index

| fact | symbol / file | addr / value |
| --- | --- | --- |
| LSU count / unified | `XCHAL_NUM_LOADSTORE_UNITS`, `XCHAL_UNIFIED_LOADSTORE` | `2`, `1` (`core-isa.h:226,240`) |
| 2 load + 1 store port | `nx_Load_0_interface`, `nx_Load_1_interface`, `nx_Store_0_interface` | imported (U) by `libcas-core.so`; no `nx_Store_1_interface` |
| S0/S1 op-class slots | `…S0_{Ld,LdSt,LdStALU}…`, `…S1_{Ld,ALU}…` `_inst_…_{issue,stall}` | 1795 stall + 2149 issue fns, `libcas-core.so` |
| stage encoders | `opnd_sem_{AR,vec,valign,vbool,wvec,gvr}_addr` | `@0x17a9f20…@0x17aa2c0` (`& 0x3` valign, `& 0x1f` vec) |
| issue/stall dispatch | `dll_get_issue_functions`, `dll_get_stall_functions`, `dll_set_tie_stall_eval` | `@0x17aa340`, `@0x17aa2e0`, `@0x17aa2f0` |
| load stages | `…IVP_LA2NX8_IP_{issue,stall}` | `@0x118a360` / `@0x11b32d0`: AR@1, valign@9, vec@10 |
| store stages | `…IVP_SA2NX8_IP_{issue,stall}` | `@0x11894f0` / `@0x11b33f0`: AR@1, valign@10, vec@10, commit @11 |
| 12-stage L/S pipe | `…IVP_{LV2NX8_I,SV2NX8_I}_inst_stage0..stage11` | max `…_stage11` present, all formats |
| IRAM / DataRAM | `XCHAL_INSTRAM0_*`, `XCHAL_DATARAM0_*` | `0x0`/64 KiB; `0x80000`/64 KiB/4 banks (`core-isa.h:410-424`) |
| system RAM / IOBLOCK / bypass / SimIO | `XSHAL_RAM`, `XSHAL_IOBLOCK_*`, `XSHAL_RAM_BYPASS`, `XSHAL_SIMIO` | `0x100000`/1 GiB; `0x70000000`/`0x90000000`/224 MiB; `0xA0000000`/512 MiB; `0xC0000000`/512 MiB (`system.h:75-114`) |
| reset vectors | `XCHAL_RESET_VECTOR0/1_VADDR`, `XCHAL_VECBASE_RESET_VADDR` | `0x0` / `0x100000` / `0x0` (`core-isa.h:736,740,742`) |
| AXI / ACE-Lite / ECC / wr-resp | `XCHAL_HAVE_{PIF,AXI,AXI_ECC,ACELITE,PIF_WR_RESP}` | all `1` (`system.h:353-359`) |
| write buffer / no D-cache | `XCHAL_NUM_WRITEBUFFER_ENTRIES`, `XCHAL_DCACHE_SIZE` | `8`, `0` (`core-isa.h:227,298`) |
| iDMA | `XCHAL_IDMA_{DATA_WIDTH,DESC_SIZE,MAX_OUTSTANDING_REQ}`, `…CH0_{DONE,ERR}_INTERRUPT` | `128`/`64`/`32`, `35`/`36` (`core-isa.h:436-446`) |
| I-cache | `XCHAL_ICACHE_{SIZE,LINESIZE,WAYS,ACCESS_SIZE,ECC_WIDTH}` | `16384`/`64`/`4`/`32`/`4` (`core-isa.h:296,291,369,383,379`) |
| no MMU / 16-entry MPU | `XCHAL_HAVE_TLBS`, `XCHAL_HAVE_PTP_MMU`, `XCHAL_HAVE_MPU`, `XCHAL_MPU_ENTRIES` | `0`/`0`/`1`/`16` (`core-isa.h:781,787,800-801`) |
| SBUF/PSUM geometry | `NEURON_ISA_TPB_{STATE_BUF,PSUM_BUF}_*` | offsets `0x0`/`0x2000000`; word `8`; mask `0x1fffffff`; 128 partitions (`aws_neuron_isa_tpb_common.h:39-69`) |
| device disasm | `xtensa-elf-objdump XTENSA_CORE=ncore2gp` on `libneuroncustomop.a` | `ivp_la_pp` / `la2nx8_ip` / `sa2nx8_ip` / `sapos_fp` bundles |

> All addresses are in `libcas-core.so` `.text`/`.rodata`, where VMA == file offset. Only
> `.data`/`.data.rel.ro` carry the **+0x200000** VMA−fileoffset delta
> (`readelf -SW libcas-core.so`: `.data.rel.ro` VMA `0x2070900` − file `0x1e70900` = `0x200000`;
> `.data` VMA `0x2280ed8` − file `0x2080ed8` = `0x200000`) — irrelevant to the `.text`
> `_issue`/`_stall` disassembly here, but required before any `xxd`/`objdump` on a
> `.data`-resident struct in this DLL. `[HIGH × OBSERVED]`
