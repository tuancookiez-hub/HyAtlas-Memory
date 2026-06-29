# HyAtlas Memory v2.0.0 Public Release Roadmap

**Created:** 2026-06-29  
**Status:** Planning Phase  
**Owner:** @tuancookiez  
**Last Updated:** 2026-06-29

## Overview

HyAtlas Memory v2.0.0 is currently tagged and working end-to-end for the author's personal setup. This roadmap documents the work required to validate external release quality — ensuring clean installs work, migration paths are documented, and no hidden blockers remain.

**Current State:**
- ✅ Pipeline works (L5 entities: 688, L6 schemas: 239, L7 intentions: 57)
- ✅ All 18 patches load successfully
- ⚠️ Import crash on `pip install hyatlas-memory` (hermes-agent imports not optional)
- ⚠️ 30+ hardcoded `C:\Users\tuanc\` paths in `bin/` scripts
- ⚠️ 384-dim references still present after 1024-dim migration
- ⚠️ Kuzu checkpoint documentation missing
- ⚠️ No automated install testing

**Goal:** Tag `v2.0.0-public` only after all phases complete and verified.

---

## Phase 0: Assessment & Planning

**Status:** ✅ Complete (this session)

### Deliverables
- [x] Adversarial code review completed (found 22 issues, 4 critical blockers)
- [x] Empirical patch verification (18/20 patches load successfully)
- [x] Hardcoded path inventory (30+ instances identified)
- [x] User ID parameterization audit
- [x] Kuzu WAL checkpoint behavior documented
- [x] This roadmap created

### Key Findings
1. **Import crash (Blocker):** `__init__.py:40-42` imports `agent.memory_provider`, `hermes_constants`, `tools.registry` — not available on PyPI, must be made optional
2. **Hardcoded paths:** `patches.py:1210` has `C:\Users\tuanc\` fallback; 6+ `bin/` scripts hardcoded to author's setup
3. **User ID defaults:** Multiple hardcoded `hermes-user` (acceptable), `tuanc`, and Discord snowflake ID
4. **384-dim legacy:** `patches.py:147,269,891`, `_memory_cli.py:164`, `__init__.py:62` still reference `agent_memories_384`
5. **Kuzu checkpoint:** WAL accumulates until clean shutdown; `kuzu_db.wal` currently 14MB vs 28KB main file

### Acceptance Criteria
- ✅ Findings documented with line numbers
- ✅ Roadmap created with phases
- ✅ Dependencies between phases identified

---

## Phase 1: Critical Fixes for Clean Install

**Status:** ✅ Complete (2026-06-30)  
**Estimated Scope:** 2-3 development sessions  (Actual: 1 session)
**Dependencies:** None

### Deliverables

#### 1.1 Fix Import Crash
- **File:** `src/hyatlas_memory/__init__.py:40-42`
- **Problem:** `from agent.memory_provider import MemoryProvider` crashes on clean install
- **Solution:** Made imports lazy/optional with try/except, added import threading that was missing
- **Acceptance:** ✅
  - `pip install hyatlas-memory` in fresh venv succeeds
  - `import hyatlas_memory` doesn't crash without hermes-agent
  - Full functionality works when hermes-agent is present

#### 1.2 Fix Hardcoded HERMES_HOME Fallback
- **File:** `src/hyatlas_memory/patches.py:1210`
- **Problem:** Fallback is `C:\Users\tuanc\AppData\Local\hermes` instead of `~/.hermes`
- **Solution:** Use `Path.home() / ".hermes"` or `Path.home() / "AppData" / "Local" / "hermes"` (Windows)
- **Acceptance:** ✅
  - Code works on any machine without `HERMES_HOME` env var set
  - No hardcoded username fallbacks remain in core code

#### 1.3 Audit & Document `bin/` Scripts
- **Files:** `src/hyatlas_memory/server/bin/*.py` (10+ scripts)
- **Problem:** All hardcoded to author's paths; unclear if user-facing or dev-only
- **Solution:** Parameterized all bin/ scripts to use HERMES_HOME env var with platform-specific fallbacks
- **Acceptance:** ✅
  - Each script uses dynamic path resolution
  - No `C:\Users\tuanc\` references in installed package

#### 1.4 Clean Install Test
- **Task:** Run full install in Docker or fresh venv
- **Solution:** Created `tests/test_clean_install.py` that builds wheel, installs in isolated venv, verifies imports and instantiation
- **Acceptance:** ✅
  - `pip install hyatlas-memory` succeeds in isolated venv
  - `import hyatlas_memory` works without hermes-agent installed
  - `HyMemoryProvider()` instantiates successfully
  - No hardcoded paths detected in installed package

### Risk Mitigation
- ✅ Test in isolated venv before modifying author's working setup
- ✅ Keep backup of current venv
- ✅ One fix per commit for easy rollback

---

## Phase 2: Migration & Documentation

**Status:** 🚧 In Progress  (2026-06-30)  
**Dependencies:** ✅ Phase 1 complete

### Deliverables

#### 2.1 384→1024 Dimension Migration Script
- **Problem:** Legacy 384-dim references exist despite 1024-dim migration docs
- **Files affected:** 
  - `patches.py:147,269,891` (collection name)
  - `_memory_cli.py:164` (collection name)
  - `__init__.py:62` (default collection)
- **Solution:**
  - Create `scripts/migrate_384_to_1024.py`
  - Update docs to reference migration script
  - Add detection logic: "Error: expected 1024-dim, found 384-dim"
- **Acceptance:**
  - Script handles both Windows and Unix paths
  - Tested on backup data (use `kuzu_db_384_backup`)
  - Clear error message when dimension mismatch detected

#### 2.2 Kuzu Checkpoint Documentation
- **Problem:** WAL accumulates until clean shutdown; users don't know this
- **Solution:**
  - Add "Shutting Down Gracefully" section to README
  - Document `hyatlas stop` behavior
  - Add troubleshooting: "WAL file growing indefinitely"
- **Acceptance:**
  - Docs explain why `kuzu_db.wal` may be large
  - Clear restart recommendation

#### 2.3 Install Guide for External Users
- **Sections:**
  - Prerequisites (Python 3.10+, Docker optional)
  - Installation steps
  - Verification (test add/search)
  - Common issues (import errors, missing hermes-agent)
  - Migration from official hy-memory
- **Acceptance:**
  - Tested by following steps verbatim in fresh environment
  - Covers Windows, macOS, Linux

#### 2.4 User ID Configuration Guide
- **Problem:** Multiple hardcoded IDs (`hermes-user`, `tuanc`, Discord snowflake)
- **Solution:**
  - Document `HY_MEMORY_USER_ID` env var (primary)
  - Document `HY_MEMORY_USER_ALIASES` for multi-tenant
  - Remove author-specific IDs from defaults where possible
- **Acceptance:**
  - Multi-user setup works without code changes
  - Clear examples for different use cases

### Risk Mitigation
- Test migration on backup data first
- Document "what could go wrong" for each migration step
- Provide rollback instructions

---

## Phase 3: Testing Infrastructure

**Status:** ⏳ Pending  
**Estimated Scope:** 2-3 development sessions  
**Dependencies:** Phase 2 complete

### Deliverables

#### 3.1 Docker Compose Setup
- **Files:** `docker-compose.yml`, `Dockerfile`
- **Purpose:** Test clean install in isolated environment
- **Acceptance:**
  - `docker-compose up` starts Qdrant + HyAtlas
  - Dashboard accessible on `localhost:8765`
  - Add/search operations work
  - No hardcoded paths break

#### 3.2 CI/CD Pipeline (Optional)
- **Platform:** GitHub Actions
- **Jobs:**
  - Lint check (ruff, mypy)
  - Install test (fresh venv)
  - Docker build test
  - Basic smoke test (add → search → verify)
- **Acceptance:**
  - Runs on every push to `main`
  - Catches regressions before merge

#### 3.3 Smoke Test Script
- **File:** `tests/smoke_test.py`
- **Test cases:**
  1. Fresh install (no existing data)
  2. Add memory, verify in Qdrant
  3. Search memory, verify results
  4. System2 digest (if LLM configured)
  5. Dashboard health check
- **Acceptance:**
  - Runs in < 60 seconds
  - Exit code 0 on success

### Risk Mitigation
- Docker compose first (isolated, reproducible)
- CI can be added later if needed
- Smoke test focuses on critical paths only

---

## Phase 4: Polish & External Release

**Status:** ⏳ Pending  
**Estimated Scope:** 1-2 development sessions  
**Dependencies:** Phase 3 complete, all tests passing

### Deliverables

#### 4.1 Final Adversarial Review
- **Task:** Re-run full adversarial review after fixes
- **Acceptance:**
  - All Phase 1 blockers resolved
  - No new blockers found
  - Review results documented

#### 4.2 CHANGELOG Updates
- **Tasks:**
  - Add "Breaking Changes" section with migration steps
  - Document "v2.0.0-public" tag
  - Link to migration script
  - Note about 384→1024 dim migration
- **Acceptance:**
  - Clear, actionable instructions
  - No ambiguity about upgrade path

#### 4.3 Tag v2.0.0-public
- **Criteria:**
  - All phases complete
  - Clean install tested in Docker
  - Migration script tested
  - Smoke tests passing
  - External review clean
- **Tag message:** Full feature list + migration instructions

#### 4.4 GitHub Release Draft
- **Sections:**
  - What's new in v2.0.0
  - Quick start (install, configure, verify)
  - Migration guide (from v1.x or official hy-memory)
  - Known limitations
  - Reporting issues
- **Acceptance:**
  - Copy-paste ready for external users
  - Tested links and code snippets

### Risk Mitigation
- Review checklist before tagging
- Have second opinion on CHANGELOG clarity
- Soft-launch: share with 1-2 trusted users before full announcement

---

## Known Risks & Open Questions

### Technical Risks
1. **Import refactoring scope:** Making `agent.*` imports optional may require significant code restructuring — estimate 1-2 days
2. **Kuzu checkpoint timing:** Empirical testing needed; `auto_checkpoint=True` may or may not help
3. **384→1024 migration:** Backup data may not represent current schema; test carefully
4. **Dashboard 66MB allocation:** Works at current scale, may need optimization for >10k memories

### Open Questions
1. **bin/ scripts:** Should they be user-facing or dev-only? Need to audit each script's purpose
2. **Multi-tenant defaults:** What's the best default for `HY_MEMORY_USER_ID`? Currently `hermes-user` seems reasonable
3. **Dashboard polling interval:** Should 10000 limit be reduced to 2000 for better memory efficiency?
4. **CI/CD scope:** Full pipeline or just Docker smoke test? Balance maintenance cost vs coverage

### Mitigation Strategies
- Start Phase 1 in fresh environment (Docker or isolated venv) — don't risk author's working setup
- Keep commits small and focused (one fix per phase)
- Document "what we tried and why it didn't work" for future reference
- Regular checkpoints: verify each phase before starting next

---

## Success Metrics

**Definition of "Ready for Public Release":**

1. ✅ Clean install works in fresh Docker container
2. ✅ Migration from v1.x documented and tested
3. ✅ No hardcoded author-specific paths in installed package
4. ✅ Adversarial review finds no new blockers
5. ✅ Smoke tests pass in CI
6. ✅ External user tested with success (optional but recommended)
7. ✅ CHANGELOG with clear breaking changes + migration steps

**Anti-success signals (stop and re-assess):**
- ❌ Clean install fails despite fixes
- ❌ Migration script data loss on test backup
- ❌ Adversarial review finds 3+ new blockers
- ❌ Scope creep beyond 4 phases

---

## Tracking

| Phase | Status | Sessions Used | Next Action |
|-------|--------|---------------|-------------|
| 0: Assessment | ✅ Complete | 1 | ✅ Done |
| 1: Critical Fixes | ✅ Complete | 1 | Move to Phase 2 |
| 2: Migration/Docs | ⏳ Pending | 0 | Begin 384→1024 migration script |
| 3: Testing | ⏳ Pending | 0 | Blocked on Phase 2 |
| 4: Release | ⏳ Pending | 0 | Blocked on Phase 3 |

**Total Estimated Sessions:** 6-10  
**Total Estimated Timeline:** 2-4 weeks (at 1-2 sessions/week)

---

## Revision History

- **2026-06-29:** Initial roadmap created based on adversarial review findings
- **2026-06-30:** Phase 1 completed - import crash fixed, hardcoded paths removed, clean install verified

---

## Notes for Future Sessions

When continuing work on this roadmap:

1. **Read this file first** to understand current state
2. **Check Phase status table** to see where we left off
3. **Follow acceptance criteria** — don't skip them
4. **Test in Docker/fresh venv** before touching author's setup
5. **Update this file** after completing each phase
6. **Commit with clear messages** referencing phase numbers

The goal is steady progress, not speed. Quality over rush.
