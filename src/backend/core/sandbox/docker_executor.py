"""Docker-based sandbox executor — stub for future hardening.

TODO(spec): This is a documented stub only. MVP uses SubprocessExecutor.
Implement this when moving to production to get true network isolation,
read-only filesystem (except output_dir), and resource-namespaced containers.

Interface is identical to SubprocessExecutor — swap by changing
config.EXECUTOR_CLASS = "docker".
"""

from __future__ import annotations

from .base import Executor
from ...models.dto import ExecutionResult


class DockerExecutor(Executor):
    """Runs code in a minimal, network-disabled, read-only-except-output Docker container.

    Not implemented in MVP. Raises NotImplementedError if called.

    When implemented:
    - Pull/use a base image with numpy, PIL, cv2, scipy, skimage, tifffile pre-installed.
    - Mount input_path read-only, output_dir read-write.
    - Disable network: --network none.
    - Set memory/CPU limits via Docker run flags.
    - Parse harness JSON from container stdout (same harness as SubprocessExecutor).
    """

    def execute(
        self,
        code: str,
        input_path: str,
        output_dir: str,
        timeout: int,
    ) -> ExecutionResult:
        raise NotImplementedError(
            "DockerExecutor is a stub. Use SubprocessExecutor for MVP. "
            "Set config.EXECUTOR_CLASS = 'subprocess'."
        )
