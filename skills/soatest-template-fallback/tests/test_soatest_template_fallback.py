import importlib.util
import sys
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "soatest_template_fallback.py"
spec = importlib.util.spec_from_file_location("soatest_template_fallback_impl", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class AwsParsingTests(unittest.TestCase):
    def test_parse_awsv4_auth_object(self):
        parsed = mod.parse_auth_object(
            {
                "type": "awsv4",
                "awsv4": {
                    "accessKey": "{{AWS_ACCESS_KEY}}",
                    "secretKey": "{{AWS_SECRET_KEY}}",
                    "sessionToken": "{{AWS_SESSION_TOKEN}}",
                    "service": "ec2",
                    "region": "{{AWS_REGION}}",
                    "addAuthDataToRequest": "queryParams",
                },
            }
        )
        self.assertEqual("awsv4", parsed.mode)
        self.assertEqual("${AWS_ACCESS_KEY}", parsed.access_key_id)
        self.assertEqual("${AWS_SECRET_KEY}", parsed.secret_access_key)
        self.assertEqual("${AWS_SESSION_TOKEN}", parsed.session_token)
        self.assertEqual("ec2", parsed.service)
        self.assertEqual("${AWS_REGION}", parsed.region)
        self.assertEqual(1, parsed.aws_auth_mode)


class AwsExpectationTests(unittest.TestCase):
    def test_build_expectations_deduplicates_same_awsv4_profile(self):
        targets = [
            mod.RequestTarget(
                folder_path=(),
                request_name="A",
                effective_auth=mod.AuthSpec(
                    mode="awsv4",
                    access_key_id="${AWS_ACCESS_KEY}",
                    secret_access_key="${AWS_SECRET_KEY}",
                    service="ec2",
                    region="${AWS_REGION}",
                    aws_auth_mode=1,
                ),
            ),
            mod.RequestTarget(
                folder_path=(),
                request_name="B",
                effective_auth=mod.AuthSpec(
                    mode="awsv4",
                    access_key_id="${AWS_ACCESS_KEY}",
                    secret_access_key="${AWS_SECRET_KEY}",
                    service="ec2",
                    region="${AWS_REGION}",
                    aws_auth_mode=1,
                ),
            ),
        ]
        expectations, profiles = mod.build_expectations(targets, {})
        self.assertEqual(2, len(expectations))
        self.assertEqual(1, len(profiles))
        self.assertEqual(expectations[0].auth_profile_name, expectations[1].auth_profile_name)
        self.assertEqual("awsv4", profiles[0].mode)

    def test_render_awsv4_profile_includes_query_mode(self):
        profile = mod.AuthProfile(
            mode="awsv4",
            name="AWS Signature Query",
            username="",
            password="",
            access_key_id="${AWS_ACCESS_KEY}",
            secret_access_key="${AWS_SECRET_KEY}",
            session_token="${AWS_SESSION_TOKEN}",
            service="ec2",
            region="${AWS_REGION}",
            aws_auth_mode=1,
        )
        rendered = mod.render_auth_profile(profile)
        self.assertIn("$type: AwsSignatureAuthentication", rendered)
        self.assertIn("name: AWS Signature Query", rendered)
        self.assertIn("authMode: 1", rendered)
        self.assertIn("value: \"${AWS_ACCESS_KEY}\"", rendered)
        self.assertIn("secretAccessKey:", rendered)
        self.assertIn("value: \"${AWS_SECRET_KEY}\"", rendered)


class AwsRestClientPatchTests(unittest.TestCase):
    def test_patch_auth_in_rest_client_block_sets_auth_name_for_awsv4(self):
        block = (
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      transportProperties:\n"
            "        manager:\n"
            "          protocol: 1\n"
            "          properties:\n"
            "          - $type: HTTPClientHTTPProperties\n"
            "            common:\n"
            "              method:\n"
            "                fixedValue:\n"
            "                  method: GET\n"
        )
        expectation = mod.RequestExpectation(
            request_name="A",
            mode="awsv4",
            auth_profile_name="AWS Signature Query",
        )
        updated, changed, matched = mod.patch_auth_in_rest_client_block(block, expectation)
        self.assertTrue(matched)
        self.assertTrue(changed)
        self.assertIn("authName: AWS Signature Query", updated)
        self.assertNotIn("customType: 1", updated)


class DifferenceAssertionTemplateTests(unittest.TestCase):
    def test_load_difference_assertion_templates_extracts_blocks(self):
        template = (
            "suite:\n"
            "  $type: TestSuite\n"
            "  tests:\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Numeric Difference\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: JSON Assertor\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            name: XML Assertor\n"
            "            assertions:\n"
            "              - $type: DifferenceAssertion\n"
            "                name: Numeric Difference Assertion\n"
            "                Assertion_XPath: /root/args/left\n"
            "                data:\n"
            "                  $type: NumericDifference\n"
            "                  values:\n"
            "                    - name: Difference Value\n"
            "                      value:\n"
            "                        fixedValue:\n"
            "                          $type: StringTestValue\n"
            "                          value: 3\n"
            "                  base:\n"
            "                    name: Base Value\n"
            "                    value:\n"
            "                      fixedValue:\n"
            "                        $type: StringTestValue\n"
            "                        value: 7\n"
            "            message:\n"
            "              $type: ExpectedXMLMessage\n"
            "              message: true\n"
            "      conversionStrategy:\n"
            "        dataFormatName: JSON\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Date Difference Assertion\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: JSON Assertor\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            name: XML Assertor\n"
            "            assertions:\n"
            "              - $type: DifferenceAssertion\n"
            "                name: Date Difference Assertion\n"
            "                Assertion_XPath: /root/args/endDate\n"
            "                data:\n"
            "                  $type: DateDifference\n"
            "            message:\n"
            "              $type: ExpectedXMLMessage\n"
            "              message: true\n"
            "      conversionStrategy:\n"
            "        dataFormatName: JSON\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Date Time Difference Assertion\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: JSON Assertor\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            name: XML Assertor\n"
            "            assertions:\n"
            "              - $type: DifferenceAssertion\n"
            "                name: DateTime Difference Assertion\n"
            "                Assertion_XPath: /root/args/endTs\n"
            "                data:\n"
            "                  $type: DateTimeDifference\n"
            "            message:\n"
            "              $type: ExpectedXMLMessage\n"
            "              message: true\n"
            "      conversionStrategy:\n"
            "        dataFormatName: JSON\n"
        )
        templates = mod.load_difference_assertion_templates(template)
        self.assertEqual(3, len(templates))
        self.assertIn("Numeric Difference", templates)
        self.assertTrue(any("$type: NumericDifference" in item for item in templates["Numeric Difference"].assertion_items))


class DifferenceAssertionPatchTests(unittest.TestCase):
    def test_patch_difference_assertions_inserts_into_existing_xml_assertor(self):
        template = (
            "suite:\n"
            "  tests:\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Numeric Difference\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: JSON Assertor\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            errorsOutput:\n"
            "              name: Errors\n"
            "            assertions:\n"
              "              - $type: DifferenceAssertion\n"
            "                name: Numeric Difference Assertion\n"
            "                Assertion_XPath: /root/args/left\n"
            "                data:\n"
            "                  $type: NumericDifference\n"
            "            message:\n"
            "              $type: ExpectedXMLMessage\n"
            "              message: true\n"
            "      conversionStrategy:\n"
            "        dataFormatName: JSON\n"
        )
        templates = mod.load_difference_assertion_templates(template)
        target = (
            "suite:\n"
            "  tests:\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Numeric Difference\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: Postman Script Assertor 1\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            assertions:\n"
            "              - $type: NumericAssertion\n"
            "                name: Status\n"
            "            message:\n"
            "              $type: ExpectedXMLMessage\n"
            "              message: true\n"
            "      conversionStrategy:\n"
            "        dataFormatName: JSON\n"
        )
        patched, added, already, missing, warnings = mod.patch_difference_assertions(target, templates)
        self.assertEqual(1, added)
        self.assertEqual(0, already)
        self.assertEqual(0, missing)
        self.assertFalse(warnings)
        self.assertIn("$type: DifferenceAssertion", patched)
        self.assertIn("$type: NumericDifference", patched)
        self.assertNotIn("errorsOutput:", patched)
        self.assertIn("schemaVersion: 13", patched)

    def test_patch_difference_assertions_injects_generic_tool_when_no_assertor_exists(self):
        template = (
            "suite:\n"
            "  tests:\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Numeric Difference\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: JSON Assertor\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            assertions:\n"
            "              - $type: DifferenceAssertion\n"
            "                name: Numeric Difference Assertion\n"
            "                Assertion_XPath: /root/args/left\n"
            "                data:\n"
            "                  $type: NumericDifference\n"
            "            message:\n"
            "              $type: ExpectedXMLMessage\n"
            "              message: true\n"
            "      conversionStrategy:\n"
            "        dataFormatName: JSON\n"
        )
        templates = mod.load_difference_assertion_templates(template)
        target = (
            "suite:\n"
            "  tests:\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Numeric Difference\n"
            "      outputProviders:\n"
            "        objectOutput:\n"
            "          $type: ObjectOutputProvider\n"
            "          outputTools:\n"
            "            - $type: TrafficViewer\n"
            "              name: Traffic Viewer\n"
            "      conversionStrategy:\n"
            "        dataFormatName: JSON\n"
        )
        patched, added, already, missing, warnings = mod.patch_difference_assertions(target, templates)
        self.assertEqual(1, added)
        self.assertEqual(0, already)
        self.assertEqual(0, missing)
        self.assertFalse(warnings)
        self.assertIn("outputTools:", patched)
        self.assertIn("name: JSON Assertor", patched)
        self.assertIn("$type: DifferenceAssertion", patched)

    def test_patch_difference_assertions_updates_endpoint_and_url_parameters_from_template(self):
        template = (
            "suite:\n"
            "  tests:\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Date Difference Assertion\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: JSON Assertor\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            assertions:\n"
            "              - $type: DifferenceAssertion\n"
            "                name: Date Difference Assertion\n"
            "                Assertion_XPath: /root/args/endDate\n"
            "                data:\n"
            "                  $type: DateDifference\n"
            "            message:\n"
            "              $type: ExpectedXMLMessage\n"
            "              message: true\n"
            "      router:\n"
            "        fixedValue:\n"
            "          HTTPClient_Endpoint: https://postman-echo.com/get?startDate=2026-07-10&endDate=2026-07-15\n"
            "      urlParameters:\n"
            "        properties:\n"
            "        - name: startDate\n"
            "          value:\n"
            "            fixedValue:\n"
            "              value: 2026-07-10\n"
            "        - name: endDate\n"
            "          value:\n"
            "            fixedValue:\n"
            "              value: 2026-07-15\n"
            "      transportProperties:\n"
            "        manager:\n"
            "          protocol: 1\n"
        )
        templates = mod.load_difference_assertion_templates(template)
        target = (
            "suite:\n"
            "  tests:\n"
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      iconName: RESTClient\n"
            "      name: Date Difference Assertion\n"
            "      outputTools:\n"
            "        - $type: GenericAssertionTool\n"
            "          name: Postman Script Assertor 1\n"
            "          wrappedTool:\n"
            "            $type: XMLAssertionTool\n"
            "            assertions:\n"
            "              - $type: NumericAssertion\n"
            "                name: Status\n"
            "      router:\n"
            "        fixedValue:\n"
            "          HTTPClient_Endpoint: https://postman-echo.com/get\n"
            "      transportProperties:\n"
            "        manager:\n"
            "          protocol: 1\n"
        )
        patched, added, already, missing, warnings = mod.patch_difference_assertions(target, templates)
        self.assertEqual(1, added)
        self.assertEqual(0, already)
        self.assertEqual(0, missing)
        self.assertIn("HTTPClient_Endpoint: https://postman-echo.com/get?startDate=2026-07-10&endDate=2026-07-15", patched)
        self.assertIn("urlParameters:", patched)
        self.assertIn("name: startDate", patched)
        self.assertIn("name: endDate", patched)
        self.assertTrue(any("Updated endpoint/query mapping from template" in warning for warning in warnings))


class OAuthParsingTests(unittest.TestCase):
    def test_parse_oauth2_auth_object(self):
        parsed = mod.parse_auth_object(
            {
                "type": "oauth2",
                "oauth2": {
                    "accessTokenUrl": "{{KEYCLOAK_TOKEN_URL}}",
                    "clientId": "{{CLIENT_ID}}",
                    "clientSecret": "{{CLIENT_SECRET}}",
                    "grant_type": "client_credentials",
                    "addTokenTo": "queryParams",
                    "scope": "read:hello",
                    "audience": "demo-api",
                    "accessToken": "bad",
                    "tokenType": "Bearer",
                },
            }
        )
        self.assertEqual("oauth2", parsed.mode)
        self.assertEqual("${KEYCLOAK_TOKEN_URL}", parsed.oauth_token_url)
        self.assertEqual("${CLIENT_ID}", parsed.oauth_client_id)
        self.assertEqual("${CLIENT_SECRET}", parsed.oauth_client_secret)
        self.assertEqual("client_credentials", parsed.oauth_grant_type)
        self.assertEqual("queryParams", parsed.oauth_add_token_to)
        self.assertEqual("read:hello", parsed.oauth_scope)
        self.assertEqual("demo-api", parsed.oauth_audience)
        self.assertEqual("bad", parsed.oauth_access_token)
        self.assertEqual("Bearer", parsed.oauth_header_prefix)

    def test_oauth2_inheritance_keeps_parent_add_token_to_when_missing(self):
        parent = mod.AuthSpec(
            mode="oauth2",
            oauth_token_url="${KEYCLOAK_TOKEN_URL}",
            oauth_client_id="${CLIENT_ID}",
            oauth_client_secret="${CLIENT_SECRET}",
            oauth_grant_type="client_credentials",
            oauth_add_token_to="queryParams",
        )
        child_raw = mod.parse_auth_object(
            {
                "type": "oauth2",
                "oauth2": {
                    "clientSecret": "{{BAD_CLIENT_SECRET}}",
                },
            }
        )
        effective = mod.resolve_effective_auth(child_raw, parent)
        self.assertEqual("oauth2", effective.mode)
        self.assertEqual("${BAD_CLIENT_SECRET}", effective.oauth_client_secret)
        self.assertEqual("queryParams", effective.oauth_add_token_to)


class OAuthExpectationTests(unittest.TestCase):
    def test_build_expectations_deduplicates_same_oauth2_profile(self):
        targets = [
            mod.RequestTarget(
                folder_path=(),
                request_name="A",
                effective_auth=mod.AuthSpec(
                    mode="oauth2",
                    oauth_token_url="${KEYCLOAK_TOKEN_URL}",
                    oauth_client_id="${CLIENT_ID}",
                    oauth_client_secret="${CLIENT_SECRET}",
                    oauth_grant_type="client_credentials",
                    oauth_add_token_to="header",
                ),
            ),
            mod.RequestTarget(
                folder_path=(),
                request_name="B",
                effective_auth=mod.AuthSpec(
                    mode="oauth2",
                    oauth_token_url="${KEYCLOAK_TOKEN_URL}",
                    oauth_client_id="${CLIENT_ID}",
                    oauth_client_secret="${CLIENT_SECRET}",
                    oauth_grant_type="client_credentials",
                    oauth_add_token_to="header",
                ),
            ),
        ]

        expectations, profiles = mod.build_expectations(targets, {})
        self.assertEqual(2, len(expectations))
        self.assertEqual(1, len(profiles))
        self.assertEqual(expectations[0].auth_profile_name, expectations[1].auth_profile_name)
        self.assertEqual("oauth2", profiles[0].mode)

    def test_build_expectations_keeps_same_profile_for_oauth2_access_token_override(self):
        targets = [
            mod.RequestTarget(
                folder_path=(),
                request_name="A",
                effective_auth=mod.AuthSpec(
                    mode="oauth2",
                    oauth_token_url="${KEYCLOAK_TOKEN_URL}",
                    oauth_client_id="${CLIENT_ID}",
                    oauth_client_secret="${CLIENT_SECRET}",
                    oauth_grant_type="client_credentials",
                    oauth_add_token_to="header",
                ),
            ),
            mod.RequestTarget(
                folder_path=(),
                request_name="D",
                effective_auth=mod.AuthSpec(
                    mode="oauth2",
                    oauth_token_url="${KEYCLOAK_TOKEN_URL}",
                    oauth_client_id="${CLIENT_ID}",
                    oauth_client_secret="${CLIENT_SECRET}",
                    oauth_grant_type="client_credentials",
                    oauth_add_token_to="header",
                    oauth_access_token="${BAD_TOKEN}",
                    oauth_header_prefix="Bearer",
                ),
            ),
        ]

        expectations, profiles = mod.build_expectations(targets, {})
        self.assertEqual(2, len(expectations))
        self.assertEqual(1, len(profiles))
        self.assertEqual(expectations[0].auth_profile_name, expectations[1].auth_profile_name)
        self.assertEqual("OAuth 2.0", expectations[0].auth_profile_name)
        self.assertEqual("${BAD_TOKEN}", expectations[1].oauth_access_token)

    def test_render_oauth2_profile_includes_query_token_mode(self):
        profile = mod.AuthProfile(
            mode="oauth2",
            name="OAuth 2.0 query param",
            username="",
            password="",
            oauth_token_url="${KEYCLOAK_TOKEN_URL}",
            oauth_client_id="${CLIENT_ID}",
            oauth_client_secret="${CLIENT_SECRET}",
            oauth_grant_type="client_credentials",
            oauth_add_token_to="queryParams",
            oauth_scope="read:hello",
            oauth_audience="demo-api",
        )
        rendered = mod.render_auth_profile(profile)
        self.assertIn("$type: OAuth2Authentication", rendered)
        self.assertIn("name: OAuth 2.0 query param", rendered)
        self.assertIn("grantType: 2", rendered)
        self.assertIn('tokenURI: "${KEYCLOAK_TOKEN_URL}"', rendered)
        self.assertIn('clientID: "${CLIENT_ID}"', rendered)
        self.assertIn('clientSecret: "${CLIENT_SECRET}"', rendered)
        self.assertNotIn("OAuth2CallBackURLValue", rendered)
        self.assertIn("scope:", rendered)
        self.assertIn("audience:", rendered)
        self.assertIn("accessToken: 1", rendered)


class OAuthRestClientPatchTests(unittest.TestCase):
    def test_patch_auth_in_rest_client_block_sets_auth_name_for_oauth2(self):
        block = (
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      transportProperties:\n"
            "        manager:\n"
            "          protocol: 1\n"
            "          properties:\n"
            "          - $type: HTTPClientHTTPProperties\n"
            "            common:\n"
            "              method:\n"
            "                fixedValue:\n"
            "                  method: GET\n"
        )
        expectation = mod.RequestExpectation(
            request_name="A",
            mode="oauth2",
            auth_profile_name="OAuth 2.0",
        )
        updated, changed, matched = mod.patch_auth_in_rest_client_block(block, expectation)
        self.assertTrue(matched)
        self.assertTrue(changed)
        self.assertIn("authName: OAuth 2.0", updated)
        self.assertNotIn("customType: 1", updated)

    def test_patch_auth_in_rest_client_block_injects_bearer_header_when_access_token_present(self):
        block = (
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      transportProperties:\n"
            "        manager:\n"
            "          protocol: 1\n"
            "          properties:\n"
            "          - $type: HTTPClientHTTPProperties\n"
            "            common:\n"
            "              method:\n"
            "                fixedValue:\n"
            "                  method: GET\n"
            "              httpHeaders:\n"
            "                mode: 1\n"
        )
        expectation = mod.RequestExpectation(
            request_name="A",
            mode="oauth2",
            auth_profile_name="OAuth 2.0",
            oauth_access_token="bad",
            oauth_add_token_to="header",
            oauth_header_prefix="Bearer",
        )
        updated, changed, matched = mod.patch_auth_in_rest_client_block(block, expectation)
        self.assertTrue(matched)
        self.assertTrue(changed)
        self.assertIn("authName: OAuth 2.0", updated)
        self.assertIn("httpHeaders:", updated)
        self.assertIn("properties:", updated)
        self.assertIn("name: Authorization", updated)
        self.assertIn("value: Bearer bad", updated)
        self.assertNotIn("mode: 1", updated)


class JwtParsingTests(unittest.TestCase):
    def test_parse_jwt_auth_object(self):
        parsed = mod.parse_auth_object(
            {
                "type": "jwt",
                "jwt": [
                    {"key": "algorithm", "value": "HS256", "type": "string"},
                    {"key": "secret", "value": "{{JWT_SECRET}}", "type": "string"},
                    {"key": "payload", "value": '{"sub":"demo"}', "type": "string"},
                    {"key": "addTokenTo", "value": "header", "type": "string"},
                    {"key": "headerPrefix", "value": "Bearer", "type": "string"},
                    {"key": "header", "value": '{"kid":"abc"}', "type": "string"},
                    {"key": "isSecretBase64Encoded", "value": "false", "type": "boolean"},
                ],
            }
        )
        self.assertEqual("jwt", parsed.mode)
        self.assertEqual("${JWT_SECRET}", parsed.jwt_secret)
        self.assertEqual("HS256", parsed.jwt_algorithm)
        self.assertEqual('{"sub":"demo"}', parsed.jwt_payload)
        self.assertEqual("header", parsed.jwt_add_token_to)
        self.assertEqual("Bearer", parsed.jwt_header_prefix)
        self.assertEqual('{"kid":"abc"}', parsed.jwt_headers)
        self.assertEqual(False, parsed.jwt_secret_base64_encoded)


class JwtInheritanceTests(unittest.TestCase):
    def test_resolve_effective_auth_merges_jwt_values(self):
        parent = mod.AuthSpec(
            mode="jwt",
            jwt_payload='{"sub":"demo"}',
            jwt_secret="${JWT_SECRET}",
            jwt_algorithm="HS256",
            jwt_add_token_to="header",
            jwt_header_prefix="Bearer",
        )
        raw = mod.AuthSpec(mode="jwt", jwt_secret="bad")
        effective = mod.resolve_effective_auth(raw, parent)
        self.assertEqual("jwt", effective.mode)
        self.assertEqual('{"sub":"demo"}', effective.jwt_payload)
        self.assertEqual("bad", effective.jwt_secret)
        self.assertEqual("HS256", effective.jwt_algorithm)
        self.assertEqual("header", effective.jwt_add_token_to)


class JwtExpectationTests(unittest.TestCase):
    def test_build_expectations_generates_jwt_token(self):
        targets = [
            mod.RequestTarget(
                folder_path=(),
                request_name="A",
                effective_auth=mod.AuthSpec(
                    mode="jwt",
                    jwt_payload='{"sub":"demo"}',
                    jwt_secret='${JWT_SECRET}',
                    jwt_algorithm="HS256",
                    jwt_add_token_to="header",
                    jwt_header_prefix="Bearer",
                ),
            )
        ]
        expectations, profiles = mod.build_expectations(targets, {"JWT_SECRET": "secret-value"})
        self.assertEqual(1, len(expectations))
        self.assertEqual(0, len(profiles))
        self.assertEqual("jwt", expectations[0].mode)
        self.assertEqual("header", expectations[0].jwt_add_token_to)
        self.assertTrue(expectations[0].jwt_access_token.startswith("ey"))


class JwtRestClientPatchTests(unittest.TestCase):
    def test_patch_auth_in_rest_client_block_injects_jwt_bearer_header(self):
        block = (
            "  - $type: RESTClientToolTest\n"
            "    tool:\n"
            "      $type: RESTClient\n"
            "      transportProperties:\n"
            "        manager:\n"
            "          protocol: 1\n"
            "          properties:\n"
            "          - $type: HTTPClientHTTPProperties\n"
            "            common:\n"
            "              method:\n"
            "                fixedValue:\n"
            "                  method: GET\n"
            "              httpHeaders:\n"
            "                mode: 1\n"
        )
        expectation = mod.RequestExpectation(
            request_name="A",
            mode="jwt",
            jwt_access_token="abc.def.ghi",
            jwt_add_token_to="header",
            jwt_header_prefix="Bearer",
        )
        updated, changed, matched = mod.patch_auth_in_rest_client_block(block, expectation)
        self.assertTrue(matched)
        self.assertTrue(changed)
        self.assertIn("customType: 1", updated)
        self.assertIn("name: Authorization", updated)
        self.assertIn("value: Bearer abc.def.ghi", updated)


class SchemaVersionGuardTests(unittest.TestCase):
    def test_ensure_min_schema_version_sets_when_missing(self):
        source = "---\nparasoftVersion: 2026.1.0\nproductVersion: 10.7.5\nsuite:\n  $type: TestSuite\n"
        updated = mod.ensure_min_schema_version(source, "05")
        self.assertIn("schemaVersion: 05", updated)

    def test_ensure_min_schema_version_does_not_downgrade(self):
        source = "---\nparasoftVersion: 2026.1.0\nproductVersion: 10.7.5\nschemaVersion: 13\nsuite:\n  $type: TestSuite\n"
        updated = mod.ensure_min_schema_version(source, "05")
        self.assertIn("schemaVersion: 13", updated)


class BinaryBodyTemplateFallbackTests(unittest.TestCase):
    def test_load_binary_body_templates_extracts_literal_file_block(self):
        template = """suite:
  tests:
  - $type: RESTClientToolTest
    iconName: RESTClient
    name: A_Binary_Inherit_200
    tool:
      $type: RESTClient
      literal:
        use: 2
        text:
          MessagingClient_LiteralMessage: ''
          type: application/octet-stream
        file:
          isEmpty: false
          location:
            path: artifacts/sample.bin
      mode: Literal
"""

        templates, default_template, warnings = mod.load_binary_body_templates(template)

        self.assertIn("A_Binary_Inherit_200", templates)
        self.assertIsNotNone(default_template)
        self.assertTrue(templates["A_Binary_Inherit_200"].literal_block.startswith("      literal:"))
        self.assertEqual([], warnings)

    def test_patch_binary_request_bodies_replaces_literal_block(self):
        content = """suite:
  tests:
  - $type: RESTClientToolTest
    iconName: RESTClient
    name: A_Binary_Inherit_200
    tool:
      $type: RESTClient
      literal:
        use: 1
        text:
          MessagingClient_LiteralMessage: ''
          type: application/json
      mode: Literal
"""
        template_literal = """      literal:
        use: 2
        text:
          MessagingClient_LiteralMessage: ''
          type: application/octet-stream
        file:
          isEmpty: false
          location:
            path: artifacts/sample.bin
"""

        templates = {
            "A_Binary_Inherit_200": mod.BinaryBodyTemplate(
                request_name="A_Binary_Inherit_200",
                literal_block=template_literal,
            )
        }
        targets = [
            mod.BinaryBodyTarget(
                request_name="A_Binary_Inherit_200",
                body_mode="file",
                source_reference="${BINARY_FILE_PATH}",
                content_type="application/octet-stream",
            )
        ]

        updated, patched, already, missing, template_missing, warnings = mod.patch_binary_request_bodies(
            content, targets, templates, None
        )

        self.assertEqual(1, patched)
        self.assertEqual(0, already)
        self.assertEqual(0, missing)
        self.assertEqual(0, template_missing)
        self.assertEqual([], warnings)
        self.assertIn("type: application/octet-stream", updated)
        self.assertIn("file:", updated)
        self.assertIn('path: "${BINARY_FILE_PATH}"\n      mode: Literal', updated)

    def test_patch_binary_request_bodies_reports_missing_template_entry(self):
        content = """suite:
  tests:
  - $type: RESTClientToolTest
    iconName: RESTClient
    name: A_Binary_Inherit_200
    tool:
      $type: RESTClient
      literal:
        use: 1
      mode: Literal
"""
        targets = [
            mod.BinaryBodyTarget(
                request_name="A_Binary_Inherit_200",
                body_mode="file",
                source_reference="${BINARY_FILE_PATH}",
                content_type="application/octet-stream",
            )
        ]

        updated, patched, already, missing, template_missing, warnings = mod.patch_binary_request_bodies(
            content, targets, {}, None
        )

        self.assertEqual(content, updated)
        self.assertEqual(0, patched)
        self.assertEqual(0, already)
        self.assertEqual(0, missing)
        self.assertEqual(1, template_missing)
        self.assertTrue(any("No binary-body template entry found" in warning for warning in warnings))

    def test_patch_binary_request_bodies_uses_default_template_entry(self):
        content = """suite:
  tests:
  - $type: RESTClientToolTest
    iconName: RESTClient
    name: Different_Request_Name
    tool:
      $type: RESTClient
      literal:
        use: 1
      mode: Literal
"""
        template_literal = """      literal:
        use: 2
        text:
          MessagingClient_LiteralMessage: ''
          type: application/octet-stream
        file:
          isEmpty: false
          location:
            path: artifacts/sample.bin
"""
        default_template = mod.BinaryBodyTemplate(
            request_name="DefaultBinaryTemplate",
            literal_block=template_literal,
        )
        targets = [
            mod.BinaryBodyTarget(
                request_name="Different_Request_Name",
                body_mode="file",
                source_reference="/C:/tmp/custom.bin",
                content_type="application/octet-stream",
            )
        ]

        updated, patched, already, missing, template_missing, warnings = mod.patch_binary_request_bodies(
            content, targets, {}, default_template
        )

        self.assertEqual(1, patched)
        self.assertEqual(0, already)
        self.assertEqual(0, missing)
        self.assertEqual(0, template_missing)
        self.assertIn('path: "C:/tmp/custom.bin"', updated)
        self.assertTrue(any("used built-in default template entry" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()

