from functools import reduce
import django
from django.core.cache import caches
from django_redis.cache import RedisCache
import uuid

from core import filter_validity
from django.conf import settings
from django.db import models, connection, transaction
from django.dispatch import receiver
from django.db.models.signals import post_save, post_delete

from django.db.models.expressions import RawSQL
from program import models as program_models
from core import models as core_models
from core.cache_control import cache_delete, cache_delete_many, cache_get, cache_memo_forget, cache_set
from graphql import ResolveInfo
from .apps import LocationConfig
import logging
from django.db.models import Q

logger = logging.getLogger(__file__)
LOCATION_CACHE = "location"
cache = caches[LOCATION_CACHE]


# Reads and writes go through core.cache_control (ported from release/26.10): every
# entry expires after CACHE_OBJECT_TTL, a failing backend degrades to a miss, and a
# process-local cache is not trusted while mutations run in a worker.
def _cache_get(key):
    return cache_get(key, alias=LOCATION_CACHE)


def _cache_set(key, value):
    return cache_set(key, value, alias=LOCATION_CACHE)


USER_LOCATION_CACHE_KEYS = ("user_locations_{}", "user_districts_{}")
LOCATION_GRAPH_CACHE_KEYS = ("location_graph", "location_types")


def free_cache_for_user(user_id="*"):
    """Drop the cached location and district sets for one user, or for everyone.

    ``user_id="*"`` means everyone, which is what a location change needs: it
    shifts the expanded set of every user whose branch the location falls under.
    Globbing is a django-redis extension, so the wildcard needs ``delete_pattern``
    on Redis and a full ``clear()`` on any other backend. (The condition used to
    be inverted: Redis got a literal ``delete("user_locations_*")``, which
    invalidated nothing.)
    """
    if user_id == "*":
        cache_memo_forget([key.format("") for key in USER_LOCATION_CACHE_KEYS], alias=LOCATION_CACHE)
        try:
            if isinstance(cache, RedisCache):
                for key in USER_LOCATION_CACHE_KEYS:
                    cache.delete_pattern(key.format("*"))
            else:
                cache.clear()
        except Exception as exc:  # a cache outage must not abort the write
            logger.warning("location cache flush failed: %s", exc)
        return
    cache_delete_many([key.format(user_id) for key in USER_LOCATION_CACHE_KEYS], alias=LOCATION_CACHE)


def free_cache_for_user_now_and_on_commit(user_id="*"):
    """``free_cache_for_user``, once now and once more when the write commits.

    The signals that call this run inside the writer's transaction: a request
    reading committed data in between would re-cache the old state.
    """
    free_cache_for_user(user_id)
    transaction.on_commit(lambda: free_cache_for_user(user_id))


def free_location_graph_cache():
    """Drop the cached location tree; it is rebuilt lazily on the next read."""
    cache_delete_many(LOCATION_GRAPH_CACHE_KEYS, alias=LOCATION_CACHE)


def free_location_caches_now_and_on_commit():
    """A location was created, changed, moved or deleted: drop the tree and every user's sets."""
    def flush():
        free_location_graph_cache()
        free_cache_for_user("*")
    flush()
    transaction.on_commit(flush)


@receiver(post_save, sender=core_models.InteractiveUser)
@receiver(post_delete, sender=core_models.InteractiveUser)
def free_cache_post_user_save(sender, instance, **kwargs):
    free_cache_for_user_now_and_on_commit(instance.id)


class LocationManager(models.Manager):
    def parents(self, location_id, loc_type=None):
        parents = Location.objects.raw(
            f"""
            WITH {"" if settings.MSSQL else "RECURSIVE"} CTE_PARENTS AS (
            SELECT
                "LocationId",
                "LocationType",
                "ParentLocationId"
            FROM
                "tblLocations"
            WHERE "LocationId" in ( %s )
            UNION ALL

            SELECT
                parent."LocationId",
                parent."LocationType",
                parent."ParentLocationId"
            FROM
                "tblLocations" parent
                INNER JOIN CTE_PARENTS leaf
                    ON parent."LocationId" = leaf."ParentLocationId"
            )
            SELECT * FROM CTE_PARENTS;
        """,
            (
                (
                    ",".join(map(str, location_id))
                    if isinstance(location_id, list)
                    else location_id
                ),
            ),
        )
        return self.get_location_from_ids((parents), loc_type) if loc_type else parents

    def allowed(self, user_id, loc_types=['R', 'D', 'W', 'V'], strict=True, qs=False):
        query = f"""
            WITH {"" if settings.MSSQL else "RECURSIVE"} USER_LOC AS (SELECT l."LocationId", l."ParentLocationId" FROM "tblUsersDistricts" ud JOIN "tblLocations" l ON ud."LocationId" = l."LocationId"  WHERE ud."ValidityTo"  is Null AND "UserID" = %s ),
             CTE_PARENTS AS (
            SELECT
                parent."LocationId",
                parent."LocationType",
                parent."ParentLocationId"

            FROM
                "tblLocations" parent
            WHERE "LocationId" in (SELECT "LocationId" FROM USER_LOC) 
            OR (  parent."LocationId" in  (SELECT "ParentLocationId" FROM USER_LOC) 
                    {'AND (SELECT COUNT(*) FROM USER_LOC  ul WHERE ul."ParentLocationId" = parent."LocationId" ) =  (SELECT COUNT(*) FROM "tblLocations" l WHERE l."ParentLocationId" = parent."LocationId" AND l."ValidityTo" is Null  )' if strict else ""})
            UNION ALL
            SELECT
                child."LocationId",
                child."LocationType",
                child."ParentLocationId"
            FROM
                "tblLocations"  child
                INNER JOIN CTE_PARENTS leaf
                    ON child."ParentLocationId" = leaf."LocationId"
            )
            SELECT DISTINCT "LocationId" FROM CTE_PARENTS WHERE "LocationType" in ('{"','".join(loc_types)}')
        """

        if qs is not None:
            # location_allowed = Location.objects.filter( id__in =[obj.id for obj in Location.objects.raw( query,(user_id,))])
            if settings.MSSQL: # MSSQL don't support WITH in subqueries

                with connection.cursor() as cursor:
                    cursor.execute(query, (user_id,))
                    ids = cursor.fetchall()
                    location_allowed = Location.objects.filter(id__in=[x for x, in ids])
            else:
                location_allowed = Location.objects.filter(id__in=RawSQL(query, (user_id,)))

        else:
            location_allowed = Location.objects.raw(query, (user_id,))

        return location_allowed

    def children(self, location_id, loc_type=None):
        children = Location.objects.raw(
            f"""
                WITH {"" if settings.MSSQL else "RECURSIVE"} CTE_CHILDREN AS (
                SELECT
                    "LocationId",
                    "LocationType",
                    "ParentLocationId",
                    0 as "Level"
                FROM
                    "tblLocations"
                WHERE "LocationId" = %s
                UNION ALL

                SELECT
                    child."LocationId",
                    child."LocationType",
                    child."ParentLocationId",
                    parent."Level" + 1 as "Level" 
                FROM
                    "tblLocations" child
                    INNER JOIN CTE_CHILDREN parent
                        ON child."ParentLocationId" = parent."LocationId"
                )
                SELECT * FROM CTE_CHILDREN;
            """,
            (location_id,),
        )
        return self.get_location_from_ids((children), loc_type) if loc_type else children


    def build_user_location_filter_query(self, user: core_models.InteractiveUser, prefix='location', queryset=None, loc_types=['R', 'D', 'W', 'V'], link_types=None):
        """
        The row filter for `user` on a path pointing at a Location.

        Two things narrow a user: the districts they are assigned to
        (`tblUsersDistricts`, through `allowed`) and the UBA credentials the caller asks
        for. They are AND'ed: a credential narrows *within* the assigned districts, it
        never grants access outside them. A user holding no link under the demanded
        credentials keeps the plain district filter, which is why
        `build_uba_location_filter_query` answers None rather than an empty filter.

        `link_types` is that demand, a single code or a list of them, "ALL" for every
        location aware one. It defaults to None, i.e. *no* UBA narrowing, because only
        the module owning the rows knows which credential governs them, and a credential
        must not narrow a scope it says nothing about: a user holding a CLAIM_ADMIN link
        on one facility still enrols wherever their districts allow, the insuree module
        asking for ENROLMENT only.

        What a module does *not* have to restate is the path: from the location path it
        already passes, the filter works out where the linked object sits, reading the
        credential's registry `params`. Naming the credential once per `get_queryset` also
        makes the rule reach the REST/FHIR API, which goes through the same method.
        """
        q_allowed_location = None
        if not isinstance(user, core_models.InteractiveUser):
            logger.warning(f"Access without filter for user {user.id} ")
            if queryset is not None:
                return queryset
            else:
                return Q()
        elif not user.is_imis_admin:
            q_allowed_location = Q((f"{prefix}__in", self.allowed(user.id, loc_types))) | Q((f"{prefix}__isnull", True))
            q_uba = self.build_uba_location_filter_query(
                user, prefix=prefix, loc_types=loc_types, link_types=link_types)
            if q_uba is not None:
                q_allowed_location &= q_uba

            if queryset is not None:
                return queryset.filter(q_allowed_location)
            else:
                return q_allowed_location
        else:
            if queryset is not None:
                return queryset
            else:
                return Q()

    # --- UBA aware narrowing ---------------------------------------------------- #

    def build_uba_location_filter_query(self, user, prefix='location', loc_types=('R', 'D', 'W', 'V'),
                                        link_types=None):
        """
        The narrowing the UBA credentials of `user` imply on `prefix`.

        One term per considered credential they actually hold a link on, OR'ed together:
        holding an ENROLMENT link on a village and a CLAIM_ADMIN link on a health facility
        widens what they see rather than intersecting it.

        `link_types` says which credentials are considered, a single code or a list of
        them; `ALL_UBA_LINK_TYPES` ("ALL", also honoured inside a list) takes every
        location aware one. None, the default, considers none and so narrows nothing: a
        module opts in by naming the credentials its own rows answer to.

        None when they hold no considered link, so that the caller keeps its district
        filter instead of AND'ing with something that matches nothing.
        """
        from core.uba_filters import business_access_object_ids
        from core.uba_link_types import get_location_aware_uba_link_types

        prefix_type = self._prefix_location_type(loc_types)
        q_uba = None
        for link_type in get_location_aware_uba_link_types(link_types):
            model_label = link_type.models[0] if link_type.models else None
            object_ids = business_access_object_ids(user, link_type.code, model_label)
            if not object_ids:
                continue
            term = self._uba_link_type_filter_query(link_type, object_ids, prefix, prefix_type)
            if term is None:
                # the credential says nothing about this path, e.g. a health facility
                # credential on a queryset that never reaches a health facility
                continue
            q_uba = term if q_uba is None else q_uba | term
        return q_uba

    def _uba_link_type_filter_query(self, link_type, object_ids, prefix, prefix_type):
        """
        Turn one credential into a filter on `prefix`, using its registry `params`.

        `location_field` means the credential is held on a model hanging off a location,
        so the path to it is the location path minus that last segment
        ('health_facility__location' -> 'health_facility'). `location_type` means it is
        held on a Location, so the path is the location path re-aimed at that type.
        """
        from core.uba_filters import uba_filter_from_object_ids

        params = link_type.params
        location_field = params.get("location_field")
        if location_field:
            if prefix == location_field:
                # the filtered model *is* the one the credential is held on
                return uba_filter_from_object_ids(object_ids)
            suffix = f"__{location_field}"
            if not prefix.endswith(suffix):
                return None
            return uba_filter_from_object_ids(object_ids, prefix=prefix[:-len(suffix)])

        location_type = params.get("location_type")
        configured = list(LocationConfig.location_types) or ['R', 'D', 'W', 'V']
        if not location_type or not prefix_type:
            return None
        if location_type not in configured or prefix_type not in configured:
            logger.warning(
                "UBA link type '%s' declares location_type '%s', not in the configured %s",
                link_type.code, location_type, configured)
            return None
        location_ids = self._expand_location_ids(
            object_ids,
            descendants=bool(params.get("include_descendants")),
            ancestors=bool(params.get("include_ancestors")),
        )
        delta = configured.index(location_type) - configured.index(prefix_type)
        path = self._walk_prefix(prefix, delta)
        if path is not None:
            return uba_filter_from_object_ids(location_ids, prefix=path)
        # the path cannot be walked down to the level the credential is held on: compare
        # what the row does expose, its ancestor at the prefix level. Coarser, but the
        # narrowing still applies instead of being silently dropped
        return uba_filter_from_object_ids(
            self._locations_at_type(location_ids, prefix_type), prefix=prefix)

    @staticmethod
    def _prefix_location_type(loc_types):
        """
        The loc_type the `prefix` points at, taken as the deepest type the caller allows:
        `loc_types=['D']` goes with a path climbing to the district, the default (every
        type) with a plain location field.
        """
        configured = list(LocationConfig.location_types) or ['R', 'D', 'W', 'V']
        allowed = [t for t in configured if t in (loc_types or configured)]
        return allowed[-1] if allowed else None

    @staticmethod
    def _walk_prefix(prefix, delta):
        """
        Re-aim a location path `delta` levels deeper (positive) or shallower (negative).

        A path such as 'location__parent__parent' was built by climbing from the row's own
        village up to its district, so going back down is dropping trailing '__parent'
        segments and going up is adding them. None when there is no '__parent' left to
        drop, the caller then comparing ancestors instead.
        """
        if delta == 0:
            return prefix
        if delta < 0:
            return prefix + '__parent' * (-delta)
        parts = prefix.split('__')
        if len(parts) <= delta or any(part != 'parent' for part in parts[-delta:]):
            return None
        return '__'.join(parts[:-delta])

    @staticmethod
    def _parent_paths():
        """
        'parent', 'parent__parent', ... one per level of the configured tree below its top,
        so that a fixed number of joins replaces walking the tree level by level.
        """
        configured = list(LocationConfig.location_types) or ['R', 'D', 'W', 'V']
        return ['__'.join(['parent'] * depth) for depth in range(1, len(configured))]

    @classmethod
    def _expand_location_ids(cls, location_ids, descendants=False, ancestors=False):
        """
        Widen location ids down and/or up the tree, for the `include_descendants` and
        `include_ancestors` params: a credential granted on a district can then reach its
        villages.

        `location_ids` is a list of ids or a queryset of them. Returns a lazy queryset of
        location ids, used as a subquery: a district widened to its villages never travels
        as a list of thousands of parameters.
        """
        if not isinstance(location_ids, models.QuerySet):
            location_ids = [int(object_id) for object_id in location_ids]
        paths = cls._parent_paths()
        q_ids = Q(id__in=location_ids)
        if descendants:
            for path in paths:
                q_ids |= Q(**{f"{path}__id__in": location_ids})
        widened = Location.objects.filter(q_ids, *filter_validity()).values_list('id', flat=True)
        if ancestors:
            q_ids = Q(id__in=widened)
            for path in paths:
                q_ids |= Q(id__in=widened.values(f"{path}__id"))
            widened = Location.objects.filter(q_ids, *filter_validity()).values_list('id', flat=True)
        return widened

    @classmethod
    def _locations_at_type(cls, location_ids, loc_type):
        """
        The locations of `location_ids`, or their ancestor, sitting at `loc_type`, as a lazy
        queryset of ids. Used when a path stops above the level a credential is held on.
        """
        locations = Location.objects.filter(id__in=location_ids)
        q_ids = Q(id__in=locations.values_list('id', flat=True))
        for path in cls._parent_paths():
            q_ids |= Q(id__in=locations.values(f"{path}__id"))
        return Location.objects.filter(q_ids, *filter_validity(), type=loc_type).values_list('id', flat=True)

    def uba_location_ids(self, user, link_types, include_ancestors=True):
        """
        The locations a user's UBA credentials put them on, for a location picker.

        The linked locations themselves - widened below them when the credential declares
        `include_descendants` - plus, with `include_ancestors`, every location above them,
        so a picker walking the tree from the region down can reach the linked village.
        Being able to *navigate* to an ancestor does not make it assignable: the
        mutations still check the credential on the location they write.

        `link_types` names the credentials considered, as for the row filter. None when
        the user holds no link under them, the caller then keeping its usual scope.
        Otherwise a lazy queryset of location ids, to be used in an `id__in` subquery.
        """
        from core.uba_filters import business_access_object_ids
        from core.uba_link_types import get_location_aware_uba_link_types

        widened = []
        for link_type in get_location_aware_uba_link_types(link_types):
            model_label = link_type.models[0] if link_type.models else None
            object_ids = business_access_object_ids(user, link_type.code, model_label)
            if not object_ids:
                continue
            location_field = link_type.params.get("location_field")
            if location_field:
                # held on a model hanging off a location: its location is where it sits
                from django.apps import apps
                model = apps.get_model(model_label)
                object_ids = model.objects.filter(id__in=object_ids).exclude(
                    **{f"{location_field}__isnull": True}
                ).values_list(f"{location_field}_id", flat=True)
            widened.append(self._expand_location_ids(
                object_ids, descendants=bool(link_type.params.get("include_descendants"))))
        if not widened:
            return None
        location_ids = Location.objects.filter(
            reduce(lambda q, ids: q | Q(id__in=ids), widened, Q())).values_list('id', flat=True)
        if include_ancestors:
            location_ids = self._expand_location_ids(location_ids, ancestors=True)
        return location_ids

    def get_location_from_ids(self, qsr, loc_type):
        if loc_type:
            return [x for x in list(qsr) if x.type == loc_type]
        return list(qsr)

def cache_location_graph(location_id=None):
    """Cache the location graph as a dictionary of edges."""
    locations = Location.objects.filter(
        Q(parent__isnull=False) | Q(type='R'),
        *filter_validity(),
    )
    graph = {}
    location_types = {}
    for location in locations:
        parent_id = location.parent_id if location.parent_id else "root"
        if parent_id not in graph:
            graph[parent_id] = set()
        graph[parent_id].add(location.id)
        if location.type not in location_types:
            location_types[location.type] = set()
        location_types[location.type].add(location.id)
    _cache_set("location_graph", graph)
    _cache_set("location_types", location_types)
    return location_types


def cache_location_if_not_cached():
    location_types = _cache_get("location_types")
    if location_types is None:
        location_types = cache_location_graph()
    return location_types


# Function to update the cache when Location objects are modified
def update_location_cache(sender, instance, **kwargs):
    free_location_caches_now_and_on_commit()

class Location(core_models.VersionedModel, core_models.ExtendableModel):
    # OP-2029: versions carry a business meaning, a validity date picks the one valid then.
    business_versioned = True

    objects = LocationManager()

    id = models.AutoField(db_column='LocationId', primary_key=True)
    uuid = models.CharField(db_column='LocationUUID',
                            max_length=36, default=uuid.uuid4, unique=True)
    code = models.CharField(db_column='LocationCode',
                            max_length=8, blank=True, null=True)
    name = models.CharField(db_column='LocationName',
                            max_length=50, blank=True, null=True)
    parent = models.ForeignKey('Location', models.DO_NOTHING,
                               db_column='ParentLocationId',
                               blank=True, null=True, related_name='children')
    type = models.CharField(db_column='LocationType', max_length=1)
    male_population = models.IntegerField(
        db_column='MalePopulation', blank=True, null=True)
    female_population = models.IntegerField(
        db_column='FemalePopulation', blank=True, null=True)
    other_population = models.IntegerField(
        db_column='OtherPopulation', blank=True, null=True)
    families = models.IntegerField(db_column='Families', blank=True, null=True)

    # rowid = models.TextField(db_column='RowId')
    audit_user_id = models.IntegerField(
        db_column='AuditUserId', blank=True, null=True)

    def __str__(self):
        return self.code + " " + self.name

    @classmethod
    def get_queryset(cls, queryset, user=None, *, validity=None, audits=None):
        # OP-2029: validity / audits asked explicitly win over the default (current rows)
        queryset = cls.apply_row_filters(queryset, validity, audits, default=cls.filter_queryset)
        # GraphQL calls with an info object while Rest calls with the user itself
        if isinstance(user, ResolveInfo):
            user = user.context.user
        if user is None or (settings.ROW_SECURITY and user.is_anonymous):
            return cls.no_rows(queryset)

        # OMT-280: if you create a new region and your user has district limitations, you won't find what you
        # just created. So we'll consider that if you were allowed to create it, you are also allowed to retrieve it.
        if settings.ROW_SECURITY \
                and not user.has_perms(LocationConfig.gql_mutation_create_region_locations_perms) \
                and not user.is_superuser:
            if user.is_officer:
                from core.models import Officer
                allowed = Officer.objects \
                    .filter(code=user.username, has_login=True, validity_to__isnull=True) \
                    .get().officer_allowed_locations
            elif user.is_claim_admin:
                from claim.models import ClaimAdmin
                allowed = ClaimAdmin.objects \
                    .filter(code=user.username, has_login=True, validity_to__isnull=True) \
                    .get().officer_allowed_locations
            elif user.is_imis_admin:
                return queryset
            else:
                allowed = cls.objects.allowed(user.i_user_id, qs=True)
            # narrow what the caller passed rather than replace it: a resolver returning a
            # filtered queryset (graphene hands it to this method) keeps its filter
            return queryset.filter(id__in=allowed.values('id'))
        return queryset

    @staticmethod
    def build_user_location_filter_query(cls, user: core_models.InteractiveUser, queryset=None,
                                         link_types=None):
        return cls.objects.build_user_location_filter_query(user, queryset=queryset, link_types=link_types)

    class Meta:
        managed = True
        db_table = 'tblLocations'


class HealthFacilityLegalForm(models.Model):
    code = models.CharField(db_column='LegalFormCode', primary_key=True, max_length=1)
    legal_form = models.CharField(db_column='LegalForms', max_length=50)
    sort_order = models.IntegerField(db_column='SortOrder', blank=True, null=True)
    alt_language = models.CharField(db_column='AltLanguage', max_length=50, blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'tblLegalForms'


class HealthFacilitySubLevel(models.Model):
    code = models.CharField(db_column='HFSublevel', primary_key=True, max_length=1)
    health_facility_sub_level = models.CharField(db_column='HFSublevelDesc', max_length=50, blank=True, null=True)
    sort_order = models.IntegerField(db_column='SortOrder', blank=True, null=True)
    alt_language = models.CharField(db_column='AltLanguage', max_length=50, blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'tblHFSublevel'


class HealthFacility(core_models.VersionedModel, core_models.ExtendableModel):
    # OP-2029: versions carry a business meaning, a validity date picks the one valid then.
    business_versioned = True

    class HealthFacilityStatus(models.TextChoices):
        ACTIVE = "AC"
        INACTIVE = "IN"
        DELISTED = "DE"
        IDLE = "ID"

    id = models.AutoField(db_column='HfID', primary_key=True)
    uuid = models.CharField(
        db_column='HfUUID', max_length=36, default=uuid.uuid4, unique=True)

    code = models.CharField(db_column='HFCode', max_length=8)
    name = models.CharField(db_column='HFName', max_length=100)
    acc_code = models.CharField(
        db_column='AccCode', max_length=25, blank=True, null=True)
    legal_form = models.ForeignKey(
        HealthFacilityLegalForm, models.DO_NOTHING,
        db_column='LegalForm',
        related_name="health_facilities")
    level = models.CharField(db_column='HFLevel', max_length=1)
    sub_level = models.ForeignKey(
        HealthFacilitySubLevel, models.DO_NOTHING,
        db_column='HFSublevel', blank=True, null=True,
        related_name="health_facilities")
    location = models.ForeignKey(
        Location, models.DO_NOTHING, db_column='LocationId')
    address = models.CharField(
        db_column='HFAddress', max_length=100, blank=True, null=True)
    phone = models.CharField(
        db_column='Phone', max_length=50, blank=True, null=True)
    fax = models.CharField(
        db_column='Fax', max_length=50, blank=True, null=True)
    email = models.CharField(
        db_column='eMail', max_length=50, blank=True, null=True)

    care_type = models.CharField(db_column='HFCareType', max_length=1)

    services_pricelist = models.ForeignKey('medical_pricelist.ServicesPricelist', models.DO_NOTHING,
                                           db_column='PLServiceID', blank=True, null=True,
                                           related_name="health_facilities")
    items_pricelist = models.ForeignKey('medical_pricelist.ItemsPricelist', models.DO_NOTHING, db_column='PLItemID',
                                        blank=True, null=True, related_name="health_facilities")
    offline = models.BooleanField(db_column='OffLine', default=False)
    # row_id = models.BinaryField(db_column='RowID', blank=True, null=True)
    audit_user_id = models.IntegerField(db_column='AuditUserID')
    program = models.ManyToManyField(program_models.Program)
    bank_name = models.CharField(
        db_column='BankName', max_length=255, blank=True, null=True)
    contract_start_date = models.DateField(db_column='ContractStartDate', blank=True, null=True)
    contract_end_date = models.DateField(db_column='ContractEndDate', blank=True, null=True)
    status = models.CharField(max_length=2, choices=HealthFacilityStatus.choices, default=HealthFacilityStatus.ACTIVE)

    def __str__(self):
        return self.code + " " + self.name

    @classmethod
    def get_queryset(cls, queryset, user=None, *, validity=None, audits=None, **kwargs):
        # GraphQL calls with an info object while Rest calls with the user itself
        if isinstance(user, ResolveInfo):
            user = user.context.user
        if user is None or (settings.ROW_SECURITY and user.is_anonymous):
            return cls.no_rows(queryset)
        if audits is not None and not user.has_perms(LocationConfig.gql_query_health_facilities_perms):
            # the audit trail (showHistory) is for holders of the query right only
            audits = None
        if user.has_perms(LocationConfig.gql_query_health_facilities_perms) and queryset is None \
                and validity is None and audits is None:
            queryset = HealthFacility.objects
        else:
            # OP-2029: validity / audits asked explicitly win over the default (current rows)
            queryset = cls.apply_row_filters(queryset, validity, audits, default=cls.filter_queryset)
        if settings.ROW_SECURITY and not user._u.is_imis_admin:
            return LocationManager().build_user_location_filter_query(user._u, queryset=queryset, loc_types=['D'])
        return queryset

    class Meta:
        managed = True
        db_table = 'tblHF'

    LEVEL_HEALTH_CENTER = 'C'
    LEVEL_DISPENSARY = 'D'
    LEVEL_HOSPITAL = 'H'

    CARE_TYPE_IN_PATIENT = 'I'
    CARE_TYPE_OUT_PATIENT = 'O'
    CARE_TYPE_BOTH = 'B'


class HealthFacilityCatchment(core_models.RowSecurityMixin, models.Model):
    # Visible when its health facility is (HealthFacility scopes itself in code,
    # so ParentScope narrows through HealthFacility.get_queryset).
    row_scope = core_models.ParentScope("health_facility")

    @classmethod
    def live_rows_q(cls):
        from django.db.models import Q
        return Q(validity_to__isnull=True)

    @classmethod
    def current_rows_q(cls):
        from django.db.models import Q
        return Q(legacy_id__isnull=True)

    id = models.AutoField(db_column='HFCatchmentId', primary_key=True)
    legacy_id = models.IntegerField(db_column='LegacyId', blank=True, null=True)
    health_facility = models.ForeignKey(
        HealthFacility,
        models.DO_NOTHING,
        db_column='HFID',
        related_name="catchments"
    )
    location = models.ForeignKey(
        Location,
        models.DO_NOTHING,
        db_column='LocationId',
        related_name="catchments"
    )
    catchment = models.IntegerField(db_column='Catchment', blank=True, null=True)
    validity_from = models.DateTimeField(db_column='ValidityFrom', blank=True, null=True)
    validity_to = models.DateTimeField(db_column='ValidityTo', blank=True, null=True)

    audit_user_id = models.IntegerField(db_column='AuditUserId', blank=True, null=True)

    class Meta:
        managed = True
        db_table = 'tblHFCatchment'


class UserDistrict(core_models.VersionedModel):
    id = models.AutoField(db_column="UserDistrictID", primary_key=True)
    user = models.ForeignKey(
        core_models.InteractiveUser, models.DO_NOTHING, db_column="UserID"
    )
    location = models.ForeignKey(Location, models.DO_NOTHING, db_column="LocationId")
    audit_user_id = models.IntegerField(db_column="AuditUserID")

    class Meta:
        managed = True
        db_table = 'tblUsersDistricts'
        indexes = [
            models.Index(fields=["user", "location"], name="idx_user_location"),
            models.Index(fields=["user", "location", "validity_to"], name="idx_user_location_validity"),
        ]

    @classmethod
    def get_user_districts(cls, user):
        """
        Retrieve the list of UserDistricts for a user, the locations are prefetched on two levels.
        :param user: InteractiveUser to filter on
        :return: UserDistrict *objects*
        """

        if hasattr(user, "_u"):
            user = user._u

        userDistrictKey = f"user_districts_{user.id}"
        if LocationConfig.no_location_check:
            userDistrictKey = "user_districts_all"
        cachedata = _cache_get(userDistrictKey)
        districts = []
        if cachedata is None:
            cache_location_type = cache_location_if_not_cached()
            cachedata = []
            if user.is_superuser or LocationConfig.no_location_check:
                for loc in cache_location_type.get('D', ()):
                    cachedata.append([0, loc])
            elif not isinstance(user, core_models.InteractiveUser):
                if isinstance(user, core_models.TechnicalUser):
                    logger.warning(
                        f"get_user_districts called with a technical user `{user.username}`. "
                        "We'll return an empty list, but it should be handled before reaching here."
                    )
            else:
                districts = (
                    UserDistrict.objects.filter(
                        user=user,
                        location__type="D",
                        location__parent__isnull=False,
                        *filter_validity(),
                        *filter_validity(prefix="location__"),
                    )
                    .order_by("location__parent__code")
                    .order_by("location__code")
                )
            for d in districts:
                cachedata.append([d.id, d.location_id])

            _cache_set(userDistrictKey, cachedata)

        if not districts and cachedata:
            # the ids come from the cache, the locations from the database: a cached
            # Location object would outlive any rename or move
            locations = Location.objects.in_bulk([d[1] for d in cachedata])
            parents = Location.objects.in_bulk(
                {loc.parent_id for loc in locations.values() if loc.parent_id})
            for district_id, loc_id in cachedata:
                location = locations.get(loc_id)
                if not location:
                    logger.warning(f"district {district_id}:{loc_id} not found")
                    continue
                if location.parent_id:
                    location.parent = parents.get(location.parent_id)
                districts.append(UserDistrict(id=district_id, user=user, location=location))

        return districts

    @classmethod
    def get_user_locations(cls, user):
        """
        Retrieve the list of Locations in the UserDistricts of a certain user.
        :param user: InteractiveUser to filter on
        :return: Location objects to filter on.
        """
        if not core_models.InteractiveUser.is_interactive_user(user):
            return Location.objects.none()
        return Location.objects \
            .filter(*filter_validity()) \
            .filter(parent__parent__userdistrict__user=user.i_user) \
            .order_by("code")

    @classmethod
    def get_queryset(cls, queryset, user=None, *, validity=None, audits=None):
        queryset = cls.apply_row_filters(queryset, validity, audits)
        if isinstance(user, ResolveInfo):
            user = user.context.user
        if user is None or (settings.ROW_SECURITY and user.is_anonymous):
            return cls.no_rows(queryset)
        return queryset


@receiver(post_save, sender=UserDistrict)
@receiver(post_delete, sender=UserDistrict)
def free_cache_post_user_district_save(sender, instance, **kwargs):
    free_cache_for_user_now_and_on_commit(instance.user_id)


@receiver(post_save, sender=Location)
@receiver(post_delete, sender=Location)
def location_changed(sender, instance, **kwargs):
    update_location_cache(sender, instance, **kwargs)


class OfficerVillage(core_models.VersionedModel):
    id = models.AutoField(db_column="OfficerVillageId", primary_key=True)
    officer = models.ForeignKey(
        core_models.Officer,
        models.CASCADE,
        db_column="OfficerId",
        related_name="officer_villages",
    )
    location = models.ForeignKey(
        Location,
        models.CASCADE,
        db_column="LocationId",
        related_name="officer_villages",
    )
    audit_user_id = models.IntegerField(db_column="AuditUserID")

    class Meta:
        managed = True
        db_table = 'tblOfficerVillages'

    @classmethod
    def get_queryset(cls, queryset, user=None, *, validity=None, audits=None):
        queryset = cls.apply_row_filters(queryset, validity, audits)
        if isinstance(user, ResolveInfo):
            user = user.context.user
        if user is None or (settings.ROW_SECURITY and user.is_anonymous):
            return cls.no_rows(queryset)
        return queryset


class LocationMutation(core_models.UUIDModel):
    location = models.ForeignKey(Location, models.DO_NOTHING,
                                 related_name='mutations')
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name='locations')

    class Meta:
        managed = True
        db_table = "location_LocationMutation"


class HealthFacilityMutation(core_models.UUIDModel):
    health_facility = models.ForeignKey(HealthFacility, models.DO_NOTHING,
                                        related_name='mutations')
    mutation = models.ForeignKey(
        core_models.MutationLog, models.DO_NOTHING, related_name='health_facilities')

    class Meta:
        managed = True
        db_table = "location_HealthFacilityMutation"
