# Coretype Numbering Reconciliation

> **Kernel:** `aws-neuronx-dkms 2.27.4.0` (GPL-2.0 source; cited `file:line` into `extracted/aws-neuronx-dkms_2.27.4.0_all/usr/src/aws-neuronx-2.27.4.0/`).
> **Runtime lib:** `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` · **Binaries:** `libnrt.so` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64, unstripped, DWARF), `libncfw.so` (SONAME `libncfw.so.2.31.1.0.cf13a49f`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`), `libnrtucode_extisa.so` (build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, stripped) — all under `opt/aws/neuron/lib/`.
> **Status:** Reimplementation-grade · **Evidence grade:** `file:line` (kernel) / symbol+addr (binaries), each scheme byte- or line-anchored · **Part I — Silicon & Architecture Model**, DEEP · [back to overview](overview.md)

## Abstract

The Neuron stack carries the **same three-to-four silicon generations under six different integer numbering schemes**, spread across the kernel driver, two userspace dispatch layers, and two firmware-provider libraries. A reimplementer who routes on the wrong one does not get a clean error — they mis-select a firmware blob, an ext-ISA microcode library, or a per-arch ops vtable, and the failure is silent. This page does not re-derive any scheme; each is owned and proven on its own page. This page is the **single reconciliation table** that lines them up side by side, plus the precise statement of where each scheme's selection code lives and why two of them — the firmware blob-coretype `{5,12,20,28}` and the ext-ISA arch_id `{6,13,21,29}` — look almost identical but are off by one and used by different code paths.

The four silicon generations are: **SUNDA** (NeuronCore-v2 = Trn1 + Inf2), **CAYMAN** (NeuronCore-v3 = Trn2), **MARIANA** (NeuronCore-v4 = Trn3), and **MARIANA_PLUS** (NeuronCore-v4+, the *same* Trn3 silicon running a `v4_plus` firmware/microcode image). The first three are silicon; the fourth is a microcode tier that only exists in the firmware-provider libraries — the kernel enum, the KaenaHal arch type, and the encd arch dispatch all stop at three values. Three of the six schemes share the *same* small integers `{2,3,4}` (the kernel enum, the KaenaHal `al_hal_tpb_arch_type`, and the encd arch-id), because they were deliberately kept in lock-step at the kernel↔runtime boundary. The other three diverge: the firmware blob-coretype `{5,12,20,28}`, the ext-ISA arch_id `{6,13,21,29}`, and the `core->kind` "scheme B" `{2,9,17,25}` that lives in the GPSIMD layer. The danger zone is the two firmware schemes, because both are four-element, both are monotonic, and both gate firmware selection — but they differ by `+1` and are consumed by two different libraries.

The mental frame is the same one a driver author meets whenever a chip family is exposed through several ABIs at once: there is one *physical* taxonomy and several *encoding* taxonomies layered on top, and the only safe rule is to translate at every boundary rather than assume any two layers share an integer. The kernel↔runtime boundary happens to share integers (`{2,3,4}`), which is convenient but also a trap — it tempts a reimplementer to assume the firmware layers share them too, and they do not. The body of this page is: the centerpiece reconciliation table (one row per generation, every cell confidence-tagged); a short per-scheme section pinning each scheme's selection code to an address or `file:line`; and the in-place CORRECTION documenting the blob-coretype-vs-ext-ISA off-by-one that bit an earlier draft of [xtensa-vision-q7](../gpsimd/xtensa-vision-q7.md).

For reimplementation, the contract is:

- **Which integer each layer keys on** — kernel/KaenaHal/encd all use `{2,3,4}` (sunda/cayman/mariana); the firmware blob provider uses `{5,12,20,28}`; the ext-ISA provider uses `{6,13,21,29}`. Translate at every boundary; never alias.
- **Where each selection happens** — the device-ID→enum switch (kernel), the `al_hal_tpb_get_arch_type` cmp ladder (KaenaHal), the encd `arch_init` rev dispatch, the `libncfw_get_image`/`libncfw_ctx_log` `{5,12,20,28}` switch, and the `idx = arch_id − 6` ext-ISA index math.
- **The two firmware schemes are distinct** — `{5,12,20,28}` (blob-coretype, `libncfw.so`) vs `{6,13,21,29}` (ext-ISA arch_id, `libnrtucode_extisa.so`), offset by `+1`, different libraries, different consumers. This is the documented off-by-one trap.
- **MARIANA_PLUS is firmware-only** — it has a blob-coretype (`28`) and an ext-ISA arch_id (`29`) but **no** kernel enum, KaenaHal, or encd value; on silicon it is MARIANA (v4).

## At a glance

| | |
|---|---|
| **Silicon generations** | SUNDA (v2) · CAYMAN (v3) · MARIANA (v4) · MARIANA_PLUS (v4+, firmware tier only) |
| **Schemes sharing `{2,3,4}`** | kernel `enum neuron_arch` · KaenaHal `al_hal_tpb_arch_type` · encd arch-id |
| **Firmware blob-coretype** | `{5, 12, 20, 28}` — `libncfw.so` `libncfw_get_image`/`_ctx_log` switch |
| **ext-ISA arch_id** | `{6, 13, 21, 29}` — `libnrtucode_extisa.so` `idx = arch_id − 6` |
| **The off-by-one** | blob-coretype `{5,12,20,28}` vs ext-ISA arch_id `{6,13,21,29}` = `+1`, different libs |
| **MARIANA_PLUS** | blob-coretype `28` / ext-ISA arch_id `29` only; no kernel/KaenaHal/encd value |
| **Owning pages** | [generations-enum](generations-enum.md) · [pci-device-ids](pci-device-ids.md) · [tdrv-arch-ops](../runtime/tdrv-arch-ops.md) · [serializer-families](../firmware/serializer-families.md) · [dispatch-tables](../gpsimd/dispatch-tables.md) |

---

## The Cross-Scheme Reconciliation Table

This is the centerpiece. One row per silicon generation; one column per numbering scheme; every cell carries the integer **and** a confidence tag for *that cell's* binding. The two firmware schemes — **firmware blob-coretype** and **ext-ISA arch_id** — are kept in separate columns deliberately, because they are the pair that is easy to conflate (see the [CORRECTION](#correction--the-blob-coretype--ext-isa-arch_id-off-by-one) below).

| Codename | Kernel enum (`neuron_arch.h`) | KaenaHal (`al_hal_tpb_arch_type`) | encd arch-id | Firmware blob-coretype (`libncfw.so`) | ext-ISA arch_id (`libnrtucode_extisa.so`) | Cloud instance | Confidence |
|---|---|---|---|---|---|---|---|
| **SUNDA** (v2) | `V2 = 2` | `SUNDA = 2` | `2` | `5` | `6` | Trn1 + Inf2 | kernel/KaenaHal/encd/blob/extisa: **HIGH**; cloud: **HIGH** |
| **CAYMAN** (v3) | `V3 = 3` | `CAYMAN = 3` | `3` | `12` | `13` | Trn2 | kernel/KaenaHal/encd/blob/extisa: **HIGH**; cloud: **HIGH** |
| **MARIANA** (v4) | `V4 = 4` | `MARIANA = 4` | `4` | `20` | `21` | Trn3 | kernel/KaenaHal/encd/blob/extisa: **HIGH**; cloud: **HIGH** |
| **MARIANA_PLUS** (v4+) | *(none)* | *(none)* | *(none)* | `28` | `29` | Trn3 (v4+ ucode) | blob/extisa: **HIGH**; "no kernel/KaenaHal/encd value": **HIGH**; "= MARIANA silicon": **MEDIUM** |

Reading the table, three structural facts a reimplementer must internalize:

1. **Columns 1–3 are the same integer.** Kernel `V2/V3/V4 = {2,3,4}` (`neuron_arch.h:12-18`), KaenaHal `SUNDA/CAYMAN/MARIANA = {2,3,4}`, and the encd arch-id `{2,3,4}` are byte-for-byte the same three values. This is by construction — the runtime reads the arch back from the kernel and re-uses the integer rather than re-encoding it. A `switch(arch){2→…;3→…;4→…}` is correct in all three layers.
2. **Columns 4 and 5 are *not* the same integer, and not the same as columns 1–3.** The firmware blob-coretype is `{5,12,20,28}`; the ext-ISA arch_id is `{6,13,21,29}`. They differ from each other by `+1` and from the kernel enum entirely. Neither can be derived from the kernel enum by a simple offset — the gaps (`5→12→20→28` and `6→13→21→29`) reflect a wider firmware encoding space with reserved intervening values, not a linear remap of `{2,3,4}`.
3. **MARIANA_PLUS exists only in columns 4–5.** It has a blob-coretype (`28`) and an ext-ISA arch_id (`29`), but the kernel, KaenaHal, and encd layers have no fourth value — they top out at `4`. MARIANA_PLUS is the *same Trn3 silicon* (column 6) running a distinct `v4_plus` firmware/microcode image; the ext-ISA `T2` (arch21) and `T3` (arch29) blob bodies are byte-identical, which is the structural evidence for the "same silicon, different ucode tier" reading ([dispatch-tables §3](../gpsimd/dispatch-tables.md)).

> **GOTCHA —** the convenient `{2,3,4}` agreement across the kernel↔runtime boundary tempts the assumption that *every* layer shares those integers. It does not. The moment a request crosses into firmware-provider territory (`libncfw.so` or `libnrtucode_extisa.so`), the key changes to `{5,12,20,28}` or `{6,13,21,29}`. A reimplementer must translate `{2,3,4} → {5,12,20,28}` (or `→ {6,13,21,29}`) at that boundary; the consumer that builds these calls lives in `libnrt`'s encd layer (the `CSWTCH.113 = [6,13,21]` table @`libnrtucode_extisa` consumer-side, [dispatch-tables](../gpsimd/dispatch-tables.md)).

> **NOTE —** there is a *sixth* scheme this page references but does not own: the GPSIMD `core->kind` "scheme B" `{2,9,17,25}`, decoded on [dispatch-tables](../gpsimd/dispatch-tables.md). It is neither the kernel `{2,3,4}` (despite SUNDA also being `2` there) nor either firmware scheme. It is listed here only so a reimplementer who meets a `2`/`9`/`17`/`25` in the GPSIMD layer does not mistake it for the kernel enum or a blob-coretype. Its full derivation belongs to that page.

---

## Scheme 1 — Kernel `enum neuron_arch` `{2,3,4}`

### Where it lives

The kernel taxonomy is `enum neuron_arch` (`neuron_arch.h:12-18`): `{INVALID=0, V2=2, V3=3, V4=4, NUM=5}`, with an intentional gap at value `1`. The integer is decided **once**, in `neuron_pci_set_device_architecture` (`neuron_pci.c:205-228`): it reads the PCI device-ID, switch-maps it to the enum (`0x7164`Trn1/`0x7264`Inf2 → `V2`; `0x7364`Trn2 → `V3`; `0x7564`/`0x7565`Trn3 → `V4`), and latches it first-wins via `narch_init` (`neuron_arch.c:33`). Every later dispatch reads the cached value through `narch_get_arch` (`neuron_arch.c:42`).

### Selection code

```c
// neuron_pci_set_device_architecture  (neuron_pci.c:205-228)
switch (pdev->device) {                       // :207
  case TRN1_DEVICE_ID0:  // 0x7164
  case INF2_DEVICE_ID0:  // 0x7264
    arch = NEURON_ARCH_V2;  break;            // :213-216
  case TRN2_DEVICE_ID0:  // 0x7364
    arch = NEURON_ARCH_V3;  break;            // :217-218
  case TRN3_DEVICE_ID0:  // 0x7564
  case TRN3_DEVICE_ID1:  // 0x7565
    arch = NEURON_ARCH_V4;  break;            // :220-222
  default: return;                            // :224 — unknown id: arch stays INVALID
}
narch_init(arch, revision);                   // :227 → neuron_arch.c:33 (first-wins latch)
```

This scheme is owned in full by [generations-enum](generations-enum.md) (the enum, the latch, the EMU/QEMU and platform-type axes) and [pci-device-ids](pci-device-ids.md) (the device-ID→arch map). CONFIDENCE: **HIGH** (GPL source, `file:line`-anchored).

---

## Scheme 2 — KaenaHal `al_hal_tpb_arch_type` `{2,3,4}`

### Where it lives

The userspace runtime mirrors the kernel integer. `al_hal_tpb_get_arch_type` (`libnrt.so` @`0x44bca0`) returns `enum al_hal_tpb_arch_type {INVALID=0, INVALID_1=1, SUNDA=2, CAYMAN=3, MARIANA=4, NUM=5}` — the *same* three live values as the kernel enum, the *same* gap at `0/1`. The arch-ops layer dispatches on it once, lazily, in `tdrv_arch_ops_init` (`@0x308e80`): a `switch` on the arch type installs one of three per-arch registrars into the global `tdrv_arch_ops` vtable.

### Selection code

```c
// tdrv_arch_ops_init  (libnrt.so @0x308e80)
switch (al_hal_tpb_get_arch_type()) {          // @0x44bca0
  case 2:  tdrv_arch_register_sunda();   break;   // @0x30b6a0
  case 3:  tdrv_arch_register_cayman();  break;   // @0x30c7d0
  case 4:  tdrv_arch_register_mariana(); break;   // @0x30d900
  default: __assert_fail("…arch ops…");           // never taken in practice
}
```

The numeric `cmp $0x2 / $0x3 / $0x4` ladder against the registrar calls is the byte-exact dispatch. This scheme is owned in full by [tdrv-arch-ops](../runtime/tdrv-arch-ops.md) (the 488-byte vtable, the lazy install, the per-arch fill). CONFIDENCE: **HIGH** (DWARF + decompile, addr-anchored).

---

## Scheme 3 — encd arch-id `{2,3,4}`

### Where it lives

The Neuron encoder (`encd`) per-silicon device layer keys on the same `{2,3,4}` integer. DWARF attributes the per-arch device functions to source files `sunda.c` / `cayman.c` / `mariana.c` (`/opt/workspace/KaenaRuntime/tdrv/encd/archs/`), and `arch_init` (`@0x2563f0`) is the single rev-keyed dispatch point that calls `mariana_arch_init` (`@0x25a930`) / `cayman_arch_init` (`@0x25df70`) / `sunda_arch_init` (adjacent cell) to populate the per-arch `soc_struct_t` vtable. The mapping is `2 → sunda`, `3 → cayman`, `4 → mariana` — the encd arch dispatch agrees byte-for-byte with the KaenaHal and kernel integers.

> **CORRECTION (ENCD-ARCH-OPS) —** an earlier reading paired the encd dispatch with the firmware blob-coretype `{5,12,20,28}`. It does not: the encd `arch_init` keys on the *same* `al_hal_tpb_arch_type` `{2,3,4}` (sunda=2/cayman=3/mariana=4) as the KaenaHal layer, because `arch_init` and `tdrv_arch_ops_init` both read the latched arch type, not a firmware coretype. The `{5,12,20,28}` keys belong exclusively to the `libncfw.so` blob switch (Scheme 4), which the encd layer *calls into* (passing a translated coretype) but does not itself dispatch on. CONFIDENCE: **HIGH** (DWARF source-TU attribution + the shared `al_hal_tpb_get_arch_type` source).

This scheme is owned in full by the per-arch device-layer pages (encd archs). CONFIDENCE: **HIGH** (DWARF `file:`-attributed, addr-anchored).

---

## Scheme 4 — Firmware blob-coretype `{5,12,20,28}` (`libncfw.so`)

### Where it lives

`libncfw.so` is the collective-firmware blob provider. Its two switch-keyed entry points — `libncfw_get_image` (@`0x1179`, returns the `{iram, dram}` blob pair for a generation) and `libncfw_ctx_log` (@`0x1309`, the CC-context JSON serializer dispatcher) — both key on a **blob-coretype** `{5, 12, 20, 28}`, **not** on the kernel/KaenaHal `{2,3,4}` and **not** on the ext-ISA `{6,13,21,29}`.

### Selection code

```c
// libncfw_ctx_log  (libncfw.so @0x1309) — and the identical switch in libncfw_get_image @0x1179
switch (arch_id) {                              // arch_id = blob-coretype
  case 5:  return sunda_ncfw_ctx_log(…);        // @0x1a12b  (v2_ncfw_* images; Trn1/Inf2)
  case 12: return cayman_ncfw_ctx_log(…);       // @0x32ed2  (v3_ncfw_*; Trn2)
  case 20: return mariana_ncfw_ctx_log(…);      // @0x4bc79  (v4_ncfw_*)
  case 28: return mariana_plus_ncfw_ctx_log(…); // @0x64a20  (v4_plus_ncfw_*)
  default: return 22;                           // EINVAL on unknown coretype
}
```

The four blob-coretype values index the four embedded blob pairs (`v2/v3/v4/v4_plus_ncfw_{iram,dram}_bin`, [serializer-families](../firmware/serializer-families.md) and the embedded-payloads page). This is the scheme where **MARIANA_PLUS first appears** (coretype `28`): the kernel/KaenaHal/encd layers have no fourth value, but the blob provider does. This scheme is owned by [serializer-families](../firmware/serializer-families.md). CONFIDENCE: **HIGH** (the `{5,12,20,28}` switch is decoded byte-exact at both entry points).

---

## Scheme 5 — ext-ISA arch_id `{6,13,21,29}` (`libnrtucode_extisa.so`)

### Where it lives

`libnrtucode_extisa.so` is the GPSIMD/Q7 ext-ISA microcode provider. Its two ext-ISA entry points — `nrtucode_get_ext_isa` (@`0x87a0` → shared router `sub_8660` @`0x8660`) and `nrtucode_get_num_ext_isa_libs` (@`0x87b0`) — key on an **ext-ISA arch_id** `{6, 13, 21, 29}` via index math `idx = arch_id − 6`. This is the **authority** for the `{6,13,21,29}` scheme; it is decoded byte-exact on [dispatch-tables](../gpsimd/dispatch-tables.md) and must not be re-derived here.

### Selection code

```c
// sub_8660 (router) @0x8660 ; identical math in get_num_ext_isa_libs @0x87bc
idx = arch_id - 6;             // add $0xfffffffa  (byte 0xfa = -6)  @0x870b / @0x87bc
if ((u32)idx > 0x17) return 1; // cmp $0x17 — unsigned; folds idx<0 and idx>23
goto *JT_EXT[idx];             // only idx 0/7/15/23 → real handlers
//   arch_id 6  → T0 (SUNDA, 1 ext-ISA lib)
//   arch_id 13 → T1 (CAYMAN v3, 4 libs)
//   arch_id 21 → T2 (MARIANA v4, 4 libs)
//   arch_id 29 → T3 (MARIANA_PLUS v4+, 4 libs; bodies byte-identical to T2)
```

The byte-exact anchors are `41 83 c7 fa` (`add $-6`) @`0x870b`, the `cmp $0x17` in-range check, the `CSWTCH @0x86ada8 = {6,13,21}` consumer-side table, and `get_num_ext_isa_libs` returning lib counts `{1, 4, 4, 4}` for `{6,13,21,29}`. This scheme is owned by [dispatch-tables](../gpsimd/dispatch-tables.md). CONFIDENCE: **HIGH** (index immediate + both jump tables decoded byte-exact on the owning page).

---

## CORRECTION — the blob-coretype × ext-ISA arch_id off-by-one

> **CORRECTION (CORETYPE-OFFBY1) —** an earlier draft of [xtensa-vision-q7](../gpsimd/xtensa-vision-q7.md) used the firmware **blob-coretype** scheme `{5, 12, 20, 28}` where the ext-ISA **arch_id** scheme `{6, 13, 21, 29}` belonged (caught in the gpsimd lane audit; fixed in commit `dc88044`). The two schemes are the single most conflatable pair in the entire Neuron numbering model, and the trap is structural:
>
> - **They look the same.** Both are exactly four elements, both are strictly monotonic, both gate *firmware* selection, and their first elements (`5` vs `6`) sit one apart — so a transposed table looks plausible at a glance.
> - **They are not the same.** The blob-coretype `{5,12,20,28}` is the key of `libncfw.so`'s `libncfw_get_image` / `libncfw_ctx_log` switch (Scheme 4). The ext-ISA arch_id `{6,13,21,29}` is the key of `libnrtucode_extisa.so`'s `idx = arch_id − 6` math (Scheme 5). They are offset by exactly `+1` element-wise (`5→6`, `12→13`, `20→21`, `28→29`), they live in **two different libraries**, and they are consumed by **two different code paths** — a request to the wrong library with the wrong-by-one key either mis-selects a blob or falls through to the default error stub.
>
> The byte-exact proof that settles which is which lives on [dispatch-tables](../gpsimd/dispatch-tables.md): the ext-ISA router computes `idx = arch_id − 6` (`add $0xfffffffa` = `−6`, byte `0xfa`, @`0x870b` and @`0x87bc`), so the reachable arch_ids are `{6,13,21,29}` — never `{5,12,20,28}`. The `{5,12,20,28}` values are the *firmware blob-coretype*, proven independently by the `libncfw.so` switch at @`0x1179`/@`0x1309`. A reimplementer who needs an ext-ISA microcode library must pass `{6,13,21,29}` to `libnrtucode_extisa.so`; one who needs a collective-firmware blob must pass `{5,12,20,28}` to `libncfw.so`. The `+1` is real; do not collapse the two columns of the reconciliation table into one. CONFIDENCE: **HIGH** (both selection sites decoded byte-exact on their owning pages).

---

## Cross-References

- [Generations, the V2/V3/V4 Enum, and Cloud Naming](generations-enum.md) — owns Scheme 1: the kernel `enum neuron_arch` `{2,3,4}`, the first-wins latch, the EMU/QEMU and platform-type axes
- [PCI Device-ID → Arch Map](pci-device-ids.md) — the device-ID→kernel-enum switch that feeds Scheme 1
- [ext-ISA Dispatch Tables and arch-id Scheme](../gpsimd/dispatch-tables.md) — owns Scheme 5: the byte-exact `idx = arch_id − 6` authority for `{6,13,21,29}` and the blob-coretype off-by-one proof
- [Serializer Families (per-arch CC-context dumpers)](../firmware/serializer-families.md) — owns Scheme 4: the `libncfw.so` blob-coretype `{5,12,20,28}` switch and the MARIANA_PLUS tier
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](../runtime/tdrv-arch-ops.md) — owns Scheme 2: the KaenaHal `al_hal_tpb_arch_type` `{2,3,4}` dispatch
- [Tensilica Xtensa and Vision-Q7 Identification](../gpsimd/xtensa-vision-q7.md) — the page whose earlier draft hit the blob-coretype × ext-ISA off-by-one (fixed in `dc88044`)
- [back to overview](overview.md)
