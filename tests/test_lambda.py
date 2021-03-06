from __future__ import print_function
from assemyaml.lambda_handler import codepipeline_handler
from boto3.session import Session as Boto3Session
from contextlib import contextmanager
from json import dumps as json_dumps
from moto import mock_s3
from logging import getLogger, WARNING
from os import listdir
from os.path import dirname
from random import randint
from six import BytesIO, iteritems, next, string_types
from six.moves import cStringIO as StringIO, range
from string import ascii_letters, digits
import sys
from testfixtures import LogCapture
from unittest import TestCase
from uuid import uuid4
from yaml import dump as yaml_dump, load as yaml_load
from zipfile import ZipFile


_keyspace = ascii_letters + digits
def random_keyname(length=7):  # noqa: E302
    return "".join([
        _keyspace[randint(0, len(_keyspace) - 1)] for i in range(length)])

@contextmanager
def captured_output():
    new_out, new_err = StringIO(), StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = new_out, new_err
        yield sys.stdout, sys.stderr
    finally:
        sys.stdout, sys.stderr = old_out, old_err

log = getLogger("test_lambda")


@mock_s3
class TestLambda(TestCase):
    def setUp(self):
        self.bucket_name = "codepipeline-us-west-2-00000000000"
        self.pipeline_name = "hello"
        self.boto3 = Boto3Session(region_name="us-west-2")
        for logname in ("botocore", "s3transfer"):
            getLogger(logname).setLevel(WARNING)

    def artifact_dict(self, artifact_name, key):
        """
        tl.artifact_dict(artifact_name, key) -> dict

        Returns a dictionary for an artifact suitable for inclusion in a
        CodePipeline Lambda event. Details on this are here:
        http://docs.aws.amazon.com/codepipeline/latest/userguide/actions-invoke-lambda-function.html
        """
        return {
            "location": {
                "s3Location": {
                    "bucketName": self.bucket_name,
                    "objectKey": key,
                },
                "type": "S3",
            },
            "revision": None,
            "name": artifact_name,
        }

    def create_input_artifact(self, artifact_name, contents):
        # Zip the contents up into an S3 object
        zip_binary = BytesIO()
        with ZipFile(zip_binary, "w") as zip_file:
            for key, value in iteritems(contents):
                zip_file.writestr(key, value)

        s3_key = "%s/%s/%s.zip" % (
            self.pipeline_name, artifact_name, random_keyname())

        s3 = self.boto3.resource("s3", region_name="us-west-2")
        bucket = s3.Bucket(self.bucket_name)
        obj = bucket.Object(s3_key)
        obj.put(Body=zip_binary.getvalue())

        return self.artifact_dict(artifact_name, s3_key)

    def lambda_event(self, input_artifacts, output_artifact,
                     template_document=None,
                     resource_documents=None, default_input_filename=None,
                     local_tags=None, format=None):

        user_params = {}
        if template_document is not None:
            user_params["TemplateDocument"] = template_document

        if resource_documents is not None:
            user_params["ResourceDocuments"] = resource_documents

        if default_input_filename is not None:
            user_params["DefaultInputFilename"] = default_input_filename

        if local_tags is not None:
            user_params["LocalTags"] = local_tags

        if format is not None:
            user_params["Format"] = format

        action_cfg = {"configuration": {"FunctionName": "Lambda"}}
        if user_params:
            action_cfg["configuration"]["UserParameters"] = (
                json_dumps(user_params))

        creds = {
            "secretAccessKey": "",
            "sessionToken": "",
            "accessKeyId": "",
        }

        data = {
            "actionConfiguration": action_cfg,
            "inputArtifacts": input_artifacts,
            "outputArtifacts": [output_artifact],
            "artifactCredentials": creds,
        }

        job = {
            "id": str(uuid4()),
            "accountId": "000000000000",
            "data": data
        }

        test_params = {
            "SkipCodePipeline": True
        }

        return {
            "CodePipeline.job": job,
            "TestParameters": test_params,
        }

    def run_doc(self, filename):
        s3 = self.boto3.resource("s3", region_name="us-west-2")
        bucket = s3.Bucket(self.bucket_name)
        bucket.create()

        filename = dirname(__file__) + "/lambda/" + filename

        with open(filename, "r") as fd:
            log.info("Running Lambda test on %s" % filename)
            doc = yaml_load(fd)
            input_artifacts = []

            for artifact in doc["InputArtifacts"]:
                name = artifact["Name"]
                contents = {}

                for filename, data in iteritems(artifact.get("Files", {})):
                    if isinstance(data, (list, dict)):
                        data = yaml_dump(data)

                    contents[filename] = data

                input_artifacts.append(
                    self.create_input_artifact(name, contents))

            output_artifact = doc["OutputArtifact"]

        output_artifact_name = output_artifact.get("Name", "Output")
        output_artifact_key = "%s/%s/%s.zip" % (
            self.pipeline_name, output_artifact_name, random_keyname())
        output_artifact_dict = self.artifact_dict(
            output_artifact_name, output_artifact_key)

        event = self.lambda_event(
            input_artifacts=input_artifacts,
            output_artifact=output_artifact_dict,
            template_document=doc.get("TemplateDocument"),
            resource_documents=doc.get("ResourceDocuments"),
            default_input_filename=doc.get("DefaultInputFilename"),
            local_tags=doc.get("LocalTags"))

        log.info("Invoking Lambda codepipeline_handler")
        with captured_output() as (out, err):
            with LogCapture() as l:
                codepipeline_handler(event, None)

        # Redisplay the log records
        for record in l.records:
            getLogger(record.name).handle(record)

        log.info("Lambda codepipeline_handler done")


        expected_errors = doc.get("ExpectedErrors")
        if expected_errors:
            err = str(l) + "\n" + err.getvalue()

            if isinstance(expected_errors, string_types):
                self.assertIn(expected_errors, err)
            else:
                for err in expected_errors:
                    self.assertIn(err, err)
        else:
            output_filename, expected_content = next(
                iteritems(output_artifact["Files"]))
            expected_content = yaml_load(expected_content)

            result_obj = s3.Object(self.bucket_name, output_artifact_key)
            result_zip = result_obj.get()["Body"].read()

            with ZipFile(BytesIO(result_zip), "r") as zf:
                with zf.open(output_filename, "r") as fd:
                    result = yaml_load(fd)

            self.assertEquals(result, expected_content)

    def test_lambda_basic_transclude(self):
        self.run_doc("lambda_basic_transclude.yml")

    def test_bad_template(self):
        self.run_doc("bad_template.yml")

    def test_no_template(self):
        self.run_doc("no_template.yml")

    def test_template_parameter1(self):
        self.run_doc("template_parameter1.yml")

    def test_template_parameter2(self):
        self.run_doc("template_parameter2.yml")

    def test_template_parameter3(self):
        self.run_doc("template_parameter2.yml")

    def test_template_parameter4(self):
        self.run_doc("template_parameter2.yml")

    def test_dict_transclude(self):
        self.run_doc("test_dict_transclude.yml")

    def test_null_transclude(self):
        self.run_doc("test_null_transclude.yml")

    def test_omap_transclude(self):
        self.run_doc("test_omap_transclude.yml")

    def test_pairs_transclude(self):
        self.run_doc("test_pairs_transclude.yml")

    def test_scalar_transclude(self):
        self.run_doc("test_scalar_transclude.yml")

    def test_set_transclude(self):
        self.run_doc("test_set_transclude.yml")

    def test_bad_userparams(self):
        event = self.lambda_event([], self.artifact_dict("Output", "key"))
        event["CodePipeline.job"]["data"]["actionConfiguration"]\
            ["configuration"]["UserParameters"] = "{])}"

        with LogCapture() as l:
            codepipeline_handler(event, None)

        self.assertIn("Expecting property name", str(l))

        event = self.lambda_event([], self.artifact_dict("Output", "key"))
        event["CodePipeline.job"]["data"]["actionConfiguration"]\
            ["configuration"]["UserParameters"] = "[]"

        with LogCapture() as l:
            codepipeline_handler(event, None)

        self.assertIn("Expected a JSON object for user parameters", str(l))

    def test_unknown_artifacttype(self):
        event = self.lambda_event(
            [self.artifact_dict("Input", "missing")],
            self.artifact_dict("Output", "key"))

        event["CodePipeline.job"]["data"]["inputArtifacts"][0]["location"]\
            ["type"] = "ftp"

        with LogCapture() as l:
            codepipeline_handler(event, None)

        self.assertIn("Can't handle input artifact type ftp", str(l))

    def test_missing_artifact(self):
        event = self.lambda_event(
            [self.artifact_dict("Input", "missing")],
            self.artifact_dict("Output", "key"))

        with LogCapture() as l:
            codepipeline_handler(event, None)

        self.assertIn(
            "Unable to download input artifact 'Input' (s3://" +
            self.bucket_name + "/missing):", str(l))

    def test_bad_artifact_filename(self):
        event = self.lambda_event(
            [self.artifact_dict("Input", "missing")],
            self.artifact_dict("Output", "key"),
            template_document="qwertyuiop")

        with LogCapture() as l:
            codepipeline_handler(event, None)

        self.assertIn(
            "Invalid value for TemplateDocument: expected input_artifact::"
            "filename: qwertyuiop", str(l))

    def test_unknown_artifact_parameter(self):
        event = self.lambda_event(
            [self.artifact_dict("Input", "missing")],
            self.artifact_dict("Output", "key"),
            template_document="Foo::bar")

        with LogCapture() as l:
            codepipeline_handler(event, None)

        self.assertIn(
            "Invalid value for TemplateDocument: unknown input artifact Foo",
            str(l))

    def test_invalid_format(self):
        event = self.lambda_event(
            [],
            self.artifact_dict("Output", "key"),
            format="qwerty")

        with LogCapture() as l:
            codepipeline_handler(event, None)

        self.assertIn(
            "Invalid output format 'qwerty': valid types are 'json' and 'yaml'",
            str(l))
