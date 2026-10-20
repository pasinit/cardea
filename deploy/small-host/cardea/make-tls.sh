#!/bin/sh
# Private CA and server certificate for the kernel's TLS listener (ADR-025).
# Usage: make-tls.sh <cardea-private-ip> <output-dir>. Needs OpenSSL 3. Rotate yearly; the
# service reads certificates at startup only.
#
# Extensions are explicit so that strict verifiers accept the chain: Python 3.13 clients
# enable VERIFY_X509_STRICT and reject a CA without key usage or an AKI-less leaf.
set -eu
ip="$1"; out="$2"
umask 077
mkdir -p "$out"
cnf="$out/openssl.cnf"
cat > "$cnf" <<CNF
[req]
distinguished_name = dn
prompt = no
[dn]
CN = cardea private CA
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = IP:$ip
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
CNF
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$out/ca.key"
openssl req -x509 -new -config "$cnf" -extensions ca -key "$out/ca.key" -days 1825 \
  -subj "/CN=cardea private CA" -out "$out/ca.crt"
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$out/server.key"
openssl req -new -config "$cnf" -key "$out/server.key" -subj "/CN=cardea kernel" \
  -out "$out/server.csr"
openssl x509 -req -in "$out/server.csr" -CA "$out/ca.crt" -CAkey "$out/ca.key" \
  -set_serial "0x$(openssl rand -hex 16)" -days 365 -extfile "$cnf" -extensions server \
  -out "$out/server.crt"
rm -f "$out/server.csr" "$cnf"
openssl verify -x509_strict -CAfile "$out/ca.crt" "$out/server.crt" >/dev/null
echo "Copy $out/ca.crt to the Telos host as cardea-ca.crt; keep ca.key offline."
