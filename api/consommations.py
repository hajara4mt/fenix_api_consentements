"""
Handler GET /consommations — consos publiées (Silver), intervalles bruts.

Route PARTAGÉE Enedis/GRDF par le paramètre `provider` — **forme de réponse
IDENTIQUE** quel que soit le provider :

  - **grdf**   : parquet Silver `consos_publiees` du PCE (intervalles bruts).
  - **enedis** : table Delta `donnees_mesures` du PDL, lignes agrégées
    `label == "TOTAL"` uniquement, `val` mappée en `consommation` (valeur BRUTE,
    aucune transformation). Unité non stockée → "kWh" par défaut, comme GRDF.

Dans les deux cas on renvoie les périodes **telles quelles** (PAS d'agrégation
mensuelle), bornées dans [from, to] selon la règle « Contenu » : on garde une
période seulement si `date_debut >= from` ET `date_fin <= to`.

Réponse : { provider, sensor_id, from, to, data: [ {date_debut, date_fin,
consommation, unite}, ... ] }.
"""

import logging
import math
from datetime import datetime
from typing import Optional

from .consos_reader import read_consos_enedis, read_consos_publiees
from .ip_filter import is_ip_allowed

PROVIDERS_SUPPORTES = ("grdf", "enedis")

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _champ_invalide(champ: str, message: str) -> tuple[dict, int]:
    """400 aligné sur le reste de l'API."""
    return {"erreur": "CHAMP_INVALIDE", "message": message, "champ": champ}, 400


def _parse_jour(value) -> Optional[datetime.date]:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _s(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
    s = str(value).strip()
    return s or None


def _num(value):
    """Valeur numérique JSON-safe (NaN/numpy → None)."""
    if value is None:
        return None
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if hasattr(value, "item"):
        v = value.item()
        return None if (isinstance(v, float) and math.isnan(v)) else v
    return value


# ----------------------------------------------------------------------
# Handler
# ----------------------------------------------------------------------

def handle_consommations(req) -> tuple[dict, int]:
    """Lit les consos publiées d'un compteur sur [from, to] (intervalles bruts)."""
    # --- 1. Filtre IP ---
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    params = getattr(req, "params", None) or {}

    # --- 2. provider (grdf | enedis) ---
    provider = (params.get("provider") or "").strip().lower()
    if not provider:
        return _champ_invalide("provider", "Le champ provider est obligatoire.")
    if provider not in PROVIDERS_SUPPORTES:
        return _champ_invalide(
            "provider",
            "Le champ provider doit être 'grdf' ou 'enedis'.",
        )

    # --- 3. sensor_id (= id_pce GRDF brut, ou id_pdl Enedis) ---
    sensor_id = (params.get("sensor_id") or "").strip()
    if not sensor_id:
        return _champ_invalide("sensor_id", "Le champ sensor_id est obligatoire.")

    # --- 4. from / to ---
    from_raw = (params.get("from") or "").strip()
    to_raw = (params.get("to") or "").strip()
    from_d = _parse_jour(from_raw)
    if from_d is None:
        return _champ_invalide("from", "Le champ from doit être une date au format YYYY-MM-DD.")
    to_d = _parse_jour(to_raw)
    if to_d is None:
        return _champ_invalide("to", "Le champ to doit être une date au format YYYY-MM-DD.")
    if to_d <= from_d:
        return _champ_invalide("to", "La date to doit être strictement supérieure à from.")

    # --- 5. Lecture Silver selon le provider (forme des lignes identique) ---
    if provider == "enedis":
        rows = read_consos_enedis(sensor_id)
    else:
        rows = read_consos_publiees(sensor_id)
    if rows is None:
        return {
            "erreur": "SENSOR_INTROUVABLE",
            "message": "Aucune donnée trouvée pour ce sensor_id.",
            "sensor_id": sensor_id,
        }, 404

    # --- 6. Filtrage « Contenu » : intervalle entièrement dans [from, to] ---
    data = []
    for row in rows:
        dd = _parse_jour(row.get("date_debut"))
        df = _parse_jour(row.get("date_fin"))
        if dd is None or df is None:
            continue
        if dd >= from_d and df <= to_d:
            data.append({
                "date_debut": _s(row.get("date_debut")),
                "date_fin": _s(row.get("date_fin")),
                "consommation": _num(row.get("consommation")),
                "unite": _s(row.get("unite")) or "kWh",
            })

    data.sort(key=lambda d: d["date_debut"] or "")

    return {
        "provider": provider,
        "sensor_id": sensor_id,
        "from": from_raw,
        "to": to_raw,
        "data": data,
    }, 200
