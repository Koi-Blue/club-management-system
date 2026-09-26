import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import zlib

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    rounds = 120_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return secrets.compare_digest(check, digest)
    except (ValueError, TypeError):
        return False


def issue_token(secret: str, user_id: int, ttl: int = 14 * 24 * 3600) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(json.dumps({"uid": user_id, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
    signing = f"{header}.{payload}".encode()
    signature = hmac.new(secret.encode(), signing, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


def read_user_id(secret: str, token: str | None) -> int | None:
    if not token or token.count(".") != 2:
        return None
    header, payload, signature = token.split(".")
    expected = _b64(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    try:
        if not hmac.compare_digest(expected, signature):
            return None
        data = json.loads(_unb64(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    try:
        if int(data.get("exp", 0)) < time.time():
            return None
        return int(data["uid"])
    except (TypeError, ValueError, KeyError):
        return None


def sign_captcha(secret: str, code: str) -> str:
    return URLSafeTimedSerializer(secret, salt="club-captcha").dumps(code)


def captcha_matches(secret: str, signed: str | None, given: str) -> bool:
    if not signed:
        return False
    try:
        expected = URLSafeTimedSerializer(secret, salt="club-captcha").loads(signed, max_age=300)
    except (BadSignature, SignatureExpired):
        return False
    return secrets.compare_digest(str(expected).upper(), (given or "").strip().upper())


def new_captcha() -> tuple[str, bytes]:
    code = f"{secrets.randbelow(10000):04d}"
    return code, _captcha_png(code)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


_FONT = {
    "0": (0b11111, 0b10001, 0b10001, 0b10001, 0b10001, 0b10001, 0b11111),
    "1": (0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110),
    "2": (0b11111, 0b00001, 0b00001, 0b11111, 0b10000, 0b10000, 0b11111),
    "3": (0b11111, 0b00001, 0b00001, 0b11111, 0b00001, 0b00001, 0b11111),
    "4": (0b10001, 0b10001, 0b10001, 0b11111, 0b00001, 0b00001, 0b00001),
    "5": (0b11111, 0b10000, 0b10000, 0b11111, 0b00001, 0b00001, 0b11111),
    "6": (0b11111, 0b10000, 0b10000, 0b11111, 0b10001, 0b10001, 0b11111),
    "7": (0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000),
    "8": (0b11111, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b11111),
    "9": (0b11111, 0b10001, 0b10001, 0b11111, 0b00001, 0b00001, 0b11111),
}


def _captcha_png(code: str) -> bytes:
    scale = 3
    gap = 8
    margin = 10
    width = margin * 2 + len(code) * (5 * scale + gap) - gap
    height = margin * 2 + 7 * scale + 6
    pixels = [[(244, 245, 247) for _ in range(width)] for _ in range(height)]
    for _ in range(80):
        x = secrets.randbelow(width)
        y = secrets.randbelow(height)
        tone = 180 + secrets.randbelow(50)
        pixels[y][x] = (tone, tone, tone)
    for index, char in enumerate(code):
        glyph = _FONT[char]
        ox = margin + index * (5 * scale + gap)
        oy = margin + secrets.randbelow(5)
        for row, bits in enumerate(glyph):
            for col in range(5):
                if bits & (1 << (4 - col)):
                    for dy in range(scale):
                        for dx in range(scale):
                            px = ox + col * scale + dx
                            py = oy + row * scale + dy
                            if 0 <= px < width and 0 <= py < height:
                                shade = 35 + secrets.randbelow(30)
                                pixels[py][px] = (shade, shade, shade)
    raw = b"".join(b"\x00" + bytes(channel for pixel in row for channel in pixel) for row in pixels)
    return _png(width, height, raw)


def _png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
