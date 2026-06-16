"""
Serveur local de dev (Python pur, zéro dépendance externe) pour tester l'API
FENIX dans un navigateur SANS Azure Functions Core Tools ni Azurite.

Il sert EXACTEMENT les mêmes handlers que function_app.py, sur
http://localhost:8000/api/...

Auth données : DefaultAzureCredential (donc `az login` requis pour lire/écrire
le vrai parquet preprod). Sans auth, la VALIDATION (400/403/404) marche déjà ;
seules les réponses 200 avec données réelles nécessitent `az login` + RBAC
Storage Blob Data Contributor sur stfenixforecast.

Usage :
  python scripts/local_server.py
  → ouvre http://localhost:8000/api/grdf/droits-acces dans le navigateur (GET)
  → POST/PATCH/DELETE/retry via curl ou Invoke-RestMethod

Les GET passent au navigateur ; les autres méthodes pas (le browser ne fait que
des GET) → utiliser curl/PowerShell pour celles-là.
"""

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

# Charger local.settings.json (Values) dans l'env AVANT d'importer les handlers
# (Config lit les variables d'env à l'import). Optionnel : Config a des défauts.
_settings = os.path.join(_ROOT, "local.settings.json")
if os.path.exists(_settings):
    with open(_settings, encoding="utf-8") as f:
        for key, val in (json.load(f).get("Values") or {}).items():
            if not key.startswith("_"):
                os.environ.setdefault(key, str(val))
    print(f"[local] local.settings.json chargé ({_settings})")
else:
    print("[local] Pas de local.settings.json → défauts Config (stfenixforecast/fenixlake/grdf)")

from api.consommations import handle_consommations           # noqa: E402
from api.grdf_droits_acces import (                           # noqa: E402
    handle_create,
    handle_get,
    handle_list,
    handle_patch,
    handle_retry,
    handle_revoke,
)


class Req:
    """Shim minimal compatible avec nos handlers (headers / get_json / params)."""

    def __init__(self, headers: dict, body: bytes, params: dict):
        self.headers = headers
        self._body = body
        self.params = params

    def get_json(self):
        if not self._body:
            return None
        try:
            return json.loads(self._body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("invalid json")


_ID = r"(?P<id_pce>[^/]+)"
ROUTES = [
    ("GET", re.compile(r"^/api/grdf/droits-acces$"), lambda req, m: handle_list(req)),
    ("POST", re.compile(r"^/api/grdf/droits-acces$"), lambda req, m: handle_create(req)),
    ("POST", re.compile(rf"^/api/grdf/droits-acces/{_ID}/retry$"), lambda req, m: handle_retry(req, m["id_pce"])),
    ("GET", re.compile(rf"^/api/grdf/droits-acces/{_ID}$"), lambda req, m: handle_get(req, m["id_pce"])),
    ("PATCH", re.compile(rf"^/api/grdf/droits-acces/{_ID}$"), lambda req, m: handle_patch(req, m["id_pce"])),
    ("DELETE", re.compile(rf"^/api/grdf/droits-acces/{_ID}$"), lambda req, m: handle_revoke(req, m["id_pce"])),
    ("GET", re.compile(r"^/api/consommations$"), lambda req, m: handle_consommations(req)),
]


class Handler(BaseHTTPRequestHandler):
    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        req = Req(dict(self.headers), body, params)

        for route_method, rx, fn in ROUTES:
            if route_method != method:
                continue
            match = rx.match(path)
            if match:
                try:
                    resp, status = fn(req, match.groupdict())
                except Exception as e:  # remonte l'erreur (ex: auth Azure absente)
                    resp, status = {"erreur": "ERREUR_INTERNE", "message": f"{type(e).__name__}: {e}"}, 500
                self._send(status, resp)
                return

        self._send(404, {"erreur": "ROUTE_INTROUVABLE", "message": f"{method} {path}"})

    def _send(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def log_message(self, fmt, *args):
        print("[local]", fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("LOCAL_PORT", "8000"))
    print(f"[local] FENIX API → http://localhost:{port}/api/...")
    print("[local] GET dans le navigateur ; POST/PATCH/DELETE via curl / Invoke-RestMethod.")
    print("[local] Ctrl+C pour arrêter.\n")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
