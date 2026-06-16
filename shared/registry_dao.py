"""
Module DAO pour le registre des droits d'accès GRDF.

Gère droits_acces.parquet, le fichier Silver qui centralise :
  - Les PCE déclarés par utilisateur (insertion via API)
  - L'état des consentements (Active, Pending, Refusée, Révoquée, etc.)
  - Les périmètres autorisés par client (consos, techniques, contractuelles)
  - Les timestamps de traçabilité (dernière collecte, dernier appel, onboarding)

3 sources de remplissage des champs :
  - USER       : saisis par utilisateur via route POST /grdf/droits-acces
  - GRDF       : renvoyés par les appels API GRDF (batches)
  - PIPELINE   : timestamps et statuts calculés par le pipeline lui-même

3 méthodes d'écriture principales :
  - insert(id_pce, fields)      : nouveau PCE déclaré par user (erreur si existe)
  - upsert(id_pce, fields)      : merge ciblé (1 PCE)
  - upsert_many(records)        : merge en masse (1 lecture + 1 écriture parquet)

Toutes les écritures sont protégées par un lease ADLS pour la cohérence
en cas de batches concurrents.
"""

import io
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from azure.core.exceptions import (
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient, BlobLeaseClient, BlobServiceClient

from .config import Config

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Constantes métier
# ----------------------------------------------------------------------

# États possibles du droit d'accès (cycle de vie aligné avec NiFi + atelier).
# ATTENTION : la casse compte (comparaisons strict).
DROIT_STATES = {
    "nouveau",          # déclaré côté plateforme, pas encore envoyé à GRDF
    "A valider",        # envoyé à GRDF, en attente de validation du client (email)
    "A revérifier",     # échec déclaration (3 tentatives) ou champ manquant
    "Active",           # consentement validé, données récupérables
    "Refusée",          # client a refusé
    "Révoquée",         # consentement retiré
    "Obsolète",         # PCE inactif
    "résilié",          # PCE résilié côté GRDF
}

# Colonnes booléennes (à convertir "Vrai"/"Faux" ↔ bool lors des read/write)
BOOL_COLUMNS = {
    "perim_donnees_contractuelles",
    "perim_donnees_techniques",
    "perim_donnees_informatives",
    "perim_donnees_publiees",
}


# ----------------------------------------------------------------------
# Cache mémoire du DataFrame (par worker Python)
# ----------------------------------------------------------------------

_cached_df: Optional[pd.DataFrame] = None
_cached_at: Optional[datetime] = None


def _invalidate_cache() -> None:
    """Force la prochaine lecture à relire depuis ADLS."""
    global _cached_df, _cached_at
    _cached_df = None
    _cached_at = None


# ----------------------------------------------------------------------
# Clients Azure (initialisés une fois)
# ----------------------------------------------------------------------

_credential: Optional[DefaultAzureCredential] = None


def _get_credential() -> DefaultAzureCredential:
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _blob_service() -> BlobServiceClient:
    """
    BlobServiceClient.

    Si une connection string est fournie (STORAGE_CONNECTION_STRING ou
    AZURE_STORAGE_CONNECTION_STRING), on l'utilise — pratique en local sans
    `az login`. Sinon : account_url + DefaultAzureCredential (managed identity
    en prod / az login en local).
    """
    conn = os.environ.get("STORAGE_CONNECTION_STRING") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        return BlobServiceClient.from_connection_string(conn)
    return BlobServiceClient(
        account_url=Config.storage_account_url(),
        credential=_get_credential(),
    )


def _get_blob_client() -> BlobClient:
    """BlobClient pointant vers droits_acces.parquet."""
    return _blob_service().get_blob_client(
        container=Config.CONTAINER_NAME,
        blob=Config.droits_acces_blob_path(),
    )


# ----------------------------------------------------------------------
# Helpers de conversion (Vrai/Faux <-> bool)
# ----------------------------------------------------------------------

def _convert_grdf_bool(value: Any) -> Optional[bool]:
    """
    Convertit une valeur GRDF en bool Python.
        "Vrai" → True
        "Faux" → False
        None   → None (préservé)
        Autre  → None (avec warning)
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() == "vrai":
            return True
        if value.strip().lower() == "faux":
            return False
    logger.warning("Valeur booléenne GRDF inattendue : %r", value)
    return None


def _convert_bools_in_record(record: dict) -> dict:
    """Applique _convert_grdf_bool sur toutes les colonnes booléennes du record."""
    for col in BOOL_COLUMNS:
        if col in record:
            record[col] = _convert_grdf_bool(record[col])
    return record


# ----------------------------------------------------------------------
# Lecture / écriture parquet
# ----------------------------------------------------------------------

def _read_parquet() -> pd.DataFrame:
    """
    Lit droits_acces.parquet depuis ADLS.
    Utilise le cache mémoire (TTL configurable) pour éviter les lectures répétées.
    Renvoie un DataFrame vide si le fichier n'existe pas encore.
    """
    global _cached_df, _cached_at

    # Cache valide ?
    if _cached_df is not None and _cached_at is not None:
        age = (datetime.now(timezone.utc) - _cached_at).total_seconds()
        if age < Config.REGISTRY_CACHE_TTL_SECONDS:
            logger.debug("DataFrame réutilisé depuis le cache (age=%ds)", int(age))
            return _cached_df.copy()

    # Lecture depuis ADLS
    blob_client = _get_blob_client()
    try:
        data = blob_client.download_blob().readall()
    except ResourceNotFoundError:
        logger.info("droits_acces.parquet n'existe pas encore (premier appel)")
        empty_df = pd.DataFrame()
        _cached_df = empty_df
        _cached_at = datetime.now(timezone.utc)
        return empty_df.copy()

    table = pq.read_table(io.BytesIO(data))
    df = table.to_pandas()

    logger.info("droits_acces.parquet lu : %d lignes, %d colonnes", len(df), len(df.columns))

    _cached_df = df
    _cached_at = datetime.now(timezone.utc)
    return df.copy()


def _write_parquet(df: pd.DataFrame, lease_id: Optional[str] = None) -> None:
    """
    Écrit le DataFrame en Parquet dans ADLS.
    Si lease_id est fourni, l'écriture est protégée par ce lease.
    """
    buf = io.BytesIO()
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)

    blob_client = _get_blob_client()
    if lease_id:
        blob_client.upload_blob(buf.getvalue(), overwrite=True, lease=lease_id)
    else:
        blob_client.upload_blob(buf.getvalue(), overwrite=True)

    logger.info("droits_acces.parquet écrit : %d lignes", len(df))
    _invalidate_cache()


def _ensure_blob_exists() -> None:
    """
    S'assure que le blob existe (nécessaire pour acquire_lease).
    Si absent, crée un parquet vide.
    """
    blob_client = _get_blob_client()
    try:
        blob_client.get_blob_properties()
        return  # blob existe déjà
    except ResourceNotFoundError:
        pass

    logger.info("Création du parquet vide initial")
    empty_df = pd.DataFrame()
    buf = io.BytesIO()
    table = pa.Table.from_pandas(empty_df, preserve_index=False)
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)

    try:
        blob_client.upload_blob(buf.getvalue(), overwrite=False)
    except ResourceExistsError:
        # Race condition : un autre worker a créé le blob entre temps. OK.
        pass


def _write_parquet_with_lease(modifier_fn) -> None:
    """
    Écrit le parquet avec protection par lease blob.

    Pattern :
      1. S'assure que le blob existe
      2. Acquiert un lease (avec boucle d'attente si déjà pris)
      3. Re-lit le parquet sous lease (au cas où un autre worker l'a modifié)
      4. Applique modifier_fn(df) qui retourne le nouveau df
      5. Écrit le résultat avec le lease
      6. Libère le lease (toujours, même en cas d'erreur)

    Args:
        modifier_fn: callable qui prend le DataFrame actuel et renvoie le DataFrame modifié
    """
    _ensure_blob_exists()
    blob_client = _get_blob_client()

    # Boucle d'attente pour acquérir le lease
    lease: Optional[BlobLeaseClient] = None
    waited = 0
    while waited < Config.REGISTRY_LEASE_MAX_WAIT_SECONDS:
        try:
            lease = blob_client.acquire_lease(
                lease_duration=Config.REGISTRY_LEASE_DURATION_SECONDS,
            )
            logger.debug("Lease acquis sur droits_acces.parquet")
            break
        except HttpResponseError as e:
            if e.status_code != 409:
                raise
            logger.info(
                "Lease déjà détenu, attente %ds (cumul: %ds / %ds)",
                Config.REGISTRY_LEASE_RETRY_DELAY_SECONDS,
                waited,
                Config.REGISTRY_LEASE_MAX_WAIT_SECONDS,
            )
            time.sleep(Config.REGISTRY_LEASE_RETRY_DELAY_SECONDS)
            waited += Config.REGISTRY_LEASE_RETRY_DELAY_SECONDS

    if lease is None:
        raise RuntimeError(
            f"Impossible d'acquérir le lease sur droits_acces.parquet "
            f"après {Config.REGISTRY_LEASE_MAX_WAIT_SECONDS}s"
        )

    try:
        # Re-lecture sous lease (un autre worker a pu modifier pendant l'attente)
        _invalidate_cache()
        df = _read_parquet()

        df_new = modifier_fn(df)

        _write_parquet(df_new, lease_id=lease.id)

    finally:
        try:
            lease.release()
            logger.debug("Lease libéré")
        except Exception as e:
            logger.warning("Erreur libération lease (ignoré) : %s", e)


# ----------------------------------------------------------------------
# Helper R12 : déduplication multi-droits GRDF
# ----------------------------------------------------------------------

def _dedup_records_by_pce(records: list[dict]) -> list[dict]:
    """
    Déduplique les records par id_pce.

    GRDF peut renvoyer plusieurs droits pour un même id_pce (cas typique :
    renouvellement annuel = 1 droit historique Obsolète + 1 nouveau droit Active).
    Le parquet stocke 1 ligne par id_pce, il faut donc choisir lequel garder.

    Règle métier (validée empiriquement sur 15/15 cas de doublons R12) :
      Garder le droit le plus récent au sens GRDF
      = date_debut_droit_acces DESC
      avec tie-breaker date_creation DESC.

    Les records sans id_pce sont conservés tels quels en queue de liste
    (la boucle upsert_many les ignore déjà avec un warning).

    Args:
        records: liste brute reçue de GRDF (ex: 1156 records dont 127 doublons)

    Returns:
        liste dédupliquée (ex: 1029 records, 1 par id_pce) + records sans id_pce
    """
    if not records:
        return records

    # Séparer les records selon présence d'id_pce
    with_pce = [r for r in records if r.get("id_pce")]
    without_pce = [r for r in records if not r.get("id_pce")]

    # Tri DESC sur date_debut_droit_acces, tie-break date_creation DESC.
    # None / "" traités comme chaîne vide → moins prioritaires (epoch implicite).
    def _sort_key(r: dict) -> tuple:
        return (
            r.get("date_debut_droit_acces") or "",
            r.get("date_creation") or "",
        )

    with_pce_sorted = sorted(with_pce, key=_sort_key, reverse=True)

    # Dédup : keep="first" car trié DESC, donc le 1er vu = le plus récent.
    # sorted() Python est stable → si égalité parfaite (très rare), on préserve
    # l'ordre d'origine.
    seen: set[str] = set()
    deduped: list[dict] = []
    for r in with_pce_sorted:
        id_pce = str(r["id_pce"])
        if id_pce not in seen:
            seen.add(id_pce)
            deduped.append(r)

    return deduped + without_pce


# ----------------------------------------------------------------------
# API publique : LECTURE
# ----------------------------------------------------------------------

def get(id_pce: str) -> Optional[dict]:
    """Récupère un PCE par son id. Renvoie None s'il n'existe pas."""
    df = _read_parquet()
    if df.empty or "id_pce" not in df.columns:
        return None
    row = df[df["id_pce"] == id_pce]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def exists(id_pce: str) -> bool:
    """Renvoie True si le PCE est présent dans le parquet."""
    return get(id_pce) is not None


def list_all() -> list[dict]:
    """Renvoie tous les PCE du parquet."""
    df = _read_parquet()
    if df.empty:
        return []
    return df.to_dict(orient="records")


def list_by_state(state: str) -> list[dict]:
    """Filtre les PCE par état (Active, Pending, Refusée, etc.)."""
    if state not in DROIT_STATES:
        logger.warning("État inconnu : %r (valeurs autorisées : %s)", state, DROIT_STATES)
    df = _read_parquet()
    if df.empty or "etat_droit_acces" not in df.columns:
        return []
    filtered = df[df["etat_droit_acces"] == state]
    return filtered.to_dict(orient="records")


def list_active_without_field(field: str) -> list[dict]:
    """
    Renvoie tous les PCE Active dont le champ `field` est null/vide.

    Utilisé par les batches one-shot (get_technical, get_contractual, onboard_history)
    pour ne traiter que les PCE qui n'ont pas encore eu cette donnée collectée.

    Args:
        field: nom du champ à tester (ex: "dernier_appel_techniques",
               "dernier_appel_contractuelles", "date_onboard_history")
    """
    df = _read_parquet()
    if df.empty or "etat_droit_acces" not in df.columns:
        return []

    actives = df[df["etat_droit_acces"] == "Active"]

    if field not in actives.columns:
        # Colonne pas encore créée → tous les Active sont à traiter
        return actives.to_dict(orient="records")

    # PCE Active sans valeur dans le champ (null ou vide)
    mask = actives[field].isna() | (actives[field].astype(str).str.strip() == "")
    return actives[mask].to_dict(orient="records")


def count() -> int:
    """Renvoie le nombre total de PCE."""
    df = _read_parquet()
    return len(df)


# ----------------------------------------------------------------------
# API publique : ÉCRITURE
# ----------------------------------------------------------------------

def insert(id_pce: str, fields: dict) -> None:
    """
    Insère un nouveau PCE déclaré par un utilisateur.
    Si le PCE existe déjà, lève ValueError (utiliser upsert pour merger).
    """
    record = {"id_pce": id_pce, **fields}
    record = _convert_bools_in_record(record)
    record["derniere_maj"] = datetime.now(timezone.utc).isoformat()

    def modifier(df: pd.DataFrame) -> pd.DataFrame:
        if not df.empty and "id_pce" in df.columns and (df["id_pce"] == id_pce).any():
            raise ValueError(
                f"Le PCE {id_pce} existe déjà — utiliser upsert pour le mettre à jour"
            )
        new_row = pd.DataFrame([record])
        return pd.concat([df, new_row], ignore_index=True) if not df.empty else new_row

    _write_parquet_with_lease(modifier)
    logger.info("PCE %s inséré dans droits_acces.parquet", id_pce)


def upsert(id_pce: str, fields: dict) -> None:
    """
    Met à jour un PCE existant en mergeant les nouveaux champs avec l'existant.
    Si le PCE n'existe pas, le crée.
    """
    fields_normalized = _convert_bools_in_record(dict(fields))
    fields_normalized["derniere_maj"] = datetime.now(timezone.utc).isoformat()

    def modifier(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or "id_pce" not in df.columns or not (df["id_pce"] == id_pce).any():
            record = {"id_pce": id_pce, **fields_normalized}
            new_row = pd.DataFrame([record])
            return pd.concat([df, new_row], ignore_index=True) if not df.empty else new_row

        mask = df["id_pce"] == id_pce
        for col, val in fields_normalized.items():
            if col not in df.columns:
                df[col] = None
            df.loc[mask, col] = val
        return df

    _write_parquet_with_lease(modifier)
    logger.info("PCE %s mis à jour via upsert", id_pce)


def upsert_many(records: list[dict]) -> dict:
    """
    Insère ou met à jour PLUSIEURS PCE en une seule opération.

    BEAUCOUP plus performant que upsert() en boucle :
    1 lecture + 1 écriture du parquet, quel que soit le nombre de records.

    Pour 1000 PCE : ~2 opérations Azure au lieu de ~3000.

    [R12] Déduplication en amont : un même id_pce peut apparaître plusieurs
    fois dans la liste (cas multi-droits GRDF, ex: renouvellement annuel).
    On garde le droit le plus récent (date_debut_droit_acces DESC + date_creation DESC)
    pour refléter le dernier état GRDF.

    Returns:
        dict avec stats: {"created": int, "updated": int, "errors": int, "deduped": int}
    """
    if not records:
        logger.info("Aucun record à upsert")
        return {"created": 0, "updated": 0, "errors": 0, "deduped": 0}

    # [R12] Déduplication en amont : 1 record par id_pce, on garde le plus récent
    n_in = len(records)
    records = _dedup_records_by_pce(records)
    n_out = len(records)
    n_deduped = n_in - n_out
    if n_deduped > 0:
        logger.info(
            "[R12] Dedup upsert_many : %d records reçus → %d uniques "
            "(%d doublons résolus, critère: date_debut_droit_acces DESC + date_creation DESC)",
            n_in, n_out, n_deduped,
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    stats = {"created": 0, "updated": 0, "errors": 0, "deduped": n_deduped}

    def modifier(df: pd.DataFrame) -> pd.DataFrame:
        existing_pce = set()
        if not df.empty and "id_pce" in df.columns:
            existing_pce = set(df["id_pce"].astype(str).tolist())

        new_rows_to_append = []

        for record in records:
            id_pce = record.get("id_pce")
            if not id_pce:
                logger.warning("Record sans id_pce ignoré : %r", record)
                stats["errors"] += 1
                continue

            try:
                fields = _convert_bools_in_record(dict(record))
                fields["derniere_maj"] = now_iso
                fields.pop("id_pce", None)

                if id_pce in existing_pce:
                    mask = df["id_pce"] == id_pce
                    for col, val in fields.items():
                        if col not in df.columns:
                            df[col] = None
                        df.loc[mask, col] = val
                    stats["updated"] += 1
                else:
                    new_row = {"id_pce": id_pce, **fields}
                    new_rows_to_append.append(new_row)
                    stats["created"] += 1
                    existing_pce.add(id_pce)

            except Exception as e:
                logger.error("Erreur traitement PCE %s : %s", id_pce, e)
                stats["errors"] += 1

        if new_rows_to_append:
            new_df = pd.DataFrame(new_rows_to_append)
            df = pd.concat([df, new_df], ignore_index=True) if not df.empty else new_df

        return df

    _write_parquet_with_lease(modifier)

    logger.info(
        "upsert_many terminé : %d créés, %d màj, %d erreurs, %d doublons résolus "
        "(sur %d records entrants)",
        stats["created"], stats["updated"], stats["errors"], stats["deduped"], n_in,
    )
    return stats


def update_field(id_pce: str, field: str, value: Any) -> None:
    """Met à jour un seul champ d'un PCE."""
    upsert(id_pce, {field: value})


def mark_collected(id_pce: str, collection_type: str, status_code: int) -> None:
    """
    Met à jour les timestamps de traçabilité après un appel API.

    Args:
        id_pce: identifiant du compteur
        collection_type: 'publiee', 'informative', 'techniques', 'contractuelles', 'replay'
        status_code: code HTTP retourné par GRDF
    """
    field_map = {
        "publiee": "derniere_collecte_publiee",
        "informative": "derniere_collecte_informative",
        "techniques": "dernier_appel_techniques",
        "contractuelles": "dernier_appel_contractuelles",
        "replay": "derniere_replay_trimestrielle",
    }
    if collection_type not in field_map:
        raise ValueError(
            f"collection_type invalide : {collection_type}. "
            f"Valeurs attendues : {list(field_map.keys())}"
        )

    upsert(id_pce, {
        field_map[collection_type]: datetime.now(timezone.utc).isoformat(),
        "api_response_code": status_code,
    })


def mark_onboarded(id_pce: str, status_code: int) -> None:
    """
    Marque un PCE comme onboardé (historique 3 ans récupéré).
    Appelé par le batch onboard_history après chaque tentative.
    À ne faire qu'UNE FOIS par PCE.
    """
    upsert(id_pce, {
        "date_onboard_history": datetime.now(timezone.utc).isoformat(),
        "api_response_onboard_history": status_code,
    })


def delete(id_pce: str) -> None:
    """
    Supprime un PCE (utile pour les tests, rare en prod).
    En prod, préférer marquer etat_droit_acces='Obsolète'.
    """
    def modifier(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty or "id_pce" not in df.columns:
            return df
        return df[df["id_pce"] != id_pce].reset_index(drop=True)

    _write_parquet_with_lease(modifier)
    logger.info("PCE %s supprimé de droits_acces.parquet", id_pce)