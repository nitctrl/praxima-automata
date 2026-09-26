"""Wire the shared HTTP behaviour (request ids, global error handling) into an app."""

from fastapi import FastAPI

from praxima.entrypoints.http.errors import install_error_handlers
from praxima.entrypoints.http.middleware import RequestContextMiddleware


def install(app: FastAPI) -> None:
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware)
