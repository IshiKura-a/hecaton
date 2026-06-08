class BrokerError(Exception):
    """Broker returned a non-2xx response."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"broker {status}: {detail}")
        self.status = status
        self.detail = detail


class SandboxExecError(Exception):
    """Reached the sandbox pod but the request itself failed.

    A successful HTTP call that returned a non-zero `exit_code` is NOT
    raised; it is returned via `ExecResult`.
    """
