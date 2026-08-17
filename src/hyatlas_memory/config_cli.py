"""Config helpers for the HyAtlas CLI."""

from __future__ import annotations

import json
import os
import sys
from argparse import ArgumentParser, Namespace
from getpass import getpass
from pathlib import Path
from typing import Any

from . import layout

_EMBEDS = {
    "large": ("BAAI/bge-large-en-v1.5", 1024),
    "small": ("BAAI/bge-small-en-v1.5", 384),
}


def llm_identity(cfg: dict) -> tuple[str, str]:
    """Return the configured provider and model without secrets."""
    llm = cfg.get("llm") or {}
    provider = llm.get("provider") or ""
    model = llm.get("model") or ""
    if ":" in model:
        provider, model = model.split(":", 1)
    if not provider:
        provider = "openai"
    return provider, model


def redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def default_config() -> dict[str, Any]:
    return {
        "llm": {
            "api_key": "",
            "model": "",
            "base_url": "",
            "extra_body": {"thinking": {"type": "disabled"}},
        },
        "embedder": {
            "model": "BAAI/bge-small-en-v1.5",
            "dims": 384,
            "provider": "local",
        },
        "mode": "ultra",
        "vector_store": {
            "provider": "zvec",
            "host": "127.0.0.1",
            "port": 6333,
            "embedding_dims": 384,
        },
        "auto_start": False,
        "port": 19527,
    }


def merged() -> dict[str, Any]:
    cfg = default_config()
    old = layout.read_config()
    for key, value in old.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def save(cfg: dict[str, Any]) -> None:
    layout.ensure()
    layout.cfgfile().write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def _legacy_data_paths() -> list[Path]:
    return [
        Path.home() / ".hy_memory",
        Path("C:/qdrant-data") if sys.platform == "win32" else Path.home() / "qdrant-data",
    ]


def show(_: Namespace) -> int:
    path = layout.active_config_path()
    cfg = merged()
    emb = cfg.get("embedder", {})
    vec = cfg.get("vector_store", {})
    provider, model = llm_identity(cfg)
    print(f"HyAtlas home: {layout.home()}")
    print(f"Config path: {path or layout.cfgfile()}")
    print()
    print(f"Provider: {provider}")
    print(f"Model:    {model}")
    print()
    print("Embedder")
    print(f"  model:    {emb.get('model', '')}")
    print(f"  dims:     {emb.get('dims', '')}")
    print(f"  provider: {emb.get('provider', '')}")
    print()
    print(f"Mode: {cfg.get('mode', '')}")
    print(f"Vector store: {vec.get('provider', '')} {vec.get('host', '')}:{vec.get('port', '')}")
    warnings = []
    if path and path in layout.legacy_cfgs():
        warnings.append(f"using legacy config at {path}")
    for p in _legacy_data_paths():
        if p.exists():
            warnings.append(f"legacy Qdrant data still present at {p}")
    if warnings:
        print()
        for w in warnings:
            print(f"NOTE: {w}. Runtime uses Zvec. Archive with `hyatlas archive qdrant` or delete manually if no longer needed.")
    return 0


def model(args: Namespace) -> int:
    cfg = merged()
    cfg.setdefault("llm", {})
    if args.base_url:
        cfg["llm"]["base_url"] = args.base_url
    if args.model:
        cfg["llm"]["model"] = args.model
    if args.key:
        cfg["llm"]["api_key"] = args.key
    if args.mode:
        cfg["mode"] = args.mode
    save(cfg)
    print(f"✓ Wrote model config to {layout.cfgfile()}")
    print(f"  base_url: {cfg['llm'].get('base_url', '')}")
    print(f"  model:    {cfg['llm'].get('model', '')}")
    print(f"  api_key:  {redact(str(cfg['llm'].get('api_key', '')))}")
    return 0


def embedder(args: Namespace) -> int:
    cfg = merged()
    cfg.setdefault("embedder", {})
    if args.preset:
        cfg["embedder"]["model"], cfg["embedder"]["dims"] = _EMBEDS[args.preset]
    if args.model:
        cfg["embedder"]["model"] = args.model
    if args.dims:
        cfg["embedder"]["dims"] = args.dims
    cfg["embedder"]["provider"] = "local"
    cfg.setdefault("vector_store", {})["embedding_dims"] = cfg["embedder"].get("dims")
    save(cfg)
    print(f"✓ Wrote embedder config to {layout.cfgfile()}")
    print("WARNING: changing embedder model/dims on existing data requires re-vectorization or a new collection.")
    return 0


def validate(_: Namespace) -> int:
    cfg = merged()
    bad: list[str] = []
    llm = cfg.get("llm", {})
    emb = cfg.get("embedder", {})
    vec = cfg.get("vector_store", {})
    if cfg.get("mode") not in {"lite", "pro", "ultra"}:
        bad.append("mode must be lite, pro, or ultra")
    if cfg.get("mode") in {"pro", "ultra"}:
        for key in ("base_url", "model", "api_key"):
            if not llm.get(key):
                bad.append(f"llm.{key} is required for {cfg.get('mode')} mode")
    if not emb.get("model"):
        bad.append("embedder.model is required")
    if not isinstance(emb.get("dims"), int) or emb.get("dims", 0) <= 0:
        bad.append("embedder.dims must be a positive integer")
    if vec.get("provider", "zvec") != "zvec":
        bad.append("vector_store.provider must be 'zvec' (Qdrant is archive/migration-only)")
    if bad:
        print("Config invalid:")
        for item in bad:
            print(f"  ✗ {item}")
        return 1
    print("✓ Config shape valid")
    print(f"  {layout.active_config_path() or layout.cfgfile()}")
    return 0


def ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    label = f"{prompt} [{default}]" if default else prompt
    try:
        value = getpass(f"{label}: ").strip() if secret else input(f"{label}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return value or default


def init(_: Namespace | None = None) -> int:
    print("HyAtlas init")
    print("Writes runtime config under HYATLAS_HOME/config")
    print()
    raw = ask("Runtime home", str(layout.home()))
    os.environ["HYATLAS_HOME"] = raw
    cfg = merged()
    cfg["llm"] = {
        **cfg.get("llm", {}),
        "base_url": ask("OpenAI-compatible base URL", cfg.get("llm", {}).get("base_url", "https://api.openai.com/v1")),
        "model": ask("Model name", cfg.get("llm", {}).get("model", "")),
        "api_key": ask("API key", cfg.get("llm", {}).get("api_key", ""), secret=True),
    }
    mode = ask("Mode (lite/pro/ultra)", str(cfg.get("mode", "ultra"))).lower()
    cfg["mode"] = mode if mode in {"lite", "pro", "ultra"} else "ultra"
    print()
    print("Embedder: 1) BGE small 384 (recommended)  2) BGE large 1024  3) custom local")
    choice = ask("Embedder choice", "1")
    if choice == "2":
        cfg["embedder"] = {"provider": "local", "model": _EMBEDS["large"][0], "dims": _EMBEDS["large"][1]}
    elif choice == "3":
        dims_raw = ask("Vector dimensions")
        try:
            dims = int(dims_raw)
        except ValueError:
            print(f"Invalid dimensions: {dims_raw!r}, defaulting to 384")
            dims = 384
        cfg["embedder"] = {
            "provider": "local",
            "model": ask("Local sentence-transformers model/path"),
            "dims": dims,
        }
    else:
        cfg["embedder"] = {"provider": "local", "model": _EMBEDS["small"][0], "dims": _EMBEDS["small"][1]}
    cfg.setdefault("vector_store", {})["embedding_dims"] = cfg["embedder"]["dims"]
    save(cfg)
    layout.envfile().write_text(
        f"HYATLAS_HOME={layout.home()}\nHY_MEMORY_MODE={cfg['mode']}\n",
        encoding="utf-8",
    )
    print()
    print(f"✓ Wrote {layout.cfgfile()}")
    print(f"✓ Wrote {layout.envfile()}")
    print("Run `hyatlas config validate` next.")
    return 0


def register(sub) -> None:
    cfg = sub.add_parser("config", help="Show or update HyAtlas config")
    csub = cfg.add_subparsers(dest="config_cmd", required=True)

    cshow = csub.add_parser("show", help="Show active config with secrets redacted")
    cshow.set_defaults(func=show)

    cmodel = csub.add_parser("model", help="Set OpenAI-compatible LLM config")
    cmodel.add_argument("--base-url")
    cmodel.add_argument("--model")
    cmodel.add_argument("--key")
    cmodel.add_argument("--mode", choices=["lite", "pro", "ultra"])
    cmodel.set_defaults(func=model)

    cembed = csub.add_parser("embedder", help="Set local embedder config")
    cembed.add_argument("--preset", choices=sorted(_EMBEDS))
    cembed.add_argument("--model")
    cembed.add_argument("--dims", type=int)
    cembed.set_defaults(func=embedder)

    cval = csub.add_parser("validate", help="Validate config shape")
    cval.set_defaults(func=validate)


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="hyatlas config")
    sub = parser.add_subparsers(dest="cmd", required=True)
    register(sub)
    args = parser.parse_args(argv)
    return args.func(args) or 0
