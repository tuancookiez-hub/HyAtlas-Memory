"""Runtime path resolution for HyAtlas.

This module owns the transition from scattered legacy paths to one runtime
home. It does not move data. Callers can read from legacy paths during the
compatibility window, but all new HyAtlas-owned config should prefer
``HYATLAS_HOME``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def home() -> Path:
    """Return the canonical HyAtlas runtime home."""
    raw = os.environ.get("HYATLAS_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".hyatlas"


def cfgdir(root: str | Path | None = None) -> Path:
    return Path(root) / "config" if root is not None else home() / "config"


def datadir(root: str | Path | None = None) -> Path:
    return Path(root) / "data" if root is not None else home() / "data"


def logs(root: str | Path | None = None) -> Path:
    return Path(root) / "logs" if root is not None else home() / "logs"


def cache(root: str | Path | None = None) -> Path:
    return Path(root) / "cache" if root is not None else home() / "cache"


def snaps(root: str | Path | None = None) -> Path:
    return Path(root) / "snapshots" if root is not None else home() / "snapshots"


def envfile(root: str | Path | None = None) -> Path:
    return cfgdir(root) / ".env"


def cfgfile(root: str | Path | None = None) -> Path:
    return cfgdir(root) / "hy_memory.json"


def qcfg(root: str | Path | None = None) -> Path:
    return cfgdir(root) / "qdrant.yaml"


def qdata(root: str | Path | None = None) -> Path:
    return datadir(root) / "qdrant"


def vdir(root: str | Path | None = None) -> Path:
    return home() / "vector" if root is None else Path(root) / "vector"


def qbin(root: str | Path | None = None) -> Path:
    return vdir(root) / "qdrant" / "qdrant.exe"


def kdata(root: str | Path | None = None) -> Path:
    return datadir(root) / "kuzu_db"


def exports(root: str | Path | None = None) -> Path:
    return datadir(root) / "exports"


def ensure(root: str | Path | None = None) -> None:
    # Note: kdata() is a file (Kuzu database), not a directory; its parent is
    # datadir() which is already created here.
    for path in (cfgdir(root), datadir(root), logs(root), cache(root), snaps(root), qdata(root), exports(root)):
        path.mkdir(parents=True, exist_ok=True)


def hermes() -> Path:
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        return Path(env_home)
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        if sys.platform == "win32":
            return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"
        return Path.home() / ".hermes"


def find_qdrant() -> tuple[Path | None, Path | None]:
    """Locate the Qdrant binary and config. Shared by _start.py and migrate_cli.

    Search order:
      1. HYATLAS_QDRANT_BIN env var (explicit new override)
      2. Migrated binary under HYATLAS_HOME/vector/qdrant
      3. QDRANT_BIN env var (legacy override)
      4. `qdrant` on PATH
      5. Common locations per OS

    The migrated HYATLAS_HOME config is always used unless the user
    explicitly sets HYATLAS_QDRANT_CONFIG / QDRANT_CONFIG.
    """
    cfg = qcfg()

    def _resolve_cfg() -> Path:
        raw = os.environ.get("HYATLAS_QDRANT_CONFIG") or os.environ.get("QDRANT_CONFIG")
        if raw:
            c = Path(raw)
            if c.exists():
                return c
        return cfg

    env_new = os.environ.get("HYATLAS_QDRANT_BIN")
    if env_new and Path(env_new).is_file():
        return Path(env_new), _resolve_cfg()

    if qbin().is_file():
        return qbin(), _resolve_cfg()

    env_legacy = os.environ.get("QDRANT_BIN")
    if env_legacy and Path(env_legacy).is_file():
        return Path(env_legacy), _resolve_cfg()

    import shutil
    p = shutil.which("qdrant")
    if p:
        return Path(p), _resolve_cfg()

    if sys.platform == "win32":
        for c in [Path("C:/qdrant/qdrant.exe"), Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "qdrant" / "qdrant.exe", home() / "qdrant" / "qdrant.exe"]:
            if c.is_file():
                return c, _resolve_cfg()
    else:
        for c in [Path("/usr/local/bin/qdrant"), Path("/usr/bin/qdrant"), Path("/opt/qdrant/qdrant"), home() / "qdrant" / "qdrant"]:
            if c.is_file():
                return c, _resolve_cfg()
    return None, None


def qdrant_data() -> Path | None:
    """Best guess at the live Qdrant storage path for migration."""
    raw = os.environ.get("HYATLAS_QDRANT_DATA")
    if raw:
        return Path(raw)
    # Check HYATLAS_HOME-aware path first (test + migrated installs)
    q = qdata()
    if q.exists():
        return q
    _, cfg = find_qdrant()
    if cfg and cfg.exists():
        for line in cfg.read_text(errors="ignore").splitlines():
            if "storage_path" in line:
                return Path(line.split(":", 1)[1].strip().strip('"').strip("'"))
    if Path("C:/qdrant-data").exists():
        return Path("C:/qdrant-data")
    return None


def legacy_envs() -> list[Path]:
    return [hermes() / ".env", Path.home() / ".hy_memory" / "pkg" / ".env"]


def legacy_cfgs() -> list[Path]:
    return [hermes() / "hy_memory.json"]


def config_candidates() -> list[Path]:
    return [cfgfile(), *legacy_cfgs()]


def active_config_path() -> Path | None:
    return next((path for path in config_candidates() if path.exists()), None)


def read_config() -> dict[str, Any]:
    path = active_config_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_dotenv(path: Path, *, override: bool = False) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def load_envs() -> None:
    for path in [envfile(), *legacy_envs()]:
        load_dotenv(path)


__all__ = [
    "home",
    "cfgdir",
    "datadir",
    "logs",
    "cache",
    "snaps",
    "envfile",
    "cfgfile",
    "qcfg",
    "qdata",
    "kdata",
    "exports",
    "ensure",
    "hermes",
    "find_qdrant",
    "qdrant_data",
    "legacy_envs",
    "legacy_cfgs",
    "config_candidates",
    "active_config_path",
    "read_config",
    "load_dotenv",
    "load_envs",
]
