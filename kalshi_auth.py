"""RSA-PSS request signing for Kalshi API v2."""

import base64
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def load_private_key(path=None, key_string=None):
    """Load RSA private key from file path or PEM string."""
    if path:
        with open(path, 'rb') as f:
            key_data = f.read()
    elif key_string:
        key_data = key_string.replace('\\n', '\n').encode()
    else:
        raise ValueError("Must provide path or key_string")
    return serialization.load_pem_private_key(key_data, password=None)


def sign_request(private_key, key_id, method, path):
    """Create signed headers for Kalshi API v2."""
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    message = f"{timestamp}{method}{path.split('?')[0]}"
    signature = private_key.sign(
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )
    return {
        'KALSHI-ACCESS-KEY': key_id,
        'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode(),
        'KALSHI-ACCESS-TIMESTAMP': timestamp,
        'Content-Type': 'application/json',
    }
