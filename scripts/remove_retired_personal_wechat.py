#!/usr/bin/env python3
"""One-shot migration that removes credentials for the retired personal WeChat channel."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from r20_backend.settings_store import remove_env
from r20_gateway.secrets import delete_secrets
ENV_KEYS = {
    "R20_NOTIFY_WECHAT_ILINK_ENABLED",
    "R20_WECHAT_BASE_URL",
    "R20_WECHAT_USER_ID",
    "R20_WECHAT_BOT_TOKEN",
    "R20_WECHAT_CONTEXT_TOKEN",
}
SECRET_KEYS = {"R20_WECHAT_BOT_TOKEN", "R20_WECHAT_CONTEXT_TOKEN"}
STATE_FILES = (ROOT / "data" / "wechat_session_state.json",)


def migrate() -> dict[str, object]:
    delete_secrets(SECRET_KEYS)
    remove_env(ENV_KEYS)
    removed_files: list[str] = []
    for path in STATE_FILES:
        if path.exists():
            path.unlink()
            removed_files.append(str(path.relative_to(ROOT)))
    return {"removed_env": sorted(ENV_KEYS), "removed_secrets": sorted(SECRET_KEYS), "removed_files": removed_files}


if __name__ == "__main__":
    print(migrate())
