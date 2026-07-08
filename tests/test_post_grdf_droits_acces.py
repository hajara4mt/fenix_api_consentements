"""
Tests de POST /grdf/droits-acces.

Deux niveaux :
  - validate_create : règles de validation pures (sans Azure ni parquet)
  - handle_create   : flux complet, registry_dao mocké (aucun accès ADLS réel)

Lancer : pytest tests/ -v
"""

import pytest

from api.validation import validate_create, ValidationError


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

def _valid_body(**overrides) -> dict:
    body = {
        "id_pce": "GI12345678901234",
        "partner": "ifpeb",
        "platform_code": "PF01",
        "courriel_titulaire": "contact@exemple.fr",
        "code_postal": "75001",
        "date_debut_droit_acces": "2026-05-01",
        "date_fin_droit_acces": "2029-05-01",
        "perim_donnees_conso_debut": "2023-01-01",
        "perim_donnees_conso_fin": "2029-05-01",
        "raison_sociale_du_titulaire": "Mon Entreprise SAS",
        "nom_titulaire": "Dupont",
        "perim_donnees_contractuelles": True,
        "perim_donnees_techniques": True,
        "perim_donnees_informatives": False,
        "perim_donnees_publiees": True,
    }
    body.update(overrides)
    return body


class FakeReq:
    """Imite func.HttpRequest pour les tests du handler (headers + get_json + params)."""

    def __init__(self, json_body, headers=None, raise_on_json=False, params=None):
        self._json = json_body
        self.headers = headers or {}
        self._raise = raise_on_json
        self.params = params or {}

    def get_json(self):
        if self._raise:
            raise ValueError("invalid json")
        return self._json


# ----------------------------------------------------------------------
# validate_create — cas nominal
# ----------------------------------------------------------------------

def test_validate_ok_retourne_id_et_fields():
    id_pce, fields = validate_create(_valid_body())
    assert id_pce == "GI12345678901234"
    # noms canoniques storage
    assert fields["date_debut_droit_acces"] == "2026-05-01"
    assert fields["date_fin_droit_acces"] == "2029-05-01"
    assert fields["raison_sociale_du_titulaire"] == "Mon Entreprise SAS"
    assert fields["courriel_titulaire"] == "contact@exemple.fr"
    # role_tiers / etat NE sont PAS posés par la validation (handler s'en charge)
    assert "role_tiers" not in fields
    assert "etat_droit_acces" not in fields
    # platform_code repris tel quel dans les champs canoniques
    assert fields["platform_code"] == "PF01"


def test_validate_platform_code_trop_long():
    with pytest.raises(ValidationError) as exc:
        validate_create(_valid_body(platform_code="ABCDEFGHIJK"))  # 11 car. > 10
    assert exc.value.champ == "platform_code"


def test_validate_defauts_perimetres():
    body = _valid_body()
    for champ in (
        "perim_donnees_contractuelles",
        "perim_donnees_techniques",
        "perim_donnees_informatives",
        "perim_donnees_publiees",
    ):
        body.pop(champ, None)
    _, fields = validate_create(body)
    assert fields["perim_donnees_contractuelles"] is True
    assert fields["perim_donnees_techniques"] is True
    assert fields["perim_donnees_informatives"] is False
    assert fields["perim_donnees_publiees"] is True


def test_validate_un_seul_titulaire_suffit():
    # raison sociale seule
    body = _valid_body(nom_titulaire=None)
    _, fields = validate_create(body)
    assert fields["nom_titulaire"] is None
    assert fields["raison_sociale_du_titulaire"] == "Mon Entreprise SAS"
    # nom seul
    body = _valid_body(raison_sociale_du_titulaire=None)
    _, fields = validate_create(body)
    assert fields["raison_sociale_du_titulaire"] is None


# ----------------------------------------------------------------------
# validate_create — cas d'erreur
# ----------------------------------------------------------------------

@pytest.mark.parametrize("champ", [
    "id_pce", "partner", "platform_code", "courriel_titulaire", "code_postal",
    "date_debut_droit_acces", "date_fin_droit_acces",
    "perim_donnees_conso_debut", "perim_donnees_conso_fin",
])
def test_validate_champ_obligatoire_manquant(champ):
    body = _valid_body()
    body.pop(champ)
    with pytest.raises(ValidationError) as exc:
        validate_create(body)
    assert exc.value.champ == champ


def test_validate_id_pce_trop_long():
    with pytest.raises(ValidationError) as exc:
        validate_create(_valid_body(id_pce="X" * 21))
    assert exc.value.champ == "id_pce"


def test_validate_code_postal_invalide():
    with pytest.raises(ValidationError) as exc:
        validate_create(_valid_body(code_postal="7500"))
    assert exc.value.champ == "code_postal"


def test_validate_email_invalide():
    with pytest.raises(ValidationError) as exc:
        validate_create(_valid_body(courriel_titulaire="pas-un-email"))
    assert exc.value.champ == "courriel_titulaire"


def test_validate_date_fin_avant_debut():
    with pytest.raises(ValidationError) as exc:
        validate_create(_valid_body(date_fin_droit_acces="2026-04-01"))
    assert exc.value.champ == "date_fin_droit_acces"


def test_validate_date_fin_depasse_3_ans():
    with pytest.raises(ValidationError) as exc:
        validate_create(_valid_body(date_fin_droit_acces="2030-05-02"))
    assert exc.value.champ == "date_fin_droit_acces"


def test_validate_aucun_titulaire():
    with pytest.raises(ValidationError) as exc:
        validate_create(_valid_body(raison_sociale_du_titulaire=None, nom_titulaire=None))
    assert exc.value.champ == "raison_sociale_du_titulaire"


def test_validate_aucun_perimetre_true():
    body = _valid_body(
        perim_donnees_contractuelles=False,
        perim_donnees_techniques=False,
        perim_donnees_informatives=False,
        perim_donnees_publiees=False,
    )
    with pytest.raises(ValidationError):
        validate_create(body)


# ----------------------------------------------------------------------
# handle_create — flux complet (registry_dao mocké)
# ----------------------------------------------------------------------

def test_handle_create_201(mocker):
    import api.grdf_droits_acces as handler

    insert = mocker.patch.object(handler.registry_dao, "insert")
    body, status = handler.handle_create(FakeReq(_valid_body()))

    assert status == 201
    assert body["statut"] == "nouveau"
    assert body["id_pce"] == "GI12345678901234"
    # role_tiers auto + etat posés avant l'insert
    insert.assert_called_once()
    _, fields = insert.call_args.args
    assert fields["role_tiers"] == "AUTORISE_CONTRAT_FOURNITURE"
    assert fields["etat_droit_acces"] == "nouveau"


def test_handle_create_partner_minuscule(mocker):
    import api.grdf_droits_acces as handler
    insert = mocker.patch.object(handler.registry_dao, "insert")

    body, status = handler.handle_create(FakeReq(_valid_body(partner="IFPEB")))

    assert status == 201
    _, fields = insert.call_args.args
    assert fields["partner"] == "ifpeb"   # normalisé quel que soit la casse


def test_handle_create_400_partner_non_autorise(mocker):
    import api.grdf_droits_acces as handler
    insert = mocker.patch.object(handler.registry_dao, "insert")

    body, status = handler.handle_create(FakeReq(_valid_body(partner="Mon Entreprise")))

    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"
    assert body["champ"] == "partner"
    insert.assert_not_called()


def test_handle_create_400_champ_inconnu(mocker):
    import api.grdf_droits_acces as handler
    insert = mocker.patch.object(handler.registry_dao, "insert")

    # champ hors schéma → rejet (pas d'ajout de colonne silencieux)
    body, status = handler.handle_create(FakeReq(_valid_body(colonne_bidon="x")))

    assert status == 400
    assert body["erreur"] == "CHAMP_INCONNU"
    assert body["champ"] == "colonne_bidon"
    insert.assert_not_called()


def test_handle_create_ok_sans_perimetres_optionnels(mocker):
    import api.grdf_droits_acces as handler
    mocker.patch.object(handler.registry_dao, "insert")
    # disparition des champs optionnels (périmètres) = accepté (défauts appliqués)
    body = _valid_body()
    for champ in ("perim_donnees_contractuelles", "perim_donnees_techniques",
                  "perim_donnees_informatives", "perim_donnees_publiees"):
        body.pop(champ, None)
    _, status = handler.handle_create(FakeReq(body))
    assert status == 201


def test_handle_create_400_validation(mocker):
    import api.grdf_droits_acces as handler
    insert = mocker.patch.object(handler.registry_dao, "insert")

    body, status = handler.handle_create(FakeReq(_valid_body(code_postal="abc")))

    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"
    assert body["champ"] == "code_postal"
    insert.assert_not_called()


def test_handle_create_400_json_invalide(mocker):
    import api.grdf_droits_acces as handler
    mocker.patch.object(handler.registry_dao, "insert")

    body, status = handler.handle_create(FakeReq(None, raise_on_json=True))

    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"


def test_handle_create_409_pce_existant(mocker):
    import api.grdf_droits_acces as handler

    mocker.patch.object(handler.registry_dao, "insert", side_effect=ValueError("existe déjà"))
    mocker.patch.object(handler.registry_dao, "get", return_value={"etat_droit_acces": "Active"})

    body, status = handler.handle_create(FakeReq(_valid_body()))

    assert status == 409
    assert body["erreur"] == "PCE_EXISTANT"
    assert body["statut"] == "Active"  # exposé tel quel


def test_handle_create_409_statut_brut(mocker):
    import api.grdf_droits_acces as handler

    mocker.patch.object(handler.registry_dao, "insert", side_effect=ValueError("existe déjà"))
    # statut exposé = état interne BRUT (plus de mapping) : Révoquée reste Révoquée
    mocker.patch.object(handler.registry_dao, "get", return_value={"etat_droit_acces": "Révoquée"})

    body, status = handler.handle_create(FakeReq(_valid_body()))

    assert status == 409
    assert body["statut"] == "Révoquée"


# ----------------------------------------------------------------------
# ip_filter — whitelist, anti-spoof XFF, CIDR
# ----------------------------------------------------------------------

def _req_xff(xff: str) -> FakeReq:
    return FakeReq(None, headers={"X-Forwarded-For": xff})


def test_ip_autorisee_derniere_entree(monkeypatch):
    from api.ip_filter import is_ip_allowed
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    # Azure ajoute la vraie IP en fin : ici 10.0.0.1 → autorisé
    assert is_ip_allowed(_req_xff("1.2.3.4, 10.0.0.1:55012")) is True


def test_ip_spoof_premiere_entree_refusee(monkeypatch):
    from api.ip_filter import is_ip_allowed
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    # Tentative de spoof : IP whitelistée mise en 1ʳᵉ position, vraie IP (8.8.8.8) en fin
    assert is_ip_allowed(_req_xff("10.0.0.1, 8.8.8.8:1234")) is False


def test_ip_cidr(monkeypatch):
    from api.ip_filter import is_ip_allowed
    monkeypatch.setenv("ALLOWED_IPS", "90.80.0.0/24")
    assert is_ip_allowed(_req_xff("90.80.0.5:10")) is True
    assert is_ip_allowed(_req_xff("90.80.1.5:10")) is False


def test_ip_whitelist_vide_autorise_par_defaut(monkeypatch):
    from api.ip_filter import is_ip_allowed
    monkeypatch.delenv("ALLOWED_IPS", raising=False)
    monkeypatch.delenv("ALLOW_ALL_WHEN_UNSET", raising=False)
    assert is_ip_allowed(_req_xff("8.8.8.8:1")) is True


def test_ip_whitelist_vide_refuse_si_configure(monkeypatch):
    from api.ip_filter import is_ip_allowed
    monkeypatch.delenv("ALLOWED_IPS", raising=False)
    monkeypatch.setenv("ALLOW_ALL_WHEN_UNSET", "false")
    assert is_ip_allowed(_req_xff("8.8.8.8:1")) is False


def test_handle_create_403_ip(mocker, monkeypatch):
    import api.grdf_droits_acces as handler
    insert = mocker.patch.object(handler.registry_dao, "insert")

    # whitelist non vide + IP cliente absente → refus
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(_valid_body(), headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = handler.handle_create(req)

    assert status == 403
    assert body["erreur"] == "IP_NON_AUTORISEE"
    insert.assert_not_called()


# ----------------------------------------------------------------------
# handle_get — flux complet (registry_dao mocké)
# ----------------------------------------------------------------------

def _stored_record(**overrides) -> dict:
    rec = {
        "id_pce": "GI12345678901234",
        "partner": "ifpeb",
        "platform_code": "PF01",
        "etat_droit_acces": "Active",
        "perim_donnees_contractuelles": True,
        "perim_donnees_techniques": True,
        "perim_donnees_informatives": False,
        "perim_donnees_publiees": True,
        "date_debut_droit_acces": "2026-05-01",
        "date_fin_droit_acces": "2029-05-01",
        "date_creation": "2026-04-20T10:00:00Z",
        "derniere_maj": "2026-04-21T04:01:30Z",
    }
    rec.update(overrides)
    return rec


def test_handle_get_200(mocker):
    import api.grdf_droits_acces as handler
    mocker.patch.object(handler.registry_dao, "get", return_value=_stored_record())

    body, status = handler.handle_get(FakeReq(None), "GI12345678901234")

    assert status == 200
    assert body["statut"] == "Active"             # etat exposé tel quel
    assert body["derniere_maj"] == "2026-04-21 04:01:30"   # format simple
    assert body["perim_donnees_informatives"] is False
    assert body["message_erreur"] is None
    # forme constante : la clé existe même sans erreur
    assert "message_erreur" in body


def test_handle_get_format_date_simple(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(
        h.registry_dao, "get",
        return_value=_stored_record(
            date_creation="2026-06-16T09:03:16.415174+00:00",
            derniere_maj="2026-06-16T09:03:16.415174+00:00",
        ),
    )
    body, status = h.handle_get(FakeReq(None), "GI12345678901234")
    assert status == 200
    assert body["date_creation"] == "2026-06-16 09:03:16"
    assert body["derniere_maj"] == "2026-06-16 09:03:16"


def test_handle_get_expose_platform_code(mocker):
    """platform_code est exposé dans le GET détail."""
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record(platform_code="PF42"))
    body, status = h.handle_get(FakeReq(None), "GI12345678901234")
    assert status == 200
    assert body["platform_code"] == "PF42"


def test_handle_list_expose_platform_code(mocker):
    """platform_code est exposé dans chaque item de la liste (forme allégée)."""
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "list_all", return_value=[_stored_record(platform_code="PF42")])
    body, status = h.handle_list(FakeReq(None))
    assert status == 200
    assert body["resultats"][0]["platform_code"] == "PF42"


def test_handle_get_statut_brut(mocker):
    import api.grdf_droits_acces as handler
    # statut exposé BRUT (plus de repli) : A revérifier reste A revérifier
    mocker.patch.object(
        handler.registry_dao, "get",
        return_value=_stored_record(etat_droit_acces="A revérifier"),
    )
    body, status = handler.handle_get(FakeReq(None), "GI12345678901234")
    assert status == 200
    assert body["statut"] == "A revérifier"


def test_handle_get_message_erreur_traduit(mocker):
    import api.grdf_droits_acces as handler
    mocker.patch.object(
        handler.registry_dao, "get",
        return_value=_stored_record(
            etat_droit_acces="A revérifier",
            message_erreur_declare='{"code":"1000003","detail":"..."}',
        ),
    )
    body, status = handler.handle_get(FakeReq(None), "GI12345678901234")
    assert status == 200
    assert body["message_erreur"] == "Compteur non accrédité pour la collecte de données"


def test_handle_get_404(mocker):
    import api.grdf_droits_acces as handler
    mocker.patch.object(handler.registry_dao, "get", return_value=None)

    body, status = handler.handle_get(FakeReq(None), "INCONNU")

    assert status == 404
    assert body["erreur"] == "PCE_INTROUVABLE"
    assert body["id_pce"] == "INCONNU"


def test_handle_get_403_ip(mocker, monkeypatch):
    import api.grdf_droits_acces as handler
    get = mocker.patch.object(handler.registry_dao, "get")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = handler.handle_get(req, "GI12345678901234")

    assert status == 403
    assert body["erreur"] == "IP_NON_AUTORISEE"
    get.assert_not_called()


# ----------------------------------------------------------------------
# handle_revoke (DELETE) — révocation logique
# ----------------------------------------------------------------------

def test_handle_revoke_200(mocker):
    import api.grdf_droits_acces as handler
    upsert = mocker.patch.object(handler.registry_dao, "upsert")
    # 1er get = précondition (Active), 2e get = relecture après upsert (Révoquée)
    mocker.patch.object(
        handler.registry_dao, "get",
        side_effect=[
            _stored_record(etat_droit_acces="Active"),
            _stored_record(etat_droit_acces="Révoquée", derniere_maj="2026-06-15T09:00:00Z"),
        ],
    )

    body, status = handler.handle_revoke(FakeReq(None), "GI12345678901234")

    assert status == 200
    assert body["statut"] == "Révoquée"            # statut BRUT (plus de repli vers Obsolète)
    assert body["derniere_maj"] == "2026-06-15 09:00:00"   # format simple
    # upsert pose bien l'état Révoquée (pas de suppression physique)
    args, kwargs = upsert.call_args
    assert args[0] == "GI12345678901234"
    assert args[1]["etat_droit_acces"] == "Révoquée"
    # pas de colonne de traçabilité superflue (derniere_maj suffit)
    assert "date_demande_revocation" not in args[1]


def test_handle_revoke_depuis_nouveau(mocker):
    import api.grdf_droits_acces as handler
    mocker.patch.object(handler.registry_dao, "upsert")
    mocker.patch.object(
        handler.registry_dao, "get",
        side_effect=[
            _stored_record(etat_droit_acces="nouveau"),
            _stored_record(etat_droit_acces="Révoquée"),
        ],
    )
    body, status = handler.handle_revoke(FakeReq(None), "GI12345678901234")
    assert status == 200  # 'nouveau' est révocable


def test_handle_revoke_404_introuvable(mocker):
    import api.grdf_droits_acces as handler
    upsert = mocker.patch.object(handler.registry_dao, "upsert")
    mocker.patch.object(handler.registry_dao, "get", return_value=None)

    body, status = handler.handle_revoke(FakeReq(None), "INCONNU")

    assert status == 404
    assert body["erreur"] == "PCE_INTROUVABLE"
    upsert.assert_not_called()


def test_handle_revoke_404_deja_revoque(mocker):
    import api.grdf_droits_acces as handler
    upsert = mocker.patch.object(handler.registry_dao, "upsert")
    mocker.patch.object(
        handler.registry_dao, "get",
        return_value=_stored_record(etat_droit_acces="Révoquée"),
    )

    body, status = handler.handle_revoke(FakeReq(None), "GI12345678901234")

    assert status == 404
    assert body["erreur"] == "PCE_DEJA_REVOQUE"
    upsert.assert_not_called()


def test_handle_revoke_409_statut_incompatible(mocker):
    import api.grdf_droits_acces as handler
    upsert = mocker.patch.object(handler.registry_dao, "upsert")
    # 'Refusée' n'est pas dans REVOCABLE_STATES
    mocker.patch.object(
        handler.registry_dao, "get",
        return_value=_stored_record(etat_droit_acces="Refusée"),
    )

    body, status = handler.handle_revoke(FakeReq(None), "GI12345678901234")

    assert status == 409
    assert body["erreur"] == "STATUT_INCOMPATIBLE"
    assert body["statut"] == "Refusée"
    upsert.assert_not_called()


def test_handle_revoke_403_ip(mocker, monkeypatch):
    import api.grdf_droits_acces as handler
    get = mocker.patch.object(handler.registry_dao, "get")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = handler.handle_revoke(req, "GI12345678901234")

    assert status == 403
    assert body["erreur"] == "IP_NON_AUTORISEE"
    get.assert_not_called()


# ----------------------------------------------------------------------
# handle_patch (PATCH) — mise à jour partielle
# ----------------------------------------------------------------------

def test_handle_patch_200_modifie_date_fin(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(
        h.registry_dao, "get",
        side_effect=[
            _stored_record(etat_droit_acces="Active"),     # existant (date_debut 2026-05-01)
            _stored_record(etat_droit_acces="nouveau"),     # relecture
        ],
    )

    body, status = h.handle_patch(FakeReq({"date_fin_droit_acces": "2028-05-01"}),
                                  "GI12345678901234")

    assert status == 200
    assert body["statut"] == "nouveau"
    fields = upsert.call_args.args[1]
    assert fields["date_fin_droit_acces"] == "2028-05-01"
    # remise en file + reset compteur
    assert fields["etat_droit_acces"] == "nouveau"
    assert fields["nb_tentatives_declare"] == 0
    assert fields["message_erreur_declare"] is None


def test_handle_patch_200_modifie_perimetre(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get",
                        side_effect=[_stored_record(), _stored_record(etat_droit_acces="nouveau")])

    _, status = h.handle_patch(FakeReq({"perim_donnees_informatives": True}), "GI12345678901234")
    assert status == 200
    assert upsert.call_args.args[1]["perim_donnees_informatives"] is True


def test_handle_patch_renvoie_ressource_complete(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(
        h.registry_dao, "get",
        side_effect=[
            _stored_record(etat_droit_acces="Active"),                       # avant
            _stored_record(etat_droit_acces="nouveau", date_fin_droit_acces="2028-05-01"),  # après
        ],
    )

    body, status = h.handle_patch(FakeReq({"date_fin_droit_acces": "2028-05-01"}), "GI12345678901234")

    assert status == 200
    # ressource COMPLÈTE : modifiable à jour + non-modifiables intacts
    assert body["id_pce"] == "GI12345678901234"
    assert body["partner"] == "ifpeb"
    assert body["statut"] == "nouveau"
    assert body["date_fin_droit_acces"] == "2028-05-01"
    assert "perim_donnees_contractuelles" in body
    assert "date_creation" in body and "derniere_maj" in body


def test_handle_patch_400_champ_non_modifiable_id_pce(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record())

    body, status = h.handle_patch(FakeReq({"id_pce": "AUTRE"}), "GI12345678901234")

    assert status == 400
    assert body["erreur"] == "CHAMP_NON_MODIFIABLE"
    assert body["champ"] == "id_pce"
    upsert.assert_not_called()


def test_handle_patch_400_champ_systeme(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record())

    # tentative d'écraser un champ système (court-circuiter la déclaration)
    body, status = h.handle_patch(FakeReq({"etat_droit_acces": "Active"}), "GI12345678901234")

    assert status == 400
    assert body["erreur"] == "CHAMP_NON_MODIFIABLE"
    assert body["champ"] == "etat_droit_acces"
    upsert.assert_not_called()


def test_handle_patch_400_partner_non_modifiable(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record())

    body, status = h.handle_patch(FakeReq({"partner": "autre"}), "GI12345678901234")
    assert status == 400
    assert body["champ"] == "partner"


def test_handle_patch_400_platform_code_non_modifiable(mocker):
    """platform_code n'est pas dans la whitelist PATCH → CHAMP_NON_MODIFIABLE."""
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record())

    body, status = h.handle_patch(FakeReq({"platform_code": "PF99"}), "GI12345678901234")
    assert status == 400
    assert body["erreur"] == "CHAMP_NON_MODIFIABLE"
    assert body["champ"] == "platform_code"
    upsert.assert_not_called()


def test_handle_patch_400_email_invalide(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record())

    body, status = h.handle_patch(FakeReq({"courriel_titulaire": "pas-un-email"}), "GI12345678901234")
    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"
    assert body["champ"] == "courriel_titulaire"
    upsert.assert_not_called()


def test_handle_patch_400_croise_3ans(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "upsert")
    # existant date_debut=2026-05-01 ; on patche date_fin à +4 ans → 400
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record())

    body, status = h.handle_patch(FakeReq({"date_fin_droit_acces": "2030-06-01"}), "GI12345678901234")
    assert status == 400
    assert body["champ"] == "date_fin_droit_acces"


def test_handle_patch_400_body_vide(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=_stored_record())

    body, status = h.handle_patch(FakeReq({}), "GI12345678901234")
    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"


def test_handle_patch_404(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=None)

    body, status = h.handle_patch(FakeReq({"code_postal": "75002"}), "INCONNU")
    assert status == 404
    assert body["erreur"] == "PCE_INTROUVABLE"
    upsert.assert_not_called()


def test_handle_patch_403_ip(mocker, monkeypatch):
    import api.grdf_droits_acces as h
    get = mocker.patch.object(h.registry_dao, "get")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq({"code_postal": "75002"}, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_patch(req, "GI12345678901234")
    assert status == 403
    get.assert_not_called()


# ----------------------------------------------------------------------
# handle_retry (POST .../retry) — relance
# ----------------------------------------------------------------------

def test_handle_retry_200_depuis_a_reverifier(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(
        h.registry_dao, "get",
        side_effect=[
            _stored_record(etat_droit_acces="A revérifier"),
            _stored_record(etat_droit_acces="nouveau"),
        ],
    )

    body, status = h.handle_retry(FakeReq(None), "GI12345678901234")

    assert status == 200
    assert body["statut"] == "nouveau"
    # reset complet : etat + compteur + message
    fields = upsert.call_args.args[1]
    assert fields["etat_droit_acces"] == "nouveau"
    assert fields["nb_tentatives_declare"] == 0
    assert fields["message_erreur_declare"] is None


def test_handle_retry_200_depuis_refusee(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(
        h.registry_dao, "get",
        side_effect=[
            _stored_record(etat_droit_acces="Refusée"),
            _stored_record(etat_droit_acces="nouveau"),
        ],
    )
    _, status = h.handle_retry(FakeReq(None), "GI12345678901234")
    assert status == 200  # Refusée est relançable


def test_handle_retry_404(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    mocker.patch.object(h.registry_dao, "get", return_value=None)

    body, status = h.handle_retry(FakeReq(None), "INCONNU")

    assert status == 404
    assert body["erreur"] == "PCE_INTROUVABLE"
    upsert.assert_not_called()


def test_handle_retry_409_non_relancable(mocker):
    import api.grdf_droits_acces as h
    upsert = mocker.patch.object(h.registry_dao, "upsert")
    # 'Active' n'est pas relançable
    mocker.patch.object(h.registry_dao, "get",
                        return_value=_stored_record(etat_droit_acces="Active"))

    body, status = h.handle_retry(FakeReq(None), "GI12345678901234")

    assert status == 409
    assert body["erreur"] == "STATUT_INCOMPATIBLE"
    assert body["statut"] == "Active"
    upsert.assert_not_called()


def test_handle_retry_403_ip(mocker, monkeypatch):
    import api.grdf_droits_acces as h
    get = mocker.patch.object(h.registry_dao, "get")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_retry(req, "GI12345678901234")

    assert status == 403
    get.assert_not_called()


# ----------------------------------------------------------------------
# handle_list (GET liste) — filtres + pagination
# ----------------------------------------------------------------------

def _records(states):
    return [
        _stored_record(id_pce=f"PCE{i:02d}", etat_droit_acces=s)
        for i, s in enumerate(states)
    ]


def test_handle_list_200_sans_filtre(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "list_all",
                        return_value=_records(["Active", "nouveau", "Refusée"]))

    body, status = h.handle_list(FakeReq(None))

    assert status == 200
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["resultats"]) == 3
    # item ALLÉGÉ : pas de date_debut_droit_acces ni date_creation
    item = body["resultats"][0]
    assert "date_debut_droit_acces" not in item
    assert "date_creation" not in item
    assert "date_fin_droit_acces" in item and "derniere_maj" in item


def test_handle_list_filtre_statut_brut(mocker):
    import api.grdf_droits_acces as h
    # filtre = égalité simple sur le statut BRUT : "Obsolète" ne matche QUE Obsolète
    # (plus de repli : Révoquée et résilié ne sont PAS inclus)
    mocker.patch.object(h.registry_dao, "list_all",
                        return_value=_records(["Active", "Révoquée", "résilié", "Obsolète", "nouveau"]))

    body, status = h.handle_list(FakeReq(None, params={"statut": "Obsolète"}))

    assert status == 200
    assert body["total"] == 1
    assert all(it["statut"] == "Obsolète" for it in body["resultats"])


def test_handle_list_filtre_revoquee_brut(mocker):
    import api.grdf_droits_acces as h
    # on peut désormais filtrer sur Révoquée directement (statut interne brut)
    mocker.patch.object(h.registry_dao, "list_all",
                        return_value=_records(["A valider", "A revérifier", "Révoquée", "Active"]))

    body, _ = h.handle_list(FakeReq(None, params={"statut": "Révoquée"}))
    assert body["total"] == 1
    assert body["resultats"][0]["statut"] == "Révoquée"


def test_handle_list_pagination(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "list_all", return_value=_records(["Active"] * 10))

    body, _ = h.handle_list(FakeReq(None, params={"limit": "3", "offset": "6"}))

    assert body["total"] == 10
    assert body["limit"] == 3
    assert body["offset"] == 6
    ids = [it["id_pce"] for it in body["resultats"]]
    assert ids == ["PCE06", "PCE07", "PCE08"]


def test_handle_list_400_statut_invalide(mocker):
    import api.grdf_droits_acces as h
    list_all = mocker.patch.object(h.registry_dao, "list_all")

    body, status = h.handle_list(FakeReq(None, params={"statut": "Cloturé"}))

    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"   # aligné avec le reste de l'API
    assert body["champ"] == "statut"
    list_all.assert_not_called()


@pytest.mark.parametrize("limit", ["0", "500", "abc", "-3"])
def test_handle_list_400_limit(mocker, limit):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "list_all")
    body, status = h.handle_list(FakeReq(None, params={"limit": limit}))
    assert status == 400
    assert body["champ"] == "limit"


def test_handle_list_400_offset(mocker):
    import api.grdf_droits_acces as h
    mocker.patch.object(h.registry_dao, "list_all")
    body, status = h.handle_list(FakeReq(None, params={"offset": "-1"}))
    assert status == 400
    assert body["champ"] == "offset"


def test_handle_list_403_ip(mocker, monkeypatch):
    import api.grdf_droits_acces as h
    list_all = mocker.patch.object(h.registry_dao, "list_all")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"})

    body, status = h.handle_list(req)

    assert status == 403
    list_all.assert_not_called()


# ----------------------------------------------------------------------
# handle_consommations (GET /consommations) — intervalles bruts
# ----------------------------------------------------------------------

def _conso_rows():
    # date_debut → date_fin (cf. exemples : fenêtre demandée 2024-01-01 → 2024-06-01)
    return [
        {"date_debut": "2023-12-01", "date_fin": "2024-01-01", "frequence": "1M", "consommation": 900.0, "unite": "kWh"},   # R1 avant from
        {"date_debut": "2024-01-01", "date_fin": "2024-02-01", "frequence": "1M", "consommation": 1234.5, "unite": "kWh"},  # R2 dedans
        {"date_debut": "2024-03-01", "date_fin": "2024-04-01", "frequence": "1M", "consommation": 1100.0, "unite": "kWh"},  # R3 dedans
        {"date_debut": "2024-05-01", "date_fin": "2024-06-01", "frequence": "1M", "consommation": 800.0, "unite": "kWh"},   # R4 dedans (bord)
        {"date_debut": "2024-05-01", "date_fin": "2024-07-01", "frequence": "2M", "consommation": 1500.0, "unite": "kWh"},  # R5 déborde to
        {"date_debut": "2023-11-01", "date_fin": "2024-02-01", "frequence": "3M", "consommation": 2000.0, "unite": "kWh"},  # R6 déborde from
    ]


def _params(**kw):
    base = {"provider": "grdf", "sensor_id": "GI12345678901234",
            "from": "2024-01-01", "to": "2024-06-01"}
    base.update(kw)
    return base


def test_consommations_200_contenu(mocker):
    import api.consommations as c
    mocker.patch.object(c, "read_consos_publiees", return_value=_conso_rows())

    body, status = c.handle_consommations(FakeReq(None, params=_params()))

    assert status == 200
    assert body["provider"] == "grdf"
    assert body["sensor_id"] == "GI12345678901234"
    # règle "Contenu" → seuls R2, R3, R4 (strictement dans [from, to])
    debuts = [d["date_debut"] for d in body["data"]]
    assert debuts == ["2024-01-01", "2024-03-01", "2024-05-01"]
    assert body["data"][0]["consommation"] == 1234.5
    assert body["data"][0]["unite"] == "kWh"
    assert set(body["data"][0].keys()) == {"date_debut", "date_fin", "consommation", "unite"}


def test_consommations_200_vide_si_aucun_contenu(mocker):
    import api.consommations as c
    mocker.patch.object(c, "read_consos_publiees", return_value=_conso_rows())
    body, status = c.handle_consommations(
        FakeReq(None, params=_params(**{"from": "2025-01-01", "to": "2025-02-01"}))
    )
    assert status == 200
    assert body["data"] == []


def test_consommations_404_sensor_introuvable(mocker):
    import api.consommations as c
    mocker.patch.object(c, "read_consos_publiees", return_value=None)

    body, status = c.handle_consommations(FakeReq(None, params=_params()))

    assert status == 404
    assert body["erreur"] == "SENSOR_INTROUVABLE"
    assert body["sensor_id"] == "GI12345678901234"


def test_consommations_400_provider_non_grdf(mocker):
    import api.consommations as c
    read = mocker.patch.object(c, "read_consos_publiees")

    body, status = c.handle_consommations(FakeReq(None, params=_params(provider="enedis")))

    assert status == 400
    assert body["erreur"] == "CHAMP_INVALIDE"
    assert body["champ"] == "provider"
    read.assert_not_called()


def test_consommations_400_sensor_manquant(mocker):
    import api.consommations as c
    mocker.patch.object(c, "read_consos_publiees")
    params = _params()
    del params["sensor_id"]
    body, status = c.handle_consommations(FakeReq(None, params=params))
    assert status == 400
    assert body["champ"] == "sensor_id"


def test_consommations_400_to_avant_from(mocker):
    import api.consommations as c
    mocker.patch.object(c, "read_consos_publiees")
    body, status = c.handle_consommations(
        FakeReq(None, params=_params(**{"from": "2024-06-01", "to": "2024-01-01"}))
    )
    assert status == 400
    assert body["champ"] == "to"


def test_consommations_400_date_invalide(mocker):
    import api.consommations as c
    mocker.patch.object(c, "read_consos_publiees")
    body, status = c.handle_consommations(FakeReq(None, params=_params(**{"from": "01/01/2024"})))
    assert status == 400
    assert body["champ"] == "from"


def test_consommations_403_ip(mocker, monkeypatch):
    import api.consommations as c
    read = mocker.patch.object(c, "read_consos_publiees")
    monkeypatch.setenv("ALLOWED_IPS", "10.0.0.1")
    req = FakeReq(None, headers={"X-Forwarded-For": "8.8.8.8:1234"}, params=_params())

    body, status = c.handle_consommations(req)

    assert status == 403
    read.assert_not_called()


# ----------------------------------------------------------------------
# adict_messages — traduction
# ----------------------------------------------------------------------

def test_translate_adict():
    from api.adict_messages import translate_adict, MESSAGE_INCONNU
    assert translate_adict("1000007") == "Compteur plus accrédité, autorisation expirée"
    assert translate_adict("code_bidon") == MESSAGE_INCONNU
    assert translate_adict(None) is None
    assert translate_adict("") is None


def test_resolve_message_erreur():
    from api.adict_messages import resolve_message_erreur, MESSAGE_INCONNU
    # champ propre prioritaire
    assert resolve_message_erreur({"code_adict_declare": "1000008"}) == "Aucune donnée disponible pour ce compteur"
    # best-effort dans le message brut
    assert resolve_message_erreur({"message_erreur_declare": "... Operation Denied ..."}) == "Déclaration rejetée par le distributeur"
    # message présent mais code inconnu
    assert resolve_message_erreur({"message_erreur_declare": "boom"}) == MESSAGE_INCONNU
    # aucun indice
    assert resolve_message_erreur({"etat_droit_acces": "Active"}) is None
