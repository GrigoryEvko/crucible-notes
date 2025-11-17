# NVPTX Assign Valid Global Names

**Pass Type**: Symbol naming and linkage pass
**LLVM Class**: `llvm::NVPTXAssignValidGlobalNames`
**Category**: Code Generation / Symbol Management
**Extracted From**: CICC decompiled code (80,281 C files)
**Analysis Quality**: MEDIUM - Inferred from name mangling patterns
**Pass Index**: Listed in optimization_pass_mapping.json

---

## Overview

NVPTXAssignValidGlobalNames ensures all global symbols (functions, variables, constants) have valid names compatible with PTX syntax and CUDA linking requirements. This pass:
- Sanitizes symbol names to meet PTX naming constraints
- Handles C++ name mangling for device code
- Ensures uniqueness of symbol names
- Manages visibility and linkage attributes

**Key Purpose**: Transform LLVM IR symbols into valid PTX identifiers before code emission.

---

## PTX Naming Requirements

### Valid PTX Identifiers

**PTX Specification** (Section 4.1):
- Must start with: `[a-zA-Z_$%]`
- May contain: `[a-zA-Z0-9_$]`
- Cannot be PTX reserved keywords
- Maximum length: typically 1024 characters
- Case-sensitive

**Examples**:
```ptx
.visible .func kernel_main();          ✓ Valid
.visible .func _Z6kernelv();           ✓ Valid (mangled C++)
.func (.param .u32 r) $func$0();       ✓ Valid (compiler-generated)

.func 123invalid();                    ✗ Invalid (starts with digit)
.func ke rnel();                       ✗ Invalid (contains space)
.func for();                           ✗ Invalid (PTX keyword)
```

---

## Symbol Categories

### 1. User-Defined Functions

**CUDA Source**:
```cuda
__global__ void myKernel() { }
__device__ int helper(int x) { }
```

**LLVM IR**:
```llvm
define void @myKernel() #0 { }
define i32 @helper(i32 %x) #1 { }
```

**PTX**:
```ptx
.visible .entry myKernel() { }
.func (.param .u32 r) helper(.param .u32 x) { }
```

**No changes needed** - already valid names.

### 2. C++ Mangled Names

**CUDA Source**:
```cuda
namespace ns {
    template<typename T>
    __device__ T add(T a, T b) { return a + b; }
}

__global__ void kernel() {
    int x = ns::add<int>(1, 2);
}
```

**LLVM IR**:
```llvm
define i32 @_ZN2ns3addIiEET_S1_S1_(i32 %a, i32 %b) { }
```

**PTX**:
```ptx
.func (.param .u32 r) _ZN2ns3addIiEET_S1_S1_(
    .param .u32 a,
    .param .u32 b
) { }
```

**Already valid** - Itanium C++ mangling uses valid PTX characters.

### 3. Compiler-Generated Symbols

**LLVM IR** (intermediate):
```llvm
define internal void @.omp_outlined.() { }
define internal i32 @llvm.global_ctors.1() { }
```

**Must be sanitized**:
```ptx
.func $omp_outlined$0() { }
.func $llvm$global_ctors$1() { }
```

**Transformation**: Replace `.` with `$`, ensure valid start character.

### 4. Global Variables

**CUDA Source**:
```cuda
__device__ int globalVar = 42;
__constant__ float constants[256];
```

**LLVM IR**:
```llvm
@globalVar = addrspace(1) global i32 42
@constants = addrspace(4) global [256 x float]
```

**PTX**:
```ptx
.global .u32 globalVar = 42;
.const .align 4 .f32 constants[256];
```

**Valid names maintained**.

---

## Algorithm

### Phase 1: Collect All Symbols

```
GlobalSymbols = {}

FOR each GlobalValue GV in Module:
    Name = GV.getName()
    Kind = getKind(GV)  // function, variable, constant
    Linkage = GV.getLinkage()
    GlobalSymbols.add({GV, Name, Kind, Linkage})
```

### Phase 2: Validate and Sanitize Names

```
FOR each Symbol S in GlobalSymbols:
    OrigName = S.Name

    // Check validity
    IF NOT isValidPTXName(OrigName):
        NewName = sanitizeName(OrigName)
        S.Name = NewName
        RenameMap[OrigName] = NewName
```

**Sanitization Rules**:
```cpp
string sanitizeName(string name) {
    string result;

    // Ensure valid first character
    if (!isValidPTXStartChar(name[0])) {
        result = "$" + name;  // Prefix with $
    } else {
        result = name;
    }

    // Replace invalid characters
    for (char c : result) {
        if (!isValidPTXChar(c)) {
            c = '_';  // Replace with underscore
        }
    }

    // Check for reserved keywords
    if (isPTXKeyword(result)) {
        result = "$" + result;
    }

    return result;
}
```

### Phase 3: Ensure Uniqueness

```
UsedNames = Set{}

FOR each Symbol S in GlobalSymbols:
    Name = S.Name

    // Check for collisions
    IF Name in UsedNames:
        Suffix = 0
        WHILE (Name + "$" + Suffix) in UsedNames:
            Suffix += 1
        S.Name = Name + "$" + Suffix

    UsedNames.add(S.Name)
```

### Phase 4: Update References

```
FOR each Function F in Module:
    FOR each Instruction I in F:
        IF I references GlobalValue:
            OldName = I.getOperand().getName()
            IF OldName in RenameMap:
                I.setOperand(RenameMap[OldName])
```

---

## Transformation Examples

### Example 1: Invalid Start Character

**Before**:
```llvm
define internal void @0sanitize_me() {
  ret void
}
```

**After**:
```llvm
define internal void @$0sanitize_me() {
  ret void
}
```

**PTX**:
```ptx
.func $0sanitize_me() {
    ret;
}
```

### Example 2: Invalid Characters

**Before**:
```llvm
@my.global.var = global i32 0
```

**After**:
```llvm
@my_global_var = global i32 0
```

**PTX**:
```ptx
.global .u32 my_global_var = 0;
```

### Example 3: Reserved Keyword Collision

**Before**:
```llvm
define void @add() {  ; 'add' might conflict with PTX instruction
  ret void
}
```

**After** (if collision detected):
```llvm
define void @$add() {
  ret void
}
```

**PTX**:
```ptx
.func $add() {
    ret;
}
```

### Example 4: Name Collision After Sanitization

**Before**:
```llvm
define void @func.1() { }
define void @func$1() { }  ; Would collide after sanitizing func.1
```

**After**:
```llvm
define void @func$1() { }     ; First
define void @func$1$0() { }   ; Renamed to avoid collision
```

**PTX**:
```ptx
.func func$1() { }
.func func$1$0() { }
```

---

## Special Cases

### C++ Template Instantiations

**LLVM IR**:
```llvm
@_ZN6kernel3runILi32EEEvv = alias void (), void ()* @_ZN6kernel3runILi32EEEvv.1
```

**PTX** (unchanged - already valid):
```ptx
.func _ZN6kernel3runILi32EEEvv() { }
```

**Note**: Itanium mangling is PTX-safe.

### Weak Symbols

**LLVM IR**:
```llvm
@weakVar = weak global i32 0
```

**PTX**:
```ptx
.weak .global .u32 weakVar = 0;
```

**Name unchanged**, but linkage attribute added.

### External Symbols

**LLVM IR**:
```llvm
declare void @externalFunc()
```

**PTX**:
```ptx
.extern .func externalFunc();
```

**Name unchanged** - must match external definition.

---

## Visibility and Linkage

### PTX Linkage Attributes

| LLVM Linkage | PTX Attribute | Visibility |
|--------------|---------------|------------|
| `external` | `.visible` | Device-wide |
| `internal` | (none) | File-local |
| `weak` | `.weak` | Device-wide, overridable |
| `linkonce` | `.weak` | Link-time selection |
| `private` | (none) | Function-local |

**Example**:
```llvm
; LLVM IR
define internal void @localFunc() { }
define void @publicFunc() { }
define weak void @weakFunc() { }
```

```ptx
// PTX
.func localFunc() { }          // Internal
.visible .func publicFunc() { } // External
.weak .func weakFunc() { }      // Weak
```

---

## Performance Impact

### Compile Time

**Overhead**: Minimal (< 0.1% of total compilation time)
- Simple string operations
- Linear pass through symbols

### Runtime Impact

**None**: Name changes are purely cosmetic for linking.

**Debugger Impact**: Sanitized names appear in debuggers
- May be less readable than original
- But necessary for correctness

---

## Interaction with Other Passes

### Run After

1. **Inlining**: Reduces number of functions
2. **GlobalDCE**: Removes unused globals
3. **GlobalOpt**: Optimizes global variables
4. **All IR transformations**: Must be final before PTX emission

### Run Before

1. **PTXAsmPrinter**: Emits final PTX code
2. **Debug Info Emission**: Needs final symbol names
3. **Linking**: Symbol names must be stable

### Preserved

**All optimizations**: This pass only renames, doesn't change semantics.

---

## CUDA Developer Considerations

### Avoid Problematic Names

**Recommendation**: Use valid C/C++ identifiers

```cuda
// Good
__device__ void my_function() { }
__device__ int variable_name;

// Avoid (though compiler will fix)
// Cannot use: spaces, special characters
```

### Debugging with Mangled Names

**Use `cu++filt`** to demangle names:
```bash
nvdisasm kernel.cubin | c++filt

# _ZN2ns3addIiEET_S1_S1_ → ns::add<int>(int, int)
```

### Extern "C" for Simple Names

**Force C linkage** to avoid mangling:
```cuda
extern "C" __device__ void simpleFunc() {
    // PTX: .func simpleFunc() - no mangling
}
```

---

## Debugging

### View PTX Symbol Names

```bash
nvcc -ptx -o kernel.ptx kernel.cu
grep "\.func\|\.entry\|\.global" kernel.ptx
```

**Output**:
```ptx
.visible .entry myKernel()
.func (.param .u32 r) _ZN2ns6helperEi(.param .u32 p0)
.global .u32 globalVar
```

### Check for Renaming

**Compare LLVM IR vs PTX**:
```bash
# Generate IR
nvcc -Xclang -emit-llvm -c kernel.cu -o kernel.bc
llvm-dis kernel.bc -o kernel.ll

# Generate PTX
nvcc -ptx kernel.cu -o kernel.ptx

# Compare symbol names
```

---

## Related Passes

1. **NVPTXGenericToNVVM**: Transforms intrinsics (may create new symbols)
2. **GlobalDCE**: Removes unused globals before naming
3. **PTXAsmPrinter**: Consumes final valid names
4. **DebugInfoEmission**: Uses symbol names for debug info

---

## Summary

NVPTXAssignValidGlobalNames is a critical code generation pass that:
- ✓ Sanitizes all symbol names for PTX compatibility
- ✓ Ensures uniqueness of symbol names
- ✓ Handles C++ name mangling correctly
- ✓ Manages linkage and visibility attributes
- ✓ Prepares module for final PTX emission

**Critical for**: PTX code generation, linking, debugging
**Performance Impact**: None (naming only)
**Reliability**: Essential for correctness - invalid names cause link errors

**Key Insight**: PTX has strict naming requirements - this pass ensures all LLVM symbols are transformed into valid PTX identifiers before emission.
