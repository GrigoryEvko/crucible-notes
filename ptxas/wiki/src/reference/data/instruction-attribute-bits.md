# Instruction Attribute Bits (110 used bits)

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

The per-instruction attribute bitmask records boolean modifier presence and small packed fields on
each PTX instruction form. `kind` is one of: `BOOL` (a single modifier flag), `FIELD` (a multi-bit
sub-field — the slash-separated names are the candidate meanings depending on the opcode that owns
the bit), `AMBIG` (the bit is shared by two unrelated opcodes and its meaning depends on which is
decoded), `UNNAMED` / `UNK` (the bit is set on real forms but its semantic name is not recovered).
`confidence` is HIGH for cleanly named booleans, MED for fields, LOW for ambiguous bits.

| bit | attribute | kind | confidence |
|---|---|---|---|

| 0 | BOP | BOOL | HIGH |
| 2 | CMP | BOOL | HIGH |
| 3 | (unnamed) | UNNAMED | - |
| 4 | (unnamed) | UNNAMED | - |
| 5 | RESULT | BOOL | HIGH |
| 6 | RESULTP | BOOL | HIGH |
| 7 | APRX | BOOL | HIGH |
| 8 | RELU | BOOL | HIGH |
| 9 | FTZ | BOOL | HIGH |
| 10 | NOFTZ | BOOL | HIGH |
| 11 | SAT | BOOL | HIGH |
| 12 | SATF | BOOL | HIGH |
| 13 | (unnamed) | UNNAMED | - |
| 14 | (unnamed) | UNNAMED | - |
| 15 | (unnamed) | UNNAMED | - |
| 19 | (unnamed) | UNNAMED | - |
| 22 | VSAT | BOOL | HIGH |
| 23 | CC | BOOL | HIGH |
| 24 | SHAMT | BOOL | HIGH |
| 25 | ROUNDF | BOOL | HIGH |
| 26 | ROUNDI | BOOL | HIGH |
| 27 | SIGNED | BOOL | HIGH |
| 28 | FLOW | BOOL | HIGH |
| 29 | BRANCH | BOOL | HIGH |
| 30 | (used, unlabeled) | UNK | - |
| 32 | DOUBLERES | BOOL | HIGH |
| 33 | LARG | BOOL | HIGH |
| 34 | SREGARG | BOOL | HIGH |
| 35 | MEMSPACE | FIELD | MED |
| 36 | DESC/MEMSPACES/MBARRIER/IM2COL | FIELD | MED |
| 37 | TESTP | BOOL | HIGH |
| 38 | CACHEOP | BOOL | HIGH |
| 39 | ORDER/SCOPE/MEMSPACE | FIELD | MED |
| 40 | PROXYKIND | FIELD | MED |
| 41 | PREFETCHSIZE | FIELD | MED |
| 42 | PREFETCHSIZE/LEVEL | FIELD | MED |
| 43 | EVICTPRIORITY | BOOL | HIGH |
| 44 | ORDER/SCOPE/MEMSPACE | FIELD | MED |
| 45 | (used, unlabeled) | UNK | - |
| 46 | TEXADDR | BOOL | HIGH |
| 47 | TEXMOD | BOOL | HIGH |
| 48 | MULTICAST/PACKEDOFF/MBARRIER/IM2COL | FIELD | MED |
| 49 | MULTICAST/PACKEDOFF/MBARRIER/IM2COL | FIELD | MED |
| 50 | MULTICAST/PACKEDOFF/MBARRIER/IM2COL | FIELD | MED |
| 51 | PACKEDOFF/MBARRIER/IM2COL | FIELD | MED |
| 52 | (unnamed) | UNNAMED | - |
| 53 | (used, unlabeled) | UNK | - |
| 54 | MULTICAST/PACKEDOFF/IM2COL | FIELD | MED |
| 55 | MULTICAST | FIELD | MED |
| 56 | MULTICAST | FIELD | MED |
| 59 | (used, unlabeled) | UNK | - |
| 60 | MULTICAST/PACKEDOFF/DESC/MEMSPACES/MBARRIER/IM2COL | FIELD | MED |
| 63 | COMPMOD | BOOL | HIGH |
| 64 | SURFQ | BOOL | HIGH |
| 65 | SMPLQ | BOOL | HIGH |
| 66 | TEXQ | BOOL | HIGH |
| 67 | VOTE | BOOL | HIGH |
| 68 | ATOMOPF | BOOL | HIGH |
| 69 | ATOMOPI | BOOL | HIGH |
| 70 | ATOMOPB | BOOL | HIGH |
| 71 | ARITHOP | BOOL | HIGH |
| 72 | CAS | BOOL | HIGH |
| 73 | CLAMP | BOOL | HIGH |
| 74 | SHR\|VMAD | AMBIG | LOW |
| 75 | SHR\|VMAD | AMBIG | LOW |
| 76 | PRMT | BOOL | HIGH |
| 77 | SHFL | BOOL | HIGH |
| 78 | (unnamed) | UNNAMED | - |
| 81 | ALIGN/SYNC | FIELD | MED |
| 82 | NOINC | BOOL | HIGH |
| 83 | NOCOMPLETE | BOOL | HIGH |
| 84 | SHAREDSCOPE | BOOL | HIGH |
| 85 | BAR | BOOL | HIGH |
| 86 | ALIGN/SYNC | FIELD | MED |
| 87 | (unnamed) | UNNAMED | - |
| 90 | SHAPE/VECTORIZABLE | FIELD | MED |
| 91 | CACHEPREFETCH/PREFETCHSIZE/CACHEHINT | FIELD | MED |
| 92 | PREFETCHSIZE | FIELD | MED |
| 93 | TRANS | BOOL | HIGH |
| 94 | NUM | BOOL | HIGH |
| 95 | SEQ | BOOL | HIGH |
| 96 | GROUP | BOOL | HIGH |
| 97 | (unnamed) | UNNAMED | - |
| 98 | EXPAND | BOOL | HIGH |
| 99 | THREADGROUP | BOOL | HIGH |
| 100 | SPARSITY | BOOL | HIGH |
| 101 | SPFORMAT | BOOL | HIGH |
| 104 | (used, unlabeled) | UNK | - |
| 105 | ABS\|NANMODE\|XORSIGN | AMBIG | LOW |
| 106 | ABS\|NANMODE\|XORSIGN | AMBIG | LOW |
| 107 | TRANSA | BOOL | HIGH |
| 108 | SHAPE/IGNOREC | FIELD | MED |
| 109 | NEGB | BOOL | HIGH |
| 110 | SHAPE/IGNOREC | FIELD | MED |
| 111 | SHAPE/IGNOREC | FIELD | MED |
| 112 | (unnamed) | UNNAMED | - |
| 114 | ABS\|NANMODE\|XORSIGN | AMBIG | LOW |
| 115 | MULTICAST/CACHEHINT/PACKEDOFF/DESC/MEMSPACES/MBARRIER/IM2COL | FIELD | MED |
| 116 | OOB | BOOL | HIGH |
| 117 | PROXYKIND | FIELD | MED |
| 118 | (used, unlabeled) | UNK | - |
| 119 | (unnamed) | UNNAMED | - |
| 120 | (used, unlabeled) | UNK | - |
| 121 | (unnamed) | UNNAMED | - |
| 122 | (unnamed) | UNNAMED | - |
| 123 | (unnamed) | UNNAMED | - |
| 124 | (unnamed) | UNNAMED | - |
| 125 | (unnamed) | UNNAMED | - |
| 126 | (unnamed) | UNNAMED | - |
| 127 | (unnamed) | UNNAMED | - |
