import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "postman_auth_implementation.py"
spec = importlib.util.spec_from_file_location("postman_auth_impl", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class AuthPayloadTests(unittest.TestCase):
    def test_basic_payload_uses_fixed_password(self):
        auth = mod.AuthSpec(mode="basic", auth_type="basic", username="${u}", password="${p}")
        payload = mod.build_authentication_type_payload(auth)
        self.assertEqual("basic", payload["type"])
        self.assertEqual("fixed", payload["basic"]["password"]["type"])
        self.assertEqual("${p}", payload["basic"]["password"]["fixed"])

    def test_digest_payload_uses_digest_block_and_fixed_password(self):
        auth = mod.AuthSpec(mode="digest", auth_type="digest", username="${du}", password="${dp}")
        payload = mod.build_authentication_type_payload(auth)
        self.assertEqual("digest", payload["type"])
        self.assertIn("digest", payload)
        self.assertEqual("fixed", payload["digest"]["password"]["type"])
        self.assertEqual("${dp}", payload["digest"]["password"]["fixed"])

    def test_ntlm_payload_contains_domain_and_workstation(self):
        auth = mod.AuthSpec(
            mode="ntlm",
            auth_type="ntlm",
            username="${nu}",
            password="${np}",
            domain="${nd}",
            workstation="${nw}",
        )
        payload = mod.build_authentication_type_payload(auth)
        self.assertEqual("ntlm", payload["type"])
        self.assertEqual("${nd}", payload["ntlm"]["domain"]["fixed"])
        self.assertEqual("${nw}", payload["ntlm"]["workstation"]["fixed"])

    def test_awsv4_payload_contains_expected_fields(self):
        auth = mod.AuthSpec(
            mode="awsv4",
            auth_type="awsv4",
            access_key_id="${AWS_ACCESS_KEY}",
            secret_access_key="${AWS_SECRET_KEY}",
            session_token="${AWS_SESSION_TOKEN}",
            service="ec2",
            region="${AWS_REGION}",
            aws_auth_mode=1,
        )
        payload = mod.build_authentication_type_payload(auth)
        self.assertEqual("awsSignature", payload["type"])
        self.assertEqual("${AWS_ACCESS_KEY}", payload["awsSignature"]["accessKeyId"]["fixed"])
        self.assertEqual("${AWS_SECRET_KEY}", payload["awsSignature"]["secretAccessKey"]["fixed"])
        self.assertEqual("${AWS_SESSION_TOKEN}", payload["awsSignature"]["sessionToken"]["fixed"])
        self.assertEqual("ec2", payload["awsSignature"]["service"]["fixed"])
        self.assertEqual("${AWS_REGION}", payload["awsSignature"]["region"]["fixed"])
        self.assertEqual(1, payload["awsSignature"]["authMode"])

    def test_bearer_payload_is_applied_as_manual_header(self):
        auth = mod.AuthSpec(mode="bearer", auth_type="bearer", bearer_token="${GOOD_BEARER}")
        payload = mod.build_authentication_type_payload(auth)
        self.assertIsNone(payload)

    def test_apikey_payload_is_applied_as_manual_header_or_query(self):
        auth = mod.AuthSpec(
            mode="apikey",
            auth_type="apikey",
            api_key_name="x-api-key",
            api_key_value="${GOOD_API_KEY}",
            api_key_in="header",
        )
        payload = mod.build_authentication_type_payload(auth)
        self.assertIsNone(payload)


class EnvironmentResolutionTests(unittest.TestCase):
    def test_resolve_auth_spec_with_environment_replaces_variables(self):
        auth = mod.AuthSpec(
            mode="basic",
            auth_type="basic",
            username="${username}",
            password="${password}",
        )
        env = {"username": "purchaser", "password": "password"}
        resolved = mod.resolve_auth_spec_with_environment(auth, env)
        self.assertEqual("purchaser", resolved.username)
        self.assertEqual("password", resolved.password)

    def test_resolve_auth_spec_with_environment_keeps_missing_values(self):
        auth = mod.AuthSpec(
            mode="basic",
            auth_type="basic",
            username="${username}",
            password="${password}",
        )
        env = {"username": "purchaser"}
        resolved = mod.resolve_auth_spec_with_environment(auth, env)
        self.assertEqual("purchaser", resolved.username)
        self.assertEqual("${password}", resolved.password)

    def test_resolve_awsv4_spec_with_environment_replaces_variables(self):
        auth = mod.AuthSpec(
            mode="awsv4",
            auth_type="awsv4",
            access_key_id="${AWS_ACCESS_KEY}",
            secret_access_key="${AWS_SECRET_KEY}",
            session_token="${AWS_SESSION_TOKEN}",
            service="ec2",
            region="${AWS_REGION}",
        )
        env = {
            "AWS_ACCESS_KEY": "AKIA123",
            "AWS_SECRET_KEY": "SECRET123",
            "AWS_SESSION_TOKEN": "TOKEN123",
            "AWS_REGION": "us-west-2",
        }
        resolved = mod.resolve_auth_spec_with_environment(auth, env)
        self.assertEqual("AKIA123", resolved.access_key_id)
        self.assertEqual("SECRET123", resolved.secret_access_key)
        self.assertEqual("TOKEN123", resolved.session_token)
        self.assertEqual("us-west-2", resolved.region)

    def test_resolve_apikey_spec_keeps_variable_reference(self):
        auth = mod.AuthSpec(
            mode="apikey",
            auth_type="apikey",
            api_key_name="x-api-key",
            api_key_value="${GOOD_API_KEY}",
            api_key_in="header",
        )
        env = {"GOOD_API_KEY": "good-key-123"}
        resolved = mod.resolve_auth_spec_with_environment(auth, env)
        self.assertEqual("${GOOD_API_KEY}", resolved.api_key_value)

    def test_resolve_bearer_spec_with_environment_replaces_token(self):
        auth = mod.AuthSpec(mode="bearer", auth_type="bearer", bearer_token="${GOOD_BEARER}")
        env = {"GOOD_BEARER": "token-123"}
        resolved = mod.resolve_auth_spec_with_environment(auth, env)
        self.assertEqual("token-123", resolved.bearer_token)


class PersistenceCheckTests(unittest.TestCase):
    def test_persistence_check_passes_for_noauth(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {"security": {"httpAuthentication": {"performAuthentication": {"enabled": False}}}},
                }
            }
        }
        self.assertTrue(mod.auth_persisted_matches(rest_client, mod.AuthSpec(mode="noauth", auth_type="noauth")))

    def test_persistence_check_passes_for_bearer_header_mode(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {"httpAuthentication": {"performAuthentication": {"enabled": False}}},
                        "httpHeaders": {
                            "type": "literal",
                            "literal": "Accept: application/json\nAuthorization: Bearer ${GOOD_BEARER}",
                        },
                    },
                }
            }
        }
        expected = mod.AuthSpec(mode="bearer", auth_type="bearer", bearer_token="${GOOD_BEARER}")
        self.assertTrue(mod.auth_persisted_matches(rest_client, expected))

    def test_persistence_check_passes_for_apikey_header_mode(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {"httpAuthentication": {"performAuthentication": {"enabled": False}}},
                        "httpHeaders": {
                            "type": "literal",
                            "literal": "Accept: application/json\nx-api-key: ${GOOD_API_KEY}",
                        },
                    },
                }
            }
        }
        expected = mod.AuthSpec(
            mode="apikey",
            auth_type="apikey",
            api_key_name="x-api-key",
            api_key_value="${GOOD_API_KEY}",
            api_key_in="header",
        )
        self.assertTrue(mod.auth_persisted_matches(rest_client, expected))

    def test_persistence_check_passes_for_apikey_query_mode(self):
        rest_client = {
            "resource": {
                "type": "literalText",
                "literalText": {"fixed": "${BASE_URL}/anything?case=B&X-Api-Key=abc123"},
            },
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {"httpAuthentication": {"performAuthentication": {"enabled": False}}}
                    },
                }
            },
        }
        expected = mod.AuthSpec(
            mode="apikey",
            auth_type="apikey",
            api_key_name="X-Api-Key",
            api_key_value="abc123",
            api_key_in="query",
        )
        self.assertTrue(mod.auth_persisted_matches(rest_client, expected))

    def test_persistence_check_detects_mismatch(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {
                            "httpAuthentication": {
                                "performAuthentication": {
                                    "enabled": True,
                                    "value": {
                                        "useGlobal": False,
                                        "authenticationType": {
                                            "type": "basic",
                                            "basic": {
                                                "username": {"type": "fixed", "fixed": "${u}"},
                                                "password": {"type": "fixed", "fixed": "${p}"},
                                            },
                                        },
                                    },
                                }
                            }
                        }
                    },
                }
            }
        }
        expected = mod.AuthSpec(mode="digest", auth_type="digest", username="${u}", password="${p}")
        self.assertFalse(mod.auth_persisted_matches(rest_client, expected))

    def test_persistence_check_accepts_masked_password_for_basic(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {
                            "httpAuthentication": {
                                "performAuthentication": {
                                    "enabled": True,
                                    "value": {
                                        "useGlobal": False,
                                        "authenticationType": {
                                            "type": "basic",
                                            "basic": {
                                                "username": {"type": "fixed", "fixed": "${username}"},
                                                "password": {"type": "masked", "masked": "AwAAAAA="},
                                            },
                                        },
                                    },
                                }
                            }
                        }
                    },
                }
            }
        }
        expected = mod.AuthSpec(
            mode="basic",
            auth_type="basic",
            username="${username}",
            password="${password}",
        )
        self.assertTrue(mod.auth_persisted_matches(rest_client, expected))

    def test_persistence_check_passes_for_awsv4(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {
                            "httpAuthentication": {
                                "performAuthentication": {
                                    "enabled": True,
                                    "value": {
                                        "useGlobal": False,
                                        "authenticationType": {
                                            "type": "awsSignature",
                                            "awsSignature": {
                                                "accessKeyId": {"type": "fixed", "fixed": "${AWS_ACCESS_KEY}"},
                                                "secretAccessKey": {"type": "fixed", "fixed": "${AWS_SECRET_KEY}"},
                                                "sessionToken": {"type": "fixed", "fixed": "${AWS_SESSION_TOKEN}"},
                                                "service": {"type": "fixed", "fixed": "ec2"},
                                                "region": {"type": "fixed", "fixed": "${AWS_REGION}"},
                                                "authMode": 1,
                                            },
                                        },
                                    },
                                }
                            }
                        }
                    },
                }
            }
        }
        expected = mod.AuthSpec(
            mode="awsv4",
            auth_type="awsv4",
            access_key_id="${AWS_ACCESS_KEY}",
            secret_access_key="${AWS_SECRET_KEY}",
            session_token="${AWS_SESSION_TOKEN}",
            service="ec2",
            region="${AWS_REGION}",
            aws_auth_mode=1,
        )
        self.assertTrue(mod.auth_persisted_matches(rest_client, expected))


class PayloadUpdateTests(unittest.TestCase):
    def test_build_payload_replaces_existing_authorization_header_for_bearer(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {
                            "httpAuthentication": {
                                "performAuthentication": {
                                    "enabled": True,
                                    "value": {
                                        "useGlobal": False,
                                        "authenticationType": {"type": "basic", "basic": {}},
                                    },
                                }
                            }
                        },
                        "httpHeaders": {
                            "type": "literal",
                            "literal": "X-Test: 1\nAuthorization: Bearer old-token\nAuthorization: Bearer stale-token",
                        },
                    },
                }
            }
        }

        auth = mod.AuthSpec(mode="bearer", auth_type="bearer", bearer_token="{{GOOD_BEARER}}")
        payload = mod.build_rest_client_update_payload(rest_client, auth)

        perform = payload["httpOptions"]["transport"]["http10"]["security"]["httpAuthentication"]["performAuthentication"]
        self.assertFalse(perform["enabled"])
        self.assertNotIn("value", perform)

        literal = payload["httpOptions"]["transport"]["http10"]["httpHeaders"]["literal"]
        self.assertIn("X-Test: 1", literal)
        self.assertIn("Authorization: Bearer ${GOOD_BEARER}", literal)
        self.assertEqual(1, literal.count("Authorization:"))

    def test_build_payload_upserts_apikey_header_and_disables_auth(self):
        rest_client = {
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {
                            "httpAuthentication": {
                                "performAuthentication": {
                                    "enabled": True,
                                    "value": {
                                        "useGlobal": False,
                                        "authenticationType": {"type": "basic", "basic": {}},
                                    },
                                }
                            }
                        },
                        "httpHeaders": {
                            "type": "literal",
                            "literal": "Accept: application/json\nx-api-key: stale",
                        },
                    },
                }
            }
        }

        auth = mod.AuthSpec(
            mode="apikey",
            auth_type="apikey",
            api_key_name="x-api-key",
            api_key_value="{{GOOD_API_KEY}}",
            api_key_in="header",
        )
        payload = mod.build_rest_client_update_payload(rest_client, auth)

        perform = payload["httpOptions"]["transport"]["http10"]["security"]["httpAuthentication"]["performAuthentication"]
        self.assertFalse(perform["enabled"])
        self.assertNotIn("value", perform)

        literal = payload["httpOptions"]["transport"]["http10"]["httpHeaders"]["literal"]
        self.assertIn("Accept: application/json", literal)
        self.assertIn("x-api-key: ${GOOD_API_KEY}", literal)
        self.assertEqual(1, literal.lower().count("x-api-key:"))

    def test_build_payload_upserts_apikey_query_parameter(self):
        rest_client = {
            "resource": {
                "type": "literalText",
                "literalText": {"fixed": "${BASE_URL}/anything?case=B"},
            },
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "security": {
                            "httpAuthentication": {
                                "performAuthentication": {
                                    "enabled": True,
                                    "value": {
                                        "useGlobal": False,
                                        "authenticationType": {"type": "basic", "basic": {}},
                                    },
                                }
                            }
                        }
                    },
                }
            },
        }

        auth = mod.AuthSpec(
            mode="apikey",
            auth_type="apikey",
            api_key_name="X-Api-Key",
            api_key_value="token-123",
            api_key_in="query",
        )
        payload = mod.build_rest_client_update_payload(rest_client, auth)

        perform = payload["httpOptions"]["transport"]["http10"]["security"]["httpAuthentication"]["performAuthentication"]
        self.assertFalse(perform["enabled"])
        self.assertNotIn("value", perform)

        new_url = payload["resource"]["literalText"]["fixed"]
        self.assertEqual("${BASE_URL}/anything?case=B&X-Api-Key=token-123", new_url)


class ParsingTests(unittest.TestCase):
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

    def test_parse_bearer_auth_object(self):
        parsed = mod.parse_auth_object(
            {
                "type": "bearer",
                "bearer": [
                    {"key": "token", "value": "{{GOOD_BEARER}}", "type": "string"}
                ],
            }
        )
        self.assertEqual("bearer", parsed.mode)
        self.assertEqual("${GOOD_BEARER}", parsed.bearer_token)
        self.assertEqual("Bearer", parsed.bearer_prefix)

    def test_parse_jwt_auth_object(self):
        parsed = mod.parse_auth_object(
            {
                "type": "jwt",
                "jwt": [
                    {"key": "algorithm", "value": "HS256", "type": "string"},
                    {"key": "secret", "value": "{{JWT_SECRET}}", "type": "string"},
                    {"key": "payload", "value": '{"sub":"demo"}', "type": "string"},
                ],
            }
        )
        self.assertEqual("jwt", parsed.mode)
        self.assertEqual("jwt", parsed.auth_type)

    def test_parse_apikey_auth_object_defaults_to_header(self):
        parsed = mod.parse_auth_object(
            {
                "type": "apikey",
                "apikey": [
                    {"key": "key", "value": "x-api-key", "type": "string"},
                    {"key": "value", "value": "{{GOOD_API_KEY}}", "type": "string"},
                ],
            }
        )
        self.assertEqual("apikey", parsed.mode)
        self.assertEqual("x-api-key", parsed.api_key_name)
        self.assertEqual("${GOOD_API_KEY}", parsed.api_key_value)
        self.assertEqual("header", parsed.api_key_in)

    def test_parse_apikey_auth_object_query_placement(self):
        parsed = mod.parse_auth_object(
            {
                "type": "apikey",
                "apikey": [
                    {"key": "in", "value": "query", "type": "string"},
                    {"key": "key", "value": "X-Api-Key", "type": "string"},
                    {"key": "value", "value": "abc123", "type": "string"},
                ],
            }
        )
        self.assertEqual("apikey", parsed.mode)
        self.assertEqual("X-Api-Key", parsed.api_key_name)
        self.assertEqual("abc123", parsed.api_key_value)
        self.assertEqual("query", parsed.api_key_in)


class YamlNormalizationTests(unittest.TestCase):
    def test_normalize_basic_auth_profiles_collapses_to_single_profile(self):
        source = """suite:
  authentications:
  - $type: BasicAuthentication
    name: Basic
    username:
      fixedValue:
        $type: StringTestValue
        username: purchaser
  - $type: BasicAuthentication
    name: Basic 2
    username:
      fixedValue:
        $type: StringTestValue
        username: purchaser
  tests:
  - dummy: true
"""
        result = mod.normalize_basic_auth_profiles_yaml(source)
        self.assertIn('name: Basic', result)
        self.assertIn('username: "${username}"', result)
        self.assertIn('password: "${password}"', result)
        self.assertNotIn('name: Basic 2', result)

    def test_normalize_basic_auth_profiles_rewrites_auth_name_references(self):
        source = """suite:
  authentications:
  - $type: BasicAuthentication
    name: Basic 9
    username:
      fixedValue:
        $type: StringTestValue
        username: purchaser
  tests:
  - dummy: true
        authName: Basic 9
        authName: Basic 11
"""
        result = mod.normalize_basic_auth_profiles_yaml(source)
        self.assertIn('authName: Basic', result)
        self.assertNotIn('authName: Basic 9', result)
        self.assertNotIn('authName: Basic 11', result)


if __name__ == "__main__":
    unittest.main()

