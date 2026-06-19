# Runtime End-to-End Call-Graph Spine

This page is the **symbol-level backbone** of the host runtime lane: the explicit
named `caller@addr → callee@addr` edge graph that runs from the public `nrt_*` /
`nrtucode_*` API entry, through **load → prelink → relocate → core-create →
opset-build**, into **execute → dispatch → device kickoff**, and out to the
**DMA-ring handoff**. The narrative pages reference these edges by name; this page
fixes each edge to a confirmed `call` site so a reimplementer can reconstruct the
driver's call backbone byte-for-byte.

It is deliberately an **edge graph, not prose**. Every node is named with its real
recovered symbol and `.text` address; every edge carries **what crosses it** (which
artifact, handle, or opcode). The semantics of each node belong to its narrative
sibling — this page only ties them together and cites where the bytes prove the edge.

- **Narrative / per-node semantics** → [runtime/runtime-synthesis.md](runtime-synthesis.md)
- **Per-export signatures + addresses** → [runtime/public-api-table.md](public-api-table.md)
- **Patch-table format consumed by `relocate_op`** → [runtime/ucode-relocation-consumer.md](ucode-relocation-consumer.md)
- **Descriptor handoff (the tail of the kickoff branch)** → page `runtime/host-device-descriptor-handoff.md` (forward reference; stub at time of writing)
- **Worked custom-op end-to-end trace** → [../orientation/customop-end-to-end.md](../orientation/customop-end-to-end.md)

---

## Method, scope, and tag legend

The spine is driven from the per-object **callgraph sidecar** (the who-calls-whom
edge list), and **every critical edge is then re-confirmed** by a bounded
`objdump -d --start-address=… --stop-address=…` at the actual call site in the
shipped binary. Edges marked OBSERVED below have the call-site bytes shown or were
byte-verified this pass.

Primary artifact (host runtime): **`libnrt.so.2.31.24.0`** — x86-64 ELF,
`122,956,336` bytes, `BuildID[sha1]=8bb57aba…`, git `0b044f4ce`, **not stripped**
(`.symtab` + `.debug_info`, ~17,372 functions, 121 `nrt_*` exports under
`NRT_2.0.0`). In `.text`/`.rodata` the virtual address equals the file offset, so a
`call <imm32>` target reads directly as the callee address.

Secondary artifact (PI-library link/relocate, §3): **`libnrtucode_internal.so`** —
its on-disk symtab is stripped; names below are IDA-recovered and the callgraph.json
edges are the authoritative edge source for that object.

### Confidence × provenance tags

| Tag | Meaning |
|---|---|
| **HIGH** | The caller demonstrably reaches the callee (direct `call <imm>` to the symbol). |
| **MED** | Reached through PLT/GOT, a string-resolved `dlopen`, or one inferred thread/dlopen hop. |
| **LOW** | Edge reasoned only across two other edges; no single trace. |
| **OBSERVED** | Edge read from a shipped artifact this pass — call-site `objdump` of `libnrt.so` (VA==file-offset `.text`) or the IDA callgraph.json of `libnrtucode_internal.so`. |
| **INFERRED** | Edge reasoned across two OBSERVED edges, or across a `dlopen` / function-pointer boundary; not single-traced. |
| **CARRIED** | Edge taken from a sibling report whose grounding this page re-confirms (none remain un-reconfirmed here). |

> **NOTE — the spine is the union of two object models.** Edges in §1–§2 and §4–§6
> live in **L-HOST `libnrt.so`** and are confirmed by direct x86-64 `call`
> disassembly. Edges in **§3 (prelink/relocate)** live in **L-INT
> `libnrtucode_internal.so`** (IDA names). The host→ucode hop is **by string, not by
> ELF symbol link** — `ucode_init_module` resolves `"libnrtucode_extisa.so"` via
> `dlopen`/`dlsym`, so that one edge is **MED·INFERRED** (§1).

> **GOTCHA — `capi.so` is NOT a runtime entry.** `engines-1.1/capi.so` exports only
> `bind_engine` / `v_check` / `_init` / `_fini` (an OpenSSL-style `ENGINE` bind
> vtable) and imports no `nrt`/`nrtucode` symbols. The real public entry to the spine
> is `nrt_*` (L-HOST) and `nrtucode_*` (the ucode build path). Do not mistake the
> engine shim for a device entry point.

---

## §1 — INIT spine (device bring-up + ucode bootstrap)

Entry: **`nrt_init@0x94e90`** (`@@NRT_2.0.0`, size `0x1967`). The init fan-out is
wide; only the edges that matter to the spine are listed.

```c
// nrt_init @0x94e90  — front of the chain
nrt_init {
    nrt_config_parse_init_config @0x8a1d0;     // config blob
    nrt_get_dev_info             @0x83ad0;     // device probe
    tdrv_get_platform_type       @0x269430;    // driver/arch select
    al_hal_tpb_get_arch_type     @0x44bca0;    // arch dispatch key (sunda/mariana/cayman)
    hw_decode_init_tables        @0x225150;    // HW-decode (hwdce) tables
    kmgr_init                    @0xde080;     // *** kernel-mgr bring-up ***
    kmgr_register_framework      @0xddff0;
    ucode_init_module            @0x225940;    // *** GPSIMD ucode bootstrap ***
}

// ucode_init_module @0x225940  — resolves the ucode lib BY NAME
ucode_init_module {
    ucode_shared_library_name @0x225930;       // -> "libnrtucode_extisa.so"
    dlopen@plt @0x3d3c0;                        // string-resolved load (NOT symbol link)
    dlsym@plt  @0x3d950;                        // bind nrtucode_get_* handshake fns
    al_hal_tpb_get_arch_type @0x44bca0;
}
// => cross-object into L-UCODE nrtucode_get_api_level / _build_version / _git_version
//    is reached by name through the dlsym'd handles.            [MED INFERRED]
```

| Edge | Tag | Confirmation |
|---|---|---|
| `nrt_init@0x94e90 → ucode_init_module@0x225940` | HIGH·OBSERVED | `95da4: e8 97 fb 18 00  call 225940` |
| `nrt_init@0x94e90 → kmgr_init@0xde080` | HIGH·OBSERVED | `95e75: e8 06 82 04 00  call de080` |
| `ucode_init_module@0x225940 → ucode_shared_library_name@0x225930` | HIGH·OBSERVED | `225caa: e8 81 fc ff ff  call 225930` |
| `ucode_init_module@0x225940 → dlopen@plt` / `dlsym@plt` | HIGH·OBSERVED | `2259a8: call 3d3c0`; `2259e6: call 3d950` |
| `ucode_init_module → "libnrtucode_extisa.so"` (dlopen string) | MED·INFERRED | string-resolved, no ELF symbol edge |

The per-arch POOL/GPSIMD engine register bring-up (`aws_hal_stpb_init@0x458350 →
aws_hal_stpb_*_pooling_init → *_pooling_{regs,dma,hw_decode_table,ucode_seq}_init →
*_pooling_release_{seq,eng}_run_stall`) hangs off device attach with the same edge
shape per arch; the register semantics belong to the HW pages, not this spine.

---

## §2 — LOAD spine (NEFF → kelf stage → silent ucode install)

Entry: **`nrt_load@0xa9fe0`** (`@@NRT_2.0.0`). The thin wrapper hands off to the real
body `nrt_load_util@0xa9920`, which drives `kmgr_load_nn_nc` — the load body.

```c
// what crosses each edge is named in the comment
nrt_load @0xa9fe0 {
    nrt_load_util @0xa9920;                     // the real body (neff*, size, vnc, **model)
}
nrt_load_util @0xa9920 {
    neff_get_header_from_buffer  @0x4ca2c0;     // parse NEFF header
    nrt_config_parse_model_config@0x85f50;
    dlr_db_get_nn_cached_interned_model_id @0xdc150;  // dedup cache (H_NN)
    kmgr_load_nn_nc @0xde280;                   // *** the load body ***  (kelf_nn**)
    nrt_save_neff @0x99be0; nrt_core_dump @0x92b90;   // diagnostics / error unwind
}
kmgr_load_nn_nc @0xde280 {
    dlr_kelf_load  @0xe0830;                    // neff -> kelf_nn*
    dlr_kelf_stage @0xe0970;                    // *** stage kelf onto virtual_core ***
    dlr_model::create_nds_model_info @0xddd60;
    dlr_model::save_replica_group_info @0xdbc70;
    kmgr_unstage_kelf_model @0xdc180;           // unwind
}
dlr_kelf_stage @0xe0970 {
    dlr_kelf_stage_model_add @0xe0730;          // single-TPB path  (kbin*, virtual_core*)
    dlr_kelf_stage_multi_tpb_model_add @0xe0580;// multi-TPB path
}
dlr_kelf_stage_model_add @0xe0730 {            // threaded shim @0xe07f0 -> here
    kbl_model_add @0x3058e0;                     // *** kernel-binary add ***
}
kbl_model_add @0x3058e0 {
    al_hal_tpb_get_arch_type @0x44bca0;
    dma_queue_bundle_instance_lut_init @0x22ec00;   // DMA queue LUT
    dma_ring_setup_queue_bundles @0x22e630;         // DMA ring bundles
    sequencer_setup_instr @0x4483d0;                // instr sequencer
    kbl_get_fmap_set @0x446960;                      // feature-map set
    ucode_stage_libs @0x310ea0;                      // *** silent Pool-Q7 ucode install ***
}
ucode_stage_libs @0x310ea0 {                   // mutex-guarded
    db_physical_core_get_mla_and_tpb @0x2272a0;
    ucode_alloc_new_staged_libs @0x310660;          // alloc staged-lib slot
    ucode_stage_libs_impl @0x310c00;
}
ucode_stage_libs_impl @0x310c00 {
    dmem_buf_copyin @0x229820;                       // *** ucode image -> device DMEM ***
    calloc; strncpy;
}
```

| Edge (caller@addr → callee@addr) | Tag | Confirmation (`objdump` call site) |
|---|---|---|
| `nrt_load@0xa9fe0 → nrt_load_util@0xa9920` | HIGH·OBSERVED | `aa0a2: e8 79 f8 ff ff  call a9920` |
| `nrt_load_util@0xa9920 → kmgr_load_nn_nc@0xde280` | HIGH·OBSERVED | `a9b62: e8 19 47 03 00  call de280` |
| `kmgr_load_nn_nc@0xde280 → dlr_kelf_stage@0xe0970` | HIGH·OBSERVED | `dedd0: e8 9b 1b 00 00  call e0970` |
| `dlr_kelf_stage@0xe0970 → dlr_kelf_stage_model_add@0xe0730` | HIGH·OBSERVED | `e0b0f: e8 1c fc ff ff  call e0730` |
| `dlr_kelf_stage_model_add@0xe0730 → kbl_model_add@0x3058e0` | HIGH·OBSERVED | `e0795: e8 46 51 22 00  call 3058e0` |
| `kbl_model_add@0x3058e0 → dma_queue_bundle_instance_lut_init@0x22ec00` | HIGH·OBSERVED | `305af8: e8 03 91 f2 ff  call 22ec00` |
| `kbl_model_add@0x3058e0 → sequencer_setup_instr@0x4483d0` | HIGH·OBSERVED | `305de4: e8 e7 25 14 00  call 4483d0` |
| `kbl_model_add@0x3058e0 → dma_ring_setup_queue_bundles@0x22e630` | HIGH·OBSERVED | `305e61: e8 ca 87 f2 ff  call 22e630` |
| `kbl_model_add@0x3058e0 → ucode_stage_libs@0x310ea0` | HIGH·OBSERVED | `3062d8: e8 c3 ab 00 00  call 310ea0` |
| `ucode_stage_libs@0x310ea0 → ucode_stage_libs_impl@0x310c00` | HIGH·OBSERVED | `310f03: call 310c00` (also `31102a:`) |
| `ucode_stage_libs_impl@0x310c00 → dmem_buf_copyin@0x229820` | HIGH·OBSERVED | `310c95: e8 86 8b f1 ff  call 229820` |

> **NOTE — `dmem_buf_copyin` is the "silent ucode install" terminus.** The Pool-Q7
> ucode image is copied host→DMEM here as a side effect of `kbl_model_add`; the
> caller never names a separate ucode-load API. The byte-level DMA mechanics belong
> to the descriptor-handoff page; this spine only fixes the edge.

---

## §3 — PRELINK / RELOCATE spine (the TIER-1 PI-library loader)

This subgraph lives entirely in **L-INT `libnrtucode_internal.so`** (IDA-recovered
names; addresses are the callgraph.json `from_addr`/`to_addr`). It is the
position-independent (PI) ucode-library link/relocate that builds the loadable image
whose load/unload instruction sequences the runtime then issues into the POOL stream.

```c
nrtucode_ll_create @0x9b1a90 {                  // "link-library create"
    nrtucode_get_ext_isa_internal @0x9b2b30;    // ext-ISA select
    nrtucode_objcount_increment   @0x9b17a0;    // lifetime / leak counter
    nrtucode_context_log          @0x9b0540;
    prelink                       @0x9b5d60;    // *** prelink driver ***
}
prelink @0x9b5d60 {
    prelink_load_lib     @0x9b5e70;             // load + validate segments
    prelink_relocate_lib @0x9b6160;             // apply relocations
    memset @0x9b7c90;                            // zero scratch
}
prelink_load_lib @0x9b5e70 {
    validate_dynamic_load @0x9b71f0;            // dyn-load sanity
    get_dyn_info  @0x9b6ed0;                     // read .dynamic
    xtlib_host_word @0x9b6e00;                   // host-endian word read
    find_align @0x9b6e50; align_ptr @0x9b6e40; log_error @0x9b60a0;
}
validate_dynamic_load @0x9b71f0 {
    xtlib_verify_magic @0x9b6d40;               // *** PI-lib section magic ***
    xtlib_host_word    @0x9b6e00;
}
prelink_relocate_lib @0x9b6160 {
    relocate_op @0x9b6660;                       // *** apply one relocation ***
    log_error   @0x9b60a0;
}
relocate_op @0x9b6660 {                          // consumes the patch-table entry
    memcpy;                                       // writes the patched word
    log_error;
}
```

| Edge (L-INT, callgraph.json) | Tag | What crosses it |
|---|---|---|
| `nrtucode_ll_create@0x9b1a90 → prelink@0x9b5d60` | HIGH·OBSERVED | the opset's PI-library image, ready to link |
| `prelink@0x9b5d60 → prelink_load_lib@0x9b5e70` | HIGH·OBSERVED | raw library segment + `.dynamic` |
| `prelink@0x9b5d60 → prelink_relocate_lib@0x9b6160` | HIGH·OBSERVED | validated image awaiting relocation |
| `prelink_load_lib@0x9b5e70 → validate_dynamic_load@0x9b71f0` | HIGH·OBSERVED | candidate dyn-load header |
| `validate_dynamic_load@0x9b71f0 → xtlib_verify_magic@0x9b6d40` | HIGH·OBSERVED | the PI-library section magic word |
| `prelink_relocate_lib@0x9b6160 → relocate_op@0x9b6660` | HIGH·OBSERVED | one patch-table entry (per the relocation-consumer page) |
| `relocate_op@0x9b6660 → memcpy` | HIGH·OBSERVED | the patched word written into the image |

The heavier full-segment loader `xtlib_host_load_pi_library@0x9b7650` is a sibling
path (it adds `xtlib_load_seg@0x9b6d90` / `xtlib_load_pi_scratch@0x9b7830`) but gates
on the same `validate_dynamic_load → xtlib_verify_magic` pair. `relocate_op` is the
node whose **input** the relocation-consumer page describes; `xtlib_verify_magic` is
the magic-check node that page pins to. This spine only states they are reached.

---

## §4 — OPSET / LL BUILD + the LOAD_POOL_ARGUMENT (0x107A) handoff

The opset is built opcode-by-opcode, an `ll` (link-library) is created from it
(crossing into §3 `prelink`), then the runtime asks for the load/unload instruction
sequence and pushes it into the POOL instruction stream as
`LOAD_POOL_ARGUMENT 0x107A`.

```c
nrtucode_opset_create        @0x9b24c0 (L-INT);     // -> context_log / malloc / memset
nrtucode_opset_add_instruction;                      // builds the opcode set
nrtucode_ll_create           @0x9b1a90  ---> §3 prelink
nrtucode_ll_get_load_sequence @0x30a440 (L-UCODE);   // returns load-instr stream
nrtucode_ll_get_load_sequence_num_instrs @0x30a2c0;
// host side: that stream is emitted into POOL as opcode 0x7A:
add_load_pool_arguments @0x276780 { add_ins @0x322480; }   // emits header word 0x107A
```

The single OBSERVED caller of `add_load_pool_arguments` is the embedding-update
pseudo-op translator:

| Edge | Tag | Confirmation |
|---|---|---|
| `translate_one_pseudo_embedding_update_instr_v2@0x2770f0 → add_load_pool_arguments@0x276780` | HIGH·OBSERVED | callgraph: sole caller |
| `add_load_pool_arguments@0x276780 → add_ins@0x322480` | HIGH·OBSERVED | `2767d1: e8 aa bc 0a 00  call 322480` |
| `add_load_pool_arguments` emits header word `0x107A` | HIGH·OBSERVED | `276788: 41 ba 7a 10 00 00  mov $0x107a,%r10d` |

> **NOTE — `0x107A` decodes as opcode `0x7A` (`LOAD_POOL_ARGUMENT`) with the high
> nibble carrying the instruction class.** The `mov $0x107a,%r10d` at `0x276788` is
> the header word the runtime stamps into the POOL micro-program; the
> runtime-synthesis page expands the full per-op micro-program around it.

> **NOTE — L-UCODE bodies reach internal helpers as `@@Base+off` thunks** (e.g.
> `nrtucode_ll_create` reaching `nrtucode_get_hwdecode_table+0x50`). Those are
> in-`.text` trampolines of a differently-built object and are **not name-stable
> cross-object edges**, so L-INT's callgraph (the `nrtucode_ll_create → prelink` edge
> of §3) is the authority for this build path.

---

## §5 — EXECUTE / DISPATCH spine (`nrt_execute → kmgr → tpb_xu`)

Entry: **`nrt_execute@0x91de0`** (`@@NRT_2.0.0`, size `0x326`). It calls the unified
body `nrt_execute_repeat` **through the PLT** (see GOTCHA), which drives `kmgr_exec`,
which forks into the SYNC and ASYNC paths.

```c
nrt_execute @0x91de0 {
    nrt_execute_repeat @0x91650;                // *** unified body *** (via PLT thunk @0x3c890)
    // + profiling fan-out: nrt_profile_start, nrt_inspect_device_profile_start/stop,
    //   nrt_sys_trace_new_event
}
nrt_execute_repeat @0x91650 {
    kmgr_get_ifmap_count @0xdd650;
    kmgr_exec @0xdfd50;                          // *** dispatch body ***
}
kmgr_exec @0xdfd50 {
    kmgr_exec_pre @0xdf820;                      // resource prep (ifmaps, cc-resources)
    kbl_tensor_set_copy @0x308200;               // ifmap copy-in
    kmgr_sync_exec @0xdca70;                     // *** SYNC path ***
    kmgr_async_exec_add_work @0xe6d20;           // *** ASYNC path (§7) ***
    kmgr_async_exec_poll @0xe6ab0;
    kmgr_exec_resources_free @0xdd350;
}
kmgr_exec_pre @0xdf820 {
    kbl_init_compute_resources @0x306650;
    kbl_compute_build_compute_resources @0x306790;
    kbl_exec_build_and_load_cc_resources @0x306e30;
    kbl_create_reference_feature_map_set @0x308420;
    kbl_acquire_hw_exec_queue_lock @0x308d90;  kbl_release_hw_exec_queue_lock @0x308e00;
}
kmgr_sync_exec @0xdca70 {
    tdrv_get_platform_type @0x269430;
    tpb_xu_get_by_vcore @0xe7b10;               // resolve exec unit
    tpb_xu_schedule_exec @0xe8040;              // *** schedule ***
    tpb_xu_sync_exec_get_pooled_comp_efd @0xe87e0;  // completion eventfd
    tpb_xu_get_last_completed @0xe8410;  tpb_xu_release_pooled_eventfd @0xe87f0;
}
tpb_xu_schedule_exec @0xe8040 {
    tpb_xu_schedule_request @0xe7540;           // *** request *** (-> §6 fork)
    nrt_sys_trace_new_event_with_seq @0x509cc0;
}
```

| Edge (caller@addr → callee@addr) | Tag | Confirmation |
|---|---|---|
| `nrt_execute@0x91de0 → nrt_execute_repeat@0x91650` (via PLT `0x3c890`) | HIGH·OBSERVED | `91f56: call 3c890 <nrt_execute_repeat@plt>` → GOT → body `0x91650` (`@@NRT_2.0.0`) |
| `nrt_execute_repeat@0x91650 → kmgr_exec@0xdfd50` | HIGH·OBSERVED | `91963: e8 e8 e3 04 00  call dfd50` |
| `kmgr_exec@0xdfd50 → kmgr_exec_pre@0xdf820` | HIGH·OBSERVED | `dfde4: e8 37 fa ff ff  call df820` |
| `kmgr_exec@0xdfd50 → kmgr_sync_exec@0xdca70` | HIGH·OBSERVED | `e0011: e8 5a ca ff ff  call dca70` |
| `kmgr_exec@0xdfd50 → kmgr_async_exec_add_work@0xe6d20` | HIGH·OBSERVED | `dfe42: e8 d9 6e 00 00  call e6d20` |
| `kmgr_sync_exec@0xdca70 → tpb_xu_get_by_vcore@0xe7b10` | HIGH·OBSERVED | `dcb5f: e8 ac af 00 00  call e7b10` |
| `kmgr_sync_exec@0xdca70 → tpb_xu_schedule_exec@0xe8040` | HIGH·OBSERVED | `dcbdb: e8 60 b4 00 00  call e8040` |
| `tpb_xu_schedule_exec@0xe8040 → tpb_xu_schedule_request@0xe7540` | HIGH·OBSERVED | `e810b: e8 30 f4 ff ff  call e7540` |

> **GOTCHA — `nrt_execute → nrt_execute_repeat` crosses the PLT, not a direct call.**
> The call site is `call 3c890 <nrt_execute_repeat@plt>`, and the PLT stub jumps
> through a GOT slot (`jmp *0xbcab9a(%rip)`) into the real body at `0x91650`, which is
> itself the exported `nrt_execute_repeat@@NRT_2.0.0`. The edge is real and HIGH, but
> it is **PLT/GOT-mediated** — a reimplementer wiring a direct call to `0x91650` is
> correct; expecting a direct `call 91650` in `nrt_execute`'s body is not.

> **NOTE — `tpb_xu_schedule_request` is a file-local symbol.** It disassembles as
> `_ZL23tpb_xu_schedule_request…` (`_ZL` = internal linkage) at `0xe7540`. It is not
> an export; reach it only through `tpb_xu_schedule_exec`.

---

## §6 — DEVICE KICKOFF + DMA-RING handoff (the host↔device boundary)

**`tpb_xu_schedule_request@0xe7540`** (file-local, size `0x51f`) is the **fork
point**: it both BUILDS the hw-exec queue entry (descriptor handoff) and TRIGGERS the
device (inference doorbell).

```c
tpb_xu_schedule_request @0xe7540 {
  //--- BUILD branch (descriptor handoff) ---
  dlr_add_to_hw_exec_queue @0xdd820;            // build exec-queue entry
  //--- TRIGGER branch (inference kickoff) ---
  dlr_kickoff_exec @0xdd890;                    // *** trigger device ***
  //--- and exec-unit queue / barriers ---
  xuq_client_submit @0xe9f10;  xuq_client_trigger_next @0xea090;
  kbl_sync_mode_exec_enc_barrier @0x3069e0;  kbl_async_mode_exec_enc_barrier @0x306a00;
  dlr_add_model_stop_request @0xdd950;          // stop path
}

// BUILD branch -> the DMA-ring tail (descriptor-handoff page territory)
dlr_add_to_hw_exec_queue @0xdd820 { kbl_compute_setup @0x306fb0; }
kbl_compute_setup @0x306fb0 {
    hw_exec_queue_add_exec_request @0x321420;
    kbl_exec_cc_enq_proxy_tasks @0x306fa0;       // collective proxy tasks
}
hw_exec_queue_add_exec_request @0x321420 {
    encd_get_num_descs_model_switch @0x253ac0;   // descriptor count
    hw_exec_queue_add_descriptors @0x3206f0;
}
hw_exec_queue_add_descriptors @0x3206f0 {
    sw_dma_queue_reserve_descriptors @0x448ce0;  // reserve ring slots
    sw_dma_queue_set_descriptors @0x448d30;
}
sw_dma_queue_set_descriptors @0x448d30 {
    sw_dma_queue_ring_id_get_at_idx @0x448cc0;
    dma_ring_copy_descriptors @0x22eca0;         // *** into the DMA ring ***  (last host node)
}

// TRIGGER branch -> the doorbell
dlr_kickoff_exec @0xdd890 { kbl_infer_kickoff @0x307320; }
kbl_infer_kickoff @0x307320 {
    db_physical_core_get_mla_and_tpb @0x2272a0;
    exec_kickoff_infer @0x2632e0;
    ntrace_record_event @0x300710;
}
exec_kickoff_infer @0x2632e0 {                  // *** host<->device dispatch boundary ***
    db_physical_core_get_mla_and_tpb @0x2272a0;
    tdrv_sync_get_inference_start @0x30a5c0;     // *** driver-side start ***
    ndl_nc_semaphore_increment @0xc3ba0;         // *** NC DOORBELL — one semaphore write ***
}
tdrv_sync_get_inference_start @0x30a5c0 {
    tdrv_arch_ops_init @0x308e80;                // function-pointer table init
    ensure_arch_ops_initialized.part.0 @0x308e50;
    // final hop into the kernel driver (ioctl/mmio) is via the arch_ops vtable ptr
    // -> NOT name-traceable; the named host spine ENDS here.    [MED INFERRED]
}
```

| Edge (caller@addr → callee@addr) | Tag | Confirmation |
|---|---|---|
| `tpb_xu_schedule_request@0xe7540 → dlr_add_to_hw_exec_queue@0xdd820` | HIGH·OBSERVED | `e7747: e8 d4 60 ff ff  call dd820` |
| `tpb_xu_schedule_request@0xe7540 → dlr_kickoff_exec@0xdd890` | HIGH·OBSERVED | `e7773: e8 18 61 ff ff  call dd890` |
| `tpb_xu_schedule_request@0xe7540 → xuq_client_submit@0xe9f10` | HIGH·OBSERVED | `e75f8: e8 13 29 00 00  call e9f10` |
| `dlr_add_to_hw_exec_queue@0xdd820 → kbl_compute_setup@0x306fb0` | HIGH·OBSERVED | `dd84a: e8 61 97 22 00  call 306fb0` |
| `kbl_compute_setup@0x306fb0 → hw_exec_queue_add_exec_request@0x321420` | HIGH·OBSERVED | `3070c9: e8 52 a3 01 00  call 321420` |
| `hw_exec_queue_add_exec_request@0x321420 → hw_exec_queue_add_descriptors@0x3206f0` | HIGH·OBSERVED | `321558: e8 93 f1 ff ff  call 3206f0` |
| `hw_exec_queue_add_exec_request@0x321420 → encd_get_num_descs_model_switch@0x253ac0` | HIGH·OBSERVED | `3215f9: e8 c2 24 f3 ff  call 253ac0` |
| `hw_exec_queue_add_descriptors@0x3206f0 → sw_dma_queue_reserve_descriptors@0x448ce0` | HIGH·OBSERVED | `32074b: e8 90 85 12 00  call 448ce0` |
| `hw_exec_queue_add_descriptors@0x3206f0 → sw_dma_queue_set_descriptors@0x448d30` | HIGH·OBSERVED | `320788: e8 a3 85 12 00  call 448d30` |
| `sw_dma_queue_set_descriptors@0x448d30 → dma_ring_copy_descriptors@0x22eca0` | HIGH·OBSERVED | `448e5c: e8 3f 5e de ff  call 22eca0` |
| `dlr_kickoff_exec@0xdd890 → kbl_infer_kickoff@0x307320` | HIGH·OBSERVED | `dd8f5: e8 26 9a 22 00  call 307320` |
| `kbl_infer_kickoff@0x307320 → exec_kickoff_infer@0x2632e0` | HIGH·OBSERVED | `307354: e8 87 bf f5 ff  call 2632e0` |
| `exec_kickoff_infer@0x2632e0 → tdrv_sync_get_inference_start@0x30a5c0` | HIGH·OBSERVED | `2632f9: e8 c2 72 0a 00  call 30a5c0` |
| `exec_kickoff_infer@0x2632e0 → ndl_nc_semaphore_increment@0xc3ba0` | HIGH·OBSERVED | `263314: e8 87 08 e6 ff  call c3ba0` |
| `tdrv_sync_get_inference_start@0x30a5c0 → tdrv_arch_ops_init@0x308e80` | HIGH·OBSERVED | `30a5f0: e8 8b e8 ff ff  call 308e80` |
| `arch_ops vtable → kernel-driver ioctl/mmio` | MED·INFERRED | function-pointer hop; not name-traceable |

> **NOTE — `dma_ring_copy_descriptors@0x22eca0` is the last host-side node** before
> the ring is visible to the device DMA engine (it is called three times in
> `sw_dma_queue_set_descriptors`, at `448e5c` / `448e7d` / `448ec4`). Its byte layout
> and doorbell belong to the descriptor-handoff page.

> **NOTE — the named host spine ENDS at `tdrv_sync_get_inference_start` +
> `ndl_nc_semaphore_increment`.** The doorbell is literally ONE semaphore write
> (`ndl_nc_semaphore_increment`); `tdrv_sync_get_inference_start` asks the driver
> layer to mark the inference start. Beyond that the hop into the kernel driver runs
> through the `arch_ops` function-pointer table (initialized by `tdrv_arch_ops_init`)
> and is INFERRED at name level. After the doorbell the Vision-Q7 POOL engine runs the
> staged ucode (the §1–§4 output).

---

## §7 — ASYNC EXECUTE variant

The async branch off `kmgr_exec` (§5) is a worker-thread variant that lands on the
same `tpb_xu_schedule_request` kickoff (§6) from a worker context.

```c
kmgr_exec @0xdfd50 -> kmgr_async_exec_add_work @0xe6d20 {
    kaew_post_request @0xe5cd0;                  // enqueue to worker thread
}
// [worker thread] kaew_* -> ... -> tpb_xu_schedule_request   (same §6 kickoff)
kmgr_exec @0xdfd50 -> kmgr_async_exec_poll @0xe6ab0;          // completion wait
```

| Edge | Tag | Confirmation |
|---|---|---|
| `kmgr_async_exec_add_work@0xe6d20 → kaew_post_request@0xe5cd0` | HIGH·OBSERVED | callgraph: `_ZL17kaew_post_request…@0xe5cd0` |
| `[worker] kaew_* → … → tpb_xu_schedule_request` | MED·INFERRED | crosses the thread boundary; not single-traced |

---

## §8 — Consolidated spine diagram (one screen)

Layer key: `[H]` = `libnrt.so` · `[P]` = `libnrtucode.so` · `[I]` = `libnrtucode_internal.so`

```
INIT     nrt_init [H 0x94e90]
           -> ucode_init_module [H 0x225940] -> ucode_shared_library_name [H 0x225930]
                 ~~dlopen string~~> "libnrtucode_extisa.so" -> nrtucode_get_api_level [P]
           -> kmgr_init [H 0xde080]

BUILD    nrtucode_opset_create [I 0x9b24c0] -> nrtucode_opset_add_instruction [I/P]
           -> nrtucode_ll_create [I 0x9b1a90]
                 -> nrtucode_get_ext_isa_internal [I 0x9b2b30]
                 -> prelink [I 0x9b5d60]
                       -> prelink_load_lib [I 0x9b5e70] -> validate_dynamic_load [I 0x9b71f0]
                              -> xtlib_verify_magic [I 0x9b6d40]        (PI-lib magic)
                       -> prelink_relocate_lib [I 0x9b6160] -> relocate_op [I 0x9b6660]
                              -> memcpy   (applies one patch word)
           -> nrtucode_ll_get_load_sequence [P 0x30a440] ===> POOL stream opcode 0x107A
                 (host emit: add_load_pool_arguments [H 0x276780] mov $0x107a,%r10d)

LOAD     nrt_load [H 0xa9fe0] -> nrt_load_util [H 0xa9920]
           -> neff_get_header_from_buffer [H 0x4ca2c0]
           -> kmgr_load_nn_nc [H 0xde280]
                 -> dlr_kelf_load [H 0xe0830]
                 -> dlr_kelf_stage [H 0xe0970] -> dlr_kelf_stage_model_add [H 0xe0730]
                       -> kbl_model_add [H 0x3058e0]
                             -> sequencer_setup_instr [H 0x4483d0]
                             -> dma_ring_setup_queue_bundles [H 0x22e630]
                             -> ucode_stage_libs [H 0x310ea0]
                                   -> ucode_stage_libs_impl [H 0x310c00]
                                         -> dmem_buf_copyin [H 0x229820]   (ucode -> DMEM)

EXEC     nrt_execute [H 0x91de0] --PLT--> nrt_execute_repeat [H 0x91650]
           -> kmgr_exec [H 0xdfd50]
                 -> kmgr_exec_pre [H 0xdf820]
                       -> kbl_compute_build_compute_resources [H 0x306790]
                       -> kbl_create_reference_feature_map_set [H 0x308420]
                 -> kmgr_sync_exec [H 0xdca70]                            (SYNC)
                 -> kmgr_async_exec_add_work [H 0xe6d20] -> kaew_post_request [H 0xe5cd0]  (ASYNC)

DISPATCH kmgr_sync_exec [H 0xdca70]
           -> tpb_xu_get_by_vcore [H 0xe7b10]
           -> tpb_xu_schedule_exec [H 0xe8040]
                 -> tpb_xu_schedule_request [H 0xe7540]   (_ZL file-local; the FORK)

KICKOFF  tpb_xu_schedule_request [H 0xe7540]
           |-(build)-> dlr_add_to_hw_exec_queue [H 0xdd820] -> kbl_compute_setup [H 0x306fb0]
           |              -> hw_exec_queue_add_exec_request [H 0x321420]
           |                    -> hw_exec_queue_add_descriptors [H 0x3206f0]
           |                          -> sw_dma_queue_set_descriptors [H 0x448d30]
           |                                -> dma_ring_copy_descriptors [H 0x22eca0]  (=> handoff page)
           |-(trigger)-> dlr_kickoff_exec [H 0xdd890] -> kbl_infer_kickoff [H 0x307320]
                          -> exec_kickoff_infer [H 0x2632e0]
                                -> tdrv_sync_get_inference_start [H 0x30a5c0]
                                -> ndl_nc_semaphore_increment [H 0xc3ba0]   *** DEVICE DOORBELL ***
```

**One-line spine (public entry → device):**

```
nrt_init -> nrt_load -> nrt_load_util -> kmgr_load_nn_nc -> dlr_kelf_stage ->
kbl_model_add -> ucode_stage_libs(_impl) -> dmem_buf_copyin   ||  (ucode build:
nrtucode_opset/ll_create -> prelink -> prelink_relocate_lib -> relocate_op)  ||
nrt_execute -> nrt_execute_repeat -> kmgr_exec -> kmgr_sync_exec ->
tpb_xu_schedule_exec -> tpb_xu_schedule_request -> dlr_kickoff_exec ->
kbl_infer_kickoff -> exec_kickoff_infer -> tdrv_sync_get_inference_start +
ndl_nc_semaphore_increment  -> [Vision-Q7 POOL engine runs the staged ucode].
```

---

## §9 — Adversarial self-verification

The five spine edges most likely to be wrong (file-local symbol, PLT-mediated,
function-pointer terminus, multi-call dedup) were re-challenged against the binary:

| # | Edge | Risk | Re-challenge result |
|---|---|---|---|
| 1 | `tpb_xu_schedule_exec@0xe8040 → tpb_xu_schedule_request@0xe7540` | callee is `_ZL` file-local — could be a same-name export | **HOLDS.** `e810b: e8 30 f4 ff ff  call e7540 <_ZL23tpb_xu_schedule_request…>` — the call resolves to the local symbol, no export confusion. |
| 2 | `nrt_execute@0x91de0 → nrt_execute_repeat@0x91650` | could be a direct call; risk of citing the wrong target | **HOLDS, with caveat.** It is `call 3c890 <nrt_execute_repeat@plt>`; the PLT stub `jmp *0xbcab9a(%rip)` lands on the exported body `0x91650`. Recorded as PLT-mediated (GOTCHA in §5). |
| 3 | `exec_kickoff_infer@0x2632e0 → ndl_nc_semaphore_increment@0xc3ba0` | the doorbell — the most consequential edge | **HOLDS.** `263314: e8 87 08 e6 ff  call c3ba0 <ndl_nc_semaphore_increment>`. |
| 4 | `tdrv_sync_get_inference_start@0x30a5c0 → kernel driver` | claimed as the host-spine terminus | **HOLDS as INFERRED.** Direct calls are only to `tdrv_arch_ops_init@0x308e80` / `ensure_arch_ops_initialized@0x308e50`; the driver hop runs through the arch_ops fn-ptr table — correctly tagged MED·INFERRED, spine ends here. |
| 5 | `kbl_model_add@0x3058e0 → ucode_stage_libs@0x310ea0` | the "silent install" edge, deep in a `0xb5b`-byte body | **HOLDS.** `3062d8: e8 c3 ab 00 00  call 310ea0 <ucode_stage_libs>`. |

No edge failed re-challenge. The PLT mediation on edge #2 is the only refinement and
is surfaced as a GOTCHA so a reimplementer does not look for a direct `call 91650`.

**Address agreement with siblings:** every spine address here matches the committed
runtime-synthesis and public-api-table pages — `nrt_load@0xa9fe0`,
`kmgr_load_nn_nc@0xde280`, `nrt_execute@0x91de0`, `nrt_init@0x94e90`,
`exec_kickoff_infer@0x2632e0`, `ndl_nc_semaphore_increment@0xc3ba0`, and
`add_load_pool_arguments@0x276780` emitting `mov $0x107a`. **No CORRECTION required** —
the spine, the narrative, and the API table agree on names and addresses.

---

## §10 — Cross-references

- **Narrative for every node here** (load/execute/lifecycle prose, the 0x107A
  micro-program expansion, the doorbell semantics) → [runtime/runtime-synthesis.md](runtime-synthesis.md)
- **Per-export signature + address table** for the `nrt_*` / `nrtucode_*` entries →
  [runtime/public-api-table.md](public-api-table.md)
- **Format of the patch entry consumed by `relocate_op`** (§3) →
  [runtime/ucode-relocation-consumer.md](ucode-relocation-consumer.md)
- **Byte layout + doorbell of `dma_ring_copy_descriptors`** (§6 tail) → page
  `runtime/host-device-descriptor-handoff.md` (forward reference; stub at time of writing)
- **Worked Vision-Q7 custom-op trace** that this spine dispatches →
  [../orientation/customop-end-to-end.md](../orientation/customop-end-to-end.md)
