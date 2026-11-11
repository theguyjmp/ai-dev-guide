export const meta = {
  name: 'app-builders',
  description: '16 short-lived builder agents claim rebuild rows, build them in warm per-slot worktrees, and submit to the batch land train',
  phases: [{ title: 'Build', detail: 'worker loops: claim -> build -> submit, fresh agent per row' }],
}
const SLOTS = (args && args.slots) || 16
const MAX_PER_WORKER = (args && args.max) || 40
const H = (args && args.home) || '<harness>'
const SCHEMA = { type: 'object', properties: {
  status: { type: 'string', enum: ['submitted', 'released', 'idle'] },
  row: { type: 'string' }, note: { type: 'string' } }, required: ['status'] }

const brief = (slot) => `You are a builder for the rebuild (Rust workspace). One row, end to end, then return.

1. Claim: run \`cd ${H} && py -3 harness.py claim --slot ${slot} --wait 540\`. It prints JSON. If "row" is null, return {status:"idle"} at once.
2. Work ONLY in the printed "worktree" (already on a fresh branch from main). Every Bash call: \`cd <worktree> && <env_prefix> cargo ...\` (the shell does not keep cwd). Never use another worktree or the main checkout.
3. Read the row's line in spec/FUNCTIONS.md (grep for "| <row> |"), its acceptance test files, and only the source files you need (grep/sed ranges, not whole huge files). If "previous_branch" is set, first \`git merge --no-edit <branch>\` to salvage that earlier attempt (resolve conflicts) and continue from it. "old_harness_reason" and "recent_rejects" say why earlier attempts failed - fix exactly that.
4. You OWN this row completely. There is no planner to send it back to: if a dependency capability is missing, a stub is in the way, the row is too big, or a file is not in the row's file list - implement what is needed yourself, in the module where it belongs. Keep it minimal and clean (no duplicated helpers - grep for an existing one first; clippy -D warnings clean).
5. Done = the row's acceptance tests pass: \`cargo test -q --test <stem> -- --exact <test_name>\` for each test in "tests" (tests under tests/acceptance/s*.rs may be slow in debug: add --release for those if a debug run takes over 3 min). Then \`cargo clippy -q -p <crate> --all-targets -- -D warnings\` for each crate you touched. Do NOT run the whole workspace test suite - the land train does that once per batch.
6. Rules: never edit the status column of spec/FUNCTIONS.md; never edit or weaken acceptance tests of other rows; edit this row's own test only if it contradicts the row's spec text, and say why in the commit message. No new [[test]] entries for other rows. No sleeping or polling.
6b. The claim JSON has "must_also_before_commit": follow it exactly. A second copy of anything an owner already has (core::math, core::param, core::refusal, spec/index/*) or any dup/shape/strict-clippy problem gets your branch rejected at land.
7. Commit (git add -A && git commit -m "<row>: <what>"), then \`cd ${H} && py -3 harness.py submit --slot ${slot} --row <row>\` and return {status:"submitted", row}.
8. Budget: about 90 tool calls. If you cannot get it green by then, or the same step fails twice the same way, commit whatever compiles, run \`cd ${H} && py -3 harness.py release --slot ${slot} --row <row> --reason "<precise blocker: file, error, what is needed>"\`, and return {status:"released", row, note}.
Keep tool output small: pipe cargo output through tail -30 or grep error.`

async function worker(slot) {
  let idle = 0, n = 0
  while (n < MAX_PER_WORKER && idle < ((args && args.idle) || 2)) {
    n++
    const r = await agent(brief(slot), { label: `s${slot}#${n}`, phase: 'Build', schema: SCHEMA })
    if (!r || r.status === 'idle') { idle++; continue }
    idle = 0
    log(`s${slot}: ${r.status} ${r.row || ''} ${r.note ? '- ' + r.note.slice(0, 80) : ''}`)
  }
  return { slot, runs: n }
}

phase('Build')
const res = await parallel(Array.from({ length: SLOTS }, (_, i) => () => worker(i)))
return res