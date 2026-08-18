import json
import tempfile
import unittest
from pathlib import Path

from grapheng import ContractViolation, IntentCompiler, TaskIntent, inspect_project


class IntentCompilerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.compiler = IntentCompiler()

    def tearDown(self):
        self.temporary.cleanup()

    def test_first_five_templates_are_selected_from_chinese_or_english_goals(self):
        checks = (("python3", "-m", "unittest"),)
        cases = {
            "修复登录崩溃并补回归用例": "fix",
            "增加边界条件测试": "test",
            "Refactor the routing module without behavior changes": "refactor",
            "调研三种缓存方案并给出建议": "research",
            "Prepare the next release and changelog": "release",
        }

        for objective, expected in cases.items():
            with self.subTest(objective=objective):
                intent = self.compiler.compile(
                    objective, self.workspace, check_commands=checks
                )
                self.assertEqual(expected, intent.template)

    def test_project_inspection_infers_bounded_node_and_python_checks(self):
        (self.workspace / "package.json").write_text(
            json.dumps(
                {
                    "scripts": {
                        "test": "vitest run",
                        "typecheck": "tsc --noEmit",
                        "lint": "eslint .",
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.workspace / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        (self.workspace / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\n", encoding="utf-8"
        )

        profile = inspect_project(self.workspace)

        self.assertEqual(("node", "python"), profile.kinds)
        self.assertEqual(
            (
                ("pnpm", "test"),
                ("pnpm", "typecheck"),
                ("python3", "-m", "pytest"),
            ),
            profile.suggested_checks,
        )

    def test_missing_critical_context_stops_before_agent_planning(self):
        intent = self.compiler.compile("处理一下", self.workspace)

        self.assertTrue(intent.needs_clarification)
        self.assertEqual(2, len(intent.clarification_questions))

    def test_research_uses_read_only_git_verification_without_project_policy(self):
        (self.workspace / ".git").mkdir()

        intent = self.compiler.compile("调研本地模块关系", self.workspace)

        self.assertEqual("research", intent.template)
        self.assertFalse(intent.mutation_allowed)
        self.assertEqual((("git", "diff", "--exit-code"),), intent.verification_commands)
        self.assertFalse(intent.needs_clarification)

    def test_intent_contract_rejects_unknown_or_malformed_values(self):
        intent = self.compiler.compile(
            "修复 parser 错误",
            self.workspace,
            check_commands=(("python3", "-m", "unittest"),),
            constraints=("Do not change the public interface",),
        )
        value = intent.to_dict()
        value["template"] = "invented"

        with self.assertRaisesRegex(ContractViolation, "unsupported task template"):
            TaskIntent.from_dict(value)
        with self.assertRaisesRegex(ContractViolation, "constraints must be an array"):
            self.compiler.compile(
                "修复 parser 错误",
                self.workspace,
                check_commands=(("python3", "-m", "unittest"),),
                constraints="unsafe string",
            )


if __name__ == "__main__":
    unittest.main()
