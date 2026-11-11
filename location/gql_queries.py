from core.gql.custom_lookup import NotEqual
import graphene
import base64
from graphene_django import DjangoObjectType
from core.gql.scoped_object_type import ScopedQuerysetMixin
from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext as _
from core import prefix_filterset, ExtendedConnection
from location.apps import LocationConfig
from location.models import HealthFacilityLegalForm, Location, HealthFacilitySubLevel, HealthFacilityCatchment, \
    HealthFacility, UserDistrict, OfficerVillage
from django.db.models import Field


class LocationGQLType(DjangoObjectType):
    client_mutation_id = graphene.String()
    Field.register_lookup(NotEqual)

    def resolve_parent(self, info):
        if not info.context.user.is_authenticated:
            raise PermissionDenied(_("unauthorized"))
        if "location_loader" in info.context.dataloaders and self.parent_id:
            return info.context.dataloaders["location_loader"].load(self.parent_id)
        return self.parent

    class Meta:
        model = Location
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "uuid": ["exact"],
            "code": ["exact", "istartswith", "icontains", "iexact", "ne"],
            "name": ["exact", "istartswith", "icontains", "iexact", "ne"],
            "type": ["exact"],
            "parent__uuid": ["exact", "in"],  # can't import itself!
            "parent__parent__uuid": ["exact", "in"],  # can't import itself!
            # can't import itself!
            "parent__parent__parent__uuid": ["exact", "in"],
            "parent__id": ["exact", "in"],  # can't import itself!
        }
        connection_class = ExtendedConnection

    def resolve_client_mutation_id(self, info):
        if not info.context.user.is_authenticated:
            raise PermissionDenied(_("unauthorized"))
        location_mutation = self.mutations.select_related(
            'mutation').filter(mutation__status=0).first()
        return location_mutation.mutation.client_mutation_id if location_mutation else None

    @classmethod
    def get_queryset(cls, queryset, info):
        if info.field_name == "locationsAll":
            return queryset
        else:
            return Location.get_queryset(queryset, info.context.user)


class HealthFacilityLegalFormGQLType(DjangoObjectType):
    class Meta:
        model = HealthFacilityLegalForm


class HealthFacilitySubLevelGQLType(DjangoObjectType):
    class Meta:
        model = HealthFacilitySubLevel


class HealthFacilityCatchmentGQLType(ScopedQuerysetMixin, DjangoObjectType):
    class Meta:
        model = HealthFacilityCatchment


# The connection paths on which a resolver deliberately lifted the health
# facility scope (healthFacilitiesStr with ignoreLocation: walking *up* to a
# facility outside the user's districts is allowed, as in the reference
# test_location_walk_row_security). Keyed by the field path so a sibling
# connection in the same request stays scoped.
HF_SCOPE_LIFTED = "_location_hf_scope_lifted"


def _path_key(info):
    path = getattr(info, "path", None)
    as_list = getattr(path, "as_list", None)
    return tuple(as_list()) if callable(as_list) else str(path)


def lift_health_facility_scope(info):
    lifted = getattr(info.context, HF_SCOPE_LIFTED, None)
    if lifted is None:
        lifted = set()
        setattr(info.context, HF_SCOPE_LIFTED, lifted)
    lifted.add(_path_key(info))


class HealthFacilityGQLType(ScopedQuerysetMixin, DjangoObjectType):
    """Row security of health facilities is HealthFacility.get_queryset, applied to
    every connection (root and nested, e.g. location.healthFacilities)."""
    client_mutation_id = graphene.String()

    @classmethod
    def get_queryset(cls, queryset, info):
        if _path_key(info) in (getattr(info.context, HF_SCOPE_LIFTED, None) or ()):
            return queryset
        return super().get_queryset(queryset, info)

    class Meta:
        model = HealthFacility
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "uuid": ["exact"],
            "code": ["exact", "istartswith", "icontains", "iexact"],
            "fax": ["exact", "istartswith", "icontains", "iexact", "isnull"],
            "email": ["exact", "istartswith", "icontains", "iexact", "isnull"],
            "name": ["exact", "istartswith", "icontains", "iexact"],
            "level": ["exact"],
            "sub_level": ["exact", "isnull"],
            "care_type": ["exact"],
            "legal_form__code": ["exact"],
            "phone": ["exact", "istartswith", "icontains", "iexact"],
            "status": ["exact"],
            **prefix_filterset("location__", LocationGQLType._meta.filter_fields)
        }
        connection_class = ExtendedConnection

    def resolve_location(self, info):
        if not info.context.user.is_authenticated:
            raise PermissionDenied(_("unauthorized"))
        if "location_loader" in info.context.dataloaders:
            return info.context.dataloaders["location_loader"].load(self.location_id)

    def resolve_catchments(self, info):
        if not info.context.user.has_perms(LocationConfig.gql_query_health_facilities_perms):
            raise PermissionDenied(_("unauthorized"))
        return self.catchments.filter(validity_to__isnull=True)

    def resolve_client_mutation_id(self, info):
        if not info.context.user.has_perms(LocationConfig.gql_query_health_facilities_perms):
            raise PermissionDenied(_("unauthorized"))
        health_facility_mutation = self.mutations.select_related(
            'mutation').filter(mutation__status=0).first()
        return health_facility_mutation.mutation.client_mutation_id if health_facility_mutation else None


class UserRegionGQLType(graphene.ObjectType):
    id = graphene.String()
    uuid = graphene.String()
    code = graphene.String()
    name = graphene.String()

    def __init__(self, region):
        self.id = str(base64.b64encode(
            f"LocationGQLType:{region.id}".encode()), 'utf-8')
        self.uuid = region.uuid
        self.code = region.code
        self.name = region.name


class UserDistrictGQLType(graphene.ObjectType):
    id = graphene.String()
    uuid = graphene.String()
    code = graphene.String()
    name = graphene.String()
    parent = graphene.Field(UserRegionGQLType)

    def __init__(self, district):
        self.id = str(base64.b64encode(
            f"LocationGQLType:{district.location_id}".encode()), 'utf-8')
        self.uuid = district.location.uuid
        self.code = district.location.code
        self.name = district.location.name
        self.parent = UserRegionGQLType(district.location.parent)


class UserDistrictType(DjangoObjectType):
    class Meta:
        model = UserDistrict
        filter_fields = {
            "id": ["exact"],
            "user": ["exact"],
            "location": ["exact"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return UserDistrict.get_queryset(queryset, info)


class OfficerVillageGQLType(DjangoObjectType):
    class Meta:
        model = OfficerVillage

    @classmethod
    def get_queryset(cls, queryset, info):
        return OfficerVillage.get_queryset(queryset, info).filter(validity_to__isnull=True)
