"""check_cycles.py: the module map in spec/ARCHITECTURE.md against the real imports in the code.

ARCHITECTURE.md format: one section per module; any other line in it (public API, notes) is free text.
    ## Module <name>
    Paths: src/shop/core/, src/shop/money.py
    Depends on: base, util
    Public: shop.core, shop.money.cents
Paths: repo-relative, comma-separated; a trailing / is a directory; * ? [ make a glob; a file belongs to the module
whose matching path is the longest. Depends on: the modules it may import, comma-separated; - or none for none.
Public (optional, Python): the dotted modules and names other modules may import; a whole module listed allows every
name in it. Without it, a module's public face is the __init__.py of each Paths directory and each Paths file, and an
__init__.py with a literal __all__ exports only those names. A Public entry deeper than a Paths directory whose
__init__.py has a literal __all__ must name a submodule or name in that __all__, and no entry may be _private.
Fails, printing `file:line: message` per violation, on a malformed module section, a cycle in the declared graph,
a real import between modules that the declared deps do not allow, a cycle in the real module graph, an import
cycle between Python files, and a Python import into another module past its public API (Public, else its face).
Real imports: Python (ast, exact; `if TYPE_CHECKING:` imports are left out of file cycles), Rust
(`use crate::|super::|self::`, `mod x;`, workspace crate paths, Cargo path and workspace deps) and JS/TS
(import, export from, require, import()); Rust and JS are best effort.
    py -3 check_cycles.py [--repo R] [--arch spec/ARCHITECTURE.md] [path ...]
Paths (overhaul.toml runs `check_cycles.py src`) limit the scan to files under them, and a source file there that
no module owns is a violation; without paths every owned file is scanned. Exit 0 clean, 1 violations.
"""
import argparse
import ast
import fnmatch
import json
import os
import posixpath
import re
import sys
import tomllib

JS = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")
SRC = (".py", ".rs") + JS
MANIFESTS = ("Cargo.toml", "package.json")
MOD_RE = re.compile(r"##\s+Module:?\s+`?([A-Za-z0-9_.\-]+)`?")
KEY_RE = re.compile(r"\s*(?:[-*]\s+)?\**\s*(paths|depends on|public)\s*\**\s*:\s*\**(.*)$", re.I)
RS_MOD = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+([A-Za-z_]\w*)\s*;", re.M)
RS_PATH = re.compile(r"\b(crate|super|self)(?=\s*::)((?:\s*::\s*[A-Za-z_]\w*)*)(\s*::\s*\{([^{}]*)\})?")
RS_EXT = re.compile(r"\b([A-Za-z_]\w*)\s*::")
JS_RE = re.compile(r"""(?:\b(?:import|export)\b[\w\s{},*$]*?\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)"""
                   r"""(['"])([^'"\n]+)\1""")


def load_toml(path):
    """The parsed TOML file, or None when it is missing or invalid."""
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def conf_path(repo, key, default):
    """overhaul.toml [paths].<key>, or the default."""
    val = ((load_toml(os.path.join(repo, "overhaul.toml")) or {}).get("paths") or {}).get(key)
    return val if isinstance(val, str) and val.strip() else default


def read(path):
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def join(*parts):
    """Normalised posix join of repo-relative parts; '' is the repo root."""
    path = posixpath.normpath("/".join(p for p in parts if p)) if any(parts) else ""
    return "" if path == "." else path


def norm_path(text):
    path = text.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return "" if path in (".", "/") else path


def first(fset, cands):
    return next((c for c in cands if c in fset), None)


def walk(repo):
    """Repo files as posix relative paths, skipping dot dirs, caches, node_modules, venvs and cargo targets."""
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if not (
            d.startswith(".") or d in ("node_modules", "__pycache__", "site-packages")
            or os.path.isfile(os.path.join(root, d, "pyvenv.cfg"))
            or os.path.isfile(os.path.join(root, d, "CACHEDIR.TAG"))))
        rel = os.path.relpath(root, repo).replace(os.sep, "/")
        out += [n if rel == "." else f"{rel}/{n}" for n in sorted(files)]
    return out


def parse_architecture(text):
    """({module: {"line", "paths": [(pattern, line)], "deps": [(module, line)], "keys"}}, [(line, problem)])."""
    mods, probs, cur, fence = {}, [], None, False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        if re.match(r"#{1,2}(\s|$)", line):
            m = MOD_RE.match(line)
            cur = None
            if m and m.group(1) in mods:
                probs.append((n, f"module {m.group(1)} is defined twice"))
            elif m:
                cur = mods[m.group(1)] = {"line": n, "paths": [], "deps": [], "public": [], "keys": set()}
            continue
        k = KEY_RE.match(line) if cur else None
        if not k:
            continue
        key = k.group(1).lower()
        cur["keys"].add(key)
        items = [i.replace("`", " ").split() for i in k.group(2).split(",")]
        items = [i[0] for i in items if i and i[0].lower() not in ("-", "none")]
        if key == "paths":
            cur["paths"] += [(norm_path(i), n) for i in items]
        elif key == "public":
            cur["public"] += [(i, n) for i in items]
        else:
            cur["deps"] += [(i, n) for i in items]
    claimed = {}
    for name, mod in mods.items():
        for key in ("paths", "depends on"):
            if key not in mod["keys"]:
                probs.append((mod["line"], f"module {name} has no '{key.capitalize()}:' line"))
        if "paths" in mod["keys"] and not mod["paths"]:
            probs.append((mod["line"], f"module {name} lists no paths"))
        for path, n in mod["paths"]:
            other = claimed.setdefault(path.rstrip("/"), name)
            if other != name:
                probs.append((n, f"path {path} is claimed by modules {other} and {name}"))
        for dep, n in mod["deps"]:
            if dep == name:
                probs.append((n, f"module {name} depends on itself"))
            elif dep not in mods:
                probs.append((n, f"module {name} depends on unknown module {dep}"))
    return mods, sorted(probs)


def owner(mods, rel):
    """The module whose longest Paths entry matches the repo-relative file, or None."""
    best, size = None, -1
    for name, mod in mods.items():
        for path, _ in mod["paths"]:
            pat = path.rstrip("/")
            if any(c in pat for c in "*?["):
                hit = fnmatch.fnmatchcase(rel, pat)
            else:
                hit = pat == "" or rel == pat or rel.startswith(pat + "/")
            if hit and len(pat) > size:
                best, size = name, len(pat)
    return best


def cycles(graph):
    """One cycle [a, b, ..., a] per strongly connected component that has one; graph = {node: successors}."""
    index, low, on, stack, found = {}, {}, set(), [], []
    for root in sorted(graph):
        if root in index:
            continue
        index[root] = low[root] = len(index)
        stack.append(root)
        on.add(root)
        work = [(root, iter(sorted(graph.get(root, ()))))]
        while work:
            v, succ = work[-1]
            for w in succ:
                if w not in index:
                    index[w] = low[w] = len(index)
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = set()
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.add(w)
                        if w == v:
                            break
                    if len(comp) > 1 or v in graph.get(v, ()):
                        found.append(path_in(graph, comp))
    return found


def path_in(graph, comp):
    """A shortest cycle through the smallest node of a strongly connected component."""
    start = min(comp)
    prev, queue = {start: None}, [start]
    for v in queue:
        for w in sorted(graph.get(v, ())):
            if w == start:
                path = [start]
                while v is not None:
                    path.append(v)
                    v = prev[v]
                return path[::-1]
            if w in comp and w not in prev:
                prev[w] = v
                queue.append(w)
    return [start, start]


def py_index(pyfiles):
    """Dotted module name -> [(root, file)] over the repo root, src/ and every package or script root."""
    fset, roots, idx = set(pyfiles), {"", "src"}, {}
    for f in pyfiles:
        d = posixpath.dirname(f)
        while d and f"{d}/__init__.py" in fset:
            d = posixpath.dirname(d)
        roots.add(d)
    for f in pyfiles:
        for r in roots:
            if r and not f.startswith(r + "/"):
                continue
            mod = f[len(r) + 1 if r else 0:-3].replace("/", ".")
            mod = mod[:-9] if mod.endswith(".__init__") else mod
            if mod and mod != "__init__":
                idx.setdefault(mod, []).append((r, f))
    return idx


def py_resolve(idx, dotted, rel):
    """The repo file an absolute import reaches (deepest module first), or None for stdlib and third party."""
    parts = dotted.split(".")
    if parts[0] in sys.stdlib_module_names:
        return None
    for k in range(len(parts), 0, -1):
        cands = idx.get(".".join(parts[:k]))
        if cands:
            for want in (posixpath.dirname(rel), "", "src"):
                for r, f in cands:
                    if r == want:
                        return f
            return sorted(cands)[0][1]
    return None


def py_edges(rel, text, fset, idx):
    """([(line, target file, type_only)], parse problem or None) for one Python file."""
    try:
        tree = ast.parse(text, rel)
    except (SyntaxError, ValueError) as err:
        return [], (getattr(err, "lineno", None) or 1, f"cannot parse: {getattr(err, 'msg', err)}")
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "TYPE_CHECKING" in (getattr(node.test, "id", ""),
                                                            getattr(node.test, "attr", "")):
            guarded.update(id(sub) for stmt in node.body for sub in ast.walk(stmt))
    edges = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets = [(py_resolve(idx, a.name, rel), None) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level:
            base = posixpath.dirname(rel)
            for _ in range(node.level - 1):
                base = posixpath.dirname(base)
            pkg = join(base, (node.module or "").replace(".", "/"))
            tail = [pkg + ".py", join(pkg, "__init__.py")] if node.module else [join(pkg, "__init__.py")]
            targets = []
            for a in node.names:
                sub = first(fset, [join(pkg, a.name + ".py"), join(pkg, a.name, "__init__.py")])
                targets.append((sub, None) if sub else (first(fset, tail), a.name))
        elif isinstance(node, ast.ImportFrom):
            whole = py_resolve(idx, node.module, rel)
            targets = [(t, a.name if t == whole else None)
                       for a in node.names for t in [py_resolve(idx, f"{node.module}.{a.name}", rel)]]
        else:
            continue
        edges += [(node.lineno, t, id(node) in guarded, name) for t, name in targets if t and t != rel]
    return edges, None


def py_public(idx, entry):
    """(file, name or None) that a dotted Public entry names, or None when it is no module in the tree."""
    parts = entry.split(".")
    for k in range(len(parts), 0, -1):
        if ".".join(parts[:k]) in idx:
            if k < len(parts) - 1:
                return None
            return py_resolve(idx, ".".join(parts[:k]), ""), (parts[k] if k < len(parts) else None)
    return None


def top_names(text):
    """Names bound at module level: defs, classes, assignments and imports."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in getattr(node, "targets", [getattr(node, "target", None)]):
                names.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


def dunder_all(text):
    """The literal names in a module-level __all__, or None when there is none."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in node.targets)
                and isinstance(node.value, (ast.List, ast.Tuple))):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return None


def face_all(repo, alls, face):
    """The cached literal __all__ of a face __init__.py, or None."""
    if face not in alls:
        alls[face] = dunder_all(read(os.path.join(repo, face)))
    return alls[face]


def private_entry(repo, mod, hit, entry, alls):
    """Why a Public entry exposes a private name: a _name, or a name past a face __all__; None when it is public."""
    if any(part.startswith("_") and not part.startswith("__") for part in entry.split(".")):
        return "is private (a _name); export a public name instead"
    tgt, name = hit
    for p, _ in mod["paths"]:
        face = p + "__init__.py"
        if not p.endswith("/") or not tgt.startswith(p) or tgt == face:
            continue
        exported = face_all(repo, alls, face)
        sub = tgt[len(p):].split("/")[0].removesuffix(".py")
        if exported is not None and sub not in exported and (name is None or name not in exported):
            return (f"reaches past its face {face}: its __all__ has neither {sub} nor {name or sub}; "
                    f"export it there and import it from the face")
    return None


def public_problems(repo, mods, idx, arch, py_edges_out):
    """Python imports into another module that miss its Public entries (or, without them, its face files)."""
    out, listed, alls = [], {}, {}
    for name, mod in mods.items():
        if mod["public"]:
            listed[name] = set()
        for entry, n in mod["public"]:
            hit = py_public(idx, entry)
            if hit and owner(mods, hit[0]) == name and (
                    hit[1] is None or hit[1] in top_names(read(os.path.join(repo, hit[0])))):
                why = private_entry(repo, mod, hit, entry, alls)
                if why:
                    out.append(f"{arch}:{n}: Public entry {entry} of module {name} {why}")
                    continue
                listed[name].add(hit)
            elif any(owner(mods, f) == name for cands in idx.values() for _, f in cands):
                out.append(f"{arch}:{n}: Public entry {entry} of module {name} is no module or name it owns")
    for src, line, tgt, b, name in py_edges_out:
        if b in listed:
            ok = (tgt, None) in listed[b] or (tgt, name) in listed[b] or (
                name in (None, "*") and any(f == tgt for f, _ in listed[b]))
        else:
            face = any(p.rstrip("/") == tgt or (p.endswith("/") and tgt == p + "__init__.py")
                       for p, _ in mods[b]["paths"])
            if face and name not in (None, "*") and tgt.endswith("__init__.py"):
                exported = face_all(repo, alls, tgt)
                face = exported is None or name in exported
            ok = face
        if not ok:
            what = f"{name} from {tgt}" if name else tgt
            out.append(f"{src}:{line}: imports {what}, past the public API of module {b}; "
                       f"use a name module {b} exports, or add it to its Public: line in {arch}")
    return out


def strip_comments(text, quotes):
    """Blank // and /* */ comments, keeping strings and line numbers."""
    out, i, n, q = [], 0, len(text), None
    while i < n:
        c = text[i]
        if q:
            out.append(text[i:i + 2] if c == "\\" else c)
            i += 2 if c == "\\" else 1
            q = None if c == q else q
        elif c in quotes:
            q = c
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * text.count("\n", i, j))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def rs_file(fset, src, segs):
    """The deepest .rs file a module path reaches from the crate's src dir (the crate root when empty)."""
    found, d = first(fset, [f"{src}/lib.rs", f"{src}/main.rs"]), src
    for seg in segs:
        hit = first(fset, [f"{d}/{seg}.rs", f"{d}/{seg}/mod.rs"])
        if not hit:
            break
        found, d = hit, f"{d}/{seg}"
    return found


def rs_edges(rel, text, fset, crates):
    code, edges = strip_comments(text, '"'), []
    cdir = posixpath.dirname(rel)
    while cdir and join(cdir, "Cargo.toml") not in fset:
        cdir = posixpath.dirname(cdir)
    src = join(cdir, "src") if join(cdir, "Cargo.toml") in fset else None

    def add(pos, target):
        if target and target != rel:
            edges.append((code.count("\n", 0, pos) + 1, target, False))

    if src and rel.startswith(src + "/"):
        here = rel[len(src) + 1:-3].split("/")
        here = here[:-1] if here[-1] == "mod" or here in (["lib"], ["main"]) else here
        for m in RS_MOD.finditer(code):
            add(m.start(1), rs_file(fset, src, here + [m.group(1)]))
        for m in RS_PATH.finditer(code):
            segs = [] if m.group(1) == "crate" else here[:-1] if m.group(1) == "super" else list(here)
            rest = [s.strip() for s in m.group(2).split("::") if s.strip()]
            while rest and rest[0] in ("super", "self"):
                segs = segs[:-1] if rest.pop(0) == "super" else segs
            for item in (m.group(4).split(",") if m.group(4) else [""]):
                tail = []
                for tok in (t.strip() for t in item.split("::")):
                    if not re.fullmatch(r"[A-Za-z_]\w*", tok) or tok == "self":
                        break
                    tail.append(tok)
                add(m.start(1), rs_file(fset, src, segs + rest + tail))
    for m in RS_EXT.finditer(code):
        tgt = crates.get(m.group(1))
        if tgt and not (src and tgt.startswith(src + "/")):
            add(m.start(1), tgt)
    return edges


def js_edges(rel, text, fset, pkgs):
    code, edges = strip_comments(text, "'\"`"), []
    for m in JS_RE.finditer(code):
        spec = m.group(2)
        if spec.startswith("."):
            base = join(posixpath.dirname(rel), spec)
            stem, ext = posixpath.splitext(base)
            cands = [base] + [base + e for e in JS] + [f"{base}/index{e}" for e in JS]
            tgt = first(fset, cands + ([stem + e for e in JS] if ext in JS else []))
        else:
            parts = spec.split("/")
            tgt = pkgs.get("/".join(parts[:2]) if spec.startswith("@") else parts[0])
        if tgt and tgt != rel:
            edges.append((code.count("\n", 0, m.start(2)) + 1, tgt, False))
    return edges


def manifests(repo, files, fset):
    """Workspace crates by name and by dir (-> lib.rs, or Cargo.toml) and JS packages by name (-> package.json)."""
    crates, crate_dirs, pkgs = {}, {}, {}
    for f in files:
        base, d = posixpath.basename(f), posixpath.dirname(f)
        if base == "Cargo.toml":
            data = load_toml(os.path.join(repo, f)) or {}
            tgt = first(fset, [join(d, "src", "lib.rs"), join(d, "src", "main.rs")]) or f
            crate_dirs[d] = tgt
            for name in ((data.get("package") or {}).get("name"), (data.get("lib") or {}).get("name")):
                if isinstance(name, str):
                    crates[name.replace("-", "_")] = tgt
        elif base == "package.json":
            try:
                name = json.loads(read(os.path.join(repo, f))).get("name")
            except (ValueError, AttributeError):
                name = None
            if isinstance(name, str):
                pkgs[name] = f
    return crates, crate_dirs, pkgs


def cargo_edges(rel, text, crates, crate_dirs):
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []
    d, lines, edges = posixpath.dirname(rel), text.splitlines(), []
    for table in ("dependencies", "build-dependencies"):
        for name, spec in (data.get(table) or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            if isinstance(spec.get("path"), str):
                pdir = join(d, spec["path"])
                tgt = crate_dirs.get(pdir, join(pdir, "Cargo.toml"))
            elif spec.get("workspace"):
                tgt = crates.get(str(spec.get("package", name)).replace("-", "_"))
            else:
                continue
            line = next((i for i, text_line in enumerate(lines, 1)
                         if re.match(rf'\s*"?{re.escape(name)}"?\s*[.=]', text_line)), 1)
            if tgt:
                edges.append((line, tgt, False))
    return edges


def check(repo, arch=None, roots=None):
    """(violations as 'file:line: message', modules, files scanned)."""
    arch = arch or conf_path(repo, "architecture", "spec/ARCHITECTURE.md")
    apath = os.path.join(repo, arch)
    if not os.path.isfile(apath):
        return [f"{arch}:1: missing; it declares each module's Paths and Depends on"], 0, 0
    mods, probs = parse_architecture(read(apath))
    out = [f"{arch}:{n}: {msg}" for n, msg in probs]
    if not mods:
        return out + [f"{arch}:1: no '## Module <name>' sections"], 0, 0
    allowed = {m: {d for d, _ in v["deps"] if d in mods and d != m} for m, v in mods.items()}
    for cyc in cycles(allowed):
        out.append(f"{arch}:{mods[cyc[0]]['line']}: declared dependency cycle: {' -> '.join(cyc)}")
    files = walk(repo)
    fset = set(files)
    scan = [f for f in files if f.endswith(SRC) or posixpath.basename(f) in MANIFESTS]
    if roots:
        tops = [norm_path(r).rstrip("/") for r in roots]
        scan = [f for f in scan if any(t == "" or f == t or f.startswith(t + "/") for t in tops)]
        out += [f"{f}:1: owned by no module in {arch}" for f in scan if f.endswith(SRC) and not owner(mods, f)]
    scan = [f for f in scan if owner(mods, f)]
    idx = py_index([f for f in files if f.endswith(".py")])
    crates, crate_dirs, pkgs = manifests(repo, files, fset)
    edges = []
    for f in scan:
        text = read(os.path.join(repo, f))
        if f.endswith(".py"):
            found, err = py_edges(f, text, fset, idx)
            if err:
                out.append(f"{f}:{err[0]}: {err[1]}")
        elif f.endswith(".rs"):
            found = rs_edges(f, text, fset, crates)
        elif f.endswith(JS):
            found = js_edges(f, text, fset, pkgs)
        elif f.endswith("Cargo.toml"):
            found = cargo_edges(f, text, crates, crate_dirs)
        else:
            found = []
        edges += [(f, line, tgt, tonly, rest[0] if rest else None) for line, tgt, tonly, *rest in found]
    scanned, mgraph, fgraph, cross_py = set(scan), {}, {}, []
    for src, line, tgt, tonly, name in edges:
        a, b = owner(mods, src), owner(mods, tgt)
        if b and a != b and src.endswith(".py") and tgt.endswith(".py"):
            cross_py.append((src, line, tgt, b, name))
        if b and a != b:
            mgraph.setdefault(a, {}).setdefault(b, f"{src}:{line}")
            if b not in allowed[a]:
                out.append(f"{src}:{line}: module {a} imports {b} ({tgt}); {a} may depend on: "
                           f"{', '.join(sorted(allowed[a])) or '-'}")
        if src.endswith(".py") and tgt.endswith(".py") and tgt in scanned and not tonly:
            fgraph.setdefault(src, {}).setdefault(tgt, line)
    for cyc in cycles(mgraph):
        out.append(f"{mgraph[cyc[0]][cyc[1]]}: import cycle between modules: {' -> '.join(cyc)}")
    for cyc in cycles(fgraph):
        out.append(f"{cyc[0]}:{fgraph[cyc[0]][cyc[1]]}: import cycle: {' -> '.join(cyc)}")
    out += public_problems(repo, mods, idx, arch, cross_py)
    return list(dict.fromkeys(out)), len(mods), len(scan)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="scan only under these repo-relative paths; unowned source fails")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--arch", help="default: overhaul.toml [paths].architecture, else spec/ARCHITECTURE.md")
    args = ap.parse_args(argv)
    out, nmods, nfiles = check(args.repo, args.arch, args.paths)
    sys.stdout.reconfigure(errors="replace")
    last = f"CYCLES FAIL {len(out)} problems" if out else f"CYCLES OK {nmods} modules, {nfiles} files"
    print("\n".join(out + [last]))
    return 1 if out else 0


if __name__ == "__main__":
    sys.exit(main())
