class VestaDbError(Exception):
    """Bază pentru erorile de domeniu ale stratului de date."""


class SessionNotFound(VestaDbError):
    pass


class SessionClosed(VestaDbError):
    """Sesiunea e CANCELLED sau INTERRUPTED; nu mai acceptă acțiuni."""


class InvalidTransition(VestaDbError):
    pass


class StaleVersion(VestaDbError):
    """state_version nu mai corespunde: altcineva a modificat sesiunea între timp."""


class AlertStillActive(VestaDbError):
    """Sesiunea are o alertă activă; trebuie rezolvată explicit, nu anulată."""


class AlertNotFound(VestaDbError):
    pass


class InvalidLocation(VestaDbError):
    pass


class Unauthorized(VestaDbError):
    """Token lipsă, necunoscut, expirat sau revocat."""


class Forbidden(VestaDbError):
    """Token valid, dar fără drept pe sesiunea / acțiunea cerută."""
