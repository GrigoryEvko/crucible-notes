#!/usr/bin/env python3
"""Extract ptxas's diagnostic/message catalog from the ptxas ELF binary.

ptxas keeps its emittable diagnostics in two arrays of 16-byte records living in
the writable `.data` section.  Each record is:

    struct ptxas_msg {
        uint32_t severity;   //  2=info 3=warning 5=error 6=fatal (4="error*", recoverable; 1=note)
        uint32_t _pad;       //  always 0
        const char *format;  //  printf-style format string in .rodata
    };

The message id is simply the record's index into its table.  The emit function
`sub_42F590(&table[id], ...)` reads `record.severity`, maps it to a printed
prefix ("fatal   ", "error   ", "warning ", "info    ", ...) and vfprintf's the
format string with the call-site varargs.  Severity->prefix/class mapping is
itself read from two small rodata tables (prefix `char*[]` and class `uint8[]`):

    code 0: (internal)  class 4      code 4: "error*  " class 1
    code 1: ""/note     class 0      code 5: "error   " class 2
    code 2: "info    "  class 0      code 6: "fatal   " class 4
    code 3: "warning "  class 1

Two tables are present (distinct id-spaces):
  * "main" (465 records) - PTX front-end / SASS assembler diagnostics
  * "link" ( 41 records) - linker / options-file / DWARF diagnostics

All format strings are stored in cleartext in .rodata; no de-obfuscation needed.
(Records immediately following the main table are the ROT47-obfuscated opcode/
pseudo-instruction name pool, not diagnostics.)

Recovered solely from a freely distributed binary (DMCA 1201(f) interoperability
research).  Run against your own ptxas to regenerate.
"""
import struct
import csv
import sys


SEV = {1: "note", 2: "info", 3: "warning", 4: "error*", 5: "error", 6: "fatal"}

# Table locations for CUDA 13.0.88 ptxas (file offsets in .data).  For another
# build, run `find_tables()` which auto-locates them.
TABLES_13_0_88 = [("main", 0x23FA4D0, 465), ("link", 0x23FD590, 41)]


def section_map(elf: bytes):
    """Return (rodata_vma, rodata_off, rodata_size) by parsing the ELF section
    headers, so .rodata string pointers can be resolved to file offsets."""
    e_shoff = struct.unpack_from("<Q", elf, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", elf, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", elf, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", elf, 0x3E)[0]
    shstr_off = struct.unpack_from("<Q", elf, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]

    def name_at(noff):
        end = elf.find(b"\x00", shstr_off + noff)
        return elf[shstr_off + noff:end].decode()

    secs = {}
    for i in range(e_shnum):
        base = e_shoff + i * e_shentsize
        nm = name_at(struct.unpack_from("<I", elf, base)[0])
        addr = struct.unpack_from("<Q", elf, base + 0x10)[0]
        off = struct.unpack_from("<Q", elf, base + 0x18)[0]
        size = struct.unpack_from("<Q", elf, base + 0x20)[0]
        secs[nm] = (addr, off, size)
    return secs


def resolver(elf):
    secs = section_map(elf)
    rod_vma, rod_off, rod_sz = secs[".rodata"]

    def isrod(p):
        return rod_vma <= p < rod_vma + rod_sz

    def cstr(vma):
        o = vma - (rod_vma - rod_off)
        end = elf.find(b"\x00", o)
        return elf[o:end].decode("latin1", "replace")

    return isrod, cstr, secs


def find_tables(elf):
    """Auto-locate diagnostic tables: contiguous runs (>=20) of {sev in valid
    set, pad==0, rodata ptr} 16-byte records in .data."""
    isrod, _, secs = resolver(elf)
    data_vma, data_off, data_sz = secs[".data"]
    runs = []
    off = data_off
    end = data_off + data_sz
    while off + 16 <= end:
        sev, pad, ptr = struct.unpack_from("<IIQ", elf, off)
        if sev in SEV and pad == 0 and isrod(ptr):
            start = off
            n = 0
            while off + 16 <= end:
                sev, pad, ptr = struct.unpack_from("<IIQ", elf, off)
                if sev in SEV and pad == 0 and isrod(ptr):
                    n += 1
                    off += 16
                else:
                    break
            if n >= 20:
                runs.append((start, n))
        else:
            off += 16
    return runs


def extract(path, tables=None):
    elf = open(path, "rb").read()
    isrod, cstr, _ = resolver(elf)
    if tables is None:
        runs = find_tables(elf)
        base_names = ["main", "link"]
        names = [base_names[i] if i < len(base_names) else "extra%d" % i
                 for i in range(len(runs))]
        tables = [(names[i], runs[i][0], runs[i][1]) for i in range(len(runs))]
    rows = []
    for comp, start, n in tables:
        for idx in range(n):
            sev, pad, ptr = struct.unpack_from("<IIQ", elf, start + idx * 16)
            if not (isrod(ptr) and pad == 0 and sev in SEV):
                raise ValueError("bad record %s[%d] @%#x" % (comp, idx, start + idx * 16))
            rows.append((comp, idx, sev, SEV[sev], cstr(ptr)))
    return rows


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "../../ptxas/ptxas"
    auto = "--auto" in sys.argv
    rows = extract(path, None if auto else TABLES_13_0_88)
    w = csv.writer(sys.stdout, delimiter="\t")
    w.writerow(["table", "id", "severity_code", "severity", "message", "source"])
    for comp, idx, sev, name, msg in rows:
        w.writerow([comp, idx, sev, name, msg, "plaintext"])


if __name__ == "__main__":
    main()
