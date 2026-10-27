import time

from fudan_gateway.auth import RequestVerifier, sign_request, signature


def test_signed_request_is_single_use_and_bound_to_path():
    key = b"fixture-key-material-only-00000000"
    verifier = RequestVerifier(key)
    header = sign_request(key, "GET", "/v1/connect/server")
    assert not verifier.verify(header, "GET", "/v1/connect/other")
    assert verifier.verify(header, "GET", "/v1/connect/server")
    assert not verifier.verify(header, "GET", "/v1/connect/server")


def test_expired_and_tampered_authentication_rejected():
    key = b"fixture-key-material-only-00000000"
    timestamp = int(time.time()) - 120
    nonce = "00" * 16
    header = f"v1:{timestamp}:{nonce}:{signature(key, 'GET', '/x', timestamp, nonce)}"
    assert not RequestVerifier(key).verify(header, "GET", "/x")
    assert not RequestVerifier(key).verify(sign_request(key, "GET", "/x")[:-1] + "z", "GET", "/x")
    assert not RequestVerifier(key).verify("malformed", "GET", "/x")
