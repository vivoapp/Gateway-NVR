"""The one error shape of the API (contract section 9)."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

MESSAGES = {
    "invalid_argument": "The request is not valid.",
    "unauthenticated": "A valid grant is required.",
    "forbidden": "The grant does not allow this.",
    "camera_not_found": "No camera with this id is on this gateway.",
    "not_found": "Not found, or expired.",
    "no_recording": "Nothing is recorded in this range.",
    "conflict": "Not possible in the current state.",
    "range_too_long": "The time range is too long.",
    "codec_unsupported": "The client cannot play this stream's codec.",
    "limit_reached": "A limit is reached. Try again later.",
    "upstream_unavailable": "The recorder or the stream service did not answer.",
    "disk_near_limit": "The recording disk is at its safe limit.",
}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None, **extra):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message or MESSAGES.get(code, code)
        self.extra = extra

    def response(self) -> JSONResponse:
        body = {"error": {"code": self.code, "message": self.message}}
        body.update(self.extra)
        headers = {"WWW-Authenticate": "Bearer"} if self.status == 401 else None
        return JSONResponse(body, status_code=self.status, headers=headers)


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return exc.response()

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query", "path"))
        msg = f"Invalid {where}." if where else MESSAGES["invalid_argument"]
        return ApiError(400, "invalid_argument", msg).response()

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            return ApiError(404, "not_found").response()
        if exc.status_code == 405:
            return JSONResponse({"error": {"code": "invalid_argument", "message": "Method not allowed."}},
                                status_code=405)
        return JSONResponse({"error": {"code": "invalid_argument", "message": str(exc.detail)}},
                            status_code=exc.status_code)
