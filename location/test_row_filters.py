"""get_queryset honours the OP-2029 validity contract (epic OP-3317, phase 1).

Ported from the openIMIS reference location/tests/test_row_filters.py
(feature/OP-3317). Explicit validity values return the expected rows; a caller
asking for nothing gets what it got before the contract existed.
"""
from datetime import datetime, timedelta

from django.test import TestCase

from core.models.row_filters import Audits, Validity
from core.test_helpers import (
    assert_row_filters, create_right_only_user, create_security_tree, create_test_interactive_user,
    mark_deleted)
from location.apps import LocationConfig
from location.models import HealthFacility, HealthFacilityCatchment, Location
from location.test_helpers import create_test_health_facility, create_test_location


class LocationRowFiltersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_test_interactive_user(username="locrowfil")

    def test_location(self):
        live = create_test_location("R", custom_props={"code": "RWF1", "name": "row filters live"})
        deleted = mark_deleted(create_test_location("R", custom_props={"code": "RWF2", "name": "row filters deleted"}))
        assert_row_filters(
            self, Location, Location.objects.filter(id__in=[live.id, deleted.id]), self.user,
            live.id, deleted.id, default_hides_deleted=True,
        )

    def test_health_facility(self):
        district = create_test_location("D", custom_props={"code": "RWFD", "name": "row filters district"})
        live = create_test_health_facility("RWFHF1", district.id, custom_props={"code": "RWFHF1"})
        deleted = mark_deleted(create_test_health_facility("RWFHF2", district.id, custom_props={"code": "RWFHF2"}))
        assert_row_filters(
            self, HealthFacility, HealthFacility.objects.filter(id__in=[live.id, deleted.id]), self.user,
            live.id, deleted.id, default_hides_deleted=True,
        )

    def test_a_validity_date_picks_the_location_valid_then(self):
        location = create_test_location("R", custom_props={"code": "RWF3", "name": "row filters dated"})
        rows = Location.objects.filter(id=location.id)
        before = Validity.at(location.validity_from - timedelta(days=1))
        self.assertFalse(Location.get_queryset(rows, self.user, validity=before).exists())
        self.assertTrue(
            Location.get_queryset(rows, self.user, validity=Validity.at(datetime.now() + timedelta(seconds=1))).exists()
        )

    def test_health_facility_audits_are_its_saved_versions(self):
        district = create_test_location("D", custom_props={"code": "RWFD2", "name": "row filters district 2"})
        hf = create_test_health_facility("RWFHF3", district.id, custom_props={"code": "RWFHF3"})
        copy_id = hf.save_history()
        rows = HealthFacility.objects.filter(code="RWFHF3")
        self.assertEqual({hf.id}, set(HealthFacility.get_queryset(rows, self.user).values_list("id", flat=True)))
        self.assertEqual(
            {hf.id, copy_id},
            set(HealthFacility.get_queryset(rows, self.user, audits=Audits.ALL).values_list("id", flat=True)))


class HealthFacilityScopeContractTest(TestCase):
    """The contract keeps row security: a validity value never widens the districts."""

    @classmethod
    def setUpTestData(cls):
        cls.tree = create_security_tree("SPLR")
        cls.user = create_right_only_user(
            "splruser", [int(p) for p in LocationConfig.gql_query_health_facilities_perms], [cls.tree.d1.id])

    def test_validity_all_stays_in_the_users_districts(self):
        rows = HealthFacility.objects.filter(code__startswith="SPLR")
        visible = set(HealthFacility.get_queryset(rows, self.user, validity=Validity.ALL)
                      .values_list("code", flat=True))
        self.assertEqual({"SPLRHF1"}, visible)

    def test_audits_need_the_query_right(self):
        no_right = create_right_only_user("splrnone", [], [self.tree.d1.id])
        copy_id = self.tree.hf1.save_history()
        rows = HealthFacility.objects.filter(code="SPLRHF1")
        self.assertNotIn(copy_id, set(HealthFacility.get_queryset(rows, no_right, audits=Audits.ALL)
                                      .values_list("id", flat=True)))
        self.assertIn(copy_id, set(HealthFacility.get_queryset(rows, self.user, audits=Audits.ALL)
                                   .values_list("id", flat=True)))

    def test_catchments_follow_their_health_facility(self):
        catchments = [
            HealthFacilityCatchment.objects.create(
                health_facility=hf, location=village, catchment=100, audit_user_id=-1,
                validity_from="2019-01-01")
            for hf, village in ((self.tree.hf1, self.tree.v1), (self.tree.hf2, self.tree.v2))]
        visible = set(HealthFacilityCatchment.get_queryset(
            HealthFacilityCatchment.objects.filter(id__in=[c.id for c in catchments]), self.user)
            .values_list("health_facility__code", flat=True))
        self.assertEqual({"SPLRHF1"}, visible)
