# Copyright 2026 The Crossplane Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dependencies declared by referring to another resource's fields.

Providers resolve cross-resource references themselves, through fields like
vpcIdRef and vpcIdSelector that the provider turns into a value at reconcile
time. That works, but Crossplane never sees the relationship: it can't order
anything, and a provider that can't resolve a reference yet retries against
the cloud API until it can.

Writing the reference as a field value instead makes the relationship
visible. The function says where the value comes from, the SDK records the
dependency, and Crossplane waits rather than letting the provider discover
the problem:

    from crossplane.function import reference, resource

    vpc = reference.named("vpc", VPC)

    subnet = Subnet(
        spec={
            "forProvider": {
                "region": "us-east-1",
                "vpcId": reference.external_name(vpc),  # instead of vpcIdSelector
            }
        }
    )
    resource.update(rsp.desired.resources["subnet"], subnet)

    reference.resolve(req, rsp)

That records that subnet depends on vpc, and fills in the VPC's external name
once it exists. For any other field, read it through the named resource and
wrap it in ref:

    "arn": reference.ref(vpc.status.atProvider.arn),

A reference is a marker string until resolve replaces it, because a string is
what a model's own validation accepts in a string field. So references work
only in string fields. Anywhere else the model rejects them, which is at
least a loud failure rather than a silent one.
"""

import json
import types
import typing

import pydantic
from google.protobuf import struct_pb2 as structpb

from crossplane.function import request, resource, response
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

PREFIX = "${xp-ref:"
"""Starts every reference marker. The SDK's runtime looks for it."""

_SUFFIX = "}"

_EXTERNAL_NAME = "@externalName"
_EXTERNAL_NAME_ANNOTATION = "crossplane.io/external-name"

_COMPOSED = "composed"
_REQUIRED = "required"

M = typing.TypeVar("M", bound=pydantic.BaseModel)
V = typing.TypeVar("V")


class _Source(typing.NamedTuple):
    """The resource a reference points at."""

    kind: str  # _COMPOSED or _REQUIRED.
    name: str  # A composed resource name, or a requirement name.
    resource_name: str | None = None  # Required only: one of the matches.
    namespace: str | None = None  # Required only: namespace of resource_name.

    def describe(self) -> str:
        if self.kind == _COMPOSED:
            return self.name
        s = f"requirement {self.name}"
        if self.resource_name:
            ns = f"{self.namespace}/" if self.namespace else ""
            s += f" ({ns}{self.resource_name})"
        return s


class _Field:
    """A stand-in for a resource, or a field of one, that records its path.

    Reading an attribute or an item returns another stand-in one step deeper.
    Given the resource's model it follows the model's own fields, so a typo
    fails here rather than producing a reference to nothing, and a field whose
    JSON name isn't a valid Python name (from, class) is recorded under its
    JSON name.

    It deliberately has no value. Anything that would need one - a truth
    test, str(), iteration - raises, because code that branches on a field
    that doesn't exist yet would otherwise take the wrong branch silently.
    """

    __slots__ = ("_model", "_path", "_source")

    def __init__(self, source: _Source, path: tuple, model: typing.Any = None):
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_model", model)

    def __getattr__(self, attr: str) -> "_Field":
        # Dunder lookups come from copy, pickle, pydantic and friends probing
        # for protocols. Answering them would make a stand-in look like it
        # supports things it doesn't.
        if attr.startswith("__"):
            raise AttributeError(attr)

        key, model = _field_of(self._model, attr, self._describe())
        return _Field(self._source, (*self._path, key), model)

    def __getitem__(self, key: str | int) -> "_Field":
        return _Field(self._source, (*self._path, key), _item_of(self._model))

    def __setattr__(self, attr: str, value: typing.Any) -> None:
        msg = f"cannot set {attr} on {self._describe()}: references are read-only"
        raise AttributeError(msg)

    def __bool__(self) -> bool:
        msg = (
            f"{self._describe()} has no value yet. Wrap it in reference.ref() to "
            "assign it; read the observed resource instead to branch on it."
        )
        raise TypeError(msg)

    def __iter__(self) -> typing.NoReturn:
        msg = f"cannot iterate {self._describe()}; index it instead, e.g. [0]"
        raise TypeError(msg)

    def __str__(self) -> str:
        msg = f"{self._describe()} has no value yet. Wrap it in reference.ref()."
        raise TypeError(msg)

    def __repr__(self) -> str:
        return f"<reference to {self._describe()}>"

    def _describe(self) -> str:
        path = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in self._path)
        return f"{self._source.describe()}{path}"


def _unwrap(annotation: typing.Any) -> typing.Any:
    """Strip Optional, unions with None, and Annotated from a field's type."""
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
            continue
        if origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            if len(args) == 1:
                annotation = args[0]
                continue
        return annotation


def _is_model(t: typing.Any) -> bool:
    return isinstance(t, type) and issubclass(t, pydantic.BaseModel)


def _field_of(model: typing.Any, attr: str, where: str) -> tuple[str, typing.Any]:
    """Return the JSON key for attr on model, and the type of that field.

    Without a model there is nothing to check against, so the attribute is the
    key and the result is untyped.
    """
    if not _is_model(model):
        return attr, None

    fields = model.model_fields
    if attr in fields:
        info = fields[attr]
        return info.alias or attr, _unwrap(info.annotation)

    # Accept the JSON name too, so from and from_ both work.
    for info in fields.values():
        if info.alias == attr:
            return attr, _unwrap(info.annotation)

    msg = f"{where} has no field {attr!r}; {model.__name__} has {', '.join(fields)}"
    raise AttributeError(msg)


def _item_of(model: typing.Any) -> typing.Any:
    """Return the element type of a list or dict type, if it has one."""
    origin, args = typing.get_origin(model), typing.get_args(model)
    if origin is list and args:
        return _unwrap(args[0])
    if origin is dict and len(args) == 2:  # noqa: PLR2004
        return _unwrap(args[1])
    return None


@typing.overload
def named(name: str, model: type[M]) -> M: ...
@typing.overload
def named(name: str, model: None = None) -> typing.Any: ...
def named(name: str, model: type[M] | None = None) -> typing.Any:
    """Name a composed resource so its fields can be referenced.

    Args:
        name: The composed resource's name, the key into desired and observed
            state.
        model: The resource's generated model, if it has one. Fields are then
            checked against it as they're read, and editors can complete them.

    Returns:
        A stand-in that reads like the resource itself:

            vpc = reference.named("vpc", VPC)
            "vpcId": reference.ref(vpc.status.atProvider.id),
    """
    return _Field(_Source(_COMPOSED, name), (), model)


@typing.overload
def named_required(
    requirement_name: str,
    model: type[M],
    *,
    name: str | None = None,
    namespace: str | None = None,
) -> M: ...
@typing.overload
def named_required(
    requirement_name: str,
    model: None = None,
    *,
    name: str | None = None,
    namespace: str | None = None,
) -> typing.Any: ...
def named_required(
    requirement_name: str,
    model: type[M] | None = None,
    *,
    name: str | None = None,
    namespace: str | None = None,
) -> typing.Any:
    """Name a resource the function requires but doesn't compose.

    Args:
        requirement_name: The requirement name, as passed to
            response.require_resources.
        model: The resource's generated model, if it has one.
        name: Name of one resource the requirement matched. Needed when it
            can match more than one; a reference declines to guess.
        namespace: Namespace of name, for a namespaced resource.

    Returns:
        A stand-in that reads like the resource itself.

    Crossplane never deletes a resource it didn't compose, so the dependency
    this records orders only creation and updates.
    """
    return _Field(_Source(_REQUIRED, requirement_name, name, namespace), (), model)


def ref(value: V) -> V:
    """Turn a field of a named resource into a reference you can assign.

    Args:
        value: A field read through a stand-in from named or named_required,
            for example vpc.status.atProvider.id.

    Returns:
        A marker string that resolve replaces with the field's value. It's
        typed as the field is, so it goes wherever that type is expected.

    Raises:
        TypeError: value isn't a field of a named resource. That would
            otherwise silently record no dependency at all.
        ValueError: value is the whole resource rather than a field of it.
    """
    if not isinstance(value, _Field):
        msg = (
            "ref() expects a field of a resource from named() or named_required(), "
            f"for example ref(vpc.status.atProvider.id), not {type(value).__name__}"
        )
        raise TypeError(msg)
    if not value._path:
        msg = (
            f"ref() needs a field, not the whole {value._describe()} resource. "
            "Use external_name() to reference its external name."
        )
        raise ValueError(msg)
    return typing.cast(V, _encode(value._source, list(value._path)))


def external_name(named_resource: typing.Any) -> str:
    """Reference a resource's external name, its identifier outside Kubernetes.

    Args:
        named_resource: A stand-in from named or named_required.

    Returns:
        A marker string that resolve replaces with the external name.

    This is what a provider's own somethingIdRef would have resolved to, so it
    directly replaces a reference or selector field:

        "vpcId": reference.external_name(vpc),  # instead of vpcIdSelector

    Raises:
        TypeError: named_resource isn't a stand-in for a resource.
    """
    if not isinstance(named_resource, _Field) or named_resource._path:
        msg = "external_name() expects a resource from named() or named_required()"
        raise TypeError(msg)
    return _encode(named_resource._source, [_EXTERNAL_NAME])


def _encode(source: _Source, path: list) -> str:
    # JSON rather than a dotted path, because keys like crossplane.io/name
    # contain dots and lists are indexed by number.
    body = json.dumps([*source, path], separators=(",", ":"))
    return f"{PREFIX}{body}{_SUFFIX}"


def _decode(v: typing.Any) -> tuple[_Source, list] | None:
    if not isinstance(v, str) or not v.startswith(PREFIX) or not v.endswith(_SUFFIX):
        return None
    try:
        kind, name, resource_name, namespace, path = json.loads(
            v[len(PREFIX) : -len(_SUFFIX)]
        )
    except (ValueError, TypeError):
        return None
    return _Source(kind, name, resource_name, namespace), path


_MISSING = object()


def _value_at(obj: typing.Any, path: list) -> typing.Any:
    for key in path:
        if isinstance(key, int):
            if not isinstance(obj, list) or not -len(obj) <= key < len(obj):
                return _MISSING
            obj = obj[key]
        elif isinstance(obj, dict) and key in obj:
            obj = obj[key]
        else:
            return _MISSING
    return obj


class _Resolver:
    """Resolves the references in one desired composed resource."""

    def __init__(self, req: fnv1.RunFunctionRequest, name: str):
        self.req = req
        self.name = name
        self.sources: set[_Source] = set()
        self.unresolved = False

        # The dependent as it was last observed, if it exists. A reference
        # whose source has gone falls back to the value the dependent already
        # has, rather than dropping the field and having the apply unset it.
        self.observed = _MISSING
        if name in req.observed.resources:
            self.observed = resource.struct_to_dict(
                req.observed.resources[name].resource
            )

    def source(self, s: _Source) -> typing.Any:
        if s.kind == _COMPOSED:
            if s.name not in self.req.observed.resources:
                return _MISSING
            return resource.struct_to_dict(self.req.observed.resources[s.name].resource)

        items = request.get_required_resources(self.req, s.name)
        if s.resource_name is None:
            # With several matches and nothing to pick by there is no right
            # answer, so decline rather than guess.
            return items[0] if len(items) == 1 else _MISSING
        for item in items:
            meta = item.get("metadata", {})
            if meta.get("name") == s.resource_name and (
                s.namespace is None or meta.get("namespace") == s.namespace
            ):
                return item
        return _MISSING

    def value(self, marker: tuple[_Source, list], at: list) -> typing.Any:
        s, path = marker
        self.sources.add(s)

        src = self.source(s)
        if src is not _MISSING:
            if path == [_EXTERNAL_NAME]:
                v = _value_at(
                    src, ["metadata", "annotations", _EXTERNAL_NAME_ANNOTATION]
                )
                if v is _MISSING:
                    v = _value_at(src, ["metadata", "name"])
            else:
                v = _value_at(src, path)
            if v is not _MISSING and v is not None:
                return v

        kept = _value_at(self.observed, at)
        if kept is not _MISSING:
            return kept

        self.unresolved = True
        return _MISSING

    def walk(self, v: typing.Any, at: list) -> typing.Any:
        marker = _decode(v)
        if marker is not None:
            return self.value(marker, at)
        if isinstance(v, dict):
            out = {}
            for k, child in v.items():
                r = self.walk(child, [*at, k])
                # Leave out a field whose value isn't available yet rather
                # than sending a null a provider would try to apply.
                if r is not _MISSING:
                    out[k] = r
            return out
        if isinstance(v, list):
            walked = (self.walk(child, [*at, i]) for i, child in enumerate(v))
            return [r for r in walked if r is not _MISSING]
        return v


def resolve(req: fnv1.RunFunctionRequest, rsp: fnv1.RunFunctionResponse) -> None:
    """Replace references in desired state, and record the dependencies.

    Args:
        req: The RunFunctionRequest, whose observed and required resources the
            references are resolved against.
        rsp: The RunFunctionResponse to update. Call this once desired state is
            complete, just before returning it.

    Every desired composed resource that refers to another gets a dependency
    on it, alongside any already declared. A reference whose value isn't
    available yet is left out, and the dependency means Crossplane doesn't
    create the resource until it is. A resource that already exists keeps the
    value it has.

    A Crossplane that doesn't advertise CAPABILITY_DEPENDENCIES ignores the
    dependencies, and would create a resource with the field missing. So
    there, a resource with a reference that can't be resolved yet is left out
    of desired state until it can.
    """
    ordered = request.has_capability(req, fnv1.CAPABILITY_DEPENDENCIES)
    declared = {
        (d.resource, d.WhichOneof("depends_on"), _target(d))
        for d in rsp.dependencies.items
    }

    for name in list(rsp.desired.resources):
        r = rsp.desired.resources[name]
        if not _contains_marker(r.resource):
            continue

        resolver = _Resolver(req, name)
        body = resolver.walk(resource.struct_to_dict(r.resource), [])

        if resolver.unresolved and not ordered and resolver.observed is _MISSING:
            del rsp.desired.resources[name]
            continue

        r.resource.CopyFrom(resource.dict_to_struct(body))

        for s in sorted(resolver.sources, key=lambda s: tuple(p or "" for p in s)):
            if s.kind == _COMPOSED:
                if s.name == name or (name, "composed_resource", s.name) in declared:
                    continue
                response.add_dependency(rsp, name, s.name)
                declared.add((name, "composed_resource", s.name))
                continue

            key = (name, "required_resource", (s.name, s.resource_name, s.namespace))
            if key in declared:
                continue
            response.add_required_resource_dependency(
                rsp, name, s.name, name=s.resource_name, namespace=s.namespace
            )
            declared.add(key)


def _target(d: fnv1.Dependency) -> typing.Any:
    if d.WhichOneof("depends_on") == "required_resource":
        r = d.required_resource
        return (
            r.requirement_name,
            r.name if r.HasField("name") else None,
            r.namespace if r.HasField("namespace") else None,
        )
    return d.composed_resource


def unresolved(rsp: fnv1.RunFunctionResponse) -> list[str]:
    """Return the desired composed resources that still contain references.

    Args:
        rsp: The RunFunctionResponse to check.

    Returns:
        The names of desired composed resources with a reference in them,
        which means resolve wasn't called after they were built.
    """
    return [
        name
        for name, r in rsp.desired.resources.items()
        if _contains_marker(r.resource)
    ]


def _contains_marker(s: structpb.Struct) -> bool:
    def walk(v: structpb.Value) -> bool:
        kind = v.WhichOneof("kind")
        if kind == "string_value":
            return v.string_value.startswith(PREFIX)
        if kind == "struct_value":
            return any(walk(x) for x in v.struct_value.fields.values())
        if kind == "list_value":
            return any(walk(x) for x in v.list_value.values)
        return False

    return any(walk(v) for v in s.fields.values())
