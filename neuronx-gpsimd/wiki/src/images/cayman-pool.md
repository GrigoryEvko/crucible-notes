# CAYMAN × POOL image (dual-dispatch)

The **CAYMAN × POOL** firmware image is the baseline **central general-compute engine**
(`engine_idx = 2`) for the NC-v3 generation, and it is the **only** one of the five NX engines
that ships **two distinct firmware cores**. Every Mariana / Mariana+ / Maverick / Sunda POOL page
([mariana-pool.md](./mariana-pool.md), [sunda-pool.md](./sunda-pool.md)) references this page as
its diff baseline. Unlike its siblings [× ACT](./cayman-act.md), [× DVE](./cayman-dve.md),
[× PE](./cayman-pe.md) and [× SP](./cayman-sp.md) — each a *single* `cayman/seq/` SEQ-dispatch
core — POOL is a **two-core fetch-decode → execute pipeline**:

1. **`NX_POOL`** — the NX-class SEQ sequencer (`'S:'` log dialect, reset `06 76 00 00`). It is the
   *same* `cayman/seq/` chassis as ACT/DVE/PE/SP, recompiled with the **richest** general-compute
   handler subset (**41 distinct** `S:` handlers — more than PE's 24 and ACT's 26; DVE has 53 but
   those are narrow vector variants, whereas POOL spans the widest distinct primitive set).
2. **`Q7_POOL`** — a *separate* per-pool-core compute engine (`'P%i:'` log dialect, reset
   `06 7f 00 00`). It runs the **`kernel_info_table` linear scan** keyed by the packed
   `(opcode<<24)|(spec<<16)` 4-byte key, then `callx8`s the tensor kernel. Its kernels live in the
   four `EXTISA_n` relocatable Xtensa ELFs and the flat Q7 IRAM.

The two cores are bridged by **one** POOL-exclusive SEQ handler: **`0xF0 = ExtendedInst`**. That
single handler — present in `NX_POOL`, **absent** from ACT/DVE/PE/SP (5-way set-diff) — is why
*only* POOL has the dual-dispatch. This page carves **both** core image sets byte-exact from
`libnrtucode_internal.so`, reproduces **both** dispatch loops as annotated C, reconciles the `0xF0`
escape across the two cores, enumerates the 41-handler roster and the POOL compute-kernel list, and
pins the cross-engine code / PROF sharing.

The decode/lookup mechanics of each path are documented in depth in
[POOL Engine Dispatch](../firmware/pool/pool-dispatch.md),
[POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md),
[kernel_info_table Binary Layout](../firmware/pool/kernel-info-table.md) and
[External-Library / Prelink Loader](../firmware/pool/external-lib-loader.md); this page is the
**CAYMAN image-level ground truth** those pages diff against.

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve from
`libnrtucode_internal.so` (sha256 `b7c67e89…`) and decoded with the shipped `ncore2gp`
`xtensa-elf-objdump`. The page default is `[HIGH/OBSERVED]`; claims that depart from it
carry an explicit tag.

> **NOTE — the objects used.** Container:
> `…/custom_op/c10/lib/libnrtucode_internal.so` (sha256
> `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`, ELF64 x86-64 DYN, **not
> stripped**). The first R `LOAD` is the identity map (`off 0x0 == vaddr 0x0`, `filesz 0x9af194`,
> per `readelf -lW`), so each `<NAME>_get.data` accessor address is
> **simultaneously** the `.rodata` VA and the file offset of its blob — carve =
> `so[ptr : ptr+size]`. All 46 POOL blob VAs (`0xa0600…0x302880`) fall inside this R `LOAD`.
> Disassembler:
> `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump`
> (GNU Binutils 2.34.20200201, `XTENSA_CORE=ncore2gp`, ConfigName `Xm_ncore2gp`, uarch Cairo,
> Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`, `IsaMaxInstructionSize=32` FLIX/VLIW).

> **GOTCHA — carve the blobs, not the stubs.** The getter accessor lives in `.text` (e.g.
> `CAYMAN_NX_POOL_DEBUG_IRAM_get` at `.text` VA `0x9b36a0`), and `.text` has a **`0x2000`
> VA − file-offset delta** (`readelf -SW`: `.text` VA `0x9b01a0`, file `0x9af1a0`). A raw `dd`
> aimed at the **stub** VA therefore reads garbage. The blob VAs in `.rodata` are identity-mapped
> (`.rodata` VA `0x46b0` == file `0x46b0`), so the *carved blobs* are correct as long as you carve
> at the `IMG-PTR` (the `.data` accessor address), never the stub.

---

## 1. The headline

1. **POOL is the only engine with two cores → the only one with a dual-dispatch.** The 46 POOL
   getters split into a **14-getter `NX_POOL` (SEQ)** family and a **32-getter `Q7_POOL`
   (compute)** family. The byte-level proof of two cores: `NX_POOL` IRAM head is `06 76 00 00`
   (`j 0x1dc`), shared with ACT/DVE/PE/SP; `Q7_POOL` IRAM head is `06 7f 00 00` (`j 0x200`), a
   **distinct** reset trampoline. Both converge on `enter_run @0x90`.
2. **`engine_idx = 2`.** The corpus CSR enum is `PE=0 ACT=1 POOL=2 DVE=3 SP=4` (carried from the PE
   page's verified CSR enum); the Q7 boot-identity log
   `"P%i: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u"` is the
   self-naming string that emits this index at boot. `[HIGH/OBSERVED for the log;
   HIGH/CARRIED for the enum value]`
3. **The `0xF0 = ExtendedInst` SEQ handler is the bridge.** `NX_POOL` table slot for opcode `0xf0`
   (`@ DRAM 0x80814 + (0xf0−0x41)*4 = file 0xad0`) reads `90 31 00 00` = trampoline `0x3190` →
   `"S: ExtendedInst"`. On the Q7 side, opcode `0xf0` appears as **five** `kernel_info_table` rows
   differing only by the spec byte. **Same opcode, two cores** — reconciled in §4c.
4. **41-handler POOL roster, the richest distinct compute subset.** `18` shared-all-5 SEQ control
   handlers + `7` shared-with-DVE compute primitives + `16` POOL-only handlers (incl.
   `ExtendedInst`, `RandGetState`/`RandSetState`, `Sort`, `TensorGather`, `EmbeddingUpdate`,
   `SB2SB_Collective`).
5. **PROF is generic, code is private.** `NX_POOL` `PROF_CAM`/`PROF_TABLE` are **byte-identical**
   across ACT/DVE/PE/POOL (4/4, sha256 `8fd7e422` / `ce761f81`), but `NX_POOL` shares **no**
   IRAM/DRAM bytes with any sibling — each is a separately-compiled `cayman/seq/` build.

> **WALL.** `PeManageSeed (0x08)`, `LdweightsMX`, `MatmulMX` and the Mariana `ConvLutLoad (0xe4)`
> matmul-path micro-ops do **not** belong to CAYMAN POOL. POOL *does* carry a `ConvLutLoad` `S:`
> handler (§5c), but the Mariana weight-load / matmul family first ships on PE in v4 and is asserted
> nowhere in this CAYMAN image. `arch_id 36` is **INFERRED**. `[HIGH/CARRIED]`

---

## 2. The 46 image getters (instruction-exact)

`nm libnrtucode_internal.so | rg -c 'CAYMAN.*POOL.*_get$'` = **46**. Each getter is the canonical
4-instruction `(img-ptr, size)` stub disassembled from `.text 0x9b31a0…0x9b3c90`:

```text
CAYMAN_NX_POOL_DEBUG_IRAM_get @ .text 0x9b36a0:
  48 8d 05 79 dd 7f ff   lea  -0x802287(%rip),%rax   # 0x1b1420  (= IMG-PTR, .rodata identity)
  48 89 07               mov  %rax,(%rdi)
  48 c7 06 20 c8 01 00   movq $0x1c820,(%rsi)         # = SIZE
  c3                     ret
```

`28` getters carry real bytes; `18` are zero-size SRAM/EXTRAM boundary cursors (the `(ptr, 0)`
return at the start of the *next* blob — the same contiguous-layout aliasing as ACT/DVE/PE). Each
`IMG-PTR`/`SIZE` below matches [image-catalog-index.md](./image-catalog-index.md).

### 2a. `NX_POOL` (CLS = NX) — 14 getters (the SEQ sequencer)

| VARIANT | REGION | IMG-PTR (.rodata = file off) | SIZE | STATUS |
| --- | --- | --- | --- | --- |
| PERF | IRAM | `0x0a0600` | `0x17280` | REAL (SEQ code) |
| PERF | DRAM | `0x0b7880` | `0x03020` | REAL (SEQ data, no `S:`) |
| PERF | SRAM/EXTRAM | `0x0ba8a0` | `0` | EMPTY (boundary cursor) |
| TEST | IRAM | `0x11c920` | `0x16a00` | REAL |
| TEST | DRAM | `0x133320` | `0x03320` | REAL |
| TEST | SRAM/EXTRAM | `0x136640` | `0` | EMPTY |
| **DEBUG** | **IRAM** | `0x1b1420` | `0x1c820` | REAL (code; sha `8e4412b9`) |
| **DEBUG** | **DRAM** | `0x1cdc40` | `0x06f20` | REAL (data + 187 `S:` logs; sha `7bdf6ed7`) |
| DEBUG | SRAM/EXTRAM | `0x1d4b60` | `0` | EMPTY |
| **PROF** | **CAM** | `0x3094a0` | `0x00400` | REAL (HW-decode CAM) |
| **PROF** | **TABLE** | `0x3098a0` | `0x02000` | REAL (8 KiB profile table) |

### 2b. `Q7_POOL` (CLS = Q7) — 32 getters (compute + DKL + EXTISA)

| VARIANT | REGION | IMG-PTR | SIZE | STATUS |
| --- | --- | --- | --- | --- |
| PERF | IRAM | `0x1f4860` | `0x16360` | REAL (Q7 compute code) |
| PERF | DRAM | `0x20abc0` | `0x13200` | REAL (Q7 data) |
| TEST | IRAM | `0x21ddc0` | `0x17d60` | REAL |
| TEST | DRAM | `0x235b20` | `0x13500` | REAL |
| **DEBUG** | **IRAM** | `0x249020` | `0x1ea40` | REAL (code; sha `513a8a22`) |
| **DEBUG** | **DRAM** | `0x267a60` | `0x15d00` | REAL (data + 156 `P%i:` logs; sha `226f4254`) |
| DKL_PERF | IRAM | `0x27d760` | `0x10120` | REAL (dyn-kernel-load build) |
| DKL_PERF | DRAM | `0x28d880` | `0x13c00` | REAL |
| **DKL_DEBUG** | **IRAM** | `0x2a1480` | `0x13fc0` | REAL (+ 136 `P%i:` logs; sha `7e5e39ac`) |
| **DKL_DEBUG** | **DRAM** | `0x2b5440` | `0x16680` | REAL (sha `6c716c5b`) |
| DKL_TEST | IRAM | `0x2cbac0` | `0x10120` | REAL (**== DKL_PERF**, §6c) |
| DKL_TEST | DRAM | `0x2dbbe0` | `0x13c00` | REAL (**== DKL_PERF**) |
| PERF_EXTISA_0 | SO | `0x2ef7e0` | `0x0a260` | REAL (`EM_XTENSA` ELF; 17-entry table) |
| PERF_EXTISA_1 | SO | `0x2f9a60` | `0x00f5c` | REAL (ELF; 1-entry table) |
| PERF_EXTISA_2 | SO | `0x2fa9e0` | `0x01500` | REAL (ELF; 2-entry table) |
| PERF_EXTISA_3 | SO | `0x2fbf00` | `0x06974` | REAL (ELF; 9-entry table; cptc family) |
| PERF_EXTISA_{0–3} | JSON | (4 blobs) | `0x20` | REAL (dummy `{"dummy_message":"hello world"}`) |

> **QUIRK — `DKL_TEST` is `DKL_PERF`.** `cmp` confirms `Q7_DKL_PERF_IRAM == Q7_DKL_TEST_IRAM`
> (both sha `c05e05eb…`) and `Q7_DKL_PERF_DRAM == Q7_DKL_TEST_DRAM` (both `e98902ac…`). The DKL
> build has only a DEBUG-vs-release split; there is no separate TEST flavor.

> **QUIRK — EXTISA ships under PERF only.** The DEBUG/TEST Q7 builds carry **no** EXTISA getters;
> the `EXTISA_n` relocatable kernel containers are the **production custom-op** packaging. The
> DEBUG Q7 image still self-names every kernel via `P%i:` logs, which is the RE substrate.

### 2c. Carve provenance + byte-identity

Carve rule: `.rodata` file-offset == VA, so `blob = so[IMG-PTR : IMG-PTR+SIZE]` (`dd bs=1`). The 15
images carved + sha256'd (every digest matches the catalog and the firmware anchors):

| IMAGE | SIZE | sha256 |
| --- | --- | --- |
| `NX_DEBUG_IRAM` | `0x1c820` | `8e4412b9…ed70a` (== firmware `iram.bin` anchor) |
| `NX_DEBUG_DRAM` | `0x6f20` | `7bdf6ed7…16ecd` (== firmware `dram.bin` anchor) |
| `NX_PERF_IRAM` | `0x17280` | `9049bf8c…1e0cb` (== IMG-04/05 POOL PERF) |
| `NX_PROF_CAM` | `0x400` | `8fd7e422…a95f31` (== ACT/DVE/PE) |
| `NX_PROF_TABLE` | `0x2000` | `ce761f81…821cf1` (== ACT/DVE/PE) |
| `Q7_DEBUG_IRAM` | `0x1ea40` | `513a8a22…75cb` |
| `Q7_DEBUG_DRAM` | `0x15d00` | `226f4254…0128e` |
| `Q7_DKL_DEBUG_IRAM` | `0x13fc0` | `7e5e39ac…5231f` |
| `Q7_DKL_DEBUG_DRAM` | `0x16680` | `6c716c5b…fe066` |
| `Q7_DKL_PERF_IRAM` | `0x10120` | `c05e05eb…0c6ed` |
| `Q7_DKL_TEST_IRAM` | `0x10120` | `c05e05eb…0c6ed` (identical) |
| `Q7_EXTISA_0_SO` | `0xa260` | `910d41c3…b5527` (== `internal_CAYMAN_0.so`) |
| `Q7_EXTISA_1_SO` | `0xf5c` | `469c2413…d9193` |
| `Q7_EXTISA_2_SO` | `0x1500` | `efd06876…1c933` |
| `Q7_EXTISA_3_SO` | `0x6974` | `052ac31c…500c8` (== `internal_CAYMAN_3.so`) |

The static archive `libnrtucode.a` ships exactly 46 CAYMAN POOL members (`2 hwdecode_` + `44 img_`);
spot-reconciliation (`objcopy -O binary --only-section=.rodata` vs carve, `cmp`) was IDENTICAL for
all 12 members checked across both families + EXTISA + PROF + DKL. The internal `.so` getter blob
== the `.a` member `.rodata`.

---

## 3. Flat-image geometry + the two reset vectors

Three distinct packaging forms coexist in the CAYMAN POOL corpus.

**(A) `NX_POOL` IRAM/DRAM — flat device segments (no ELF magic):**

```text
IRAM head = 06 76 00 00 00 00  86 77 00 00 00 00        (the NX reset trampoline)
  0x000: 06 76 00   j 0x1dc       ; primary reset -> boot
  0x006: 86 77 00   j 0x1e8       ; secondary    -> halt
  0x1dc: const16 a0,0 ; const16 a0,0x90 ; jx a0   -> enter_run @0x90
  0x1e8: halt 0
DRAM head = 34 cb 99 60                                  (header word 0x6099cb34)
```

**(B) `Q7_POOL` IRAM/DRAM (incl. DKL) — flat device segments, DIFFERENT reset:**

```text
IRAM head = 06 7f 00 00 00 00  86 80 00 00 00 00        (the Q7 reset trampoline)
  0x000: 06 7f 00   j 0x200       ; primary reset -> boot
  0x006: 86 80 00   j 0x20c       ; secondary    -> halt
  0x200: const16 a0,0 ; const16 a0,0x90 ; jx a0   -> enter_run @0x90
  0x20c: halt 0
DRAM head = 34 cb 99 60                                  (same header word 0x6099cb34)
```

> **The signature of two cores.** `NX_POOL` boots via `j 0x1dc` (`06 76 00 00`, shared by all five
> NX engines); `Q7_POOL` boots via `j 0x200` (`06 7f 00 00`, the *distinct* Q7-compute reset).
> Both trampolines then converge on `enter_run @0x90`. The differing primary-jump bytes
> (`06 76` vs `06 7f`) are the byte-level proof that `NX_POOL` and `Q7_POOL` are separate cores
> in one engine cluster — verified by `xxd -l 12` on both IRAM carves.

**(C) `EXTISA_0..3` SO — real `EM_XTENSA` ELFs** (ELFCLASS32 LE, `e_machine = 94`, `ET_EXEC`,
dynamically linked, stripped — **not** flat). `EXTISA_0` section geometry (`readelf -SW`):

```text
[1]  .text             VA 0x01000000  off 0x100   size 0x6f1e
[7]  kernel_info_table VA 0x02000380  off 0x7400  size 0x88   (17 entries)
[8]  .globstruct       VA 0x02000408  off 0x7488  size 0x48
[9]  .bss              VA 0x02000450  off 0x74d0  size 0x3c
[33] .rela.got         (17 × R_XTENSA_RELATIVE funcVA relocs)
```

The four `EXTISA` ELFs are the **relocatable Q7 POOL kernel containers** — the
`kernel_info_table` + the kernel bodies. The flat Q7 PERF IRAM is the *baked* form of the same PERF
kernel corpus (a 256-byte window at EXTISA `.text+0x300` matches flat `Q7_PERF_IRAM @0x85e0` at
247/256 bytes — two packaging views differing only by load-time relocation deltas).
`[HIGH/OBSERVED for the geometry + the window match]`

**Disassembly proof** (shipped `ncore2gp` objdump): `NX_DEBUG_IRAM` decodes real windowed
ABI — **679 `entry` / 873 `retw`**, plus `const16`/`call8`/`callx8`/`jx`
and the IVP `ivp_scatterw` vector op; `Q7_DEBUG_IRAM` decodes **430 `entry` / 573 `retw`**. Both
cores carry a full FLIX vector compute datapath (the bundle-interleaved FLIX lanes are partly
desynced by the linear sweep — the documented disassembler limitation).

---

## 4. The two dispatch paths

POOL's dual-dispatch is a **two-core pipeline**, not two paths inside one core. The `NX_POOL`
sequencer decodes the `'S:'` instruction stream; for compute opcodes it hands `(opcode, spec)` to
the `Q7_POOL` core, which runs the `kernel_info_table` scan and the kernel. The `0xF0` SEQ handler
is the explicit escape that bridges them.

```text
NX_POOL (SEQ sequencer)                     Q7_POOL (per-core compute)
-----------------------------------------   -------------------------------------------------
fetches the 'S:' instruction stream         receives (opcode,spec) from the SEQ front-end
 ("Dispatch opcode=0x%x")                     ("In dispatch, CPU ID: %0d, got opcode 0x%x.")
decode opcode byte -> table[byte - 0x41]     LINEAR SCAN of kernel_info_table by packed key
 @ DRAM 0x80814 (178 entries, 55 real)       (spec<<16 | opcode<<24) -> callx8 funcVA -> kernel
4 hops: table->tramp->impl->thunk->execute() 1 hop:  table -> funcVA
miss -> "S: ErrorHandler : Bad Opcode"       partitions by get_cpu_id() across pool channels
source: cayman/seq/src/...                   miss -> "P%i: UNKNOWN OPCODE=0x%x"
                                             source: dispatch.hpp / dispatch_wrapper.hpp
```

Both cores share the `.globstruct` dispatcher state block — magic word `0x6099cb34`, the SAME
header word that heads every flat DRAM (`34 cb 99 60`) and the `EXTISA .globstruct @0x02000408`
(byte-identical). `NonzeroWithCount` appears on **both** (`S:` on NX, `P%i:`
on Q7) — the decode/execute pair across the two cores. `[strings + magic HIGH/OBSERVED; the
SEQ→Q7 handoff slot MED — direction HIGH (NX owns `fetch_cache_line`/`start_fill_siram`;
Q7 "got" the opcode), the slot is not pinned]`

### 4a. Path 1 — the `NX_POOL` SEQ 178-slot ASCII hub

Table base `DRAM 0x80814` (`file 0x814`). First words (`xxd -s 0x814 NX_DEBUG_DRAM.bin`):

```text
0x814: 74 30 00 00  98 31 00 00  9d 30 00 00  ad 30 00 00   ('A'=0x3074 Tensor-Tensor ; default 0x3198 ;
0x824: 64 30 00 00  ee 2f 00 00  f6 2f 00 00  98 31 00 00     'C'=0x309d Tensor-Scalar ; 'D'=0x30ad ;
                                                              'E'=0x3064 Pool ; 'F'=0x2fee ; 'G'=0x2ff6 ; default ...)
```

The **178-slot** size is *not* arbitrary: the index is `opcode_byte − 0x41`, and the highest
opcode handled is `0xf2` → index `0xf2 − 0x41 = 177` → **178 entries (0…177)** required:
`len(slots) = 178`, of which **55 are real handlers** and **123 are the default slot
`0x3198`** (`"S: Bad Opcode"` path). This is the **same 178-bound** as ACT/SEQ, and **larger** than
DVE's **170-bound** (DVE tops out at a lower opcode and uses dual tables `@0x814/0xabc`). The
`"S: Dispatch opcode=0x%x"` log is at `DRAM 0x80e38`. PERF relocates the clean indexed table to
`DRAM 0x80218`.

Reproduced as annotated C (the `NX_POOL` decode loop; the 4-hop indirection is the C++
`Handler::execute()` virtual chain):

```c
/* NX_POOL — the SEQ ASCII-opcode hub (cayman/seq/ chassis, 'S:' dialect).
 * Direct-indexed dispatch over a 178-slot u32 table of trampoline VAs. */
#define SEQ_TABLE   ((const uint32_t *)(DRAM + 0x80814))  /* 178 entries */
#define SEQ_BASE    0x41                                  /* 'A'; lowest opcode */
#define SEQ_BOUND   178                                   /* 0xf2-0x41 + 1      */
#define SEQ_DEFAULT 0x3198                                /* "S: Bad Opcode"    */

void nx_pool_dispatch(uint8_t opcode, decoded_inst_t *ins) {
    LOG("S: Dispatch opcode=0x%x", opcode);             /* @ DRAM 0x80e38 */
    unsigned idx = (unsigned)opcode - SEQ_BASE;          /* opcode_byte - 0x41 */
    if (idx >= SEQ_BOUND) goto bad;                      /* out of table range */

    uint32_t tramp = SEQ_TABLE[idx];                     /* slot = trampoline VA */
    if (tramp == SEQ_DEFAULT) goto bad;                  /* 123 of 178 slots */

    /* 4-hop chain: tramp -> impl -> handler thunk -> C++ Handler::execute().
     * For opcode 0xf0 this resolves to the ExtendedInst bridge (see 4c/5).   */
    ((seq_trampoline_t)tramp)(ins);                      /* tramp 0x3190 for 0xf0 */
    return;
bad:
    LOG("S: ErrorHandler : Bad Opcode(0x%x)", opcode);   /* hard-fault spin */
    hard_fault_spin();
}
```

### 4b. Path 2 — the `Q7_POOL` `kernel_info_table` back-end

The Q7 dispatcher (`'P%i:'`, `entry 0x01005610` in the EXTISA ELF) runs a **linear scan** over the
8-byte `kernel_info_table` records, matching the packed key. `EXTISA_0`'s 17 records decode
byte-exact (`xxd -s 0x7400 -l 0x88 Q7_EXTISA_0_SO.bin`):

| idx | opcode | spec | funcVA | role |
| --- | --- | --- | --- | --- |
| 0 | `0x7e` | 0 | `0x01000080` | `pool_iota` |
| 1 | `0x7c` | 0 | `0x010003f8` | `pool_cross_lane_reduce_arith` |
| 2 | `0x7d` | 0 | `0x01000410` | `pool_cross_lane_reduce_bitvec` |
| 3 | `0x45` | 0 | `0x01000b90` | `decode_pool` |
| 4 | `0x51` | 0 | `0x0100105c` | (tensor primitive) |
| 5 | `0x41` | 0 | `0x01000f1c` | `pool_tensor_tensor_arith` |
| **6** | **`0xf0`** | **0** | `0x01003370` | `ExtendedInstEngineNop` |
| **7** | **`0xf0`** | **1** | `0x01003380` | `pool_extended_inst_copy` |
| **8** | **`0xf0`** | **2** | `0x01003484` | `decode_extended_inst_tensor_tensor_arith` |
| **9** | **`0xf0`** | **4** | `0x010037a8` | Rand/Cptc band → `decode_pool` |
| **10** | **`0xf0`** | **3** | `0x01003a60` | Rand/Cptc band → `decode_pool` |
| 11 | `0x52` | 0 | `0x01003b40` | (tensor primitive) |
| 12 | `0x46` | 0 | `0x010040c0` | `pool_copy` |
| 13 | `0x47` | 0 | `0x01004160` | (tensor primitive) |
| 14 | `0xbe` | 0 | `0x01004204` | (tensor primitive) |
| 15 | `0xf2` | 0 | `0x0100484c` | `get_sequence_bounds` / `nonzero_with_count` |
| 16 | `0x7b` | 0 | `0x01004dc4` | `decode_tensor_dequantize` |

Record format: `{ u8 0; u8 0; u8 spec(+2); u8 opcode(+3); u32_le funcVA(+4) }`. The
native-LE `u32` of the first 4 bytes is therefore `(opcode<<24)|(spec<<16)` — the packed key. All
17 `funcVA`s carry an `R_XTENSA_RELATIVE` reloc.

```c
/* Q7_POOL — the kernel_info_table back-end (dispatch.hpp, 'P%i:' dialect).
 * Linear scan by the packed (opcode<<24)|(spec<<16) key -> callx8 funcVA. */
typedef struct { uint8_t z0, z1, spec, opcode; uint32_t funcVA; } kit_entry_t;
#define KIT       ((const kit_entry_t *)(0x02000380))  /* EXTISA_0: 17 entries */
#define KIT_N     17

void q7_pool_dispatch(unsigned cpu_id, uint8_t opcode, uint8_t spec, decoded_inst_t *ins) {
    LOG("P%i: In dispatch, CPU ID: %0d, got opcode 0x%x.", cpu_id, opcode);

    /* Build the packed key; the spec byte sub-selects within one opcode.
     * For non-extended ops, spec is 0 (one row per opcode).               */
    uint32_t key = ((uint32_t)opcode << 24) | ((uint32_t)spec << 16);

    for (int i = 0; i < KIT_N; ++i) {                    /* LINEAR SCAN */
        uint32_t row = *(const uint32_t *)&KIT[i];       /* native-LE first 4 bytes */
        if (row == key) {
            kernel_fn_t fn = (kernel_fn_t)KIT[i].funcVA;  /* relocated funcVA */
            fn(ins, cpu_id);                              /* callx8 -> tensor kernel */
            return;
        }
    }
    /* miss path differs for the 0xf0 family (see dispatch_extended_inst) */
    if (opcode == 0xf0) LOG("P%i: UNKNOWN EXTENDED OPCODE=%d", cpu_id, spec);
    else                LOG("P%i: UNKNOWN OPCODE=0x%x",       cpu_id, opcode);
}
```

> **NOTE — work partitioning.** The Q7 core fans the kernel across pool channels by
> `get_cpu_id()` (`"P%i: num_chans = %0d"`, `"P%i: Starting pooling engine: %i"`). Per-core index
> `i` is the `P%i:` prefix argument — the same per-core split the `'P%i:'` dispatch loop documents.

### 4c. The `0xF0` escape — reconciled across the two cores

This is the key question: how does the SEQ "`0xf0 = ExtendedInst`" (one SEQ handler) relate to the
Q7 "`0xf0` × 5 specs" (five `kernel_info_table` rows)? They are the **same opcode seen on the two
cores**:

- **SEQ side (`NX_POOL`):** index `0xf0 − 0x41 = 0xaf = 175`; entry `@ file 0x814 + 175·4 = 0xad0`.
  The slot reads `90 31 00 00` = trampoline `0x3190` → impl `0x235c` → handler `0xb3f0`
  (thunk `0x96d4`) → logs `"S: ExtendedInst"` at `DRAM 0x826a5`.
- **Q7 side (`EXTISA_0`):** opcode `0xf0` is registered **five times** (idx 6–10, specs
  `0,1,2,4,3`) at funcVAs `0x01003370 / 0x01003380 / 0x01003484 / 0x010037a8 / 0x01003a60`. The Q7
  dispatcher's single linear scan matches the packed `(spec, opcode)` key, so a `(0xf0, spec)`
  lands on exactly one of the five rows. `[rows HIGH/OBSERVED; specs 0/1/2 binding HIGH; specs
  3/4 → {RandGetState, RandSetState, Cptc} MED (FLIX-desynced extended path)]`

**The reconciliation:** the SEQ `0xf0` handler is the **front-end route** that forwards an
extended instruction (`opcode 0xf0` + its `spec` sub-byte) to the Q7 POOL core; the Q7
`kernel_info_table` is the **back-end** the spec byte sub-selects. The
"`(opcode<<24)|(spec<<16)` two-level escape" is realized as: **top-level opcode-byte `0xf0` in
*both* tables**, then the **spec byte at record `+2` (Q7 side)** = the `ExtendedInst` sub-opcode.
There is **no third dispatch level** — the five `0xf0+spec` rows **are** the spec sub-dispatch
table.

> **CORRECTION — there is no separate in-loop `0xf0` special-case.** A naive reading might expect
> the Q7 dispatcher to special-case `0xf0` and branch into a secondary table. It does not: the five
> `0xf0` rows live inline in the same `kernel_info_table`, and the *same* linear scan that resolves
> every other opcode resolves them by the spec byte. The native-LE `u32 (opcode<<24)|(spec<<16)`
> framing and the byte-exact "opcode `@+3`, spec `@+2`" framing describe the **identical 4 key
> bytes**.

> **QUIRK — why only POOL has the dual-dispatch.** `ExtendedInst` is a **POOL-exclusive** SEQ
> handler: the 5-way `S:`-roster set-diff shows it present in `NX_POOL`, **absent** from
> ACT/DVE/PE/SP. Only POOL ships *both* the SEQ `0xf0` bridge *and* a separate Q7 compute core —
> which is exactly why only POOL has a dual-dispatch.

### 4d. Three-way opcode reconciliation (SEQ ASCII ↔ Q7 key ↔ kernel name)

The SEQ ASCII opcodes (decoded as `byte − 0x41`) and the Q7 `kernel_info_table` numeric opcodes are
the **same opcode numbers** on the two cores:

| SEQ `S:` name | Q7 opcode | Q7 `P%i:` kernel |
| --- | --- | --- |
| `Tensor-Tensor` | `0x41` | `pool_tensor_tensor_arith` → `TensorTensorArith` |
| `Pool` | `0x45` | `decode_pool` → `Pool : num_chans` |
| `Iota` | `0x7e` | `pool_iota` → `Iota : num_chans` |
| `CrossLaneReduce` | `0x7c` | `pool_cross_lane_reduce_arith` → `TensorReduceBitvec` |
| `TensorDequantize` | `0x7b` | `decode_tensor_dequantize` → `TensorDequantize` |
| `GetSequenceBounds` / `NonzeroWithCount` | `0xf2` | `get_sequence_bounds` / `nonzero_with_count` |
| `ExtendedInst` | `0xf0` (×5 specs) | `ExtendedInst<Variant>` |

`[HIGH/OBSERVED for the opcode-number identity; per-kernel route HIGH/MED.]`

---

## 5. The 41-handler `NX_POOL` roster, diffed vs ACT/DVE/PE/SP

Method (identical to the ACT/DVE/PE pages): extract every single-token `"S: <OpName>"` handler log
from each engine's CAYMAN DEBUG DRAM (`strings | rg -P '^S: [A-Za-z][\w/-]*$' | sort -u`), set-diff.
Per-engine distinct counts:

| Engine | Distinct `S:` handlers |
| --- | --- |
| DVE | 53 |
| **POOL** | **41** |
| ACT | 26 |
| PE | 24 |
| SP | 18 |

POOL = 41, the **richest GENERAL-compute subset** (DVE's 53 are narrow data/vector variants; POOL
spans the widest distinct primitive set).

### 5a. The 18 shared-all-5 SEQ control/move core (5-way intersection)

`AluOp · BRANCH · BranchPrefetchHint · Event_Semaphore · EXT_BREAK · Halt · INS_BREAK · INS_FL ·
MOVE · NOP · NOTIFY · POLL_SEM · Redirect · SET_OM · STRONG_ORDER · TensorLoad · TensorStore ·
WRITE` — byte-for-handler-name identical in all five engines.

### 5b. POOL's 7 shared-with-DVE compute primitives (POOL+DVE, not all-5)

`EngineNop · MEMSET/RNG · Pool · Tensor-Reduce · Tensor-Scalar · Tensor-Scalar-PTR · Tensor-Tensor`
— the dense tensor-arith primitives POOL and DVE both carry.

### 5c. The 16 POOL-only handlers (set-diff: absent from ACT/DVE/PE/SP)

| Handler | Role |
| --- | --- |
| `ConvLutLoad` | load a conv lookup table |
| `CrossLaneReduce` | cross-lane (channel) reduction |
| `EmbeddingUpdate` | scatter/gather embedding update |
| **`ExtendedInst`** | **the `0xf0` escape to the Q7 `kernel_info_table` (§4c)** |
| `GetSequenceBounds` | sequence-bound compute |
| `Iota` | index/iota generation |
| `LoadPoolArgument` | load a pool-argument block |
| `ModifyPoolConfig` | modify the pool config CSRs |
| **`RandGetState`** | **RNG state read** |
| **`RandSetState`** | **RNG state write** |
| `SB2SB_Collective` | state-buffer-to-state-buffer collective (cross-NC) |
| `Sort` | sort kernel |
| `TensorDequantize` | dequantize kernel |
| `TensorGather` | gather kernel |
| `TensorScalarAddr` | tensor-scalar with address operand |
| `TensorScalarAffineSelect` | affine-select tensor-scalar |

> **NOTE — the RNG lives on POOL, not PE.** The `RandGetState`/`RandSetState` pair is **POOL-only**
> in the 5-way diff — confirming the RNG/seed handlers ship on POOL. The `MEMSET/RNG` entry in §5b
> is the shared MEMSET path; the stateful RNG-seed read/write is POOL-exclusive.

### 5d. Apples-to-apples (same regex on all 5 DEBUG DRAMs)

- **POOL-only (16):** the §5c list (incl. `ExtendedInst` = the dual-dispatch bridge).
- **PE-only (5):** `Ldweights · Matmul · MatmulSparse · LdTags · PeRegWrite`.
- **ACT-only (7):** `Activate · ActivateQuantize · ActivationReadAccumulator ·
  ActivationTableLoad · Cast · Copy · TensorScalar`.
- **DVE-only (28):** the batch-norm / predicated / match / scan / dropout / cumulative set.
- **SP (0 compute):** pure 18-handler SEQ control core.

POOL, ACT, DVE, PE, SP are the **same** `cayman/seq/` engine with **disjoint** compute subsets;
POOL's is the widest GENERAL-compute set + the unique `ExtendedInst` escape.

---

## 6. The Q7 compute engine, the EXTISA back-end, and the DKL layer

### 6a. The `Q7_POOL` (`'P%i:'`) compute dispatcher

Q7 DEBUG DRAM carries 156 `P%i:` logs (the RE substrate), incl.:

```text
P%i: Entering Dispatch / Exiting Dispatch
P%i: In dispatch, CPU ID: %0d, got opcode 0x%x.       (per-opcode trace)
P%i: dispatch : modify_pool_config
P%i: dispatch_extended_inst(%d) : num_chans = %0d      (the 0xf0 sub-dispatcher)
P%i: UNKNOWN OPCODE=0x%x  /  UNKNOWN EXTENDED OPCODE=%d (the two miss paths)
P%i: Starting pooling engine: %i  /  Stopping pooling engine
P%i: engine_base_addr=%llx tpb_base_addr=%llx -> ... engine_idx=%u   (boot identity)
```

The two miss paths are the structural proof of the two-level key: a plain-opcode miss emits
`UNKNOWN OPCODE=0x%x`; a `0xf0`-spec miss emits `UNKNOWN EXTENDED OPCODE=%d` (the spec). Source
module `dispatch.hpp`.

### 6b. The per-image `kernel_info_table` sub-tables

Each EXTISA ELF carries its own `kernel_info_table` section (all decoded byte-exact):

| ELF | section VA | entries | content |
| --- | --- | --- | --- |
| `EXTISA_0` | `0x02000380` (`0x88`) | 17 | the main table (§4b) |
| `EXTISA_1` | `0x02000048` (`0x08`) | 1 | `0x7e` iota only |
| `EXTISA_2` | `0x02000070` (`0x10`) | 2 | `0x7c`, `0x7d` cross-lane reduce |
| `EXTISA_3` | `0x020008c8` (`0x48`) | 9 | incl. `0xe4`/0 @`0x1002258` (cptc dispatcher) + `0xf0`/spec 7 @`0x1003b64` (cptc extended path) |

`EXTISA_3` hosts the `cptc_decode_impl<1..6>` DTYPE-selected codec family (the template arg is a
**DTYPE** selector, *not* the POOL spec). The `.xt.prop` demangled kernel names (`c++filt`)
corroborate the funcVA roster: `pool_iota`, `pool_cross_lane_reduce_arith/_bitvec`,
`decode_pool(bool)`, `decode_tensor_tensor_arith`, `decode_tensor_dequantize`,
`decode_extended_inst_tensor_tensor_arith`, `pool_extended_inst_copy`, `iota_impl<true/false>`,
`nonzero_with_count_impl<float/int>`, `get_sequence_bounds_impl`,
`TensorDequantize::proc_4bit_mx_8`.

> **NOTE — no baked weight table.** The per-kernel state pointers are the `.bss` band in the EXTISA
> ELF (`.bss @0x02000450 size 0x3c`, one slot per kernel). CAYMAN POOL carries **no** host-supplied
> weight LUT baked into the firmware; kernels read operands from the decoded instruction + SBUF.
> `[HIGH geometry / MED exhaustive per-table decode]`

### 6c. The DKL (DYNAMIC_KERNEL_LOAD) build variant

DKL is the Q7 POOL engine (`'P%i:'` prefix, reset `06 7f 00 00`) compiled with the dynamic
custom-op kernel-loading layer. DKL DEBUG DRAM carries symbols **absent** from the base Q7 DRAM:

```text
dynamic_kernel_dispatch          dynamic_extended_op_kernel_dispatch
.kernel_info_table               dispatch_wrapper.hpp
P%i: Corrupted prelink library; NULL start symbol
P%i: CustomOps not supported on Cayman
```

DKL loads a custom-op "prelink library" at runtime and dispatches into it via the **same**
`kernel_info_table` mechanism — a DKL-build extension of the existing dispatch, **not** a third
mechanism. `[symbols HIGH/OBSERVED; runtime-load semantics INFERRED-HIGH]`

> **CORRECTION — CustomOps are gated OFF on CAYMAN.** Despite the DKL image shipping, the string
> `"P%i: CustomOps not supported on Cayman"` shows the dynamic custom-op path is **disabled** on
> CAYMAN — it is a forward-looking layer. See
> [External-Library / Prelink Loader](../firmware/pool/external-lib-loader.md).

---

## 7. The POOL compute-kernel name list

Named from the Q7 DEBUG DRAM's own `"P%i: Decode : <Kernel>"` / `"P%i: <Kernel> : num_chans"` log
strings:

```text
Cast   Copy   Iota   Pool   Rng   NonzeroWithCount   TensorDequantize   GetSequenceBounds
TensorReduceBitvec   TensorTensorBitvec   TensorTensorArith   SB2SB_Collective   Sbuf2Sbuf
DECODE_SORT (sort)   RandGetState   RandSetState
ExtendedInstCopy   ExtendedInstTensorTensorArith   ExtendedInstEngineNop
ExtendedInstRandGetState   ExtendedInstRandSetState   ExtendedInstCptcDecode
```

Per-kernel helpers named in DRAM: `proc_4bit_mx_8` / `proc_4bit_non_mx` / `proc_6bit_non_mx`
(TensorDequantize MX paths); `decode_embedding_update` / `decode_sb2sb_collective` / `decode_sort` /
`decode_extended_inst_sb2sb` / `dma_memcopy_impl` / `dge_reshape_apply_impl` / `run_indirect_copy`.
The DKL build adds `DmaMemcopy`, `do_indirection`, `allgather` (the dynamic-kernel-load /
custom-op kernels).

---

## 8. DEBUG vs PERF(release) vs TEST (both families)

| IMAGE | total strings | `S:` logs | `P%i:` logs |
| --- | --- | --- | --- |
| `NX_POOL` DEBUG DRAM | 303 | 187 | 0 |
| `NX_POOL` PERF DRAM | 17 | 0 | 0 |
| `NX_POOL` TEST DRAM | 65 | 0 | 0 |
| `Q7_POOL` DEBUG DRAM | 254 | 0 | 156 |
| `Q7_POOL` PERF DRAM | 36 | 0 | 0 |
| `Q7_POOL` TEST DRAM | 62 | 0 | 0 |
| `Q7_DKL` DEBUG DRAM | 227 | 0 | 136 |
| `Q7_DKL` PERF DRAM | 3 | 0 | 0 |

DEBUG is the only build with the runtime logs (NX: 187 `S:`; Q7: 156 `P%i:`) — the RE substrate.
PERF (production/release) strips all logs (the residual strings are assertion source-paths). TEST
sits between. The dispatch mechanism is **invariant** across builds: same reset vectors, same SEQ
table base (DEBUG `0x80814` / PERF `0x80218`), same `kernel_info_table` format, same UNKNOWN-OPCODE
arms. DEBUG→PERF is a pure observability change.

---

## 9. Cross-engine code / PROF sharing

| Engine | NX `PERF_IRAM` sha256 | `PROF_CAM` | `PROF_TABLE` |
| --- | --- | --- | --- |
| ACT | `5ef2a351…` | `8fd7e422` | `ce761f81` |
| DVE | `9fa066f4…` | `8fd7e422` | `ce761f81` |
| PE | `13ba3969…` | `8fd7e422` | `ce761f81` |
| **POOL** | **`9049bf8c…`** | **`8fd7e422`** | **`ce761f81`** |
| SP | `5a6f6eaa…` | (no PROF) | (no PROF) |

- **CODE (IRAM/DRAM):** `NX_POOL` shares **no** bytes with ACT/DVE/PE/SP — each is a
  separately-compiled `cayman/seq/` build with its own handler subset. Sharing is at the
  source/structure level (identical reset vector `06 76 00 00`, dispatch model, 18-handler control
  core), **not** the linked-byte level.
- **PROF (CAM/TABLE):** **byte-identical** across all 4 NX engines (4/4;
  `cmp` of POOL vs ACT `PROF_TABLE` is IDENTICAL). `PROF_CAM` = 16-byte records
  `{opcode_id, mask=0xff, enable=1, rsvd=0}` (first records `0x01 / 0x06 / 0x02`);
  `PROF_TABLE` header word `0x00000201` (`01 02 00 00`), 8 KiB preallocated. The HW-decode profiling
  CAM/table are a **generic** shipped resource, not per-engine. SP ships no PROF. See
  [PROF CAM/TABLE Formats](./prof-cam-table-formats.md).
- **RESET VECTORS:** the five NX engines all share `06 76 00 00` (`j 0x1dc`); the Q7 POOL core has
  its own `06 7f 00 00` (`j 0x200`). The `.globstruct` magic `0x6099cb34` is shared by the EXTISA
  `.globstruct` and every flat DRAM head.

---

## 10. Engine-model classification

CAYMAN × POOL is the **central general-compute engine** (`engine_idx = 2`) and the only NX engine
with a **dual-dispatch two-core pipeline**:

| Property | `NX_POOL` (SEQ) | `Q7_POOL` (compute) |
| --- | --- | --- |
| CLS | NX | Q7 |
| log prefix | `S:` | `P%i:` (per pool-core index `i`) |
| reset vector | `j 0x1dc` (`06 76 00 00`) | `j 0x200` (`06 7f 00 00`) |
| dispatch table | 178 × 4B @ DRAM `0x80814` | 17 × 8B `kernel_info_table` @ `0x02000380` |
| key | ASCII opcode byte − `0x41` | `(opcode<<24)\|(spec<<16)` packed |
| lookup | direct-indexed jump | LINEAR SCAN by packed key |
| handler form | C++ `Handler::execute()` | flat C kernel fn (`callx8 funcVA`) |
| indirection | table→tramp→impl→thunk→execute (4 hops) | table → funcVA (1 hop) |
| packaging | flat IRAM/DRAM | flat IRAM/DRAM + EXTISA ELFs + DKL |
| miss policy | `"S: Bad Opcode"` → spin | `"P%i: UNKNOWN OPCODE=0x%x"` |
| source tree | `cayman/seq/src/…` | `dispatch.hpp` / `dispatch_wrapper.hpp` |
| distinct ops | 41 SEQ handlers (incl. `0xf0 ExtendedInst`) | 17/1/2/9 `kernel_info_table` entries + cptc sub-dispatch |

The two cores coexist as a fetch/decode → execute pipeline: `NX_POOL` decodes the `'S:'` stream and
routes compute opcodes (via the `0xf0 ExtendedInst` bridge) to `Q7_POOL`, which runs the
`kernel_info_table` dispatch and the kernel. The `0xf0` escape reconciles the two tables: opcode
`0xf0` in the SEQ table = the same opcode `0xf0` (×5 specs) in the `kernel_info_table`, the
`(opcode<<24)|(spec<<16)` two-level key (`opcode @+3`, `spec @+2`).

---

## 11. Honesty ledger

**HIGH / OBSERVED:**

- 46 POOL getters parsed instruction-exact (28 real + 18 zero-size/cursor); both families (14 NX +
  32 Q7). 15 carves sha256-verified, incl. DKL_PERF == DKL_TEST byte-identity.
- Both reset vectors (`06 76 00 00` / `06 7f 00 00`) and both converge on `enter_run @0x90`.
- SEQ 178-slot table @ `0x80814` decoded (55 real + 123 default `0x3198`); `0xf0` slot @ `0xad0`
  = `0x3190`; the 178-bound derived from `0xf2 − 0x41 + 1`.
- `kernel_info_table` 17/1/2/9 entries decoded byte-exact; the five `0xf0+spec` rows pinned;
  the `0xf0` escape reconciled.
- 41 POOL handlers (18 shared-all-5 + 7 shared-with-DVE + 16 POOL-only incl. `ExtendedInst` +
  RNG); counts DVE 53 / POOL 41 / ACT 26 / PE 24 / SP 18.
- PROF_CAM/PROF_TABLE byte-identical across ACT/DVE/PE/POOL (4/4); `.globstruct` magic `0x6099cb34`
  shared.

**MED / INFERRED:**

- The exact SEQ→Q7 handoff register/decode-slot (direction HIGH; exact slot not pinned).
- `0xf0` specs 3/4 → {RandGetState, RandSetState, Cptc} (specs 0/1/2 HIGH; 3/4 MED, FLIX-desync).
- EXTISA-vs-flat: the 247/256 window match → same PERF corpus in two packaging views
  (relocation-delta interpretation INFERRED-HIGH).
- DKL runtime custom-op load semantics (from symbol names; CustomOps gated off on CAYMAN).

**LOW / NOT CLAIMED:** which silicon part/runtime selects DEBUG/PERF/TEST/DKL; the SUNDA/MARIANA/
MAVERICK POOL variants (the diff pages); the exact per-opcode operand layout of each kernel.

---

## 12. Cross-references

- [Image Catalog Index](./image-catalog-index.md) — the 386-getter map; the 46 CAYMAN POOL rows.
- [POOL Engine Dispatch](../firmware/pool/pool-dispatch.md) — the 178-slot SEQ hub decode in depth.
- [POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md) — the spec sub-dispatch.
- [kernel_info_table Binary Layout](../firmware/pool/kernel-info-table.md) — the 8-byte record /
  packed-key format.
- [External-Library / Prelink Loader](../firmware/pool/external-lib-loader.md) — the DKL custom-op
  loader.
- [PROF CAM/TABLE Formats](./prof-cam-table-formats.md) — the 4/4 byte-identical NX profiling
  resource.
- [SUNDA × POOL](./sunda-pool.md) — the NC-v2 POOL diff baseline.
- [MARIANA × POOL](./mariana-pool.md) — the NC-v4 POOL forward diff.
- Sibling CAYMAN engines: [× ACT](./cayman-act.md), [× DVE](./cayman-dve.md), [× PE](./cayman-pe.md),
  [× SP](./cayman-sp.md).
