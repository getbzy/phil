# Operator proposals

## 2026-09-26 19:4xZ: the unscreened fallback has no runnable pool view on the operator runner

**Symptom.** CYCLE.md step 4 says that when `screen.py prepare` reports
quota exhaustion, the cycle researches "unscreened candidates as before".
On the operator machine, loop.sh's `--allowedTools` permits only
`Bash(python3 core/*)` plus git, so `scan.py | <anything but core/>` and
shell filters all need approval, which a headless run cannot give. Scan's
stdout is ~1,100 JSONL rows (~1 MB), too large to read raw, and no core
script lists or filters the pool. The screener cap was reached at the
18:40Z prepare today (150/150, all operator), so every later cycle today
has this problem.

**What I did.** I read candidates from the previous prepare's batch files
(`reports/screener-work/20260926T184046Z/batch-*.json`) with Grep/Read.
Those files are up to an hour old and hold the stratified 300, not the
full 1,107. I wrote `strategy/tools/pool.py` (a compact, filterable view
of scan's stdout that skips markets already forecast), but this runner
cannot execute it.

**Ask (either one fixes it).** (a) Add `"Bash(python3 strategy/tools/*)"`
to loop.sh's allowlist, since those are the agent's own tools, which
CYCLE.md already says I own. Or (b) have `screen.py prepare`, on quota
exhaustion, still write a compact unscreened pool file (after the
pre-filters) into a work dir that the cycle can Read.
