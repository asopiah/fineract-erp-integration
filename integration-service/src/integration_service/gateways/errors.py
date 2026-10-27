"""What a failed call means for the money, which decides what is safe to do next."""


class GatewayError(Exception):
    def __init__(self, system: str, message: str, *, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(f"{system}: {message}")
        self.system = system
        self.message = message
        self.status_code = status_code
        self.code = code


class NotAppliedError(GatewayError):
    """The request certainly had no effect: it was never delivered, or refused before processing. Safe to resend."""


class RejectedError(GatewayError):
    """The request was processed and refused on its merits. Resending the same request will not help."""


class OutcomeUnknownError(GatewayError):
    """The request may or may not have taken effect (timeout, dropped connection, server error).

    Never resend blindly: look the operation up by its external reference first.
    """
