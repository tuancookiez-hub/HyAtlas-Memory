#!/usr/bin/env python3
"""
Clean Install Test

Tests hyatlas_memory package without hermes-agent in the Python path.
Simulates what happens when someone does `pip install hyatlas-memory` without
having hermes-agent installed.
"""
import sys
import os
import tempfile
import subprocess
from pathlib import Path

def test_import_without_hermes():
    """Test that package imports successfully without hermes-agent."""
    print("=" * 70)
    print("TEST: Import hyatlas_memory WITHOUT hermes-agent")
    print("=" * 70)
    
    # Create a temporary script that tries to import
    test_script = """
import sys
import warnings

# Suppress the expected ImportWarning
warnings.filterwarnings('ignore', category=ImportWarning)

try:
    import hyatlas_memory
    print(f"✅ SUCCESS: Import worked")
    print(f"   Version: {hyatlas_memory.__version__}")
    print(f"   Hermes available: {hyatlas_memory._HERMES_AVAILABLE}")
    
    # Try to instantiate provider
    from hyatlas_memory import HyMemoryProvider
    provider = HyMemoryProvider()
    print(f"✅ SUCCESS: Provider instantiated")
    print(f"   Available: {provider.is_available()}")
    
    # Check that stubs work
    from hyatlas_memory import MemoryProvider, get_hermes_home, tool_error
    print(f"✅ SUCCESS: Stubs available")
    print(f"   MemoryProvider: {MemoryProvider}")
    print(f"   get_hermes_home(): {get_hermes_home()}")
    print(f"   tool_error('test'): {tool_error('test')}")
    
    sys.exit(0)
    
except Exception as e:
    print(f"❌ FAILED: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
"""
    
    # Create temporary venv-like environment
    # Remove hermes-agent from sys.path, only keep hyatlas_memory src
    clean_env = os.environ.copy()
    
    # Create a minimal PYTHONPATH that excludes hermes-agent
    project_root = Path(__file__).parent
    src_path = project_root / "src"
    
    clean_env["PYTHONPATH"] = str(src_path)
    
    # Run the test script with clean environment
    result = subprocess.run(
        [sys.executable, "-c", test_script],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30
    )
    
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    
    success = result.returncode == 0
    print(f"\n{'✅ PASSED' if success else '❌ FAILED'}")
    print("=" * 70)
    
    return success


def test_hermes_available():
    """Test that package still works WITH hermes-agent (regression test)."""
    print("\n" + "=" * 70)
    print("TEST: Import hyatlas_memory WITH hermes-agent (regression)")
    print("=" * 70)
    
    # This test runs in the same environment (hermes-agent already available via venv)
    test_script = """
from hyatlas_memory import HyMemoryProvider, MemoryProvider
import hyatlas_memory
print(f"✅ SUCCESS: Import worked")
print(f"   Available: {HyMemoryProvider().is_available()}")
"""
    
    # Create a subprocess with current environment (includes hermes-agent via venv)
    clean_env = os.environ.copy()
    project_root = Path(__file__).parent
    src_path = project_root / "src"
    
    # Prepend src to PATH so it's used over any pip-installed venv version
    clean_env["PYTHONPATH"] = str(src_path) if not clean_env.get("PYTHONPATH") else f"{src_path};{clean_env['PYTHONPATH']}"
    
    result = subprocess.run(
        [sys.executable, "-c", test_script],
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30
    )
    
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    
    success = result.returncode == 0
    print(f"\n{'✅ PASSED' if success else '❌ FAILED'}")
    print("=" * 70)
    
    return success


if __name__ == "__main__":
    print("HyAtlas Memory - Clean Install Test Suite")
    print(f"Python: {sys.executable}")
    print(f"Version: {sys.version}\n")
    
    results = []
    
    # Test 1: Clean install (no hermes-agent)
    results.append(("Import without hermes-agent", test_import_without_hermes()))
    
    # Test 2: Regression test (with hermes-agent)
    results.append(("Import with hermes-agent", test_hermes_available()))
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")
    
    all_passed = all(passed for _, passed in results)
    print("=" * 70)
    
    if all_passed:
        print("✅ All tests passed!")
        sys.exit(0)
    else:
        print("❌ Some tests failed")
        sys.exit(1)
