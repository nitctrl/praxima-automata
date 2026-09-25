"""LiveKit deploy entrypoint. Keep this path: `uv run src/agent.py start|console|dev`.

The worker lives in `praxima.entrypoints.voice_worker`.
"""

from praxima.entrypoints.voice_worker import main

if __name__ == "__main__":
    main()
