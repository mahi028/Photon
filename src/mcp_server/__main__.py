import sys
from pathlib import Path

# Make `src.backend...` importable regardless of the client's working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.mcp_server.server import main  # noqa: E402

main()
