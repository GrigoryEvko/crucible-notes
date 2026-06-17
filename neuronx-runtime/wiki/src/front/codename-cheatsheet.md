# Architecture & Codename Cheat-Sheet

> **Pinned to:** kernel `aws-neuronx-dkms 2.27.4.0` (GPL-2.0 source) · runtime `libnrt.so 2.31.24.0-0b044f4ce`. The DEEP reconciliation (all six schemes + the off-by-one CORRECTION) is [arch/coretype-numbering](../arch/coretype-numbering.md); this page is the one-screen lookup, not a re-derivation.

## Abstract

Neuron names the same three silicon generations under several parallel integer schemes, spread across the kernel driver, the userspace runtime, and the firmware-provider libraries. This page is the terse cross-table for the *common* path — codename ↔ kernel arch enum ↔ PCI device-ID ↔ encoder (`encd`) arch-id ↔ cloud/marketing name ↔ DWARF source dir — so a reader can resolve a value at a glance.

It does **not** reconcile the firmware schemes or explain the off-by-one trap between them. Those, and the full per-scheme selection-code anchors, live on [arch/coretype-numbering](../arch/coretype-numbering.md); defer to it whenever the answer matters. The cross-table below agrees with that page byte-for-byte (`SUNDA=V2=2`, `CAYMAN=V3=3`, `MARIANA=V4=4`).

## The Cross-Table

Vendor is `0x1D0F` (Amazon / Annapurna Labs) for every row (`neuron_device.h:35`). One row per silicon generation; the kernel enum, encd arch-id, and the runtime `al_hal_tpb_arch_type` all share the same `{2,3,4}` integer by construction.

| Codename | Kernel arch enum (`neuron_arch.h`) | PCI device-ID(s) | encd arch-id | Cloud / marketing | Source dir (`tdrv/encd/archs/`) | Confidence |
|---|---|---|---|---|---|---|
| **SUNDA** | `NEURON_ARCH_V2 = 2` | `0x7164` (Trn1) · `0x7264` (Inf2) | `2` | Trainium1 + Inferentia2 | `sunda.c` | HIGH |
| **CAYMAN** | `NEURON_ARCH_V3 = 3` | `0x7364` (Trn2) | `3` | Trainium2 | `cayman.c` | HIGH |
| **MARIANA** | `NEURON_ARCH_V4 = 4` | `0x7564` · `0x7565` (Trn3) | `4` | Trainium3 | `mariana.c` | HIGH |

> **NOTE —** the counts do not line up, by design: 5 device-IDs ≠ 3 arch enums ≠ 4 products. `V2` is two products (Trn1 training + Inf2 inference) on one arch; `V4` is two device-IDs (`0x7564`/`0x7565`) on one arch. Route on the integer, never the name. There is **no enum value `1`** — the gap is intentional (`neuron_arch.h:12-18`). Device-ID derivation: [arch/pci-device-ids](../arch/pci-device-ids.md).

## Firmware-Scheme Note (deferred)

The firmware-provider libraries do **not** reuse `{2,3,4}`. Two *distinct* four-element schemes gate firmware selection, and they are off by one — the single most conflatable pair in the model:

- **Firmware blob-coretype `{5, 12, 20, 28}`** — `libncfw.so` collective-firmware blob switch (`libncfw_get_image` / `libncfw_ctx_log`).
- **ext-ISA arch_id `{6, 13, 21, 29}`** — `libnrtucode_extisa.so` GPSIMD/Q7 microcode router (`idx = arch_id − 6`).

A fourth firmware tier, **MARIANA_PLUS** (blob `28` / ext-ISA `29`), exists *only* in these two libraries — the kernel enum, the encd arch-id, and the KaenaHal type all stop at three values; on silicon it is MARIANA (Trn3) running a `v4_plus` image. Do **not** alias `{5,12,20,28}` with `{6,13,21,29}`, and do not derive either from `{2,3,4}`. The byte-exact proof, the `+1` CORRECTION, and the sixth (GPSIMD `core->kind`) scheme are on → [arch/coretype-numbering](../arch/coretype-numbering.md).

## Cross-References

- [Coretype Numbering Reconciliation](../arch/coretype-numbering.md) — the full six-scheme matrix, per-scheme selection-code anchors, and the blob-coretype × ext-ISA off-by-one CORRECTION this page defers to
- [PCI Device-ID → Arch Map](../arch/pci-device-ids.md) — the vendor/device-ID table, the ID→arch switch, and the per-arch BAR layout
- [Generations, the V2/V3/V4 Enum, and Cloud Naming](../arch/generations-enum.md) — the kernel enum, the first-wins latch, and the EMU/QEMU + platform-type axes
- [ext-ISA Dispatch Tables and arch-id Scheme](../gpsimd/dispatch-tables.md) — the `{6,13,21,29}` authority (`idx = arch_id − 6`) and the GPSIMD `core->kind` scheme
