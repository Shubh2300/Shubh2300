"""Unit tests for the approval state machine + service.

Pure logic with an in-memory store (test isolation, not mock patient data).
"""

import unittest
from datetime import datetime, timezone

from services.approvals import (
    ApprovalEvent,
    ApprovalService,
    ApprovalState,
    ApprovalStateMachine,
    InMemoryApprovalStore,
    InvalidTransition,
)


class StateMachineTest(unittest.TestCase):
    def test_happy_path_transitions(self):
        s = ApprovalState.proposed
        s = ApprovalStateMachine.next_state(s, ApprovalEvent.submit)
        self.assertEqual(s, ApprovalState.pending_approval)
        s = ApprovalStateMachine.next_state(s, ApprovalEvent.approve)
        self.assertEqual(s, ApprovalState.approved)
        s = ApprovalStateMachine.next_state(s, ApprovalEvent.start)
        self.assertEqual(s, ApprovalState.executing)
        s = ApprovalStateMachine.next_state(s, ApprovalEvent.verify)
        self.assertEqual(s, ApprovalState.verified)
        self.assertTrue(ApprovalStateMachine.is_terminal(s))

    def test_reject_path(self):
        s = ApprovalStateMachine.next_state(
            ApprovalState.pending_approval, ApprovalEvent.reject
        )
        self.assertEqual(s, ApprovalState.rejected)
        self.assertTrue(ApprovalStateMachine.is_terminal(s))

    def test_executing_can_go_to_needs_human_review(self):
        s = ApprovalStateMachine.next_state(
            ApprovalState.executing, ApprovalEvent.needs_review
        )
        self.assertEqual(s, ApprovalState.needs_human_review)

    def test_illegal_transition_raises(self):
        with self.assertRaises(InvalidTransition):
            ApprovalStateMachine.next_state(
                ApprovalState.proposed, ApprovalEvent.approve
            )
        with self.assertRaises(InvalidTransition):
            # cannot approve something already verified
            ApprovalStateMachine.next_state(
                ApprovalState.verified, ApprovalEvent.approve
            )

    def test_cannot_start_before_approval(self):
        self.assertFalse(
            ApprovalStateMachine.can(
                ApprovalState.pending_approval, ApprovalEvent.start
            )
        )


class ApprovalServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryApprovalStore()
        fixed = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
        self.service = ApprovalService(self.store, clock=lambda: fixed)
        self.fixed = fixed

    def _submit(self):
        return self.service.submit(
            approval_id="appr_1",
            action="sis.get_patient_demographics",
            intent={"action": "sis.get_patient_demographics", "inputs": {}},
            submitted_by="staff@clinic",
        )

    def test_submit_creates_pending(self):
        rec = self._submit()
        self.assertEqual(rec.state, ApprovalState.pending_approval)
        self.assertEqual(self.service.list_pending()[0].id, "appr_1")

    def test_approve_records_who_and_when(self):
        self._submit()
        rec = self.service.approve("appr_1", approver_user_id="nurse@clinic")
        self.assertEqual(rec.state, ApprovalState.approved)
        self.assertEqual(rec.approver_user_id, "nurse@clinic")
        self.assertEqual(rec.decided_at, self.fixed)
        # no longer pending
        self.assertEqual(self.service.list_pending(), [])

    def test_reject_records_who_and_when(self):
        self._submit()
        rec = self.service.reject(
            "appr_1", approver_user_id="nurse@clinic", reason="wrong patient"
        )
        self.assertEqual(rec.state, ApprovalState.rejected)
        self.assertEqual(rec.approver_user_id, "nurse@clinic")
        self.assertEqual(rec.decided_at, self.fixed)

    def test_full_lifecycle_to_verified(self):
        self._submit()
        self.service.approve("appr_1", "nurse@clinic")
        self.service.mark_executing("appr_1")
        rec = self.service.mark_outcome("appr_1", ApprovalEvent.verify)
        self.assertEqual(rec.state, ApprovalState.verified)

    def test_cannot_approve_twice(self):
        self._submit()
        self.service.approve("appr_1", "nurse@clinic")
        with self.assertRaises(InvalidTransition):
            self.service.approve("appr_1", "other@clinic")

    def test_approve_missing_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.service.approve("nope", "nurse@clinic")

    def test_mark_outcome_rejects_non_outcome_event(self):
        self._submit()
        with self.assertRaises(InvalidTransition):
            self.service.mark_outcome("appr_1", ApprovalEvent.approve)


if __name__ == "__main__":
    unittest.main()
