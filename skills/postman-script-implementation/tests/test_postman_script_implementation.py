import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "postman_script_implementation.py"
spec = importlib.util.spec_from_file_location("postman_script_impl", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class ScriptPolicyTests(unittest.TestCase):
    def test_requires_script_fallback_for_pre_request(self):
        occurrence = mod.ScriptOccurrence(
            phase=mod.PHASE_PRE,
            listen=mod.LISTEN_PRE,
            scope="request",
            json_path="item[0].event[0]",
            folder_hierarchy=(),
            request_name="Req",
            event_index=0,
            script_text="pm.variables.set('x', 1);",
            line_count=1,
            has_executable_code=True,
        )
        self.assertTrue(
            mod.requires_script_fallback(
                occurrence=occurrence,
                has_unmapped_set_assignments=False,
                native_created=False,
                script_text=occurrence.script_text,
            )
        )

    def test_fallback_script_generation_disabled(self):
        occurrence = mod.ScriptOccurrence(
            phase=mod.PHASE_POST,
            listen=mod.LISTEN_POST,
            scope="request",
            json_path="item[0].event[1]",
            folder_hierarchy=(),
            request_name="Req",
            event_index=1,
            script_text="pm.test('status', function(){ pm.expect(pm.response.code).to.equal(200); });",
            line_count=1,
            has_executable_code=True,
        )
        self.assertFalse(
            mod.can_generate_functional_extension_script(
                occurrence=occurrence,
                script_text=occurrence.script_text,
                conversion_mode=mod.SCRIPT_MODE_PREFER_NATIVE,
            )
        )

    def test_requires_script_fallback_for_hard_advanced_patterns_even_with_native_created(self):
        occurrence = mod.ScriptOccurrence(
            phase=mod.PHASE_POST,
            listen=mod.LISTEN_POST,
            scope="request",
            json_path="item[0].event[2]",
            folder_hierarchy=(),
            request_name="Req",
            event_index=2,
            script_text="pm.sendRequest(\"http://example.com\", function () { });",
            line_count=1,
            has_executable_code=True,
        )
        self.assertTrue(
            mod.requires_script_fallback(
                occurrence=occurrence,
                has_unmapped_set_assignments=False,
                native_created=True,
                script_text=occurrence.script_text,
            )
        )

    def test_does_not_require_fallback_for_simple_if_when_native_created(self):
        occurrence = mod.ScriptOccurrence(
            phase=mod.PHASE_POST,
            listen=mod.LISTEN_POST,
            scope="request",
            json_path="item[0].event[3]",
            folder_hierarchy=(),
            request_name="Req",
            event_index=3,
            script_text="if (json.data && json.data.orderNumber) { pm.environment.set('orderNumber', json.data.orderNumber); }",
            line_count=1,
            has_executable_code=True,
        )
        self.assertFalse(
            mod.requires_script_fallback(
                occurrence=occurrence,
                has_unmapped_set_assignments=False,
                native_created=True,
                script_text=occurrence.script_text,
            )
        )

    def test_non_request_scripts_are_omitted_with_warning(self):
        occurrence = mod.ScriptOccurrence(
            phase=mod.PHASE_PRE,
            listen=mod.LISTEN_PRE,
            scope="collection",
            json_path="event[0]",
            folder_hierarchy=(),
            request_name=None,
            event_index=0,
            script_text="pm.environment.set('x', '1');",
            line_count=1,
            has_executable_code=True,
        )
        result = mod.apply_non_request_event(
            client=None,  # not used in native-only policy path
            top_suite_id="/TestAssets/any.tst/Test Suite",
            occurrence=occurrence,
            storage_mode=mod.SCRIPT_STORAGE_EMBEDDED,
            script_language="JavaScript (OpenJDK Nashorn)",
            external_script_dir=None,
        )
        self.assertEqual([], result.created_tool_ids)
        self.assertTrue(any("omitted" in warning.lower() for warning in result.warnings))


    def test_pm_test_titles_are_used_for_assertion_names(self):
        script = """
            const json = pm.response.json();

            pm.test("Add item 1 request succeeded", function () {
                pm.expect(json.status).to.equal(1);
            });

            pm.test("Add item 1 message", function () {
                pm.expect(json.message).to.equal("success");
            });
        """

        candidates = mod.extract_assertion_candidates(script)
        test_names = mod.extract_postman_test_names(script)
        renamed = mod.apply_postman_test_names_to_assertions(candidates, test_names)

        def read_name(candidate):
            for key, value in candidate.assertion.items():
                if key != "type" and isinstance(value, dict) and "name" in value:
                    return value["name"]
            return None

        self.assertEqual(
            ["Add item 1 request succeeded", "Add item 1 message"],
            [read_name(candidate) for candidate in renamed],
        )

    def test_assertion_names_stay_aligned_when_middle_test_has_only_response_code(self):
        script = """
            const json = pm.response.json();

            pm.test("A - status assertion", function () {
                pm.expect(json.status).to.equal(1);
            });

            pm.test("B - response code only", function () {
                pm.expect(pm.response.code).to.be.oneOf([200, 304]);
            });

            pm.test("C - message assertion", function () {
                pm.expect(json.message).to.equal("success");
            });
        """

        candidates = mod.extract_assertion_candidates_aligned_with_tests(script)

        def read_name(candidate):
            for key, value in candidate.assertion.items():
                if key != "type" and isinstance(value, dict) and "name" in value:
                    return value["name"]
            return None

        self.assertEqual(2, len(candidates))
        self.assertEqual(
            ["A - status assertion", "C - message assertion"],
            [read_name(candidate) for candidate in candidates],
        )

    def test_string_wrapper_tolowercase_is_supported_for_include_assertions(self):
        script = """
            const json = pm.response.json();
            pm.expect(String(json.message).toLowerCase()).to.include("success");
            pm.expect(String(json.message).toLowerCase()).to.contain("success");
        """
        candidates = mod.extract_assertion_candidates(script)
        string_assertions = [
            c.assertion["stringComparisonAssertion"]
            for c in candidates
            if c.assertion.get("type") == "stringComparisonAssertion"
        ]

        self.assertEqual(2, len(string_assertions))
        self.assertEqual("contain", string_assertions[0]["configuration"]["stringOperator"])
        self.assertEqual("contain", string_assertions[1]["configuration"]["stringOperator"])

    def test_regex_assertions_are_extracted_with_condition_and_flags(self):
        script = """
            const json = pm.response.json();

            pm.test("Email format", function () {
                pm.expect(json.email).to.match(/^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$/i);
            });

            pm.test("Order id not alpha", function () {
                pm.expect(json.orderId).to.not.match(/[A-Za-z]+/);
            });
        """

        candidates = mod.extract_assertion_candidates(script)
        regex_candidates = [
            c.assertion["regularExpressionAssertion"]
            for c in candidates
            if c.assertion.get("type") == "regularExpressionAssertion"
        ]

        self.assertEqual(2, len(regex_candidates))

        first = regex_candidates[0]
        self.assertEqual("match", first["configuration"]["condition"])
        self.assertEqual("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", first["configuration"]["regularExpression"]["fixed"])
        self.assertTrue(first["options"]["ignoreCase"])

        second = regex_candidates[1]
        self.assertEqual("not match", second["configuration"]["condition"])
        self.assertEqual("[A-Za-z]+", second["configuration"]["regularExpression"]["fixed"])

    def test_regex_assertions_support_new_regexp_constructor(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.email).to.match(new RegExp("^[^@\\\\s]+@[^@\\\\s]+\\\\.[^@\\\\s]+$", "i"));
            pm.expect(json.userName).to.not.match(new RegExp("^[A-Z]+$"));
        """

        candidates = mod.extract_assertion_candidates(script)
        regex_candidates = [
            c.assertion["regularExpressionAssertion"]
            for c in candidates
            if c.assertion.get("type") == "regularExpressionAssertion"
        ]
        self.assertEqual(2, len(regex_candidates))
        self.assertEqual("match", regex_candidates[0]["configuration"]["condition"])
        self.assertTrue(regex_candidates[0]["options"]["ignoreCase"])
        self.assertEqual("not match", regex_candidates[1]["configuration"]["condition"])

    def test_structure_assertions_are_extracted(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.message).to.exist;
            pm.expect(json.items).to.not.be.empty;
            pm.expect(json.items).to.be.an("array");
        """

        candidates = mod.extract_assertion_candidates(script)

        has_content = [c.assertion["hasContentAssertion"] for c in candidates if c.assertion.get("type") == "hasContentAssertion"]
        has_children = [c.assertion["hasChildrenAssertion"] for c in candidates if c.assertion.get("type") == "hasChildrenAssertion"]
        type_assertions = [c.assertion["typeAssertion"] for c in candidates if c.assertion.get("type") == "typeAssertion"]

        self.assertEqual(1, len(has_content))
        self.assertEqual("true", has_content[0]["configuration"]["hasContent"]["fixed"])
        self.assertEqual(1, len(has_children))
        self.assertEqual("true", has_children[0]["configuration"]["hasChildren"]["fixed"])
        self.assertEqual(1, len(type_assertions))
        self.assertEqual("array", type_assertions[0]["configuration"]["expectedType"]["fixed"])

    def test_value_occurrence_assertions_are_extracted_from_filtered_length(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.items.filter((item) => String(item.id) === String(pm.environment.get("itemId1"))).length).to.equal(1);
            pm.expect(json.items.filter((item) => item.id === pm.environment.get("itemId2")).length).to.be.at.least(1);
        """

        candidates = mod.extract_assertion_candidates(script)
        value_occurrence = [
            c.assertion["valueOccurrenceAssertion"]
            for c in candidates
            if c.assertion.get("type") == "valueOccurrenceAssertion"
        ]

        self.assertEqual(2, len(value_occurrence))

        first = value_occurrence[0]
        self.assertEqual("==", first["configuration"]["operator"])
        self.assertEqual("${itemId1}", first["configuration"]["elementValue"]["fixed"])
        self.assertEqual("1", first["configuration"]["expectedValue"]["fixed"])

        second = value_occurrence[1]
        self.assertEqual(">=", second["configuration"]["operator"])
        self.assertEqual("${itemId2}", second["configuration"]["elementValue"]["fixed"])
        self.assertEqual("1", second["configuration"]["expectedValue"]["fixed"])

    def test_filtered_length_assertion_uses_filter_alias_predicate_for_occurrence(self):
        script = """
            const json = pm.response.json();
            const items = json.data.content;
            const validItems = items.filter(item => item.inStock >= 5);
            pm.expect(validItems.length).to.be.at.least(2);
        """

        candidates = mod.extract_assertion_candidates(script)
        occurrence = [
            c.assertion["occurrenceAssertion"]
            for c in candidates
            if c.assertion.get("type") == "occurrenceAssertion"
        ]

        self.assertEqual(1, len(occurrence))
        self.assertEqual(">=", occurrence[0]["configuration"]["operator"])
        self.assertEqual("2", occurrence[0]["configuration"]["expectedValue"]["fixed"])
        xpath = occurrence[0]["selectedElement"]["xpath"]
        self.assertIn("/content[1]/item[*]", xpath)
        self.assertIn("number(inStock[1])>=5", xpath)

    def test_range_assertions_are_extracted_for_numeric_date_and_datetime(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.total).to.be.within(10, 100);
            pm.expect(json.orderDate).to.be.within("2026-01-01", "2026-12-31");
            pm.expect(json.createdAt).to.be.within("2026-01-01T00:00:00Z", "2026-12-31T23:59:59Z");
        """

        candidates = mod.extract_assertion_candidates(script)

        numeric_range = [
            c.assertion["numericRangeAssertion"]
            for c in candidates
            if c.assertion.get("type") == "numericRangeAssertion"
        ]
        date_range = [
            c.assertion["dateRangeAssertion"]
            for c in candidates
            if c.assertion.get("type") == "dateRangeAssertion"
        ]
        date_time_range = [
            c.assertion["dateTimeRangeAssertion"]
            for c in candidates
            if c.assertion.get("type") == "dateTimeRangeAssertion"
        ]

        self.assertEqual(1, len(numeric_range))
        self.assertEqual("10", numeric_range[0]["configuration"]["lowerBoundValue"]["fixed"])
        self.assertEqual("100", numeric_range[0]["configuration"]["upperBoundValue"]["fixed"])

        self.assertEqual(1, len(date_range))
        self.assertEqual("2026-01-01", date_range[0]["configuration"]["lowerBoundDate"]["fixed"])
        self.assertEqual("2026-12-31", date_range[0]["configuration"]["upperBoundDate"]["fixed"])

        self.assertEqual(1, len(date_time_range))
        self.assertEqual("2026-01-01T00:00:00Z", date_time_range[0]["configuration"]["lowerBoundDateTime"]["fixed"])
        self.assertEqual("2026-12-31T23:59:59Z", date_time_range[0]["configuration"]["upperBoundDateTime"]["fixed"])

    def test_or_assertion_is_extracted_from_oneof(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.status).to.be.oneOf(["success", "warning", "queued"]);
        """

        candidates = mod.extract_assertion_candidates(script)
        or_assertions = [c.assertion["orAssertion"] for c in candidates if c.assertion.get("type") == "orAssertion"]

        self.assertEqual(1, len(or_assertions))
        nested = or_assertions[0]["configuration"]["assertions"]
        self.assertEqual(3, len(nested))
        self.assertTrue(all(a.get("type") == "valueAssertion" for a in nested))

    def test_response_code_oneof_does_not_consume_following_equal_assertion(self):
        script = """
            const json = pm.response.json();
            pm.expect(pm.response.code).to.be.oneOf([200, 201]);
            pm.expect(json.status).to.equal(1);
        """

        candidates = mod.extract_assertion_candidates(script)
        numeric_assertions = [
            c.assertion["numericAssertion"]
            for c in candidates
            if c.assertion.get("type") == "numericAssertion"
        ]
        or_assertions = [c.assertion for c in candidates if c.assertion.get("type") == "orAssertion"]

        self.assertEqual(1, len(numeric_assertions))
        self.assertEqual("1", numeric_assertions[0]["configuration"]["expectedValue"]["fixed"])
        self.assertEqual("=", numeric_assertions[0]["configuration"]["operator"])
        self.assertEqual(0, len(or_assertions))

    def test_response_code_expectations_include_have_status_shorthand(self):
        script = """
            pm.test("Items request was successful", function () {
                pm.response.to.have.status(200);
            });
        """

        self.assertEqual({200}, mod.extract_response_code_expectations(script))

    def test_response_code_expectations_allow_have_status_without_semicolon(self):
        script = 'pm.test("Status is 200", () => pm.response.to.have.status(200));'
        self.assertEqual({200}, mod.extract_response_code_expectations(script))

    def test_response_code_expectations_include_array_include_pattern(self):
        script = """
            pm.test(\"Status is 401 or 400\", () => pm.expect([400,401]).to.include(pm.response.code));
        """
        self.assertEqual({400, 401}, mod.extract_response_code_expectations(script))

    def test_response_code_range_constraints_are_intersected(self):
        script = """
            pm.expect(pm.response.code).to.be.within(200, 299);
            pm.expect(pm.response.code).to.be.below(250);
        """

        codes = mod.extract_response_code_expectations(script)
        self.assertEqual(50, len(codes))
        self.assertIn(200, codes)
        self.assertIn(249, codes)
        self.assertNotIn(250, codes)
        self.assertNotIn(199, codes)

    def test_response_code_at_least_and_at_most_constraints(self):
        script = """
            pm.expect(pm.response.code).to.be.at.least(200);
            pm.expect(pm.response.code).to.be.at.most(299);
        """

        codes = mod.extract_response_code_expectations(script)
        self.assertEqual(set(range(200, 300)), codes)

    def test_response_time_upper_bound_is_extracted_from_below(self):
        script = """
            pm.test("Items response was fast enough", function () {
                pm.expect(pm.response.responseTime).to.be.below(1000);
            });
        """

        self.assertEqual(999, mod.extract_response_time_upper_bound_ms(script))

    def test_rest_client_timeout_payload_uses_custom_mode(self):
        current = {
            "name": "Sample",
            "misc": {
                "timeout": {
                    "action": "failOnTimeout",
                    "milliseconds": {"mode": "Default", "value": 30000},
                }
            },
        }

        updated = mod.build_rest_client_update_payload_for_timeout(current, 999)
        self.assertEqual("Custom", updated["misc"]["timeout"]["milliseconds"]["mode"])
        self.assertEqual(999, updated["misc"]["timeout"]["milliseconds"]["value"])

    def test_environment_get_exist_maps_to_has_content_using_set_source_xpath(self):
        script = """
            const json = pm.response.json();
            const items = json.data.content;
            const selectedItem = items.find(item => item.inStock > 0);
            pm.environment.set("selectedItemId", selectedItem.id);
            pm.expect(pm.environment.get("selectedItemId")).to.exist;
        """

        candidates = mod.extract_assertion_candidates(script)
        has_content = [
            c.assertion["hasContentAssertion"]
            for c in candidates
            if c.assertion.get("type") == "hasContentAssertion"
        ]

        self.assertEqual(1, len(has_content))
        self.assertEqual("true", has_content[0]["configuration"]["hasContent"]["fixed"])
        self.assertIn("id", has_content[0]["selectedElement"]["xpath"])

    def test_aligned_extraction_preserves_set_context_for_env_get_exist(self):
        script = """
            const json = pm.response.json();
            const items = json.data.content;
            const selectedItem = items.find(item => item.inStock > 0);
            pm.environment.set("selectedItemId", selectedItem.id);

            pm.test("Saved selected item for cart request", function () {
                pm.expect(pm.environment.get("selectedItemId")).to.exist;
            });
        """

        candidates = mod.extract_assertion_candidates_aligned_with_tests(script)
        has_content = [
            c.assertion["hasContentAssertion"]
            for c in candidates
            if c.assertion.get("type") == "hasContentAssertion"
        ]

        self.assertEqual(1, len(has_content))
        self.assertEqual("Saved selected item for cart request", has_content[0]["name"])
        self.assertEqual("true", has_content[0]["configuration"]["hasContent"]["fixed"])
        self.assertIn("id", has_content[0]["selectedElement"]["xpath"])

    def test_lengthof_assertions_map_to_occurrence_assertions(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.data.content).to.have.lengthOf(3);
            pm.expect(json.data.content).to.not.have.lengthOf(0);
        """

        candidates = mod.extract_assertion_candidates(script)
        occurrence = [
            c.assertion["occurrenceAssertion"]
            for c in candidates
            if c.assertion.get("type") == "occurrenceAssertion"
        ]

        self.assertEqual(2, len(occurrence))
        self.assertEqual(["==", "!="], [a["configuration"]["operator"] for a in occurrence])
        self.assertEqual(["3", "0"], [a["configuration"]["expectedValue"]["fixed"] for a in occurrence])
        self.assertTrue(all("/item[*]" in a["selectedElement"]["xpath"] for a in occurrence))

    def test_property_assertions_map_to_native_assertions(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.data).to.have.property("id");
            pm.expect(json.data).to.have.property("status", 1);
            pm.expect(json.data).to.not.have.property("missing");
        """

        candidates = mod.extract_assertion_candidates(script)
        has_content = [
            c.assertion["hasContentAssertion"]
            for c in candidates
            if c.assertion.get("type") == "hasContentAssertion"
        ]
        numeric = [
            c.assertion["numericAssertion"]
            for c in candidates
            if c.assertion.get("type") == "numericAssertion"
        ]

        self.assertEqual(2, len(has_content))
        self.assertEqual(["true", "false"], [a["configuration"]["hasContent"]["fixed"] for a in has_content])
        self.assertEqual(1, len(numeric))
        self.assertEqual("1", numeric[0]["configuration"]["expectedValue"]["fixed"])

    def test_literal_state_assertions_map_to_native_assertions(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.success).to.be.true;
            pm.expect(json.flag).to.not.be.false;
            pm.expect(json.optional).to.be.null;
        """

        candidates = mod.extract_assertion_candidates(script)
        value_assertions = [c for c in candidates if c.assertion.get("type") == "valueAssertion"]
        string_assertions = [c for c in candidates if c.assertion.get("type") == "stringComparisonAssertion"]
        type_assertions = [c for c in candidates if c.assertion.get("type") == "typeAssertion"]

        self.assertEqual(1, len(value_assertions))
        self.assertEqual(1, len(string_assertions))
        self.assertEqual(1, len(type_assertions))
        self.assertEqual("null", type_assertions[0].assertion["typeAssertion"]["configuration"]["expectedType"]["fixed"])

    def test_and_assertion_is_extracted_from_include_chain(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.message).to.include("hello").and.to.include("world");
        """

        candidates = mod.extract_assertion_candidates(script)
        and_assertions = [c.assertion["andAssertion"] for c in candidates if c.assertion.get("type") == "andAssertion"]

        self.assertEqual(1, len(and_assertions))
        nested = and_assertions[0]["configuration"]["assertions"]
        self.assertEqual(2, len(nested))
        self.assertTrue(all(a.get("type") == "stringComparisonAssertion" for a in nested))

    def test_include_chain_does_not_create_malformed_standalone_assertion(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.message).to.include("suc").and.to.include("cess");
        """

        candidates = mod.extract_assertion_candidates(script)
        self.assertEqual(1, len(candidates))
        self.assertEqual("andAssertion", candidates[0].assertion.get("type"))

    def test_numeric_equal_and_not_equal_map_to_numeric_assertions(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.data.id).to.equal(0);
            pm.expect(json.status).to.not.eql(1);
        """

        candidates = mod.extract_assertion_candidates(script)
        numeric_assertions = [
            c.assertion["numericAssertion"]
            for c in candidates
            if c.assertion.get("type") == "numericAssertion"
        ]
        self.assertEqual(2, len(numeric_assertions))
        self.assertEqual(["=", "!="], [a["configuration"]["operator"] for a in numeric_assertions])

    def test_quoted_numeric_literals_do_not_map_to_numeric_assertions(self):
        script = """
            const json = pm.response.json();
            pm.expect(json.data.id).to.equal("0");
            pm.expect(json.status).to.not.eql("1");
        """

        candidates = mod.extract_assertion_candidates(script)
        types = [c.assertion.get("type") for c in candidates]
        self.assertIn("valueAssertion", types)
        self.assertIn("stringComparisonAssertion", types)
        self.assertNotIn("numericAssertion", types)


    def test_normalize_expected_value_supports_iteration_data_get(self):
        self.assertEqual("${itemId}", mod.normalize_expected_value('pm.iterationData.get("itemId")'))
        self.assertEqual("${itemId}", mod.normalize_expected_value('String(pm.iterationData.get("itemId"))'))

    def test_collect_iteration_data_variable_names_dedupes_and_preserves_order(self):
        target = mod.RequestScriptTarget(
            folder_path=(),
            request_name="Req",
            occurrences=(
                mod.ScriptOccurrence(
                    phase=mod.PHASE_PRE,
                    listen=mod.LISTEN_PRE,
                    scope="request",
                    json_path="item[0].event[0]",
                    folder_hierarchy=(),
                    request_name="Req",
                    event_index=0,
                    script_text='pm.variables.set("x", pm.iterationData.get("itemId"));',
                    line_count=1,
                    has_executable_code=True,
                ),
                mod.ScriptOccurrence(
                    phase=mod.PHASE_POST,
                    listen=mod.LISTEN_POST,
                    scope="request",
                    json_path="item[0].event[1]",
                    folder_hierarchy=(),
                    request_name="Req",
                    event_index=1,
                    script_text='pm.expect(pm.iterationData.get("region")).to.equal("us");',
                    line_count=1,
                    has_executable_code=True,
                ),
            ),
        )

        non_request_occ = mod.ScriptOccurrence(
            phase=mod.PHASE_UNKNOWN,
            listen="unknown",
            scope="collection",
            json_path="event[0]",
            folder_hierarchy=(),
            request_name=None,
            event_index=0,
            script_text='pm.expect(pm.iterationData.get("itemId")).to.exist;',
            line_count=1,
            has_executable_code=True,
        )

        names = mod.collect_iteration_data_variable_names([target], [non_request_occ])
        self.assertEqual(["itemId", "region"], names)



class ResponseCodeMappingRegressionTests(unittest.TestCase):
    class _FakeClient:
        def __init__(self):
            self.rest_client = {"id": "/id", "name": "Req", "misc": {}}

        def get_rest_client(self, _rest_client_id):
            return dict(self.rest_client)

        def update_rest_client(self, _rest_client_id, body):
            self.rest_client = body
            return {}

    def _build_occurrence(self, script_text):
        return mod.ScriptOccurrence(
            phase=mod.PHASE_POST,
            listen=mod.LISTEN_POST,
            scope="request",
            json_path="item[0].event[0]",
            folder_hierarchy=(),
            request_name="Req",
            event_index=0,
            script_text=script_text,
            line_count=1,
            has_executable_code=True,
        )

    def test_status_only_arrow_script_maps_response_code_without_omission_warning(self):
        client = self._FakeClient()
        occurrence = self._build_occurrence(
            'pm.test("Status is 200", () => pm.response.to.have.status(200));'
        )

        result = mod.apply_request_event(
            client=client,
            rest_client_id="/id",
            request_name="Req",
            occurrence=occurrence,
            conversion_mode=mod.SCRIPT_MODE_PREFER_NATIVE,
            storage_mode=mod.SCRIPT_STORAGE_EMBEDDED,
            script_language="JavaScript (OpenJDK Nashorn)",
            external_script_dir=None,
            script_variable_names=[],
        )

        self.assertTrue(result.updated_rest_client)
        self.assertEqual([], result.warnings)
        self.assertEqual("200", client.rest_client["misc"]["validHttpResponseCodes"]["fixed"])

    def test_array_include_status_script_maps_multi_response_codes(self):
        client = self._FakeClient()
        occurrence = self._build_occurrence(
            'pm.test("Status is 401 or 400", () => pm.expect([400,401]).to.include(pm.response.code));'
        )

        result = mod.apply_request_event(
            client=client,
            rest_client_id="/id",
            request_name="Req",
            occurrence=occurrence,
            conversion_mode=mod.SCRIPT_MODE_PREFER_NATIVE,
            storage_mode=mod.SCRIPT_STORAGE_EMBEDDED,
            script_language="JavaScript (OpenJDK Nashorn)",
            external_script_dir=None,
            script_variable_names=[],
        )

        self.assertTrue(result.updated_rest_client)
        self.assertEqual([], result.warnings)
        self.assertEqual("400,401", client.rest_client["misc"]["validHttpResponseCodes"]["fixed"])

if __name__ == "__main__":
    unittest.main()
