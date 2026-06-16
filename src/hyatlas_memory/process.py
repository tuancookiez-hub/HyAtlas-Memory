"""Subprocess lifecycle manager for the Hy-Memory Python server.

Spawns ``python -m hy_memory.server`` as a child process, manages health
checks, and handles graceful shutdown.  Windows-compatible.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_VENV_NAME = "hy-memory-venv"
_DEFAULT_PORT = 19527
_HEALTH_POLL_INTERVAL = 1.0
_HEALTH_POLL_TIMEOUT = 90  # first start may need to install deps


class HyMemoryProcess:
    """Manages the Hy-Memory server subprocess."""

    def __init__(self, config: dict):
        self._config = config
        self._port = int(config.get("server_port", _DEFAULT_PORT))
        self._host = config.get("server_host", "127.0.0.1")
        self._process: subprocess.Popen | None = None
        self._started_by_us = False

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    # ------------------------------------------------------------------
    # Python / venv resolution
    # ------------------------------------------------------------------

    def _venv_dir(self) -> Path:
        """Return the venv directory path."""
        from hermes_constants import get_hermes_home
        return get_hermes_home() / _VENV_NAME

    def _venv_python(self) -> str:
        """Return path to the venv's Python executable."""
        venv = self._venv_dir()
        if sys.platform == "win32":
            return str(venv / "Scripts" / "python.exe")
        return str(venv / "bin" / "python3")

    def _venv_exists(self) -> bool:
        return Path(self._venv_python()).is_file()

    def _ensure_venv(self) -> str:
        """Create venv if needed, install hy-memory. Returns python path."""
        venv = self._venv_dir()
        python = self._venv_python()

        if self._venv_exists():
            return python

        logger.info("[hy-memory] Creating venv at %s", venv)
        venv.mkdir(parents=True, exist_ok=True)

        # Create venv
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True, capture_output=True, timeout=60,
        )

        # Install hy-memory + deps
        logger.info("[hy-memory] Installing hy-memory in venv...")
        pip = str(venv / "Scripts" / "pip.exe") if sys.platform == "win32" else str(venv / "bin" / "pip")
        subprocess.run(
            [pip, "install", "--quiet", "hy-memory", "kuzu", "chromadb"],
            check=True, capture_output=True, timeout=300,
        )
        logger.info("[hy-memory] Venv ready")
        return python

    # ------------------------------------------------------------------
    # Env var construction
    # ------------------------------------------------------------------

    def _build_env(self) -> dict:
        """Build environment variables for the Hy-Memory server process.

        Translates our config dict into the env vars that hy_memory reads.
        """
        env = os.environ.copy()

        cfg = self._config

        # Mode
        mode = cfg.get("mode", "pro")
        env["MEMORY_MODE"] = mode

        # LLM
        llm = cfg.get("llm", {})
        if llm.get("provider"):
            env["MEMORY_LLM_PROVIDER"] = llm["provider"]
        if llm.get("model"):
            env["MEMORY_LLM_MODEL"] = llm["model"]
        if llm.get("api_key"):
            env["MEMORY_LLM_API_KEY"] = llm["api_key"]
        if llm.get("base_url"):
            env["MEMORY_LLM_BASE_URL"] = llm["base_url"]
        if llm.get("temperature") is not None:
            env["MEMORY_LLM_TEMPERATURE"] = str(llm["temperature"])

        # Embedder
        emb = cfg.get("embedder", {})
        if emb.get("provider"):
            env["MEMORY_EMBEDDER_PROVIDER"] = emb["provider"]
        if emb.get("model"):
            env["MEMORY_EMBEDDER_MODEL"] = emb["model"]
        if emb.get("api_key"):
            env["MEMORY_EMBEDDER_API_KEY"] = emb["api_key"]
        if emb.get("base_url"):
            env["MEMORY_EMBEDDER_BASE_URL"] = emb["base_url"]
        if emb.get("dims"):
            env["MEMORY_EMBEDDING_DIMS"] = str(emb["dims"])

        # Vector store
        vs = cfg.get("vector_store", {})
        if vs.get("provider"):
            env["MEMORY_VECTOR_STORE"] = vs["provider"]

        # Graph store
        gs = cfg.get("graph_store", {})
        if gs.get("provider"):
            env["MEMORY_GRAPH_PROVIDER"] = gs["provider"]

        # Cache
        cache = cfg.get("cache", {})
        if cache.get("backend"):
            env["MEMORY_CACHE_BACKEND"] = cache["backend"]

        # Data directory
        data_dir = cfg.get("data_dir", "")
        if data_dir:
            env["MEMORY_DATA_DIR"] = os.path.expanduser(data_dir)

        # Server port/host
        env["HY_MEMORY_SERVER_PORT"] = str(self._port)
        env["HY_MEMORY_SERVER_HOST"] = self._host

        # Log level
        log_level = cfg.get("log_level", "INFO")
        env["MEMORY_LOG_LEVEL"] = log_level

        # Thinking mode (for deepseek/kimi/hunyuan models)
        thinking = cfg.get("thinking_mode", "")
        if thinking:
            env["HY_MEMORY_THINKING_MODE"] = thinking

        return env

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start the Hy-Memory server.

        Returns True if the server is running (either we started it or
        it was already running).
        """
        from .client import HyMemoryClient
        client = HyMemoryClient(self.base_url, timeout=3)

        # Already running?
        if client.is_reachable():
            logger.info("[hy-memory] Server already running at %s", self.base_url)
            return True

        # Ensure venv + deps
        try:
            python = self._ensure_venv()
        except Exception as e:
            logger.error("[hy-memory] Failed to prepare venv: %s", e)
            return False

        env = self._build_env()

        # Spawn the server process
        logger.info("[hy-memory] Starting server on %s:%d", self._host, self._port)

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        try:
            self._process = subprocess.Popen(
                [python, "-m", "hy_memory.server",
                 "--port", str(self._port),
                 "--host", self._host],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            self._started_by_us = True
        except Exception as e:
            logger.error("[hy-memory] Failed to spawn server: %s", e)
            return False

        # Wait for health
        logger.info("[hy-memory] Waiting for server health check...")
        if client.wait_until_ready(timeout=_HEALTH_POLL_TIMEOUT):
            logger.info("[hy-memory] Server ready")
            return True

        logger.error("[hy-memory] Server failed health check within %ds", _HEALTH_POLL_TIMEOUT)
        self.stop()
        return False

    def stop(self):
        """Stop the server if we started it."""
        if self._process is None:
            return

        logger.info("[hy-memory] Stopping server (pid %d)...", self._process.pid)
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(self._process.pid)],
                    capture_output=True, timeout=10,
                )
            else:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=3)
        except Exception as e:
            logger.warning("[hy-memory] Error stopping server: %s", e)
        finally:
            self._process = None
            self._started_by_us = False

    def is_running(self) -> bool:
        """Check if the server process is alive and responsive."""
        if self._process is not None:
            if self._process.poll() is not None:
                # Process exited
                self._process = None
                self._started_by_us = False
                return False

        from .client import HyMemoryClient
        client = HyMemoryClient(self.base_url, timeout=3)
        return client.is_reachable()

    def ensure_running(self) -> bool:
        """Start if not running. Returns True if server is available."""
        if self.is_running():
            return True
        return self.start()
