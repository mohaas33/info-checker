"""Print the models available on Nebius Token Factory (GET /v1/models).

Usage (from backend/):  python scripts/list_models.py [filter]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import get_client  # noqa: E402


async def main() -> None:
    needle = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    models = [m async for m in get_client().models.list()]
    ids = sorted(m.id for m in models if needle in m.id.lower())
    for model_id in ids:
        print(model_id)
    print(f"\n{len(ids)} models")


if __name__ == "__main__":
    asyncio.run(main())
