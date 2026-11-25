export const meta = {
  name: 'fleet-builders',
  description: '16 short-lived builder agents claim rebuild rows, build them in warm per-slot worktrees, and land each through the project push script (merge first, verified behind)',
  phases: [{ title: 'Build', detail: 'worker loops: claim -> build -> push, fresh agent per row' }],
}
const SLOTS = (args && args.slots) || 16
// slot_list: explicit slot numbers (e.g. [20, 21, 22]) so a second run never reuses slots, worktrees or claims of a
// run still going; default 0..slots-1
const SLOT_LIST = (args && args.slot_list) || Array.from({ length: SLOTS }, (_, i) => i)
const MAX_PER_WORKER = (args && args.max) || 40
const H = (args && args.home) || (() => { throw new Error('pass args.home = the harness folder') })()
// push: the project's push script (CYCLE.md "The minimum cycle" step 2); {slot} and {row} are filled in
const PUSH = (args && args.push) || (() => { throw new Error('pass args.push = the push command, e.g. "bash push.sh {slot} {row}"') })()
const SCHEMA = { type: 'object', properties: {
  status: { type: 'string', enum: ['pushed', 'released', 'idle'] },
  row: { type: 'string' }, note: { type: 'string' } }, required: ['status'] }

const brief = (slot) => `You are a builder for the rebuild (the claim JSON's "stack" names the stack, its done check and its output habit). One row, end to end, then return.

1. Claim: run \`cd ${H} && py -3 harness.py claim --slot ${slot} --wait 540\`. It prints JSON. If "row" is null, return {status:"idle"} at once.
2. Work ONLY in the printed "worktree" (already on a fresh branch from main). Every Bash call: \`cd <worktree> && <env_prefix> <stack.env_use command>\` (the shell does not keep cwd). Never use another worktree or the main checkout.
3. Read the row's line in spec/FUNCTIONS.md (grep for "| <row> |"), its acceptance test files, and only the source files you need (grep/sed ranges, not whole huge files). If "previous_branch" is set, first \`git merge --no-edit <branch>\` to salvage that earlier attempt (resolve conflicts) and continue from it. "old_harness_reason" and "recent_rejects" say why earlier attempts failed - fix exactly that.
4. You OWN this row completely. There is no planner to send it back to: if a dependency capability is missing, a stub is in the way, the row is too big, or a file is not in the row's file list - implement what is needed yourself, in the module where it belongs. Keep it minimal and clean: before adding or renaming ANY public item (function, type, enum, trait, const, static) find the owner with the index search the claim JSON names, then grep; strict lint clean.
5. Done = the row's acceptance tests pass, run as the claim JSON's "stack".done says (each test in "tests", then the lint or syntax check on what you touched). Do NOT run the whole workspace test suite - the verifier runs it behind main.
6. Rules: never edit the status column of spec/FUNCTIONS.md; never edit or weaken acceptance tests of other rows; edit this row's own test only if it contradicts the row's spec text, and say why in the commit message. Never hand-edit a shared list (test manifest, test group root, index, generated table): add your own files and let the generator register them. No sleeping or polling.
6b. The claim JSON has "must_also_before_commit": follow it exactly. A second copy of anything an owner already has (spec/ARCHITECTURE.md owners, spec/index/*), a public name another module declares, a function grown into a copy of a sibling, or any dup/shape/strict-lint problem gets your push refused.
7. Commit (git add -A && git commit -m "<row>: <what>"), run \`cd ${H} && py -3 harness.py self-review --slot ${slot}\` and fix every duplicate it lists until it prints an empty list (the verifier reverts anything it lists), then run \`cd ${H} && ${PUSH.replace('{slot}', slot)}\` with <row> for {row}. Exit 0 PUSHED: return {status:"pushed", row}. 5 RACE: run it again. 4 RECHECK or 1 REFUSED: fix what it prints, commit, run it again (twice at most, then step 8). 3 HELD: step 8 with the hold reason.
8. Budget: about 90 tool calls. If you cannot get it green by then, or the same step fails twice the same way, commit whatever compiles, run \`cd ${H} && py -3 harness.py release --slot ${slot} --row <row> --reason "<precise blocker: file, error, what is needed>"\`, and return {status:"released", row, note}.
Keep tool output small: the claim JSON's "stack".tail says how.`

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
const res = await parallel(SLOT_LIST.map((s) => () => worker(s)))
return res