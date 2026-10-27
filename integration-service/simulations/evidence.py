"""The evidence template every simulation fills in."""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Evidence:
    title: str
    covers: str
    initial_state: Any = None
    request: Any = None
    response: Any = None
    fineract_state: Any = None
    erp_state: Any = None
    integration_state: Any = None
    expected: str = ""
    actual: str = ""
    explanation: str = ""
    checks: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def check(self, name: str, condition: bool) -> None:
        self.checks[name] = condition

    def to_markdown(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        rows = [
            ("1. Initial state", self.initial_state),
            ("2. Request", self.request),
            ("3. Response", self.response),
            ("4. Fineract state", self.fineract_state),
            ("5. ERP state", self.erp_state),
            ("6. Integration state", self.integration_state),
            ("7. Expected result", self.expected),
            ("8. Actual result", self.actual),
            ("9. Explanation", self.explanation),
        ]
        parts = [f"## {self.title} — {verdict}", "", f"*Covers: {self.covers}*", ""]
        for label, value in rows:
            parts.append(f"**{label}**")
            parts.append("")
            parts.append(_render(value))
            parts.append("")
        failed = [name for name, ok in self.checks.items() if not ok]
        parts.append(
            f"Checks: {len(self.checks) - len(failed)}/{len(self.checks)} passed"
            + (f" (failed: {failed})" if failed else "")
        )
        return "\n".join(parts) + "\n"


def _render(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, str):
        return value
    return "```json\n" + json.dumps(value, indent=2, default=str) + "\n```"
