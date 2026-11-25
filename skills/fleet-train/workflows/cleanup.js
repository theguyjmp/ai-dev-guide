export const meta = {
  name: 'fleet-cleanup',
  description: 'Sweep duplicate blocks, repeated literals, long functions and strict lint findings, one agent per disjoint module group, each landed through the project push script',
  phases: [{ title: 'Clean', detail: 'one agent per module group' }],
}
const H = (args && args.home) || (() => { throw new Error('pass args.home = the harness folder') })()
const S = (args && args.scripts) || '~/.claude/skills/overhaul-build/scripts'
// groups: disjoint module groups, e.g. [{name: 'core-io', crates: ['core', 'io']}, {name: 'ui', crates: ['ui'], note: '...'}]
// (two groups never share a module: CYCLE.md "Fix by problem class"); pkg_prefix: the package name prefix, if any
const GROUPS = (args && args.groups) || (() => { throw new Error('pass args.groups = disjoint module groups') })()
const PKG = (args && args.pkg_prefix) || ''
// push: the project's push script (CYCLE.md "The minimum cycle" step 2); {slot} and {name} are filled in
const PUSH = (args && args.push) || (() => { throw new Error('pass args.push = the push command, e.g. "bash push.sh {slot} {name}"') })()
const SCHEMA = { type: 'object', properties: { pushed: { type: 'boolean' }, fixed: { type: 'number' }, left: { type: 'string' } }, required: ['pushed'] }
const brief = (g, slot) => `Pure cleanup pass on the rebuild (Rust). No behaviour changes.
1. \`cd ${H} && py -3 harness.py clean-start --slot ${slot} --name ${g.name}\` prints JSON {worktree, env_prefix}. Work ONLY in that worktree; every cargo call: \`cd <worktree> && <env_prefix> cargo ...\`.
2. Your scope: crates ${g.crates.map(c => 'crates/' + c).join(', ')}${g.note ? ' (' + g.note + ')' : ''}. Do not edit files outside it (another agent owns them).
3. Find problems from the worktree: \`py -3 ${S}/dup_blocks.py crates src\`, \`py -3 ${S}/dup_literals.py crates src\`, \`py -3 ${S}/shape_check.py crates src\`, and for each crate in scope \`cargo clippy -q -p ${PKG}<crate> --all-targets -- -D warnings -D clippy::too_many_lines -D clippy::unwrap_used -D clippy::cognitive_complexity\` (pipe through tail -40). Ignore problems in files outside your scope.
4. Fix each in scope: repeated blocks -> one named fn called from both places (reuse an existing helper if one exists - grep first); repeated literals -> a named const; functions over 30 lines or too complex -> split into named steps; unwrap -> proper error handling or expect with a reason only where it truly cannot fail. Keep public behaviour identical.
5. Verify: clippy strict as above is clean for your crates, the three scripts report nothing in your scope, \`cargo test -q -p ${PKG}<crate> --lib\` passes for each crate in scope. Do not run acceptance suites (the verifier runs all of them behind main).
6. \`git add -A && git commit -m "cleanup(${g.name}): <summary>"\`, then \`cd ${H} && ${PUSH.replace('{slot}', slot).replace('{name}', g.name)}\` (exit 0 pushed; 5 run it again; 4 or 1 fix what it prints and run it again).
Cap ~100 tool calls; small outputs (tail/grep). If you hit the cap, commit and push what is clean and say what is left. Return {pushed, fixed, left}.`

phase('Clean')
const res = await parallel(GROUPS.map((g, i) => () => agent(brief(g, 200 + i), { label: `clean:${g.name}`, phase: 'Clean', schema: SCHEMA })))
return res