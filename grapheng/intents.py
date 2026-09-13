"""Compile natural-language goals into reviewable engineering intent."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ContractViolation

TASK_INTENT_SCHEMA_VERSION = 1
TASK_TEMPLATES = ("fix", "test", "refactor", "research", "release", "general")

_TEMPLATE_TERMS = {
    "fix": (
        "fix", "bug", "broken", "error", "failure", "regression", "修复", "故障",
        "报错", "错误", "异常", "崩溃", "回归",
    ),
    "release": (
        "release", "publish", "shipping", "changelog", "发布", "发版", "上线",
        "版本", "变更日志",
    ),
    "refactor": (
        "refactor", "cleanup", "restructure", "simplify", "重构", "整理", "简化",
        "解耦", "收敛",
    ),
    "test": (
        "test", "coverage", "assertion", "测试", "覆盖率", "用例", "断言",
    ),
    "research": (
        "research", "investigate", "compare", "evaluate", "analyze", "audit",
        "调研", "研究", "对比", "评估", "分析", "审计", "看看", "了解",
    ),
}

@dataclass(frozen=True)
class _TemplateDetails:
    """Fixed per-template expectations resolved before an intent is built."""

    deliverables: Tuple[str, ...]
    risk_focus: Tuple[str, ...]
    mutation_allowed: bool


_TEMPLATE_DETAILS: Mapping[str, _TemplateDetails] = {
    "fix": _TemplateDetails(
        deliverables=("root cause", "minimal fix", "regression coverage"),
        risk_focus=("reproduction evidence", "nearby regressions", "rollback safety"),
        mutation_allowed=True,
    ),
    "test": _TemplateDetails(
        deliverables=("behavioral tests", "failure evidence", "coverage rationale"),
        risk_focus=("false positives", "test isolation", "production behavior drift"),
        mutation_allowed=True,
    ),
    "refactor": _TemplateDetails(
        deliverables=("behavior-preserving change", "focused verification", "rollback point"),
        risk_focus=("public contracts", "hidden callers", "behavior drift"),
        mutation_allowed=True,
    ),
    "research": _TemplateDetails(
        deliverables=("evidence-backed findings", "options and tradeoffs", "recommended next step"),
        risk_focus=("source quality", "uncertainty", "unintended workspace changes"),
        mutation_allowed=False,
    ),
    "release": _TemplateDetails(
        deliverables=("release readiness findings", "verification evidence", "release blockers"),
        risk_focus=("version consistency", "artifact completeness", "rollback readiness"),
        mutation_allowed=True,
    ),
    "general": _TemplateDetails(
        deliverables=("small scoped change", "verification evidence", "rollback point"),
        risk_focus=("scope creep", "project contracts", "behavior regressions"),
        mutation_allowed=True,
    ),
}

_VAGUE_OBJECTIVES = {
    "fix it", "handle it", "improve it", "do it", "take a look", "处理一下", "修一下",
    "改一下", "优化一下", "看一下", "搞一下", "解决一下",
}


def _strings(value: Any, field: str, allow_empty: bool = True) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractViolation(f"task intent {field} must be an array")
    items = tuple(value)
    if not allow_empty and not items:
        raise ContractViolation(f"task intent {field} cannot be empty")
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ContractViolation(f"task intent {field} must contain non-empty strings")
    return tuple(item.strip() for item in items)


def _commands(value: Any) -> Tuple[Tuple[str, ...], ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractViolation("task intent verification_commands must be an array")
    commands = []
    for command in value:
        if not isinstance(command, (list, tuple)) or not command:
            raise ContractViolation("task intent verification commands cannot be empty")
        if any(not isinstance(item, str) or not item for item in command):
            raise ContractViolation("task intent verification commands must contain strings")
        commands.append(tuple(command))
    return tuple(commands)


@dataclass(frozen=True)
class ProjectProfile:
    kinds: Tuple[str, ...]
    markers: Tuple[str, ...]
    suggested_checks: Tuple[Tuple[str, ...], ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kinds": list(self.kinds),
            "markers": list(self.markers),
        }


@dataclass(frozen=True)
class TaskIntent:
    template: str
    objective: str
    constraints: Tuple[str, ...]
    project_kinds: Tuple[str, ...]
    project_markers: Tuple[str, ...]
    verification_commands: Tuple[Tuple[str, ...], ...]
    assumptions: Tuple[str, ...]
    deliverables: Tuple[str, ...]
    risk_focus: Tuple[str, ...]
    mutation_allowed: bool
    clarification_questions: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.template not in TASK_TEMPLATES:
            raise ContractViolation(f"unsupported task template: {self.template}")
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ContractViolation("task intent objective cannot be empty")
        if not isinstance(self.mutation_allowed, bool):
            raise ContractViolation("task intent mutation_allowed must be a boolean")
        for field in (
            "constraints", "project_kinds", "project_markers", "assumptions",
            "deliverables", "risk_focus", "clarification_questions",
        ):
            _strings(getattr(self, field), field)
        _commands(self.verification_commands)

    @property
    def needs_clarification(self) -> bool:
        return bool(self.clarification_questions)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": TASK_INTENT_SCHEMA_VERSION,
            "template": self.template,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "project": {
                "kinds": list(self.project_kinds),
                "markers": list(self.project_markers),
            },
            "verification_commands": [list(item) for item in self.verification_commands],
            "assumptions": list(self.assumptions),
            "deliverables": list(self.deliverables),
            "risk_focus": list(self.risk_focus),
            "mutation_allowed": self.mutation_allowed,
            "clarification_questions": list(self.clarification_questions),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskIntent":
        required = {
            "schema_version", "template", "objective", "constraints", "project",
            "verification_commands", "assumptions", "deliverables", "risk_focus",
            "mutation_allowed", "clarification_questions",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ContractViolation("task intent has an invalid contract")
        if value["schema_version"] != TASK_INTENT_SCHEMA_VERSION:
            raise ContractViolation("unsupported task intent schema_version")
        if not isinstance(value["template"], str) or not isinstance(
            value["objective"], str
        ):
            raise ContractViolation("task intent template and objective must be strings")
        project = value["project"]
        if not isinstance(project, dict) or set(project) != {"kinds", "markers"}:
            raise ContractViolation("task intent project has an invalid contract")
        return cls(
            template=value["template"],
            objective=value["objective"],
            constraints=_strings(value["constraints"], "constraints"),
            project_kinds=_strings(project["kinds"], "project.kinds", allow_empty=False),
            project_markers=_strings(project["markers"], "project.markers"),
            verification_commands=_commands(value["verification_commands"]),
            assumptions=_strings(value["assumptions"], "assumptions"),
            deliverables=_strings(value["deliverables"], "deliverables", allow_empty=False),
            risk_focus=_strings(value["risk_focus"], "risk_focus", allow_empty=False),
            mutation_allowed=value["mutation_allowed"],
            clarification_questions=_strings(
                value["clarification_questions"], "clarification_questions"
            ),
        )

    @classmethod
    def basic(
        cls,
        objective: str,
        verification_commands: Sequence[Sequence[str]],
    ) -> "TaskIntent":
        details = _TEMPLATE_DETAILS["general"]
        return cls(
            template="general",
            objective=objective.strip(),
            constraints=(),
            project_kinds=("policy-defined",),
            project_markers=(),
            verification_commands=tuple(tuple(item) for item in verification_commands),
            assumptions=("Task intent was supplied through the advanced engineering interface.",),
            deliverables=details.deliverables,
            risk_focus=details.risk_focus,
            mutation_allowed=True,
        )

    @classmethod
    def legacy(cls, objective: str) -> "TaskIntent":
        details = _TEMPLATE_DETAILS["general"]
        return cls(
            template="general",
            objective=objective.strip(),
            constraints=(),
            project_kinds=("legacy",),
            project_markers=(),
            verification_commands=(),
            assumptions=("Loaded from an engineering plan created before intent compilation.",),
            deliverables=details.deliverables,
            risk_focus=details.risk_focus,
            mutation_allowed=True,
        )


class IntentCompiler:
    """Hides template selection, project inspection and safe-default compilation."""

    def compile(
        self,
        objective: str,
        workspace: Path,
        check_commands: Optional[Sequence[Sequence[str]]] = None,
        template: Optional[str] = None,
        constraints: Sequence[str] = (),
    ) -> TaskIntent:
        if not isinstance(objective, str) or not objective.strip():
            raise ContractViolation("task objective cannot be empty")
        if template is not None and template not in TASK_TEMPLATES[:-1]:
            raise ContractViolation(f"unsupported task template: {template}")
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ContractViolation(f"task workspace does not exist: {workspace}")
        if isinstance(constraints, (str, bytes)):
            raise ContractViolation("task intent constraints must be an array")
        normalized_constraints = _strings(tuple(constraints), "constraints")
        selected = template or self._select_template(objective)
        profile = inspect_project(workspace)
        commands = (
            tuple(tuple(item) for item in check_commands)
            if check_commands is not None
            else profile.suggested_checks
        )
        commands = _commands(commands)
        details = _TEMPLATE_DETAILS[selected]
        questions = []
        if objective.strip().casefold().rstrip("。.!！?") in _VAGUE_OBJECTIVES:
            questions.append("What exact behavior, area, or outcome should this task address?")
        if not commands:
            if not details.mutation_allowed and (workspace / ".git").exists():
                commands = (("git", "diff", "--exit-code"),)
            else:
                questions.append(
                    "Which command proves this task is correct? Add a project policy or provide a recognizable test/build setup."
                )
        assumptions = [
            f"Selected the {selected} template from the objective."
            if template is None
            else f"Used the explicitly selected {selected} template.",
            "Project rules, protected paths, budgets, approval, and Reality Anchor remain mandatory.",
        ]
        if check_commands is None and commands:
            assumptions.append("Verification commands were inferred from project markers.")
        return TaskIntent(
            template=selected,
            objective=objective.strip(),
            constraints=normalized_constraints,
            project_kinds=profile.kinds,
            project_markers=profile.markers,
            verification_commands=commands,
            assumptions=tuple(assumptions),
            deliverables=details.deliverables,
            risk_focus=details.risk_focus,
            mutation_allowed=details.mutation_allowed,
            clarification_questions=tuple(questions),
        )

    @staticmethod
    def _select_template(objective: str) -> str:
        lowered = objective.casefold()
        scores = {
            name: sum(1 for term in terms if term in lowered)
            for name, terms in _TEMPLATE_TERMS.items()
        }
        highest = max(scores.values(), default=0)
        if highest == 0:
            return "general"
        return next(name for name in _TEMPLATE_TERMS if scores[name] == highest)


def inspect_project(workspace: Path) -> ProjectProfile:
    kinds: List[str] = []
    markers: List[str] = []
    checks: List[Tuple[str, ...]] = []

    package_json = workspace / "package.json"
    if package_json.is_file():
        kinds.append("node")
        markers.append("package.json")
        checks.extend(_node_checks(workspace, package_json))

    python_markers = tuple(
        name for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
        if (workspace / name).is_file()
    )
    if python_markers:
        kinds.append("python")
        markers.extend(python_markers)
        if (workspace / "pytest.ini").is_file() or _contains(
            workspace / "pyproject.toml", "[tool.pytest"
        ):
            checks.append(("python3", "-m", "pytest"))
        elif (workspace / "tests").is_dir():
            checks.append(("python3", "-m", "unittest", "discover", "-s", "tests"))

    if (workspace / "Cargo.toml").is_file():
        kinds.append("rust")
        markers.append("Cargo.toml")
        checks.append(("cargo", "test"))
    if (workspace / "go.mod").is_file():
        kinds.append("go")
        markers.append("go.mod")
        checks.append(("go", "test", "./..."))

    makefile = workspace / "Makefile"
    if not checks and makefile.is_file() and _contains(makefile, "test:"):
        kinds.append("make")
        markers.append("Makefile")
        checks.append(("make", "test"))

    if not kinds:
        kinds.append("generic")
    return ProjectProfile(
        kinds=tuple(dict.fromkeys(kinds)),
        markers=tuple(dict.fromkeys(markers)),
        suggested_checks=tuple(dict.fromkeys(checks)),
    )


def _node_checks(workspace: Path, package_json: Path) -> Tuple[Tuple[str, ...], ...]:
    try:
        package = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
    if not isinstance(scripts, dict):
        return ()
    prefix: Tuple[str, ...]
    if (workspace / "pnpm-lock.yaml").is_file():
        prefix = ("pnpm",)
    elif (workspace / "yarn.lock").is_file():
        prefix = ("yarn",)
    elif (workspace / "bun.lock").is_file() or (workspace / "bun.lockb").is_file():
        prefix = ("bun", "run")
    else:
        prefix = ("npm", "run")
    commands: List[Tuple[str, ...]] = []
    for name in ("test", "typecheck", "lint", "build"):
        script = scripts.get(name)
        if isinstance(script, str) and script.strip():
            if name == "test" and "no test specified" in script.casefold():
                continue
            commands.append((*prefix, name))
        if len(commands) == 2:
            break
    return tuple(commands)


def _contains(path: Path, needle: str) -> bool:
    try:
        return needle in path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
