"""Smoke test for the r2egym scaffold: acquire + invoke a tool.

Same env vars as run_bare.py. Additionally requires r2egym installed
in the trainer environment (the adapter uses it to build the Action
object).

Verifies:
  1. acquire(scaffold="r2egym") returns a handle with scaffold set
  2. requirements.txt was installed (chardet) before handle returned
  3. /opt/agent-tools/file_editor exists, readonly, executable
  4. sb.invoke() dispatches through R2EGymAdapter -> bash command
     -> sandbox /execute -> tool runs -> stdout comes back
"""

import os
import time

from hecaton_envs import SandboxProvider
from r2egym.agenthub.action.action import Action

provider = SandboxProvider.from_env(run_id=os.environ.get("HECATON_RUN_ID", "scaffold-smoke"))

freed = provider.revoke()
print(f"revoke prior: {freed}")

sb = provider.acquire(template="python-runtime", scaffold="r2egym")
print(f"acquired: id={sb.id} host={sb.host} port={sb.port} scaffold={sb.scaffold!r}")

# (1) mount visible inside the pod
ls = sb.exec("ls -la /opt/agent-tools")
print(f"--- ls /opt/agent-tools (exit={ls.exit_code}) ---")
print(ls.stdout)

# (2) chardet was installed at acquire time. Scaffold deps live in
# /tmp/scaffold-deps (not the sandbox image's site-packages — the
# sandbox runs as a non-root user with no HOME), so any raw exec that
# wants them must prepend PYTHONPATH. invoke() does this automatically;
# we mimic it here just to confirm the install landed.
chardet_check = sb.exec(
    "env PYTHONPATH=/tmp/scaffold-deps "
    "python3 -c 'import chardet; print(chardet.__version__)'"
)
print(f"chardet version (exit={chardet_check.exit_code}): {chardet_check.stdout.strip()}")

# (3) invoke through the scaffold adapter — exercises the full path:
#     Action -> R2EGymAdapter.render -> /opt/agent-tools/file_editor.py ...
#     -> /execute -> R2EGymAdapter.parse -> ExecResult
# file_editor refuses non-.py paths (hardcoded by the upstream tool),
# so we plant a .py file we own. /tmp is writable by the sandbox uid;
# /app would also work but only on the python-runtime-sandbox image.
sb.exec("bash -c 'echo \"print(42)\" > /tmp/probe.py'")
act = Action(function_name="file_editor", parameters={"command": "view", "path": "/tmp/probe.py"})
result = sb.invoke(act)
print(f"--- file_editor view /tmp/probe.py (exit={result.exit_code}) ---")
print(result.stdout)
if result.stderr:
    print(f"stderr: {result.stderr}")

# Hold so Grafana panels have something to render for one scrape cycle.
print("holding sandbox for 60s")
time.sleep(60)

provider.release(sb)
print("released")
