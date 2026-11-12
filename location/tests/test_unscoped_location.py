"""
The unscoped-location right (gql_unscoped_location_perms, 121907): its holder is not
narrowed to the locations of their districts. An IMIS admin is unscoped too, with no role grant.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings

from core.test_helpers import create_security_tree, create_right_only_user, perm_ids
from location.apps import LocationConfig
from location.models import Location, LocationManager, is_location_unscoped


@override_settings(ROW_SECURITY=True)
class UnscopedLocationRightTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.t = create_security_tree("ULR")
        cls.query = perm_ids(LocationConfig.gql_query_locations_perms)
        cls.district_user = create_right_only_user("ulrdistrict", cls.query, [cls.t.d1.id])
        cls.national_user = create_right_only_user(
            "ulrnational", cls.query + perm_ids(LocationConfig.gql_unscoped_location_perms), [cls.t.d1.id])

    def setUp(self):
        cache.clear()

    def _villages(self, user):
        return set(Location.objects.filter(id__in=[self.t.v1.id, self.t.v2.id]).filter(
            LocationManager().build_user_location_filter_query(user._u, prefix="id"))
            .values_list("id", flat=True))

    def test_right_declared(self):
        self.assertEqual(["121907"], LocationConfig.gql_unscoped_location_perms)

    def test_district_user_is_narrowed(self):
        self.assertFalse(is_location_unscoped(self.district_user))
        self.assertEqual({self.t.v1.id}, self._villages(self.district_user))

    def test_holder_of_the_right_sees_every_location(self):
        self.assertTrue(is_location_unscoped(self.national_user))
        self.assertEqual({self.t.v1.id, self.t.v2.id}, self._villages(self.national_user))
        ids = set(Location.get_queryset(
            Location.objects.filter(id__in=[self.t.v1.id, self.t.v2.id]), self.national_user)
            .values_list("id", flat=True))
        self.assertEqual({self.t.v1.id, self.t.v2.id}, ids)

    def test_imis_admin_unscoped(self):
        """no role grant needed: an IMIS admin holds every right, and is unscoped anyway"""
        from core.test_helpers import create_test_interactive_user  # IMIS admin role by default
        admin = create_test_interactive_user(username="unscadm")
        self.assertTrue(is_location_unscoped(admin))
