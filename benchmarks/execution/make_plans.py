"""Generate campaign plans once and certify their declared hashes.

Existing plans are verified without regeneration. New plans are generated in a
temporary directory and published only after verification. Failed generation or
a mismatching hash never replaces a plan or writes a new checksum certificate.
The specification supplies argv as a list, never shell command text.
"""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def make_plans(scenario, output):
    scenario, output = Path(scenario).resolve(), Path(output).resolve()
    specification = json.loads((scenario / "plans.spec.json").read_text())
    generator = scenario / specification["generator"]
    if not generator.is_file():
        raise ValueError(f"missing generator: {generator}")
    output.mkdir(parents=True, exist_ok=True)
    for plan in specification["plans"]:
        name = plan["name"]
        if Path(name).name != name or name in (".", ".."):
            raise ValueError(f"plan name must be a filename: {name}")
        target = output / name
        expected = plan["sha256"]
        with tempfile.TemporaryDirectory(prefix=".plan-", dir=output) as temporary:
            candidate = target if target.exists() else Path(temporary) / name
            if not target.exists():
                subprocess.run([sys.executable, str(generator), "--out", str(candidate),
                                "--seed", str(plan["seed"]), *plan["args"]],
                               cwd=scenario, check=True, stdout=subprocess.DEVNULL)
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"{name}: expected {expected}, got {actual}")
            if candidate != target:
                # Exclusive creation also refuses a concurrently published plan.
                with target.open("xb") as stream:
                    stream.write(candidate.read_bytes())
            certificate = output / (name + ".sha256")
            certificate.write_text(f"{expected}  {name}\n")
        print(f"{name}: verified {actual}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        make_plans(arguments.scenario, arguments.out)
    except (ValueError, OSError, subprocess.CalledProcessError) as failure:
        parser.exit(6, f"Plan certification failed: {failure}\n")


if __name__ == "__main__":
    main()
