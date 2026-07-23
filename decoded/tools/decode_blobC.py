import os, sys, struct

path = sys.argv[1] if len(sys.argv) > 1 else "/usr/local/cuda-13.1/nvvm/bin/cicc"
outpath = sys.argv[2] if len(sys.argv) > 2 else "/tmp/rp/cicc_blob_C.bin"
base_vma = 0x50d16a0
length   = 0x19670
# fileoff(v) = v - 0x4d3e7e0 + 0x493d7e0
def fileoff(v):
    return v - 0x4d3e7e0 + 0x493d7e0

start_off = fileoff(base_vma)
print("start file offset:", hex(start_off), "len:", hex(length))

with open(path, "rb") as f:
    f.seek(start_off)
    data = f.read(length)

out = bytearray(len(data))
for i, b in enumerate(data):
    out[i] = b ^ ((3*i) & 0xFF)

os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
with open(outpath, "wb") as f:
    f.write(out)

print("decoded", len(out), "bytes")
print("first 200 bytes repr:")
print(repr(bytes(out[:200])))
