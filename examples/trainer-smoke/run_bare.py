"""End-to-end smoke test of the hecaton trainer SDK from inside a container.

Required env (set by trainer-entrypoint.sh / docker run -e):
  HECATON_BROKER_URL  e.g. http://100.82.79.70:30443
  HECATON_TOKEN       shared bearer token
  HECATON_RUN_ID      identifier for this run
  TS_AUTHKEY          tag:trainer Tailscale auth key (used by entrypoint)
"""

import os

from hecaton_envs import SandboxProvider

provider = SandboxProvider.from_env(run_id=os.environ.get("HECATON_RUN_ID", "smoke"))

freed = provider.revoke()
print(f"revoke prior: {freed}")

sb = provider.acquire(template="python-runtime")
print(f"acquired: id={sb.id} host={sb.host} port={sb.port}")

# Sandbox /execute treats the command as argv, not a shell string —
# wrap in bash -c to use shell operators like `&&`.
result = sb.exec('bash -c \'uname -a && python3 -c "print(2+2)"\'')
print(f"exit_code: {result.exit_code}  duration: {result.duration_s:.2f}s")
print(f"stdout: {result.stdout!r}")
print(f"stderr: {result.stderr!r}")

provider.release(sb)
print("released")
