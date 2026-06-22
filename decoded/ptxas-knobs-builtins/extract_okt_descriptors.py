#!/usr/bin/env python3
"""Extract the ptxas OKT knob dump-schema descriptor table.

The dump path (`DUMP_KNOBS_TO_FILE` / JSON knob dumper `sub_446240`) is driven by a
self-describing schema table at VMA ``0x1CE9C40`` in ptxas (CUDA 13.0.88). It is the
*only* reader of the table (single ``mov $0x1CE9C40,%ebx`` at ``0x44675C``); the table
carries no runtime behavior, which is why every field is a plain ``char*`` ASCII
string the dumper can re-emit byte-for-byte.

Layout (binary-verified): a contiguous array of **1000** entries, **72-byte stride**
(9 ``char*`` qwords each). The count is exact::

    (0x1CFB580 - 0x1CE9C40) / 72 == 1000

The nine string pointers per entry map to the emitted JSON schema fields::

    field  off   column          example (entry 0)
    [0]    0x00  bss_offset      "0x590"
    [1]    0x08  (name/index)    ""           (usually empty)
    [2]    0x10  type            "OKT_INT"
    [3]    0x18  (description)   ""           (usually empty)
    [4]    0x20  min             "0"
    [5]    0x28  max             "1"
    [6]    0x30  default         "\"\""       (empty-string literal)
    [7]    0x38  stepsize        "0x1"
    [8]    0x40  flags           "0x0"

Emitted as a 10-column TSV: ``index`` plus the nine raw string fields. Strings are
emitted exactly as stored (control chars escaped) so the TSV reproduces byte-for-byte.

Recovered solely from a freely distributed binary (DMCA 1201(f) interoperability
research).
"""
import csv
import struct
import sys

TABLE_VMA = 0x1CE9C40
SENTINEL_VMA = 0x1CFB580
STRIDE = 72
NFIELDS = 9
COUNT = (SENTINEL_VMA - TABLE_VMA) // STRIDE  # == 1000


def section_table(elf: bytes):
    e_shoff = struct.unpack_from("<Q", elf, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", elf, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", elf, 0x3C)[0]
    secs = []
    for i in range(e_shnum):
        base = e_shoff + i * e_shentsize
        addr = struct.unpack_from("<Q", elf, base + 0x10)[0]
        off = struct.unpack_from("<Q", elf, base + 0x18)[0]
        size = struct.unpack_from("<Q", elf, base + 0x20)[0]
        secs.append((addr, off, size))
    return secs


def vma_to_off(secs, vma):
    for addr, off, size in secs:
        if addr and addr <= vma < addr + size:
            return off + (vma - addr)
    return None


def cstr(elf, secs, ptr):
    if ptr == 0:
        return ""
    off = vma_to_off(secs, ptr)
    if off is None:
        return "<nonrodata 0x%x>" % ptr
    end = elf.find(b"\x00", off)
    return elf[off:end].decode("latin1", "replace")


def _clean(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def extract(path):
    elf = open(path, "rb").read()
    secs = section_table(elf)
    base = vma_to_off(secs, TABLE_VMA)
    if base is None:
        raise SystemExit("table VMA 0x%x not mapped in %s" % (TABLE_VMA, path))
    rows = []
    for i in range(COUNT):
        eoff = base + i * STRIDE
        ptrs = struct.unpack_from("<%dQ" % NFIELDS, elf, eoff)
        fields = [cstr(elf, secs, p) for p in ptrs]
        rows.append((i, fields))
    return rows


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "../../ptxas/ptxas"
    rows = extract(path)
    w = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    w.writerow(["index", "bss_offset", "name", "type", "description",
                "min", "max", "default", "stepsize", "flags"])
    for idx, f in rows:
        w.writerow([idx] + [_clean(x) for x in f])


if __name__ == "__main__":
    main()
