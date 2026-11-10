"""Send a sample ESC/POS receipt to the virtual printer."""

import socket
import sys

ESC, GS = b"\x1b", b"\x1d"


def qr(data: bytes) -> bytes:
    store = data
    n = len(store) + 3
    return (
        GS + b"(k\x03\x00\x31\x43\x06"  # module size 6
        + GS + b"(k\x03\x00\x31\x45\x31"  # error level M
        + GS + b"(k" + bytes([n & 0xFF, n >> 8]) + b"\x31\x50\x30" + store
        + GS + b"(k\x03\x00\x31\x51\x30"
    )


def raster_checker(width_bytes: int = 16, height: int = 48) -> bytes:
    rows = b"".join(
        (b"\xf0" if (y // 8) % 2 == 0 else b"\x0f") * width_bytes for y in range(height)
    )
    return GS + b"v0\x00" + bytes([width_bytes, 0, height, 0]) + rows


receipt = b"".join([
    ESC + b"@",
    ESC + b"a\x01", GS + b"!\x11", b"SWIFT STORE\n", GS + b"!\x00",
    b"123 Main Street\n", b"Tel. 555-0100\n\n",
    ESC + b"a\x00",
    b"Item                       Qty    Price\n",
    b"-" * 48 + b"\n",
    b"Coffee                       2     6.00\n",
    b"Bagel                        1     3.50\n",
    ESC + b"E\x01", b"TOTAL                              9.50\n", ESC + b"E\x00",
    ESC + b"-\x01", b"Underlined", ESC + b"-\x00", b" ", GS + b"B\x01", b" Reverse ", GS + b"B\x00", b"\n",
    ESC + b"M\x01", b"Font B small text line\n", ESC + b"M\x00",
    ESC + b"t\x10", b"Caf\xe9 cr\xe8me\n",
    ESC + b"a\x01",
    raster_checker(),
    GS + b"h\x50" + GS + b"H\x02" + GS + b"k\x49\x0c{B1234567890",
    qr(b"https://example.com/receipt/42"),
    b"\nThank you!\n",
    ESC + b"d\x03",
    GS + b"V\x42\x00",
])

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9100
    with socket.create_connection((host, port)) as sock:
        sock.sendall(receipt)
    print(f"Sent {len(receipt)} bytes to {host}:{port}")
