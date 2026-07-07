"""Run HyAtlas digest — stable path for Hermes background/cron (no MSYS path mangling).

Install copy to: %LOCALAPPDATA%\\hermes\\scripts\\run_hyatlas_digest.py

On success, prints one Discord-friendly summary line (for no_agent cron deliver).
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"F:/HyAtlas-Memory")
SCRIPT = ROOT / "scripts" / "run_digest_once.py"
LOG = Path.home() / ".hyatlas" / "logs" / "digest_run_latest.log"
DEFAULT_PY = Path(r"C:/Users/tuanc/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe")


def _discord_summary(log_text: str, exit_code: int) -> str:
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    if exit_code != 0:
        return (
            f"HyAtlas weekly digest FAILED (exit {exit_code}) at {ts} "
            "— see ~/.hyatlas/logs/digest_run_latest.log"
        )
    before = after = None
    for line in log_text.splitlines():
        if line.startswith("BEFORE "):
            try:
                before = json.loads(line[7:])
            except json.JSONDecodeError:
                pass
        if line.startswith("AFTER "):
            try:
                after = json.loads(line[6:])
            except json.JSONDecodeError:
                pass
    if before and after:
        b = before.get("layer_counts") or {}
        a = after.get("layer_counts") or {}
        d5 = (a.get("l5_knowledge") or 0) - (b.get("l5_knowledge") or 0)
        d6 = (a.get("l6_schema") or 0) - (b.get("l6_schema") or 0)
        dr = (after.get("relation_count") or 0) - (before.get("relation_count") or 0)
        flag = " (check S2)" if "no_clusters" in log_text else ""
        return (
            f"🧠 HyAtlas digest {ts}: L5 {b.get('l5_knowledge')}→{a.get('l5_knowledge')} ({d5:+d}), "
            f"L6 {b.get('l6_schema')}→{a.get('l6_schema')} ({d6:+d}), relations {dr:+d}{flag}"
        )
    if "HTTP 200" in log_text:
        return f"🧠 HyAtlas digest OK at {ts} (see digest_run_latest.log)"
    return f"HyAtlas digest finished (exit 0) at {ts} — log incomplete"


def main() -> int:
    py = DEFAULT_PY if DEFAULT_PY.is_file() else Path(sys.executable)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(py), str(SCRIPT), "hermes-user", "default"]
    with LOG.open("w", encoding="utf-8") as f:
        f.write(" ".join(cmd) + "\n\n")
        f.flush()
        proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT))
    log_text = LOG.read_text(encoding="utf-8", errors="replace")
    summary = _discord_summary(log_text, proc.returncode)
    print(summary)
    with LOG.open("a", encoding="utf-8") as f:
        f.write("\nSUMMARY " + summary + "\n")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())