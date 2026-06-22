#!/usr/bin/env python3
"""
Extract ptxas's binary-derived SASS scheduling model:

  (1) the static per-scheduling-class descriptor table  (.rodata VMA 0x2297C00)
  (2) the per-Ori-opcode scalar latency oracle          (built by sub_738E20)

Everything here is recovered purely from the ptxas binary (CUDA 13.0.88).
No external machine-description source is consulted.

Usage:
    python3 extract_sched_tables.py /path/to/ptxas_rodata.bin
"""
import struct, sys

RODATA_BASE_VMA = 0x1CE2E00          # .rodata section VMA (binary-layout.md)
SCHED_TBL_VMA   = 0x2297C00          # static scheduling-class descriptor table
STRIDE          = 72                 # bytes per class record
N_CLASSES       = 256                # strictly-increasing unit_id run 2..771

def extract_class_table(rodata: bytes):
    off = SCHED_TBL_VMA - RODATA_BASE_VMA
    rows = []
    for i in range(N_CLASSES):
        r = rodata[off + i*STRIDE : off + (i+1)*STRIDE]
        uid   = struct.unpack_from('<I', r, 0)[0]     # +0  scheduling class id
        maskA = r[8:16]                               # +8  pipe-eligibility bytes
        maskB = r[16:24]                              # +16 dual-issue-eligibility bytes
        params= list(struct.unpack_from('<12I', r, 24))  # +24 twelve sched params
        rows.append((uid, maskA, maskB, params))
    return rows

# Binary-derived per-opcode scalar latency oracle (sub_738E20, sm_9x / Blackwell
# family). The constructor switches on the Ori opcode id (index into the
# oracle+744 DWORD array, read back by sub_8BF3A0). Per-band Ori-opcode sets:
ORACLE_BANDS = {
    300: {16, 223, 228},                                   # long global-memory miss
     24: {17, 42, 53, 55, 91, 183, 195},                   # SFU / slow-MIO
     13: {38, 59, 60, 62, 67, 78, 79, 106, 162, 180,
          182, 192, 194, 199, 215, 221},                   # medium / control / MIO
     30: {88, 89},                                          # shared / atomic (LDS/STS class)
}
ORACLE_DEFAULT_ALU = 6     # default for non-memory opcodes
ORACLE_DEFAULT_MEM = 300   # default when the opcode's property bit 0x40 (memory) is set
ORACLE_SCOREBOARD  = 5     # oracle+2212[op] = 5 when property bit 0x02 (decoupled) is set
ORACLE_N_OPCODES   = 355   # loop runs opcode 0..354

def opcode_latency(op: int, is_memory: bool) -> int:
    for lat, ops in ORACLE_BANDS.items():
        if op in ops:
            return lat
    return ORACLE_DEFAULT_MEM if is_memory else ORACLE_DEFAULT_ALU

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'ptxas_rodata.bin'
    rodata = open(path, 'rb').read()
    rows = extract_class_table(rodata)
    print("# ptxas static scheduling-class descriptor table  (.rodata VMA 0x%X)" % SCHED_TBL_VMA)
    print("# %d classes, %d-byte stride; binary-derived (CUDA 13.0.88)" % (len(rows), STRIDE))
    print("# columns: class  pipeA(hex)  pipeB(hex)  p0..p11")
    for uid, mA, mB, p in rows:
        print("%4d  %s  %s  %s" % (uid, mA.hex(), mB.hex(), ' '.join(map(str, p))))

if __name__ == '__main__':
    main()
