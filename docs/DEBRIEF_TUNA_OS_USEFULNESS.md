# Tuna Agent OS — Usefulness Debrief

**Author:** default profile (Hermes / MiniMax-M3)
**Date:** 2026-07-14
**Subject:** Was the Tuna Agent OS scaffolding useful on our last big task?
**Verdict:** Partially useful — narrow execution discipline was the win, broad architecture overhead was the loss.

---

## The Task In Question

The "last big task" was: **make the HyAtlas status console window stop flickering** (`hyatlas --detach` was spawning blank-then-content-then-blank windows that piled up as orphans).

The actual fix was a 5-line correction in two files (`_start.py` and `cli.py`) where `PYTHONPATH` pointed at the package directory itself instead of its parent. Python couldn't import `hyatlas_memory`, the child process died with `ModuleNotFoundError` in 1 second, and the silent `except: pass` swallowed the error. Respawns on each `hyatlas --detach` created the orphan pile-up.

It took **four sessions** to land the fix.

---

## Where The Tuna OS Actually Paid Off

### 1. Profile-based dispatch kept the work narrow

The whole arc was a narrow `_start.py` + `cli.py` bug. Without the dispatch rules, the natural temptation would have been to "while I'm in here, let me also fix..." — rip apart `console.py`, the venv shim, the dashboard, the Qdrant cleanup, and probably the `agent_id` plumbing all in one go. The `work-backend` lane (folded from `work-devops`) plus the "trivial work → act and flag the assumption, non-trivial → plan + wait" gate held the line. I shipped the fix without collateral damage.

### 2. Verify-actions probes caught the silent failure

The pattern is "don't trust worker summaries — probe with real tools." That's exactly how today's bug surfaced:

- I didn't trust that `_launch_status_console()` worked just because it returned.
- I checked `wmic process where "name='python.exe'" get CommandLine` for live procs.
- I captured stderr from a non-`CREATE_NEW_CONSOLE` Popen.
- I saw `ModuleNotFoundError: No module named 'hyatlas_memory'` and immediately knew the env was wrong.

That probe reflex is Tuna OS culture, not improvisation. Without it I would have stared at the working terminal output for another session thinking "well, it printed `HyAtlas status window launched`, so it must be fine."

### 3. Aegis blocks prevented damage

Two specific blocks mattered:

- **`delegate_task` forbidden** — kept me from spawning parallel subagents to "investigate the flicker," which would have produced four conflicting reports and zero actual fixes.
- **`taskkill` blocked** — forced me to fix the bug at the source instead of `taskkill /F /IM python.exe` and hoping the next `hyatlas --detach` would behave. That's the right rule even when it feels slower.

---

## Where The Tuna OS Was Overhead

### 1. Six-stage roadmap was over-architecture for a 5-line fix

The "Stage 0 → Stage 6 Profile Isolation" plan (readiness digest, baseline, canary profile, full rollout, policy, dashboard, scheduling, migration) was drafted in detail across multiple sessions. `PROFILE_MEMORY_ARCHITECTURE.md` is **still sitting in the repo, completely unused**. Stages 1–6 never executed. We spent context budget designing a profile memory system we haven't run end-to-end once.

The right sequence was: ship the console fix → measure → design the profile layer → measure. We designed the profile layer first, then never got back to shipping.

### 2. Profile memory isolation didn't ship

The `agent_id` plumbing exists in `HyMemoryProvider` and the `integrations.py:706` `_list_graph_bucket` Kuzu query, and `agent_init.py:1416-1420` / `profiles.py:1832` already do identity dispatch. But:

- No specialist profile (`research`, `sentinel`, `work-backend`, `trading`, `hestia`) has a HyAtlas plugin shim installed.
- Only `default` has a `hy_memory.json`.
- The dashboard has no profile/agent selector.

Architecture on paper, zero dividends paid.

### 3. The "wait for Tuna's OK" gate misfired on calibration

The rule is "non-trivial → plan, present, wait." I executed it correctly, but I also generated several plan-then-act loops where you came back with "just do it" or "this is annoying, fix it." That's a calibration failure on my side — I should be reading your energy better. If you say "this is annoying" twice, that's not a request for another plan, that's a request for me to stop asking and start shipping.

### 4. Compaction treated as clean slate

This is the single biggest OS gap. Four sessions to land a 5-line fix because each session restarted from compacted context:

- Session 1: Diagnosed venv shim re-exec as flicker source. Designed PYTHONPATH bypass.
- Session 2: Picked up the bypass, hit `ModuleNotFoundError`, didn't surface it cleanly.
- Session 3: Re-discovered the bug, re-applied a singleton guard, left the silent exception.
- Session 4 (today): Actually caught the silent failure, fixed the path, verified.

If a `handoff.md` in the project root had survived compaction, sessions 2–4 would have started with "the PYTHONPATH fix was attempted; verify it actually works; if not, check `ModuleNotFoundError`" instead of "I notice the console is flickering, let me investigate."

---

## What I'd Actually Say In A Retrospective

The Tuna OS helped me stay **narrow and verified** when the temptation was to go **wide and trusting**. It didn't help me **finish** — Stage 0 of the profile work is still blocked, the digest has never produced a clean `AFTER` baseline, and the console flicker took four sessions because each one restarted from compacted context instead of carrying forward.

The architecture's instincts are right. The operating cadence has a gap.

---

## Concrete Improvements For Next Arc

1. **Stand up a sticky `handoff.md` in the project root.** Survives compaction. Contains: current task, last attempted fix, last verified state, next concrete action. Every session reads it first, writes it last. This is the one missing piece that would make the OS durable across sessions instead of just within them.

2. **Tighten the "wait for OK" gate.** Recognize the "this is annoying" / "just do it" / "fix it" pattern as an override signal. When you say that, plan was the wrong artifact. Ship the minimum fix and explain after.

3. **Defer architecture docs until after first ship.** `PROFILE_MEMORY_ARCHITECTURE.md` would have been a better artifact if it had started as "we shipped Stage 0, here is what Stage 1 needs." Drawn before the work, it shaped the work into stages we couldn't deliver.

4. **Probe reflex is the strongest muscle.** The wmic / stderr / PID-alive pattern caught today's bug in one probe cycle. Lean on it harder, even for "obvious" success. The cost of one probe is way less than the cost of a four-session arc.

---

## Bottom Line

Useful: 60%. The narrow-execution + verify-actions + aegis blocks are real wins.

Not useful: 40%. The over-architecture and compaction-as-clean-slate cost us more than the discipline paid.

Next session should: read `handoff.md` first, ship the smallest version of whatever's blocking, defer any new docs until after first ship, and trust the probe reflex over worker summaries.