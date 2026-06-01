__version__ = "0.0.1"


def _load_local_env() -> None:
    """Populate os.environ from a repo-root ``.env`` (gitignored) before any
    submodule reads config. Minimal parser — no python-dotenv dependency. Does
    NOT override variables already set in the real environment (so the shell /
    test harness always wins). Keeps secrets like FLOWBOARD_FLOW_API_KEY out of
    the source tree.
    """
    import os
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    try:
        text = env_path.read_text()
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_local_env()
