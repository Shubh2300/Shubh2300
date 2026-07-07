"""Unit tests for registry validation (pure logic, no network/db)."""

import unittest

from services.registry import ActionRegistry


REGISTRY_DATA = {
    "version": "test",
    "actions": [
        {
            "action": "sis.get_patient_demographics",
            "system": "sis",
            "endpoint": "/sis/patient/demographics",
            "method": "POST",
            "write": False,
            "required_inputs": ["patient_id"],
            "optional_inputs": ["as_of"],
        },
        {
            "action": "svigg.cancel_appointment",
            "system": "svigg",
            "endpoint": "/svigg/appointment/cancel",
            "method": "POST",
            "write": True,
            "inputs": [
                {"name": "encounter_id", "required": True},
                {"name": "cancel_reason", "required": True},
                {"name": "note", "required": False},
            ],
        },
    ],
}


class RegistryValidationTest(unittest.TestCase):
    def setUp(self):
        self.registry = ActionRegistry.from_data(REGISTRY_DATA)

    def test_known_actions_loaded(self):
        self.assertEqual(
            self.registry.actions(),
            ["sis.get_patient_demographics", "svigg.cancel_appointment"],
        )

    def test_unknown_action_rejected(self):
        result = self.registry.validate({"action": "sis.delete_everything", "inputs": {}})
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "rejected")
        self.assertTrue(result.unknown_action)

    def test_missing_action_rejected(self):
        result = self.registry.validate({"inputs": {}})
        self.assertFalse(result.ok)
        self.assertIn("missing an 'action'", result.errors[0])

    def test_missing_required_inputs_listed(self):
        result = self.registry.validate(
            {"action": "svigg.cancel_appointment", "inputs": {"note": "x"}}
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            sorted(result.missing_inputs), ["cancel_reason", "encounter_id"]
        )

    def test_empty_string_counts_as_missing(self):
        result = self.registry.validate(
            {
                "action": "sis.get_patient_demographics",
                "inputs": {"patient_id": ""},
            }
        )
        self.assertFalse(result.ok)
        self.assertIn("patient_id", result.missing_inputs)

    def test_valid_intent_accepted(self):
        result = self.registry.validate(
            {
                "action": "sis.get_patient_demographics",
                "system": "sis",
                "inputs": {"patient_id": "22041163"},
            }
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.missing_inputs, [])
        self.assertIsNotNone(result.contract)
        self.assertEqual(result.contract.endpoint, "/sis/patient/demographics")

    def test_system_mismatch_rejected(self):
        result = self.registry.validate(
            {
                "action": "sis.get_patient_demographics",
                "system": "svigg",
                "inputs": {"patient_id": "22041163"},
            }
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("does not match" in e for e in result.errors))

    def test_optional_inputs_not_required(self):
        # as_of is optional; absence must not fail validation.
        result = self.registry.validate(
            {
                "action": "sis.get_patient_demographics",
                "inputs": {"patient_id": "22041163"},
            }
        )
        self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()
