# Diagnostic Message Catalog (506 entries)

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

Every diagnostic string ptxas can emit, recovered from its message tables. Each entry has a table
name, an in-table id, a numeric severity code, the decoded severity, and the format string. `%s`
/ `%d` placeholders are filled at emit time from the failing `(SM, ISA, instruction, modifier)`
tuple or the offending token.

Severity distribution: error=341, fatal=103, info=13, warning=49.
Tables: link=41, main=465.

| table | id | sev | severity | message |
|---|---|---|---|---|

| main | 0 | 6 | fatal | Memory allocation failure |
| main | 1 | 6 | fatal | Stray '"' character |
| main | 2 | 6 | fatal | Stray '[' character |
| main | 3 | 6 | fatal | Stray '' character |
| main | 4 | 6 | fatal | Could not open output file '%s' |
| main | 5 | 6 | fatal | Could not open input file '%s' |
| main | 6 | 6 | fatal | Memory allocation failure |
| main | 7 | 6 | fatal | Unexpected non-ASCII character encountered on line %u |
| main | 8 | 6 | fatal | Unexpected EOF encountered on line %u |
| main | 9 | 5 | error | Non-generic address is not supported as operand for %s instruction |
| main | 10 | 5 | error | Illegal operand type for address/array-index |
| main | 11 | 5 | error | Instruction '%s' is not yet implemented %s '%s' |
| main | 12 | 5 | error | Only %s vector modifier supported with type %s on %s operation for %s |
| main | 13 | 6 | fatal | Unsupported Function '%s' on arch '%s' or higher |
| main | 14 | 5 | error | '%s' syntax expected for instruction '%s' |
| main | 15 | 5 | error | Argument %d of instruction '%s': value '%d' must be %s the value '%d' of argument %d |
| main | 16 | 5 | error | Unsupported modifier '%s' for instruction '%s' |
| main | 17 | 5 | error | Unsupported modifier '%s' %s for instruction '%s' |
| main | 18 | 5 | error | Unexpected number of '%s' modifiers specified |
| main | 19 | 6 | fatal | No variable storage specified |
| main | 20 | 5 | error | Number of threads participating in barrier must be in multiple of warp size |
| main | 21 | 5 | error | Call to '%s' requires call prototype |
| main | 22 | 2 | info | Potential out of bound access while accessing symbol '%s' of type '%s' with instruction type '%s'. |
| main | 23 | 5 | error | Illegal to use symbol '%s' of type %s as %s. Use %s only. |
| main | 24 | 5 | error | Invalid combination of '%s' and '%s' for instruction '%s' %s |
| main | 25 | 6 | fatal | Number of %s arguments (%d) to inline function '%s' crosses the limit %d. Consider increasing the limit |
| main | 26 | 5 | error | Argument %d of instruction '%s': Maximum of %d-bits are expected to be set |
| main | 27 | 5 | error | Invalid reserved shared memory variable %s |
| main | 28 | 5 | error | Registers 0-3 are reserved by ABI and cannot be used for %s |
| main | 29 | 5 | error | Minimum parameter register %d and number of parameter registers %d together overflows maximum register limit %d for %s |
| main | 30 | 5 | error | Unexpected number of arguments specified for pragma %s |
| main | 31 | 3 | warning | Unknown pragma '%s'. Ignoring pragma |
| main | 32 | 5 | error | Cannot define alias to variable object '%s' |
| main | 33 | 5 | error | Function '%s' with %s scope cannot be aliased |
| main | 34 | 5 | error | Mismatch in prototype of alias '%s' and aliasee '%s'. %s differ(s) |
| main | 35 | 5 | error | '%s' is already aliased to '%s' and cannot be redefined |
| main | 36 | 5 | error | Cannot alias a function to itself |
| main | 37 | 5 | error | Cannot define alias to entry function '%s' |
| main | 38 | 5 | error | Illegal redefinition of function '%s' as alias |
| main | 39 | 5 | error | Modifier '%s' on instruction '%s' %s is not supported starting %s and later architectures |
| main | 40 | 2 | info | Advisory: Modifier '%s' should be used on instruction '%s' instead of modifier '%s' as it is expected to have substantially reduced performance on some future architectures |
| main | 41 | 3 | warning | Advisory: '%s' modifier on instruction '%s' should be used on .target '%s' instead of .target '%s' as this feature is expected to have substantially reduced performance on some future architectures |
| main | 42 | 3 | warning | '%s' modifier on instruction %s is deprecated since PTX ISA version %s and will be discontinued from future PTX ISA version |
| main | 43 | 3 | warning | '%s' modifier on instruction %s may produce unexpected results on %s and later architectures |
| main | 44 | 5 | error | Instruction '%s' with '%s' is not supported starting from PTX ISA version %s |
| main | 45 | 5 | error | Instruction '%s' without '.sync' is not supported on .target sm_70 and higher from PTX ISA version 6.4 |
| main | 46 | 3 | warning | Instruction '%s' without '.sync' is deprecated since PTX ISA version 6.0 and will be discontinued in a future PTX ISA version |
| main | 47 | 3 | warning | Instruction '%s' without '.sync' may produce unpredictable results on %s and later architectures |
| main | 48 | 5 | error | Argument(s) to pragma '%s' cannot be empty |
| main | 49 | 5 | error | Attribute '%s' specified multiple times in function |
| main | 50 | 5 | error | Pragma '%s' is allowed only within function scope |
| main | 51 | 5 | error | Pragma '%s' cannot be specified for device functions |
| main | 52 | 5 | error | Pragma '%s' cannot be specified for function '%s' |
| main | 53 | 5 | error | local_maxnreg specified multiple times in function '%s' |
| main | 54 | 5 | error | Pragma '%s' specified multiple times in function '%s' |
| main | 55 | 5 | error | Invalid range '%s' specified for .pragma '%s' |
| main | 56 | 5 | error | Invalid value '%s' specified for .pragma '%s' |
| main | 57 | 5 | error | Illegal %clayout '%s' for instruction '%s' |
| main | 58 | 5 | error | Feature '%s' cannot be compiled for architecture '%s' |
| main | 59 | 5 | error | Instruction '%s' cannot be compiled for architecture '%s' |
| main | 60 | 5 | error | Feature '%s' not supported on .target '%s' |
| main | 61 | 5 | error | Instruction '%s' not supported on .target '%s' |
| main | 62 | 5 | error | Illegal matrix shape '%s' for instruction '%s' with '%s' modifier |
| main | 63 | 5 | error | Illegal matrix shape '%s' for instruction '%s' |
| main | 64 | 5 | error | Modifier '%s' requires '%s' space for instruction '%s' |
| main | 65 | 5 | error | %s must be in '%s' space |
| main | 66 | 5 | error | Only the last input parameter can be an incomplete array. |
| main | 67 | 5 | error | %s cannot be an incomplete array. |
| main | 68 | 3 | warning | Program is doing double precision computations. |
| main | 69 | 5 | error | Undefined metadata index '%d' |
| main | 70 | 5 | error | Duplicate metadata node with index '%d' |
| main | 71 | 5 | error | Illegal use of attribute '%s' for instruction '%s' |
| main | 72 | 5 | error | Illegal to predicate instruction '%s' with operand '%s' |
| main | 73 | 5 | error | Illegal attribute '%s' for symbol '%s' |
| main | 74 | 5 | error | Illegal operand type to instruction '%s' |
| main | 75 | 3 | warning | %s |
| main | 76 | 3 | warning | Double is not supported. Demoting to float |
| main | 77 | 5 | error | Modifier '%s' requires .target %s or higher |
| main | 78 | 5 | error | Modifier '%s' requires PTX ISA .version %s or later |
| main | 79 | 5 | error | Feature '%s' requires .target %s or higher |
| main | 80 | 5 | error | Modifier '%s' on '%s' requires .target %s or higher |
| main | 81 | 5 | error | Modifier '%s' on '%s' requires PTX ISA .version %s or later |
| main | 82 | 5 | error | Feature '%s' requires PTX ISA .version %s or later |
| main | 83 | 5 | error | Instruction '%s%s%s%s' requires PTX ISA .version %d.%d or later |
| main | 84 | 5 | error | Instruction '%s%s%s%s' requires .target sm_%d or higher |
| main | 85 | 5 | error | Instruction '%s' requires .target %s or higher |
| main | 86 | 5 | error | Undefined preprocessor macro '%s' |
| main | 87 | 6 | fatal | Non-terminated preprocessor macro |
| main | 88 | 6 | fatal | Missing .ENDMACRO |
| main | 89 | 6 | fatal | Missing .ENDIF |
| main | 90 | 6 | fatal | Ill formed conditional |
| main | 91 | 5 | error | Too many operands for macro '%s' |
| main | 92 | 5 | error | Formal parameter '%s' is also the name of a reserved macro parameter. |
| main | 93 | 5 | error | Formal parameter names must be all different. |
| main | 94 | 5 | error | Deprecated feature: '%s' not supported as of PTX version %s |
| main | 95 | 5 | error | Unimplemented feature: %s |
| main | 96 | 5 | error | Source location %d %d %d used in inlined_at directive must be used in a preceding .loc directive |
| main | 97 | 5 | error | Debug information not found in presence of .target debug |
| main | 98 | 5 | error | %s directive(s) required for directive '%s' |
| main | 99 | 5 | error | Conflicting directives: %s |
| main | 100 | 5 | error | Directive map_f64_to_f32 is not supported with SM 1.3 or higher |
| main | 101 | 5 | error | Instruction '%s' requires SM 1.3 or higher, or map_f64_to_f32 directive |
| main | 102 | 6 | fatal | Missing .target directive at start of file '%s' |
| main | 103 | 6 | fatal | Target architecture not defined at start of file '%s' |
| main | 104 | 5 | error | Conflicting .target option '%s' |
| main | 105 | 6 | fatal | Unsupported .target '%s' |
| main | 106 | 6 | fatal | Missing .version directive at start of file '%s' |
| main | 107 | 6 | fatal | Unsupported .version %s; current version is '%s' |
| main | 108 | 5 | error | Instruction '%s' applies only to signed numeric types |
| main | 109 | 5 | error | Read-only special register cannot be destination argument |
| main | 110 | 5 | error | Special register argument not allowed for instruction '%s' |
| main | 111 | 5 | error | Module-scoped variables in %s state space are not allowed with ABI |
| main | 112 | 5 | error | Undefined file index #%d |
| main | 113 | 5 | error | Duplicate file index #%d |
| main | 114 | 5 | error | Unable to infer type of vector elements (assuming .b8) |
| main | 115 | 5 | error | Incompatible elements of vector expression |
| main | 116 | 2 | info | Field '%s' is ignored in .texref variables in independent texture mode |
| main | 117 | 6 | fatal | Unrecognized field name '%s' in opaque struct initializer |
| main | 118 | 6 | fatal | Too many elements in opaque struct initializer |
| main | 119 | 5 | error | Opaque structure initializer requires named fields |
| main | 120 | 6 | fatal | Greater number of elements in array initializer |
| main | 121 | 6 | fatal | Incorrect number of elements in vector initializer |
| main | 122 | 6 | fatal | Variable used as initial value not in .global or .const state space |
| main | 123 | 6 | fatal | Expected initializer for symbol '%s'. |
| main | 124 | 6 | fatal | Invalid initial value expression |
| main | 125 | 5 | error | Invalid mask value '0x%llx' used in initializer |
| main | 126 | 6 | fatal | Invalid initial value symbol '%s' |
| main | 127 | 5 | error | Unrecognized initial value symbol '%s' |
| main | 128 | 6 | fatal | Initial value type mismatch |
| main | 129 | 5 | error | Directive '%s' is supported only for '%s' functions |
| main | 130 | 5 | error | Invalid directive '%s' for function '%s' |
| main | 131 | 5 | error | Operand negation not allowed for instruction '%s' |
| main | 132 | 5 | error | Floating-point coordinates required |
| main | 133 | 5 | error | Integer coordinates require .s32 instruction type |
| main | 134 | 5 | error | Illegal type for %s |
| main | 135 | 5 | error | Value out of range for type .b%d %lld |
| main | 136 | 5 | error | Positive non-zero value expected for %s |
| main | 137 | 5 | error | Barrier number '%llu' out of range, expected to be in range [0..15] |
| main | 138 | 5 | error | Instruction '%s' requires .v%d type |
| main | 139 | 5 | error | Operand vector size doesn't match geometry for instruction '%s' |
| main | 140 | 5 | error | Illegal query modifier for instruction '%s' |
| main | 141 | 6 | fatal | No initial value is allowed for .f16 type |
| main | 142 | 5 | error | No initial value is allowed for external symbol '%s' |
| main | 143 | 5 | error | No initial value is allowed for '%s' in this state space |
| main | 144 | 5 | error | Unexpected number of state spaces specified in instruction '%s' |
| main | 145 | 5 | error | Multiple state spaces specified |
| main | 146 | 5 | error | %s %d is invalid for %s |
| main | 147 | 5 | error | Alignment value too large, %s |
| main | 148 | 5 | error | Argument %d of '%s' instruction: value must be %s |
| main | 149 | 5 | error | Alignment must be a power of two |
| main | 150 | 5 | error | Argument %d of instruction '%s' : value '%lld' expected to be a non-zero positive |
| main | 151 | 5 | error | Instruction '%s' requires a non-zero thread count argument |
| main | 152 | 5 | error | Membar level required for instruction '%s' |
| main | 153 | 5 | error | Declarations not allowed in .sreg space |
| main | 154 | 5 | error | %s state space expected for feature '%s' |
| main | 155 | 5 | error | State space incorrect for feature '%s' |
| main | 156 | 5 | error | State space mismatch between instruction and address in instruction '%s' |
| main | 157 | 5 | error | State space incorrect for instruction '%s', expected '%s' |
| main | 158 | 5 | error | State space incorrect for instruction '%s' |
| main | 159 | 5 | error | No state space qualifier expected for instruction '%s' |
| main | 160 | 5 | error | Sink '_' is expected as a destination operand for instruction '%s' with '%s' |
| main | 161 | 6 | fatal | Result discard mode is not allowed for instruction '%s' |
| main | 162 | 5 | error | Sink '_' not allowed as a destination operand for instruction '%s' |
| main | 163 | 5 | error | Sink '_' not allowed as a source operand |
| main | 164 | 5 | error | Illegal state space for address operand '%s' with instruction '%s' |
| main | 165 | 5 | error | Illegal rounding modifier for instruction '%s' |
| main | 166 | 5 | error | Incorrect '%s' modifier specified for instruction '%s' |
| main | 167 | 5 | error | Illegal modifier '%s' for instruction '%s' with '%s' |
| main | 168 | 5 | error | Illegal modifier '%s' for instruction '%s' with type '%s' |
| main | 169 | 5 | error | Illegal modifier '%s' for instruction '%s', for arch '%s' |
| main | 170 | 5 | error | Illegal modifier '%s' for instruction '%s' |
| main | 171 | 5 | error | Unknown modifier '%s' |
| main | 172 | 5 | error | Multiple %s modifiers specified |
| main | 173 | 5 | error | Duplicate '%s' modifier specified for instruction '%s' |
| main | 174 | 5 | error | Duplicate %s modifier |
| main | 175 | 5 | error | Argument %d of instruction '%s': unexpected value '%lld' when '%s' and '%s' |
| main | 176 | 5 | error | Argument %d of instruction '%s': unexpected value '%lld', expected to be %s |
| main | 177 | 5 | error | Argument %d of instruction '%s': unexpected value '%lld', expected to be multiple of %d |
| main | 178 | 5 | error | Argument %d of instruction '%s': value '%lld' expected to be in range [%d..%d] when %s |
| main | 179 | 5 | error | Argument %d of instruction '%s': value '%f' out of range, expected to be in range (%f .. %f] |
| main | 180 | 5 | error | Argument %d of instruction '%s': value '%lld' out of range, expected to be in range [%lld..%lld] |
| main | 181 | 5 | error | Argument %d of instruction '%s': value '%d' out of range, expected to be in range [%d..%d] |
| main | 182 | 5 | error | Argument %d of instruction '%s': must be %d-bit size |
| main | 183 | 5 | error | Argument %d of instruction '%s': must be constant |
| main | 184 | 5 | error | Argument %d of instruction '%s': must be register or constant |
| main | 185 | 5 | error | Indirect access to texture requires unified texture mode |
| main | 186 | 5 | error | Indirect access to surface requires .u64 type |
| main | 187 | 5 | error | Indirect access to sampler requires .u64 type |
| main | 188 | 5 | error | Indirect access to texture requires .u64 type |
| main | 189 | 5 | error | Instruction or declaration violates .target texmode_%s setting |
| main | 190 | 5 | error | Argument %d of instruction '%s': value of mask should be between 1 and 127 |
| main | 191 | 5 | error | Argument %d of instruction '%s': vector size should be equal to number of bits set in mask |
| main | 192 | 5 | error | Argument %d of instruction '%s': duplicate elements are not allowed in destination vector |
| main | 193 | 5 | error | Argument %d of instruction '%s': .samplerref or .u64 register expected |
| main | 194 | 5 | error | Argument %d of instruction '%s': .surfref or .u64 register expected |
| main | 195 | 5 | error | Argument %d of instruction '%s': .texref or .u64 register expected |
| main | 196 | 5 | error | Argument %d of instruction '%s': .samplerref expected |
| main | 197 | 5 | error | Argument %d of instruction '%s': must be register |
| main | 198 | 5 | error | Result register required for instruction '%s' |
| main | 199 | 5 | error | Instruction '%s' requires .u32 or .u64 type |
| main | 200 | 5 | error | Instruction '%s' requires '%s' type with '%s' modifier. |
| main | 201 | 5 | error | Instruction '%s' requires '%s' type |
| main | 202 | 5 | error | Incorrect type '%s' for operation '%s' in instruction '%s' |
| main | 203 | 5 | error | Operation requires .pred type |
| main | 204 | 5 | error | Operation requires .u32 type |
| main | 205 | 5 | error | Operation %s requires %s type for instruction '%s' |
| main | 206 | 5 | error | Illegal operation '%s' for instruction '%s' |
| main | 207 | 5 | error | Reduction operation is required for instruction '%s' |
| main | 208 | 5 | error | Illegal reduction operation for instruction '%s%s%s' |
| main | 209 | 5 | error | Boolean operation is required for instruction '%s' |
| main | 210 | 5 | error | Comparison qualifier is not allowed for instruction '%s' |
| main | 211 | 5 | error | Mismatch between instruction type and comparison qualifier |
| main | 212 | 5 | error | Vector qualifier is not allowed for instruction '%s' |
| main | 213 | 5 | error | Illegal argument to predicate negation |
| main | 214 | 5 | error | Illegal argument to unary minus |
| main | 215 | 5 | error | Operand negation limited to either (source A xor source B) or source C |
| main | 216 | 5 | error | Illegal combination of .sat and .add modifiers |
| main | 217 | 5 | error | Illegal secondary operation for video instruction '%s' |
| main | 218 | 5 | error | Video selector is not allowed on 'source C' operand |
| main | 219 | 5 | error | Video selector is not allowed on source operand for instruction '%s' |
| main | 220 | 5 | error | Video selector is not allowed on destination for instruction '%s' |
| main | 221 | 5 | error | Video instruction cannot have destination selector and secondary operation |
| main | 222 | 5 | error | Unexpected Byte/Word selector when '%s' and '%s' |
| main | 223 | 5 | error | Incompatible Byte/Word selector when '%s' and '%s' |
| main | 224 | 5 | error | Byte/Word selector is required for operand %d of instruction '%s' |
| main | 225 | 5 | error | Argument %d of instruction '%s': Argument vector size mismatch |
| main | 226 | 5 | error | Argument %d of instruction '%s': incompatible Byte/word selector when '%s' and '%s' |
| main | 227 | 5 | error | Byte selector is required for operand %d of instruction '%s' |
| main | 228 | 5 | error | Incorrect byte selector for operand %d of instruction '%s' |
| main | 229 | 5 | error | Incorrect video selector for operand %d |
| main | 230 | 5 | error | Unknown video selector: '%s' |
| main | 231 | 5 | error | Unknown vector selector: '%c' |
| main | 232 | 5 | error | Conflicting use of '.po' modifier with operand negation |
| main | 233 | 5 | error | Multiple instruction post-operation flags set |
| main | 234 | 5 | error | Multiple comparisons set |
| main | 235 | 5 | error | Illegal vector-vector operands for vector-scalar or scalar-vector mov instruction |
| main | 236 | 5 | error | Vector operand is not allowed for instruction '%s' |
| main | 237 | 5 | error | Argument vector size mismatch for instruction '%s' |
| main | 238 | 5 | error | Vector of size %d is expected for argument %d of instruction '%s' |
| main | 239 | 5 | error | Vector is not expected for argument %d of instruction '%s' |
| main | 240 | 5 | error | Vector expected for argument %d |
| main | 241 | 5 | error | Result vector expected |
| main | 242 | 5 | error | Predicate expression expected |
| main | 243 | 5 | error | Modifier '.cc' applies only to %s integer types |
| main | 244 | 5 | error | Modifier '.sat' does not apply to '%s' type |
| main | 245 | 5 | error | Modifier '.ftz' does not apply to this type of convert operation |
| main | 246 | 5 | error | %s modifier required for instruction '%s' with modifier '%s' |
| main | 247 | 5 | error | Modifier '%s' required for instruction '%s' with type '%s' and operation '%s' |
| main | 248 | 5 | error | '%s' modifier required for instruction '%s' with destination type '%s' |
| main | 249 | 5 | error | '%s' modifier required for instruction '%s' with type '%s' |
| main | 250 | 5 | error | %s modifier required for instruction '%s' with %d arguments |
| main | 251 | 5 | error | %s modifier required for instruction '%s%s%s%s' |
| main | 252 | 5 | error | %s modifier required for instruction '%s' |
| main | 253 | 5 | error | Illegal cache operation for instruction 'ld.nc' |
| main | 254 | 5 | error | Modifier '.nc' requires .global space |
| main | 255 | 5 | error | Modifier '%s' requires modifier '%s' for instruction '%s' |
| main | 256 | 5 | error | Modifier '%s' requires modifier '%s' |
| main | 257 | 5 | error | Modifier '%s' requires %s with '%s' instruction |
| main | 258 | 5 | error | Modifier '%s' cannot be combined with modifier '%s' |
| main | 259 | 5 | error | Modifier '%s' cannot be applied to '%s' space for instruction '%s' |
| main | 260 | 5 | error | Modifier '%s' cannot be applied to '%s' space |
| main | 261 | 5 | error | Predicate output expected for instruction '%s' |
| main | 262 | 5 | error | Predicate output not allowed for instruction '%s' |
| main | 263 | 5 | error | Illegal instruction types specified for '%s' with shape '%s' |
| main | 264 | 5 | error | Not a name of any known instruction: '%s' |
| main | 265 | 5 | error | Unexpected number of arguments specified for %s with '%s' |
| main | 266 | 5 | error | Unexpected number of arguments specified for %s with shape '%s' |
| main | 267 | 5 | error | Unexpected number of instruction types specified for %s with shape '%s' |
| main | 268 | 5 | error | Unexpected number of operations specified for %s with shape '%s' |
| main | 269 | 5 | error | Incorrect operations specified for %s with shape '%s' |
| main | 270 | 5 | error | .dtype must be '%s' when .ctype is '%s' for '%s' instruction with shape '%s' |
| main | 271 | 5 | error | Modifier '%s' require for instruction %s with shape '%s' |
| main | 272 | 5 | error | Modifier %s not allowed for instruction %s with shape '%s' |
| main | 273 | 5 | error | Incorrect instruction type specified for %s with shape '%s' |
| main | 274 | 5 | error | Modifier %s not allowed for shape '%s' |
| main | 275 | 5 | error | Inconsistent type '%s' with vector .v%d for '%s' |
| main | 276 | 5 | error | Instruction '%s' supports vector of only %s types |
| main | 277 | 3 | warning | Unexpected instruction types specified for '%s' |
| main | 278 | 5 | error | Unexpected instruction types specified for '%s' |
| main | 279 | 5 | error | Arguments mismatch for instruction '%s' |
| main | 280 | 5 | error | Illegal expression '%s' |
| main | 281 | 5 | error | Unknown symbol '%s' |
| main | 282 | 5 | error | Unknown field '%s' |
| main | 283 | 5 | error | Array indexing on non-array |
| main | 284 | 5 | error | Type %s may only be used as an instruction type |
| main | 285 | 5 | error | Vector type too large, exceeds 128 bit limit |
| main | 286 | 5 | error | Array index in the '%s' instruction has unexpected type. Expecting type .u32 |
| main | 287 | 5 | error | Vector with elements of different types are not allowed in %s instruction |
| main | 288 | 5 | error | Incorrect no. of matrix: %d for %s with shape '%s' |
| main | 289 | 5 | error | Illegal vector size: %d |
| main | 290 | 5 | error | Illegal bank number: %d |
| main | 291 | 5 | error | Constant banks 1..%d are reserved for external incomplete arrays |
| main | 292 | 5 | error | Inconsistent state space for parameter '%s' |
| main | 293 | 5 | error | Formal parameter '%s' declared in .param space cannot be used as argument to call |
| main | 294 | 5 | error | Call argument '%s' has illegal state space |
| main | 295 | 5 | error | Illegal to write to function input parameter '%s' |
| main | 296 | 5 | error | Illegal to read function return parameter '%s' |
| main | 297 | 5 | error | Multiple return parameters require .register state space |
| main | 298 | 5 | error | Function parameter '%s' has illegal state space |
| main | 299 | 5 | error | Illegal parameter name '%s' |
| main | 300 | 5 | error | Entry parameter '%s' must have .param state space |
| main | 301 | 5 | error | Parameter variables must be declared within .entry parameter list |
| main | 302 | 5 | error | Parameter variables may not be declared at module scope |
| main | 303 | 5 | error | Parameter variables must be declared within .entry function |
| main | 304 | 5 | error | %s variables must be declared in .global space |
| main | 305 | 5 | error | Texture/surface variable must be at module or entry parameter scope |
| main | 306 | 5 | error | Texture variables must be declared at module scope |
| main | 307 | 5 | error | Function definition conflicts with '.extern' declaration for function '%s' |
| main | 308 | 5 | error | '.extern' variable '%s' cannot be resolved by a '.static' |
| main | 309 | 5 | error | Inconsistent redefinition of variable '%s' |
| main | 310 | 5 | error | Illegal scope for variable '%s' |
| main | 311 | 5 | error | Immediate addresses allowed only for .local state space |
| main | 312 | 5 | error | Variable '%s' cannot be allocated in .param state space |
| main | 313 | 5 | error | Variable '%s' cannot be allocated into a register |
| main | 314 | 5 | error | Predicate type can only be used to define scalar register variables |
| main | 315 | 5 | error | Predicate variable '%s' must be in register state space |
| main | 316 | 5 | error | Non-external variable '%s' has incomplete type |
| main | 317 | 5 | error | Vector type only allowed over basic types |
| main | 318 | 5 | error | Illegal to take address of parameter '%s' |
| main | 319 | 5 | error | Illegal Addressing Mode in instruction '%s' |
| main | 320 | 5 | error | Address expected for argument %d of instruction '%s' |
| main | 321 | 6 | fatal | .const or .global array expected for argument %d of instruction '%s' |
| main | 322 | 6 | fatal | Label or %s array expected for argument %d of instruction '%s' |
| main | 323 | 5 | error | Label with '.branchtargets' directive expected for argument %d of instruction '%s' |
| main | 324 | 5 | error | Label expected for argument %d of instruction '%s' |
| main | 325 | 6 | fatal | Label expected for forward reference of '%s' |
| main | 326 | 5 | error | Illegal call target, device function expected |
| main | 327 | 5 | error | Illegal branch target, label expected |
| main | 328 | 5 | error | Variable '%s' must be type .u32 or .u64 |
| main | 329 | 5 | error | Array of incomplete type |
| main | 330 | 5 | error | Illegal target for instruction '%s' |
| main | 331 | 5 | error | Illegal argument for formal parameter '%s' |
| main | 332 | 5 | error | Alignment of argument does not match formal parameter '%s' |
| main | 333 | 5 | error | Type of argument does not match formal parameter '%s' |
| main | 334 | 5 | error | Call has wrong number of parameters |
| main | 335 | 6 | fatal | Call target not recognized |
| main | 336 | 5 | error | Function '%s' not declared in this scope |
| main | 337 | 5 | error | Parameter '%s' uses .allocno %d which overlaps with other parameter's .allocno |
| main | 338 | 5 | error | Parameter '%s' uses .allocno %d which is not aligned for type '%s' |
| main | 339 | 5 | error | Parameter '%s' has illegal alignment (must be aligned to 1, 2, 4, 8, 16, 32, 64, or 128 bytes) |
| main | 340 | 5 | error | Function '%s' uses .allocno for some input and return parameters but not all |
| main | 341 | 5 | error | Mismatch between declaration and definition of function '%s', %s differ |
| main | 342 | 5 | error | Inconsistent prototypes of '%s', %s differ |
| main | 343 | 5 | error | Inconsistent redefinition of '%s' as entry and function |
| main | 344 | 6 | fatal | Inconsistent redefinition of symbol '%s' |
| main | 345 | 5 | error | Duplicate definition of macro '%s' |
| main | 346 | 5 | error | Duplicate definition of function '%s' |
| main | 347 | 5 | error | Duplicate definition of variable '%s' |
| main | 348 | 5 | error | Duplicate definition of label '%s' |
| main | 349 | 5 | error | Unsupported cast operation |
| main | 350 | 6 | fatal | Constant expression has division by zero |
| main | 351 | 5 | error | Constant expression type mismatch |
| main | 352 | 5 | error | Integer constant expression expected |
| main | 353 | 5 | error | Constant overflow |
| main | 354 | 5 | error | Entry function '%s' uses too much parameter space (0x%x bytes, 0x%x max). |
| main | 355 | 5 | error | Illegal placement of kernel parameter attribute |
| main | 356 | 5 | error | Illegal value for .address_size directive (must be 32 or 64) |
| main | 357 | 6 | fatal | Error reading obfuscated PTX file |
| main | 358 | 5 | error | Parsing error near '%s': %s |
| main | 359 | 6 | fatal | Parsing error near '%s': %s |
| main | 360 | 6 | fatal | Input file '%s' could not be opened |
| main | 361 | 3 | warning | GNU Jobserver support requested, but no compatible jobserver found. Ignoring '--jobserver' |
| main | 362 | 6 | fatal | Jobserver requested, but an error occurred |
| main | 363 | 5 | error | Function '%s' uses reserved shared memory when not allowed. |
| main | 364 | 6 | fatal | Cancellation of compilation requested through callback function |
| main | 365 | 2 | info | Disabling default position-independent-code(pic) compilation mode as program requires more resources than allowed. |
| main | 366 | 5 | error | Function '%s' uses single CTA(.cta_group::1) and CTA pair granularity(.cta_group::2) and that is not allowed. |
| main | 367 | 5 | error | Function '%s' using feature '%s' and with shape '%s' cannot be compiled with specified register target constraints |
| main | 368 | 3 | warning | Caller and callee expected to have different return address register but '%s' and '%s' both use R%d as return address register |
| main | 369 | 3 | warning | Function '%s' specifies register R%d as scratch register which is used as return address register |
| main | 370 | 3 | warning | Parameter register R%d is not marked as scratch for function '%s' |
| main | 371 | 3 | warning | Register spills may happen since callee '%s' specifies register R%d as scratch but caller '%s' doesn't specify |
| main | 372 | 5 | error | Parameter registers from R%d to R%d overlap with return address register R%d to R%d for %s '%s' |
| main | 373 | 5 | error | Feature '%s' requires command line option '%s'. |
| main | 374 | 5 | error | Pragma '%s' has value %d which is not aligned |
| main | 375 | 5 | error | %s %d is invalid for %s |
| main | 376 | 5 | error | Cannot specify R%d-R%d as %s since function '%s' is restricted to use maximum of %d registers |
| main | 377 | 5 | error | Pragma %s value should be atleast %d %s |
| main | 378 | 3 | warning | Pragma %s requires %s, ignoring pragma |
| main | 379 | 5 | error | Pragma %s conflicts with previously defined pragma %s for function '%s' |
| main | 380 | 6 | fatal | ABI mismatch when '%s' calls '%s': %s differ |
| main | 381 | 6 | fatal | Mismatch in return address abi when '%s' calls '%s' |
| main | 382 | 6 | fatal | Pragma '%s' cannot be specified for syscall function '%s' |
| main | 383 | 3 | warning | '%s' option is ignored unless '%s' option is specified |
| main | 384 | 3 | warning | '%s' option should be specified when '%s' option is given |
| main | 385 | 6 | fatal | Unsupported instruction '%s' used when compiling for '%s' target architecture |
| main | 386 | 3 | warning | Too many values specified for option '%s', option will be ignored |
| main | 387 | 3 | warning |  %s section not found |
| main | 388 | 6 | fatal |  32-Bit compilation is no longer supported |
| main | 389 | 6 | fatal |  32-Bit ABI (--machine 32 or 32-Bit addressing) is not supported on %s or higher architectures. |
| main | 390 | 6 | fatal |  %d Bit host architecture (--machine) being used must match with .address_size of %d bits for -g compilation |
| main | 391 | 3 | warning |  %d Bit host architecture (--machine) being used mismatches with .address_size of %d bits |
| main | 392 | 3 | warning | Program uses %d-bit address %s which is conflicting with .address_size %d |
| main | 393 | 6 | fatal | Unknown function %s |
| main | 394 | 6 | fatal | File '%s' could not be opened |
| main | 395 | 5 | error | Program using constant pointers passed as entry function parameter cannot use cvta.const |
| main | 396 | 3 | warning | Program compiled without abi might not work properly under debugger |
| main | 397 | 3 | warning | '%s' option is not fully implemented for '%s' and may not work as expected |
| main | 398 | 3 | warning | '%s' is a deprecated option, It will be removed in future release and is not recommended |
| main | 399 | 5 | error | Entry function '%s' with max regcount of %d calls function '%s' with regcount of %d |
| main | 400 | 2 | info | Command line option '%s' not supported for '%s', will be ignored |
| main | 401 | 3 | warning | Option '%s' may not work as expected in separate compilation unless all modules participating in linking have this option specified. |
| main | 402 | 6 | fatal | %s is not allowed when command line option '%s' is specified  |
| main | 403 | 5 | error | %s is not allowed when command line option '%s' is specified  |
| main | 404 | 3 | warning | Command line option '%s' not supported for '%s', will be ignored |
| main | 405 | 6 | fatal | '%s' requires PTX ISA .version %s or later |
| main | 406 | 5 | error | Function with multiple return values is not allowed with ABI |
| main | 407 | 3 | warning | Function %s%s%s with multiple return values found, disabling ABI |
| main | 408 | 3 | warning | Use of module scoped .local or .reg variable '%s' found, disabling ABI |
| main | 409 | 6 | fatal | Line No: %s: Cannot take address of %s '%s' %s |
| main | 410 | 6 | fatal | Cannot take address of %s '%s' %s |
| main | 411 | 3 | warning | Value specified in pragma %s %d for '%s' should be lower than the %s %d. Ignoring %s |
| main | 412 | 6 | fatal | Invalid value for --Ofast-compile/-Ofc (%s), is not 'max', 'mid', 'min', or '0' |
| main | 413 | 3 | warning | Value of RegUsageLevel (%d) is out of valid range {0..10}, option will be ignored |
| main | 414 | 3 | warning | Too big maxrregcount value specified %d, will be ignored |
| main | 415 | 3 | warning | For %s %s adjusting per thread register count of %d to lower bound of %d |
| main | 416 | 2 | info | Overriding maximum register limit %d for '%s' with %s %d %s |
| main | 417 | 2 | info | Overriding global maxrregcount %d with entry-specific value %d %s |
| main | 418 | 3 | warning | Value of %s for entry %s is out of range. %s will be ignored |
| main | 419 | 3 | warning | Too many %s specified for entry %s, will be ignored |
| main | 420 | 6 | fatal | Function '%s' is recursive |
| main | 421 | 3 | warning | Unresolved extern variable '%s' in whole program compilation, ignoring extern qualifier |
| main | 422 | 6 | fatal | Unresolved extern %s '%s' |
| main | 423 | 6 | fatal | Conflicting options %s and %s specified |
| main | 424 | 3 | warning | Option %s requires option %s; ignoring since latter is not specified |
| main | 425 | 3 | warning | Conflicting options %s and %s specified, ignoring %s option |
| main | 426 | 2 | info | STAT: %s |
| main | 427 | 5 | error | Invalid value '%d' of '%s' with option '%s' |
| main | 428 | 6 | fatal | Invalid value '%s' for option '%s' |
| main | 429 | 6 | fatal | Unknown option '%s' |
| main | 430 | 3 | warning | Construct '%s' on %s is supported only with %s, ignoring construct |
| main | 431 | 3 | warning | Pragma '%s' on %s is supported only with %s, ignoring pragma |
| main | 432 | 3 | warning | Pragma '%s' not supported on %s, ignoring pragma |
| main | 433 | 5 | error | Pragma '%s' not supported |
| main | 434 | 3 | warning | Option '%s' not supported for gpu architecture '%s', will be ignored |
| main | 435 | 5 | error | Option '%s' not supported for gpu architecture '%s' |
| main | 436 | 6 | fatal | '%s' requires ABI |
| main | 437 | 5 | error | Unresolved label '%s' used in function_name attribute. |
| main | 438 | 5 | error | Invalid value '%s' for option -abi. |
| main | 439 | 3 | warning | .loc directive without .file directive is found, line information in generated binary may not be complete |
| main | 440 | 5 | error | PTX .version %s does not support .target %s |
| main | 441 | 6 | fatal | PTX with .target '%s' cannot be compiled for architecture '%s' |
| main | 442 | 6 | fatal | Program with .target '%s' cannot be compiled to different SM-family architecture |
| main | 443 | 6 | fatal | Program with .target '%s' cannot be compiled to future architecture |
| main | 444 | 6 | fatal | SM version specified by .target is higher than default SM version assumed |
| main | 445 | 5 | error | labels '%s' and '%s' must be defined in the same section |
| main | 446 | 5 | error | label '%s' invalid |
| main | 447 | 2 | info | Function properties for %s     %d bytes stack frame, %d bytes spill stores, %d bytes spill loads |
| main | 448 | 2 | info | Fatpoint count for entry %s for regclass %s : %d  |
| main | 449 | 5 | error | Invalid default cache modifier on %s specified: %s |
| main | 450 | 6 | fatal | Optimized debugging not supported |
| main | 451 | 2 | info | (C%04d) %s |
| main | 452 | 3 | warning | (C%04d) %s |
| main | 453 | 5 | error | (C%04d) %s |
| main | 454 | 6 | fatal | (C%04d) %s |
| main | 455 | 2 | info | Compiling entry function '%s' for '%s' |
| main | 456 | 2 | info | %s |
| main | 457 | 6 | fatal | Ptx assembly aborted due to errors |
| main | 458 | 6 | fatal | Unsupported code generation for gpu '%s', forcetext=%d |
| main | 459 | 6 | fatal | Unsupported gpu architecture '%s' |
| main | 460 | 6 | fatal | Output file '%s' could not be opened |
| main | 461 | 6 | fatal | Parsed trace file not in expected format : %s |
| main | 462 | 6 | fatal | Input file '%s' not in expected format |
| main | 463 | 6 | fatal | Input file '%s' could not be opened |
| main | 464 | 6 | fatal | Internal compiler error%s, please report |
| link | 0 | 6 | fatal | Dwarf version %d is not supported |
| link | 1 | 6 | fatal | Duplicate debug line sequence for a section found |
| link | 2 | 6 | fatal | Actual row for context %d is unknown |
| link | 3 | 6 | fatal | Debug line table not present for current sequence |
| link | 4 | 6 | fatal | LEB128 enoding error while encoding '%s' |
| link | 5 | 6 | fatal | Unknown argument %d for option %s |
| link | 6 | 6 | fatal | Unknown argument %s for option %s |
| link | 7 | 6 | fatal | Knobs file not found |
| link | 8 | 6 | fatal | Input file '%s' could not be opened |
| link | 9 | 6 | fatal | %s value (%s) out of range |
| link | 10 | 6 | fatal | Too many options file opened '%s' |
| link | 11 | 3 | warning | option '%s' has been deprecated |
| link | 12 | 6 | fatal | Could not open options file '%s' |
| link | 13 | 6 | fatal | No value specified for option '%s' |
| link | 14 | 6 | fatal | Value '%s' is not defined for option '%s' |
| link | 15 | 6 | fatal | Keyword '%s' is not defined for option '%s' |
| link | 16 | 6 | fatal | Unknown option '%s' |
| link | 17 | 6 | fatal | '%s': expected a number |
| link | 18 | 6 | fatal | '%s': expected true or false |
| link | 19 | 6 | fatal | redefinition of keyword '%s'  |
| link | 20 | 6 | fatal | '%s' is not in 'keyword=value' format |
| link | 21 | 3 | warning | incompatible redefinition for option '%s', the last value of this option was used |
| link | 22 | 6 | fatal | redefinition of argument '%s' |
| link | 23 | 6 | fatal | no argument expected after '%s' |
| link | 24 | 6 | fatal | argument expected after '%s' |
| link | 25 | 5 | error | Function '%s' uses single CTA(.cta_group::1) and CTA pair granularity(.cta_group::2) and that is not allowed. |
| link | 26 | 5 | error | Function '%s' uses reserved shared memory when not allowed. |
| link | 27 | 5 | error | For kernel functions with parameter size higher than 4k bytes on sm_7x and sm_8x, all objects must be compiled with 12.1 or later |
| link | 28 | 5 | error | Cannot link shared object '%s' with partially linked kernel '%s' |
| link | 29 | 3 | warning | Function '%s' and function '%s' have conflicting cache preference, falling back to use cache preference of entry '%s' |
| link | 30 | 3 | warning | Function '%s' has address taken but no calls to it found |
| link | 31 | 5 | error | Function '%s' uses %d bytes stack but limited to %d |
| link | 32 | 6 | fatal | Internal error: %s |
| link | 33 | 6 | fatal | Unified symbols found but index file not specified. |
| link | 34 | 5 | error | entry function '%s' with max regcount of %d calls function '%s' with regcount of %d |
| link | 35 | 5 | error | Undefined reference to '%s' in '%s' |
| link | 36 | 3 | warning | Stack size for entry function '%s' cannot be statically determined |
| link | 37 | 6 | fatal | More than %d %s used in entry function '%s' |
| link | 38 | 5 | error | Entry function '%s' uses too much data for compiler-generated constants; please recompile with -Xptxas --disable-optimizer-constants |
| link | 39 | 5 | error | File uses too much global %s data (0x%llx bytes, 0x%x max) |
| link | 40 | 5 | error | Entry function '%s' uses too much %s data (0x%llx bytes, 0x%x max) |
