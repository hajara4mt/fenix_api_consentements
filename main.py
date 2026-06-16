"""
FENIX API — application FastAPI (ASGI), point d'entrée de production.

Déploiement : Azure **App Service Plan** (Linux, Python).
  Startup Command : gunicorn -w 4 -k uvicorn.workers.UvicornWorker \
                    --bind 0.0.0.0:8000 --timeout 600 main:app

Lancement local :
  python -m uvicorn main:app --reload
  → http://127.0.0.1:8000/docs  (Swagger : teste TOUTES les routes)
  → http://127.0.0.1:8000/api/grdf/droits-acces  (GET direct)

Auth ADLS via DefaultAzureCredential :
  - En local : `az login` (+ RBAC Storage Blob Data Contributor sur le compte).
  - Sur App Service : Managed Identity de l'App Service (+ même rôle RBAC).
  Sans auth, la validation (400/403/404) répond ; les 200 avec données réelles
  échouent en 500.

Config : variables d'environnement (App Settings côté App Service). En local,
main.py charge local.settings.json si présent (sinon défauts de shared.config).
Les routes appellent les mêmes handlers que les tests (api/).
"""

import json
import os
import sys
from typing import Optional

# --- Charger local.settings.json dans l'env AVANT d'importer les handlers ---
# (shared.config lit les variables d'env à l'import). Optionnel : Config a des
# défauts (stfenixforecast / fenixlake / grdf).
_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
_settings = os.path.join(_ROOT, "local.settings.json")
if os.path.exists(_settings):
    with open(_settings, encoding="utf-8") as _f:
        for _k, _v in (json.load(_f).get("Values") or {}).items():
            if not _k.startswith("_"):
                os.environ.setdefault(_k, str(_v))

# Le code Enedis (shared/settings.py) attend AZURE_STORAGE_CONNECTION_STRING ;
# on l'alias sur STORAGE_CONNECTION_STRING pour ne pas dupliquer le secret.
if not os.environ.get("AZURE_STORAGE_CONNECTION_STRING") and os.environ.get("STORAGE_CONNECTION_STRING"):
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = os.environ["STORAGE_CONNECTION_STRING"]

from fastapi import FastAPI, Query, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from api.consommations import handle_consommations  # noqa: E402
from api.enedis_consent import (  # noqa: E402
    handle_create_consent,
    handle_get_consent,
    handle_list_consents,
    handle_patch_consent,
    handle_retry_consent,
    handle_revoke_consent,
)
from api.grdf_droits_acces import (  # noqa: E402
    handle_create,
    handle_get,
    handle_list,
    handle_patch,
    handle_retry,
    handle_revoke,
)

app = FastAPI(
    title="FENIX API — dev local",
    description="Route déclaration , suivi consentement & récuperation de données GRDF ENEDIS - Préprod .",
    version="dev",
)


# ----------------------------------------------------------------------
# Exception handlers : aligner les erreurs FastAPI sur NOTRE contrat
# (sinon body malformé → 422 {detail}, route inconnue → 404 {detail})
# ----------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    # Champ hors schéma (extra="forbid") → notre 400 CHAMP_INCONNU (avec le champ)
    for err in exc.errors():
        if err.get("type") == "extra_forbidden":
            loc = err.get("loc") or []
            champ = loc[-1] if loc else None
            return JSONResponse(
                status_code=400,
                content={
                    "erreur": "CHAMP_INCONNU",
                    "message": f"Le champ {champ} n'est pas reconnu (hors schéma).",
                    "champ": champ,
                },
            )
    # Corps JSON / type de champ malformé → notre 400 CHAMP_INVALIDE
    return JSONResponse(
        status_code=400,
        content={
            "erreur": "CHAMP_INVALIDE",
            "message": "Requête invalide (corps JSON ou paramètre malformé).",
        },
    )


@app.exception_handler(StarletteHTTPException)
async def _on_http_error(request: Request, exc: StarletteHTTPException):
    # 404 route inconnue / 405 méthode non autorisée → notre forme {erreur, message}
    codes = {404: "ROUTE_INTROUVABLE", 405: "METHODE_NON_AUTORISEE"}
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "erreur": codes.get(exc.status_code, "ERREUR_HTTP"),
            "message": str(exc.detail),
        },
    )


class _Req:
    """Shim compatible avec nos handlers (headers / get_json / params)."""

    def __init__(self, headers: dict, json_body, params: dict):
        self.headers = headers
        self._json = json_body
        self.params = params

    def get_json(self):
        return self._json


def _resp(result) -> JSONResponse:
    body, status = result
    return JSONResponse(content=body, status_code=status)


# ----------------------------------------------------------------------
# Schémas du body (Swagger affiche les champs + leur type).
# Tous Optional : la validation métier (obligatoires, formats, règles croisées)
# reste faite par les handlers. extra="forbid" → schéma propre (pas de
# additionalProp dans /docs) et tout champ inconnu est rejeté ; l'exception
# handler le remappe en notre 400 CHAMP_INCONNU (avec le nom du champ).
# ----------------------------------------------------------------------

class DroitAccesCreate(BaseModel):
    """Body de POST /grdf/droits-acces (déclaration d'un PCE)."""
    model_config = ConfigDict(extra="forbid")

    id_pce: Optional[str] = None
    partner: Optional[str] = None
    courriel_titulaire: Optional[str] = None
    code_postal: Optional[str] = None
    date_debut_droit_acces: Optional[str] = None
    date_fin_droit_acces: Optional[str] = None
    perim_donnees_conso_debut: Optional[str] = None
    perim_donnees_conso_fin: Optional[str] = None
    raison_sociale_du_titulaire: Optional[str] = None
    nom_titulaire: Optional[str] = None
    perim_donnees_contractuelles: Optional[bool] = None
    perim_donnees_techniques: Optional[bool] = None
    perim_donnees_informatives: Optional[bool] = None
    perim_donnees_publiees: Optional[bool] = None


class DroitAccesPatch(BaseModel):
    """Body de PATCH /grdf/droits-acces/{id_pce} — uniquement les champs modifiables."""
    model_config = ConfigDict(extra="forbid")

    courriel_titulaire: Optional[str] = None
    code_postal: Optional[str] = None
    date_debut_droit_acces: Optional[str] = None
    date_fin_droit_acces: Optional[str] = None
    perim_donnees_conso_debut: Optional[str] = None
    perim_donnees_conso_fin: Optional[str] = None
    raison_sociale_du_titulaire: Optional[str] = None
    nom_titulaire: Optional[str] = None
    perim_donnees_contractuelles: Optional[bool] = None
    perim_donnees_techniques: Optional[bool] = None
    perim_donnees_informatives: Optional[bool] = None
    perim_donnees_publiees: Optional[bool] = None


class ConsentCreate(BaseModel):
    """Body de POST /enedis/consent (déclaration d'un PDL)."""
    model_config = ConfigDict(extra="forbid")

    id_pdl: Optional[str] = None
    partner: Optional[str] = None
    date_signature_mandat: Optional[str] = None
    date_debut_autorisation: Optional[str] = None
    date_fin_autorisation: Optional[str] = None
    raison_sociale: Optional[str] = None
    civilite: Optional[str] = None
    nom: Optional[str] = None
    prenom: Optional[str] = None
    injection: Optional[bool] = None
    soutirage: Optional[bool] = None
    get_cdc: Optional[bool] = None
    get_dm: Optional[bool] = None


class ConsentPatch(BaseModel):
    """Body de PATCH /enedis/consent/{id_pdl} — uniquement les champs modifiables."""
    model_config = ConfigDict(extra="forbid")

    date_signature_mandat: Optional[str] = None
    date_debut_autorisation: Optional[str] = None
    date_fin_autorisation: Optional[str] = None
    raison_sociale: Optional[str] = None
    civilite: Optional[str] = None
    nom: Optional[str] = None
    prenom: Optional[str] = None
    injection: Optional[bool] = None
    soutirage: Optional[bool] = None
    get_cdc: Optional[bool] = None
    get_dm: Optional[bool] = None


def _model_body(payload: BaseModel) -> dict:
    """Dict des champs RÉELLEMENT envoyés (les champs omis ne sont pas inclus)."""
    return payload.model_dump(exclude_unset=True)


# ----------------------------------------------------------------------
# Routes GRDF (mêmes chemins que function_app.py, préfixe /api)
# ----------------------------------------------------------------------

@app.get("/api/grdf/droits-acces", tags=["GRDF"])
def list_droits(
    request: Request,
    statut: Optional[str] = None,
    limit: Optional[str] = None,
    offset: Optional[str] = None,
):
    params = {k: v for k, v in (("statut", statut), ("limit", limit), ("offset", offset)) if v is not None}
    return _resp(handle_list(_Req(dict(request.headers), None, params)))


@app.post("/api/grdf/droits-acces", tags=["GRDF"])
def create_droit(request: Request, payload: DroitAccesCreate):
    return _resp(handle_create(_Req(dict(request.headers), _model_body(payload), {})))


@app.get("/api/grdf/droits-acces/{id_pce}", tags=["GRDF"])
def get_droit(id_pce: str, request: Request):
    return _resp(handle_get(_Req(dict(request.headers), None, {}), id_pce))


@app.patch("/api/grdf/droits-acces/{id_pce}", tags=["GRDF"])
def patch_droit(id_pce: str, request: Request, payload: DroitAccesPatch):
    return _resp(handle_patch(_Req(dict(request.headers), _model_body(payload), {}), id_pce))


@app.delete("/api/grdf/droits-acces/{id_pce}", tags=["GRDF"])
def delete_droit(id_pce: str, request: Request):
    return _resp(handle_revoke(_Req(dict(request.headers), None, {}), id_pce))


@app.post("/api/grdf/droits-acces/{id_pce}/retry", tags=["GRDF"])
def retry_droit(id_pce: str, request: Request):
    return _resp(handle_retry(_Req(dict(request.headers), None, {}), id_pce))


# ----------------------------------------------------------------------
# Données de consommation
# ----------------------------------------------------------------------

@app.get("/api/consommations", tags=["Consommations"])
def consommations(
    request: Request,
    provider: Optional[str] = None,
    sensor_id: Optional[str] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
):
    params = {}
    for key, val in (("provider", provider), ("sensor_id", sensor_id), ("from", from_), ("to", to)):
        if val is not None:
            params[key] = val
    return _resp(handle_consommations(_Req(dict(request.headers), None, params)))


# ----------------------------------------------------------------------
# Routes Enedis (consentements PDL — table Delta pdl)
# ----------------------------------------------------------------------

@app.post("/api/enedis/consent", tags=["Enedis"])
def create_consent(request: Request, payload: ConsentCreate):
    return _resp(handle_create_consent(_Req(dict(request.headers), _model_body(payload), {})))


@app.get("/api/enedis/consents", tags=["Enedis"])
def list_consents(
    request: Request,
    statut: Optional[str] = None,
    limit: Optional[str] = None,
    offset: Optional[str] = None,
):
    params = {k: v for k, v in (("statut", statut), ("limit", limit), ("offset", offset)) if v is not None}
    return _resp(handle_list_consents(_Req(dict(request.headers), None, params)))


@app.get("/api/enedis/consent/{id_pdl}", tags=["Enedis"])
def get_consent(id_pdl: str, request: Request):
    return _resp(handle_get_consent(_Req(dict(request.headers), None, {}), id_pdl))


@app.patch("/api/enedis/consent/{id_pdl}", tags=["Enedis"])
def patch_consent(id_pdl: str, request: Request, payload: ConsentPatch):
    return _resp(handle_patch_consent(_Req(dict(request.headers), _model_body(payload), {}), id_pdl))


@app.post("/api/enedis/consent/{id_pdl}/retry", tags=["Enedis"])
def retry_consent(id_pdl: str, request: Request):
    return _resp(handle_retry_consent(_Req(dict(request.headers), None, {}), id_pdl))


@app.delete("/api/enedis/consent/{id_pdl}", tags=["Enedis"])
def delete_consent(id_pdl: str, request: Request):
    return _resp(handle_revoke_consent(_Req(dict(request.headers), None, {}), id_pdl))


@app.get("/", include_in_schema=False)
def root():
    return {"api": "FENIX (dev local)", "docs": "/docs", "routes_grdf": "/api/grdf/droits-acces"}
