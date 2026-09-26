"""Sanitize the small diagnostic envelope; never accept arbitrary payloads."""
import hashlib
import re


def receipt_ref(key):
    return hashlib.sha256(key.encode()).hexdigest()[:16] if key else None


def clean_text(value, secrets=(), limit=512):
    if not isinstance(value, str):
        return None
    for secret in secrets:
        if isinstance(secret, str) and secret:
            value = value.replace(secret, "[redacted]")
    # Embedded objects may contain receipt contents or credentials, not prose.
    if any(char in value for char in '{}[]<>'):
        # Our own redaction marker is safe; any other object/markup is not.
        rest = value.replace('[redacted]', '')
        if any(char in rest for char in '{}[]<>'):
            return '[structured message omitted]'
    value = re.sub(r'(?i)(?:bearer\s+|(?:access[_ -]?token|refresh[_ -]?token|password|secret)\s*[:=]\s*)\S+',
                   '[redacted]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+', '[redacted]', value)
    value = re.sub(r'https?://\S+', '[redacted]', value)
    value = re.sub(r'\+?\d[\d\s().-]{7,}\d', '[redacted]', value)
    value = re.sub(r'[A-Za-z0-9_+/=-]{24,}(?:\.[A-Za-z0-9_+/=-]+)*', '[redacted]', value)
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value)
    return value[:limit]


FIELDS = {'operation', 'http_status', 'fns_code', 'message', 'error_type', 'attempt',
          'duration_ms', 'retry_in_seconds', 'receipt_ref', 'offset', 'page_size',
          'seen', 'downloaded', 'skipped', 'failed', 'pages', 'requests', 'retries',
          'phase', 'reason', 'exception_type', 'frames', 'next_sync_at', 'outcome', 'trigger'}


def clean_context(context):
    result = {}
    for key, value in context.items():
        if key not in FIELDS or value is None:
            continue
        if isinstance(value, str):
            result[key] = clean_text(value)
        elif isinstance(value, (int, float, bool)):
            result[key] = value
    return result
