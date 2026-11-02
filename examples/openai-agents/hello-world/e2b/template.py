from pathlib import Path

from e2b import Template

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]

template = (
    Template(file_context_path=EXAMPLE_ROOT)
    .from_python_image("3.12")
    .set_workdir("/workspace")
    .copy("e2b/requirements-runtime.txt", "/workspace/requirements-runtime.txt")
    .run_cmd(
        "python -m pip install --no-cache-dir --requirement "
        "/workspace/requirements-runtime.txt"
    )
    .copy("agent.py", "/workspace/agent.py", mode=0o500)
)
