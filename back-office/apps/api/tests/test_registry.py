"""Unit tests for registry validation (pure logic, no network/db).

Uses the real actions.json / action_intent.schema.json field names
(action_name, target_system, action_inputs, missing_fields).
"""

import unittest

from services.registry import ActionRegistry


REGISTRY_DATA = {
    "version": "test",
    "actions": [
        {
            "action_name": "get_patient_demographics",
            "target_system": "both",
            "risk_level": 1,
            "requires_approval": False,
            "required_inputs": ["emr_id"],
            "implementation_status": "IMPLEMENTED_VENDORED",
        },
        {
            "action_name": "cancel_appointment",
            "target_system": "svigg",
            "risk_level": 2,
            "requires_approval": True,
            "required_inputs": ["encounter_id", "cancel_reason"],
            "implementation_status": "IMPLEMENTED_VENDORED",
        },
        {
            "action_name": "upload_patient_document",
            "target_system": "sis",
            "risk_level": 3,
            "requires_approval": True,
            "required_inputs": ["document"],
            "implementation_status": "BLOCKED_PENDING_REAL_SELECTOR_OR_CREDENTIALS",
        },
    ],
}


def _intent(**over):
    base = {
        "action_name": "get_patient_demographics",
        "target_system": "sis",
        "risk_level": 1,
        "requires_approval": False,
        "patient_identifiers": {"emr_id": "22041163"},
        "action_inputs": {},
        "missing_fields": [],
        "reason": "test",
    }
    base.update(over)
    return base


class RegistryValidationTest(unittest.TestCase):
    def setUp(self):
        self.registry = ActionRegistry.from_data(REGISTRY_DATA)

    def test_known_actions_loaded(self):
        self.assertEqual(
            self.registry.actions(),
            ["cancel_appointment", "get_patient_demographics", "upload_patient_document"],
        )

    def test_unknown_action_rejected(self):
        result = self.registry.validate(_intent(action_name="delete_everything"))
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "rejected")
        self.assertTrue(result.unknown_action)

    def test_missing_action_name_rejected(self):
        result = self.registry.validate({"target_system": "sis", "missing_fields": []})
        self.assertFalse(result.ok)
        self.assertIn("missing an 'action_name'", result.errors[0])

    def test_missing_fields_makes_intent_incomplete(self):
        result = self.registry.validate(
            _intent(
                action_name="cancel_appointment",
                target_system="svigg",
                risk_level=2,
                missing_fields=["encounter_id", "cancel_reason"],
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            result.missing_inputs, ["encounter_id", "cancel_reason"]
        )

    def test_valid_intent_accepted(self):
        result = self.registry.validate(_intent())
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.missing_inputs, [])
        self.assertIsNotNone(result.contract)
        self.assertTrue(result.implemented)

    def test_system_incompatible_rejected(self):
        # cancel_appointment is svigg-only; an sis intent is incompatible.
        result = self.registry.validate(
            _intent(
                action_name="cancel_appointment",
                target_system="sis",
                risk_level=2,
                missing_fields=[],
                action_inputs={"encounter_id": "E1", "cancel_reason": "or"},
            )
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("incompatible" in e for e in result.errors))

    def test_both_system_contract_accepts_concrete_system(self):
        # get_patient_demographics is 'both'; an sis intent is compatible.
        result = self.registry.validate(_intent(target_system="sis"))
        self.assertTrue(result.ok)

    def test_not_yet_implemented_flagged_but_registry_valid(self):
        result = self.registry.validate(
            _intent(
                action_name="upload_patient_document",
                target_system="sis",
                risk_level=3,
                action_inputs={"document": "x"},
                missing_fields=[],
            )
        )
        # Known + complete -> ok, but flagged as not yet implemented.
        self.assertTrue(result.ok)
        self.assertFalse(result.implemented)

    def test_mechanical_fallback_without_missing_fields(self):
        # An intent WITHOUT missing_fields falls back to required_inputs diff.
        result = self.registry.validate(
            {
                "action_name": "cancel_appointment",
                "target_system": "svigg",
                "action_inputs": {"encounter_id": "E1"},  # cancel_reason absent
            }
        )
        self.assertFalse(result.ok)
        self.assertIn("cancel_reason", result.missing_inputs)


if __name__ == "__main__":
    unittest.main()
