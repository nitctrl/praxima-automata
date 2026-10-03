"""LiveKit deploy entrypoint. Keep this path: `uv run src/agent.py start|console|dev`.

The worker lives in `praxima.ai.worker.main`.
"""

from praxima.ai.worker.main import main

if __name__ == "__main__":
    main()
