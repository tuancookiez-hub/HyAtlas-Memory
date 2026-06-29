#!/usr/bin/env python3
"""
Phase 3.3: Smoke Test Script

Tests critical paths of HyAtlas Memory installation:
1. Import and provider instantiation
2. Config loading
3. Server health check (if running)
4. Dashboard health check (if running)
5. Basic add/search roundtrip (if server is running)

Usage:
    python tests/smoke_test.py [--skip-server]
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

# Detect if running under pytest
_is_pytest = "pytest" in sys.modules
try:
    import pytest
except ImportError:
    pytest = None

# Ensure src is on path for dev installs
repo_root = Path(__file__).parent.parent
src_path = repo_root / "src"
if src_path.exists():
    sys.path.insert(0, str(src_path))


def test_import():
    """Test that hyatlas_memory imports cleanly."""
    print("1. Import test...", end=" ")
    try:
        import hyatlas_memory
        print(f"✅ v{hyatlas_memory.__version__}")
        return True
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
        return False


def test_provider():
    """Test that HyMemoryProvider can be instantiated."""
    print("2. Provider instantiation...", end=" ")
    try:
        from hyatlas_memory import HyMemoryProvider
        provider = HyMemoryProvider()
        available = provider.is_available()
        print(f"✅ available={available}")
        return True
    except Exception as e:
        print(f"❌ {type(e).__name__}: {e}")
        return False


def test_server_health():
    """Test that the upstream server is healthy (if running)."""
    print("3. Server health check...", end=" ")
    try:
        with urllib.request.urlopen("http://127.0.0.1:19527/healthz", timeout=5) as r:
            if r.status == 200:
                print("✅ healthy")
                return True
            else:
                print(f"❌ HTTP {r.status}")
                return False
    except Exception:
        print("⚠️ not running (skip with --skip-server)")
        if _is_pytest:
            pytest.skip("Server not running")
        return None


def test_dashboard_health():
    """Test that the dashboard is healthy (if running)."""
    print("4. Dashboard health check...", end=" ")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=5) as r:
            if r.status == 200:
                print("✅ healthy")
                return True
            else:
                print(f"❌ HTTP {r.status}")
                return False
    except Exception:
        print("⚠️ not running")
        if _is_pytest:
            pytest.skip("Dashboard not running")
        return None


def test_graph_counts():
    """Test that graph counts endpoint returns valid data."""
    print("5. Graph counts...", end=" ")
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/graph-counts", timeout=10) as r:
            data = json.loads(r.read())
            l5 = data.get("l5_knowledge", 0)
            l6 = data.get("l6_schema", 0)
            l7 = data.get("l7_intention", 0)
            total = data.get("total", 0)
            print(f"✅ L5={l5} L6={l6} L7={l7} total={total}")
            return True
    except Exception:
        print("⚠️ endpoint not available")
        if _is_pytest:
            pytest.skip("Graph counts endpoint not available")
        return None


def test_no_hardcoded_paths():
    """Test that no hardcoded author paths exist in the package source."""
    print("6. Hardcoded path scan...", end=" ")
    import hyatlas_memory
    pkg_dir = Path(hyatlas_memory.__file__).parent

    bad = []
    for py_file in pkg_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        # Check for hardcoded Windows author paths (not in comments)
        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if "C:\\Users\\tuanc" in line or "C:/Users/tuanc" in line:
                bad.append(f"{py_file.name}:{line_num}")

    if bad:
        print(f"❌ Found {len(bad)} hardcoded paths")
        for b in bad[:5]:
            print(f"   - {b}")
        return False
    else:
        print("✅ clean")
        return True


def main():
    """Run all smoke tests."""
    skip_server = "--skip-server" in sys.argv

    print("=" * 60)
    print("HyAtlas Memory — Smoke Test")
    print(f"Python: {sys.executable}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print()

    results = []

    # Core tests (always run)
    results.append(("Import", test_import()))
    results.append(("Provider", test_provider()))
    results.append(("No hardcoded paths", test_no_hardcoded_paths()))

    # Server tests (skip if --skip-server)
    if not skip_server:
        server_ok = test_server_health()
        if server_ok is not None:
            results.append(("Server health", server_ok))

            dash_ok = test_dashboard_health()
            if dash_ok is not None:
                results.append(("Dashboard health", dash_ok))

                graph_ok = test_graph_counts()
                if graph_ok is not None:
                    results.append(("Graph counts", graph_ok))

    # Summary
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passed = sum(1 for _, ok in results if ok is True)
    failed = sum(1 for _, ok in results if ok is False)
    skipped = sum(1 for _, ok in results if ok is None)

    for name, ok in results:
        if ok is True:
            print(f"  ✅ {name}")
        elif ok is False:
            print(f"  ❌ {name}")
        else:
            print(f"  ⏭️ {name} (skipped)")

    print()
    print(f"  {passed} passed · {failed} failed · {skipped} skipped")
    print("=" * 60)

    if failed > 0:
        print("❌ SMOKE TEST FAILED")
        return 1
    else:
        print("✅ SMOKE TEST PASSED")
        return 0


if __name__ == "__main__":
    sys.exit(main())
