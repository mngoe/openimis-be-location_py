"""
Security parity tests for location (row scope on reads, target scope on writes) against
the openimis-dev-tools reference (location fix/setup-version-1.12.0).

Each test asserts the REFERENCE behaviour: a failure is a gap in Cameroon.
Tags: [REF <reference file>::<test>] ported/adapted, [NEW] no reference equivalent.

Note on ignoreLocation: the reference DELIBERATELY lets `healthFacilitiesStr(ignoreLocation:
true)` return facilities outside the user's districts (and walk up to their ancestors);
what must stay scoped is the way back down (test_location_walk_row_security). These tests
follow the reference, not the "excludes R2D1 HF" wording of the plan.

Fixture: create_security_tree("SPL") -> R1>D1>W1>V1 (+HF1 in D1) and R2>D2>W2>V2 (+HF2 in
D2). The restricted users are assigned D1 only.

Run: manage.py test location.test_security_parity --keepdb
"""
import json

from django.core.cache import cache
from django.test import TestCase

from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase
from core.test_helpers import (
    create_right_only_user,
    create_security_tree,
    create_test_interactive_user,
    get_test_token,
    perm_ids,
)
from location.apps import LocationConfig
from location.models import HealthFacility, Location

LOC_QUERY = perm_ids(LocationConfig.gql_query_locations_perms)
HF_QUERY = perm_ids(LocationConfig.gql_query_health_facilities_perms)
LOC_CREATE = perm_ids(LocationConfig.gql_mutation_create_locations_perms)
LOC_EDIT = perm_ids(LocationConfig.gql_mutation_edit_locations_perms)
LOC_DELETE = perm_ids(LocationConfig.gql_mutation_delete_locations_perms)
LOC_MOVE = perm_ids(LocationConfig.gql_mutation_move_location_perms)
HF_EDIT = perm_ids(LocationConfig.gql_mutation_edit_health_facilities_perms)
HF_DELETE = perm_ids(LocationConfig.gql_mutation_delete_health_facilities_perms)


class LocationReadScopeTest(openIMISGraphQLTestCase):
    """Reads: district row scope on locations and health facilities, and the walk rules
    of the reference (up is free, down stays scoped, locationsAll exempt)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tree = create_security_tree("SPL")
        cls.user = create_right_only_user("spl_reader", LOC_QUERY + HF_QUERY, [cls.tree.d1.id])
        cls.super_user = create_test_interactive_user(username="spl_super")
        cls.token = get_test_token(cls.user)
        cls.super_token = get_test_token(cls.super_user)

    def setUp(self):
        cache.clear()

    def _q(self, query, token=None, variables=None):
        response = self.query(
            query, variables=variables,
            headers={"HTTP_AUTHORIZATION": f"Bearer {token or self.token}"})
        self.assertResponseNoErrors(response)
        return json.loads(response.content)["data"]

    def test_health_facilities_connection_is_district_scoped(self):
        """[REF location/models.py HealthFacility.get_queryset (district filter)]"""
        data = self._q('{ healthFacilities(code_Istartswith: "SPLHF", first: 10) '
                       '{ edges { node { code } } } }')
        codes = {e["node"]["code"] for e in data["healthFacilities"]["edges"]}
        self.assertEqual({"SPLHF1"}, codes)

    def test_health_facilities_str_is_scoped_without_ignore_location(self):
        """[REF location/schema.py resolve_health_facilities_str]"""
        data = self._q('{ healthFacilitiesStr(str: "SPLHF", first: 10) '
                       '{ edges { node { code } } } }')
        codes = {e["node"]["code"] for e in data["healthFacilitiesStr"]["edges"]}
        self.assertEqual({"SPLHF1"}, codes)

    def test_locations_are_district_scoped(self):
        """[REF location/models.py Location.get_queryset]"""
        data = self._q('{ locations(code_Istartswith: "SPLD", first: 10) '
                       '{ edges { node { code } } } }')
        codes = {e["node"]["code"] for e in data["locations"]["edges"]}
        self.assertEqual({"SPLD1"}, codes)

    def test_location_of_a_held_facility_comes_with_its_ancestors(self):
        """[REF test_location_walk_row_security.py::test_location_of_a_held_facility_comes_with_its_ancestors]
        ignoreLocation hands out a foreign HF; its location and the parent of that must
        resolve (not null)."""
        data = self._q('query($s: String!) { healthFacilitiesStr(str: $s, ignoreLocation: true, '
                       'first: 5) { edges { node { code location { code parent { code } } } } } }',
                       variables={"s": "SPLHF2"})
        nodes = [e["node"] for e in data["healthFacilitiesStr"]["edges"]]
        self.assertEqual(["SPLHF2"], [n["code"] for n in nodes])
        self.assertIsNotNone(nodes[0]["location"], "HF.location resolves to null")
        self.assertEqual("SPLD2", nodes[0]["location"]["code"])
        self.assertEqual("SPLR2", nodes[0]["location"]["parent"]["code"])

    def test_way_down_from_a_foreign_ancestor_stays_scoped(self):
        """[REF test_location_walk_row_security.py::test_way_down_from_a_foreign_ancestor_stays_scoped]
        Reverse relations (children) of a foreign region must resolve through the row scope."""
        data = self._q('{ locationsAll(code: "SPLR2", first: 1) { edges { node { code '
                       'children { edges { node { code } } } } } } }')
        children = [e["node"]["code"]
                    for e in data["locationsAll"]["edges"][0]["node"]["children"]["edges"]]
        self.assertNotIn("SPLD2", children)

    def test_locations_all_still_walks_the_whole_tree(self):
        """[REF test_location_walk_row_security.py::test_locations_all_still_walks_the_whole_tree]"""
        data = self._q('{ locationsAll(code: "SPLD2", first: 5) '
                       '{ edges { node { code parent { code } } } } }')
        nodes = [e["node"] for e in data["locationsAll"]["edges"]]
        self.assertEqual(["SPLD2"], [n["code"] for n in nodes])
        self.assertEqual("SPLR2", nodes[0]["parent"]["code"])

    def test_locations_all_exemption_survives_an_alias(self):
        """[REF test_location_walk_row_security.py::test_locations_all_exemption_survives_an_alias]"""
        data = self._q('{ everything: locationsAll(code: "SPLD2", first: 5) '
                       '{ edges { node { code } } } }')
        self.assertEqual(["SPLD2"], [e["node"]["code"] for e in data["everything"]["edges"]])

    def test_superuser_is_not_location_restricted(self):
        """[REF test_location_walk_row_security.py::test_superuser_is_not_location_restricted]"""
        data = self._q('{ healthFacilities(code_Istartswith: "SPLHF", first: 10) '
                       '{ edges { node { code } } } }', token=self.super_token)
        codes = {e["node"]["code"] for e in data["healthFacilities"]["edges"]}
        self.assertEqual({"SPLHF1", "SPLHF2"}, codes)


class LocationWriteTargetScopeTest(TestCase):
    """Writes: the object a mutation updates/deletes must be one the user may read.
    [NEW] for location - the reference location module is itself unscoped on writes
    (gql_mutations.py:326/362), but the reference core principle is
    core/tests/test_mutation_target_scope.py (target_queryset == readable rows).
    Each refusal test has a positive control on the user's own district."""

    @classmethod
    def setUpTestData(cls):
        cls.tree = create_security_tree("SPW")
        cls.user = create_right_only_user(
            "spl_writer", LOC_QUERY + HF_QUERY + LOC_CREATE + LOC_EDIT + LOC_DELETE + LOC_MOVE
            + HF_EDIT + HF_DELETE, [cls.tree.d1.id])
        cls.admin = create_test_interactive_user(username="spl_wadmin")

    def setUp(self):
        cache.clear()

    def _hf_payload(self, hf, name):
        return {"uuid": str(hf.uuid), "code": hf.code, "name": name,
                "legal_form_id": hf.legal_form_id, "level": hf.level,
                "location_id": hf.location_id, "care_type": hf.care_type, "offline": False}

    def _update_hf(self, hf, name):
        from location.gql_mutations import UpdateHealthFacilityMutation
        errors = UpdateHealthFacilityMutation.async_mutate(self.user, **self._hf_payload(hf, name))
        return errors, HealthFacility.objects.filter(code=hf.code, validity_to__isnull=True).first()

    def test_control_updating_own_health_facility_works(self):
        errors, hf = self._update_hf(self.tree.hf1, "SPW own renamed")
        self.assertFalse(errors, errors)
        self.assertEqual("SPW own renamed", hf.name)

    def test_updating_a_foreign_health_facility_is_refused(self):
        errors, hf = self._update_hf(self.tree.hf2, "SPW pwned")
        self.assertNotEqual("SPW pwned", hf.name, "HF outside the user's districts was updated")

    def test_deleting_a_foreign_health_facility_is_refused(self):
        from location.gql_mutations import DeleteHealthFacilityMutation
        DeleteHealthFacilityMutation.async_mutate(
            self.user, uuid=str(self.tree.hf2.uuid), code=self.tree.hf2.code)
        self.assertTrue(
            HealthFacility.objects.filter(id=self.tree.hf2.id, validity_to__isnull=True).exists(),
            "HF outside the user's districts was deleted")

    def _update_village(self, village, name):
        from location.gql_mutations import UpdateLocationMutation
        errors = UpdateLocationMutation.async_mutate(
            self.user, uuid=str(village.uuid), code=village.code, name=name, type="V")
        return errors, Location.objects.get(id=village.id)

    def test_control_updating_own_village_works(self):
        errors, v = self._update_village(self.tree.v1, "SPW own village")
        self.assertFalse(errors, errors)
        self.assertEqual("SPW own village", v.name)

    def test_updating_a_foreign_village_is_refused(self):
        errors, v = self._update_village(self.tree.v2, "SPW pwned village")
        self.assertNotEqual("SPW pwned village", v.name,
                            "village outside the user's districts was updated")

    def test_deleting_a_foreign_village_is_refused(self):
        from location.gql_mutations import DeleteLocationMutation
        DeleteLocationMutation.async_mutate(
            self.user, uuid=str(self.tree.v2.uuid), code=self.tree.v2.code)
        self.assertIsNone(Location.objects.get(id=self.tree.v2.id).validity_to,
                          "village outside the user's districts was deleted")

    def _own_ward_with_village(self, code):
        """A ward of the user's district holding one village, fresh for each test."""
        ward = Location.objects.create(code=code, name=f"{code} ward", type="W", parent=self.tree.d1,
                                       audit_user_id=-1, validity_from="2019-01-01")
        village = Location.objects.create(code=f"{code}V", name=f"{code} village", type="V",
                                          parent=ward, audit_user_id=-1, validity_from="2019-01-01")
        return ward, village

    def _delete_ward(self, ward, new_parent):
        from location.gql_mutations import DeleteLocationMutation
        return DeleteLocationMutation.async_mutate(
            self.user, uuid=str(ward.uuid), code=ward.code, new_parent_uuid=str(new_parent.uuid))

    def test_control_deleting_with_own_new_parent_works(self):
        """[NEW] the children of a deleted location go to the new parent the user may read."""
        ward, village = self._own_ward_with_village("SPWA")
        errors = self._delete_ward(ward, self.tree.w1)
        self.assertFalse(errors, errors)
        self.assertIsNotNone(Location.objects.get(id=ward.id).validity_to)
        self.assertEqual(self.tree.w1.id, Location.objects.get(id=village.id).parent_id)

    def test_deleting_with_a_foreign_new_parent_is_refused(self):
        """[NEW] the children of a deleted location cannot be handed to a parent outside
        the user's districts."""
        ward, village = self._own_ward_with_village("SPWB")
        errors = self._delete_ward(ward, self.tree.w2)
        self.assertTrue(errors)
        self.assertIsNone(Location.objects.get(id=ward.id).validity_to,
                          "location deleted although its new parent is outside the user's districts")
        self.assertEqual(ward.id, Location.objects.get(id=village.id).parent_id,
                         "children moved under a parent outside the user's districts")

    def _move(self, location, new_parent):
        from location.gql_mutations import MoveLocationMutation
        return MoveLocationMutation.async_mutate(
            self.user, uuid=str(location.uuid), code=location.code, new_parent_uuid=str(new_parent.uuid))

    def test_control_moving_within_own_district_works(self):
        ward, village = self._own_ward_with_village("SPWC")
        errors = self._move(village, self.tree.w1)
        self.assertFalse(errors, errors)
        self.assertEqual(self.tree.w1.id, Location.objects.get(id=village.id).parent_id)

    def test_moving_into_a_foreign_parent_is_refused(self):
        """[NEW] a location cannot be moved under a parent outside the user's districts."""
        ward, village = self._own_ward_with_village("SPWD")
        errors = self._move(village, self.tree.w2)
        self.assertTrue(errors)
        self.assertEqual(ward.id, Location.objects.get(id=village.id).parent_id,
                         "location moved under a parent outside the user's districts")

    def test_moving_a_foreign_location_is_refused(self):
        """[NEW] a location outside the user's districts cannot be moved into them."""
        errors = self._move(self.tree.v2, self.tree.w1)
        self.assertTrue(errors)
        self.assertEqual(self.tree.w2.id, Location.objects.get(id=self.tree.v2.id).parent_id,
                         "location outside the user's districts was moved")

    def test_deleting_a_district_does_not_crash(self):
        """[NEW] DeleteLocationMutation.__delete_user_districts uses an undefined `user`
        (gql_mutations.py:168): the district is soft-deleted, then a NameError is
        returned as a mutation error."""
        from location.gql_mutations import DeleteLocationMutation
        district = Location.objects.create(code="SPWD9", name="SPW extra", type="D",
                                           parent=self.tree.r1, audit_user_id=-1,
                                           validity_from="2019-01-01")
        errors = DeleteLocationMutation.async_mutate(
            self.admin, uuid=str(district.uuid), code=district.code)
        self.assertFalse(errors, errors)
