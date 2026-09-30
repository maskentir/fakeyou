# P0.5 preflight TLS fixtures (test-only, never production)

- `preflight-localhost.pem`: self-signed cert, CN=localhost, SAN DNS:localhost + IP:127.0.0.1
- `preflight-localhost-key.pem`: matching private key, no passphrase

Valid until Sep 2036. If the TLS stub test starts failing with certificate
errors, regenerate with:

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
  -keyout tests/fixtures/preflight-localhost-key.pem \
  -out tests/fixtures/preflight-localhost.pem
