"""
Tests du CONTRAT au niveau FastAPI (via TestClient).

Vérifie que la couche HTTP (main.py) respecte le même format d'erreur que les
handlers — y compris les erreurs générées par FastAPI lui-même (body malformé,
route inconnue), remappées par les exception handlers.

Ces tests ne touchent PAS l'ADLS (ils visent la validation / le routage).
"""

import warnings

import pytest

warnings.filterwarnings("ignore")  # silence StarletteDeprecationWarning (httpx)

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)


def test_body_malforme_donne_notre_400():
    # Avant fix : FastAPI renvoyait 422 {detail:[...]}
    r = client.post(
        "/api/grdf/droits-acces",
        content="{bad json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json()["erreur"] == "CHAMP_INVALIDE"


def test_body_non_objet_donne_notre_400():
    r = client.post("/api/grdf/droits-acces", json=[1, 2, 3])
    assert r.status_code == 400
    assert r.json()["erreur"] == "CHAMP_INVALIDE"


def test_route_inconnue_donne_notre_404():
    r = client.get("/api/route-inexistante")
    assert r.status_code == 404
    body = r.json()
    assert body["erreur"] == "ROUTE_INTROUVABLE"
    assert "message" in body


def test_methode_non_autorisee_donne_notre_format():
    # PUT non défini sur cette route → 405 remappé
    r = client.put("/api/grdf/droits-acces/GI123")
    assert r.status_code == 405
    assert r.json()["erreur"] == "METHODE_NON_AUTORISEE"


def test_validation_passe_au_handler():
    # provider invalide → notre 400 CHAMP_INVALIDE (champ provider) via le handler
    r = client.get("/api/consommations", params={
        "provider": "enedis", "sensor_id": "x", "from": "2024-01-01", "to": "2024-06-01",
    })
    assert r.status_code == 400
    assert r.json()["erreur"] == "CHAMP_INVALIDE"
    assert r.json()["champ"] == "provider"


def test_create_validation_passe_au_handler():
    # body partiel (champs obligatoires manquants) → NOTRE validation, pas Pydantic 422
    r = client.post("/api/grdf/droits-acces", json={"id_pce": "GI_X"})
    assert r.status_code == 400
    assert r.json()["erreur"] == "CHAMP_INVALIDE"
    assert "champ" in r.json()   # message précis par champ (≠ 422 générique)


def test_create_champ_inconnu_passe_au_handler():
    # champ inconnu via la couche Pydantic (extra='allow') → handler → CHAMP_INCONNU
    r = client.post("/api/grdf/droits-acces", json={"id_pce": "GI_X", "colonne_bidon": "y"})
    assert r.status_code == 400
    assert r.json()["erreur"] == "CHAMP_INCONNU"
    assert r.json()["champ"] == "colonne_bidon"


def test_get_liste_400_passe_au_handler():
    # statut invalide → notre 400 CHAMP_INVALIDE (champ statut)
    r = client.get("/api/grdf/droits-acces", params={"statut": "Cloturé"})
    assert r.status_code == 400
    assert r.json()["champ"] == "statut"
