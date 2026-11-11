"""Codebase stats of app-rebuild main, as JSON (for the rebuild dashboard)."""
import json, re, subprocess
REPO = "<repo>"
git = lambda *a: subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
files = git("ls-tree", "-r", "--name-only", "main").split()
FN = re.compile(r"^\s*(?:pub(?:\([a-z]+\))?\s+)?(?:const\s+|async\s+)?fn\s+\w+", re.M)


def measure(paths):
    loc = fns = todo = 0
    for p in paths:
        t = git("show", "main:" + p)
        loc += sum(1 for l in t.splitlines() if l.strip() and not l.strip().startswith("//"))
        fns += len(FN.findall(t))
        todo += t.count("todo!(")
    return loc, fns, todo


src = [f for f in files if f.endswith(".rs") and f.startswith(("crates/", "src/")) and "/tests/" not in f]
tests = [f for f in files if f.endswith(".rs") and f.startswith("tests/")]
cpp = [f for f in files if f.endswith((".cpp", ".h", ".hpp"))]
loc, fns, todo = measure(src)
tloc, tfns, _ = measure(tests)
cloc, _, _ = measure(cpp)
print(json.dumps({"sha": git("rev-parse", "--short", "main").strip(), "crates": len({f.split("/")[1] for f in src if f.startswith("crates/")}),
                  "source_files": len(src), "source_loc": loc, "functions": fns, "todo_stubs": todo,
                  "test_files": len(tests), "test_loc": tloc, "test_fns": tfns, "cpp_files": len(cpp), "cpp_loc": cloc,
                  "tracked_files": len(files)}))
