"""
Tests de POST /enedis/consent (handler direct, stockage Delta mocké).

Le DAO Delta (adls_client) est mocké : aucun accès réel à Azure.
"""

import json

import pandas as pd
import pytest

from api.enedis_validation import validate_consent, ValidationError


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

def _valid_body(**overrides) -> dict:
    body = {
        "id_pdl": "00000000001965",
        "partner": "ifpeb",
        "platform_code": "PF01",
        "date_signature_mandat": "2025-06-05",
        "date_debut_autorisation": "2016-01-01",
        "date_fin_autorisation": "2035-06-05",
        "raison_sociale": "Mon Entreprise SAS",
        "nom": "Dupont",
        "prenom": "Jean",
        "injection": False,
        "soutirage": True,
        "get_cdc": True,
        "get_dm": True,
    }
    body.update(overrides)
    return body


class FakeReq:
    def __init__(self, json_body, headers=None, raise_on_json=False):
        self._json = json_body
        self.headers = headers or {}
        self._raise = raise_on_json

    def get_json(self):
        if self._raise:
            raise ValueError("invalid json")
        return self._json


# ----------------------------------------------------------------------
# validate_consent
# ----------------------------------------------------------------------

def test_validate_ok_booleens_en_string():
    id_pdl, fields = validate_consent(_valid_body())
    assert id_pdl == "00000000001965"
    # booléens normalisés en strings "true"/"false"
    assert fields["injection"] == "false"
    assert fields["soutirage"] == "true"
    assert fields["get_cdc"] == "true"
    # dates en objets date (pour date32)
    from datetime import date
    assert isinstance(fields["date_debut_autorisation"], date)


def test_validate_partner_minuscule():
    _, fields = validate_consent(_valid_body(partner="IFPEB"))
    assert fields["partner"] == "ifpeb"


def test_validate_partner_non_autorise():
    with pytest.raises(ValidationError) as e:
        validate_consent(_valid_body(partner="Mon Entreprise"))
    assert e.value.champ == "partner"


def test_validate_defaut_get_cdc_dm():
    body = _valid_body()
    del body["get_cdc"]
    del body["get_dm"]
    _, fields = validate_consent(body)
    assert fields["get_cdc"] == "true"
    assert fields["get_dm"] == "true"


def test_validate_titulaire_raison_sociale_seule():
    body = _valid_body()
    del body["nom"]
    del body["prenom"]
    _, fields = validate_consent(body)
    assert fields["raison_sociale"] == "Mon Entreprise SAS"
    assert fields["nom"] is None
    assert fields["prenom"] is None


def test_validate_titulaire_nom_seul():
    body = _valid_body()
    del body["raison_sociale"]
    del body["prenom"]
    _, fields = validate_consent(body)
    assert fields["nom"] == "Dupont"
    assert fields["raison_sociale"] is None


def test_validate_titulaire_aucun():
    body = _valid_body()
    del body["raison_sociale"]
    del body["nom"]
    with pytest.raises(ValidationError) as e:
        validate_consent(body)
    assert e.value.champ == "raison_sociale"


def test_validate_prenom_optionnel():
    body = _valid_body()
    del body["prenom"]
    _, fields = validate_consent(body)  # ne lève pas
    assert fields["prenom"] is None


def test_validate_id_pdl_trop_long():
    with pytest.raises(ValidationError) as e:
        validate_consent(_valid_body(id_pdl="123456789012345"))  # 15 car. > 14
    assert e.value.champ == "id_pdl"


def test_validate_id_pdl_varchar_ok():
    # varchar(14) : pas forcément numérique, ≤ 14 caractères
    _, fields = validate_consent(_valid_body(id_pdl="PDL_ABC_12345"))
    assert fields["id_pdl"] == "PDL_ABC_12345"


def test_validate_platform_code_present():
    _, fields = validate_consent(_valid_body(platform_code="PF42"))
    assert fields["platform_code"] == "PF42"


def test_validate_platform_code_obligatoire():
    body = _valid_body()
    del body["platform_code"]
    with pytest.raises(ValidationError) as e:
        validate_consent(body)
    assert e.value.champ == "platform_code"


def test_validate_platform_code_trop_long():
    with pytest.raises(ValidationError) as e:
        validate_consent(_valid_body(platform_code="ABCDEFGHIJK"))  # 11 car. > 10
    assert e.value.champ == "platform_code"


def test_validate_civilite_invalide():
    with pytest.raises(ValidationError) as e:
        validate_consent(_valid_body(civilite="X"))
    assert e.value.champ == "civilite"


def test_validate_aucun_soutirage_injection():
    with pytest.raises(ValidationError):
        validate_consent(_valid_body(injection=False, soutirage=False))


def test_validate_aucun_cdc_dm():
    with pytest.raises(ValidationError):
        validate_consent(_valid_body(get_cdc=False, get_dm=False))


def test_validate_date_fin_avant_debut():
    with pytest.raises(ValidationError) as e:
        validate_consent(_valid_body(date_fin_autorisation="2015-01-01"))
    assert e.value.champ == "date_fin_autorisation"


# ----------------------------------------------------------------------
# handle_create_consent (adls_client mocké)
# ----------------------------------------------------------------------

def test_create_consent_201(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame())
    append = mocker.patch.object(h.adls_client, "append_rows")

    body, status = h.handle_create_consent(FakeReq(_valid_body()))

    assert status == 201
    assert body["statut"] == "nouveau"
    assert body["id_pdl"] == "00000000001965"
    # la ligne écrite a 22 colonnes, statut nouveau, booléens en string
    df = append.call_args.args[1]
    row = df.iloc[0]
    assert row["statut"] == "nouveau"
    assert row["soutirage"] == "true"
    assert row["injection"] == "false"
    assert row["platform_code"] == "PF01"
    assert set(df.columns) == {
        "id_pdl", "partner", "platform_code", "date_signature_mandat",
        "date_debut_autorisation",
        "date_fin_autorisation", "raison_sociale", "civilite", "nom", "prenom",
        "date_creation", "date_modification", "injection", "soutirage", "get_cdc",
        "get_dm", "statut_cdc", "statut_dm", "date_premiere_valeur_dm", "statut",
        "commentaire", "erreur",
    }


def test_create_consent_409(mocker):
    import api.enedis_consent as h
    mocker.patch.object(
        h.adls_client, "read_table_filtered",
        return_value=pd.DataFrame([{"id_pdl": "00000000001965", "statut": "traite"}]),
    )
    append = mocker.patch.object(h.adls_client, "append_rows")

    body, status = h.handle_create_consent(FakeReq(_valid_body()))

    assert status == 409
    assert body["erreur"] == "PDL_EXISTANT"
    assert body["statut"] == "traite"
    append.assert_not_called()


def test_create_consent_400_id_pdl(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table_filtered")
    append = mocker.patch.object(h.adls_client, "append_rows")

    body, status = h.handle_create_consent(FakeReq(_valid_body(id_pdl="123456789012345")))

    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"
    assert body["champ"] == "id_pdl"
    append.assert_not_called()


def test_create_consent_400_champ_inconnu(mocker):
    import api.enedis_consent as h
    append = mocker.patch.object(h.adls_client, "append_rows")

    body, status = h.handle_create_consent(FakeReq(_valid_body(colonne_bidon="x")))

    assert status == 400
    assert body["erreur"] == "CHAMP_INCONNU"
    assert body["champ"] == "colonne_bidon"
    append.assert_not_called()


def test_create_consent_403_ip(mocker, monkeypatch):
    import api.enedis_consent as h
    read = mocker.patch.object(h.adls_client, "read_table_filtered")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(_valid_body(), headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_create_consent(req)

    assert status == 403
    read.assert_not_called()


# ----------------------------------------------------------------------
# handle_get_consent (GET /enedis/consent/{id_pdl})
# ----------------------------------------------------------------------

def _pdl_row(**over):
    base = {
        "id_pdl": "00000000001965", "partner": "ifpeb", "platform_code": "PF01",
        "statut": "traite",
        "statut_cdc": "true", "statut_dm": "false",
        "date_creation": pd.Timestamp("2026-04-14 10:00:00"),
        "date_modification": pd.Timestamp("2026-04-15 02:01:30"),
        "erreur": None,
    }
    base.update(over)
    return base


def test_get_consent_200(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame([_pdl_row()]))

    body, status = h.handle_get_consent(FakeReq(None), "00000000001965")

    assert status == 200
    assert body["statut"] == "traite"
    assert body["platform_code"] == "PF01"   # exposé dans le GET détail
    assert body["statut_cdc"] is True       # "true" → bool JSON
    assert body["statut_dm"] is False
    assert body["date_creation"] == "2026-04-14 10:00:00"
    assert body["message_erreur"] is None
    assert set(body.keys()) == {
        "id_pdl", "partner", "platform_code", "statut", "statut_cdc", "statut_dm",
        "message_erreur", "date_creation", "date_modification",
    }


def test_get_consent_statut_cdc_null(mocker):
    import api.enedis_consent as h
    mocker.patch.object(
        h.adls_client, "read_table_filtered",
        return_value=pd.DataFrame([_pdl_row(statut="nouveau", statut_cdc=None, statut_dm=None)]),
    )
    body, _ = h.handle_get_consent(FakeReq(None), "00000000001965")
    assert body["statut_cdc"] is None
    assert body["statut_dm"] is None


def test_get_consent_message_erreur_brut(mocker):
    import api.enedis_consent as h
    erreur_json = json.dumps({"code_statut_traitement": "SGT450", "message_retour_traitement": "PDL non éligible"})
    mocker.patch.object(
        h.adls_client, "read_table_filtered",
        return_value=pd.DataFrame([_pdl_row(statut="erreur", erreur=erreur_json)]),
    )
    body, _ = h.handle_get_consent(FakeReq(None), "00000000001965")
    assert body["message_erreur"] == "PDL non éligible"


def test_get_consent_404(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame())

    body, status = h.handle_get_consent(FakeReq(None), "INCONNU")

    assert status == 404
    assert body["erreur"] == "PDL_INTROUVABLE"
    assert body["id_pdl"] == "INCONNU"


def test_get_consent_403_ip(mocker, monkeypatch):
    import api.enedis_consent as h
    read = mocker.patch.object(h.adls_client, "read_table_filtered")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_get_consent(req, "00000000001965")

    assert status == 403
    read.assert_not_called()


# ----------------------------------------------------------------------
# handle_retry_consent (POST /enedis/consent/{id_pdl}/retry)
# ----------------------------------------------------------------------

def test_retry_consent_200(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(
        h.adls_client, "read_table_filtered",
        side_effect=[
            pd.DataFrame([_pdl_row(statut="erreur")]),                                       # avant
            pd.DataFrame([_pdl_row(statut="nouveau", date_modification=pd.Timestamp("2026-06-16 09:00:00"))]),  # après
        ],
    )

    body, status = h.handle_retry_consent(FakeReq(None), "00000000001965")

    assert status == 200
    assert body["statut"] == "nouveau"
    assert body["date_modification"] == "2026-06-16 09:00:00"
    updates = update.call_args.args[2]
    assert updates["statut"] == "'nouveau'"
    assert updates["erreur"] == "NULL"


def test_retry_consent_409_non_erreur(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(h.adls_client, "read_table_filtered",
                        return_value=pd.DataFrame([_pdl_row(statut="traite")]))

    body, status = h.handle_retry_consent(FakeReq(None), "00000000001965")

    assert status == 409
    assert body["erreur"] == "STATUT_INCOMPATIBLE"
    assert body["statut"] == "traite"
    update.assert_not_called()


def test_retry_consent_404(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame())

    body, status = h.handle_retry_consent(FakeReq(None), "INCONNU")

    assert status == 404
    assert body["erreur"] == "PDL_INTROUVABLE"
    update.assert_not_called()


def test_retry_consent_403_ip(mocker, monkeypatch):
    import api.enedis_consent as h
    read = mocker.patch.object(h.adls_client, "read_table_filtered")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_retry_consent(req, "00000000001965")

    assert status == 403
    read.assert_not_called()


# ----------------------------------------------------------------------
# handle_patch_consent (PATCH /enedis/consent/{id_pdl})
# ----------------------------------------------------------------------

def _pdl_full_row(**over):
    from datetime import date
    base = {
        "id_pdl": "00000000001965", "partner": "ifpeb", "platform_code": "PF01",
        "statut": "traite",
        "date_signature_mandat": date(2025, 6, 5),
        "date_debut_autorisation": date(2016, 1, 1),
        "date_fin_autorisation": date(2035, 6, 5),
        "raison_sociale": "Mon Entreprise SAS", "civilite": None, "nom": "Dupont", "prenom": "Jean",
        "injection": "false", "soutirage": "true", "get_cdc": "true", "get_dm": "true",
        "statut_cdc": None, "statut_dm": None, "date_premiere_valeur_dm": None,
        "date_creation": pd.Timestamp("2026-04-14 10:00:00"),
        "date_modification": pd.Timestamp("2026-04-14 10:00:00"),
        "commentaire": None, "erreur": None,
    }
    base.update(over)
    return base


def test_patch_consent_200_ressource_complete(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(
        h.adls_client, "read_table_filtered",
        side_effect=[
            pd.DataFrame([_pdl_full_row()]),                                  # existant (traite)
            pd.DataFrame([_pdl_full_row(statut="nouveau")]),                  # après
        ],
    )

    body, status = h.handle_patch_consent(FakeReq({"date_fin_autorisation": "2040-06-05"}), "00000000001965")

    assert status == 200
    assert body["statut"] == "nouveau"                       # ressource complète (9 champs)
    assert set(body.keys()) == {
        "id_pdl", "partner", "platform_code", "statut", "statut_cdc", "statut_dm",
        "message_erreur", "date_creation", "date_modification",
    }
    # UPDATE : champ modifié (DATE literal) + statut nouveau + erreur null
    _, kwargs = update.call_args
    updates = update.call_args.args[2]
    assert updates["date_fin_autorisation"] == "DATE '2040-06-05'"
    assert updates["statut"] == "'nouveau'"
    assert updates["erreur"] == "NULL"


def test_patch_consent_400_champ_non_modifiable(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame([_pdl_full_row()]))

    # partner non modifiable
    body, status = h.handle_patch_consent(FakeReq({"partner": "autre"}), "00000000001965")
    assert status == 400
    assert body["erreur"] == "CHAMP_NON_MODIFIABLE"
    assert body["champ"] == "partner"
    update.assert_not_called()


def test_patch_consent_400_platform_code_non_modifiable(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame([_pdl_full_row()]))

    body, status = h.handle_patch_consent(FakeReq({"platform_code": "PF99"}), "00000000001965")
    assert status == 400
    assert body["erreur"] == "CHAMP_NON_MODIFIABLE"
    assert body["champ"] == "platform_code"
    update.assert_not_called()


def test_patch_consent_400_champ_systeme(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame([_pdl_full_row()]))

    body, status = h.handle_patch_consent(FakeReq({"statut": "traite"}), "00000000001965")
    assert status == 400
    assert body["champ"] == "statut"


def test_patch_consent_400_croise_dates(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "update_rows")
    # existant date_debut=2016-01-01 ; on patche date_fin avant → 400
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame([_pdl_full_row()]))

    body, status = h.handle_patch_consent(FakeReq({"date_fin_autorisation": "2010-01-01"}), "00000000001965")
    assert status == 400
    assert body["champ"] == "date_fin_autorisation"


def test_patch_consent_400_body_vide(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame([_pdl_full_row()]))

    body, status = h.handle_patch_consent(FakeReq({}), "00000000001965")
    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"


def test_patch_consent_404(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_rows")
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame())

    body, status = h.handle_patch_consent(FakeReq({"nom": "Martin"}), "INCONNU")
    assert status == 404
    assert body["erreur"] == "PDL_INTROUVABLE"
    update.assert_not_called()


def test_patch_consent_403_ip(mocker, monkeypatch):
    import api.enedis_consent as h
    read = mocker.patch.object(h.adls_client, "read_table_filtered")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq({"nom": "Martin"}, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_patch_consent(req, "00000000001965")
    assert status == 403
    read.assert_not_called()


# ----------------------------------------------------------------------
# handle_revoke_consent (DELETE /enedis/consent/{id_pdl})
# ----------------------------------------------------------------------

def test_revoke_consent_200(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_pdl_statut")
    mocker.patch.object(
        h.adls_client, "read_table_filtered",
        side_effect=[
            pd.DataFrame([_pdl_row(statut="traite")]),                                  # avant
            pd.DataFrame([_pdl_row(statut="revoque", date_modification=pd.Timestamp("2026-06-16 09:00:00"))]),  # après
        ],
    )

    body, status = h.handle_revoke_consent(FakeReq(None), "00000000001965")

    assert status == 200
    assert body["statut"] == "revoque"
    assert body["date_modification"] == "2026-06-16 09:00:00"
    update.assert_called_once_with("00000000001965", "revoque")


def test_revoke_consent_404_introuvable(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_pdl_statut")
    mocker.patch.object(h.adls_client, "read_table_filtered", return_value=pd.DataFrame())

    body, status = h.handle_revoke_consent(FakeReq(None), "INCONNU")

    assert status == 404
    assert body["erreur"] == "PDL_INTROUVABLE"
    update.assert_not_called()


def test_revoke_consent_404_deja_revoque(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_pdl_statut")
    mocker.patch.object(h.adls_client, "read_table_filtered",
                        return_value=pd.DataFrame([_pdl_row(statut="revoque")]))

    body, status = h.handle_revoke_consent(FakeReq(None), "00000000001965")

    assert status == 404
    assert body["erreur"] == "PDL_DEJA_REVOQUE"
    update.assert_not_called()


def test_revoke_consent_409_non_traite(mocker):
    import api.enedis_consent as h
    update = mocker.patch.object(h.adls_client, "update_pdl_statut")
    mocker.patch.object(h.adls_client, "read_table_filtered",
                        return_value=pd.DataFrame([_pdl_row(statut="nouveau")]))

    body, status = h.handle_revoke_consent(FakeReq(None), "00000000001965")

    assert status == 409
    assert body["erreur"] == "STATUT_INCOMPATIBLE"
    assert body["statut"] == "nouveau"
    update.assert_not_called()


def test_revoke_consent_403_ip(mocker, monkeypatch):
    import api.enedis_consent as h
    read = mocker.patch.object(h.adls_client, "read_table_filtered")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_revoke_consent(req, "00000000001965")

    assert status == 403
    read.assert_not_called()


# ----------------------------------------------------------------------
# handle_list_consents (GET /enedis/consents)
# ----------------------------------------------------------------------

def _pdl_table(statuts):
    return pd.DataFrame([
        _pdl_row(id_pdl=f"PDL{i:011d}", statut=s) for i, s in enumerate(statuts)
    ])


class FakeReqP:
    def __init__(self, params=None, headers=None):
        self.params = params or {}
        self.headers = headers or {}


def test_list_consents_200_sans_filtre(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table", return_value=_pdl_table(["traite", "nouveau", "erreur"]))

    body, status = h.handle_list_consents(FakeReqP())

    assert status == 200
    assert body["total"] == 3
    assert body["limit"] == 50
    assert len(body["resultats"]) == 3
    # item = même forme que le GET détail (9 champs)
    assert body["resultats"][0]["platform_code"] == "PF01"
    assert set(body["resultats"][0].keys()) == {
        "id_pdl", "partner", "platform_code", "statut", "statut_cdc", "statut_dm",
        "message_erreur", "date_creation", "date_modification",
    }


def test_list_consents_filtre_statut(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table",
                        return_value=_pdl_table(["traite", "erreur", "traite", "nouveau"]))

    body, _ = h.handle_list_consents(FakeReqP(params={"statut": "traite"}))
    assert body["total"] == 2
    assert all(it["statut"] == "traite" for it in body["resultats"])


def test_list_consents_pagination(mocker):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table", return_value=_pdl_table(["traite"] * 10))

    body, _ = h.handle_list_consents(FakeReqP(params={"limit": "3", "offset": "6"}))
    assert body["total"] == 10
    assert body["limit"] == 3
    assert body["offset"] == 6
    ids = [it["id_pdl"] for it in body["resultats"]]
    assert ids == ["PDL00000000006", "PDL00000000007", "PDL00000000008"]


def test_list_consents_400_statut_invalide(mocker):
    import api.enedis_consent as h
    read = mocker.patch.object(h.adls_client, "read_table")

    body, status = h.handle_list_consents(FakeReqP(params={"statut": "erreur_resiliation"}))

    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"
    assert body["champ"] == "statut"
    read.assert_not_called()


@pytest.mark.parametrize("limit", ["0", "500", "abc"])
def test_list_consents_400_limit(mocker, limit):
    import api.enedis_consent as h
    mocker.patch.object(h.adls_client, "read_table")
    body, status = h.handle_list_consents(FakeReqP(params={"limit": limit}))
    assert status == 400
    assert body["champ"] == "limit"


def test_list_consents_403_ip(mocker, monkeypatch):
    import api.enedis_consent as h
    read = mocker.patch.object(h.adls_client, "read_table")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    body, status = h.handle_list_consents(FakeReqP(headers={"X-Forwarded-For": "8.8.8.8:1"}))
    assert status == 403
    read.assert_not_called()
