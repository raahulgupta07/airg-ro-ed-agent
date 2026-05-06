#!/bin/bash
# Generate self-signed cert for local development
set -e
SSL_DIR="$(dirname "$0")/../nginx/ssl"
mkdir -p "$SSL_DIR"
if [ -f "$SSL_DIR/cert.pem" ]; then
    echo "Cert already exists at $SSL_DIR/cert.pem"
    exit 0
fi
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$SSL_DIR/key.pem" \
    -out "$SSL_DIR/cert.pem" \
    -subj "/CN=localhost/O=RO-ED Local Dev" \
    -addext "subjectAltName=DNS:localhost,DNS:ro-ed.local,IP:127.0.0.1"
chmod 600 "$SSL_DIR/key.pem"
echo "Generated cert: $SSL_DIR/cert.pem"
echo "  Browser will warn about self-signed — click Advanced → Proceed."
