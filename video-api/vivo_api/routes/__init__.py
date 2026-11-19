"""API routes, one module per area."""
from ..models import ErrorResponse

# Descriptions are spelled out so openapi.json does not depend on the Python version's wording.
_DESCRIPTIONS = {
    400: "invalid_argument",
    401: "unauthenticated: no grant, or the grant failed a check",
    403: "forbidden: the grant does not cover this camera, permission or object",
    404: "camera_not_found, not_found or no_recording",
    409: "conflict",
    413: "range_too_long",
    422: "codec_unsupported",
    429: "limit_reached (the body names the limit)",
    502: "upstream_unavailable",
    503: "disk_near_limit",
}
ERRORS = {code: {"model": ErrorResponse, "description": text} for code, text in _DESCRIPTIONS.items()}
