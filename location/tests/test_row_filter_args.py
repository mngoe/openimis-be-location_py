import json
from dataclasses import dataclass

from graphql_jwt.shortcuts import get_token

from core.models import User
from core.models.openimis_graphql_test_case import openIMISGraphQLTestCase
from core.test_helpers import create_test_interactive_user, mark_deleted
from location.test_helpers import create_test_health_facility, create_test_location


@dataclass
class DummyContext:
    user: User


class LocationRowFilterArgsTest(openIMISGraphQLTestCase):
    """locations / health facilities: the legacy arguments keep their meaning,
    isDeleted / showHistory are new.

    Validity is applied by the models (OP-2029), not by the resolvers: the
    resolvers only state what their legacy arguments mean.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.live_loc = create_test_location("D", custom_props={"code": "RFAL1", "name": "row args live"})
        cls.deleted_loc = mark_deleted(
            create_test_location("D", custom_props={"code": "RFAL2", "name": "row args deleted"}))
        cls.live_hf = create_test_health_facility("RFAH1", cls.live_loc.id, custom_props={"code": "RFAH1"})
        cls.deleted_hf = mark_deleted(
            create_test_health_facility("RFAH2", cls.live_loc.id, custom_props={"code": "RFAH2"}))
        copy_id = cls.live_hf.save_history()
        cls.hf_copy = type(cls.live_hf).objects.get(id=copy_id)
        cls.admin = create_test_interactive_user(username="locRowArgsAdmin")
        cls.token = get_token(cls.admin, DummyContext(user=cls.admin))

    def _uuids(self, field, args):
        response = self.query(
            '{ %s(%s) { edges { node { uuid } } } }' % (field, args),
            headers={"HTTP_AUTHORIZATION": f"Bearer {self.token}"})
        self.assertResponseNoErrors(response)
        edges = json.loads(response.content)["data"][field]["edges"]
        return sorted(e["node"]["uuid"] for e in edges)

    def assertRows(self, expected, field, args):
        self.assertEqual(sorted(str(o.uuid) for o in expected), self._uuids(field, args))

    # health facilities

    def test_health_facilities_default_hides_deleted(self):
        self.assertRows([self.live_hf], "healthFacilities", 'code_Istartswith: "RFAH"')

    def test_health_facilities_show_history_keeps_its_legacy_meaning(self):
        # every row: live, deleted and history copies
        self.assertRows([self.live_hf, self.deleted_hf, self.hf_copy], "healthFacilities",
                        'code_Istartswith: "RFAH", showHistory: true')

    def test_health_facilities_is_deleted(self):
        self.assertRows([self.deleted_hf], "healthFacilities", 'code_Istartswith: "RFAH", isDeleted: true')
        self.assertRows([self.live_hf], "healthFacilities", 'code_Istartswith: "RFAH", isDeleted: false')

    def test_health_facilities_is_deleted_narrows_show_history(self):
        self.assertRows([self.live_hf, self.hf_copy], "healthFacilities",
                        'code_Istartswith: "RFAH", showHistory: true, isDeleted: false')

    def test_health_facilities_str(self):
        self.assertRows([self.live_hf], "healthFacilitiesStr", 'str: "RFAH"')
        self.assertRows([self.deleted_hf], "healthFacilitiesStr", 'str: "RFAH", isDeleted: true')
        self.assertRows([self.live_hf], "healthFacilitiesStr", 'str: "RFAH", isDeleted: false')

    def test_health_facilities_str_ignore_location_still_filters_validity(self):
        self.assertRows([self.live_hf], "healthFacilitiesStr", 'str: "RFAH", ignoreLocation: true')
        self.assertRows([self.deleted_hf], "healthFacilitiesStr",
                        'str: "RFAH", ignoreLocation: true, isDeleted: true')

    # locations

    def test_locations(self):
        for field, args in (("locations", 'code_Istartswith: "RFAL"'),
                            ("locationsAll", 'code_Istartswith: "RFAL"'),
                            ("locationsStr", 'str: "RFAL"')):
            with self.subTest(field=field):
                self.assertRows([self.live_loc], field, args)
                self.assertRows([self.deleted_loc], field, args + ", isDeleted: true")
                self.assertRows([self.live_loc], field, args + ", isDeleted: false")
