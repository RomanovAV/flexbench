"""Run one frozen Harbor job independently of the browser/server lifetime."""

import asyncio
import json
from pathlib import Path
import sys
import traceback

from .cli import require_harbor, run_job


def write_state(directory, **state):
    temporary = directory / "launcher-state.tmp"
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(directory / "launcher-state.json")


def main(config_path, experiment=False):
    directory = Path(config_path).parent
    write_state(directory, state="running", returncode=None)
    code = 2
    try:
        require_harbor()
        if experiment:
            from .experiment import run_experiment
            code = asyncio.run(run_experiment(directory))
        else:
            from harbor.models.job.config import JobConfig
            config = JobConfig.model_validate_json(Path(config_path).read_text(encoding="utf-8"))
            code = asyncio.run(run_job(config))
    except KeyboardInterrupt:
        code = 130
    except Exception:
        traceback.print_exc()
    finally:
        write_state(directory, state="finished", returncode=code)
    return code


if __name__ == "__main__":
    if sys.argv[1:2] == ["--experiment"]:
        raise SystemExit(main(sys.argv[2], experiment=True))
    raise SystemExit(main(sys.argv[1]))
