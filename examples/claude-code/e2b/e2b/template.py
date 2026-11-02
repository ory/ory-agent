from pathlib import Path

from e2b import Template

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
ORY_CLAUDE_VERSION = "1.4.7"

template = (
    Template(file_context_path=EXAMPLE_ROOT)
    .from_template("claude")
    .set_workdir("/workspace")
    .copy("greet.mjs", "/workspace/greet.mjs", mode=0o555)
    .run_cmd(
        "npx -y -p @ory/claude-code@"
        + ORY_CLAUDE_VERSION
        + " ory-claude install --global"
    )
    .run_cmd(
        "claude plugin list && npx -y -p @ory/claude-code@"
        + ORY_CLAUDE_VERSION
        + " ory-claude version"
    )
)
