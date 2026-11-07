"""Thread caps and rlimits shared by every sandbox child (run-code harness,
pipeline child, and the remote worker).

On many-core hosts (this box: 256 cores) native libraries size their thread
pools to the core count. Under RLIMIT_AS that fails or crashes: OpenBLAS
segfaults at import, OpenCV logs "Can't spawn new thread: res = 11" and
returns garbage. RLIMIT_CPU is also summed across threads. So every pool we
know of is pinned to SANDBOX_BLAS_THREADS through its env var, which must be
in place before the library is imported (guard.py keeps these names when it
wipes the environment, so lazy imports still see them).
"""

from __future__ import annotations

import platform

from ...config import config

# Env vars that size native thread pools. Keep guard._SAFE_ENV in sync.
THREAD_ENV_VARS = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS",
    "OPENCV_FOR_THREADS_NUM",                      # cv2 parallel_for_ pool
    "TF_NUM_INTRAOP_THREADS", "TF_NUM_INTEROP_THREADS",
)


def thread_count() -> int:
    return max(1, config.SANDBOX_BLAS_THREADS)


def thread_env() -> dict[str, str]:
    n = str(thread_count())
    return {k: n for k in THREAD_ENV_VARS}


def make_preexec(timeout: int):
    """preexec_fn applying CPU / address-space / core-dump limits, or None
    on platforms without `resource`."""
    if platform.system() == "Windows":
        return None
    import resource

    n = thread_count()
    mem = config.SANDBOX_MEMORY_LIMIT_GB * 1024 ** 3

    def _set_limits():
        # CPU time is summed across threads, so scale by the thread cap.
        resource.setrlimit(resource.RLIMIT_CPU, (timeout * 2 * n, timeout * 3 * n))
        # SANDBOX_MEMORY_LIMIT_GB=0 skips RLIMIT_AS. Needed for CUDA (it maps
        # far more virtual address space than it uses); the GPU worker caps
        # memory with its container limit instead.
        if mem > 0:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except (ValueError, resource.error):
                pass
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _set_limits
