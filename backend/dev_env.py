"""Load backend/.env and backend/.env.near (KEY=value per line) into the environment.
Neither file is committed. Values already set in the environment win."""
import os
from pathlib import Path

HERE = Path(__file__).parent


def load_env() -> None:
    for name in (".env", ".env.near"):
        path = HERE / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
