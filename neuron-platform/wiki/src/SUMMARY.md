# Summary

[AWS Neuron Platform — Cross-Stack Reference](index.md)

---

# Architecture

- [Overview](architecture/overview.md)
- [End-to-End Cross-Stack Flow (4 Diagrams)](architecture/cross-stack-flow.md)
- [Codename Decoder (Tonga/Sunda/Cayman/Mariana ↔ CoreV1-V5 ↔ trn1/inf2/trn2/trn3)](architecture/codename-decoder.md)
- [ABI Versioning Matrix (PJRT, NRT_2.0.0/3.0.0, NEFF features, NCCL plugin)](architecture/abi-versioning.md)
- [Three Firmware-Running CPUs (NeuronCore + Xtensa NCFW + Q7 management)](architecture/three-cpus.md)
- [Daemon vs Subprocess Compile Modes](architecture/daemon-vs-subprocess.md)
- [Compile Cache Mechanism](architecture/compile-cache.md)

# File Formats

- [NEFF (Neuron Executable File Format)](formats/neff.md)
- [BIR JSON Schema Reference](formats/bir-json.md)
- [HLO Custom-Call Conventions (AwsNeuron* tags)](formats/hlo-custom-calls.md)
- [Penguin Python IR](formats/penguin-ir.md)
- [KELF (Kaena-ELF) Container](formats/kelf.md)

# Cross-Stack Topics

- [IOCTL → libnrt → User API Traceability Matrix (50 rows)](topics/ioctl-userapi-traceability.md)
- [Error and Status Propagation Flow](topics/error-and-status-flow.md)
- [NRT Symbol Versioning (NRT_2.0.0, NRT_3.0.0)](topics/nrt-versioning.md)
- [PJRT C-API Version Negotiation](topics/pjrt-api-versioning.md)
- [NCCL Net Plugin ABI Handshake (v4/v5/v6)](topics/nccl-net-plugin-abi.md)
- [Bootstrap and Rendezvous Protocol](topics/bootstrap-rendezvous.md)

# Reference

- [Glossary (Cross-Stack)](glossary.md)
- [Bibliography](bibliography.md)
- [Component Index](component-index.md)
