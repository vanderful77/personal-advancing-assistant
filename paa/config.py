from pathlib import Path
import os
import tomllib
from zoneinfo import ZoneInfo


def load(path="config.toml"):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        cfg = tomllib.load(stream)
    cfg["root"] = str(path.parent)
    for name in ("data_dir", "personal_dir", "skills_dir"):
        cfg[name] = str((path.parent / cfg[name]).resolve())
    ZoneInfo(cfg["timezone"])
    if cfg["poll_seconds"] < 60:
        raise ValueError("poll_seconds must be at least 60")
    os.umask(0o077)
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(cfg["personal_dir"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return cfg
