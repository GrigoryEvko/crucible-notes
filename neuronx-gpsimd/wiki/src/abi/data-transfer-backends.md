# Data-Transfer Backends

`neuron_memcpy` is the single bulk-copy primitive every buffered staging accessor
(see [TensorStream + TCM Staging](./tensorstream-tcm.md)) funnels through to move a
span of bytes between **HBM** (the 64-bit `Q7PtrType.hbm_addr` world) and the
core's fast **on-die local memory** — the Xtensa *dataram* used as tightly-coupled
memory (TCM), and SBUF/PSUM behind it. It is **not** one routine: it is a
*one-entry dispatch* over a **3-element function-pointer table** —
`{C_MEMCPY, VEC_MEMCPY, DMA}` — whose selected entry does the actual work. The
three backends share one chunking/translate core, `memcpy_data_transfer_impl`,
which calls [`neuron_translate`](./neuron-translate-windows.md) to convert each HBM
chunk address into an NX-local pointer before the C-loop / vector-loop bodies; the
DMA backend bypasses that core entirely and programs the on-die SDMA engine via a
descriptor ring built by `init_dma_queue`.

This page reconstructs all five moving parts byte-exactly from one translation
unit: **`data_transfer.o`** in
`aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64`'s
`libneuroncustomop.a` (`ar x … data_transfer.o`). It is ELF32-Xtensa, DWARF v4,
`addr_size 0x04`, compiled from `/opt/workspace/SundaCustomOpLibrary/custom_op/library/data_transfer.cpp`.
Disassembly is native — there is no Xtensa backend in mainline `objdump`, so all
listings are
`XTENSA_SYSTEM=…/XtensaTools/config XTENSA_CORE=ncore2gp xtensa-elf-objdump -dr`
(the toolchain warns *"TIE checksum does not match"* on this config — cosmetic, the
decode is byte-exact). Struct layouts are the unit's own clang-emitted DWARF
(`DW_AT_byte_size` / `DW_AT_data_member_location`).

> **Confidence.** The dispatch table, its three relocated entries, the default
> selector value, all five function bodies, every assertion string with its
> source line, the `NeuronMemcpyMethod` enum values, the `_dma_ctx_t` and
> `SDMA_CME_BD_DESC` layouts, and the M2S/S2M direction split are **HIGH ×
> OBSERVED** — `.rela.data`/`.rela.text` records, the `.data` hex image, native
> Xtensa disasm, and DWARF emitted by the ABI's own clang. The one inference
> flagged below (the exact APB ring-doorbell register identity) is **MED ×
> INFERRED**. Every claim is anchored to an ELF symbol, a `.data`/struct offset,
> a disasm address, or `data_transfer.cpp:line`.

> **32-bit consequence.** Every pointer / `size_t` is **4 bytes**; but `uint64`
> (`Q7PtrType.hbm_addr`, the `soc_addr` argument, the descriptor `buf_ptr`, and the
> two `*_inc_reg` fields) is **8 bytes aligned to 8** on Xtensa. Do not transpose
> these offsets onto a 64-bit host.

---

## 1. The dispatch table and the two `neuron_memcpy` overloads

`neuron_memcpy` has **two overloads**, distinguished only by *which argument is the
HBM address* — i.e. the transfer *direction* — confirmed by the ELF symtab
(`xtensa-elf-nm`):

```
0000065c T _Z13neuron_memcpyPvyj   neuron_memcpy(void*, unsigned long long, unsigned int)   ; HBM → local  (cpp:402)
00000694 T _Z13neuron_memcpyyPvj   neuron_memcpy(unsigned long long, void*, unsigned int)   ; local → HBM  (cpp:407)
00000200 d data_transfer_method_table   (anon-namespace)   ; .data+0x200
0000020c d active_neuron_memcpy_method  (anon-namespace)   ; .data+0x20c
```

The table lives in the anonymous namespace at **`.data+0x200`**; its three slots are
**relocations**, not literal pointers — the linker patches them in. `.rela.data`
spells out the array contents exactly:

```
RELOCATION RECORDS FOR [.data]:
OFFSET   TYPE          VALUE
00000200 R_XTENSA_32   _Z22c_memcpy_data_transferPvybj     ; table[C_MEMCPY  = 0]
00000204 R_XTENSA_32   _Z24vec_memcpy_data_transferPvybj   ; table[VEC_MEMCPY = 1]
00000208 R_XTENSA_32   _Z17dma_data_transferPvybj          ; table[DMA       = 2]
```

The selector at `.data+0x20c` is initialised in the data image. `objdump -s -j .data`
shows the final 16 bytes:

```
 0200 00000000 00000000 00000000 02000000   ; [0x200..0x20b]=3×reloc-placeholders, [0x20c]=02 00 00 00
```

`active_neuron_memcpy_method = 0x00000002` → **DMA is the default backend.** DWARF
makes the enum and the default explicit — **[OBSERVED]** `DW_TAG_enumeration_type
NeuronMemcpyMethod`:

| Enumerator | `DW_AT_const_value` |
|------------|---------------------|
| `C_MEMCPY` | 0 |
| `VEC_MEMCPY` | 1 |
| `DMA` | 2 |
| `MAX_METHODS` | 3 |
| `DEFAULT_METHOD` | **2** |

The dispatch in both overloads is identical except for the direction flag. The
HBM→local body at `0x65c`:

```
65c: entry  a1, 32
65f: { const16 a3, .data+0x20c ; mov.a a10,a2 ; mov.a a12,a4 ; mov.a a13,a5 }   ; a3=&active_method
66f: { const16 a3, .data+0x20c ; movi a2,0 ; movi a14,1 ; mov.a a15,a6 }        ; a14 = 1  (dataram_dst = HBM→local READ)
67f: { const16 a7, .data+0x200 }                                                ; a7 = &table[0]
685: l32i.n a3, a3, 0                                                           ; a3 = active_neuron_memcpy_method
687: addx4  a3, a3, a7                                                          ; a3 = &table[method]   (×4 = sizeof fn-ptr)
68a: l32i   a3, a3, 0                                                           ; a3 = table[method]
68d: callx8 a3                                                                  ; tail-style call into backend
690: retw.n
```

The local→HBM overload at `0x694` is byte-for-byte the same dispatch, with
`movi a14, 0` instead of `1` — i.e. it differs only in **which register feeds the
backend's `dataram_dst` argument**. So the *whole* of `neuron_memcpy` is:

```c
// data_transfer.cpp:402 / :407 — both overloads, reconstructed
typedef void (*DataTransferFunc)(char* dataram_addr, uint64_t soc_addr,
                                 bool dataram_dst, uint32_t nbytes);

static DataTransferFunc data_transfer_method_table[MAX_METHODS] = {  // .data+0x200
    c_memcpy_data_transfer,    // [0] C_MEMCPY
    vec_memcpy_data_transfer,  // [1] VEC_MEMCPY
    dma_data_transfer,         // [2] DMA
};
static NeuronMemcpyMethod active_neuron_memcpy_method = DEFAULT_METHOD; // .data+0x20c == DMA

void neuron_memcpy(void* dst, uint64_t src, uint32_t nbytes)   // HBM → local
{ data_transfer_method_table[active_neuron_memcpy_method](
      (char*)dst, src, /*dataram_dst=*/true,  nbytes); }

void neuron_memcpy(uint64_t dst, void* src, uint32_t nbytes)   // local → HBM
{ data_transfer_method_table[active_neuron_memcpy_method](
      (char*)src, dst, /*dataram_dst=*/false, nbytes); }
```

> **NOTE — the local pointer is always the first arg.** Both overloads pass the
> *NX-local* side as `dataram_addr` (`char*`) and the *HBM* side as `soc_addr`
> (`uint64_t`); the `dataram_dst` boolean is the only thing that records whether the
> local side is the **destination** (read from HBM) or the **source** (write to HBM).
> This is why there is no third "device-to-device" overload here — both directions
> reuse one backend signature.

The backend selection is rebindable at runtime by `neuron_set_memcpy_method`
(`0x6cc`, `cpp:412`):

```
6cc: entry  a1, 32
6cf: mov.a  a3, a2
6d2: { bgeui.w15 a3, 4, 6e8 ; movi a2,-1 }     ; guard: method >= 4 → skip (return -1)
6da: const16 a2, .data+0x20c                   ; &active_neuron_memcpy_method
6e0: { s32i a3, a2, 0 ; movi a2,0 }            ; *selector = method ; ret 0
6e8: retw.n
```

```c
int neuron_set_memcpy_method(NeuronMemcpyMethod method) {  // cpp:412
    if ((unsigned)method >= 4) return -1;     // bgeui.w15 a3,4  — note bound is 4, not MAX_METHODS=3
    active_neuron_memcpy_method = method;
    return 0;
}
```

> **GOTCHA — the bound is `4`, not `MAX_METHODS`.** The guard is `bgeui a3,4`
> (unsigned `>= 4` rejected), so `method == 3 == MAX_METHODS` is *accepted* and
> stored even though `table[3]` is **out of bounds** (the table has slots 0..2). A
> later `neuron_memcpy` would then `l32i` whatever follows the table in `.data` —
> here the low bytes of `active_neuron_memcpy_method` itself — and `callx8` it. Treat
> `MAX_METHODS` as the real limit when reimplementing this setter.

---

## 2. The shared chunking / translate core — `memcpy_data_transfer_impl`

Both software backends are two-line trampolines into one core,
`memcpy_data_transfer_impl` (`0x0`, `cpp:69`). DWARF names its signature exactly:

```c
// _Z25memcpy_data_transfer_implPcybjb  — data_transfer.cpp:69
void memcpy_data_transfer_impl(char*    dataram_addr,   // NX-local pointer
                               uint64_t soc_addr,       // HBM / SoC address
                               bool     dataram_dst,    // true = HBM→local, false = local→HBM
                               uint32_t nbytes,
                               bool     use_vec_memcpy);// 0 = scalar memcpy, 1 = vector loop
```

`c_memcpy_data_transfer` (`0x1b8`) calls it with `use_vec_memcpy = movi a3, 0`;
`vec_memcpy_data_transfer` (`0x1e4`) with `movi a3, 1`. Both spill that 5th boolean
to the stack (`s32i.n a3, a1, 0`) and `callx8` into the core — they are the *only*
difference between the C and vector backends:

```
000001b8 <c_memcpy_data_transfer>:                000001e4 <vec_memcpy_data_transfer>:
 1bb: { const16 a8, impl ; mov.a a10,a2            1e7: { const16 a8, impl ; mov.a a10,a2
        mov.a a12,a4 ; mov.a a13,a5 }                     mov.a a12,a4 ; mov.a a13,a5 }
 1cb: { … movi a3,0 ; mov.a a14,a6 …}  ←0           1f7: { … movi a3,1 ; mov.a a14,a6 …}  ←1
 1db: s32i.n a3, a1, 0                              207: s32i.n a3, a1, 0
 1dd: callx8 a8                                     209: callx8 a8
 1e0: retw.n                                        20c: retw.n
```

The core loops over the transfer in chunks, calling `neuron_translate` on each
chunk's HBM address. Reconstructed from the body (`0x0`–`0x1a4`):

```c
void memcpy_data_transfer_impl(char* dataram_addr, uint64_t soc_addr,
                               bool dataram_dst, uint32_t nbytes, bool use_vec_memcpy)
{
    // 0x3 const16 a3, neuron_translate_ctx ; 0xd callx8  — establish the translate ctx
    void* ctx = neuron_translate_ctx();                       // cpp:87 region
    if (nbytes == 0) return;                                  // 0x20 beqz.w15 a7 → 0x1a0 retw.n

    char*    local = dataram_addr;
    uint64_t hbm   = soc_addr;
    uint32_t left  = nbytes;

    do {
        // --- translate this chunk's HBM address to an NX-local pointer ---
        void* nx_local = neuron_translate((void*)hbm, ...);   // 0x78 callx8  (cpp:94)
        uint64_t mapped_lo, mapped_hi;
        int rc = neuron_translate_mapping_lookup(             // 0x9e callx8
                     (void*)hbm, /*len*/left, &mapped_lo, &mapped_hi);
        if (rc != 0)                                          // 0xa1 bnez.w15 a10 → 0x1a4
            _Assert(".../data_transfer.cpp:92 ret == 0");     // .data+0x00, _Assert(file, line=0)

        // chunk = min(left, span_to_window_end); capped at the 128-byte vector grain
        uint32_t chunk = min_u(left, window_remaining);       // 0xc1 minu a14,a7,a2

        if (use_vec_memcpy && chunk_is_vector_eligible) {
            // ---- VECTOR path (cpp:107..171) ----  2Nx8 = 128-byte aligned vector
            // 0xff srai a10,a8,7   → chunk/128 ; loopnez over full 128B vectors
            ivp_la_pp(u0, src);                               // 0x10f ivp_la_pp prime
            for (i = 0; i < chunk/128; ++i) {                 // 0x12a loopnez
                ivp_la2nx8_ip(v0, u0, src);                   // 128B aligned load,  post-inc
                ivp_sa2nx8_ip(v0, u1, dst);                   // 128B aligned store, post-inc
            }
            // 0x145 tail: ivp_lav2nx8_xp / ivp_sav2nx8_xp variable-length 0..127B remainder
            ivp_lav2nx8_xp(v0, u0, src, rem);
            ivp_sav2nx8_xp(v0, u1, dst, rem);
            ivp_sapos_fp(u1, dst);                            // flush partial store position
        } else {
            // ---- SCALAR path (cpp:133) ----
            memcpy(dst, src, chunk);                          // 0x183 const16 memcpy ; 0x189 callx8
        }

        local += chunk;  hbm += chunk;  left -= chunk;        // 0x38 add.a / sub.a fold-back
    } while (left != 0);                                      // 0x58 beqz.w15 a7 → 0x1a0
}
```

Reading direction into the loop body: `dataram_dst` selects which of `{local,
nx_local}` is `src` vs `dst` for the `memcpy`/vector copy. When `dataram_dst` is
true (HBM→local), `src = nx_local` (the translated HBM window) and `dst = local`;
when false (local→HBM), the roles swap. The `moveqz/movnez` cluster at `0xd1`–`0xef`
is exactly this two-way pointer swap on the `dataram_dst` predicate.

> **QUIRK — the vector path is 128-byte (2Nx8) wide.** The `srai a8, 7` (`÷128`) and
> the `2nx8` mnemonics confirm the vector backend strides one **64-lane × 8-bit**
> register (= 512 bits = 64 B) per `la2nx8`, but the *aligned* path consumes
> **two** per iteration (a `2nx8` load is 128 B). The `ivp_lav2nx8_xp` /
> `ivp_sav2nx8_xp` *variable*-length variants mop up the `chunk % 128` remainder in a
> single guarded load/store, so the vector backend never falls back to scalar inside
> a chunk. The `ivp_sapos_fp` at `0x15b`/`0x16b` flushes the store-position register
> so a partial final vector lands correctly.

> **NOTE — the `cpp:92 ret == 0` assertion.** `.data+0x00` holds the string
> `".../data_transfer.cpp:92 ret == 0"`; the `_Assert(msg, 0)` at `0x1ac` fires when
> `neuron_translate_mapping_lookup` returns non-zero — i.e. the requested HBM span
> cannot be mapped into a translate window. There is **no** software fallback; a
> failed translate is fatal. Custom-op authors must keep transfers inside mappable
> regions (see [neuron_translate Window Family](./neuron-translate-windows.md), the
> 5-entry window walk with the 168-byte ctx).

---

## 3. The DMA backend — descriptor ring, M2S/S2M, kick & wait

The default backend, `dma_data_transfer` (`0x4f0`, `cpp:299`), does **not** use the
translate core. Instead it converts the HBM address into a physical *SoC* address
once, then programs a ring of **16-byte SDMA descriptors** and rings the queue.

### 3.1 `dram_addr_to_soc_addr` — physical address + per-core offset

`dram_addr_to_soc_addr(uint32_t dram_addr)` (`0x210`, `cpp:159`) maps a DRAM-window
address into the SoC physical map, *offset by this NeuronCore's id*. The id comes
from `rsr.prid` (`0x23e`), bounded `< 8`:

```c
uint64_t dram_addr_to_soc_addr(uint32_t dram_addr) {       // cpp:159
    // 0x213 movi a3,-1<<16 ; movi a4,1<<19 ; and a3=dram_addr&0xFFFF0000
    if ((dram_addr & 0xFFFF0000) != 0x00080000)            // 0x226 bne → 0x2d8 _Assert(.data+0x54)
        _Assert(".../data_transfer.cpp:160 "
                "dram_addr >= 0x80000 && dram_addr < 0x90000");  // .data+0x54
    uint32_t base   = MEM_REG[…];                          // 0x231 l32i — APB region base table
    uint32_t cpu_id = rsr_prid();                          // 0x23e rsr.prid
    if (cpu_id >= 8)                                        // 0x243 bgeui a5,8 → 0x2ec _Assert(.data+0xcc)
        _Assert(".../data_transfer.cpp:171 cpu_id < 8");   // .data+0xcc
    // 0x24b..0x2a8: a dense switch on cpu_id producing a per-core "soc-stride" multiplier
    //   cpu0→9 cpu1→11 cpu2→13 cpu3→15 cpu4→17 cpu5→19 cpu6→21 cpu7→23
    uint32_t stride = soc_stride_for_cpu(cpu_id);          // (2*cpu_id + 9)
    uint64_t soc = (uint64_t)base
                 + ((uint64_t)stride << 16)                // 0x2b0 slli a5,16
                 + dram_addr;                               // 0x2c0/0x2c8 64-bit add w/ carry
    return soc;                                             // a2:a3 = lo:hi
}
```

> **GOTCHA — the staging-window assertion is in `dram_addr_to_soc_addr`, not the
> impl.** `addr2line` places the `dram_addr >= 0x80000 && dram_addr < 0x90000`
> string at **`cpp:160`** inside `dram_addr_to_soc_addr` (`0x226 → 0x2d8`), reached
> from the **DMA** path and from `init_dma_queue`. The shared software core
> (`memcpy_data_transfer_impl`) does *not* perform this check — its bound is enforced
> by `neuron_translate` instead. So the `[0x80000, 0x90000)` 64-KiB staging window is
> a constraint on the **DMA backend's** local descriptor buffers (the dataram scratch
> in `data_scratch_map`), consistent with the TCM staging region documented in
> [TensorStream + TCM Staging](./tensorstream-tcm.md). The `cpu_id` switch is the
> per-core APB aperture stride; with 8 cores the multipliers are the odd numbers
> 9,11,…,23 — i.e. `2·cpu_id + 9` (verified `bnei`/`movi` arms at 0x284/0x290/…).

### 3.2 `init_dma_queue` — building the descriptor ring

`init_dma_queue()` (`0x314`, `cpp:206`) lays out the `_dma_ctx_t` over the
`data_scratch_map`-anchored scratch region and zeroes/initialises the descriptor
rings. The `_dma_ctx_t` layout is DWARF-pinned (`_dma_ctx_t`, decl in a private
header at `cpp:9`):

| field | offset | type | meaning |
|-------|--------|------|---------|
| `m2s_inc_reg` | 0 | `uint64_t` (reg64) | Memory-to-Stream increment/doorbell register |
| `s2m_inc_reg` | 8 | `uint64_t` (reg64) | Stream-to-Memory increment/doorbell register |
| `tx_desc` | 16 | `SDMA_CME_BD_DESC*` | TX (M2S) descriptor ring base |
| `rx_desc` | 20 | `SDMA_CME_BD_DESC*` | RX (S2M) descriptor ring base |
| `comp_desc` | 24 | `SDMA_CME_BD_DESC*` | completion descriptor ring base |
| `dma_ring_tail_idx` | 28 | `uint32_t` | producer tail index (wraps at 4) |
| `dma_ring_id` | 32 | `uint8_t` (SDMAOP) | ring/op id |
| `reserved` | 33 | — | pad |

The descriptor itself, `SDMA_CME_BD_DESC`, is **16 bytes** — **[OBSERVED]**
`DW_AT_byte_size: 16`:

| field | offset | type | notes |
|-------|--------|------|-------|
| `word0` | 0 | `SDMA_DESC_WORD0` (4B bitfield) | `length_meta`@bits, `netag0_meta`@byte2, `md`@byte2, `ringid`/`first`/`last`/`int_en`/`no_snoop`/`dmb`/`concatenate`@byte3 |
| `word1` | 4 | `SDMA_CME_DESC_WORD1` (4B union) | `block_attr` \| `optype` \| `op` \| `write_barrier` \| `endian_type` \| `send_crc` \| … |
| `buf_ptr` | 8 | `uint64_t` | 64-bit SoC/local buffer pointer (lo@8, hi@12) |

```c
void init_dma_queue() {                                    // cpp:206
    _dma_ctx_t* ctx = (_dma_ctx_t*)&data_scratch_map[0].region;  // 0x317 const16 data_scratch_map
    char* base = ctx;                                      // 0x32c l32i a6 = region base
    ctx->tx_desc   = base + 0x180;                         // 0x317 movi a7,0x180 ; 0x331 add ; 0x345 s32i @+0x218? (see below)
    ctx->rx_desc   = base + 0x140;                         // 0x317 movi a15,0x140
    ctx->comp_desc = base + 0x100;                         // 0x317 movi a5,0x108 / +0x100
    // 0x353/0x356: store ring bases into ctx slots @+0x210 / +0x214 / +0x218 of the scratch view
    memset(base + 0x100, 0, /*0x108..*/);                  // 0x34d const16 memset ; 0x359 callx8

    // --- descriptor-type / optype seeds (SDMA_CMETYPE_COPY=0, SDMAOP_*=…) ---
    base[0x104] = 3; base[0x114]=3; base[0x124]=3; base[0x134]=3;   // 0x35c..0x37e s32i a5=3
    uint32_t op = (3u << 26);                              // 0x35c movi a2,3 ; 0x364 slli a2,26
    base[0x100]=op; base[0x110]=op; base[0x120]=op; base[0x130]=op; // word1 optype field

    // --- pick the doorbell register: S2M first, fall back to M2S ---
    memw();                                                // 0x381
    uint64_t apb = read_apb_window();                      // 0x394/0x396 l32i a8,a2,0 / a2,a2,4
    apb_hi ^= (0xF << 28);  apb_lo ^= 0x555*3;             // 0x39b xor ; 0x38f addx2 0x555
    if (extended_isa::sdk::dma_queue_s2m_offset valid) {   // 0x3b6 bnez → 0x4dc on mismatch
        // load s2m_inc_reg / m2s_inc_reg externs and program ctx
    } else {
        _Assert(".../data_transfer.cpp:240 "
                "READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE"); // .data+0x171
    }
    ctx->m2s_inc_reg = extended_isa::sdk::dma_queue_m2s_offset + apb; // 0x3b9 m2s extern
    ctx->s2m_inc_reg = extended_isa::sdk::dma_queue_s2m_offset + apb; // 0x39b s2m extern

    // --- seed each ring entry's word1 (SDMA_CMETYPE_COPY / SDMAOP) and buf-pointers ---
    for (each ring entry) {                                // 0x400/0x410 callx8 dram_addr_to_soc_addr
        entry->word1 = SDMA_CMETYPE_COPY;                  // 0x44b movi a4,4 ; s32i — op/optype seed
        entry->buf_ptr = soc_addr(...);                    // 0x439/0x446 s32i a10/a11
    }
    ctx->dma_ring_tail_idx = 0;                            // 0x4b1 s32i a11(=0), base, 0x21c
    // 0x4c1..0x4d4 stash the ring base pointers (minus 0x1000 page) into ctx @0x200/0x204/0x208/0x20c
    memw();                                                // 0x4d7
}
```

> **NOTE — the `cpp:240` assertion pins the APB base.** `.data+0x171` is
> `"…cpp:240 READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE"`. `init_dma_queue`
> reads `MEM_WINDOW0_LO` (the local user-register window 0 low half) and **asserts it
> equals `SUNDA_APB_BASE`** before trusting the `dma_queue_{m2s,s2m}_offset` externs
> (`_ZN12extended_isa3sdk20dma_queue_m2s_offsetE` /
> `…s2m_offsetE`). The two offsets are *imported* (`U`) — they are the SDMA queue
> doorbell apertures resolved at link time, added to the APB window base to form the
> ctx's `m2s_inc_reg` / `s2m_inc_reg`. The exact APB register *identity* behind the
> `0xF<<28` / `0x555` masking is **MED × INFERRED** (an APB doorbell, decoded from
> the bit-twiddling but not named in this TU).

### 3.3 `dma_data_transfer` — direction split, fill, kick, wait

```c
void dma_data_transfer(char* dataram_addr, uint64_t soc_addr,
                       bool dataram_dst, uint32_t nbytes)   // cpp:299
{
    uint64_t soc = dram_addr_to_soc_addr((uint32_t)soc_addr); // 0x4f3 callx8
    if (nbytes == 0) return;                                  // 0x504 beqz.w15 a7 → 0x63d retw.n

    _dma_ctx_t* ctx = (_dma_ctx_t*)&data_scratch_map[0];      // 0x50c const16 data_scratch_map
    // snapshot ring bases & inc-regs from the scratch ctx view (offsets 0x200..0x218)
    void* tx   = ctx@0x200;  void* m2s_lo = ctx@0x210;        // 0x514/0x524 l32i
    void* s2m  = ctx@0x218;  void* comp   = ctx@0x208;

    // ---- DIRECTION SPLIT on dataram_dst (a6) ----
    if (dataram_dst == 0) {                                   // 0x537 beqz.w15 a6 → 0x640  (local→HBM)
        // local → HBM  ⇒  Memory-to-Stream (M2S)
        src = dataram_addr;  dst = soc;                       // 0x640: mov a4=a10(local), a14=a5
        inc_reg = ctx->m2s_inc_reg;                           // M2S doorbell
    } else {
        // HBM → local  ⇒  Stream-to-Memory (S2M)
        src = soc;           dst = dataram_addr;              // 0x545 mov.a a10,a3
        inc_reg = ctx->s2m_inc_reg;                           // S2M doorbell
    }
    // 0x548 movnez a5, a11, a6  selects the inc_reg per direction; a15=1 (kick value)

    uint32_t left = nbytes;
    uint8_t  tail = ctx->dma_ring_tail_idx;                   // 0x558 l8ui … @0x21c

    do {                                                      // 0x568 loop.w15  — per descriptor
        SDMA_CME_BD_DESC* d_tx = &tx_desc[tail];              // 0x578 slli tail<<4  (×16 = sizeof desc)
        SDMA_CME_BD_DESC* d_rx = &rx_desc[tail];

        uint32_t chunk = min_u(left, 0x10000);               // 0x586 minu a7, (1<<16)  — 64KiB per desc

        // ---- program TX descriptor ----
        d_tx->buf_ptr      = src;                            // 0x58d s32i a4, d_tx, 8 (lo)
        d_tx->word0       &= ~0x03FF0000;                   // 0x57b movi 0xFFFFFCFF<<16 ; and
        deposit(d_tx->word0, chunk,   bits[0:16]);          // 0x595 depbits a8,a11,0,16  (length_meta)
        deposit(d_tx->word0, tag,     bits[24:26]);         // 0x598 depbits a8,a3,24,2   (netag/md)
        d_tx->word0_hi32   = src_hi;                         // 0x59d s32i a14, d_tx, 12

        // ---- program RX descriptor (mirror) ----
        d_rx->buf_ptr      = dst;                            // 0x5b5 s32i a10, d_rx, 8
        deposit(d_rx->word0, chunk,   bits[0:16]);          // 0x5bd depbits a4,a11,0,16
        deposit(d_rx->word0, tag,     bits[24:26]);         // 0x5c0 depbits a4,a6,24,2
        d_rx->word0_hi32   = dst_hi;                         // 0x5c5 s32i a5, d_rx, 12

        memw(); memw();                                      // 0x5c7/0x5ca  — publish desc before doorbell
        *(volatile uint32_t*)inc_reg = 1;                    // 0x5cf s32i a15(=1), inc_reg, 0  — KICK
        memw();                                              // 0x5d1
        *(volatile uint32_t*)comp_inc = 1;                   // 0x5ea s32i a15, comp, 0

        // advance producer tail, wrap at 4
        tail = ctx->dma_ring_tail_idx + 1;                   // 0x5ec l32i @0x21c ; 0x5f2 addi.n 1
        if (tail == 4) tail = 0;                             // 0x5f7 bnei.w15 a8,4 ; else store 0
        ctx->dma_ring_tail_idx = tail;                       // 0x5f4 / 0x601 s32i @0x21c
        ctx->ring_byte = saltu(tag, …);                      // 0x609 s8i a3, …, 32

        src += chunk;  dst += chunk;  left -= chunk;          // 0x5d4/0x5dc add.a / sub.a
    } while (left != 0);                                     // 0x60c beqz.w15 a7 → 0x624

    // ---- WAIT for completion ----
    do {                                                     // 0x62c
        uint8_t st = comp_desc[idx].byte3 & 0x3;             // 0x62c l8ui a8, a3, 3 ; 0x62f extui 0,2
    } while (st != expected_tag);                            // 0x632 bne a8,a6 → 0x62c  (spin)
    // 0x635 bnez.w15 a7 → 0x558  (more bytes than one ring fill: refill & repeat)
}
```

The direction logic distilled:

| direction | overload | `dataram_dst` | branch | engine path | doorbell |
|-----------|----------|---------------|--------|-------------|----------|
| **HBM → local** | `neuron_memcpy(void*, u64, u32)` | `true` (1) | `0x537` not-taken / `0x545` | **S2M** (Stream→Memory) | `ctx->s2m_inc_reg` |
| **local → HBM** | `neuron_memcpy(u64, void*, u32)` | `false` (0) | `0x537` → `0x640` | **M2S** (Memory→Stream) | `ctx->m2s_inc_reg` |

> **QUIRK — the ring is 4 deep and chunks are 64 KiB.** `dma_ring_tail_idx` wraps at
> **4** (`bnei.w15 a8, 4` at `0x5f7`), so the descriptor ring holds 4 entries; each
> descriptor's `length_meta` field is 16 bits (`depbits …,0,16`) and the per-chunk
> cap is `0x10000 = 64 KiB` (`minu` at `0x586`). A transfer larger than `4 × 64 KiB`
> loops back through `init_dma_queue`'s rings via the `0x635` re-fill branch. The TX
> and RX descriptors are programmed as a **matched pair** every iteration — the M2S
> ring streams the source out, the S2M ring streams it into the destination, and the
> completion ring (`comp_desc`) is polled (`0x62c` spin on `byte3 & 0x3`) for the
> matching tag before return. The double `memw` at `0x5c7`/`0x5ca` is the publish
> fence: descriptor stores must be globally visible **before** the doorbell write at
> `0x5cf`, or the engine races the producer.

> **CORRECTION — symbol names.** Earlier notes referred to the table entries as
> `c_memcpy` / `vec_memcpy`. The actual demangled symbols are
> `c_memcpy_data_transfer` (`_Z22c_memcpy_data_transferPvybj`, `0x1b8`) and
> `vec_memcpy_data_transfer` (`_Z24vec_memcpy_data_transferPvybj`, `0x1e4`); the DMA
> entry is `dma_data_transfer` (`_Z17dma_data_transferPvybj`, `0x4f0`). The
> direction-flag argument is named `dataram_dst` in DWARF (not `is_read`), and the
> vector/scalar selector is `use_vec_memcpy` (5th argument of
> `memcpy_data_transfer_impl`, not a separate table dimension).

> **CORRECTION — the staging-window assertion's home.** ABI-12 placed the
> `dram_addr >= 0x80000 && dram_addr < 0x90000` check "in `memcpy_data_transfer_impl`".
> `addr2line` shows it at `cpp:160` inside **`dram_addr_to_soc_addr`** (`0x226 →
> 0x2d8 _Assert(.data+0x54)`), called only from the **DMA** path and
> `init_dma_queue`. The software (C/vector) core enforces mappability through
> `neuron_translate`'s `cpp:92 ret == 0` assertion instead.

---

## 4. Symbol & evidence index

ELF symtab of `data_transfer.o` — **9 defined text functions, 9 undefined externs**
(`xtensa-elf-nm | rg -c`):

```
00000000 T memcpy_data_transfer_impl(char*, unsigned long long, bool, unsigned int, bool)
000001b8 T c_memcpy_data_transfer(void*, unsigned long long, bool, unsigned int)
000001e4 T vec_memcpy_data_transfer(void*, unsigned long long, bool, unsigned int)
00000210 T dram_addr_to_soc_addr(unsigned int)
00000314 T init_dma_queue()
000004f0 T dma_data_transfer(void*, unsigned long long, bool, unsigned int)
0000065c T neuron_memcpy(void*, unsigned long long, unsigned int)         ; HBM → local
00000694 T neuron_memcpy(unsigned long long, void*, unsigned int)         ; local → HBM
000006cc T neuron_set_memcpy_method(NeuronMemcpyMethod)
00000200 d data_transfer_method_table     ; .data+0x200 (3 × R_XTENSA_32)
0000020c d active_neuron_memcpy_method     ; .data+0x20c = 0x02 (DMA)
         U _Z16neuron_translatePvy                 ; translate core (5-window walk, 168B ctx)
         U _Z20neuron_translate_ctxv
         U _Z31neuron_translate_mapping_lookupPvyPyS0_
         U _ZN12extended_isa3sdk20dma_queue_m2s_offsetE
         U _ZN12extended_isa3sdk20dma_queue_s2m_offsetE
         U data_scratch_map  U memcpy  U memset  U _Assert
```

Assertion strings (`.data` image), each `_Assert(msg, /*line*/0)`:

| `.data` offset | string | reached from |
|----------------|--------|--------------|
| `0x00` | `…/data_transfer.cpp:92 ret == 0` | impl, on `mapping_lookup` failure |
| `0x54` | `…/data_transfer.cpp:160 dram_addr >= 0x80000 && dram_addr < 0x90000` | `dram_addr_to_soc_addr` |
| `0xcc` | `…/data_transfer.cpp:171 cpu_id < 8` | `dram_addr_to_soc_addr` |
| `0x123` | `…/data_transfer.cpp:200 0` | `dram_addr_to_soc_addr` (unmapped cpu_id arm) |
| `0x171` | `…/data_transfer.cpp:240 READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE` | `init_dma_queue` |

---

## See also

- [TensorStream + TCM Staging](./tensorstream-tcm.md) — the buffered accessors that
  call `neuron_memcpy`; the dataram/TCM staging buffers this page's DMA backend fills.
- [neuron_translate Window Family](./neuron-translate-windows.md) — the
  `neuron_translate` / `…_ctx` / `…_mapping_lookup` translate core the C and vector
  backends call (5-entry window walk, 168-byte ctx, `Q7PtrType` 16-byte
  `{hbm_addr@0, ctx_ptr@8, is_nullptr@0xC}`).
- [Device Memory Allocators](./device-allocators.md) — `data_scratch_map`, over which
  the `_dma_ctx_t` and descriptor rings are laid out.
- [The DMA / Descriptor / Memory Subsystem](../dma/descriptor-model.md) — the hardware
  SDMA engine, `SDMA_CME_BD_DESC` ring semantics, and the M2S/S2M stream model in full.
