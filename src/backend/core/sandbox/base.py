"""Abstract base class for sandbox code executors.

All callers depend on this interface only — never on a concrete implementation.
Swap SubprocessExecutor → DockerExecutor by changing config.EXECUTOR_CLASS.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ...models.dto import ExecutionResult


class Executor(ABC):
    """Interface for isolated code execution environments."""

    @abstractmethod
    def execute(
        self,
        code: str,
        input_path: str,
        output_dir: str,
        timeout: int,
    ) -> ExecutionResult:
        """Execute code in isolation.

        Args:
            code: Full Python source code defining `main(input_path, output_path_dir) -> str`.
            input_path: Absolute path to the input image file.
            output_dir: Absolute path to the directory where output should be saved.
            timeout: Maximum execution time in seconds.

        Returns:
            ExecutionResult with stdout, stderr, traceback, timing, and file_exists.
        """
        ...
