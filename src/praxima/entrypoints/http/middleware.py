"""Request id propagation and last-resort error safety for every request."""

import logging
import re
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from praxima.entrypoints.http.errors import internal_error
from praxima.shared import context

logger = logging.getLogger(__name__)
REQUEST_ID_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"[A-Za-z0-9._-]{8,64}")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Set a request id and turn any unhandled exception into a generic 500.

    Handling crashes here (not by re-raising) keeps exception messages, which may contain
    user input, out of server logs. Only the exception type and request id are logged.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming if _VALID_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = context.request_id.set(request_id)
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.error("Unhandled %s (request_id=%s)", type(exc).__name__, request_id)
            response = internal_error()
        finally:
            context.request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
