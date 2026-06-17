# Per-Arch Device Layer: STPB Engine-Init, DGE and Pooling

> *All addresses, offsets, symbol names, and register CSR offsets on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF (names, no line info — `addr2line` returns `:?` for this band). All four `PT_LOAD` segments are identity-mapped, so `.text`/`.rodata` are VMA == file offset; every `0x46…`/`0x47…` is an analysis VMA. The vendored HAL package is `KaenaHal-2.31.0.0` (Amazon `brazil-pkg-cache`, `AL2_x86_64/generic-flavor`); the per-arch leaf TUs root in `.../KaenaHal-2.31.0.0/.../src/src/{sunda,cayman,mariana}/arch/*` and `.../common/tpb/aws_hal_stpb_{,_act,_dve,_pe,_pooling,_common}.c`. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every `*_init` orchestrator, every per-engine leaf, and every register-pack idiom on this page is `nm`-pinned by symbol+address and the load/store/RMW byte sequences are `objdump`-decoded against the binary; the per-arch capability divergences (SUNDA stubs HW-decode; only MARIANA implements `force_rspcomb_eight_deep`; the DGE carveout cap monotone) are read directly from the leaf bodies, not inferred. · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

This page documents the **per-arch STPB engine-init backend leaves** — the bodies that actually program the on-TPB compute engines during device bring-up, one full copy per silicon generation (**SUNDA** = arch 2, **CAYMAN** = arch 3, **MARIANA** = arch 4). These are the functions the arch-dispatch trampolines tail-call: when the runtime invokes `aws_hal_stpb_dve_regs_init` ([hal-tpb-shims §1](hal-tpb-shims.md)), the trampoline asserts the arch is latched, loads `kaena_khal[+0x208]`, and jumps into `aws_hal_stpb_{sunda,cayman,mariana}_dve_regs_init` — one of the leaves owned here. The shim layer owns *which slot*; this page owns *what the leaf does to the silicon*. Every CSR write, ucode-blob bounds check, DMA-ring program, run-stall release, and the Pooling-engine Descriptor-Generation-Engine (DGE) carveout/mapping program lives in these leaves.

The four compute engines are indexed by a single enum threaded through every accessor — `PE=0, ACT=1, POOL=2, DVE=3` (and `SP=4`, the sync-processor, owned by [hal-tpb-shims §2](hal-tpb-shims.md)). For each engine, each arch ships the same shaped bring-up: an `*_init` orchestrator that sequences `regs_init → ucode_seq_init → [hw_decode_table_init] → dma_init → release_run_stall`, with Pooling inserting an extra `q7_ucode_eng_init` step and a *second* run-stall release (the Q7 engine stall is a distinct register from the sequencer stall). The leaves are pure register-builders sitting directly on the first-party `al_reg_{read,write}32` / `al_mem_write_buf` CSR primitives (`/opt/workspace/KaenaRuntime/tdrv/hal_platform.c`) below them and on the per-arch `aws_hal_arch_<arch>_*` register-offset accessors beside them. They take a per-engine `aws_hal_stpb_eng_info` descriptor (208 B) carved out of the master `aws_hal_stpb` context.

Three per-arch divergences repay reading before the tables, because each is a place a reimplementer who builds "one engine-init and parameterize by arch index" gets a *silently wrong* result. First, **SUNDA has no HW-decode**: all four `aws_hal_stpb_sunda_*_hw_decode_table_init` leaves are `xor eax,eax; ret` stubs, and SUNDA's `act_init`/`pe_init` orchestrators omit the call entirely. Second, **only MARIANA implements `force_rspcomb_eight_deep`** (DVE sequencer `regs+0x3c0`); SUNDA and CAYMAN stub both the setter and the offset-getter. Third, the **DGE carveout cap is a per-arch monotone** — `0x30000`/192 KiB on SUNDA, `0x38000`/224 KiB on CAYMAN, `0x40000`/256 KiB on MARIANA — baked as a literal `cmp` in each `pooling_set_dge_carveout` body. None of these is reachable from the shim; all three are properties of the leaf this page owns.

For reimplementation, the contract is:

- **The engine × arch leaf matrix** — for each `(engine, arch)` pair, the `*_init` orchestrator address and the ordered sub-step it drives, plus the per-step leaf (`regs_init`, `ucode_seq_init`, `hw_decode_table_init`, `dma_init`, `release_run_stall`). The orchestrator order is fixed; the *membership* of `hw_decode_table_init` is arch-conditional.
- **The register-build idioms** — the ACT FP-special-value CSR block (`regs+0x240..0x25c`, eight floats: `0 / -1 / +inf / -1 / -inf / -1 / qNaN / qNaN`), the DVE sequencer RNG block (`+0x304/+0x308/+0x30c/+0x310/+0x314`, LFSR reset seed `0xCD9E8D57`), the engine instr-cfg pack at `regs+0xA00`/`+0xA04`, and the ucode-seq bounds-check-then-`write_padded` template.
- **The Pooling DGE program** — `set_dge_carveout` (validate `offset+size ≤ cap`, require 16-byte alignment, pack `(size>>4)<<16 | (offset>>4)`, write the `sw_dge_carveout` NX-local reg) and `set_dge_dma_mapping` (pack `(arg2<<16) | arg1`, **no validation**), plus the dual Pooling run-stall release (sequencer then Q7 engine).
- **The three per-arch capability divergences** — SUNDA's no-HW-decode, MARIANA-only `force_rspcomb_eight_deep`, and the DGE carveout-cap monotone — each byte-anchored, each invisible from the dispatch layer.

| | |
|---|---|
| **What this page owns** | the `aws_hal_stpb_{sunda,cayman,mariana}_{pe,act,pool,dve}_*` leaf bodies — the real CSR programming the [hal-tpb-shims](hal-tpb-shims.md) trampolines tail-call |
| **Engine enum** | `PE=0, ACT=1, POOL=2, DVE=3` (`SP=4` → [hal-tpb-shims §2](hal-tpb-shims.md)); used as `eng` arg to `get_xt_local_reg_offset` / `get_seq_params` |
| **Arch enum** | `SUNDA=2, CAYMAN=3, MARIANA=4`; installed by `kaena_khal_register_funcs_v{2,3,4}` |
| **`*_init` order** | `regs_init → ucode_seq_init → [hw_decode_table_init] → dma_init → release_run_stall`; Pooling inserts `q7_ucode_eng_init` + a second (Q7) stall release |
| **Per-engine descriptor** | `aws_hal_stpb_eng_info` (208 B / `0xD0`), one each for pooling/act/pe/dve/sp inside `aws_hal_stpb` (1104 B / `0x450`) |
| **ACT FP-init CSRs** | `regs+0x240..0x25c` = `{0, 0xFFFFFFFF, 0x7F800000(+inf), 0xFFFFFFFF, 0xFF800000(-inf), 0xFFFFFFFF, 0x7FC00000(qNaN), 0x7FC00000}` |
| **DVE RNG block** | `regs+0x304` instr-dbg · `+0x308` timestamp-inc · `+0x30c` stochastic-rnd · `+0x310` rnd-mode · `+0x314` LFSR (reset seed `0xCD9E8D57`) |
| **DGE carveout cap** | SUNDA `0x30000` · CAYMAN `0x38000` · MARIANA `0x40000` (literal `cmp` per arch) |
| **First-party seam** | `al_reg_read32 @0x2658a0` · `al_reg_write32 @0x265c50` · `al_mem_write_buf @0x265990` · `al_hal_log @0x265b80` · `write_padded @0x473eb0` |

> **CORRECTION (ARCH-STPB) —** the SUNDA-band survey flagged `aws_hal_stpb_sunda_act_load_local` and `init_nx_registers` as "open all windows" arch specializations but did not classify the four SUNDA `*_hw_decode_table_init` leaves, which fell outside its address band. Direct decode here shows **all four** (`act @0x46d9a0`, `dve @0x46dde0`, `pe @0x46e3f0`, `pooling @0x46e8c0`) are `xor eax,eax; ret` — SUNDA ships *zero* HW-decode programming. The `dve_init` orchestrator still calls its stub (harmless `return 0`); `act_init`/`pe_init` skip the call. This is the arch behavioral diff that survey predicted but could not pin.

---

## 1. The Bring-Up Shape and the Engine × Arch Matrix

### Purpose

Every engine on every arch is brought up by the same five-stage pipeline, parameterized only by engine index and the per-arch register-offset accessors. Understanding the *shape* once lets a reimplementer read all twelve `(engine, arch)` orchestrators by reflex; the only thing that varies is the engine index, the per-arch CSR-offset getter, and three byte-observable capability deltas (§4). The orchestrator is the function the shim's NULL-slot policy protects: it is what `kaena_khal[engine_slot]` points at after `register_funcs_vN` runs.

### Entry Point

The runtime never calls a leaf directly. The path is shim → vtable slot → arch leaf orchestrator → in-leaf sub-steps:

```text
aws_hal_stpb_init (0x458350, REAL, hal-tpb-shims §4)
  └─ aws_hal_stpb_<eng>_init (shim @0x45bNNN, hal-tpb-shims §3)
       └─ jmp *kaena_khal[ENG_SLOT]                       ── installed by register_funcs_vN
            = aws_hal_stpb_<arch>_<eng>_init               ── THIS PAGE (orchestrator)
                 ├─ <arch>_<eng>_regs_init                 ── FP/RNG/instr-cfg CSRs
                 ├─ <arch>_<eng>_ucode_seq_init            ── IRAM/DRAM bounds + write_padded
                 ├─ <arch>_<eng>_hw_decode_table_init      ── CAM/PROFILE (SUNDA: stub)
                 ├─ [POOL only] aws_hal_q7_ucode_eng_init  ── trampoline → q7_ucode_eng_init_<arch>
                 ├─ <arch>_<eng>_dma_init                  ── 2 DMA ring triples + dma_ctrl
                 └─ <arch>_<eng>_release_run_stall         ── ungate the engine
                      └─ [POOL only] release_eng_run_stall ── second stall: Q7 engine reg
```

### Algorithm

A representative orchestrator, modelled on `aws_hal_stpb_cayman_act_init` (`@0x4724e0`, the only orchestrator wholly self-contained in one cell) — its SUNDA/MARIANA twins are byte-shaped identically apart from the conditional HW-decode call:

```c
// aws_hal_stpb_<arch>_act_init(cfg)        // cayman @0x4724e0 / mariana @0x4669f0 / sunda @0x46d9b0
// cfg = aws_hal_stpb_eng_info*  (208 B; act_info slice of the aws_hal_stpb context)
function act_init(cfg):
    al_hal_log(4, "%s: tpb_mem_handle=%p, tpb_regs=%p\n",        // @0x826a70, shared by ALL arch *_init
               __func__, cfg->tpb_mem_handle, cfg->tpb_regs)     //   +0x00 / +0x08

    rc = act_regs_init(cfg);                if rc != 0: return rc   // FP-init + instr-cfg + dbg
    rc = act_ucode_seq_init(cfg);           if rc != 0: return rc   // IRAM/DRAM bounds + load
    // --- ARCH-CONDITIONAL: present on CAYMAN/MARIANA; ABSENT on SUNDA act/pe orchestrators ---
    rc = act_hw_decode_table_init(cfg, cfg->disable_hw_decode);  // +208; SUNDA leaf is a stub anyway
    if rc != 0: return rc
    rc = act_dma_init(cfg);                 if rc != 0: return rc   // ring triples + dma_ctrl(1)
    return act_release_run_stall(cfg)                              // ungate
    // short-circuits on first non-zero — a failed stage aborts the engine, leaving it stalled.
```

`act_regs_init` (`cayman @0x471f90`) is the one whose register block a reimplementer must reproduce byte-for-byte — the eight FP-special-value CSRs are the activation accumulator's reduction-identity clamps:

```c
// aws_hal_stpb_<arch>_act_regs_init(cfg)   // cayman @0x471f90 / mariana @0x4664a0 / sunda @0x46d480
function act_regs_init(cfg):
    regs = cfg->tpb_regs                                  // +0x08
    // FP special-value detect/clamp registers — VERIFIED objdump, all three arches agree:
    write32(regs+0x240, 0x00000000)        // zero_val
    write32(regs+0x244, 0xFFFFFFFF)        // zero_mask
    write32(regs+0x248, 0x7F800000)        // pos_inf_val (+inf)        [mariana orders 248 last; values same set]
    write32(regs+0x24c, 0xFFFFFFFF)        // pos_inf_mask
    write32(regs+0x250, 0xFF800000)        // neg_inf_val (-inf)
    write32(regs+0x254, 0xFFFFFFFF)        // neg_inf_mask
    write32(regs+0x258, 0x7FC00000)        // nan_val (qNaN)
    write32(regs+0x25c, 0x7FC00000)        // nan_mask
    rc = act_instr_debug_level_set(cfg->mem, regs, cfg->inst_debug_level)   // +0xB8; maps via
    if rc != 0: return rc                  //   aws_hal_arch_stpb_to_instr_debug_level (EINVAL/-22 on bad)
    // engine instruction-config pack (RMW) — ACT uses +0xA04; PE/POOL use +0xA00:
    rmw32(regs+0xA04, fields_from(cfg[25], cfg->impl_nq_index<<4, cfg[50]<<8))   // NQ-index packing
    write32(xt_local_reg(cfg->mem, ACT=1, 1) + 0x1260, cfg->iram_cache_block_size)  // +0xC8
    return 0
```

> **NOTE —** the `regs+0x240..0x25c` value *set* is identical across SUNDA/CAYMAN/MARIANA (`0 / -1 / +inf / -1 / -inf / -1 / qNaN / qNaN`), but the *write order* differs slightly between arch templates (MARIANA emits `+0x248`/`+0x24c` after the inf pair). The eight target offsets and the eight 32-bit values are HIGH; treat the order as cosmetic — these are independent CSR writes with no read-after-write between them.

### Function Map — the engine × arch leaf matrix

The twelve `*_init` orchestrators, each pinned by `nm`, with the ordered sub-steps each drives. `regs_init`/`ucode_seq_init`/`hw_decode_table_init` addresses are the per-step leaves; the **HW-dec** column marks whether the orchestrator calls HW-decode (`yes`) or the leaf is a `ret 0` stub / call-skipped (`stub`).

| Engine | Arch | `*_init` orchestrator | `regs_init` | `ucode_seq_init` | `hw_decode_table_init` | HW-dec | Conf |
|---|---|---|---|---|---|---|---|
| PE (0) | SUNDA | `0x46e400` | `0x46e140` | `0x46e210` | `0x46e3f0` | **stub** | CERTAIN |
| PE (0) | CAYMAN | `0x473150` | `0x472d80` | `0x472e20` | `0x473000` | yes | CERTAIN |
| PE (0) | MARIANA | `0x467680` | `0x467290` | `0x467350` | `0x467530` | yes | CERTAIN |
| ACT (1) | SUNDA | `0x46d9b0` | `0x46d480` | `0x46d860` | `0x46d9a0` | **stub** | CERTAIN |
| ACT (1) | CAYMAN | `0x4724e0` | `0x471f90` | `0x472240` | `0x472380` | yes | CERTAIN |
| ACT (1) | MARIANA | `0x4669f0` | `0x4664a0` | `0x466750` | `0x466890` | yes | CERTAIN |
| POOL (2) | SUNDA | `0x46e8d0` | `0x46e5d0` | `0x46e6a0` | `0x46e8c0` | **stub** | CERTAIN |
| POOL (2) | CAYMAN | `0x4737b0` | `0x473370` | `0x473430` | `0x473650` | yes | CERTAIN |
| POOL (2) | MARIANA | `0x467d30` | `0x4678f0` | `0x4679b0` | `0x467bd0` | yes | CERTAIN |
| DVE (3) | SUNDA | `0x46df80` | `0x46db00` | `0x46ddf0` | `0x46dde0` | **stub** | CERTAIN |
| DVE (3) | CAYMAN | `0x472bf0` | `0x472650` | `0x472a60` | `0x472900` | yes | CERTAIN |
| DVE (3) | MARIANA | `0x467100` | `0x466b60` | `0x466f70` | `0x466e10` | yes | CERTAIN |

> **GOTCHA —** "HW-dec = stub" on SUNDA is not a placeholder you can fill later — it is the arch's contract. All four `aws_hal_stpb_sunda_*_hw_decode_table_init` leaves are `xor eax,eax; ret` (verified bytes), and SUNDA's `act_init`/`pe_init` orchestrators **do not even call** the step (the call list jumps `regs_init → ucode_seq_init → dma_init → release_run_stall`). SUNDA's `dve_init`/`pooling_init` *do* call their stubs, which harmlessly return 0. A reimplementer who wires a real CAM/PROFILE programmer into the SUNDA HW-decode slot is programming a CSR block the v2 silicon does not have. The HW-decode CAM/PROFILE machinery is a CAYMAN-and-later capability.

### Considerations

The descriptor every leaf consumes is the 208-byte `aws_hal_stpb_eng_info`, one slice of the 1104-byte `aws_hal_stpb` context. The leaf-relevant fields, with the byte offsets the builders load (all HIGH — named DWARF structs, sizes self-consistent with the loads):

| Field | Offset | Type | Consumer |
|---|---|---|---|
| `tpb_mem_handle` | `+0x00` (ctx) | `volatile u8*` | `al_mem_write_buf` dest base; `mem` arg to xt-local accessors |
| `tpb_regs` | `+0x08` (ctx) | `volatile u8*` | CSR base for `al_reg_{read,write}32` / RMW |
| `seq_ucode {iram,dram}` | `eng+0x00` | 2× `{buf,size}` (16 B) | `ucode_seq_init` bounds-check + `write_padded` |
| `hw_decode {profile,cam}` | `eng+0x40` | 2× `{buf,size}` | `hw_decode_table_init` (CAYMAN/MARIANA only) |
| `dma_info` | `eng+0x60` (88 B) | `{enabled; rx@+8; tx@+48}` | `dma_init` ring-triple program |
| `inst_debug_level` | `eng+0xB8` | `u32` | `instr_debug_level_set` (mapped via `stpb_to_instr_debug_level`) |
| `impl_nq_index` | `eng+0xC0` | `u32` | `regs_init` `+0xA04`/`+0xA00` NQ pack |
| `disable_hw_decode` | `eng+0xC4` | `bool` | `hw_decode_table_init` arg6 (disable path) |
| `iram_cache_block_size` | `eng+0xC8` | `u32` | `regs_init` `xt_local+0x1260` write |

> **QUIRK —** the `_init` shims' `inst_debug_level` bound-check (`≤ 8`, [hal-tpb-shims §3](hal-tpb-shims.md)) reads arg `+0xC0` (DVE/ACT) or `+0xE0` (Pooling), but the `eng_info` struct's `+0xC0` is `impl_nq_index`, not a debug level. The `_init` argument is therefore a *wider* engine-create-params struct that overlays `inst_debug_level` at `+0xC0`/`+0xE0`, not the bare 208-byte `eng_info`. The leaves on *this* page read `inst_debug_level` from `eng+0xB8` (the real `eng_info` field); the shim reads it from the wider wrapper. Both are byte-firm; the wider wrapper type is not re-derived here (MED).

---

## 2. The DVE Sequencer RNG Block and Per-Arch Knob Presence

### Purpose

The DVE (vector engine, index 3) carries a sequencer register block the other engines do not — the stochastic-rounding RNG state machine: an LFSR seed, an xorwow increment, two stochastic-rounding controls, and (MARIANA only) a "force response-comb eight-deep" knob and a "pool-arbiter chicken bit." `dve_regs_init` programs these on top of the shared `instr_debug_level` + instr-cfg path. The block is a single contiguous CSR window at `regs+0x300..0x3f0`, and which knobs in it are *live* is a per-arch property — the cleanest capability axis in the whole STPB layer.

### Algorithm

`dve_regs_init` (`mariana @0x466b60`, the richest variant) and the leaf accessors it calls:

```c
// aws_hal_stpb_mariana_dve_regs_init(cfg)        // @0x466b60  (cayman @0x472650 / sunda @0x46db00)
function dve_regs_init(cfg):
    regs = cfg->tpb_regs
    dve_instr_debug_level_set(cfg->mem, regs, cfg->inst_debug_level)    // EINVAL/-22 on bad level
    rmw32(regs+0xA04, pack_dve(cfg->impl_nq_index, cfg[50]))            // DVE NQ-index field packing
    dve_sequencer_force_pool_arb_chicken_bit(regs)                      // MARIANA: RMW +0x3f0 |= 2
                                                                        // SUNDA/CAYMAN: not called here
    write32(xt_local_reg(cfg->mem, DVE=3, 1) + 0x1260, cfg->iram_cache_block_size)
    dve_sequencer_write_xorwow_inc_val_default(regs)                   // write32(+0x324, 0x800587C5)
    return 0

// DVE sequencer leaf CSR accessors (block base = tpb_regs); offsets VERIFIED objdump, all arches:
//   reset_rng_seed                     write32(regs+0x314, 0xCD9E8D57)   // fixed LFSR reset seed
//   write_lfsr(v)                      write32(regs+0x314, v)           // same CSR, arbitrary seed
//   sequencer_update_instr_dbg_ctrl    rmw32 (regs+0x304) low nibble = lvl & 0xF
//   read_timestamp_inc_val             return read32(regs+0x308)
//   write_stochastic_rnd(v)            write32(regs+0x30c, v)
//   write_stochastic_rnd_mode(v)       write32(regs+0x310, v)
//   force_pool_arb_chicken_bit         rmw32 (regs+0x3f0) |= 2          // MARIANA-active
//   set_force_rspcomb_eight_deep(v)    rmw32 (regs+0x3c0) bit0 = v & 1  // MARIANA ONLY (else stub)
//   get_offset_force_rspcomb_eight_deep return 960 (=0x3c0)             // MARIANA; else returns 0
```

### Function Map — the per-arch knob-presence axis

The two DVE knobs that exist on one arch and are stubbed on the others. This is the table a reimplementer needs to avoid building dead CSR writes for SUNDA/CAYMAN or omitting them for MARIANA:

| Knob (DVE sequencer) | SUNDA (2) | CAYMAN (3) | MARIANA (4) | CSR | Conf |
|---|---|---|---|---|---|
| `set_force_rspcomb_eight_deep` | `0x46ceb0` **stub** (`repz ret`) | `0x471dd0` **stub** (`repz ret`) | `0x4662a0` **live** — RMW `regs+0x3c0` bit0 | `+0x3c0` | CERTAIN |
| `get_offset_force_rspcomb_eight_deep` | `0x46cec0` → `0` | `0x471de0` → `0` | `0x4662d0` → `960` (`0x3c0`) | — | CERTAIN |
| `force_pool_arb_chicken_bit` | present, not in `regs_init` | `0x471d90` (in `dve_init` path) | `0x466260` (in `regs_init`) | `+0x3f0` bit1 | HIGH |
| `reset_rng_seed` / `write_lfsr` | `0x46cd70` / `0x46ced0` | `0x471d30` / `0x471df0` | (band 0x466200..) | `+0x314` | CERTAIN |

> **QUIRK —** the `force_rspcomb_eight_deep` *offset getter* is the tell. On SUNDA/CAYMAN it returns `0` — the sentinel for "this CSR does not exist on this arch" — while MARIANA returns `960` (`0x3c0`), the real byte offset. A reimplementer building a generic "read the rspcomb offset, then RMW it" caller must treat `0` as *absent*, not as "offset zero": writing bit0 of `regs+0x000` on SUNDA would clobber an unrelated register. The setter being a `repz ret` no-op on SUNDA/CAYMAN is the matching half — the silicon has no such knob, so the write is correctly elided.

### Considerations

DVE also owns a parameter-RAM and a four-table model the other engines lack (`dve_write_parameter_ram`, `dve_write_tables`). The four DVE tables map to fixed `tpb_mem`-relative destinations (`get_dve_table_offsets_<arch>`): `0x2B82000`/`0x4000`, `0x2B80000`/`0x1000`, `0x2B81000`/`0x1000`, `0x2B86000`/`0x400`, sourced from the config block at `a2+24/+8/+16/+0` respectively, each gated by a non-NULL source pointer; the parameter RAM is `0x2B87000`, cap `0x400`. The `0x2B8xxxx` prefix aligns with the DVE engine sequencer base `0x802B00000` low word — the same engine aperture the ucode loader programs into `engine_base_address_lo/hi`. The exact role of each of the four tables (opcode / control-fast / control-slow / datapath, per the DWARF `dve_config_dma_state_t` member order) is MED — the offsets and sizes are HIGH, the name↔slot pairing is inferred.

---

## 3. The Pooling Engine and the Descriptor-Generation-Engine (DGE)

### Purpose

The Pooling engine (index 2, the Q7 vision/pooling vector core) is the most divergent of the four. Beyond the standard pipeline it carries: a **Q7 ucode-engine init** step (`aws_hal_q7_ucode_eng_init`, itself a trampoline through `kaena_khal+0x3a8` to the per-arch `q7_ucode_eng_init_<arch>` leaf), a **dual run-stall release** (the sequencer stall and the Q7 engine stall are *separate* CSRs), and the **Descriptor-Generation-Engine (DGE)** program — a hardware descriptor-prefetch engine the Pooling core uses to stream DMA descriptors, configured by two ops: `set_dge_carveout` (reserve a region of the engine's local memory for DGE descriptors) and `set_dge_dma_mapping` (bind the DGE to a DMA channel). The DGE is the Pooling-only feature the shim layer exposes as four slots (`+0x370..+0x388`, [hal-tpb-shims §3](hal-tpb-shims.md)); the bodies are here.

### Entry Point

```text
aws_hal_stpb_<arch>_pooling_init                       ── orchestrator (THIS PAGE)
  ├─ pooling_regs_init                                 ── +0xA00 pack + q7 notific sw-queue (8-nibble)
  ├─ pooling_ucode_seq_init                            ── "POOL seq iram/dram"
  ├─ aws_hal_q7_ucode_eng_init (0x451080)              ── trampoline → kaena_khal+0x3a8
  │     = aws_hal_q7_ucode_eng_init_<arch>             ──   sunda 0x46b9a0 / cayman 0x4714c0 / mariana 0x464b00
  ├─ [CAYMAN/MARIANA] pooling_hw_decode_table_init     ── SUNDA SKIPS this call
  ├─ pooling_dma_init
  ├─ pooling_release_seq_run_stall                     ── sequencer stall: nx_local release_run_stall(0)
  └─ pooling_release_eng_run_stall                     ── Q7 engine stall: q7_release_run_stall(0)
```

### Algorithm

The DGE carveout program — the one DGE op with real validation, decoded from `aws_hal_stpb_mariana_pooling_set_dge_carveout` (`@0x467e80`, bytes verified):

```c
// aws_hal_stpb_<arch>_pooling_set_dge_carveout(mem, size, offset)
//   mariana @0x467e80 / cayman @0x473900 / sunda @0x46ea00
function pooling_set_dge_carveout(mem, size, offset):
    base = aws_hal_arch_<arch>_get_xt_local_reg_offset(mem, POOL=2, inst=1)
    // PER-ARCH CAP — a literal cmp in each body (VERIFIED objdump):
    //   SUNDA 0x30000 (192 KiB) · CAYMAN 0x38000 (224 KiB) · MARIANA 0x40000 (256 KiB)
    if offset + size > DGE_CARVEOUT_CAP:
        al_hal_log(1, "dge carveout params oob, offset %u size %u", offset, size)   // @0x826f10
        return -EINVAL                                                              // 0xFFFFFFEA
    if (size & 0xF) != 0 or (offset & 0xF) != 0:        // both must be 16-byte aligned
        al_hal_log(1, "invalid dge carveout params, offset %u size %u", offset, size)  // @0x826f40
        return -EINVAL
    // pack: high half = size>>4, low half = offset>>4 (16-byte granules)
    packed = ((size >> 4) << 16) | (offset >> 4)
    aws_hal_arch_<arch>_write_tpb_nx_local_reg_sw_dge_carveout(base, packed)
    return 0

// aws_hal_stpb_<arch>_pooling_set_dge_dma_mapping(mem, hi, lo)
//   mariana @0x467e00 / cayman @0x473880 / sunda @0x46e980   — NO VALIDATION (verified)
function pooling_set_dge_dma_mapping(mem, hi, lo):
    base = aws_hal_arch_<arch>_get_xt_local_reg_offset(mem, POOL=2, inst=1)
    packed = (hi << 16) | lo                            // raw pack, no bounds/alignment check
    return aws_hal_arch_<arch>_write_tpb_nx_local_reg_sw_dge_dma_mapping(base, packed)
```

The `_get_dge_*_register_offset_and_val` variants (e.g. cayman `get_dge_carveout_register_offset_and_val @0x473990`) compute the same `packed` value and the register offset but *return both as out-params* instead of writing — used by the descriptor-builder path that batches register programming. The cayman getter's cap is `0x38000`, confirming the same per-arch monotone holds for the offset-and-val form.

### Function Map — the Pooling DGE and Q7 leaves

| Op | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Notes | Conf |
|---|---|---|---|---|---|
| `pooling_set_dge_carveout` | `0x46ea00` | `0x473900` | `0x467e80` | cap `0x30000`/`0x38000`/`0x40000`; 16-B align | CERTAIN |
| `pooling_set_dge_dma_mapping` | `0x46e980` | `0x473880` | `0x467e00` | pack `(hi<<16)\|lo`, no validation | CERTAIN |
| `pooling_get_dge_carveout_reg_off_and_val` | `0x46ea90` | `0x473990` | `0x467f10` | out-param form of carveout | CERTAIN |
| `pooling_get_dge_dma_mapping_reg_off_and_val` | `0x46e9b0` | `0x4738b0` | `0x467e30` | out-param form of dma-mapping | CERTAIN |
| `pooling_release_seq_run_stall` | `0x46e7e0` | `0x473570` | `0x467af0` | sequencer stall (`release_run_stall`) | CERTAIN |
| `pooling_release_eng_run_stall` | `0x46e810` | `0x4735a0` | `0x467b20` | Q7 engine stall (`q7_release_run_stall`) | CERTAIN |
| `q7_ucode_eng_init_<arch>` | `0x46b9a0` | `0x4714c0` | `0x464b00` | via `q7_ucode_eng_init @0x451080` trampoline (slot `+0x3a8`) | CERTAIN |
| `pooling_regs_init` q7-notific helper | — *(none)* | `aws_reg_write_..._cayman @0x473300` | `aws_reg_write_..._mariana @0x467880` | 8-nibble broadcast of 4-bit arg into `regs+0xA14` | CERTAIN |

> **GOTCHA —** Pooling releases **two** stalls and the order matters: `pooling_init` calls `release_seq_run_stall` (sequencer) *then* `release_eng_run_stall` (Q7 engine) — the Q7 engine stall is a distinct `xt_local` register (`write_tpb_xt_local_reg_q7_release_run_stall`, per-arch at sunda `0x4796e0` / cayman `0x47b290` / mariana `0x477ba0`), not the sequencer's `release_run_stall`. The other three engines release a single stall. A reimplementer who collapses Pooling to one stall release leaves the Q7 vector engine gated and the pooling core never runs, with no error — `release_run_stall(0)` returns void.

### Considerations

There is **no `aws_reg_write_tpb_notific_sw_queue_pool_q7_nt_sunda`** — the 8-nibble Q7 notific SW-queue broadcast helper exists only on CAYMAN (`0x473300`) and MARIANA (`0x467880`). SUNDA's `pooling_regs_init` (`@0x46e5d0`) packs its instr-cfg and writes `xt_local+0x1260` but does not broadcast a Q7 notific SW-queue value, consistent with SUNDA being the generation without the HW-decode / Q7-notific surface. The `q7_ucode_eng_init` trampoline (`@0x451080`) is itself worth noting: it asserts `al_hal_tpb_get_arch_type() != 0` and tail-jumps `kaena_khal+0x3a8` — the same dispatch discipline as the STPB shims, applied to a Q7-specific sub-table (`khal_q7`), so the Pooling orchestrator reaches the arch-correct Q7 ucode init without naming a generation.

---

## 4. The Three Per-Arch Divergences a Reimplementer Must Honor

### Purpose

The leaves are 95% arch-templated boilerplate — the same body with a different `_<arch>_` infix and a different register-offset getter. The 5% that genuinely differs is the part that bites. This section collects the three byte-anchored divergences so a reimplementer can build the common template once and then special-case exactly these three points, rather than auditing all twelve orchestrators by hand.

### Algorithm

```text
DIVERGENCE 1 — HW-decode capability (arch ≥ CAYMAN)
  SUNDA:   all four *_hw_decode_table_init = xor eax,eax; ret  (no CAM/PROFILE silicon)
           act_init/pe_init SKIP the call; dve_init/pooling_init call the stub (return 0)
  CAYMAN:  real CAM+PROFILE programmer (write_padded both, size-checked vs get_eng_hw_decode_table_params)
  MARIANA: real (same shape as CAYMAN)
  → reimplementer: gate HW-decode on arch ≥ 3; do not program SUNDA's absent CSRs.

DIVERGENCE 2 — DVE force_rspcomb_eight_deep (MARIANA only)
  SUNDA/CAYMAN: setter = repz ret (no-op);   offset-getter returns 0  (absent sentinel)
  MARIANA:      setter = RMW regs+0x3c0 bit0; offset-getter returns 960 (0x3c0)
  → reimplementer: treat offset 0 as "knob absent", not "offset zero"; elide the write on arch < 4.

DIVERGENCE 3 — Pooling DGE carveout cap (monotone by generation)
  SUNDA   0x30000 (192 KiB)   ── literal cmp in pooling_set_dge_carveout @0x46ea00
  CAYMAN  0x38000 (224 KiB)   ── @0x473900 / @0x473990
  MARIANA 0x40000 (256 KiB)   ── @0x467e80
  → reimplementer: the cap is a per-arch constant, not a runtime-probed value; bake the right literal.
```

### Considerations

> **NOTE —** all three divergences are invisible from the dispatch layer. The [hal-tpb-shims](hal-tpb-shims.md) trampolines load one slot and jump; they cannot tell whether the leaf they land in is a real programmer or a `ret 0` stub, nor whether it caps carveout at 192 or 256 KiB. The shim's NULL-slot policy (DVE asserts, PE/Pool/SP no-op) is a *different* axis — it guards against a slot the registrar never filled, whereas these three divergences are about leaves that *are* filled but behave per-generation. A reimplementer reproducing the shim layer faithfully still has all three of these to encode in the backends. That separation — dispatch policy in the shim, capability policy in the leaf — is the layering this page and [hal-tpb-shims](hal-tpb-shims.md) split between them.

> **QUIRK —** the DGE carveout monotone (`0x30000 → 0x38000 → 0x40000`) tracks the growth of the Pooling engine's local descriptor memory across silicon generations, but the runtime never reads the cap from a register — each `pooling_set_dge_carveout` body carries its arch's cap as an immediate `cmp`. This means the cap is a *compile-time* property of the KaenaHal build, not a device-probed one: a reimplementer cannot derive it from BAR space and must instead pin it to the arch enum exactly as the leaves do.

## Related Components

| Name | Relationship |
|---|---|
| `kaena_khal_register_funcs_v2/v3/v4` (`0x468740` / `0x46ed70` / `0x4622e0`) | install these leaf addresses into the `kaena_khal` slots; arch 2→v2 (sunda), 3→v3 (cayman), 4→v4 (mariana) |
| `aws_hal_arch_<arch>_get_xt_local_reg_offset` (sunda `0x479070` / cayman `0x47afe0` / mariana `0x4778f0`) | the per-arch NX-local register-window base getter every leaf calls with `(mem, engine, inst)` |
| `aws_hal_get_seq_params_<arch>` / `get_eng_hw_decode_table_params_<arch>` / `get_act_table_params_<arch>` | the per-arch capacity/offset providers the `ucode_seq_init`/`hw_decode_table_init`/`load_local` leaves bounds-check against |
| `write_padded` (`0x473eb0`) | shared `al_zalloc → memcpy → al_mem_write_buf → al_free` blob stager used by every `ucode_seq_init`/`hw_decode_table_init` |
| `aws_hal_q7_ucode_eng_init` (`0x451080`) | the Pooling-only Q7 ucode trampoline (`kaena_khal+0x3a8`) the pooling orchestrators call between ucode-seq and HW-decode |

## Cross-References

- [KaenaHal: TPB/STPB Arch-Dispatch Shims](hal-tpb-shims.md) — the trampolines that tail-call every leaf on this page; owns the `kaena_khal` slot map, the canonical dispatch idiom, and the DVE-assert / PE-Pool-no-op NULL-slot policies
- [KaenaHal: Overview and Platform-Services Adapter](hal-adapter.md) — `al_hal_tpb_get/set_arch_type`, the arch latch (`hal_target @0xCAEB60`) that selects which registrar fills the slots these leaves occupy
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — the parallel userspace arch vtable one layer up; the `aws_get_*` register-offset shims (a different dispatch object) the orchestrators do *not* use
- [Per-Arch Device Layer: Geometry and Address Map](arch-geometry.md) — the per-arch engine base addresses (PE `0x802600000`, ACT `0x802400000`, POOL `0x803000000`, DVE `0x802B00000`), SBUF/IRAM/DRAM apertures, and the Trn3-mesh `+0x800000000` high bit the ucode loaders subtract
- [TopSP Notification Path](../kernel/topsp.md) — the kernel-side TopSP/Q7 notification engine the Pooling `q7` notific SW-queue helper and the DGE notification path program from userspace
- [back to index](../index.md)
