<%!
def _render(value):
    """repr() with double quotes so the output matches ruff format."""
    if value is None:
        return "None"
    if isinstance(value, str):
        return '"%s"' % value
    if isinstance(value, tuple):
        return "(%s)" % ", ".join(_render(item) for item in value)
    return repr(value)
%>"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence
% if imports:

${imports}
% endif
% if upgrades or downgrades:

from alembic import op
% endif

revision: str = ${_render(up_revision)}
down_revision: str | Sequence[str] | None = ${_render(down_revision)}
branch_labels: str | Sequence[str] | None = ${_render(branch_labels)}
depends_on: str | Sequence[str] | None = ${_render(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
