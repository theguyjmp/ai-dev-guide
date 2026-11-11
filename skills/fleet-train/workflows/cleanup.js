export const meta = {
  name: 'app-cleanup',
  description: 'Fix duplicate blocks, repeated literals, long functions and strict clippy findings on the pilot project rebuild main, one agent per crate group, landed through the train',
  phases: [{ title: 'Clean', detail: 'one agent per crate group' }],
}
const H = (args && args.home) || '<harness>'
const S = '~/.claude/skills/overhaul-build/scripts'
const GROUPS = [
  { name: 'drawing-a', crates: ['drawing'], note: 'only files in crates/drawing/src whose file name starts with a-l' },
  { name: 'drawing-b', crates: ['drawing'], note: 'only files in crates/drawing/src whose file name starts with m-z, plus sub-folders' },
  { name: 'library-interop', crates: ['library', 'interop'] },
  { name: 'sketch-refs', crates: ['sketch', 'refs'] },
  { name: 'part-kernel-store', crates: ['part', 'kernel', 'store'] },
  { name: 'doc-assembly-view', crates: ['doc', 'assembly', 'view'] },
  { name: 'rest', crates: ['table', 'samples', 'clash', 'core', 'checks', 'tess', 'motion', 'jobs', 'gpu', 'plm', 'script', 'server', 'sweep', 'versions', 'agent', 'analysis', 'geom'], note: 'also the root src/ folder' },
]
const SCHEMA = { type: 'object', properties: { submitted: { type: 'boolean' }, fixed: { type: 'number' }, left: { type: 'string' } }, required: ['submitted'] }
const brief = (g, slot) => `Pure cleanup pass on the rebuild (Rust). No behaviour changes.
1. \`cd ${H} && py -3 harness.py clean-start --slot ${slot} --name ${g.name}\` prints JSON {worktree, env_prefix}. Work ONLY in that worktree; every cargo call: \`cd <worktree> && <env_prefix> cargo ...\`.
2. Your scope: crates ${g.crates.map(c => 'crates/' + c).join(', ')}${g.note ? ' (' + g.note + ')' : ''}. Do not edit files outside it (another agent owns them).
3. Find problems from the worktree: \`py -3 ${S}/dup_blocks.py crates src\`, \`py -3 ${S}/dup_literals.py crates src\`, \`py -3 ${S}/shape_check.py crates src\`, and for each crate in scope \`cargo clippy -q -p app-<crate> --all-targets -- -D warnings -D clippy::too_many_lines -D clippy::unwrap_used -D clippy::cognitive_complexity\` (pipe through tail -40). Ignore problems in files outside your scope.
4. Fix each in scope: repeated blocks -> one named fn called from both places (reuse an existing helper if one exists - grep first); repeated literals -> a named const; functions over 30 lines or too complex -> split into named steps; unwrap -> proper error handling or expect with a reason only where it truly cannot fail. Keep public behaviour identical.
5. Verify: clippy strict as above is clean for your crates, the three scripts report nothing in your scope, \`cargo test -q -p app-<crate> --lib\` passes for each crate in scope. Do not run acceptance suites (the land train runs all of them).
6. \`git add -A && git commit -m "cleanup(${g.name}): <summary>"\`, then \`cd ${H} && py -3 harness.py clean-submit --slot ${slot} --name ${g.name}\`.
Cap ~100 tool calls; small outputs (tail/grep). If you hit the cap, commit and submit what is clean and say what is left. Return {submitted, fixed, left}.`

phase('Clean')
const res = await parallel(GROUPS.map((g, i) => () => agent(brief(g, 200 + i), { label: `clean:${g.name}`, phase: 'Clean', schema: SCHEMA })))
return res