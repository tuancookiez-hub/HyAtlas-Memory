"""Local OpenAI-compatible embedding server for Hy-Memory.

Spawned by HyMemoryProcess alongside the hy-memory server. Loads a
sentence-transformers model and exposes ``/v1/embeddings`` matching
OpenAI's response shape so hy-memory's existing ``_embed_openai`` path
can hit it without any upstream changes.

Mirrors the Hindsight local-embedded pattern: bundled daemon in the same
venv, auto-shuts down after ``--idle-timeout`` seconds of inactivity.

Usage::

    python embed_server.py --model BAAI/bge-small-en-v1.5 --port 19528
"""

import argparse
import logging
import signal
import threading
import time
from pathlib import Path

from pydantic import BaseModel, Field

_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


class EmbeddingRequest(BaseModel):
    """Request body for ``/v1/embeddings``. Module-level (not local)
    because pydantic v2 cannot resolve forward references for classes
    defined inside a function scope — the schema would never build and
    every request would 422 with ``loc=['query','req']``."""
    input: list[str] = Field(..., min_length=1)
    model: str | None = None


def _build_logger(log_path: Path | None) -> logging.Logger:
    """File-only logger. errors='replace' so surrogate chars from
    upstream libs (uvicorn, sentence-transformers) don't crash the
    encoding on Windows. Parent process captures stdout separately."""
    log = logging.getLogger("hy_memory_embed")
    log.setLevel(logging.INFO)
    log.propagate = False
    fmt = logging.Formatter(_LOG_FORMAT)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, mode="a", encoding="utf-8",
                                 errors="replace")
        fh.setFormatter(fmt)
        log.addHandler(fh)
    return log


# Known sentence-transformers models and their native embedding dims.
# Used so we can report dim without forcing a model load on /info.
_KNOWN_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "BAAI/bge-large-en-v1.5": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "intfloat/e5-small-v2": 384,
    "intfloat/e5-base-v2": 768,
    "intfloat/e5-large-v2": 1024,
}


class EmbedServer:
    """Local sentence-transformers HTTP server with idle shutdown."""

    def __init__(self, model: str, port: int, idle_timeout: int = 300,
                 device: str = "cpu", log_path: Path | None = None):
        self.model = model
        self.port = port
        self.idle_timeout = idle_timeout
        self.device = device
        self.log = _build_logger(log_path)

        self._model = None  # lazy
        self._model_lock = threading.Lock()
        self._last_request = 0.0
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._server = None
        self._watchdog: threading.Thread | None = None
        self._loader: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Model loading
    # ------------------------------------------------------------------

    def _load_model(self):
        """Lazy + thread-safe model load. Sets ``_ready`` when complete."""
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is not None:
                return self._model
            self.log.info("Loading %s on %s...", self.model, self.device)
            t0 = time.time()
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model, device=self.device)
            self.log.info(
                "Model loaded in %.1fs (dim=%d)",
                time.time() - t0, self._model.get_sentence_embedding_dimension(),
            )
            self._ready.set()
            return self._model

    def _start_loader(self) -> None:
        """Kick off model load in a background thread.

        Returns immediately. ``_ready`` event is set when load completes.
        This is what makes ``/ready`` actually mean "model loaded" — not
        just "server up". Without this, the first /v1/embeddings request
        would block on the download+load.
        """
        if self._loader is not None:
            return
        self._loader = threading.Thread(
            target=self._load_model, daemon=True, name="hy-mem-model-loader",
        )
        self._loader.start()

    def _dim(self) -> int:
        """Return native embedding dim. Avoids a model load if known."""
        if self._model is not None:
            return self._model.get_sentence_embedding_dimension()
        if self.model in _KNOWN_DIMS:
            return _KNOWN_DIMS[self.model]
        return self._load_model().get_sentence_embedding_dimension()

    # ------------------------------------------------------------------
    # FastAPI app
    # ------------------------------------------------------------------

    def _build_app(self):
        from fastapi import FastAPI, HTTPException

        app = FastAPI(title="Hy-Memory Local Embedder")

        @app.get("/healthz")
        def healthz():
            return {"status": "ok", "model": self.model}

        @app.get("/ready")
        def ready():
            if self._ready.is_set():
                return {"status": "ready", "model": self.model, "dim": self._dim()}
            return {"status": "loading", "model": self.model}

        @app.get("/info")
        def info():
            return {
                "model": self.model,
                "dim": self._dim(),
                "device": self.device,
                "idle_timeout": self.idle_timeout,
            }

        @app.post("/v1/embeddings")
        async def embeddings(req: EmbeddingRequest):
            self._last_request = time.time()
            try:
                model = self._load_model()
                vecs = model.encode(req.input, convert_to_numpy=True)
            except Exception as e:
                self.log.exception("Embedding failed")
                raise HTTPException(status_code=500, detail=str(e)) from e
            data = [
                {"object": "embedding", "embedding": v.tolist(), "index": i}
                for i, v in enumerate(vecs)
            ]
            return {
                "object": "list",
                "data": data,
                "model": req.model or self.model,
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            }

        return app

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _watchdog_loop(self):
        """Exit after ``idle_timeout`` seconds with no requests."""
        while not self._stop.is_set():
            # Tick every 5s; check idle since last request.
            self._stop.wait(5.0)
            if self._stop.is_set():
                break
            if self.idle_timeout <= 0:
                continue
            if self._last_request <= 0:
                continue
            idle = time.time() - self._last_request
            if idle >= self.idle_timeout:
                self.log.info(
                    "Idle for %.0fs (limit %ds), shutting down",
                    idle, self.idle_timeout,
                )
                self._stop.set()
                if self._server is not None:
                    self._server.should_exit = True
                break

    def start(self):
        """Start the server. Blocks until shutdown."""
        import uvicorn

        app = self._build_app()
        cfg = uvicorn.Config(
            app, host="127.0.0.1", port=self.port,
            log_level="warning", access_log=False,
        )
        self._server = uvicorn.Server(cfg)

        if self.idle_timeout > 0:
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="hy-mem-embed-watchdog",
            )
            self._watchdog.start()

        def _shutdown(*_):
            self.log.info("Signal received, shutting down")
            self._stop.set()
            if self._server is not None:
                self._server.should_exit = True

        signal.signal(signal.SIGINT, _shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _shutdown)

        self.log.info(
            "Hy-Memory local embedder ready on http://127.0.0.1:%d (model=%s)",
            self.port, self.model,
        )
        # Kick off model load in background — /ready will return 200 only
        # when this completes. First run downloads ~130MB from HF Hub.
        self._start_loader()
        self._server.run()

    def stop(self):
        """Signal shutdown (used by watchdog and external callers)."""
        self._stop.set()
        if self._server is not None:
            self._server.should_exit = True

    def wait_ready(self, timeout: float = 90.0) -> bool:
        """Block until the model is loaded (or timeout). Returns success."""
        return self._ready.wait(timeout=timeout)


def main():
    p = argparse.ArgumentParser(description="Hy-Memory local embedder")
    p.add_argument("--model", default="BAAI/bge-small-en-v1.5",
                   help="sentence-transformers model ID")
    p.add_argument("--port", type=int, default=19528,
                   help="Port to listen on")
    p.add_argument("--idle-timeout", type=int, default=300,
                   help="Auto-shutdown after N seconds idle (0=disabled)")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda", "mps"],
                   help="Inference device")
    p.add_argument("--log-path", type=Path, default=None,
                   help="Optional log file path")
    args = p.parse_args()

    server = EmbedServer(
        model=args.model,
        port=args.port,
        idle_timeout=args.idle_timeout,
        device=args.device,
        log_path=args.log_path,
    )
    server.start()


if __name__ == "__main__":
    main()
