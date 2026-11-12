from django.test import TestCase

from core.cache_control import cache_get, cache_set
from core.services.userServices import create_or_update_user_districts
from core.test_helpers import create_enrolment_officer_role, create_test_interactive_user
from location.models import (
    LOCATION_CACHE,
    Location,
    UserDistrict,
    free_cache_for_user,
)
from location.test_helpers import create_test_location


class LocationCacheInvalidationTest(TestCase):
    """The per-user district cache and the location tree must follow the database.

    Each test reads once to fill the cache, changes the data through the path a
    mutation takes, and reads again: a stale entry shows up as the old answer.
    """

    @classmethod
    def setUpTestData(cls):
        cls.region = create_test_location('R', custom_props={"code": "CCH-R", "name": "Cache region"})
        cls.district = create_test_location(
            'D', custom_props={"code": "CCH-D1", "name": "Cache district 1", "parent": cls.region})
        cls.other_district = create_test_location(
            'D', custom_props={"code": "CCH-D2", "name": "Cache district 2", "parent": cls.region})
        cls.user = create_test_interactive_user(
            username="loc_cache_user", roles=[create_enrolment_officer_role().id])
        cls.i_user = cls.user.i_user

    def setUp(self):
        free_cache_for_user("*")
        UserDistrict.objects.filter(user=self.i_user).delete()
        self._assign(self.district)

    def _assign(self, district):
        return UserDistrict.objects.create(
            user=self.i_user, location=district, validity_from="2019-06-01", audit_user_id=-1)

    def _district_ids(self):
        return sorted(d.location_id for d in UserDistrict.get_user_districts(self.i_user))

    def test_new_user_district_is_seen(self):
        self.assertEqual(self._district_ids(), [self.district.id])
        self._assign(self.other_district)
        self.assertEqual(self._district_ids(), sorted([self.district.id, self.other_district.id]))

    def test_bulk_reassignment_is_seen(self):
        # core's service ends the current rows with a bulk update(), which sends no post_save
        self.assertEqual(self._district_ids(), [self.district.id])
        create_or_update_user_districts(self.i_user, [self.other_district.id], -1)
        self.assertEqual(self._district_ids(), [self.other_district.id])

    def test_renamed_district_is_seen(self):
        UserDistrict.get_user_districts(self.i_user)
        Location.objects.filter(id=self.district.id).update(name="stale")  # no signal on purpose
        location = Location.objects.get(id=self.district.id)
        location.name = "Renamed district"
        location.save()
        names = [d.location.name for d in UserDistrict.get_user_districts(self.i_user)]
        self.assertEqual(names, ["Renamed district"])

    def test_free_all_users_drops_user_entries(self):
        # the "*" branch used to be inverted: on Redis it deleted a key literally named "user_*"
        cache_set("user_districts_4242", [[0, 1]], alias=LOCATION_CACHE)
        cache_set("user_locations_4242", [1], alias=LOCATION_CACHE)
        free_cache_for_user("*")
        self.assertIsNone(cache_get("user_districts_4242", alias=LOCATION_CACHE))
        self.assertIsNone(cache_get("user_locations_4242", alias=LOCATION_CACHE))

    def test_new_location_is_in_the_tree(self):
        from location.models import cache_location_if_not_cached
        cache_location_if_not_cached()
        new_district = create_test_location(
            'D', custom_props={"code": "CCH-D3", "name": "Cache district 3", "parent": self.region})
        self.assertIn(new_district.id, cache_location_if_not_cached().get('D', set()))
