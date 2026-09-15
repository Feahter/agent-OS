import unittest

from grapheng.state_machine import (
    TERMINAL_PHASES,
    ResidentTransition,
    project_execution_phase,
    resident_control_transition,
    settle_resident_phase,
)


class ResidentStateMachineTests(unittest.TestCase):
    def test_control_transitions_are_table_driven(self):
        cases = (
            ("pause", "queued", "graph", True, ResidentTransition("paused", "pause")),
            ("pause", "waiting", "graph", True, ResidentTransition("paused", "pause")),
            (
                "pause",
                "running",
                "graph",
                True,
                ResidentTransition("pause_requested", "pause"),
            ),
            ("resume", "paused", "graph", True, ResidentTransition("queued", None)),
            (
                "resume",
                "running",
                "graph",
                False,
                ResidentTransition("queued", None),
            ),
            (
                "cancel",
                "waiting",
                "orca",
                True,
                ResidentTransition("cancel_requested", "cancel"),
            ),
            (
                "cancel",
                "waiting",
                "graph",
                True,
                ResidentTransition("cancelled", "cancel"),
            ),
        )
        for action, state, kind, owner_alive, expected in cases:
            with self.subTest(action=action, state=state, kind=kind):
                self.assertEqual(
                    expected,
                    resident_control_transition(
                        state,
                        action,
                        kind=kind,
                        owner_alive=owner_alive,
                    ),
                )

    def test_illegal_and_terminal_control_transitions_are_rejected(self):
        self.assertIsNone(
            resident_control_transition(
                "running", "resume", kind="graph", owner_alive=True
            )
        )
        for phase in TERMINAL_PHASES:
            with self.subTest(phase=phase):
                self.assertIsNone(
                    resident_control_transition(
                        phase, "pause", kind="graph", owner_alive=True
                    )
                )

    def test_terminal_settlement_wins_over_late_nonterminal_projection(self):
        self.assertEqual("succeeded", settle_resident_phase("succeeded", "paused"))
        self.assertEqual("failed", settle_resident_phase("running", "failed"))
        self.assertEqual("paused", settle_resident_phase("pause_requested", "paused"))

    def test_real_execution_terminal_wins_over_pause_projection(self):
        for phase in TERMINAL_PHASES:
            with self.subTest(phase=phase):
                self.assertEqual(
                    phase,
                    project_execution_phase(phase, pause_requested=True),
                )
        self.assertEqual(
            "paused",
            project_execution_phase("paused", pause_requested=True),
        )


if __name__ == "__main__":
    unittest.main()
