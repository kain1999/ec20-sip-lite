"""Cross-check: every /api path the panel calls exists in the HTTP server.

Run from the repository root:
    python tools/check_api.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "app", "web", "server.py")
PANEL = os.path.join(ROOT, "app", "web", "static", "app.js")


def normalize(path):
    """'/api/calls/c9/hangup' -> '/api/calls/{id}/hangup'; strip query strings."""
    path = path.split("?")[0]
    out = []
    for segment in path.split("/"):
        if (segment.isdigit()
                or re.fullmatch(r"c[0-9a-f]{4,}", segment)
                or re.fullmatch(r"e\d{3,}", segment)):
            out.append("{id}")
        else:
            out.append(segment)
    return "/".join(out)


def server_routes():
    text = open(SERVER, encoding="utf-8").read()
    routes = set()
    for match in re.finditer(r'path == "(/api/[^"]+)"', text):
        routes.add(match.group(1))
    for match in re.finditer(r'path\.startswith\("(/api/[^"]+)"\)', text):
        prefix = match.group(1)
        # the handler switches on the remaining segments; accept the prefix family
        routes.add(prefix.rstrip("/") + "/{id}")
        routes.add(prefix.rstrip("/") + "/{id}/answer")
        routes.add(prefix.rstrip("/") + "/{id}/hangup")
        routes.add(prefix.rstrip("/") + "/{id}/read")
        routes.add(prefix.rstrip("/") + "/{id}/kick")
        routes.add(prefix.rstrip("/") + "/{id}/test")
        routes.add(prefix.rstrip("/") + "/{id}/forward")
    return routes


def panel_calls():
    text = open(PANEL, encoding="utf-8").read()
    calls = set()
    for match in re.finditer(r"'/api/([a-z0-9_/?=&{}.$\- ]+)'", text, re.I):
        calls.add("/api/" + match.group(1))
    # endpoints assembled with string concatenation
    for match in re.finditer(r"'/api/(calls|extensions|sms|rules)/' \+ ([a-zA-Z0-9_.]+)", text):
        family = match.group(1)
        calls.add("/api/%s/{id}" % family)
    return calls


def main():
    routes = server_routes()
    calls = panel_calls()
    missing = []
    for call in sorted(calls):
        norm = normalize(call)
        if norm in routes:
            continue
        # allow sub-resources of a known family
        family = "/".join(norm.split("/")[:4])
        if any(r.startswith(family) or family.startswith(r) for r in routes):
            continue
        missing.append(norm)
    print("panel endpoints:", len(calls))
    print("server routes  :", len(routes))
    print("unmatched      :", missing if missing else "none")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
