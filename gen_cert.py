# -*- coding: utf-8 -*-
"""สร้างใบรับรอง HTTPS (self-signed) ให้ server — มือถือต้องใช้ https ถึงจะเปิดกล้องได้
ใช้:  python gen_cert.py 172.48.2.68          (ใส่ IP ของเครื่อง server ได้หลายตัว คั่นด้วยช่องว่าง)
ได้ไฟล์ cert/cert.pem, cert/key.pem และ cert/rm-delay-ca.crt (สำหรับติดตั้งในมือถือให้ไม่ขึ้นคำเตือน)
"""
import datetime, ipaddress, os, sys
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ips = sys.argv[1:] or ["127.0.0.1"]
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cert")
os.makedirs(out, exist_ok=True)
now = datetime.datetime.now(datetime.timezone.utc)

# 1) CA ของบริษัท (ติดตั้งในมือถือครั้งเดียว)
ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RM Delay Local CA")])
ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
      .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
      .not_valid_after(now + datetime.timedelta(days=3650))
      .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
      .sign(ca_key, hashes.SHA256()))

# 2) ใบรับรองของ server (อายุ 825 วัน ตามข้อกำหนดของ iPhone)
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
san = []
for v in ips:
    try:
        san.append(x509.IPAddress(ipaddress.ip_address(v)))
    except ValueError:
        san.append(x509.DNSName(v))
cert = (x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ips[0])]))
        .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256()))

with open(os.path.join(out, "key.pem"), "wb") as f:
    f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
with open(os.path.join(out, "cert.pem"), "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
with open(os.path.join(out, "rm-delay-ca.crt"), "wb") as f:
    f.write(ca.public_bytes(serialization.Encoding.DER))
# ให้ดาวน์โหลดใบ CA ไปติดตั้งในมือถือได้จาก https://<IP>:8443/rm-delay-ca.crt
static_ca = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "rm-delay-ca.crt")
with open(static_ca, "wb") as f:
    f.write(ca.public_bytes(serialization.Encoding.DER))
print("สร้างแล้ว:", out, "สำหรับ", ", ".join(ips))
