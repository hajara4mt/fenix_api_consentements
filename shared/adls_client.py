"""
shared/adls_client.py
──────────────────────────────────────────────────────────────────────────
Helpers Delta + ADLS réutilisables par toutes les functions.

Toutes les fonctions de ce module utilisent shared.settings pour la config
(connexion Azure, paths Bronze/Silver).

OPÉRATIONS SUR LES TABLES DELTA SILVER
    read_table(name)            → DataFrame entier
    read_table_filtered(name, predicate)  → DataFrame filtré
    count_rows(name)            → nombre de lignes
    append_rows(name, df)       → INSERT (append) avec validation schéma
    update_rows(name, predicate, updates)   → UPDATE WHERE
    delete_rows(name, predicate)            → DELETE WHERE
    upsert_rows(name, df, key_cols)         → MERGE (INSERT ou UPDATE)

HELPERS MÉTIER ENEDIS
    update_pdl_statut(id_pdl, statut, ...)  → UPDATE pdl WHERE id_pdl=...
    insert_log_erreur(id_pdl, code, message, service)
    set_pdl_erreur_json(id_pdl, code, message)
    clear_pdl_erreur(id_pdl)

SAUVEGARDE BRONZE
    save_bronze_xml(endpoint, id_pdl, xml_content, suffix='')
"""

import json
import logging
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
import pyarrow as pa
from azure.storage.blob import BlobServiceClient
from deltalake import DeltaTable, write_deltalake

from shared.settings import (
    AZURE_STORAGE_CONNECTION_STRING,
    BRONZE_PATH,
    CONTAINER_NAME,
    get_silver_table_uri,
    get_storage_options,
)
from shared.schemas import SCHEMAS, get_schema, validate_dataframe


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# LECTURE
# ═══════════════════════════════════════════════════════════════════════════


def read_table(name: str) -> pd.DataFrame:
    """Charge entièrement une table Delta Silver en DataFrame Pandas.

    Pour les petites tables. Pour les grosses, utiliser read_table_filtered.
    """
    _ensure_table_exists(name)
    uri = get_silver_table_uri(name)
    dt = DeltaTable(uri, storage_options=get_storage_options())
    df = dt.to_pandas()
    logger.debug(f"read_table('{name}') → {len(df)} lignes")
    return df


def read_table_filtered(
    name: str,
    column: str,
    operator: str,
    value,
) -> pd.DataFrame:
    """Lit une table Delta avec un filtre simple (column op value).

    Exemples :
        read_table_filtered('pdl', 'statut', '=', 'nouveau')
        read_table_filtered('demandes_historique', 'fichier_traite', '=', 'false')

    Operators supportés : '=', '!=', '<', '>', '<=', '>='
    """
    _ensure_table_exists(name)
    uri = get_silver_table_uri(name)
    dt = DeltaTable(uri, storage_options=get_storage_options())

    # deltalake supporte les filtres via to_pandas(partitions=...) mais c'est
    # limité aux colonnes de partition. Plus simple : filtrer en Pandas après lecture.
    # Pour optimiser plus tard : utiliser pyarrow.dataset avec pushdown predicates.
    df = dt.to_pandas()

    ops = {
        "=": lambda c, v: df[c] == v,
        "!=": lambda c, v: df[c] != v,
        "<": lambda c, v: df[c] < v,
        ">": lambda c, v: df[c] > v,
        "<=": lambda c, v: df[c] <= v,
        ">=": lambda c, v: df[c] >= v,
    }
    if operator not in ops:
        raise ValueError(f"Opérateur '{operator}' non supporté. Choix : {list(ops)}")

    mask = ops[operator](column, value)
    df_filtered = df[mask].reset_index(drop=True)
    logger.debug(
        f"read_table_filtered('{name}', {column}{operator}{value!r}) → "
        f"{len(df_filtered)}/{len(df)} lignes"
    )
    return df_filtered


def count_rows(name: str) -> int:
    """Compte les lignes d'une table Delta.

    Implémentation simple : on charge la table en Pandas et on compte.
    Pour de très grosses tables on pourrait optimiser via les stats Delta,
    mais ce n'est pas un goulot d'étranglement ici.
    """
    _ensure_table_exists(name)
    uri = get_silver_table_uri(name)
    dt = DeltaTable(uri, storage_options=get_storage_options())
    return len(dt.to_pandas())


# ═══════════════════════════════════════════════════════════════════════════
# ÉCRITURE
# ═══════════════════════════════════════════════════════════════════════════


def append_rows(name: str, df: pd.DataFrame, validate: bool = True) -> int:
    """Append des lignes dans une table Delta après validation du schéma.

    Args:
        name : nom de la table ('pdl', 'dtc', ...)
        df : DataFrame Pandas avec les colonnes correspondant au schéma
        validate : si True, valide df contre le schéma avant écriture

    Returns:
        Nombre de lignes ajoutées.

    Raises:
        ValidationError : si le DataFrame ne respecte pas le schéma.
    """
    if len(df) == 0:
        logger.debug(f"append_rows('{name}') → 0 lignes, skip")
        return 0

    if validate:
        validate_dataframe(df, name, strict=True)

    schema = get_schema(name)
    uri = get_silver_table_uri(name)

    table = pa.Table.from_pandas(df, schema=schema, preserve_index=False)
    write_deltalake(
        uri,
        table,
        mode="append",
        storage_options=get_storage_options(),
    )
    logger.info(f"append_rows('{name}') → +{len(df)} ligne(s)")
    return len(df)


def update_rows(
    name: str,
    predicate: str,
    updates: dict[str, str],
) -> None:
    """UPDATE WHERE — modifie les lignes d'une table Delta.

    Args:
        name : nom de la table
        predicate : expression SQL WHERE (ex: "id_pdl = '12345'")
        updates : dict colonne → expression SQL (ex: {"statut": "'traite'"})
                  ⚠️ Les chaînes doivent être quotées : "'traite'", pas "traite"
                  ⚠️ Les dates : "TIMESTAMP '2026-06-09 12:00:00'"

    Exemples :
        update_rows('pdl', "id_pdl = '12345'", {"statut": "'traite'"})
        update_rows('services_souscrits',
                    "id_service_souscrit = 'ABC'",
                    {"etat_activation": "'RESILIE'"})
    """
    _ensure_table_exists(name)
    uri = get_silver_table_uri(name)
    dt = DeltaTable(uri, storage_options=get_storage_options())
    dt.update(predicate=predicate, updates=updates)
    logger.info(f"update_rows('{name}') WHERE {predicate} SET {list(updates.keys())}")


def delete_rows(name: str, predicate: str) -> None:
    """DELETE WHERE — supprime les lignes correspondantes.

    Args:
        name : nom de la table
        predicate : expression SQL WHERE
    """
    _ensure_table_exists(name)
    uri = get_silver_table_uri(name)
    dt = DeltaTable(uri, storage_options=get_storage_options())
    dt.delete(predicate=predicate)
    logger.info(f"delete_rows('{name}') WHERE {predicate}")


def upsert_rows(
    name: str,
    df: pd.DataFrame,
    key_cols: list[str],
    validate: bool = True,
) -> dict:
    """MERGE — INSERT ou UPDATE selon les clés.

    Pour chaque ligne de df : si une ligne avec les mêmes key_cols existe,
    elle est UPDATÉE ; sinon, INSERT.

    Args:
        name : nom de la table
        df : DataFrame des nouvelles données
        key_cols : colonnes qui définissent l'identité (ex: ['id_pdl'])
        validate : si True, valide df contre le schéma

    Returns:
        Dict avec les stats : {"inserted": N, "updated": M}

    Exemple (services_souscrits PK composite) :
        upsert_rows(
            'services_souscrits',
            df_new_services,
            key_cols=['id_pdl', 'id_service_souscrit'],
        )
    """
    if len(df) == 0:
        return {"inserted": 0, "updated": 0}

    if validate:
        validate_dataframe(df, name, strict=True)

    schema = get_schema(name)
    uri = get_silver_table_uri(name)
    dt = DeltaTable(uri, storage_options=get_storage_options())

    source = pa.Table.from_pandas(df, schema=schema, preserve_index=False)

    # Construction du predicate de merge sur les clés
    merge_predicate = " AND ".join(
        f"target.{k} = source.{k}" for k in key_cols
    )

    # Toutes les colonnes non-clé sont à mettre à jour si match
    update_cols = [f.name for f in schema if f.name not in key_cols]
    update_set = {col: f"source.{col}" for col in update_cols}

    # Toutes les colonnes en INSERT (pour les not-matched)
    insert_set = {f.name: f"source.{f.name}" for f in schema}

    result = (
        dt.merge(
            source=source,
            predicate=merge_predicate,
            source_alias="source",
            target_alias="target",
        )
        .when_matched_update(updates=update_set)
        .when_not_matched_insert(updates=insert_set)
        .execute()
    )

    stats = {
        "inserted": result.get("num_target_rows_inserted", 0),
        "updated": result.get("num_target_rows_updated", 0),
    }
    logger.info(f"upsert_rows('{name}') → {stats}")
    return stats


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS MÉTIER ENEDIS
# ═══════════════════════════════════════════════════════════════════════════


def update_pdl_statut(
    id_pdl: str,
    statut: str,
    statut_cdc: Optional[str] = None,
    statut_dm: Optional[str] = None,
    commentaire: Optional[str] = None,
    erreur_json: Optional[str] = None,
) -> None:
    """Met à jour le statut et les champs annexes d'un PDL.

    Args:
        id_pdl : identifiant du compteur
        statut : nouveau statut ('processing', 'traite', 'erreur', ...)
        statut_cdc : 'true'/'false' éligibilité CDC (optionnel)
        statut_dm : 'true'/'false' éligibilité DM (optionnel)
        commentaire : annotation libre (varchar 50 — ne PAS y mettre du JSON)
        erreur_json : JSON court de l'erreur ENEDIS (colonne 'erreur')

    date_modification est mise à jour automatiquement via NOW().
    """
    updates = {
        "statut": f"'{_sql_escape(statut)}'",
        "date_modification": "current_timestamp()",
    }
    if statut_cdc is not None:
        updates["statut_cdc"] = f"'{_sql_escape(statut_cdc)}'"
    if statut_dm is not None:
        updates["statut_dm"] = f"'{_sql_escape(statut_dm)}'"
    if commentaire is not None:
        # tronque à 50 caractères (contrainte MySQL varchar(50))
        commentaire_safe = commentaire[:50]
        updates["commentaire"] = f"'{_sql_escape(commentaire_safe)}'"
    if erreur_json is not None:
        updates["erreur"] = f"'{_sql_escape(erreur_json)}'"

    update_rows(
        "pdl",
        predicate=f"id_pdl = '{_sql_escape(id_pdl)}'",
        updates=updates,
    )


def set_pdl_erreur_json(
    id_pdl: str,
    code: str,
    message: str,
) -> None:
    """Stocke le JSON court de la dernière erreur ENEDIS dans pdl.erreur.

    Format produit :
        {"code_statut_traitement":"...","message_retour_traitement":"..."}
    """
    erreur_dict = {
        "code_statut_traitement": code,
        "message_retour_traitement": message,
    }
    erreur_json = json.dumps(erreur_dict, ensure_ascii=False)
    update_rows(
        "pdl",
        predicate=f"id_pdl = '{_sql_escape(id_pdl)}'",
        updates={
            "erreur": f"'{_sql_escape(erreur_json)}'",
            "date_modification": "current_timestamp()",
        },
    )


def clear_pdl_erreur(id_pdl: str) -> None:
    """Remet la colonne 'erreur' à NULL pour un PDL (utilisé après succès)."""
    update_rows(
        "pdl",
        predicate=f"id_pdl = '{_sql_escape(id_pdl)}'",
        updates={
            "erreur": "NULL",
            "date_modification": "current_timestamp()",
        },
    )


def insert_log_erreur(
    id_pdl: str,
    code_erreur: str,
    message_erreur: str,
    service_appele: str,
    timestamp: Optional[datetime] = None,
) -> None:
    """Append une ligne dans log_erreur.

    Args:
        id_pdl : compteur concerné
        code_erreur : code SGT (SGT4G3, SGT570, ...)
        message_erreur : message lisible
        service_appele : endpoint SOAP qui a planté ('ConsultationMesures', ...)
        timestamp : par défaut NOW()
    """
    ts = timestamp or datetime.now(timezone.utc).replace(tzinfo=None)
    df = pd.DataFrame([{
        "id_pdl": id_pdl,
        "code_erreur": code_erreur,
        "message_erreur": message_erreur,
        "timestamp": ts,
        "service_appele": service_appele,
    }])
    append_rows("log_erreur", df, validate=True)


def insert_demande_historique(
    id_affaire: str,
    id_pdl: str,
    code_prestation: str,
    mesures_corrigees: str,
    mesures_type_code: str,
    date_debut_demandee: str,
    date_fin_demandee: str,
    mesures_pas: Optional[str] = None,
) -> None:
    """Append une ligne dans demandes_historique (F2).

    Pas de UPSERT côté Delta : si la même id_affaire arrive 2 fois (ne devrait
    pas, ENEDIS génère des id uniques), on aura un doublon — à dédupliquer plus
    tard. NiFi utilisait `INSERT IGNORE` côté MySQL (clé primaire = id_affaire).

    Args:
        id_affaire : identifiant unique retourné par ENEDIS (clé primaire)
        id_pdl : compteur concerné
        code_prestation : code variante R (ex: 'R63_BRUTE_SOUTIRAGE').
                          ⚠️ référentiel DIFFÉRENT de services_souscrits.code_prestation
        mesures_corrigees : 'true' ou 'false'
        mesures_type_code : 'COURBES', 'INDEX' (ou 'PMAX' si réactivé un jour)
        date_debut_demandee : 'yyyy-MM-dd'
        date_fin_demandee   : 'yyyy-MM-dd'
        mesures_pas : optionnel, on laisse None pour M023 (champ non renvoyé)
    """
    df = pd.DataFrame([{
        "id_affaire": id_affaire,
        "id_pdl": id_pdl,
        "code_prestation": code_prestation,
        "mesures_corrigees": mesures_corrigees,
        "mesures_type_code": mesures_type_code,
        "mesures_pas": mesures_pas,
        "date_debut_demandee": pd.to_datetime(date_debut_demandee).date(),
        "date_fin_demandee": pd.to_datetime(date_fin_demandee).date(),
        "datetime_requete": datetime.now(timezone.utc).replace(tzinfo=None),
        "date_premiere_valeur": None,   # rempli par F5 plus tard
        "fichier_traite": "false",      # F5 le passera à 'true' une fois parsé
    }])
    append_rows("demandes_historique", df, validate=True)


def demande_historique_existe_recente(
    id_pdl: str,
    code_prestation: str,
    days: int = 30,
) -> Optional[dict]:
    """Vérifie si une demande M023 équivalente existe déjà récemment.

    Permet d'éviter d'envoyer 2 fois la même demande à ENEDIS (coût + fichiers
    SFTP dupliqués). Utilisé par F2 (étape 10 de F1) avant chaque appel M023.

    Une demande est considérée comme "doublon" si :
      - Même id_pdl
      - Même code_prestation (ex: R63_BRUTE_SOUTIRAGE)
      - datetime_requete dans les `days` derniers jours

    Args:
        id_pdl : PDL concerné
        code_prestation : code variante R (ex: 'R63_BRUTE_SOUTIRAGE')
        days : fenêtre de recherche (par défaut 30 jours)

    Returns:
        dict avec les infos de la demande existante (id_affaire, datetime_requete...)
        si une demande récente existe, sinon None.
    """
    from datetime import timedelta

    try:
        df = read_table_filtered("demandes_historique", "id_pdl", "=", id_pdl)
    except Exception as e:
        logger.warning(
            f"Lecture demandes_historique a échoué (table vide ?) : {e}"
        )
        return None

    if df is None or len(df) == 0:
        return None

    # Filtrer par code_prestation
    df_filtered = df[df["code_prestation"] == code_prestation]
    if len(df_filtered) == 0:
        return None

    # Filtrer par fenêtre temporelle
    seuil = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    # datetime_requete peut être NaT ou timestamp
    df_recent = df_filtered[
        df_filtered["datetime_requete"].notna()
        & (df_filtered["datetime_requete"] >= seuil)
    ]

    if len(df_recent) == 0:
        return None

    # Prendre la plus récente (au cas où il y en aurait plusieurs)
    row = df_recent.sort_values("datetime_requete", ascending=False).iloc[0]
    return {
        "id_affaire": row["id_affaire"],
        "datetime_requete": row["datetime_requete"],
        "code_prestation": row["code_prestation"],
    }


def write_donnees_mesures(mesures: list[dict]) -> int:
    """Append des mesures dans la table Silver donnees_mesures (F3).

    Fidèle au format pivot Energisme + bornes de période :
      Colonnes attendues dans chaque dict :
        id_pdl, label, val, ts, date_debut, date_fin, fmt, uuid, misc

    Ajoute automatiquement la colonne d'audit :
      - ingestion_ts : timestamp précis d'écriture

    ⚠️ `ingestion_date` supprimée (artefact Python, jamais une vraie partition).
       La partition cible = `id_pdl` (1 sous-dossier par compteur).

    Args:
        mesures : liste de dicts au format pivot Energisme.
                  Chaque dict doit contenir :
                  id_pdl, label, val, ts, date_debut, date_fin, fmt, uuid, misc

    Returns:
        Nombre de lignes insérées/mises à jour (0 si liste vide).
    """
    if not mesures:
        logger.info("write_donnees_mesures : liste vide, rien à écrire")
        return 0

    ingestion_ts = datetime.now(timezone.utc).replace(tzinfo=None)

    df = pd.DataFrame(mesures)

    # Sécurité : vérifier que toutes les colonnes attendues sont présentes
    expected_cols = {"id_pdl", "label", "val", "ts", "date_debut", "date_fin", "fmt", "uuid", "misc"}
    missing = expected_cols - set(df.columns)
    if missing:
        raise ValueError(
            f"write_donnees_mesures : colonnes manquantes dans les mesures : {missing}. "
            f"Attendu : {expected_cols}"
        )

    df["ingestion_ts"] = ingestion_ts

    # Ne garder QUE les colonnes du schéma (ordre + drop des éventuelles colonnes extra)
    df = df[[
        "id_pdl", "label", "val", "ts", "date_debut", "date_fin",
        "fmt", "uuid", "misc", "ingestion_ts",
    ]]

    # MERGE keep-latest sur (id_pdl, label, ts) :
    #   - match   → UPDATE (on garde la dernière)
    #   - no match → INSERT
    # Table partitionnée par id_pdl (1 sous-dossier par compteur).
    stats = upsert_rows(
        "donnees_mesures",
        df,
        key_cols=["id_pdl", "label", "ts"],
        validate=True,
    )
    n = stats["inserted"] + stats["updated"]
    logger.info(
        f"write_donnees_mesures : {stats['inserted']} insérée(s), "
        f"{stats['updated']} mise(s) à jour (keep-latest)"
    )
    return n


# ═══════════════════════════════════════════════════════════════════════════
# SAUVEGARDE BRONZE
# ═══════════════════════════════════════════════════════════════════════════


def save_bronze_xml(
    endpoint: str,
    id_pdl: str,
    xml_content: str,
    suffix: str = "",
) -> str:
    """Sauvegarde une réponse XML brute dans Bronze.

    Args:
        endpoint : nom du dossier endpoint ('consultation_dtc', 'recherche_services',
                   'commande_collecte')
        id_pdl : compteur concerné
        xml_content : la réponse XML brute (string)
        suffix : suffixe optionnel pour le nom de fichier (ex: code_flux R50)

    Returns:
        Le chemin complet du blob créé (pour log/référence).

    Structure de stockage :
        bronze/enedis/{endpoint}/{id_pdl}/{timestamp}[_{suffix}].xml
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    suffix_part = f"__{suffix}" if suffix else ""
    blob_path = (
        f"{BRONZE_PATH}/{endpoint}/{id_pdl}/{timestamp}{suffix_part}.xml"
    )

    client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
    container_client = client.get_container_client(CONTAINER_NAME)

    container_client.upload_blob(
        name=blob_path,
        data=xml_content.encode("utf-8"),
        overwrite=False,  # éviter d'écraser (le timestamp garantit l'unicité)
        content_type="application/xml",
    )

    logger.debug(f"save_bronze_xml → {blob_path}")
    return blob_path


# ═══════════════════════════════════════════════════════════════════════════
# Helpers privés
# ═══════════════════════════════════════════════════════════════════════════


def _ensure_table_exists(name: str) -> None:
    """Vérifie qu'une table Delta existe, lève une erreur claire sinon."""
    if name not in SCHEMAS:
        raise ValueError(
            f"Table inconnue : '{name}'. "
            f"Tables disponibles : {list(SCHEMAS.keys())}"
        )


def _sql_escape(value: str) -> str:
    """Échappe les apostrophes dans une valeur SQL (basique mais suffisant ici)."""
    if value is None:
        return ""
    return str(value).replace("'", "''")