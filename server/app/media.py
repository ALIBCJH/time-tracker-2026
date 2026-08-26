"""Serving stored images in development.

In production S3 hands the browser a presigned URL and this blueprint is never
reached. Locally there is no S3, so the same signed-URL contract is honoured
here — the signature is checked, not waved through, so the gallery exercises
the real access path rather than a permissive stand-in that hides mistakes.
"""
from flask import Blueprint, abort, current_app, request, send_file
import io

bp = Blueprint('media', __name__)


@bp.get('/media/<path:key>')
def serve(key):
    """No login check, by design: the signature IS the authorisation, exactly as
    a presigned S3 URL works. It expires in minutes, so a link that leaks is
    worth nothing shortly afterwards."""
    store = current_app.storage
    verify = getattr(store, 'verify', None)
    if verify is None:
        abort(404)                       # S3 in use; nothing to serve here
    if not verify(key, request.args.get('expires'), request.args.get('sig')):
        abort(403)
    try:
        data = store.get(key)
    except (FileNotFoundError, ValueError):
        abort(404)
    return send_file(io.BytesIO(data), mimetype='image/webp',
                     max_age=300, conditional=False)
