import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def main() -> int:
    from pdm_memory import verify

    payload = json.loads(sys.stdin.read() or "{}")
    intent = payload.get("intent", "")
    goals = payload.get("goals", [])
    torsion_threshold = payload.get("torsion_threshold")

    kwargs = {}
    if torsion_threshold is not None:
        kwargs["torsion_threshold"] = torsion_threshold

    report = verify(intent, goals, **kwargs)
    result = report.as_dict()
    result["is_safe_to_act"] = report.status == "ALIGNED"
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
