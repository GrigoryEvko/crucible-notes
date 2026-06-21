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
 * Run:    ./launch_cubin <file.cubin> <entry> <Nwords> [seed]
 *
 * The harness seeds the buffer deterministically, launches one block of 32
 * threads, copies the result back, and prints it.  sass_scheduler --verify-dyn
 * runs it on the original and the recomposed cubin and diffs the output.
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
        fprintf(stderr, "usage: %s <cubin> <entry> <Nwords> [seed]\n", argv[0]);
        return 1;
    }
    const char *cubin = argv[1];
    const char *entry = argv[2];
    int n = atoi(argv[3]);
    unsigned seed = (argc > 4) ? (unsigned)strtoul(argv[4], NULL, 0) : 12345u;
    if (n <= 0 || n > (1 << 20)) { fprintf(stderr, "bad N\n"); return 1; }

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

    void *args[] = { &dptr };
    check(cuLaunchKernel(fn, 1, 1, 1, 32, 1, 1, 0, NULL, args, NULL),
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
