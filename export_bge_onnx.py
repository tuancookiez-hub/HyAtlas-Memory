#!/usr/bin/env python
"""Export BGE-small-en-v1.5 to ONNX so the v4 Go server can embed locally.

The Go binary does NOT bundle the model (133MB .data is gitignored). Run this
once on the dev machine (needs the HyAtlas venv + transformers) to regenerate
models/bge-small-en-v1.5.onnx(.data). The Go server's embed path (LocalEmbedder
or the embed_server subprocess) loads it at runtime.

Usage:
    D:/HyAtlas/.hyatlas/venv/Scripts/python.exe -m venv-not-needed
    python export_bge_onnx.py

Firewall note (Windows): the venv python cannot reach huggingface.co (WinError
10013 from the per-app outbound allowlist). This script relies on the model
being cached locally. If you wipe the HF cache, re-download via a browser or
bundle the export alongside the repo.
"""
import os

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["OMP_NUM_THREADS"] = "4"

from pathlib import Path

import torch
from transformers import AutoModel, AutoTokenizer

MODEL_DIR = Path(
    r"C:/Users/tuanc/.cache/huggingface/hub/"
    r"models--BAAI--bge-small-en-v1.5/snapshots/"
    r"5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
)
OUT = Path(r"F:/HyAtlas-Memory-Go/models/bge-small-en-v1.5.onnx")


def main() -> None:
    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModel.from_pretrained(str(MODEL_DIR))
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
