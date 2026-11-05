"""Measure a project for overhaul-check. Read-only, standard library only.

Usage: py -3 -I measure.py <repo> [--out report.json] [--depth 2] [--days 180]
Writes the full JSON to --out (default <system temp>/measure.json, never inside the repo) and prints a 13-line summary.
Heuristic: function sizes, duplication and import graphs are approximations, good to +-10%.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "target", "dist", "build", "out", ".next", ".nuxt", ".venv",
             "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache", "coverage", "vendor",
             "third_party", ".idea", ".vscode", "bin", "obj", ".gradle", ".turbo", ".cache",
             "site-packages", "Pods", "DerivedData", ".svelte-kit", "storybook-static"}
LANG = {".py": "python", ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "ts",
        ".tsx": "ts", ".rs": "rust", ".go": "go", ".c": "c", ".h": "c", ".cpp": "cpp",
        ".cc": "cpp", ".hpp": "cpp", ".cs": "csharp", ".java": "java", ".kt": "kotlin",
        ".swift": "swift", ".php": "php", ".rb": "ruby", ".dart": "dart", ".scala": "scala",
        ".lua": "lua", ".vue": "vue", ".svelte": "svelte", ".fs": "fsharp", ".liquid": "liquid",
        ".gd": "gdscript", ".fsx": "fsharp", ".m": "objc", ".mm": "objc"}
STYLE_EXT = {".css", ".scss", ".sass", ".less"}
BRACE = {"js", "ts", "rust", "go", "c", "cpp", "csharp", "java", "kotlin", "swift", "php",
         "dart", "scala", "vue", "svelte", "objc"}
KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "else", "do", "try", "with",
            "using", "lock", "foreach", "match", "loop", "sizeof", "typeof", "new", "throw",
            "await", "yield", "case", "when", "unless", "elif", "defer", "go", "select"}
FN_PATTERNS = [
    re.compile(r"\b(?:function|func|fun|fn)\b\s*\*?\s*(?:\([^)]*\)\s*)?(\w+)?\s*(?:<[^>]*>)?\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?"
               r"(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|\w+\s*=>)"),
    re.compile(r"^\s*(?:[\w<>\[\],:*&~?]+\s+)*?(\w+)\s*(?:<[^>]*>)?\s*\([^;{}]*\)?\s*(?:const\s*)?"
               r"(?:->\s*[^{;]+|:\s*[^{;=]+)?\s*(?:throws\s+[\w, .]+)?\s*\{?\s*$"),
]
PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)")
STRIP_STR = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')
TRIVIAL = re.compile(r"^(?:[{}()\[\];,]+|else|end|break;?|return;?|pass|\*/|/\*\*?|\*|"
                     r"(?:import|from|use|using|package|#include|require)\b.*|//.*|#.*|@\w+.*)$")
MARKERS = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
SUPPRESS = re.compile(r"@ts-ignore|@ts-expect-error|eslint-disable|# type: ?ignore|# noqa|"
                      r"#\[allow\(|NOLINT|@SuppressWarnings|\bas any\b|:\s*any\b|\.unwrap\(\)")
SECRETS = [("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
           ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY")),
           ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
           ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
           ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
           ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
           ("password-literal", re.compile(r"(?i)\b(?:password|passwd|secret|api_?key)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"))]
LLM_CALL = re.compile(r"messages\.create\(|messages\.stream\(|api\.anthropic\.com|@anthropic-ai/sdk|"
                      r"@ai-sdk/anthropic|ChatAnthropic|anthropic\.Anthropic\(|new Anthropic\(|AnthropicBedrock|"
                      r"AnthropicVertex|bedrock-runtime.*anthropic|InvokeModel.*claude", re.I)
MANIFEST_NAMES = {"package.json", "Cargo.toml", "pyproject.toml", "requirements.txt", "go.mod", "pom.xml",
                  "build.gradle", "Gemfile", "composer.json"}
CAP_WORDS = ("store", "registry", "picker", "undo", "history", "cache", "logger", "log", "config",
             "settings", "utils", "util", "helpers", "api", "client", "service", "manager",
             "router", "state", "theme", "auth", "db", "queue", "events", "bus")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# Wordiness ("AI slop"): filler phrases that add words and no meaning.
SLOP = re.compile(
    r"(?i)\b(?:it'?s (?:important|worth) (?:to note|noting)|it is (?:important|worth|crucial|essential) to|"
    r"in order to|comprehensive|robust(?:ly)?|seamless(?:ly)?|leverag(?:e|es|ed|ing)|delv(?:e|es|ing)|"
    r"utiliz(?:e|es|ed|ing)|facilitat\w*|streamlin\w*|cutting[- ]edge|state[- ]of[- ]the[- ]art|best practices|"
    r"empower\w*|unlock(?:s|ing)? the|harness(?:es|ing)? the power|in today'?s|"
    r"plays? an? (?:crucial|key|vital|pivotal|important) role|a testament to|furthermore|moreover|"
    r"ensure that|meticulous\w*|elevat(?:e|es|ing) (?:your|the)|"
    r"this (?:function|method|class|component|module) (?:is used to|will|handles|is responsible))\b")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿⭐]")
DOC_EXT = {".md", ".mdx", ".rst"}
HASH_COMMENT_LANGS = {"python", "ruby", "gdscript"}
WINDOW = 8


def is_test(rel):
    r = rel.lower().replace("\\", "/")
    name = r.rsplit("/", 1)[-1]
    return bool(re.search(r"(^|/)(tests?|__tests__|spec|specs|e2e|testing)/", r) or
                re.match(r"test_.*\.py$|.*_test\.(py|go|rs)$|.*\.(test|spec)\.[jt]sx?$|.*Tests?\.(cs|java|kt|swift)$", name))


def module_of(rel, depth):
    parts = rel.replace("\\", "/").split("/")[:-1]
    return "/".join(parts[:depth]) or "."


def func_lengths(lines, lang):
    out = []
    if lang == "python":
        for i, line in enumerate(lines):
            m = PY_DEF.match(line)
            if not m:
                continue
            ind, end = len(m.group(1)), i
            for j in range(i + 1, len(lines)):
                s = lines[j]
                if not s.strip():
                    continue
                if len(s) - len(s.lstrip()) <= ind and not s.lstrip().startswith((")", "#")):
                    break
                end = j
            out.append((m.group(2), i + 1, end - i + 1))
        return out
    if lang not in BRACE:
        return out
    clean = [STRIP_STR.sub('""', l.split("//")[0]) for l in lines]
    for i, line in enumerate(clean):
        name = None
        for p in FN_PATTERNS:
            m = p.search(line)
            if m:
                name = m.group(1) or "<anon>"
                break
        if not name or name in KEYWORDS:
            continue
        start = None
        for j in range(i, min(i + 4, len(clean))):
            if "{" in clean[j]:
                start = j
                break
            if ";" in clean[j]:
                break
        if start is None:
            continue
        depth, end = 0, None
        for j in range(start, min(start + 5000, len(clean))):
            depth += clean[j].count("{") - clean[j].count("}")
            if depth <= 0:
                end = j
                break
        if end is not None:
            out.append((name, i + 1, end - i + 1))
    return out


def imports_of(rel, text, lang, files_set):
    """Return repo-relative files this file imports (best effort)."""
    res, base = [], Path(rel).parent
    if lang in ("js", "ts", "vue", "svelte"):
        for spec in re.findall(r"""(?:from|import|require\()\s*['"](\.{1,2}/[^'"]+)['"]""", text):
            p = os.path.normpath(str(base / spec)).replace("\\", "/")
            for cand in (p, *(p + e for e in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", ".svelte")),
                         *(p + "/index" + e for e in (".ts", ".tsx", ".js", ".jsx"))):
                if cand in files_set:
                    res.append(cand)
                    break
    elif lang == "python":
        for dots, mod in re.findall(r"^\s*from\s+(\.*)([\w.]*)\s+import", text, re.M) + \
                [("", m) for m in re.findall(r"^\s*import\s+([\w.]+)", text, re.M)]:
            root = base
            for _ in range(max(0, len(dots) - 1)):
                root = root.parent
            stem = (root / mod.replace(".", "/")) if dots else Path(mod.replace(".", "/"))
            s = str(stem).replace("\\", "/")
            for cand in (s + ".py", s + "/__init__.py"):
                if cand in files_set:
                    res.append(cand)
                    break
    elif lang == "rust":
        src = None
        parts = rel.replace("\\", "/").split("/")
        if "src" in parts:
            src = "/".join(parts[:parts.index("src") + 1])
        if src:
            for mod in re.findall(r"\bcrate::(\w+)", text):
                for cand in (f"{src}/{mod}.rs", f"{src}/{mod}/mod.rs"):
                    if cand in files_set:
                        res.append(cand)
                        break
    return res


def tarjan(graph):
    idx, low, stack, on, out, n = {}, {}, [], set(), [], [0]
    sys.setrecursionlimit(100000)

    def visit(v):
        idx[v] = low[v] = n[0]
        n[0] += 1
        stack.append(v)
        on.add(v)
        for w in graph.get(v, ()):
            if w not in idx:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop()
                on.discard(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                out.append(sorted(comp))
    for v in list(graph):
        if v not in idx:
            visit(v)
    return out


def git_stats(repo, days):
    def run(*a):
        return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300).stdout
    try:
        if not run("rev-parse", "--is-inside-work-tree").strip():
            return None
    except Exception:  # noqa: BLE001
        return None
    head_ts = int(run("log", "-1", "--format=%ct").strip() or 0)
    since = head_ts - days * 86400  # window ends at the last commit, so stale repos still measure
    log = run("log", f"--since=@{since}", "--no-merges", "--format=@@%H%x09%an%x09%s", "--name-only")
    commits, fixes, reverts, authors, churn = 0, 0, 0, Counter(), Counter()
    fix_churn = Counter()
    is_fix = False
    for line in log.splitlines():
        if line.startswith("@@"):
            _, author, subj = (line[2:].split("\t") + ["", ""])[:3]
            commits += 1
            authors[author] += 1
            is_fix = bool(re.search(r"\b(fix|bug|hotfix|patch|regression|broken|repair)", subj, re.I))
            fixes += is_fix
            reverts += subj.lower().startswith("revert")
        elif line.strip():
            churn[line.strip()] += 1
            if is_fix:
                fix_churn[line.strip()] += 1
    total = run("rev-list", "--count", "HEAD").strip()
    roots = run("rev-list", "--max-parents=0", "HEAD").split()
    first = run("log", "-1", "--format=%as", roots[-1]).strip() if roots else ""
    last = run("log", "-1", "--format=%as").strip()
    return {"commits_total": int(total or 0), "first_commit": first, "last_commit": last,
            "window_days": days, "window_ends_at_last_commit": True,
            "commits": commits, "fix_commits": fixes,
            "fix_share": round(fixes / commits, 3) if commits else 0, "reverts": reverts,
            "authors": authors.most_common(10), "hotspots": churn.most_common(25),
            "fix_hotspots": fix_churn.most_common(15)}


def main():
    a = sys.argv[1:]
    if not a or a[0].startswith("--"):
        print(__doc__)
        sys.exit(2)
    repo = Path(a[0]).resolve()
    opt = lambda k, d: a[a.index(k) + 1] if k in a else d  # noqa: E731
    out_path = Path(opt("--out", os.path.join(tempfile.gettempdir(), "measure.json")))
    depth, days = int(opt("--depth", 2)), int(opt("--days", 180))

    files, manifests, docs = [], [], []
    for root, dirs, names in os.walk(repo):
        # src/bin holds real code (Rust binaries); every other bin/ is build output.
        dirs[:] = [d for d in dirs if not d.startswith(".") and
                   (d not in SKIP_DIRS or (d == "bin" and Path(root).name == "src"))]
        for nm in names:
            p = Path(root) / nm
            if nm in MANIFEST_NAMES and len(manifests) < 50:
                manifests.append(str(p.relative_to(repo)))
            ext = p.suffix.lower()
            if ext in DOC_EXT:
                docs.append(p)
            if ext in LANG or ext in STYLE_EXT:
                try:
                    if ".min." in nm or p.stat().st_size > 2_000_000:
                        continue
                except OSError:  # broken symlink
                    continue
                files.append(p)
    files_set = {str(p.relative_to(repo)).replace("\\", "/") for p in files}

    per_lang, per_mod, sizes, funcs = Counter(), defaultdict(lambda: [0, 0]), [], []
    markers, suppress, secrets, llm_nocache, llm_files = Counter(), Counter(), [], [], 0
    cache_files = []
    slop_hits, slop_phrases, slop_files, emoji, comment_lines = 0, Counter(), Counter(), 0, 0
    hex_raw, var_tokens, test_loc, code_loc = 0, 0, 0, 0
    windows, line_count_for_dup = defaultdict(list), 0
    meaningful_by_file, edges = {}, defaultdict(set)
    basenames = defaultdict(set)

    for p in files:
        rel = str(p.relative_to(repo)).replace("\\", "/")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        if lines and sum(len(l) for l in lines) / len(lines) > 300:
            continue  # minified or generated
        ext = p.suffix.lower()
        lang = LANG.get(ext, "style")
        loc = sum(1 for l in lines if l.strip())
        per_lang[lang] += loc
        if is_test(rel):
            test_loc += loc
        else:
            code_loc += loc
        m = per_mod[module_of(rel, depth)]
        m[0] += 1
        m[1] += loc
        sizes.append((loc, rel))
        tokens = {t.lower() for t in re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", re.sub(r"\.(test|spec)$", "", p.stem))}
        for w in CAP_WORDS:
            if w in tokens:
                basenames[w].add(module_of(rel, 3))
                break
        for i, l in enumerate(lines, 1):
            for mk in MARKERS.findall(l):
                markers[mk] += 1
            if SUPPRESS.search(l):
                suppress[lang] += 1
            for kind, rx in SECRETS:
                if rx.search(l) and not re.search(r"example|placeholder|your[_-]|xxxx|<", l, re.I):
                    secrets.append(f"{rel}:{i} {kind}")
            if lang != "style":
                s = l.lstrip()
                if s.startswith(("//", "/*", "* ", "*/")) or (lang in HASH_COMMENT_LANGS and s.startswith("#")
                                                             and not s.startswith("#!")) or (lang == "lua" and s.startswith("--")):
                    comment_lines += 1
                for mt in SLOP.finditer(l):
                    slop_hits += 1
                    slop_phrases[mt.group(0).lower()] += 1
                    slop_files[rel] += 1
                emoji += len(EMOJI.findall(l))
        if lang in ("ts", "js", "style", "vue", "svelte"):
            hex_raw += len(HEX.findall(text))
            var_tokens += text.count("var(--")
        sets_cache = lang != "style" and ("cache_control" in text or "cacheControl" in text)
        if sets_cache:
            cache_files.append(rel)
        if lang != "style" and LLM_CALL.search(text):
            llm_files += 1
            if not sets_cache:
                llm_nocache.append(rel)
        if lang != "style" and not is_test(rel):
            for name, line, n in func_lengths(lines, lang):
                funcs.append((n, f"{rel}:{line}", name))
            for dep in imports_of(rel, text, lang, files_set):
                a_mod, b_mod = module_of(rel, depth), module_of(dep, depth)
                if a_mod != b_mod:
                    edges[a_mod].add(b_mod)
        # duplication windows
        norm = []
        for i, l in enumerate(lines):
            s = " ".join(l.split())
            if len(s) >= 6 and not TRIVIAL.match(s):
                norm.append((i, s))
        meaningful_by_file[rel] = len(norm)
        line_count_for_dup += len(norm)
        for k in range(len(norm) - WINDOW + 1):
            h = hash(tuple(s for _, s in norm[k:k + WINDOW]))
            windows[h].append((rel, k))

    doc_words, doc_sizes = 0, []
    for p in docs:
        rel = str(p.relative_to(repo)).replace("\\", "/")
        try:
            if p.stat().st_size > 2_000_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        w = len(re.findall(r"[A-Za-z0-9][\w'-]*", text))
        doc_words += w
        doc_sizes.append((w, rel))
        for i, l in enumerate(text.splitlines(), 1):
            for mt in SLOP.finditer(l):
                slop_hits += 1
                slop_phrases[mt.group(0).lower()] += 1
                slop_files[rel] += 1
            emoji += len(EMOJI.findall(l))
    doc_sizes.sort(reverse=True)

    covered, pair = set(), Counter()
    for occ in windows.values():
        if len(occ) < 2 or len(occ) > 200:
            continue
        distinct = []
        for f, k in occ:
            if all(f != g or abs(k - j) >= WINDOW for g, j in distinct):
                distinct.append((f, k))
        if len(distinct) < 2:
            continue
        for f, k in distinct:
            for d in range(WINDOW):
                covered.add((f, k + d))
        fs = sorted({f for f, _ in distinct})
        for x in range(len(fs)):
            for y in range(x + 1, len(fs)):
                pair[(fs[x], fs[y])] += 1
            if len(fs) == 1:
                pair[(fs[0], fs[0])] += 1
    dup_pct = round(100 * len(covered) / line_count_for_dup, 1) if line_count_for_dup else 0

    sizes.sort(reverse=True)
    funcs.sort(reverse=True)
    total_loc = code_loc + test_loc
    nfun = len(funcs)
    cycles = tarjan({k: sorted(v) for k, v in edges.items()})
    fan_in = Counter(b for v in edges.values() for b in v)
    dup_caps = {w: sorted(m) for w, m in basenames.items() if len(m) >= 3}

    report = {
        "repo": str(repo),
        "loc": {"total": total_loc, "code": code_loc, "test": test_loc,
                "test_ratio": round(test_loc / code_loc, 3) if code_loc else 0,
                "by_language": per_lang.most_common(), "files": len(sizes)},
        "files_over": {str(t): sum(1 for n, _ in sizes if n > t) for t in (500, 1000, 2000)},
        "largest_files": sizes[:25],
        "modules": sorted(([k, v[0], v[1]] for k, v in per_mod.items()), key=lambda r: -r[2])[:40],
        "functions": {"count": nfun,
                      "over_75": sum(1 for n, _, _ in funcs if n > 75),
                      "over_150": sum(1 for n, _, _ in funcs if n > 150),
                      "share_over_75": round(sum(1 for n, _, _ in funcs if n > 75) / nfun, 3) if nfun else 0,
                      "longest": funcs[:25]},
        "duplication": {"pct_lines": dup_pct, "window": WINDOW,
                        "top_pairs": [[a, b, c] for (a, b), c in pair.most_common(20)]},
        "duplicate_capability_names": dup_caps,
        "module_graph": {"depth": depth, "edges": sum(len(v) for v in edges.values()),
                         "cycles": cycles[:20], "cycle_count": len(cycles),
                         "top_fan_in": fan_in.most_common(15),
                         "top_fan_out": sorted(((k, len(v)) for k, v in edges.items()), key=lambda r: -r[1])[:15]},
        "markers": dict(markers), "markers_per_kloc": round(sum(markers.values()) / (code_loc / 1000), 2) if code_loc else 0,
        "suppressions": dict(suppress), "suppressions_per_kloc": round(sum(suppress.values()) / (code_loc / 1000), 2) if code_loc else 0,
        "secrets": secrets[:50], "secret_hits": len(secrets),
        "llm": {"files_calling": llm_files, "files_without_cache_control": llm_nocache,
                "files_setting_cache_control": cache_files[:30],
                "note": "a call file without cache_control may rely on a shared helper listed in files_setting_cache_control; confirm before scoring"},
        "style": {"raw_hex_colors": hex_raw, "css_var_tokens": var_tokens},
        "slop": {"filler_hits": slop_hits,
                 "filler_per_kloc": round(slop_hits / (code_loc / 1000), 2) if code_loc else 0,
                 "top_phrases": slop_phrases.most_common(15), "top_files": slop_files.most_common(15),
                 "comment_ratio": round(comment_lines / code_loc, 3) if code_loc else 0,
                 "emoji": emoji, "doc_files": len(doc_sizes), "doc_words": doc_words,
                 "doc_words_per_kloc": round(doc_words / (code_loc / 1000)) if code_loc else 0,
                 "largest_docs": doc_sizes[:15]},
        "manifests": manifests,
        "git": git_stats(repo, days),
    }
    out_path.write_text(json.dumps(report, indent=1), encoding="utf-8")

    L, F, D, G = report["loc"], report["functions"], report["duplication"], report["git"]
    print(f"repo {repo.name}: {L['total']} LOC in {L['files']} files; test ratio {L['test_ratio']}")
    print("languages: " + ", ".join(f"{k} {v}" for k, v in L["by_language"][:6]))
    print(f"files over 500/1000/2000: {report['files_over']['500']}/{report['files_over']['1000']}/{report['files_over']['2000']}")
    print(f"functions {F['count']}: over 75 lines {F['over_75']} ({F['share_over_75']:.1%}), over 150 {F['over_150']}")
    print(f"duplication {D['pct_lines']}% of meaningful lines (windows of {WINDOW})")
    print(f"module cycles {report['module_graph']['cycle_count']} at depth {depth}; edges {report['module_graph']['edges']}")
    print("duplicate-capability name hits (leads, unconfirmed): " + (", ".join(f"{k}x{len(v)}" for k, v in dup_caps.items()) or "none"))
    print(f"markers/kLOC {report['markers_per_kloc']}, suppressions/kLOC {report['suppressions_per_kloc']}, secret hits {len(secrets)}")
    print(f"LLM call files {llm_files}, without cache_control {len(llm_nocache)}, files setting cache_control {len(cache_files)}")
    print(f"style: raw hex {hex_raw} vs var(--) {var_tokens}")
    W = report["slop"]
    print(f"slop: filler {W['filler_per_kloc']}/kLOC ({slop_hits}), comment ratio {W['comment_ratio']}, docs {doc_words} words in {len(doc_sizes)} files ({W['doc_words_per_kloc']}/kLOC), emoji {emoji}")
    if G:
        print(f"git: {G['commits']} commits in the {days} d before {G['last_commit']}, fix share {G['fix_share']:.0%}, reverts {G['reverts']}, total {G['commits_total']} since {G['first_commit']}")
    print(f"full report: {out_path.resolve()}")


if __name__ == "__main__":
    main()
