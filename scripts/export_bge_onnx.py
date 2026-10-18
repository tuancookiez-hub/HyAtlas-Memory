#!/usr/bin/env python
"""Export BGE-small-en-v1.5 to ONNX so the HyAtlas Go server can embed locally.

Release binaries embed the model, so most users never need this. A plain source build
reads models/bge-small-en-v1.5.onnx(.data) at runtime; run this once (needs torch and
transformers) to regenerate it from the Hugging Face model.

Usage (from the repo root):
    python scripts/export_bge_onnx.py
    python scripts/export_bge_onnx.py --model-dir PATH_TO_LOCAL_SNAPSHOT

By default the model is loaded from the Hugging Face cache by name (offline, so it must
already be cached; set HF_HUB_OFFLINE=0 to allow a download). The ONNX files are written
to <repo>/models/.
"""
import argparse
import os

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("OMP_NUM_THREADS", "4")

from pathlib import Path

import torch
from transformers import AutoModel, AutoTokenizer

MODEL_ID = "BAAI/bge-small-en-v1.5"
OUT = Path(__file__).resolve().parent.parent / "models" / "bge-small-en-v1.5.onnx"


def _model_source() -> str:
    """The local snapshot given with --model-dir, else the model id (resolved from the cache)."""
    ap = argparse.ArgumentParser(description="Export BGE-small-en-v1.5 to ONNX.")
    ap.add_argument("--model-dir", help="a local snapshot directory of the model")
    args = ap.parse_args()
    return args.model_dir or MODEL_ID


def main() -> None:
    src = _model_source()
    tok = AutoTokenizer.from_pretrained(src)
    model = AutoModel.from_pretrained(src)
    model.eval()
    dummy = tok(
        ["redis cluster timeout"],
        padding=True,
        truncation=True,
        return_tensors="pt",
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with torch.no_grad():
        torch.onnx.export(
            model,
            (dummy["input_ids"], dummy["attention_mask"]),
            str(OUT),
            opset_version=14,
            input_names=["input_ids", "attention_mask"],
            output_names=["last_hidden_state"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "seq"},
                "attention_mask": {0: "batch", 1: "seq"},
                "last_hidden_state": {0: "batch", 1: "seq"},
            },
        )
    print(f"exported -> {OUT} ({OUT.stat().st_size / 1e6:.1f}MB + .data weights)")


if __name__ == "__main__":
    main()
