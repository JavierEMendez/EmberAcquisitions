"""Self-check for the acquisitions tab. Run it before shipping a change here.

    python acq_selfcheck.py

The acquisitions modules were lifted from a standalone Flask app, and every bug
this catches is one that already shipped at least once:

  names     `from acq_gis import *` silently skips leading-underscore names, so
            a route using one raises NameError only when that route is first
            hit. This is how the amenities, submarket, market and communities
            endpoints all started returning 500s after the port.
  imports   the lifted code kept importing helpers `from app`, which in the
            standalone app is where they live and here is a module that has
            none of them. Lazy imports meant nothing failed until an admin
            clicked Bootstrap.
  config    acq_routes.init_app() pulls its dependencies out of app.config, so
            the blueprint registers cleanly whether or not they are wired.
  routes    the blueprint must register, and every template it renders exist.

Exits non-zero if anything fails, so it can gate a commit.
"""
import ast
import builtins
import io
import os
import re
import sys

FAIL = []
OK = []


def check(label, ok, detail=""):
    (OK if ok else FAIL).append(label)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")


def _bound_names(tree):
    """Every name bound anywhere in the module (params, imports, assignments)."""
    bound = set()

    def args_of(a):
        return (a.posonlyargs + a.args + a.kwonlyargs +
                ([a.vararg] if a.vararg else []) + ([a.kwarg] if a.kwarg else []))

    class B(ast.NodeVisitor):
        def visit_FunctionDef(self, n):
            bound.add(n.name)
            bound.update(x.arg for x in args_of(n.args))
            self.generic_visit(n)
        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, n):
            bound.update(x.arg for x in args_of(n.args))
            self.generic_visit(n)

        def visit_ClassDef(self, n):
            bound.add(n.name)
            self.generic_visit(n)

        def visit_Name(self, n):
            if isinstance(n.ctx, (ast.Store, ast.Del)):
                bound.add(n.id)
            self.generic_visit(n)

        def visit_ExceptHandler(self, n):
            if n.name:
                bound.add(n.name)
            self.generic_visit(n)

        def visit_Import(self, n):
            bound.update((a.asname or a.name).split(".")[0] for a in n.names)

        def visit_ImportFrom(self, n):
            bound.update(a.asname or a.name for a in n.names if a.name != "*")

        def visit_Global(self, n):
            bound.update(n.names)

        def visit_comprehension(self, n):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    bound.add(t.id)
            self.generic_visit(n)

    B().visit(tree)
    return bound


def _loaded_names(tree):
    """Every bare name read (attribute bases only, not attribute names)."""
    used = set()

    class U(ast.NodeVisitor):
        def visit_Attribute(self, n):
            self.visit(n.value)

        def visit_Name(self, n):
            if isinstance(n.ctx, ast.Load):
                used.add(n.id)

    U().visit(tree)
    return used


print("acquisitions self-check\n")

# --- 1. every global resolves ------------------------------------------------
import acq_routes  # noqa: E402
import acq_gis     # noqa: E402
import acq_parcels  # noqa: E402
import acq_report  # noqa: E402

# acq_report was missing from this list, which is exactly how a report that
# called a function I had deleted reached production: nothing here looked at
# the module, so "name '_thesis' is not defined" only surfaced when someone
# pressed the button.
for mod in (acq_routes, acq_gis, acq_parcels, acq_report):
    name = mod.__name__
    tree = ast.parse(io.open(name + ".py", encoding="utf-8").read())
    unresolved = sorted(n for n in _loaded_names(tree) - _bound_names(tree)
                        if not hasattr(builtins, n) and n not in vars(mod))
    check(f"{name}: all globals resolve",
          not unresolved,
          f"NameError at runtime: {', '.join(unresolved[:8])}" if unresolved else "")

# --- 2. nothing imports from the portal's app module -------------------------
bad = []
for f in ("acq_gis.py", "acq_parcels.py", "acq_routes.py", "acq_store.py"):
    for i, line in enumerate(io.open(f, encoding="utf-8"), 1):
        if re.match(r"\s*from app import|\s*import app\b", line):
            bad.append(f"{f}:{i}")
check("no module imports helpers from the portal's app.py", not bad,
      " ".join(bad))

# --- 3. blueprint registers, with the config contract it declares ------------
try:
    from flask import Flask

    required = sorted(set(re.findall(r'app\.config\[[\'"](ACQ_[A-Z_]+)[\'"]\]',
                                     io.open("acq_routes.py", encoding="utf-8").read())))
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update({k: (lambda *a, **k: None) for k in required})
    acq_routes.init_app(app)
    routes = [str(r) for r in app.url_map.iter_rules() if "acq" in str(r)]
    check("blueprint registers", len(routes) > 50, f"{len(routes)} acquisition routes")

    supplied = set(re.findall(r'[\'"](ACQ_[A-Z_]+)[\'"]', io.open("app.py", encoding="utf-8").read()))
    absent = [k for k in required if k not in supplied]
    check("app.py supplies every config key init_app requires", not absent,
          ", ".join(absent))
except Exception as e:
    check("blueprint registers", False, f"{type(e).__name__}: {e}")

# --- 4. every endpoint the front end calls actually exists -------------------
# The map page was once a from-scratch rewrite rather than a port: it had 21 of
# the real page's 128 functions and called none of its 41 endpoints. Nothing
# caught that, because every module imported and every route registered — the
# page simply never asked for any of it. This compares the two directions.
def _normalise(url):
    u = url.split("?")[0]
    u = re.sub(r"\$\{[^}]*\}", "<x>", u)
    return u.rstrip("/") or "/"


def _matches(call, rule):
    a = [p for p in _normalise(call).strip("/").split("/") if p]
    b = [p for p in rule.strip("/").split("/") if p]
    if len(a) != len(b):
        return False
    return all(y.startswith("<") or x == "<x>" or x == y for x, y in zip(a, b))


try:
    front = ""
    for f in ("static/js/acquisitions.js", "static/js/acquisitions_project.js"):
        if os.path.exists(f):
            front += io.open(f, encoding="utf-8").read()
    for t in os.listdir("templates"):
        if t.startswith("acquisitions"):
            front += io.open(os.path.join("templates", t), encoding="utf-8").read()
    calls = sorted(set(re.findall(r"""fetch\(\s*[`'"]([^`'"]+)""", front)))
    rules = [str(r) for r in app.url_map.iter_rules()]
    unreachable = [c for c in calls
                   if c.startswith("/") and not any(_matches(c, r) for r in rules)]
    check(f"all {len(calls)} endpoints the front end calls exist",
          not unreachable, ", ".join(unreachable[:4]))
except Exception as e:
    check("all endpoints the front end calls exist", False, f"{type(e).__name__}: {e}")

# --- 5. every rendered template exists ---------------------------------------
src = io.open("acq_routes.py", encoding="utf-8").read()
missing = [t for t in set(re.findall(r'render_template\(\s*["\']([^"\']+)["\']', src))
           if not os.path.exists(os.path.join("templates", t))]
check("every rendered template exists", not missing, ", ".join(missing))

print(f"\n{len(OK)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
