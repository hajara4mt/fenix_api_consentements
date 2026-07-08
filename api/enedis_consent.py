"""
Handlers des routes /enedis/consent (consentements PDL).

Écrit dans la table Delta `pdl` (vendorée : shared/adls_client.py).
⚠️ Spécificités Enedis vs GRDF :
  - stockage Delta Lake (append_rows / update_rows), pas de lease
  - booléens en strings "true"/"false"
  - la ligne écrite doit contenir TOUTES les 22 colonnes du schéma pdl
    (append_rows valide colonnes manquantes ET en trop)

Itération courante : POST (création). Le reste (GET/PATCH/DELETE/retry/list)
viendra route par route.
"""

import json
import logging
from datetime import datetime, timezone

import pandas as pd

from shared import adls_client
from shared.schemas import StatutPDL

from .enedis_validation import (
    ALLOWED_CONSENT_FIELDS,
    BOOL_FIELDS,
    DATE_FIELDS,
    MODIFIABLE_CONSENT_FIELDS,
    ValidationError,
    validate_consent,
    validate_patch_consent,
)
from .ip_filter import is_ip_allowed

logger = logging.getLogger(__name__)


def handle_create_consent(req) -> tuple[dict, int]:
    """POST /enedis/consent — crée un PDL 'nouveau' dans la table Delta pdl."""

    # --- 1. Filtre IP ---
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    # --- 2. Parse JSON ---
    try:
        body = req.get_json()
    except ValueError:
        return {
            "erreur": "CHAMP_INVALIDE",
            "message": "Le corps de la requête doit être un JSON valide.",
        }, 400

    # --- 2bis. Schéma strict ---
    if isinstance(body, dict):
        for key in body:
            if key not in ALLOWED_CONSENT_FIELDS:
                return {
                    "erreur": "CHAMP_INCONNU",
                    "message": f"Le champ {key} n'est pas reconnu (hors schéma de création).",
                    "champ": key,
                }, 400

    # --- 3. Validation ---
    try:
        id_pdl, fields = validate_consent(body)
    except ValidationError as e:
        reponse = {"erreur": "CHAMP_INVALIDE", "message": e.message}
        if e.champ:
            reponse["champ"] = e.champ
        return reponse, 400

    # --- 4. 409 si le PDL existe déjà (read-then-append) ---
    try:
        existant = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
    except Exception:
        logger.exception("Erreur lecture table pdl pour %s", id_pdl)
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de la lecture du registre.",
        }, 500
    if existant is not None and len(existant) > 0:
        return {
            "erreur": "PDL_EXISTANT",
            "message": "Un consentement existe déjà pour ce PDL.",
            "id_pdl": id_pdl,
            "statut": str(existant.iloc[0].get("statut")),
        }, 409

    # --- 5. Construction de la ligne COMPLÈTE (22 colonnes) + append ---
    # platform_code est déjà dans `fields` (ajouté par validate_consent).
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = {
        **fields,
        "date_creation": now,
        "date_modification": now,
        "statut_cdc": None,                 # calculé par F1
        "statut_dm": None,                  # calculé par F1
        "date_premiere_valeur_dm": None,    # rempli par F5
        "statut": "nouveau",
        "commentaire": None,
        "erreur": None,
    }
    try:
        adls_client.append_rows("pdl", pd.DataFrame([row]), validate=True)
    except Exception:
        logger.exception("Erreur écriture pdl %s", id_pdl)
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de l'enregistrement du consentement.",
        }, 500

    logger.info("PDL %s enregistré (statut='nouveau')", id_pdl)
    return {
        "id_pdl": id_pdl,
        "statut": "nouveau",
        "message": "Consentement enregistré. Traitement au prochain cycle SGE.",
        "date_creation": now.strftime("%Y-%m-%d %H:%M:%S"),
    }, 201


# ----------------------------------------------------------------------
# Helpers de sérialisation (valeurs Delta/pandas → JSON-safe)
# ----------------------------------------------------------------------

def _str_or_none(value):
    if value is None or pd.isna(value):
        return None
    s = str(value).strip()
    return s or None


def _bool_or_none(value):
    """statut_cdc/statut_dm stockés en "true"/"false"/null → bool JSON / null."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("true", "1", "vrai"):
        return True
    if s in ("false", "0", "faux"):
        return False
    return None


def _fmt_dt(value):
    """Timestamp/datetime/ISO → 'YYYY-MM-DD HH:MM:SS' (UTC)."""
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "strftime"):  # pandas Timestamp / datetime / date
        try:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s or None


def _message_erreur(value):
    """Colonne erreur (JSON {code_statut_traitement, message_retour_traitement})
    → message brut (message_retour_traitement). None si pas d'erreur."""
    if value is None or pd.isna(value):
        return None
    s = str(value).strip()
    if not s or s.lower() == "null":
        return None
    try:
        return json.loads(s).get("message_retour_traitement") or None
    except (json.JSONDecodeError, AttributeError, TypeError):
        return s


# ----------------------------------------------------------------------
# Serializer commun d'un PDL (forme « statut seul », 8 champs)
# Utilisé par le GET détail ET les items de liste.
# ----------------------------------------------------------------------

def _serialize_pdl(row) -> dict:
    return {
        "id_pdl": _str_or_none(row.get("id_pdl")),
        "partner": _str_or_none(row.get("partner")),
        "platform_code": _str_or_none(row.get("platform_code")),
        "statut": _str_or_none(row.get("statut")),
        "statut_cdc": _bool_or_none(row.get("statut_cdc")),
        "statut_dm": _bool_or_none(row.get("statut_dm")),
        "message_erreur": _message_erreur(row.get("erreur")),
        "date_creation": _fmt_dt(row.get("date_creation")),
        "date_modification": _fmt_dt(row.get("date_modification")),
    }


# ----------------------------------------------------------------------
# Handler GET /enedis/consent/{id_pdl}
# ----------------------------------------------------------------------

def handle_get_consent(req, id_pdl: str) -> tuple[dict, int]:
    """Consulte le statut d'un PDL (forme centrée statut)."""
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    try:
        df = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
    except Exception:
        logger.exception("Erreur lecture table pdl pour %s", id_pdl)
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de la lecture du registre.",
        }, 500

    if df is None or len(df) == 0:
        return {
            "erreur": "PDL_INTROUVABLE",
            "message": "Aucun consentement trouvé pour ce PDL.",
            "id_pdl": id_pdl,
        }, 404

    return _serialize_pdl(df.iloc[0]), 200


# ----------------------------------------------------------------------
# Handler GET /enedis/consents — liste paginée
# ----------------------------------------------------------------------

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100
STATUTS_PDL = tuple(e.value for e in StatutPDL)   # 7 statuts internes (ordre)


def _erreur_champ(champ: str, message: str) -> tuple[dict, int]:
    return {"erreur": "CHAMP_INVALIDE", "message": message, "champ": champ}, 400


def _parse_pagination_int(raw, champ: str, minimum: int, maximum):
    if raw is None or str(raw).strip() == "":
        return None, None
    try:
        value = int(str(raw).strip())
    except ValueError:
        borne = f"entre {minimum} et {maximum}" if maximum is not None else f"≥ {minimum}"
        return None, _erreur_champ(champ, f"{champ} doit être un entier {borne}.")
    if value < minimum or (maximum is not None and value > maximum):
        borne = f"entre {minimum} et {maximum}" if maximum is not None else f"≥ {minimum}"
        return None, _erreur_champ(champ, f"{champ} doit être {borne}.")
    return value, None


def handle_list_consents(req) -> tuple[dict, int]:
    """Liste paginée des PDL (items = même forme que le GET détail)."""
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    params = getattr(req, "params", None) or {}

    # --- statut (égalité simple sur les 7 internes) ---
    statut = params.get("statut")
    statut = statut.strip() if isinstance(statut, str) else statut
    if statut:
        if statut not in STATUTS_PDL:
            return _erreur_champ("statut", "statut doit être l'un de : " + ", ".join(STATUTS_PDL) + ".")
    else:
        statut = None

    # --- limit / offset (strict) ---
    limit, err = _parse_pagination_int(params.get("limit"), "limit", 1, LIST_LIMIT_MAX)
    if err:
        return err
    if limit is None:
        limit = LIST_LIMIT_DEFAULT
    offset, err = _parse_pagination_int(params.get("offset"), "offset", 0, None)
    if err:
        return err
    if offset is None:
        offset = 0

    # --- lecture + filtrage + tri + pagination ---
    try:
        df = adls_client.read_table("pdl")
    except Exception:
        logger.exception("Erreur lecture table pdl (liste)")
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de la lecture du registre.",
        }, 500

    if statut and "statut" in df.columns:
        df = df[df["statut"].astype(str) == statut]
    if "id_pdl" in df.columns:
        df = df.sort_values("id_pdl")

    total = len(df)
    page = df.iloc[offset:offset + limit]
    resultats = [_serialize_pdl(row) for _, row in page.iterrows()]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "resultats": resultats,
    }, 200


# ----------------------------------------------------------------------
# Handler POST /enedis/consent/{id_pdl}/retry — relance
# ----------------------------------------------------------------------

# Seul un PDL en 'erreur' est relançable (décision projet, aligné MD).
RETRYABLE_STATUTS = {"erreur"}


def handle_retry_consent(req, id_pdl: str) -> tuple[dict, int]:
    """
    Relance un PDL bloqué en 'erreur' (POST .../retry, sans body).

    Remet le PDL en file : statut='nouveau' + erreur effacée → F1 (toutes les 2h)
    re-tente la déclaration SGE. Aucune donnée du consentement n'est modifiée.
    """
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    try:
        df = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
    except Exception:
        logger.exception("Erreur lecture pdl pour %s", id_pdl)
        return {"erreur": "ERREUR_INTERNE", "message": "Erreur interne lors de la lecture du registre."}, 500
    if df is None or len(df) == 0:
        return {
            "erreur": "PDL_INTROUVABLE",
            "message": "Aucun consentement trouvé pour ce PDL.",
            "id_pdl": id_pdl,
        }, 404

    statut = _str_or_none(df.iloc[0].get("statut"))
    if statut not in RETRYABLE_STATUTS:
        return {
            "erreur": "STATUT_INCOMPATIBLE",
            "message": "Seul un consentement en statut erreur peut être relancé.",
            "id_pdl": id_pdl,
            "statut": statut,
        }, 409

    try:
        adls_client.update_rows("pdl", f"id_pdl = '{adls_client._sql_escape(id_pdl)}'", {
            "statut": "'nouveau'",
            "erreur": "NULL",
            "date_modification": "current_timestamp()",
        })
    except Exception:
        logger.exception("Erreur relance pdl %s", id_pdl)
        return {"erreur": "ERREUR_INTERNE", "message": "Erreur interne lors de la relance."}, 500

    date_modif = None
    try:
        updated = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
        if updated is not None and len(updated) > 0:
            date_modif = _fmt_dt(updated.iloc[0].get("date_modification"))
    except Exception:
        pass

    logger.info("PDL %s relancé (statut='nouveau')", id_pdl)
    return {
        "id_pdl": id_pdl,
        "statut": "nouveau",
        "message": "Relance enregistrée. Re-traitement au prochain cycle SGE.",
        "date_modification": date_modif,
    }, 200


# ----------------------------------------------------------------------
# Handler DELETE /enedis/consent/{id_pdl} — révocation
# ----------------------------------------------------------------------

# Révocable uniquement depuis 'traite' (décision projet, suit le MD).
REVOCABLE_STATUTS = {"traite"}
# Déjà révoqué / terminé → 404 PDL_DEJA_REVOQUE.
STATUTS_DEJA_REVOQUES = {"revoque", "résilié"}


def handle_revoke_consent(req, id_pdl: str) -> tuple[dict, int]:
    """
    Révoque un consentement (DELETE) — révocation LOGIQUE.

    Passe statut='revoque' (via update_pdl_statut). ⚠️ La résiliation effective
    auprès de SGE devra être faite par un futur batch (aucun n'existe à ce jour),
    qui passera le PDL en 'résilié'. Tant que le PDL est 'revoque', F3 continue
    de collecter (F3 n'exclut que 'nouveau' et 'résilié').
    """
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    try:
        df = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
    except Exception:
        logger.exception("Erreur lecture pdl pour %s", id_pdl)
        return {"erreur": "ERREUR_INTERNE", "message": "Erreur interne lors de la lecture du registre."}, 500

    if df is None or len(df) == 0:
        return {
            "erreur": "PDL_INTROUVABLE",
            "message": "Aucun consentement trouvé pour ce PDL.",
            "id_pdl": id_pdl,
        }, 404

    statut = _str_or_none(df.iloc[0].get("statut"))

    if statut in STATUTS_DEJA_REVOQUES:
        return {
            "erreur": "PDL_DEJA_REVOQUE",
            "message": "Ce PDL a déjà été révoqué, il n'est plus révocable.",
            "id_pdl": id_pdl,
        }, 404

    if statut not in REVOCABLE_STATUTS:
        return {
            "erreur": "STATUT_INCOMPATIBLE",
            "message": "Seul un consentement avec statut traite peut être révoqué.",
            "id_pdl": id_pdl,
            "statut": statut,
        }, 409

    try:
        adls_client.update_pdl_statut(id_pdl, "revoque")
    except Exception:
        logger.exception("Erreur révocation pdl %s", id_pdl)
        return {"erreur": "ERREUR_INTERNE", "message": "Erreur interne lors de la révocation."}, 500

    # Relecture pour renvoyer la date_modification réelle
    date_modif = None
    try:
        updated = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
        if updated is not None and len(updated) > 0:
            date_modif = _fmt_dt(updated.iloc[0].get("date_modification"))
    except Exception:
        pass

    logger.info("PDL %s révoqué (statut='revoque')", id_pdl)
    return {
        "id_pdl": id_pdl,
        "statut": "revoque",
        "message": "Révocation enregistrée. La résiliation auprès de SGE sera traitée par un batch ultérieur.",
        "date_modification": date_modif,
    }, 200


# ----------------------------------------------------------------------
# Handler PATCH /enedis/consent/{id_pdl} — mise à jour
# ----------------------------------------------------------------------

def _sql_value(col: str, val) -> str:
    """Valeur Python normalisée → littéral SQL pour update_rows (Delta)."""
    if col in DATE_FIELDS:
        return f"DATE '{val.isoformat()}'"          # val = datetime.date
    if col in BOOL_FIELDS:
        return f"'{val}'"                            # 'true' / 'false'
    return "NULL" if val is None else "'" + adls_client._sql_escape(val) + "'"


def handle_patch_consent(req, id_pdl: str) -> tuple[dict, int]:
    """Met à jour un consentement (PATCH partiel) → statut='nouveau', erreur=null."""
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    try:
        body = req.get_json()
    except ValueError:
        return {"erreur": "CHAMP_INVALIDE", "message": "Le corps de la requête doit être un JSON valide."}, 400
    if not isinstance(body, dict):
        return {"erreur": "CHAMP_INVALIDE", "message": "Le corps de la requête doit être un objet JSON."}, 400

    try:
        df = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
    except Exception:
        logger.exception("Erreur lecture pdl pour %s", id_pdl)
        return {"erreur": "ERREUR_INTERNE", "message": "Erreur interne lors de la lecture du registre."}, 500
    if df is None or len(df) == 0:
        return {
            "erreur": "PDL_INTROUVABLE",
            "message": "Aucun consentement trouvé pour ce PDL.",
            "id_pdl": id_pdl,
        }, 404

    # whitelist stricte
    for key in body:
        if key not in MODIFIABLE_CONSENT_FIELDS:
            return {
                "erreur": "CHAMP_NON_MODIFIABLE",
                "message": f"Le champ {key} ne peut pas être modifié.",
                "champ": key,
            }, 400
    if not body:
        return {"erreur": "CHAMP_INVALIDE", "message": "Aucun champ modifiable fourni."}, 400

    # validation partielle + croisée sur le merge
    try:
        out = validate_patch_consent(body, df.iloc[0].to_dict())
    except ValidationError as e:
        reponse = {"erreur": "CHAMP_INVALIDE", "message": e.message}
        if e.champ:
            reponse["champ"] = e.champ
        return reponse, 400

    # UPDATE Delta : champs modifiés + remise en file (nouveau) + erreur effacée
    updates = {col: _sql_value(col, val) for col, val in out.items()}
    updates["statut"] = "'nouveau'"
    updates["erreur"] = "NULL"
    updates["date_modification"] = "current_timestamp()"
    try:
        adls_client.update_rows("pdl", f"id_pdl = '{adls_client._sql_escape(id_pdl)}'", updates)
    except Exception:
        logger.exception("Erreur mise à jour pdl %s", id_pdl)
        return {"erreur": "ERREUR_INTERNE", "message": "Erreur interne lors de la mise à jour."}, 500

    # ressource complète à jour
    try:
        updated = adls_client.read_table_filtered("pdl", "id_pdl", "=", id_pdl)
        row = updated.iloc[0] if (updated is not None and len(updated) > 0) else df.iloc[0]
    except Exception:
        row = df.iloc[0]
    logger.info("PDL %s mis à jour (%d champ(s)), statut='nouveau'", id_pdl, len(body))
    return _serialize_pdl(row), 200
