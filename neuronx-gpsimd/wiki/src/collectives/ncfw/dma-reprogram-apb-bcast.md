# NCFW DMA Reprogram + APB Broadcast + Alloc Bitmap

This page decodes, byte-exact, how the **NCFW management core** reconfigures the
DMA engines **between collective steps**. Three mechanisms compose one step:

1. the **in-place ring-base/size reload** — the firmware does not rebuild the
   standing [pring](pring-descriptors.md); it re-arms each participating engine's
   M2S/S2M ring CSRs (`base_lo`/`base_hi`/`len`) by laying *reprogram descriptors*
   into a dedicated **reprogram section** (§1);
2. the **APB broadcast** — one write to a per-engine **`BCAST_UDMA`** window that
   the fabric fans to N DMA engines (the ring *doorbell* + re-arm fan out), gated
   by a group mask (§2);
3. the **resource-allocation bitmap** — a static per-queue-bundle `u64` table
   (`cayman_dma_alloc_bitmap`) whose low 32 bits are an engine-aperture mask and
   high 32 bits a queue/channel mask, from which the runtime
   `dma_engines_bitmap` ([NCFW DMA naming](ncfw-dram-ctx-log.md)) is built (§3).

It is the *between-steps re-arm* companion to [pring](pring-descriptors.md) (the
ring template/physical split), to the [al_udma HW engine](../../dma/udma-hw-engine.md)
(the per-queue CSR map this page writes), and to the
[SDMA windows + APB chain](../../dma/sdma-windows-apb.md) (the `BCAST_UDMA`
aperture and the fabric fan-out path).

> **GOTCHA — two Xtensa cores, do not cross the wires.** The on-device code that
> *executes* the per-step reprogram/bcast/alloc runs on the **scalar Xtensa-LX**
> management core (NCFW), not the Vision-Q7 "Cairo" FLIX datapath. The only
> registered Xtensa config is `ncore2gp` (the Q7 NX core), so ~26–28 % of NCFW
> code bytes mis-decode as Vision bundles; the on-device dispatch loop is
> FLIX-undecodable. **Therefore every algorithm here is recovered from host
> x86-64 code** — the `libnrt.so` *builder/encoder* (full DWARF, the authoritative
> struct source) and the `libncfw.so` *ctx_log decoder* (whose field offsets
> mirror the firmware struct layout) — plus the static `.rodata` tables and the
> committed cayman RTL address map. See the
> [scalar-LX core page](../../uarch/ncfw-lx-core.md) for the ISA evidence and
> [pring](pring-descriptors.md) for why the host side is fully recoverable.

**Provenance & confidence.** Every fact below is read **this session** from two
shipped host libraries with stock binutils (`readelf -SW`/`-n`, `nm -nS`,
`objdump -d -M intel`, `sha256sum`) and a Python ELF/`struct` byte reader:

| binary | role | sha256 (head) | BuildID (head) |
|---|---|---|---|
| `libnrt.so.2.31.24.0` | the **encoder** — builds the reprogram descriptors, the bcast value, resolves the alloc bitmap; carries DWARF | `956382de…` | `8bb57aba…` |
| `libncfw.so` | the **decoder** — the `ctx_log` JSON pretty-printer that walks the firmware DRAM structs | `598920d7…` | `a98f8e1c…` |

Both SHA256 + BuildID re-verified this pass. Section deltas confirmed by
`readelf -SW`: in **both** binaries `.text`/`.rodata` have **VMA == file offset**
(so all `.text`/`.rodata` addresses below are read directly); `libncfw.so` `.data`
has a `+0x1000` VMA→file delta but **no struct read here touches `.data`**. Tags
follow the [Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = byte/disasm/symbol read this pass; `INFERRED` = deduced from
structure/siblings; `CARRIED` = cited from a committed page. Lawful
interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)); no vendor source
snapshot consulted.

---

## 1. The DMA reprogram — the in-place ring-base/size reload

Between collective steps the firmware **re-arms** the standing pring rather than
rebuilding it: for each participating engine it overwrites the per-queue M2S/S2M
ring CSRs `{base_lo, base_hi, len}` with the new step's values, via DMA-copy
descriptors the host lays into a *reprogram section*. The base/size differ per
engine (each engine's ring lives at a distinct HBM address), so they **cannot be
broadcast** — they are written per `(engine, QID)` (contrast §2, which broadcasts
the ring *trigger*).

### 1.1 The six ring-CSR descriptors `[OBSERVED HIGH]`

The reprogram loop is emitted inline in **`encd_load_executable`** (`@0x246ae0`).
Six descriptors per `(engine, QID)`, one per ring-CSR field; the verbatim log
strings (`.rodata`, `strings -t x` this pass) name each role:

| `.rodata` | string |
|---|---|
| `0x806e60` | `[nec_dev %u] Adding reprogram descriptors in basic block %u for DMA engine %u QID %u` |
| `0x806eb8` | `Descriptor added for reprogram m2s_low  src={0x%lx} dst={0x%lx}` |
| `0x806f08` | `Descriptor added for reprogram m2s_high src={0x%lx} dst={0x%lx}` |
| `0x806f58` | `Descriptor added for reprogram m2s_size src={0x%lx} dst={0x%lx}` |
| `0x806fa8` | `Descriptor added for reprogram s2m_low  src={0x%lx} dst={0x%lx}` |
| `0x806ff8` | `Descriptor added for reprogram s2m_high src={0x%lx} dst={0x%lx}` |
| `0x807048` | `Descriptor added for reprogram s2m_size src={0x%lx} dst={0x%lx}` |

Each descriptor is a **DMA copy** whose **DST is the target ring CSR**, resolved
by a host queue-CSR offset getter (`nm`-confirmed symbols):

| field | host getter | symbol addr |
|---|---|---|
| `m2s_low`  | `get_dma_queue_base_low_offset`  | `0x318ac0` |
| `m2s_high` | `get_dma_queue_base_high_offset` | `0x318b20` |
| `m2s_size` | `get_dma_queue_size_offset`      | `0x318b80` |

Each getter computes `<dir>_offset + ((q+1)<<12) + <register>`. The cayman
within-block register constants are byte-exact this pass:

```c
/* aws_hal_udma_get_m2s_queue_offset_cayman @0x473a90  [OBSERVED HIGH] */
/*   473a90: lea eax,[rdi+0x1]   ; q + 1                              */
/*   473a93: shl eax,0xc         ; << 12                             */
queue_base = (queue_id + 1) << 12;        /* 0x1000-stride per-queue bank */

/* the cayman ring-CSR offset getters (mov edx,<const>; ret) — byte-exact */
m2s_base_ptr_lo_off @0x473ab0 = 0x1028;   /* = queue_base(q=0) + TDRBP_low  +0x28 */
m2s_base_ptr_hi_off @0x473af0 = 0x102c;   /* = queue_base(q=0) + TDRBP_high +0x2c */
m2s_len_off         @0x473b30 = 0x1030;   /* = queue_base(q=0) + TDRL       +0x30 */
/* s2m mirror @0x473ad0/473b10/473b50 = 0x1028/0x102c/0x1030, in the S2M sub-block */

/* the M2S/S2M direction sub-block split — byte-exact */
m2s_offset_cayman @0x473a30 = 0;          /* xor eax,eax ; ret  -> M2S @ +0      */
s2m_offset_cayman @0x473a40 = 0x20000;    /* mov eax,0x20000    -> S2M @ +0x20000 */
```

> **NOTE — within-block vs absolute.** `0x1028/0x102c/0x1030` are the **absolute**
> offsets for `q=0` (because `(0+1)<<12 = 0x1000`). The within-queue-block field
> offsets are `TDRBP_low +0x28`, `TDRBP_high +0x2c`, `TDRL +0x30` — exactly the
> [al_udma HW engine](../../dma/udma-hw-engine.md) M2S register map (`0x28`/`0x2c`/
> `0x30`), stride `0x1000`. For queue `q` the DST is `<dir> + ((q+1)<<12) + 0x28`
> etc. The S2M side uses the same field offsets (`RDRBP_low/high@0x28/0x2c`,
> `RDRL@0x30`) inside the S2M sub-block at `+0x20000`. `[CARRIED — udma-hw-engine.md]`

So the reprogram is a write of the ring's **new `{base_lo, base_hi, len}`** into
the per-queue M2S and S2M descriptor-ring CSRs of **each participating engine** —
the literal "reload the standing pring's ring pointer/size in place" re-arm.

### 1.2 The re-arm reset — `q_sw_ctrl rst_tail_ptr` `[OBSERVED HIGH]`

After the base/size reload the ring is re-armed via the `q_sw_ctrl` reset, which
returns the tail pointer to the ring base before the new step's descriptors are
posted. Cayman within-block constants (byte-exact `mov edx,<const>; ret`):

```c
m2s_queue_sw_ctrl_off_cayman @0x473c50 = 0x10b0;  /* = (q=0) + q_sw_ctrl +0xb0 */
s2m_queue_sw_ctrl_off_cayman @0x473c70 = 0x1064;  /* = (q=0) + q_sw_ctrl +0x64 */
```

`q_sw_ctrl` carries `rst_tail_ptr[1]` / `rst_data_tail_ptr[4]` (the per-queue
re-arm reset). The M2S `q_sw_ctrl` sits at within-block `+0xb0`, the S2M at `+0x64`
— matching [udma-hw-engine.md](../../dma/udma-hw-engine.md) (M2S `q_sw_ctrl@0xb0`,
S2M `q_sw_ctrl@0x64`). `[OBSERVED HIGH — the two getters; CARRIED — the bit fields]`

### 1.3 The reprogram section — a dedicated descriptor sub-list `[OBSERVED HIGH]`

The reprogram descriptors are **not** free-standing; they are pushed into a named
**reprogram section** of the standing pring (the pring's `reset_section`,
[pring-descriptors.md](pring-descriptors.md)). The host push primitive is
**`encd_dma_toplevel_section_descs_push_back`** (`@0x22fdd0`), which reads the
section handle's `used_desc_count` / `allocated_desc_count`. The section is
assembled from several sub-sections, each a `push_back` with a verbatim string:

| `.rodata` | sub-section |
|---|---|
| `0x806c60` | `Descriptor added for reprogram CTRL SPAD src/dst/size` — the SPAD-CTRL reload |
| `0x806d00` | `Descriptor added for reprogram SLOT %d SPAD …` — the per-slot SPAD |
| `0x806eb8…807048` | the six M2S/S2M `low`/`high`/`size` ring CSRs (§1.1) |
| `0x806e00` | `Descriptors added to reprogram section added for basic block %u toplevel_desc_n=%u` |
| `0x807190` | `Descriptors added to reprogram section (padding) … toplevel_desc_n=%u` — the padding tail |
| `0x806d60` | `failed to push descriptors to reprogram section for basic block %u` (error) |
| `0x8071f8` | `Failed to add ToplevelDMA packets for reprogram section of basic block %u` (error) |

So the reprogram section **re-writes, per basic block**: the SPAD-CTRL + per-slot
SPAD, the M2S/S2M ring base/size, and a padding tail — the complete per-step re-arm
of the standing pring.

### 1.4 The per-engine gate `[OBSERVED HIGH]`

The reprogram loop is gated per queue-bundle on the **used-engine bitmap** (§3.3):

```asm
2483c4:  call  encd_dma_get_used_dma_engine_bitmap_for_queue_bundle  ; @0x231720 -> eax
2483d6:  test  eax,eax
2483d8:  je    2476b1                  ; bitmap == 0 (no engines used) -> SKIP this qb
```

i.e. the firmware lays reprogram descriptors only for engines whose bit is set in
the queue-bundle's used bitmap, and only for the `(engine, QID)` pairs in use.
`[OBSERVED HIGH — the gate disasm inside encd_load_executable]`

### 1.5 The device-side `reprogram_info` struct (libncfw decoder) `[OBSERVED HIGH]`

The firmware's own reprogram-config struct is decoded by
**`ncfw_log_dma_reprogram_info`** (`@libncfw 0x4571`, **4 arch copies**). The
decoder emits four SoC-address fields with these keys (`.rodata`):

| struct off | key | `.rodata` |
|---|---|---|
| `+0x00` | `m2s_low`  | `0x65137` |
| `+0x04` | `m2s_high` | `0x6513f` |
| `+0x08` | `s2m_low`  | `0x65148` |
| `+0x0c` | `s2m_high` | *(reuses the address-print helper; no distinct key string)* |

Each is a 4-byte SoC address printed via the `ncfw_log_addr` helper. The **size**
leg (`m2s_size`/`s2m_size`) lives in the host reprogram descriptors (§1.1) and the
basic-block table; the device struct mirrors only the **address** half.
`[OBSERVED HIGH — the field keys + the 4 arch copies]`

---

## 2. The APB broadcast — one write fans to N DMA engines

The ring *trigger* (the tail-pointer-increment doorbell) and the re-arm reset are
**identical across all engines in a group**, so they are worth broadcasting: one
write to a per-engine **`BCAST_UDMA`** window, which the fabric fans to the whole
group. This is the complement of §1 (ring *setup* per-engine; ring *trigger*
broadcast).

### 2.1 The bcast doorbell address `[OBSERVED HIGH]`

The per-engine APB-broadcast doorbell address is resolved by
**`tdrv_arch_get_dma_queue_regs_bcast_offset_cayman`** (`@0x30c1e0`):

```asm
30c1ea:  test  ecx,ecx
30c1ec:  je    30c220              ; ecx==0 -> M2S path ; else S2M path
; --- M2S path ---
30c1ee:  call  aws_hal_udma_get_m2s_offset      ; 0
30c1f9:  call  aws_hal_udma_get_m2s_queue_offset ; (q+1)<<12
30c1fe:  add   rax,rbx
30c209:  lea   rcx,[rip+0x6d2d30]   ; # 0x9def40 <cayman_bcast_region_table>
30c212:  add   rax,[rcx+rdx*8]      ; + bcast_region_table[engine_region_idx]
; --- S2M path @30c220: get_s2m_offset (=0x20000) + get_s2m_queue_offset + same table ---
```

```c
/* [OBSERVED HIGH — the resolver chain disassembled] */
bcast_addr = cayman_bcast_region_table[engine_region_idx]   /* per-engine bcast base */
           + (direction ? 0x20000 : 0)                      /* M2S @ +0 | S2M @ +0x20000 */
           + ((queue_id + 1) << 12)                         /* the per-queue base */
           + reg;                                           /* the within-block register */
```

The three broadcast register variants (each adds the resolver above, then the
unicast register offset, `nm`-confirmed `t` symbols):

| host getter | symbol addr | register (cayman abs / within-block) |
|---|---|---|
| `get_dma_queue_tail_inc_bcast_offset`      | `0x318830` | `tail_ptr_inc`      `0x1038` / **`+0x38`** |
| `get_dma_queue_data_tail_inc_bcast_offset` | `0x318870` | `data_tail_ptr_inc` `0x10e0` / **`+0xe0`** |
| `get_dma_queue_sw_ctrl_bcast_offset`       | `0x3188b0` | `sw_ctrl`           `0x10b0` / **`+0xb0`** |

> **CORRECTION — the register constants are `0x10xx`, not `0xxx`.** The cayman
> getters return the **absolute** `q=0` offsets, byte-exact this pass:
> `aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset_cayman@0x473bf0 = 0x1038`,
> `…data_tail_ptr_inc…@0x473c30 = 0x10e0`, sw_ctrl `@0x473c50 = 0x10b0`. The
> within-block deltas are `+0x38` / `+0xe0` / `+0xb0` (`= abs − 0x1000`). `+0x38`
> is exactly the [udma-hw-engine.md](../../dma/udma-hw-engine.md) **`TDRTP_inc`
> doorbell** (`THE DOORBELL`, field `val[23:0]`). `[OBSERVED HIGH]`

The `tail_inc` getter (`@0x318830`) is shown verbatim:

```asm
318830 <get_dma_queue_tail_inc_bcast_offset>:
  31883c:  call  tdrv_arch_get_dma_queue_regs_bcast_offset   ; region+dir+queue base -> rbx
  318844:  test  ebp,ebp
  318846:  je    318860                                      ; M2S vs S2M
  318848:  call  aws_hal_udma_get_m2s_queue_tail_ptr_inc_offset  ; +0x38 (within block)
  318851:  add   rax,rbx                                     ; full bcast doorbell addr
```

### 2.2 The `bcast_region_table` **is** the `BCAST_UDMA` aperture (`+0x80000`) `[OBSERVED HIGH — numeric proof]`

`cayman_bcast_region_table` (`.rodata @0x9def40`, 8× `u64`, dumped this pass):

| idx | value | = SDMA channel base `+0x80000` |
|---|---|---|
| `[0]` | `0x001002080000` | `APB_SE_0` `SDMA_0`  (`0x1002000000`) `+0x80000` |
| `[1]` | `0x001003080000` | `APB_SE_0` `SDMA_16` (`0x1003000000`) `+0x80000` |
| `[2]` | `0x005004080000` | `APB_SE_1` `SDMA_0`  (`0x5004000000`) `+0x80000` |
| `[3]` | `0x005005080000` | `APB_SE_1` `SDMA_16` (`0x5005000000`) `+0x80000` |
| `[4]` | `0x801002080000` | die1 mirror of `[0]` (bit 47 set) |
| `[5]` | `0x801003080000` | die1 mirror of `[1]` |
| `[6]` | `0x805004080000` | die1 mirror of `[2]` |
| `[7]` | `0x805005080000` | die1 mirror of `[3]` |

Each entry is the SDMA channel base `+0x80000`, **exactly** the
[`BCAST_UDMA`](../../dma/sdma-windows-apb.md) aperture. The committed
[SDMA windows page](../../dma/sdma-windows-apb.md) establishes that a single SDMA
channel is a `0x100000` (1 MiB) slot whose lower `0x80000` is the channel
container; `BCAST_UDMA` populates `+0x80000` and **exists only on `SDMA_0` /
`SDMA_16`** of each cluster. The 8 table entries are therefore exactly:

```
{ APB_SE_0, APB_SE_1 }  ×  { SDMA_0, SDMA_16 }  ×  { die0, die1 }
```

(`SDMA_16` is `base + 16·0x100000 = base + 0x1000000`; die1 sets bit 47, e.g.
`0x801002080000 = 0x1002080000 | 0x800000000000`). A single write to one entry's
`tail_ptr_inc` (`+0x38`) hits the **broadcast mirror**, and the hardware fans it
to the group whose membership is the `papb_bcast.grps` mask (FIS).

> **NOTE — the index.** The table has **8 entries**; the caller passes a small
> engine→region index `edx` (0..7) into `[rcx+rdx*8]`. `add_dma_tail_inc_bcast`
> (`@0x273480`) supplies that index from the engine class. `[OBSERVED HIGH — the
> 8 table bytes + the `+0x80000` numeric identity; the per-cycle fabric fan-out
> is HW, see §2.6]`

### 2.3 The bcast doorbell **value** `[OBSERVED HIGH]`

The host computes the bcast doorbell value in
**`encd_basic_block_build_toplevel_mhandles_for_qbi`** (`@0x246070`). The full
byte-exact sequence (`@0x24611f..0x24614a`):

```asm
24611f:  call  encd_arch_get_pcore_idx_for_qbundle   ; eax = pcore_idx
246124:  mov   rdi,[r13+0x8]
24612e:  shl   eax,0x4                                ; pcore_idx << 4
246131:  mov   ecx,eax                                ; cl = shift amount
246133:  mov   eax,[rsp+0x20]                         ; the dma_engines_bitmap (arg)
246137:  shr   eax,cl                                 ; >> (pcore_idx << 4)
24613e:  not   eax                                    ; ~ (complement)
246140:  shl   eax,0x10                               ; << 16
246143:  or    ah,0x1                                 ; | 0x100  (bit 8)
246146:  mov   [rsp+0x38],eax                         ; m2s_apb_bcast value
24614a:  mov   [rsp+0x3c],eax                         ; s2m_apb_bcast value (same)
```

```c
/* [OBSERVED HIGH — the full shr/not/shl/or chain] */
shift  = pcore_idx << 4;                               /* select the per-pcore 16-bit lane */
lane   = (dma_engines_bitmap >> shift) & 0xffff;       /* this pcore's 16 engines */
value  = ((uint32_t)(~(dma_engines_bitmap >> shift)) << 16) | 0x100;
m2s_apb_bcast = value;
s2m_apb_bcast = value;                                  /* identical for both directions */
```

> **CORRECTION — the value is NOT a bare `(bitmap<<16)|0x100`.** Two steps the
> simplified form omits are byte-confirmed: a **`shr eax,cl`** by `pcore_idx<<4`
> (shift the bitmap down to this pcore's 16-engine lane) and a **`not eax`**
> (complement) **before** `shl 0x10` / `or ah,0x1`. So the high 16 bits carry the
> **complement** of the participating-engine lane mask, and the low field is
> `0x100` (`or ah,0x1` sets bit 8). The "name the group + arm the ring in one
> write" reading still holds, but the high bits are the **inverted** lane mask,
> not the raw bitmap. `[OBSERVED HIGH]`

The value is written per engine via `encd_devmem_copyin`; the verbatim log strings
name both the value and the source bitmap:

| `.rodata` | string |
|---|---|
| `0x8064d8` | `[nec_dev %u] copying to 0x%lx m2s_apb_bcast %x dma_bitmap=%x` |
| `0x806548` | `[nec_dev %u] copying to 0x%lx s2m_apb_bcast %x dma_bitmap=%x` |
| `0x8064a8` | `[nec_dev %d] Failed to copy m2s_apb_bcast_value` |
| `0x806518` | `[nec_dev %d] Failed to copy s2m_apb_bcast_value` |

### 2.4 The `dma_apb_bcast` config struct (libncfw decoder) `[OBSERVED HIGH]`

The firmware's per-channel `dma_apb_bcast` struct is decoded by
**`ncfw_log_dma_channel_apb_bcast`** (`@libncfw 0x4899`, **4 arch copies**):

| struct off | key | `.rodata` | meaning |
|---|---|---|---|
| `+0x00` | `m2s_tail_ptr` | `0x65150` | the pre-resolved M2S bcast doorbell SoC addr (§2.1 arithmetic done at build time) |
| `+0x08` | `s2m_tail_ptr` | `0x6515d` | the S2M bcast doorbell SoC addr |
| `+0x10` | `mask`         | `0x6516a` (`"mask"`) | the engine group mask (the firmware's view of `papb_bcast.grps`) |

The two `*_tail_ptr` fields are SoC addresses printed via `ncfw_log_addr`; `mask`
is the group mask. A `dma_apb_bcast` **mesh-event** variant additionally prints
`dma_apb_bcast_%d` (`0x65246`, two per event), `dma_apb_rsvd_addr` (`0x652cd`, the
reserved APB address), and `dma_engines_bitmap` (`0x6535c`) — the
[NCFW DMA-naming](ncfw-dram-ctx-log.md) field. `[OBSERVED HIGH — the three field
keys + the mesh-event variant keys]`

### 2.5 Which registers are broadcast- vs per-engine-programmed `[OBSERVED HIGH]`

This is the clean division of labour, and it falls **directly** out of which
getters exist for the bcast path vs which CSRs the reprogram descriptors target:

| register | within-block / abs | programmed | why |
|---|---|---|---|
| `tail_ptr_inc` (`TDRTP_inc`) | `+0x38` / `0x1038` | **BROADCAST** | the DMA **arm** (data-plane doorbell) — identical across the group |
| `data_tail_ptr_inc` | `+0xe0` / `0x10e0` | **BROADCAST** | the enhanced-prefetch arm — identical across the group |
| `q_sw_ctrl` (`rst_tail_ptr`) | `+0xb0` / `0x10b0` | **BROADCAST** | the re-arm reset — identical across the group |
| `base_ptr_lo` (`TDRBP_low`) | `+0x28` / `0x1028` | **PER-ENGINE** | each engine's ring is at a distinct HBM address |
| `base_ptr_hi` (`TDRBP_high`) | `+0x2c` / `0x102c` | **PER-ENGINE** | (cannot be broadcast) |
| `len` (`TDRL`) | `+0x30` / `0x1030` | **PER-ENGINE** | ring length differs per engine |

The bcast path has **only** `tail_inc` / `data_tail_inc` / `sw_ctrl` variant
getters — **no** `base`/`len` bcast getter exists — confirming the base/size CSRs
are never broadcast and are written per `(engine, QID)` in the reprogram section
(§1). Ring **setup** (base/size) is per-engine; ring **trigger** + re-arm is
broadcast to the group. `[OBSERVED HIGH — the getter roster vs the reprogram CSR
targets]`

> **NOTE — `cayman_get_apb_bcast_m2s_offsets`.** `@0x25ced0` resolves the queue
> id, the pcore, and `cayman_get_dma_alloc_bitmap`, then **tail-jumps** to
> `get_dma_queue_data_tail_inc_bcast_offset` (`@0x318870`) — i.e. the M2S
> "apb-bcast offsets" path arms the **enhanced-prefetch** (`data_tail_inc`)
> doorbell. `[OBSERVED HIGH — the tail-jmp]`

### 2.6 The fabric path `[CARRIED — sdma-windows-apb.md]`

A bcast APB write traverses (per the [SDMA windows + APB chain](../../dma/sdma-windows-apb.md)):
the APB-IO window (BAR3 / PEB bit 53) → the fabric crossbar + URB router →
the APB firewall (CAM allow/block) → the daisy-chain (the 32 `USER_SE` SDMA + 32
`USER_FIS` SDMA nodes) → the per-channel FIS (`fis_control` apb_decode/remap) →
the `BCAST_UDMA_M2S` CSR. The `papb_bcast.grps` mask register (FIS) gates the
fan-out group; the NCFW `dma_apb_bcast.mask` (§2.4) is the firmware's view of that
group membership. `[CARRIED — the chain blocks + the papb_bcast.grps mask are
OBSERVED on the committed page; the end-to-end write data-flow is INFERRED MED]`

---

## 3. The alloc bitmap — the resource-allocation bitmap

### 3.1 The static `dma_alloc_bitmap` table `[OBSERVED HIGH — dumped this pass]`

`cayman_dma_alloc_bitmap` (`.rodata @0x9d20a0`, 8× `u64`). Each `u64` packs two
32-bit masks — the two-half structure is **proven** by the consumer (§3.2):

| idx | `u64` | low 32 = engine mask | high 32 = queue mask |
|---|---|---|---|
| `0` | `0x0000ffff0000000b` | `0x0b` = engines `{0,1,3}`   | `0x0000ffff` = die0 q`[0..15]` |
| `1` | `0x0000ffff0000000c` | `0x0c` = engines `{2,3}`     | `0x0000ffff` |
| `2` | `0x0000ffff0000000d` | `0x0d` = engines `{0,2,3}`   | `0x0000ffff` |
| `3` | `0x0000ffff0000000f` | `0x0f` = engines `{0,1,2,3}` | `0x0000ffff` |
| `4` | `0xffff00000000000b` | `0x0b` = engines `{0,1,3}`   | `0xffff0000` = die1 q`[16..31]` |
| `5` | `0xffff00000000000c` | `0x0c` = engines `{2,3}`     | `0xffff0000` |
| `6` | `0xffff00000000000d` | `0x0d` = engines `{0,2,3}`   | `0xffff0000` |
| `7` | `0xffff00000000000f` | `0x0f` = engines `{0,1,2,3}` | `0xffff0000` |

The **low 32** is the per-NC engine-aperture mask (bits `{0,1,2,3}` = the 4 per-NC
compute-engine apertures); the **high 32** is the queue/channel mask
(`0xffff` = die0's 16 queues, `0xffff0000` = die1's). The 8 entries = 4
engine-aperture patterns × 2 dies. `[bytes OBSERVED HIGH; the half semantics
PROVEN via §3.2]`

### 3.2 The getter + the two-half decode `[OBSERVED HIGH]`

**`cayman_get_dma_alloc_bitmap`** (`@0x25cd10`) validates the index (`idx <= 1`,
`idx <= 3`, `idx <= 7` asserts), then indexes the static table by the queue-bundle
id:

```asm
25cd41:  lea   rax,[rip+0x775358]   ; # 0x9d20a0 <cayman_dma_alloc_bitmap>
25cd48:  mov   rax,[rax+rbx*8]      ; cayman_dma_alloc_bitmap[qbundle]  (stride 8)
25cd51:  ret
```

It is arch-dispatched via **`encd_arch_get_dma_alloc_bitmap`** (`@0x255cc0`;
mariana `@0x259430` / sunda `@0x25f030`). The **consumer**
`get_dma_queue_info` (`@0x232540`) **proves** the half split:

```asm
232614:  call  encd_arch_get_dma_alloc_bitmap   ; -> u64 in rax
232621:  shr   rax,0x20                         ; take the HIGH 32 (the queue mask)
232625:  tzcnt eax,eax                          ; first set bit = lowest free queue id
```

So the **high half** is a per-bit queue/channel allocation mask (`tzcnt` =
find-first-allocated channel); the **low half** is the engine mask used elsewhere.
`[OBSERVED HIGH — the getter index + the shr/tzcnt consumer]`

### 3.3 The bitmap operations — SET / FIND-FREE / VALIDITY `[OBSERVED HIGH]`

**SET (OR-in)** — build the runtime `dma_engines_bitmap` from the in-use channels.
`encd_dma_get_used_dma_engine_bitmap_for_queue_bundle` (`@0x231720`) walks the
channel array and for each in-use channel ORs its engine bit in:

```asm
231790:  mov   ebx,r8d        ; r8d = 1
231793:  sub   ecx,edi        ; ecx = engine_local - base  (the bit position)
231795:  shl   ebx,cl         ; 1 << bit
231797:  or    eax,ebx        ; bitmap |= (1 << engine_id)
```

This is the exact runtime construction of the
[NCFW `dma_engines_bitmap`](ncfw-dram-ctx-log.md).

**FIND-FREE (first-set)** — allocate a channel: `get_dma_queue_info @0x232625`
`tzcnt eax, high32` (§3.2), the find-first op over the queue mask.

**VALIDITY (is engine-id allocatable?)** — `cayman_get_mesh_dma_engine_id_from_tbl`
(`@0x25d4b0`) resolves an engine-id from the topology table
`cayman_mesh_dma_tbl_intra_chip` (`@0x9cdcc0`; engine-ids appear in contiguous
blocks of 4, e.g. `{4,5,6,7}`, `{0x14..0x17}`, with `-1`/`0xffffffff` sentinels)
and asserts it against `valid_dma_engines_bitmap`:

```asm
25dcdf:  mov   edx,0xffff       ; 16-engine valid mask
25dce7:  mov   eax,0xffffffff   ; 32-engine valid mask
25dcec:  cmove eax,edx          ; pick 16 vs 32 per num_tpb_per_vcore
25dcef:  bt    eax,ebx          ; (1 << engine_id) & valid_dma_engines_bitmap
```

So `valid_dma_engines_bitmap = 0xffff` (16-engine config) or `0xffffffff`
(32-engine), and the assert string `(1 << dma_engine_id) & valid_dma_engines_bitmap`
(`.rodata @0x80ae48`) is byte-confirmed at this site — the byte-exact form of the
[NCFW-naming](ncfw-dram-ctx-log.md) validity gate. `[OBSERVED HIGH — all three ops
disassembled]`

### 3.4 The device-side `dma_alloc_bitmap` (libncfw decoder) `[OBSERVED HIGH]`

The firmware DRAM config carries `dma_alloc_bitmap` as a `u64` **array**, decoded
by **`ncfw_log_configs_neff_dma_alloc_bitmap`** (`@libncfw 0xce01`, **4 arch
copies**). The decoder prints a **JSON array**: the array key `"%s": [`
(`.rodata @0x650fd`) followed by per-element `"0x%016…"` reads (the loop walks the
config array with stride 8). The parent caller emits the `"dma_alloc_bitmap"` key
(`@0x654d1`) then calls this array-printer. So the device-resident
`dma_alloc_bitmap` is the `u64`-per-qbundle array **mirroring** the host static
table (§3.1) — the device reads what the host laid down. `[OBSERVED HIGH — the
JSON-array key + the stride-8 element loop + the parent key]`

### 3.5 The APB-bcast mask is *also* die-banked `[OBSERVED HIGH]`

`cayman_get_apb_bcast_mask` (`@0x25bd70`) resolves the used qbundle id, reads a
per-qbundle `u32` from the passed table, then **selects a 16-bit half by qbundle
id** — the same die-banking pattern as the alloc bitmap:

```asm
25bd77:  call  cayman_get_used_qbundle_id        ; eax = qbundle id
25bd7f:  mov   edx,[rbx+rdx*4]                    ; per-qbundle u32 (stride 4)
25bd82:  cmp   eax,0x3
25bd85:  jle   25bd8f                             ; qbundle <= 3 -> die0, take low 16
25bd87:  test  dx,dx                              ; (die1 path)
25bd8c:  shr   edx,0x10                           ; qbundle > 3 -> take high 16 (die1)
25bd8f:  mov   eax,edx                            ; return the 16-bit group mask
```

> **CORRECTION — `apb_bcast_mask` is a per-qbundle `u32`, returning a `u16`.** The
> table element is read with **stride 4** (`mov edx,[rbx+rdx*4]`); its low 16 bits
> are the die0 group mask, high 16 the die1 group mask. The selector takes the
> high half for qbundle id `> 3` (die1), low half otherwise. The **returned** value
> is the 16-bit group mask. `[OBSERVED HIGH]`

### 3.6 Reconciliation vs the `dma_engines_bitmap` `[HIGH]`

- The [NCFW `dma_engines_bitmap`](ncfw-dram-ctx-log.md) (the per-collective
  participating-engine 32-bit mask) is **built at runtime** from the static
  `dma_alloc_bitmap` (§3.1) by OR-ing the in-use engines (§3.3 SET). The static
  alloc bitmap is the **allocation pool** (what a queue-bundle *may* use);
  `dma_engines_bitmap` is the **active subset** (what a step *does* use). `[HIGH]`
- `valid_dma_engines_bitmap` (here byte-decoded as `0xffff` / `0xffffffff`) is the
  **silicon validity mask** (16 vs 32 engines per TPB), enforced by the bt-test
  (§3.3). `[HIGH]`
- The alloc bitmap's two halves resolve the `engine-id (0..31) + queue_id (0..15)`
  naming model: the **low** half indexes the per-NC engine aperture, the **high**
  half is the 16-queue mask (die-banked). The `apb_bcast_mask` (§3.5) is
  die-banked the same way. `[HIGH — the consumer shr/tzcnt + the static bit
  pattern + the bcast-mask half-select]`

---

## 4. The step → (alloc, reprogram, bcast) mapping

One collective **step** composes the three legs. The host **build** order is
`OBSERVED HIGH`; the on-device per-step **execution** order runs in the
[scalar-LX dispatch loop](main-dispatch-loop.md), which is FLIX-undecodable, so the
runtime sequencing is `INFERRED-STRONG` from the build order + the pring
`reset_section` gating.

```c
/* ============ ALLOC  (per queue-bundle) ============ [OBSERVED HIGH] */
u64 alloc = cayman_get_dma_alloc_bitmap(qbundle);  /* static {low=engine apertures, high=queue mask} */
u32 dma_engines_bitmap = encd_dma_get_used_dma_engine_bitmap_for_queue_bundle(qb);  /* OR-in §3.3 */
/* validity-gated: each id asserted via  (1<<id) & valid_dma_engines_bitmap  (0xffff|0xffffffff) */

/* ============ REPROGRAM  (per engine in the bitmap) ============ [OBSERVED HIGH] */
if (dma_engines_bitmap == 0) skip_queue_bundle();           /* the §1.4 gate */
for (engine in set_bits(dma_engines_bitmap))
  for (qid in used_qids(engine)) {
    /* six per-engine ring-CSR descriptors into the reprogram section (§1.1) */
    emit_reprogram_desc(M2S, base_lo @ (q+1)<<12 + 0x28);   /* per-engine: distinct HBM ring */
    emit_reprogram_desc(M2S, base_hi @ (q+1)<<12 + 0x2c);
    emit_reprogram_desc(M2S, len     @ (q+1)<<12 + 0x30);
    emit_reprogram_desc(S2M, base_lo @ 0x20000 + (q+1)<<12 + 0x28);
    emit_reprogram_desc(S2M, base_hi @ 0x20000 + (q+1)<<12 + 0x2c);
    emit_reprogram_desc(S2M, len     @ 0x20000 + (q+1)<<12 + 0x30);
    emit_reprogram_desc(q_sw_ctrl rst_tail_ptr @ +0xb0/+0x64);  /* the re-arm reset (§1.2) */
  }

/* ============ BCAST  (per queue-bundle) ============ [OBSERVED HIGH] */
shift = pcore_idx << 4;
value = ((u32)(~(dma_engines_bitmap >> shift)) << 16) | 0x100;     /* §2.3 (note: shr+not!) */
addr  = cayman_bcast_region_table[engine_region_idx]              /* the BCAST_UDMA aperture */
      + (direction ? 0x20000 : 0) + ((qid+1)<<12) + 0x38;         /* the tail_ptr_inc doorbell */
write(addr, value);   /* ONE write -> the fabric fans to the papb_bcast group (§2.2/2.6) */
```

So: **ALLOC** names the engines/queues → **REPROGRAM** reloads each engine's ring
base/size per-engine → **BCAST** arms the masked group with one fanned tail-pointer
write. The reprogram (per-engine ring base/size) and the broadcast (group-wide ring
trigger) are **complementary**: base/size *cannot* be broadcast (distinct per
engine), the tail-ptr arm *can* (identical across the group). `[the three legs
OBSERVED HIGH; the per-step sequencing INFERRED-STRONG — the LX dispatch loop is
FLIX-undecodable]`

---

## 5. Per-generation stability

| leg | cayman | mariana | sunda |
|---|---|---|---|
| reprogram CSR offsets (`+0x28`/`+0x2c`/`+0x30`, sw_ctrl `+0xb0`/`+0x64`), `(q+1)<<12` | byte-exact | same getter pattern | same (lacks the data-tail-inc pair) |
| bcast region table (`+0x80000` `BCAST_UDMA` scheme), 3 bcast getters, the `(~(bitmap>>shift)<<16)\|0x100` value | single host binary — gen-stable | — | — |
| `dma_apb_bcast` struct (`m2s_tail_ptr@0` / `s2m_tail_ptr@8` / `mask@0x10`) decoder | 4 arch copies, identical offsets | | |
| `dma_alloc_bitmap` static table | `@0x9d20a0` | `@0x9c8660` **byte-identical to cayman** | `@0x9d60e0` **OUTLIER** |
| alloc getter | `@0x25cd10` | `@0x259430` | `@0x25f030` |

- **ALLOC: `cayman == mariana` byte-identical** (8× `u64`, same values). **SUNDA is
  the outlier**: its high (queue) masks are `0xff00` and `0x003f` (vs `0xffff`),
  the smaller SUNDA fabric. `[OBSERVED HIGH — all three dumped this pass]`

> **NOTE — the SUNDA alloc layout differs structurally, not just numerically.**
> Cayman/mariana bank by die (idx 0..3 = `0x0000ffff`, idx 4..7 = `0xffff0000`).
> SUNDA **alternates** the two smaller masks per index: idx `{0,2,4,6}` =
> `0x0000ff00`, idx `{1,3,5,7}` = `0x0000003f` (always in the **low** dword, no
> die-1 `0xffff0000` form). So SUNDA's 8 entries are 4 engine patterns × 2
> queue-mask sizes, not 4 × 2 dies. `[OBSERVED HIGH — sunda table dumped]`

- The three libncfw ctx_log decoders (`ncfw_log_dma_reprogram_info`,
  `_dma_channel_apb_bcast`, `_configs_neff_dma_alloc_bitmap`) each ship in **4 arch
  copies** with identical field-offset structure (the per-copy byte diff is only
  the rip-relative key/helper targets). `[OBSERVED HIGH — 4 copies each via nm]`

---

## 6. Confidence ledger

**HIGH / OBSERVED (bytes / disasm / DWARF / table-dump this pass):**

- **lib identities** re-verified: `libncfw` sha `598920d7…` / BuildID `a98f8e1c…`;
  `libnrt` sha `956382de…` / BuildID `8bb57aba…`; section deltas confirmed.
- **REPROGRAM**: the seven reprogram strings (`@0x806e60`/`806eb8…807048`); the
  getters `get_dma_queue_base_low/high/size_offset` (`@0x318ac0`/`318b20`/`318b80`)
  → cayman `0x1028`/`0x102c`/`0x1030` (`@0x473ab0`/`473af0`/`473b30`) + the S2M
  mirror; sw_ctrl `0x10b0`/`0x1064` (`@0x473c50`/`473c70`); `queue=(q+1)<<12`
  (`@0x473a90`); M2S/S2M sub-block `0`/`0x20000` (`@0x473a30`/`473a40`); the
  per-engine gate (`@0x2483c4`, `je 2476b1`); `…push_back` (`@0x22fdd0`); the
  reprogram-section sub-section strings; the device `reprogram_info` struct keys
  (`m2s_low@0x65137`/`m2s_high@0x6513f`/`s2m_low@0x65148`).
- **APB BROADCAST**: the resolver `tdrv_arch_get_dma_queue_regs_bcast_offset_cayman`
  (`@0x30c1e0`) = `m2s_offset(0)`/`s2m_offset(0x20000)` + `(q+1)<<12` +
  `cayman_bcast_region_table[idx]`; the 3 bcast getters (`@0x318830`/`318870`/
  `3188b0`); the cayman reg constants `0x1038`/`0x10e0`/`0x10b0` (`@0x473bf0`/
  `473c30`/`473c50`); the `bcast_region_table` 8 `u64` (`@0x9def40`); **the numeric
  proof** `[0]=0x1002080000 = APB_SE_0 SDMA_0 (0x1002000000) + 0x80000`; the bcast
  value `shr/not/shl/or` chain (`@0x24611f`) + `copyin`; the
  `m2s/s2m_apb_bcast %x dma_bitmap=%x` strings (`@0x8064d8`/`806548`); the
  `dma_apb_bcast` struct keys (`m2s_tail_ptr@0x65150`/`s2m_tail_ptr@0x6515d`/
  `mask@0x6516a`); `cayman_get_apb_bcast_mask` (`@0x25bd70`, per-qb `u32`,
  die-half select); `cayman_get_apb_bcast_m2s_offsets` (`@0x25ced0`) tail-jmp to
  the data-tail-inc bcast getter; `add_dma_tail_inc_bcast` (`@0x273480`).
- **ALLOC BITMAP**: `cayman`/`mariana`/`sunda_dma_alloc_bitmap` dumped (`cayman ==
  mariana`, sunda outlier); `cayman_get_dma_alloc_bitmap` (`@0x25cd10`,
  `[qbundle*8]`); `encd_arch_get_dma_alloc_bitmap` (`@0x255cc0`); the consumer
  `get_dma_queue_info` (`@0x232540`) `shr rax,0x20` + `tzcnt`; the OR-in
  (`@0x231795` `shl ebx,cl`/`or eax,ebx`); the `valid_dma_engines_bitmap`
  `0xffff`/`0xffffffff` bt-test (`@0x25dcef` in `cayman_get_mesh_dma_engine_id_from_tbl`
  `@0x25d4b0`) + the `(1<<id)&valid` string (`@0x80ae48`); the device decoder
  `ncfw_log_configs_neff_dma_alloc_bitmap` (`@0xce01`) JSON-array key (`@0x650fd`)
  + the `dma_alloc_bitmap` key (`@0x654d1`); the three libncfw decoders × 4 copies.

**MED / INFERRED-STRONG (multiple OBSERVED facts converge):**

- The on-device per-step **order** (alloc → reprogram → bcast): the host build
  order is OBSERVED; the LX dispatch loop that executes it is FLIX-undecodable
  ([main-dispatch-loop](main-dispatch-loop.md)), so runtime sequencing is INFERRED.
- "The bcast tail-ptr write fans via the `papb_bcast` group": the `BCAST_UDMA`
  aperture identity is PROVEN (§2.2); the per-cycle fabric fan-out is HW
  ([sdma-windows-apb](../../dma/sdma-windows-apb.md)) — MED on the exact runtime
  group-membership wiring.
- Concrete **runtime values** per step (the actual `dma_engines_bitmap`, ring
  base/size addresses, queue ids, N) are populated in the LX firmware DRAM working
  area, not in any shipped static file.

**LOW / OPEN (cited boundaries — not recoverable from shipped artifacts):**

- The symbolic **CUST3 TIE op-word** bit semantics (which aperture bits the device
  doorbell op sets) — the LX `.tie` / `core-isa.h` does not ship. The op word,
  direction nibble, and target region are proven; the internal bit-fields are not.
  See [CUST3 doorbell thunks](cust3-doorbell-thunks.md).
- The exact `al_udma_desc` bitfield encoding of the reprogram **copy descriptors**
  (the AL UDMA HW register layout — ABI territory). See
  [pring-descriptors](pring-descriptors.md).
- **v5 NCFW**: no v5 NCFW firmware image ships; nothing about a v5 interior can be
  stated as fact. The host getters/tables above are v2–v4 byte-grounded.

---

*Cross-links:* [pring-descriptors](pring-descriptors.md) ·
[ncfw-dram-ctx-log](ncfw-dram-ctx-log.md) ·
[main-dispatch-loop](main-dispatch-loop.md) ·
[cust3-doorbell-thunks](cust3-doorbell-thunks.md) ·
[lx-isa-naming-archid-synthesis](lx-isa-naming-archid-synthesis.md) ·
[al_udma HW engine](../../dma/udma-hw-engine.md) ·
[SDMA windows + APB chain](../../dma/sdma-windows-apb.md) ·
[descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md) ·
[scalar-LX core](../../uarch/ncfw-lx-core.md)
