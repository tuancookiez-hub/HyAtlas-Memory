"""One-shot System2 digest against the local hy-memory server (long HTTP timeout)."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

USER = sys.argv[1] if len(sys.argv) > 1 else "hermes-user"
# Hermes TUI / hy_memory provider writes facts under agent_id "default", not "default_agent".
AGENT = sys.argv[2] if len(sys.argv) > 2 else "default"
BASE = "http://127.0.0.1:19527"
TIMEOUT = int(sys.argv[3]) if len(sys.argv) > 3 else 3600


def get_json(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read().decode())


def post_json(path: str, body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


PROFILE_AGENTS = frozenset({
    "default", "research", "sentinel", "work-backend", "work-frontend", "trading", "hestia",
})


def preflight(user_id: str, agent_id: str) -> dict:
    """Warn when digest agent_id does not match where VDB facts live."""
    chosen = post_json("/api/v1/list", {"user_id": user_id, "agent_id": agent_id, "limit": 5000})
    vdb = chosen.get("vdb") or {}
    mems = vdb.get("memories") or []
    facts = [m for m in mems if m.get("layer") == "l3_fact"]
    fresh = sum(
        1
        for m in facts
        if (m.get("custom") or {}).get("s2_evidence_count", 0) < 1
    )
    alt = None
    alt_l2 = 0
    if agent_id not in PROFILE_AGENTS:
        try:
            d = post_json("/api/v1/list", {"user_id": user_id, "agent_id": "default", "limit": 5000})
            am = (d.get("vdb") or {}).get("memories") or []
            alt_l2 = sum(1 for m in am if m.get("layer") == "l3_fact")
            if alt_l2 > len(facts):
                alt = "default"
        except Exception:
            pass
    return {
        "agent_id": agent_id,
        "vdb_total": vdb.get("total", len(mems)),
        "l3_facts": len(facts),
        "fresh_l3": fresh,
        "suggest_agent_id": alt,
        "alt_l2_facts": alt_l2,
    }


def main() -> int:
    pf = preflight(USER, AGENT)
    print("PREFLIGHT", json.dumps(pf))
    if pf.get("suggest_agent_id") and pf["suggest_agent_id"] != AGENT:
        print(
            f"WARNING: agent_id={AGENT!r} has few facts; "
            f"hermes-user data may be under {pf['suggest_agent_id']!r} "
            f"({pf.get('alt_l2_facts')} l3_fact). Re-run with: "
            f"{sys.argv[0]} {USER} {pf['suggest_agent_id']}"
        )
        return 2

    before = get_json("/api/v1/graph")
    print(
        "BEFORE",
        json.dumps(
            {
                "layer_counts": before.get("layer_counts"),
                "node_count": before.get("node_count"),
                "relation_count": before.get("relation_count"),
            }
        ),
    )
    body = json.dumps({"user_id": USER, "agent_id": AGENT}).encode()
    req = urllib.request.Request(
        f"{BASE}/api/v1/digest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    print(f"digest POST user={USER} agent={AGENT} timeout={TIMEOUT}s ...")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode()
            elapsed = time.time() - t0
            print(f"HTTP {r.status} elapsed={elapsed:.1f}s")
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                print(raw[:8000])
                return 1
            print("RESULT", json.dumps(result, ensure_ascii=False)[:8000])
    except urllib.error.HTTPError as e:
        print(f"HTTP error {e.code}", e.read().decode()[:4000])
        return 1
    except Exception as e:
        print(f"FAILED after {time.time()-t0:.1f}s:", e)
        return 1

    after = get_json("/api/v1/graph")
    print(
        "AFTER",
        json.dumps(
            {
                "layer_counts": after.get("layer_counts"),
                "node_count": after.get("node_count"),
                "relation_count": after.get("relation_count"),
            }
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
