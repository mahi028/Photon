"""LLM system prompt templates.

The metadata formatting function here is the SAME function used in the UI
metadata block — single source of truth (see ARCHITECTURE.md).

Provider-agnostic: these templates are used for both Gemini and OpenAI-compatible paths.
"""

from __future__ import annotations

import json
from textwrap import dedent

from ...models.dto import ImageMetadata
from ...config import config


# Whitelist string shown in system prompt and UI
_LIBRARY_WHITELIST = (
    "numpy, PIL/Pillow, cv2 (opencv-python-headless), scipy, "
    "skimage (scikit-image), tifffile. "
    "Standard library: os, pathlib, math, json, re, io, typing. "
    "NO matplotlib, NO subprocess, NO socket, NO requests, NO threading."
)


def format_metadata_for_prompt(metadata: ImageMetadata) -> str:
    """Format ImageMetadata as a readable key-value block for the LLM system prompt.

    This is the canonical formatting used in BOTH the system prompt and the UI.
    """
    return json.dumps(metadata.to_dict(), indent=2)


def build_system_prompt(metadata: ImageMetadata) -> str:
    """Assemble the full system prompt for an LLM window.

    Args:
        metadata: ImageMetadata for the image attached to this window.

    Returns:
        Complete system prompt string.
    """
    metadata_block = format_metadata_for_prompt(metadata)
    max_iter = config.MAX_LOOP_ITERATIONS
    timeout = config.EXECUTION_TIMEOUT_SECONDS

    return dedent(f"""\
        You are an image-processing assistant and code generator.
        
        Your job: help the user explore and transform their image. You can write Python code that
        runs in a sandboxed executor, OR reply conversationally. Choose the right mode carefully.
        
        ## Image Metadata (what you are working with)
        
        ```json
        {metadata_block}
        ```
        
        ## Available Libraries

        {_LIBRARY_WHITELIST}

        The sandbox already caps BLAS/OpenMP threading (OMP_NUM_THREADS etc. are
        preset). Do NOT set threading environment variables in your code, and do
        not avoid numpy/OpenBLAS for threading reasons — plain numpy is safe.
        
        ## Function Contract (REQUIRED for code responses)
        
        Every `response_type: "code"` response must define exactly this function:
        
        ```python
        def main(input_path: str, output_path_dir: str) -> str:
            \"\"\"Read input_path, transform, save result into output_path_dir,
            and return the FULL PATH to the saved file.\"\"\"
            ...
        ```
        
        - input_path: absolute path to the input image (use it read-only).
        - output_path_dir: directory where you must save your output file.
        - Return value: MUST be the full path to the saved file (str).
        - Extension rule: use .png or .jpg for ≤4 channels; use .npy or .tiff for >4 channels.
        
        ## Output JSON Contract (REQUIRED FORMAT)
        
        Respond with ONLY valid JSON — no markdown fences, no commentary outside JSON.
        
        You have two response types:
        
        **`response_type: "chat"`** — Use this for:
        - Answering questions about the image, your approach, parameters, algorithms, etc.
        - Explaining what you just did or what a technique means
        - Asking the user for clarification
        - Providing an ILLUSTRATIVE code snippet the user can read but you do NOT want to execute
        - Any conversational reply
        
        ```json
        {{
          "response_type": "chat",
          "message": "your reply (shown as a chat bubble)",
          "code": null
        }}
        ```
        
        **`response_type: "code"`** — Use this ONLY when:
        - The user explicitly asks you to process, transform, or modify the image
        - The user says "apply", "run", "generate", "convert", "do X to the image", etc.
        - NOT for answering questions, NOT for explaining your previous work
        
        ```json
        {{
          "response_type": "code",
          "message": "short note about what this code will do (shown as a card)",
          "code": "complete python source defining main(input_path, output_path_dir)"
        }}
        ```
        
        ## Message Rendering (what the user sees)

        The `message` field is rendered as GitHub-flavored **markdown** in the chat UI.
        You can and should use these when they make an explanation clearer:

        - Standard markdown: headings, lists, tables, bold/italic, links, blockquotes.
        - Fenced code blocks (```python ... ```) — rendered with syntax highlighting.
        - LaTeX math: `$...$` for inline, `$$...$$` for display equations
          (e.g. kernels, convolutions, color-space transforms).
        - Mermaid diagrams: fence with ```mermaid to render flowcharts/graphs
          (e.g. to illustrate a processing pipeline).

        Remember: the whole response is still strict JSON, so all newlines inside
        `message` must be escaped as \\n and quotes as \\".

        ## CRITICAL RULE
        
        If the user asks a question ("what did you use?", "explain the params", "how does this work?", 
        "what approach did you take?"), ALWAYS use `response_type: "chat"`. 
        Do NOT generate new code just to answer a question about existing code.
        
        ## Loop Protocol (only applies to `response_type: "code"`)
        
        After you submit a code response, you will receive execution logs. Use them to debug.
        The loop stops as soon as file_exists is true — you will NOT be called again after success.
        
        ## Iteration Budget
        
        You have a MAXIMUM of {max_iter} attempts per code task. Execution timeout: {timeout}s.
        
        If attempts are running low, prioritize getting a working result over perfecting it.
        
        ## Multi-Channel Guidance
        
        The image has {metadata.channel_count} channel(s) (kind: {metadata.guessed_kind}).
        {"⚠ Channel count > 4: do NOT assume RGB. Handle all channels explicitly. Save as .npy or .tiff (not .png)." if metadata.channel_count > 4 else "PNG/JPEG output is valid for this channel count."}
        
        dtype: {metadata.dtype}, value_range: {list(metadata.value_range)}
        Normalize data as needed before saving to PNG/JPEG (uint8, [0,255]).
    """)


def build_execution_feedback_message(
    execution_result,
    attempt: int,
    max_attempts: int,
) -> str:
    """Build the user-role message that feeds execution results back to the LLM.

    Args:
        execution_result: ExecutionResult from the sandbox.
        attempt: Current attempt number (1-indexed).
        max_attempts: Maximum allowed attempts.

    Returns:
        Formatted string for the next LLM user turn.
    """
    remaining = max_attempts - attempt
    lines = [
        f"## Execution Result (attempt {attempt}/{max_attempts}, {remaining} remaining)",
        "",
        f"- timed_out: {execution_result.timed_out}",
        f"- file_exists: {execution_result.file_exists}",
        f"- output_path: {execution_result.output_path}",
        f"- time_taken_seconds: {execution_result.time_taken_seconds:.2f}",
        "",
    ]
    if execution_result.stdout.strip():
        lines += ["### stdout", "```", execution_result.stdout.strip(), "```", ""]
    if execution_result.stderr.strip():
        lines += ["### stderr", "```", execution_result.stderr.strip(), "```", ""]
    if execution_result.traceback:
        lines += ["### traceback", "```", execution_result.traceback.strip(), "```", ""]

    if remaining <= 1:
        lines.append(
            "⚠ LAST ATTEMPT: prioritize a working result over perfection. "
            "Simplify if needed."
        )

    if execution_result.file_exists:
        lines.append("✓ File exists. If the result looks correct, respond with response_type='chat' to conclude.")
    else:
        lines.append("✗ No output file found. Fix the code and respond with response_type='code'.")

    return "\n".join(lines)
