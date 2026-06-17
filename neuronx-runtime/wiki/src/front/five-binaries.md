# The Five Binaries & Subsystem Map

> *Version pin (every artifact this book derives from), all in package `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` unless noted:*
> - **`libnrt.so`** → `libnrt.so.1` → `libnrt.so.2.31.24.0` — SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce917b633a70eb3d0bc460f34ac3da620`. ELF64 x86-64 DYN, **not** stripped, DWARF v4.
> - **`aws-neuronx-dkms 2.27.4.0`** — GPL-2.0 kernel module (`neuron.ko`), shipped as C source under `/usr/src/aws-neuronx-2.27.4.0/`.
> - **`libncfw.so`** — SONAME `libncfw.so.2.31.1.0.cf13a49f`, build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, 615,640 B. ELF64 x86-64 DYN, symtab but **no** DWARF.
> - **`libnrtucode_extisa.so`** — build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, 9,656,488 B. ELF64 x86-64 DYN, **stripped**, no DWARF.
> - **`libnccom.so.2.31.24`** (pkg `aws-neuronx-collectives 2.31.24.0-1a31ba186`) — SONAME `libnccom.so.2`, build-id `9c00176c081788c9435d27d11bb40e92495463f0`; sibling **`libnccom-net.so`** build-id `3415f096…`.
> - **`libnds.a`** — static archive (6 `neuron_ds*.c.o` members), link-folded into `libnrt.so`.
>
> *Part 0 — Front Matter / orientation MAP · **Evidence grade:** every binary identity here (SONAME, build-id, the link model on each arrow) is `readelf`/`nm`/`objdump`-anchored on the named binary; the deep pages this map points at own the byte-level derivations. This page does not re-derive any subsystem. · [back to index](../index.md)*

## Abstract

A Neuron host runs **one** userspace runtime, `libnrt.so`, and that library is the hub every other binary hangs off of. Five shipped artifacts plus one static archive make up the stack: `libnrt.so` (the runtime), `aws-neuronx-dkms` (the kernel driver), `libncfw.so` (collectives firmware carrier), `libnrtucode_extisa.so` (GPSIMD/Q7 microcode provider), and `libnccom.so` + `libnccom-net.so` (the NCCL fork that drives inter-node collective transport). The sixth, `libnds.a`, is not a separate runtime object at all — it is the Neuron DataStore static library, compiled once and linked into `libnrt.so` (its 6 `nds/` TUs appear as first-party compile units in `libnrt`'s DWARF). The whole book is the expansion of this one diagram.

A model flows across these binaries in one direction with branches. A framework hands a NEFF to `libnrt`'s public `nrt_*` C ABI; `libnrt` unpacks the gzip-tar container, builds per-arch instruction blocks, and talks to silicon **only** through the driver's ioctl/mmap surface. Two of the binaries are not runtimes but *firmware carriers*: `libncfw.so` ships the Xtensa sequencer images that choreograph collective DMA, and `libnrtucode_extisa.so` ships the GPSIMD pool-engine microcode — `libnrt` pulls bytes out of their `.rodata` and DMAs them onto the device itself; neither carrier ever touches hardware. When a model uses collectives, `libnrt` additionally `dlopen`s `libnccom.so` to reach the network transport, while keeping the collective *algorithm* engine (`alg_ring`/`kangaring`/`mesh`/RDH) statically inside itself.

The single fact a reader must carry forward is that **the four edges out of `libnrt` use four different bind mechanisms**, and confusing them breaks any reimplementation: the kernel is an `ioctl` syscall surface, the two firmware carriers and `libnccom` are all `dlopen` (no `DT_NEEDED`), and `libnds.a` is a compile-time static link. The collectives seam is the subtle one — it is `dlopen`-forward but a *hard, versioned* `DT_NEEDED` in reverse, gated by an out-of-band numeric "compat 89" handshake. The rest of this map fixes each binary's identity, draws the layered stack with the bind model on every arrow, and routes the reader to the subsystem page that owns each edge.

## The Binaries at a Glance

| Binary | SONAME / name | Package / version | Build-id (prefix) | DWARF? | Role | How it binds to `libnrt` |
|---|---|---|---|---|---|---|
| `libnrt.so` | `libnrt.so.1` | `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` | `8bb57aba…` | yes (v4) | host x86-64 userspace runtime — the hub | (is the hub) |
| `neuron.ko` | `aws-neuronx-dkms` | `2.27.4.0` (GPL-2.0) | (kernel module) | (GPL C source) | PCIe accelerator driver; one `/dev/neuronN` per device | **`ioctl` + `mmap`** (ioctl magic `'N'`) |
| `libncfw.so` | `libncfw.so.2.31.1.0.cf13a49f` | `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` | `a98f8e1c…` | no (symtab only) | NCFW carrier — embeds 8 Xtensa sequencer blobs | **`dlopen`** at NCFW upload |
| `libnrtucode_extisa.so` | (no SONAME cited) | `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` | `7bb03bc4…` | no (stripped) | GPSIMD/Q7 microcode provider (52 `nrtucode_*` exports) | **`dlopen`** + `dlsym` (30-of-52, API-level-3 gate) |
| `libnccom.so` | `libnccom.so.2` | `aws-neuronx-collectives 2.31.24.0-1a31ba186` | `9c00176c…` | yes | NCCL fork — collective comm/transport core | **`dlopen`** + `dlsym` (37 `neuron*`); reverse `DT_NEEDED libnrt.so.1` |
| `libnccom-net.so` | — | `aws-neuronx-collectives 2.31.24.0-1a31ba186` | `3415f096…` | — | aws-ofi-nccl / libfabric network plugin | **`dlopen`** (via `libnccom`); handle handed back through `nrt_get_libnccl_net` |
| `libnds.a` | (static archive) | `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` | — | (folded) | Neuron DataStore (6 TUs) | **static link** into `libnrt.so` |

> **NOTE —** `libnccom.so` and `libnccom-net.so` ship in a **separate package** (`aws-neuronx-collectives`); they are absent from the runtime-lib package and are only present on a host that installed collectives. A model without collectives never `dlopen`s them.

## The Layered Stack

```text
  framework / integration (PyTorch-Neuron, …)
        │  nrt_* / nrta_*  C ABI  (149 versioned exports: 141 @NRT_2.0.0, 8 @NRT_3.0.0)
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  libnrt.so   (8bb57aba…, SONAME libnrt.so.1)   — host x86-64 runtime   │
  │  NEFF unpack · per-arch instruction blocks · collectives algo engine   │
  │  (alg_ring/kangaring/mesh/RDH embedded) · libnds.a folded in (static)  │
  └───┬──────────────┬──────────────────┬─────────────────────┬───────────┘
      │              │                  │                     │
   ioctl 'N'      dlopen             dlopen + dlsym         dlopen + dlsym
   + mmap       (NCFW upload)     (ext-ISA provider load)  (compat-89 gate)
      │              │                  │                     │
      ▼              ▼                  ▼                     ▼
  ┌────────┐   ┌──────────┐      ┌──────────────────┐   ┌──────────────┐
  │neuron  │   │libncfw.so│      │libnrtucode_      │   │ libnccom.so  │
  │.ko     │   │a98f8e1c… │      │  extisa.so       │   │ 9c00176c…    │
  │(DKMS   │   │8 Xtensa  │      │ 7bb03bc4…        │   │ NCCL fork    │
  │ 2.27.4)│   │ blobs    │      │ 13 Xtensa blobs  │   │  ─DT_NEEDED─┐│
  └───┬────┘   └────┬─────┘      └────────┬─────────┘   │   reverse → ││
      │             │  (bytes DMA'd        │  (bytes      │ libnrt.so.1 ││
      │             │   by libnrt)         │   DMA'd)     └──────┬──────┘│
      ▼             ▼                      ▼                     ▼ dlopen │
  ════════════════════════════════════════════════════════════ libnccom │
   on-device engines                                            -net.so  │
   • sync / sequencer cores (Xtensa LX) ◄── run NCFW images   (3415f096…)│
   • GPSIMD Vision-Q7    (Xtensa IVP32) ◄── run ext-ISA microcode         │
   • TPB compute (PE/ACT/POOL/DVE/SP) ◄── driven by instruction blocks    │
  ═══════════════════════════════════════════════════════════════════════
```

> **QUIRK —** the `libnccom` arrow points *out* of `libnrt` (runtime `dlopen`), but the ELF dependency points *back in*: `libnccom.so` carries `DT_NEEDED libnrt.so.1` and a VERNEED on `NRT_2.0.0`. The forward call-out is unversioned and guarded only by the numeric compat-89 gate; the reverse call-back is a hard, load-time versioned link. Two skew dimensions, two mechanisms — see [nccl-boundary](../collectives/nccl-boundary.md).

## The Four Bind Models

Each edge out of `libnrt` is a *different* kind of link. A reimplementer must reproduce the right mechanism on each — they are not interchangeable.

- **`ioctl` + `mmap` → kernel driver.** `libnrt` has no library dependency on the driver; it opens `/dev/neuronN` and issues ioctls (magic `'N'`) plus `mmap` for BAR/DRAM/notification-ring windows. There is no `.read`/`.write`/`.compat_ioctl` — the entire userspace↔device contract is the ioctl table. Evidence: the driver's `ncdev_fops` exposes only `.unlocked_ioctl` and `.mmap`. See [kernel/overview](../kernel/overview.md).
- **`dlopen` → `libncfw.so` (firmware carrier).** `libnrt`'s `encd_libncfw_init` `dlopen`s the carrier and `dlsym`s `libncfw_get_image`, which returns `{&iram, iram_size, &dram, dram_size}` pointing straight into `libncfw`'s `.rodata`; `libnrt` does the DMA and reset-release. No `DT_NEEDED`. Evidence: `libncfw` imports only libc, zero device I/O. See [firmware/overview](../firmware/overview.md).
- **`dlopen` + `dlsym` → `libnrtucode_extisa.so` (ext-ISA provider).** `libnrt`'s `ucode/ucode.c` `dlopen`s the provider, `dlsym`s a fixed 30-entry subset of its 52 `nrtucode_*` exports, and asserts `nrtucode_get_api_level() == 3` before driving init→load→query. No `DT_NEEDED`. See [gpsimd/extisa-provider](../gpsimd/extisa-provider.md).
- **`dlopen` + `dlsym` → `libnccom.so` (collectives), reverse `DT_NEEDED`.** Forward: `ncclInit` (`0x1bff30`) `dlopen`s `libnccom.so` with `RTLD_NOW`, version-gates on **compat 89**, then `dlsym`s 37 `neuron*` entry points into a `.bss` slot table — all-or-nothing. Reverse: `libnccom` hard-links `libnrt.so.1` and imports **16 `nec_*` + 4 `nrt_*` = 20** symbols, all `@NRT_2.0.0`. See [nccl-boundary](../collectives/nccl-boundary.md) and [appendix/symbol-versions](../appendix/symbol-versions.md).
- **static link → `libnds.a`.** The Neuron DataStore archive (6 `neuron_ds*.c.o` members) is compiled once and linked into `libnrt.so` at build time; its TUs show up as first-party `nds/` compile units in `libnrt`'s DWARF. There is no runtime edge. See [front/source-tree](source-tree.md).

> **GOTCHA —** the collectives *algorithm* engine is **not** in `libnccom`. The ring/kangaring/mesh/RDH composers (`alg_ring_init`, `alg_kangaring_init`, `enc_*`) are statically embedded inside `libnrt.so` itself; only the transport/bootstrap/OFI layer crosses the `dlopen` boundary into `libnccom`. A reimplementation that puts the algorithm engine behind the `dlopen` seam mis-partitions the stack.

## The Three On-Device CPUs

A second easy-to-miss split: the stack spans **three distinct CPUs**, only one of which is the host. Keeping them straight is what makes the firmware-vs-microcode distinction legible.

> **NOTE —** the three CPUs:
> 1. **Host x86-64** — runs `libnrt.so` and the kernel driver. This is the only CPU any shipped `.so` executes on directly; the two "firmware" libraries (`libncfw.so`, `libnrtucode_extisa.so`) are *host* x86-64 objects that merely *carry* device code in their `.rodata`.
> 2. **On-device sync / sequencer cores** — Tensilica **Xtensa LX** (sequencer-class TIE) on each NeuronCore's TPB. These run the **NCFW** images carried by `libncfw.so` (one IRAM + one DRAM blob per arch generation `{v2, v3, v4, v4_plus}`) to choreograph collective-communication DMA. See [firmware/overview](../firmware/overview.md).
> 3. **GPSIMD "Q7"** — Tensilica **Xtensa Vision-IVP32** (Vision-Q7 product). It runs the **ext-ISA pool-engine microcode** carried by `libnrtucode_extisa.so` (13 Xtensa ELF32 blobs). The firmware self-labels `"Q7:"` in its diagnostics. See [gpsimd/extisa-provider](../gpsimd/extisa-provider.md).
>
> The sync/sequencer cores and the GPSIMD-Q7 are the **same Tensilica Xtensa LX ISA family in two TIE configurations** — not two separate ISAs. The host x86 is the odd one out.

## Where to Go Next

Each edge and on-device target has an owning subsystem map. Route there for the byte-level derivation this page deliberately omits.

- **Public C ABI & version nodes** → [appendix/symbol-versions](../appendix/symbol-versions.md) — the three-node `.gnu.version_d` graph (`libnrt.so.1` / `NRT_2.0.0` / `NRT_3.0.0`), the export roster, and the `nec_*`/`nrt_*` reverse seam (16 + 4 = 20).
- **Kernel driver** → [kernel/overview](../kernel/overview.md) — the ioctl/mmap surface, the `ndhal` arch abstraction, DMA rings, and the privilege model.
- **Collectives boundary** → [nccl-boundary](../collectives/nccl-boundary.md) — the `dlopen`-forward / `DT_NEEDED`-reverse asymmetry, the compat-89 handshake, and the `nec_*`/`nccl*` wrapper shape.
- **Collectives firmware** → [firmware/overview](../firmware/overview.md) — `libncfw.so` as carrier + serializer, the `5/12/20/28` coretype switch, and the Xtensa sequencer images.
- **GPSIMD microcode** → [gpsimd/extisa-provider](../gpsimd/extisa-provider.md) — the 52 `nrtucode_*` exports, the four handle structs, the device mailbox map, and the load record.
- **ELF / forensics ground truth** → [forensics/elf-anatomy](../forensics/elf-anatomy.md) — VMA == file-offset, the non-standard `PROGBITS` sections, and the toolchain census.
- **Binary layout reference** → [reference/binary-layout](../reference/binary-layout.md) — the cross-binary `.rodata` blob/offset map for the embedded firmware and microcode.

## Cross-References

- [kernel/overview](../kernel/overview.md) — the `ioctl 'N'` + `mmap` edge: the only userspace↔device surface.
- [nccl-boundary](../collectives/nccl-boundary.md) — the `libnrt` ↔ `libnccom` seam: `dlopen`-forward, `DT_NEEDED`-reverse, compat-89.
- [gpsimd/extisa-provider](../gpsimd/extisa-provider.md) — the `dlopen`'d GPSIMD/Q7 microcode provider (`nrtucode_*`).
- [firmware/overview](../firmware/overview.md) — the `dlopen`'d NCFW sequencer-firmware carrier (`libncfw.so`).
- [appendix/symbol-versions](../appendix/symbol-versions.md) — the version-node graph and the 20-symbol `nec_*`/`nrt_*` reverse seam.
- [reference/binary-layout](../reference/binary-layout.md) — the embedded-blob `.rodata` layout for the two firmware carriers.
