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

import asyncio
import unittest

import pydantic
from google.protobuf import json_format

from crossplane.function import logging, reference, resource, response, runtime
from crossplane.function.proto.v1 import run_function_pb2 as fnv1

# Models shaped the way datamodel-codegen generates them from CRDs: JSON names
# as attribute names, and an alias only where the JSON name is a Python
# keyword.


class Metadata(pydantic.BaseModel):
    name: str | None = None
    annotations: dict[str, str] | None = None


class AtProvider(pydantic.BaseModel):
    id: str | None = None
    arn: str | None = None


class VPCStatus(pydantic.BaseModel):
    atProvider: AtProvider | None = None  # noqa: N815  # Generated models use JSON names.


class VPC(pydantic.BaseModel):
    metadata: Metadata | None = None
    status: VPCStatus | None = None


class Range(pydantic.BaseModel):
    from_: str | None = pydantic.Field(None, alias="from")


class Pool(pydantic.BaseModel):
    ranges: list[Range] | None = None


class SubnetForProvider(pydantic.BaseModel):
    region: str | None = None
    vpcId: str | None = None  # noqa: N815  # Generated models use JSON names.
    tags: list[str] | None = None


class SubnetSpec(pydantic.BaseModel):
    forProvider: SubnetForProvider  # noqa: N815  # Generated models use JSON names.


class Subnet(pydantic.BaseModel):
    apiVersion: str = "ec2.aws.m.upbound.io/v1beta1"  # noqa: N815  # Generated models use JSON names.
    kind: str = "Subnet"
    spec: SubnetSpec


def observed(**resources: dict) -> fnv1.State:
    return fnv1.State(
        resources={
            name: fnv1.Resource(resource=resource.dict_to_struct(body))
            for name, body in resources.items()
        }
    )


VPC_OBSERVED = {
    "apiVersion": "ec2.aws.m.upbound.io/v1beta1",
    "kind": "VPC",
    "metadata": {
        "name": "xr-vpc-abc12",
        "annotations": {"crossplane.io/external-name": "vpc-0123"},
    },
    "status": {"atProvider": {"id": "vpc-0123", "arn": "arn:aws:ec2:vpc/vpc-0123"}},
}

ORDERED = fnv1.RequestMeta(
    capabilities=[fnv1.CAPABILITY_CAPABILITIES, fnv1.CAPABILITY_DEPENDENCIES]
)
UNORDERED = fnv1.RequestMeta(capabilities=[fnv1.CAPABILITY_CAPABILITIES])


def desired_subnet(rsp: fnv1.RunFunctionResponse, vpc_id: str) -> None:
    subnet = Subnet(
        spec=SubnetSpec(forProvider=SubnetForProvider(region="us-east-1", vpcId=vpc_id))
    )
    resource.update(rsp.desired.resources["subnet"], subnet)


def edges(rsp: fnv1.RunFunctionResponse) -> list[dict]:
    return [
        json_format.MessageToDict(d, preserving_proto_field_name=True)
        for d in rsp.dependencies.items
    ]


def body(rsp: fnv1.RunFunctionResponse, name: str) -> dict:
    return resource.struct_to_dict(rsp.desired.resources[name].resource)


class TestNamed(unittest.TestCase):
    def test_follows_model_fields(self) -> None:
        vpc = reference.named("vpc", VPC)
        marker = reference.ref(vpc.status.atProvider.arn)
        self.assertTrue(marker.startswith(reference.PREFIX))
        self.assertIn('["status","atProvider","arn"]', marker)

    def test_typo_fails_where_it_is_written(self) -> None:
        vpc = reference.named("vpc", VPC)
        with self.assertRaisesRegex(
            AttributeError, "vpc.status has no field 'atprovider'"
        ):
            _ = vpc.status.atprovider

    def test_keyword_field_is_recorded_under_its_json_name(self) -> None:
        pool = reference.named("pool", Pool)
        for field in (pool.ranges[0].from_, pool.ranges[0]["from"]):
            self.assertIn('["ranges",0,"from"]', reference.ref(field))

    def test_json_name_is_accepted_too(self) -> None:
        pool = reference.named("pool", Pool)
        self.assertIn('"from"', reference.ref(getattr(pool.ranges[0], "from")))

    def test_untyped_records_what_it_is_given(self) -> None:
        vpc = reference.named("vpc")
        marker = reference.ref(vpc.metadata.annotations["crossplane.io/name"])
        self.assertIn('["metadata","annotations","crossplane.io/name"]', marker)

    def test_has_no_value(self) -> None:
        vpc = reference.named("vpc", VPC)
        field = vpc.status.atProvider.id
        with self.assertRaisesRegex(TypeError, "has no value yet"):
            bool(field)
        with self.assertRaisesRegex(TypeError, "has no value yet"):
            str(field)
        with self.assertRaises(TypeError):
            iter(field)
        with self.assertRaises(AttributeError):
            field.id = "x"

    def test_ref_rejects_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "expects a field"):
            reference.ref("vpc-0123")

    def test_ref_rejects_whole_resource(self) -> None:
        with self.assertRaisesRegex(ValueError, "external_name"):
            reference.ref(reference.named("vpc", VPC))

    def test_external_name_rejects_fields(self) -> None:
        vpc = reference.named("vpc", VPC)
        with self.assertRaises(TypeError):
            reference.external_name(vpc.status)

    def test_marker_survives_model_validation(self) -> None:
        vpc = reference.named("vpc", VPC)
        marker = reference.ref(vpc.status.atProvider.id)
        subnet = SubnetForProvider(vpcId=marker)
        self.assertEqual(subnet.vpcId, marker)

    def test_forgetting_ref_fails_validation(self) -> None:
        vpc = reference.named("vpc", VPC)
        with self.assertRaises(pydantic.ValidationError):
            SubnetForProvider(vpcId=vpc.status.atProvider.id)


class TestResolve(unittest.TestCase):
    def setUp(self) -> None:
        logging.configure(level=logging.Level.DISABLED)

    def test_substitutes_and_records_edge(self) -> None:
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=VPC_OBSERVED))
        rsp = response.to(req)
        vpc = reference.named("vpc", VPC)
        desired_subnet(rsp, reference.ref(vpc.status.atProvider.id))

        reference.resolve(req, rsp)

        self.assertEqual(
            body(rsp, "subnet")["spec"]["forProvider"]["vpcId"], "vpc-0123"
        )
        self.assertEqual(
            edges(rsp), [{"resource": "subnet", "composed_resource": "vpc"}]
        )

    def test_external_name(self) -> None:
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=VPC_OBSERVED))
        rsp = response.to(req)
        desired_subnet(rsp, reference.external_name(reference.named("vpc")))

        reference.resolve(req, rsp)

        self.assertEqual(
            body(rsp, "subnet")["spec"]["forProvider"]["vpcId"], "vpc-0123"
        )

    def test_external_name_falls_back_to_name(self) -> None:
        vpc = {"metadata": {"name": "xr-vpc-abc12"}}
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=vpc))
        rsp = response.to(req)
        desired_subnet(rsp, reference.external_name(reference.named("vpc")))

        reference.resolve(req, rsp)

        self.assertEqual(
            body(rsp, "subnet")["spec"]["forProvider"]["vpcId"], "xr-vpc-abc12"
        )

    def test_missing_source_drops_field_and_keeps_edge(self) -> None:
        req = fnv1.RunFunctionRequest(meta=ORDERED)
        rsp = response.to(req)
        vpc = reference.named("vpc", VPC)
        desired_subnet(rsp, reference.ref(vpc.status.atProvider.id))

        reference.resolve(req, rsp)

        self.assertEqual(
            body(rsp, "subnet")["spec"]["forProvider"], {"region": "us-east-1"}
        )
        self.assertEqual(
            edges(rsp), [{"resource": "subnet", "composed_resource": "vpc"}]
        )

    def test_missing_field_on_existing_source_drops_field(self) -> None:
        vpc = {"metadata": {"name": "xr-vpc-abc12"}}  # No status yet.
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=vpc))
        rsp = response.to(req)
        desired_subnet(
            rsp, reference.ref(reference.named("vpc", VPC).status.atProvider.id)
        )

        reference.resolve(req, rsp)

        self.assertNotIn("vpcId", body(rsp, "subnet")["spec"]["forProvider"])

    def test_existing_dependent_keeps_its_value(self) -> None:
        subnet = {"spec": {"forProvider": {"region": "us-east-1", "vpcId": "vpc-0123"}}}
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(subnet=subnet))
        rsp = response.to(req)
        desired_subnet(
            rsp, reference.ref(reference.named("vpc", VPC).status.atProvider.id)
        )

        reference.resolve(req, rsp)

        self.assertEqual(
            body(rsp, "subnet")["spec"]["forProvider"]["vpcId"], "vpc-0123"
        )

    def test_list_elements(self) -> None:
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=VPC_OBSERVED))
        rsp = response.to(req)
        vpc = reference.named("vpc", VPC)
        gone = reference.named("gone", VPC)
        resource.update(
            rsp.desired.resources["subnet"],
            {
                "spec": {
                    "forProvider": {
                        "tags": [
                            "static",
                            reference.ref(vpc.status.atProvider.arn),
                            reference.ref(gone.status.atProvider.arn),
                        ]
                    }
                }
            },
        )

        reference.resolve(req, rsp)

        self.assertEqual(
            body(rsp, "subnet")["spec"]["forProvider"]["tags"],
            ["static", "arn:aws:ec2:vpc/vpc-0123"],
        )
        self.assertEqual(
            edges(rsp),
            [
                {"resource": "subnet", "composed_resource": "gone"},
                {"resource": "subnet", "composed_resource": "vpc"},
            ],
        )

    def test_does_not_duplicate_declared_edges(self) -> None:
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=VPC_OBSERVED))
        rsp = response.to(req)
        response.add_dependency(rsp, "subnet", "vpc")
        vpc = reference.named("vpc", VPC)
        resource.update(
            rsp.desired.resources["subnet"],
            {
                "a": reference.ref(vpc.status.atProvider.id),
                "b": reference.ref(vpc.status.atProvider.arn),
            },
        )

        reference.resolve(req, rsp)

        self.assertEqual(
            edges(rsp), [{"resource": "subnet", "composed_resource": "vpc"}]
        )

    def test_self_reference_records_no_edge(self) -> None:
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=VPC_OBSERVED))
        rsp = response.to(req)
        vpc = reference.named("vpc", VPC)
        resource.update(
            rsp.desired.resources["vpc"],
            {"tag": reference.ref(vpc.status.atProvider.id)},
        )

        reference.resolve(req, rsp)

        self.assertEqual(edges(rsp), [])

    def test_without_capability_holds_back_by_omission(self) -> None:
        req = fnv1.RunFunctionRequest(meta=UNORDERED)
        rsp = response.to(req)
        desired_subnet(
            rsp, reference.ref(reference.named("vpc", VPC).status.atProvider.id)
        )

        reference.resolve(req, rsp)

        self.assertNotIn("subnet", rsp.desired.resources)

    def test_without_capability_resolved_resources_stay(self) -> None:
        req = fnv1.RunFunctionRequest(
            meta=UNORDERED, observed=observed(vpc=VPC_OBSERVED)
        )
        rsp = response.to(req)
        desired_subnet(
            rsp, reference.ref(reference.named("vpc", VPC).status.atProvider.id)
        )

        reference.resolve(req, rsp)

        self.assertEqual(
            body(rsp, "subnet")["spec"]["forProvider"]["vpcId"], "vpc-0123"
        )

    def test_leaves_resources_without_references_alone(self) -> None:
        req = fnv1.RunFunctionRequest(meta=ORDERED)
        rsp = response.to(req)
        resource.update(rsp.desired.resources["plain"], {"spec": {"a": 1}})
        before = rsp.SerializeToString()

        reference.resolve(req, rsp)

        self.assertEqual(rsp.SerializeToString(), before)


class TestResolveRequired(unittest.TestCase):
    def setUp(self) -> None:
        logging.configure(level=logging.Level.DISABLED)

    def req(self, *items: dict) -> fnv1.RunFunctionRequest:
        return fnv1.RunFunctionRequest(
            meta=ORDERED,
            required_resources={
                "dbs": fnv1.Resources(
                    items=[
                        fnv1.Resource(resource=resource.dict_to_struct(i))
                        for i in items
                    ]
                )
            },
        )

    def cm(self, rsp: fnv1.RunFunctionResponse, value: str) -> None:
        resource.update(
            rsp.desired.resources["app-config"], {"data": {"DB_HOST": value}}
        )

    def test_single_match(self) -> None:
        req = self.req({"metadata": {"name": "a"}, "status": {"address": "db.a"}})
        rsp = response.to(req)
        self.cm(rsp, reference.ref(reference.named_required("dbs").status.address))

        reference.resolve(req, rsp)

        self.assertEqual(body(rsp, "app-config")["data"]["DB_HOST"], "db.a")
        self.assertEqual(
            edges(rsp),
            [
                {
                    "resource": "app-config",
                    "required_resource": {"requirement_name": "dbs"},
                }
            ],
        )

    def test_named_match(self) -> None:
        req = self.req(
            {
                "metadata": {"name": "a", "namespace": "x"},
                "status": {"address": "db.xa"},
            },
            {
                "metadata": {"name": "a", "namespace": "y"},
                "status": {"address": "db.ya"},
            },
        )
        rsp = response.to(req)
        db = reference.named_required("dbs", name="a", namespace="y")
        self.cm(rsp, reference.ref(db.status.address))

        reference.resolve(req, rsp)

        self.assertEqual(body(rsp, "app-config")["data"]["DB_HOST"], "db.ya")
        self.assertEqual(
            edges(rsp)[0]["required_resource"],
            {"requirement_name": "dbs", "name": "a", "namespace": "y"},
        )

    def test_declines_to_guess_between_matches(self) -> None:
        req = self.req(
            {"metadata": {"name": "a"}, "status": {"address": "db.a"}},
            {"metadata": {"name": "b"}, "status": {"address": "db.b"}},
        )
        rsp = response.to(req)
        self.cm(rsp, reference.ref(reference.named_required("dbs").status.address))

        reference.resolve(req, rsp)

        self.assertEqual(body(rsp, "app-config"), {"data": {}})


class TestGuard(unittest.TestCase):
    def setUp(self) -> None:
        logging.configure(level=logging.Level.DISABLED)

    def test_unresolved(self) -> None:
        rsp = fnv1.RunFunctionResponse()
        desired_subnet(
            rsp, reference.ref(reference.named("vpc", VPC).status.atProvider.id)
        )
        resource.update(rsp.desired.resources["plain"], {"a": "b"})

        self.assertEqual(reference.unresolved(rsp), ["subnet"])

    def test_runtime_fails_a_forgotten_resolve(self) -> None:
        class Forgetful:
            async def RunFunction(self, req, _context):  # noqa: N802
                rsp = response.to(req)
                vpc = reference.named("vpc", VPC)
                desired_subnet(rsp, reference.ref(vpc.status.atProvider.id))
                return rsp

        guard = runtime.ReferenceGuard(wrapped=Forgetful())
        rsp = asyncio.run(guard.RunFunction(fnv1.RunFunctionRequest(), None))

        self.assertEqual(len(rsp.results), 1)
        self.assertEqual(rsp.results[0].severity, fnv1.SEVERITY_FATAL)
        self.assertIn("reference.resolve", rsp.results[0].message)

    def test_runtime_passes_a_resolved_response(self) -> None:
        class Careful:
            async def RunFunction(self, req, _context):  # noqa: N802
                rsp = response.to(req)
                vpc = reference.named("vpc", VPC)
                desired_subnet(rsp, reference.ref(vpc.status.atProvider.id))
                reference.resolve(req, rsp)
                return rsp

        guard = runtime.ReferenceGuard(wrapped=Careful())
        req = fnv1.RunFunctionRequest(meta=ORDERED, observed=observed(vpc=VPC_OBSERVED))
        rsp = asyncio.run(guard.RunFunction(req, None))

        self.assertEqual(len(rsp.results), 0)


if __name__ == "__main__":
    unittest.main()
