/* launch_cubin.c -- minimal CUDA Driver-API host harness for sass_scheduler's
 * dynamic (hazard-safety) validation.
 *
 * Loads a cubin, launches a one-argument kernel `entry(.param .u64 p)` where p
 * points to a device buffer of N u32 words, and prints the buffer back as hex.
 * Using the Driver API (cuModuleLoad + cuLaunchKernel) avoids the nvcc/gcc-16
 * host-header clash -- this is plain C linked against libcuda only.
 *
 * Build:  gcc -O2 launch_cubin.c -o launch_cubin \
 *              -I/usr/local/cuda-13.1/include \
 *              -L/usr/local/cuda-13.1/lib64/stubs -lcuda
 * Run:    ./launch_cubin <file.cubin> <entry> <Nwords> [seed] [grid] [block] [niter]
 *
 * The harness seeds the buffer deterministically, launches <grid> blocks of
 * <block> threads (default 1x32), copies the result back, and prints it.
 * sass_scheduler --verify-dyn runs it on the original and the recomposed cubin
 * and diffs the output.
 *
 * The optional [grid] [block] [niter] tail drives the GPU-measurement modes
 * (--perf-diff / --stall-profile): a larger grid/block fills the SM, and a
 * non-zero <niter> appends a second `.param .u32 niter` argument so a kernel
 * can spin its dependent chain in a loop and amplify a per-instruction stall
 * delta above launch noise.  Calls that pass only <cubin> <entry> <Nwords>
 * [seed] are unchanged: one block of 32 threads, single `.param .u64 p` arg.
 */
#include <cuda.h>
#include <stdio.h>
#include <stdlib.h>

static void check(CUresult r, const char *what) {
    if (r != CUDA_SUCCESS) {
        const char *msg = NULL;
        cuGetErrorString(r, &msg);
        fprintf(stderr, "ERROR %s: %s\n", what, msg ? msg : "?");
        exit(2);
    }
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <cubin> <entry> <Nwords> [seed] [grid] "
                        "[block] [niter]\n", argv[0]);
        return 1;
    }
    const char *cubin = argv[1];
    const char *entry = argv[2];
    int n = atoi(argv[3]);
    unsigned seed  = (argc > 4) ? (unsigned)strtoul(argv[4], NULL, 0) : 12345u;
    unsigned grid  = (argc > 5) ? (unsigned)strtoul(argv[5], NULL, 0) : 1u;
    unsigned block = (argc > 6) ? (unsigned)strtoul(argv[6], NULL, 0) : 32u;
    unsigned niter = (argc > 7) ? (unsigned)strtoul(argv[7], NULL, 0) : 0u;
    if (n <= 0 || n > (1 << 20)) { fprintf(stderr, "bad N\n"); return 1; }
    if (grid == 0) grid = 1;
    if (block == 0 || block > 1024) block = 32;

    check(cuInit(0), "cuInit");
    CUdevice dev;
    check(cuDeviceGet(&dev, 0), "cuDeviceGet");
    CUcontext ctx;
    /* CUDA 13.x cuCtxCreate maps to _v4 (4 args: ctx, params, flags, dev). */
    check(cuCtxCreate(&ctx, NULL, 0, dev), "cuCtxCreate");

    CUmodule mod;
    check(cuModuleLoad(&mod, cubin), "cuModuleLoad");
    CUfunction fn;
    check(cuModuleGetFunction(&fn, mod, entry), "cuModuleGetFunction");

    size_t bytes = (size_t)n * sizeof(unsigned);
    unsigned *host = (unsigned *)malloc(bytes);
    /* deterministic seed */
    unsigned x = seed ? seed : 1u;
    for (int i = 0; i < n; i++) { x = x * 1664525u + 1013904223u; host[i] = x; }

    CUdeviceptr dptr;
    check(cuMemAlloc(&dptr, bytes), "cuMemAlloc");
    check(cuMemcpyHtoD(dptr, host, bytes), "HtoD");

    /* One-arg kernels take just the buffer pointer; amplified loop kernels
     * (niter > 0) take a second u32 iteration count.  The kernel ABI selects
     * how many params are read, so passing the extra arg to a one-arg kernel
     * is harmless (it is never dereferenced). */
    void *args1[] = { &dptr };
    void *args2[] = { &dptr, &niter };
    void **args = (niter > 0) ? args2 : args1;
    check(cuLaunchKernel(fn, grid, 1, 1, block, 1, 1, 0, NULL, args, NULL),
          "cuLaunchKernel");
    check(cuCtxSynchronize(), "cuCtxSynchronize");

    check(cuMemcpyDtoH(host, dptr, bytes), "DtoH");
    for (int i = 0; i < n; i++) printf("%08x\n", host[i]);

    free(host);
    cuMemFree(dptr);
    cuModuleUnload(mod);
    cuCtxDestroy(ctx);
    return 0;
}
