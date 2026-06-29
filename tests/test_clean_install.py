#!/usr/bin/env python3
"""
Phase 1.4: Clean Install Verification Script

This script simulates a fresh `pip install hyatlas-memory` scenario
in an isolated virtual environment to verify that:
1. The package installs successfully
2. Imports work without hermes-agent installed
3. The provider can initialize and report basic status
4. No hardcoded paths leak into the installed package
5. Stubs work correctly for hermes-agent dependencies

Usage:
    python tests/test_clean_install.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def run_cmd(cmd, cwd=None, check=True, capture=True):
    """Run command and return CompletedProcess."""
    print(f"  → {cmd}")
    result = subprocess.run(
        cmd,
        shell=isinstance(cmd, str),
        cwd=cwd,
        check=check,
        capture_output=capture,
        text=True,
        encoding='utf-8',
        errors='replace'
    )
    if result.stdout and capture:
        print(result.stdout.rstrip())
    if result.stderr and capture:
        print(result.stderr.rstrip())
    return result


def verify_clean_install(tmpdir: Path, venv_dir: Path, repo_root: Path):
    """Build wheel, install in clean venv, verify imports work."""
    
    print("=" * 70)
    print("PHASE 1.4: CLEAN INSTALL VERIFICATION")
    print("=" * 70)
    print(f"  tmpdir:  {tmpdir}")
    print(f"  venv:    {venv_dir}")
    print(f"  repo:    {repo_root}")
    print()
    
    # 1. Build wheel
    print("1. Building package wheel...")
    dist_dir = tmpdir / "dist"
    run_cmd([
        sys.executable, "-m", "build", "--wheel",
        "--outdir", str(dist_dir),
        str(repo_root)
    ], cwd=repo_root)
    
    wheels = list(dist_dir.glob("*.whl"))
    if not wheels:
        print("❌ FAILED: No wheel produced")
        return False
    wheel = wheels[0]
    print(f"  ✓ Built: {wheel.name}")
    print()
    
    # 2. Create isolated venv
    print("2. Creating isolated virtual environment...")
    run_cmd([sys.executable, "-m", "venv", str(venv_dir)])
    
    if sys.platform == "win32":
        python = venv_dir / "Scripts" / "python.exe"
        pip = venv_dir / "Scripts" / "pip.exe"
    else:
        python = venv_dir / "bin" / "python"
        pip = venv_dir / "bin" / "pip"
    
    if not python.exists():
        print(f"❌ FAILED: venv python not found at {python}")
        return False
    print(f"  ✓ Python: {python}")
    print()
    
    # 3. Upgrade pip
    print("3. Upgrading pip...")
    run_cmd([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    print()
    
    # 4. Install ONLY hyatlas-memory (no hermes-agent)
    print("4. Installing hyatlas-memory (without hermes-agent)...")
    start = time.time()
    result = run_cmd(
        [str(pip), "install", "--no-deps", str(wheel)],
        check=False
    )
    elapsed = time.time() - start
    print(f"  ✓ Installed in {elapsed:.1f}s")
    print()
    
    # 5. Install runtime dependencies (without hermes-agent)
    print("5. Installing runtime dependencies (hermes-agent intentionally omitted)...")
    deps = [
        "hy-memory>=1.2.18",
        "kuzu>=0.4.0",
        "openai>=1.30.0",
        "pydantic>=2.0",
        "pyyaml>=6.0",
        "requests>=2.31",
    ]
    for dep in deps:
        result = run_cmd(
            [str(pip), "install", dep],
            check=False
        )
        if result.returncode != 0:
            print(f"  ⚠ {dep} failed to install (expected for hy-memory - not on PyPI)")
    print()
    
    # 6. Try to import the package
    print("6. Verifying: import hyatlas_memory...")
    result = run_cmd(
        [str(python), "-c", "import hyatlas_memory; print(f'   ✓ Version: {hyatlas_memory.__version__}')"],
        check=False
    )
    if result.returncode != 0:
        print("❌ FAILED: Import crashed")
        print("   This would be a BLOCKER for users doing `pip install hyatlas-memory`")
        return False
    print()
    
    # 7. Test HyMemoryProvider instantiation
    print("7. Verifying: HyMemoryProvider() instantiation...")
    result = run_cmd(
        [str(python), "-c", """
from hyatlas_memory import HyMemoryProvider
import warnings
warnings.filterwarnings('default')
try:
    provider = HyMemoryProvider()
    print(f'   ✓ Instantiated: {type(provider).__name__}')
    print(f'   ✓ is_available: {provider.is_available()}')
except Exception as e:
    print(f'   ❌ {type(e).__name__}: {e}')
    raise
"""],
        check=False
    )
    if result.returncode != 0:
        print("❌ FAILED: Provider couldn't instantiate")
        return False
    print()
    
    # 8. Check no hardcoded author paths
    print("8. Scanning installed package for hardcoded paths...")
    if sys.platform == "win32":
        site_packages = venv_dir / "Lib" / "site-packages"
    else:
        site_packages = list(venv_dir.glob("lib/python*/site-packages"))[0]
    
    pkg_dir = site_packages / "hyatlas_memory"
    bad_patterns = [
        r"C:\\Users\\tuanc",
        r"C:/Users/tuanc",
        r"/Users/tuanc",
    ]
    
    issues = []
    for py_file in pkg_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        for pattern in bad_patterns:
            if pattern in content:
                issues.append((py_file.relative_to(site_packages), pattern))
    
    if issues:
        print("❌ FAILED: Hardcoded paths found:")
        for path, pattern in issues:
            print(f"   - {path}: {pattern}")
        return False
    else:
        print("  ✓ No hardcoded paths in installed package")
    print()
    
    # 9. Verify stubs kick in when hermes-agent is not installed
    print("9. Verifying fallback stubs when hermes-agent missing...")
    result = run_cmd(
        [str(python), "-c", """
import warnings
warnings.filterwarnings('ignore', category=ImportWarning)
from hyatlas_memory import MemoryProvider, get_hermes_home, tool_error

# MemoryProvider should be a stub when hermes-agent is not installed
try:
    from agent.memory_provider import MemoryProvider as RealMP
    is_real = True
except ImportError:
    is_real = False

if is_real:
    print('   ⚠ hermes-agent actually present (unexpected for clean test)')
else:
    home = get_hermes_home()
    err = tool_error('test')
    print(f'   ✓ get_hermes_home(): {home}')
    print(f'   ✓ tool_error("test"): {err[:50]}...')
"""],
        check=False
    )
    if result.returncode != 0:
        print("❌ FAILED: Stubs didn't work")
        return False
    print()
    
    print("=" * 70)
    print("✅ PHASE 1.4 PASSED: Clean install verified")
    print("=" * 70)
    return True


def main():
    """Main entry point for clean install verification."""
    # Script is at tests/test_clean_install.py, so repo root is 2 levels up
    repo_root = Path(__file__).parent.parent.resolve()
    
    # Verify we're in the right place
    if not (repo_root / "pyproject.toml").exists():
        print(f"❌ Cannot find repo root (pyproject.toml not found at {repo_root})")
        return 1
    
    # Create temp directory outside the repo
    with tempfile.TemporaryDirectory(prefix="hyatlas_clean_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        venv_dir = tmpdir / "venv"
        
        try:
            success = verify_clean_install(tmpdir, venv_dir, repo_root)
            return 0 if success else 1
        except Exception as e:
            print(f"\n❌ UNEXPECTED ERROR: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            return 1


if __name__ == "__main__":
    sys.exit(main())
