"""
Handler métier de POST /grdf/droits-acces.

Rôle : déposer un nouveau droit d'accès PCE dans droits_acces.parquet avec
etat_droit_acces='nouveau'. La route NE contacte PAS GRDF : c'est le batch
declare_pce (pipeline) qui s'en charge ensuite.

Pipeline d'exécution :
  1. Filtre IP        → 403 si non whitelistée
  2. Parse JSON       → 400 si corps invalide
  3. Validation       → 400 (CHAMP_INVALIDE) au premier champ fautif
  4. Enrichissement   → role_tiers (constante), etat='nouveau', date_creation
  5. Insert lease-safe via registry_dao → 409 si le PCE existe déjà
  6. 201 Created

L'écriture passe par registry_dao.insert (mutex lease ADLS) : c'est le MÊME
chemin d'écriture que les batches du pipeline, donc pas de corruption du parquet
en cas de concurrence.

Le handler renvoie (corps_dict, status_code) ; function_app construit la
HttpResponse. Ce découplage rend le handler testable sans runtime Azure.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from shared import registry_dao

from .adict_messages import resolve_message_erreur
from .ip_filter import is_ip_allowed
from .validation import ValidationError, validate_create, validate_patch

logger = logging.getLogger(__name__)


# role_tiers n'est pas saisi par le métier : tout PCE créé via cette route reçoit
# cette valeur fixe (décision projet, alignée DB Energisme).
ROLE_TIERS_DEFAUT = "AUTORISE_CONTRAT_FOURNITURE"

# Statuts exposés = les 8 états internes BRUTS (etat_droit_acces), tels quels,
# SANS mapping (décision projet : aligné sur Enedis qui expose ses statuts bruts).
# Ordre = cycle de vie (cf. registry_dao.DROIT_STATES).
STATUTS_INTERNES = (
    "nouveau", "A valider", "A revérifier", "Active",
    "Refusée", "Révoquée", "Obsolète", "résilié",
)

# Schéma STRICT du body de création : seuls ces champs sont acceptés.
# Tout champ hors de cette liste → 400 CHAMP_INCONNU (pas d'ajout de colonne).
# (Les champs optionnels — périmètres — peuvent être omis : leur absence est OK.)
ALLOWED_CREATE_FIELDS = {
    "id_pce",
    "partner",
    "platform_code",
    "courriel_titulaire",
    "code_postal",
    "date_debut_droit_acces",
    "date_fin_droit_acces",
    "perim_donnees_conso_debut",
    "perim_donnees_conso_fin",
    "raison_sociale_du_titulaire",
    "nom_titulaire",
    "perim_donnees_contractuelles",
    "perim_donnees_techniques",
    "perim_donnees_informatives",
    "perim_donnees_publiees",
}


def handle_create(req) -> tuple[dict, int]:
    """Traite une requête POST /grdf/droits-acces. Retourne (corps, status)."""

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

    # --- 2bis. Schéma strict : refuser tout champ hors du contrat de création ---
    if isinstance(body, dict):
        for key in body:
            if key not in ALLOWED_CREATE_FIELDS:
                return {
                    "erreur": "CHAMP_INCONNU",
                    "message": f"Le champ {key} n'est pas reconnu (hors schéma de création).",
                    "champ": key,
                }, 400

    # --- 3. Validation ---
    try:
        id_pce, fields = validate_create(body)
    except ValidationError as e:
        reponse = {"erreur": "CHAMP_INVALIDE", "message": e.message}
        if e.champ:
            reponse["champ"] = e.champ
        return reponse, 400

    # --- 4. Enrichissement (champs non saisis par le métier) ---
    now_iso = datetime.now(timezone.utc).isoformat()
    fields["role_tiers"] = ROLE_TIERS_DEFAUT
    fields["etat_droit_acces"] = "nouveau"
    fields["date_creation"] = now_iso

    # --- 5. Insert lease-safe (409 si déjà présent) ---
    try:
        registry_dao.insert(id_pce, fields)
    except ValueError:
        # registry_dao.insert lève ValueError si le PCE existe déjà
        existant = registry_dao.get(id_pce) or {}
        return {
            "erreur": "PCE_EXISTANT",
            "message": "Un droit d'accès existe déjà pour ce PCE.",
            "id_pce": id_pce,
            "statut": _statut_brut(existant.get("etat_droit_acces")),
        }, 409
    except Exception:
        logger.exception("Erreur interne lors de l'insertion du PCE %s", id_pce)
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de l'enregistrement du droit d'accès.",
        }, 500

    # --- 6. Succès ---
    logger.info("PCE %s enregistré (etat='nouveau')", id_pce)
    return {
        "id_pce": id_pce,
        "statut": "nouveau",
        "message": "Droit d'accès enregistré. Traitement prévu lors du prochain batch de nuit.",
        "date_creation": _fmt_dt(now_iso),
    }, 201


# ----------------------------------------------------------------------
# Helpers de nettoyage des valeurs parquet (numpy/NaN → JSON-safe)
# ----------------------------------------------------------------------

def _statut_brut(etat) -> str:
    """Statut interne exposé TEL QUEL (brut, sans mapping). Défaut 'nouveau' si vide."""
    return _str_or_none(etat) or "nouveau"


def _str_or_none(value) -> Optional[str]:
    """Valeur → str non vide, ou None (gère NaN pandas)."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):  # scalaire numpy
        value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
    s = str(value).strip()
    return s or None


def _bool_or_none(value) -> Optional[bool]:
    """Valeur → bool, ou None (gère NaN pandas, numpy bool, 'Vrai'/'Faux')."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else bool(value)
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        t = value.strip().lower()
        if t in ("true", "vrai", "1"):
            return True
        if t in ("false", "faux", "0"):
            return False
        return None
    if hasattr(value, "item"):  # scalaire numpy
        return _bool_or_none(value.item())
    return None


def _fmt_dt(value) -> Optional[str]:
    """
    Formate un horodatage en 'YYYY-MM-DD HH:MM:SS' (sans microsecondes ni fuseau,
    UTC). Accepte un ISO 8601 ('...T09:03:16.415174+00:00' ou '...Z') ou une
    valeur déjà simple (renvoyée telle quelle si non parsable).
    """
    s = _str_or_none(value)
    if s is None:
        return None
    try:
        return datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return s


# ----------------------------------------------------------------------
# Serializer commun d'un PCE (forme constante)
# ----------------------------------------------------------------------

def _serialize_pce(record: dict, id_pce: Optional[str] = None, light: bool = False) -> dict:
    """
    Sérialise un enregistrement PCE en réponse à forme CONSTANTE.

    light=False (GET détail) : 12 champs.
    light=True  (items de liste) : 10 champs (omet date_debut_droit_acces et
    date_creation).
    """
    out = {
        "id_pce": id_pce or _str_or_none(record.get("id_pce")),
        "partner": _str_or_none(record.get("partner")),
        "platform_code": _str_or_none(record.get("platform_code")),
        "statut": _statut_brut(record.get("etat_droit_acces")),
        "perim_donnees_contractuelles": _bool_or_none(record.get("perim_donnees_contractuelles")),
        "perim_donnees_techniques": _bool_or_none(record.get("perim_donnees_techniques")),
        "perim_donnees_informatives": _bool_or_none(record.get("perim_donnees_informatives")),
        "perim_donnees_publiees": _bool_or_none(record.get("perim_donnees_publiees")),
        "date_debut_droit_acces": _str_or_none(record.get("date_debut_droit_acces")),
        "date_fin_droit_acces": _str_or_none(record.get("date_fin_droit_acces")),
        "date_creation": _fmt_dt(record.get("date_creation")),
        "derniere_maj": _fmt_dt(record.get("derniere_maj")),
        "message_erreur": resolve_message_erreur(record),
    }
    if light:
        out.pop("date_debut_droit_acces")
        out.pop("date_creation")
    return out


# ----------------------------------------------------------------------
# Handler GET /grdf/droits-acces/{id_pce}
# ----------------------------------------------------------------------

def handle_get(req, id_pce: str) -> tuple[dict, int]:
    """
    Retourne le statut d'un droit d'accès PCE (forme constante, 11 champs).

    Pipeline : filtre IP (403) → lecture parquet → 404 si absent → 200 sinon.
    """
    # --- 1. Filtre IP ---
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    # --- 2. Lecture du registre ---
    record = registry_dao.get(id_pce)
    if not record:
        return {
            "erreur": "PCE_INTROUVABLE",
            "message": "Aucun droit d'accès trouvé pour ce PCE.",
            "id_pce": id_pce,
        }, 404

    # --- 3. Réponse à forme constante ---
    return _serialize_pce(record, id_pce=id_pce, light=False), 200


# ----------------------------------------------------------------------
# Handler DELETE /grdf/droits-acces/{id_pce} — révocation
# ----------------------------------------------------------------------

# États depuis lesquels une révocation est autorisée (décision projet).
REVOCABLE_STATES = {"Active", "nouveau", "A valider"}
# États signifiant « déjà révoqué » (→ 404 PCE_DEJA_REVOQUE).
REVOKED_STATES = {"Révoquée", "résilié"}


def handle_revoke(req, id_pce: str) -> tuple[dict, int]:
    """
    Révoque un droit d'accès (DELETE).

    ⚠️ C'est une révocation LOGIQUE, pas une suppression : on passe
    etat_droit_acces='Révoquée' (via upsert lease-safe), la ligne reste dans le
    parquet. La résiliation effective auprès de GRDF devra être faite par un
    futur batch (aucun batch de résiliation n'existe à ce jour).

    Pipeline : filtre IP (403) → lecture (404 si absent) → contrôle d'état
    (404 si déjà révoqué, 409 si état non révocable) → upsert 'Révoquée' → 200.
    """
    # --- 1. Filtre IP ---
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    # --- 2. Lecture ---
    record = registry_dao.get(id_pce)
    if not record:
        return {
            "erreur": "PCE_INTROUVABLE",
            "message": "Aucun droit d'accès trouvé pour ce PCE.",
            "id_pce": id_pce,
        }, 404

    etat = _str_or_none(record.get("etat_droit_acces"))

    # --- 3a. Déjà révoqué → 404 ---
    if etat in REVOKED_STATES:
        return {
            "erreur": "PCE_DEJA_REVOQUE",
            "message": "Ce PCE a déjà été révoqué, il n'est plus révocable.",
            "id_pce": id_pce,
        }, 404

    # --- 3b. État non révocable → 409 ---
    if etat not in REVOCABLE_STATES:
        return {
            "erreur": "STATUT_INCOMPATIBLE",
            "message": "Seul un droit d'accès Active, nouveau ou A valider peut être révoqué.",
            "id_pce": id_pce,
            "statut": etat,
        }, 409

    # --- 4. Révocation logique (upsert lease-safe) ---
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        registry_dao.upsert(id_pce, {
            "etat_droit_acces": "Révoquée",
        })
    except Exception:
        logger.exception("Erreur interne lors de la révocation du PCE %s", id_pce)
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de la révocation du droit d'accès.",
        }, 500

    updated = registry_dao.get(id_pce) or {}
    logger.info("PCE %s révoqué (etat='Révoquée')", id_pce)
    return {
        "id_pce": id_pce,
        "statut": _str_or_none(updated.get("etat_droit_acces")) or "Révoquée",
        "message": "Révocation enregistrée. La résiliation auprès de GRDF sera traitée par un batch ultérieur.",
        "derniere_maj": _fmt_dt(updated.get("derniere_maj")) or _fmt_dt(now_iso),
    }, 200


# ----------------------------------------------------------------------
# Handler PATCH /grdf/droits-acces/{id_pce} — mise à jour
# ----------------------------------------------------------------------

# Whitelist STRICTE des champs modifiables via PATCH (12 champs métier).
# Tout le reste (id_pce, partner, role_tiers, champs système/pipeline) est
# non-modifiable → 400 CHAMP_NON_MODIFIABLE.
MODIFIABLE_FIELDS = {
    "courriel_titulaire",
    "code_postal",
    "date_debut_droit_acces",
    "date_fin_droit_acces",
    "perim_donnees_conso_debut",
    "perim_donnees_conso_fin",
    "raison_sociale_du_titulaire",
    "nom_titulaire",
    "perim_donnees_contractuelles",
    "perim_donnees_techniques",
    "perim_donnees_informatives",
    "perim_donnees_publiees",
}


def handle_patch(req, id_pce: str) -> tuple[dict, int]:
    """
    Met à jour un droit d'accès existant (PATCH partiel).

    Seuls les champs de MODIFIABLE_FIELDS sont acceptés (whitelist) ; tout autre
    champ → 400 CHAMP_NON_MODIFIABLE. Les champs fournis sont validés (format +
    règles croisées sur le merge avec l'existant). La modif remet le PCE en
    file de déclaration : etat='nouveau', compteur réinitialisé.

    Pipeline : IP (403) → JSON (400) → lecture (404) → whitelist (400) →
    validation (400) → upsert → 200.
    """
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
    if not isinstance(body, dict):
        return {
            "erreur": "CHAMP_INVALIDE",
            "message": "Le corps de la requête doit être un objet JSON.",
        }, 400

    # --- 3. Lecture ---
    record = registry_dao.get(id_pce)
    if not record:
        return {
            "erreur": "PCE_INTROUVABLE",
            "message": "Aucun droit d'accès trouvé pour ce PCE.",
            "id_pce": id_pce,
        }, 404

    # --- 4. Whitelist : rejeter tout champ non-modifiable ---
    for key in body:
        if key not in MODIFIABLE_FIELDS:
            return {
                "erreur": "CHAMP_NON_MODIFIABLE",
                "message": f"Le champ {key} ne peut pas être modifié.",
                "champ": key,
            }, 400

    if not body:
        return {
            "erreur": "CHAMP_INVALIDE",
            "message": "Aucun champ modifiable fourni.",
        }, 400

    # --- 5. Validation (format + règles croisées sur le merge) ---
    try:
        fields = validate_patch(body, record)
    except ValidationError as e:
        reponse = {"erreur": "CHAMP_INVALIDE", "message": e.message}
        if e.champ:
            reponse["champ"] = e.champ
        return reponse, 400

    # --- 6. Application + remise en file de déclaration (reset compteur) ---
    fields["etat_droit_acces"] = "nouveau"
    fields["nb_tentatives_declare"] = 0
    fields["message_erreur_declare"] = None
    try:
        registry_dao.upsert(id_pce, fields)
    except Exception:
        logger.exception("Erreur interne lors de la mise à jour du PCE %s", id_pce)
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de la mise à jour du droit d'accès.",
        }, 500

    updated = registry_dao.get(id_pce) or {}
    logger.info("PCE %s mis à jour (%d champ(s)), remis en 'nouveau'", id_pce, len(body))
    # Réponse = ressource COMPLÈTE (champs modifiables à jour + non-modifiables intacts)
    return _serialize_pce(updated, id_pce=id_pce, light=False), 200


# ----------------------------------------------------------------------
# Handler POST /grdf/droits-acces/{id_pce}/retry — relance
# ----------------------------------------------------------------------

# États depuis lesquels une relance est possible (décision projet).
RETRYABLE_STATES = {"A revérifier", "Refusée"}


def handle_retry(req, id_pce: str) -> tuple[dict, int]:
    """
    Relance un droit d'accès (POST .../retry, sans body).

    Remet le PCE en file de déclaration : etat='nouveau', compteur de tentatives
    remis à 0 et message d'erreur effacé → vraie relance (budget de 3 tentatives
    propre dans declare_pce). Le batch de nuit retentera.

    Pipeline : filtre IP (403) → lecture (404 si absent) → contrôle d'état
    (409 si non relançable) → upsert → 200.
    """
    # --- 1. Filtre IP ---
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    # --- 2. Lecture ---
    record = registry_dao.get(id_pce)
    if not record:
        return {
            "erreur": "PCE_INTROUVABLE",
            "message": "Aucun droit d'accès trouvé pour ce PCE.",
            "id_pce": id_pce,
        }, 404

    etat = _str_or_none(record.get("etat_droit_acces"))

    # --- 3. État non relançable → 409 ---
    if etat not in RETRYABLE_STATES:
        return {
            "erreur": "STATUT_INCOMPATIBLE",
            "message": "Seul un droit d'accès en erreur (A revérifier) ou Refusée peut être relancé.",
            "id_pce": id_pce,
            "statut": etat,
        }, 409

    # --- 4. Relance : reset état + compteur + message ---
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        registry_dao.upsert(id_pce, {
            "etat_droit_acces": "nouveau",
            "nb_tentatives_declare": 0,
            "message_erreur_declare": None,
        })
    except Exception:
        logger.exception("Erreur interne lors de la relance du PCE %s", id_pce)
        return {
            "erreur": "ERREUR_INTERNE",
            "message": "Erreur interne lors de la relance du droit d'accès.",
        }, 500

    updated = registry_dao.get(id_pce) or {}
    logger.info("PCE %s relancé (etat='nouveau', compteur réinitialisé)", id_pce)
    return {
        "id_pce": id_pce,
        "statut": _str_or_none(updated.get("etat_droit_acces")) or "nouveau",
        "message": "Relance enregistrée. Re-traitement prévu lors du prochain batch de nuit.",
        "derniere_maj": _fmt_dt(updated.get("derniere_maj")) or _fmt_dt(now_iso),
    }, 200


# ----------------------------------------------------------------------
# Handler GET /grdf/droits-acces — liste paginée
# ----------------------------------------------------------------------

LIST_LIMIT_DEFAULT = 50
LIST_LIMIT_MAX = 100


def _erreur_champ(champ: str, message: str) -> tuple[dict, int]:
    """Réponse 400 alignée sur le reste de l'API (CHAMP_INVALIDE + champ)."""
    return {"erreur": "CHAMP_INVALIDE", "message": message, "champ": champ}, 400


def _parse_pagination_int(raw, champ: str, minimum: int, maximum: Optional[int]):
    """
    Parse un entier de pagination borné. Retourne (valeur, None) si OK,
    sinon (None, réponse_400).
    """
    if raw is None or str(raw).strip() == "":
        return None, None  # absent → le caller appliquera le défaut
    try:
        value = int(str(raw).strip())
    except ValueError:
        borne = f"entre {minimum} et {maximum}" if maximum is not None else f"≥ {minimum}"
        return None, _erreur_champ(champ, f"{champ} doit être un entier {borne}.")
    if value < minimum or (maximum is not None and value > maximum):
        borne = f"entre {minimum} et {maximum}" if maximum is not None else f"≥ {minimum}"
        return None, _erreur_champ(champ, f"{champ} doit être {borne}.")
    return value, None


def handle_list(req) -> tuple[dict, int]:
    """
    Liste paginée des PCE (forme d'item ALLÉGÉE, 9 champs).

    Pipeline : filtre IP (403) → validation params (400) → filtrage statut
    (égalité exacte sur le statut brut) → tri id_pce → pagination → 200.

    Query params : statut (un des 8 internes), limit (1..100, déf. 50),
    offset (≥0, déf. 0).
    """
    # --- 1. Filtre IP ---
    if not is_ip_allowed(req):
        return {
            "erreur": "IP_NON_AUTORISEE",
            "message": "Votre adresse IP n'est pas autorisée à accéder à cette API.",
        }, 403

    params = getattr(req, "params", None) or {}

    # --- 2a. Validation statut (aligné CHAMP_INVALIDE) ---
    statut = params.get("statut")
    statut = statut.strip() if isinstance(statut, str) else statut
    if statut:
        if statut not in STATUTS_INTERNES:
            return _erreur_champ(
                "statut",
                "statut doit être l'un de : " + ", ".join(STATUTS_INTERNES) + ".",
            )
    else:
        statut = None

    # --- 2b. Validation limit / offset (400 strict) ---
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

    # --- 3. Chargement + filtrage (égalité simple sur le statut brut) ---
    records = registry_dao.list_all()
    if statut:
        records = [
            r for r in records
            if _str_or_none(r.get("etat_droit_acces")) == statut
        ]

    # --- 4. Tri stable (pagination cohérente) puis découpe ---
    records.sort(key=lambda r: str(r.get("id_pce") or ""))
    total = len(records)
    page = records[offset:offset + limit]
    resultats = [_serialize_pce(r, light=True) for r in page]

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "resultats": resultats,
    }, 200
