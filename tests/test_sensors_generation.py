import subprocess
from pathlib import Path


def test_sampler_reassignment_and_old_completion_preserve_the_successor() -> None:
    root = Path(__file__).parent.parent
    subprocess.run(
        [
            "node",
            str(root / "tests" / "perception_generation.mjs"),
            str(root / "src" / "fakeyou" / "assets" / "perception.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
