import hashlib
import json
import os
import socket
import struct
from pathlib import Path

from .config import BUFFER_SIZE, DISCOVERY_PORT, MULTICAST_GROUP


def get_local_ip():
    """Get the IPv4 address used by this machine on the LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        try:
            s.connect((MULTICAST_GROUP, DISCOVERY_PORT))
            ip = s.getsockname()[0]
        except OSError:
            ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def safe_filename(name):
    """Prevent path traversal and invalid filenames."""
    name = os.path.basename(name)
    if not name:
        name = "received_file"
    return name


def unique_destination(directory, filename):
    """Avoid overwriting an existing file."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename

    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix

    for i in range(1, 100000):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError("Could not create a unique filename.")


def calculate_sha256(filepath, progress_callback=None):
    sha = hashlib.sha256()
    total = os.path.getsize(filepath)
    done = 0

    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(BUFFER_SIZE)
            if not chunk:
                break
            sha.update(chunk)
            done += len(chunk)
            if progress_callback and total:
                progress_callback(done, total)

    return sha.hexdigest()


def send_json(sock, obj):
    """Length-prefixed JSON message."""
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    header = struct.pack("!I", len(data))
    sock.sendall(header + data)


def recv_exact(sock, n):
    """Receive exactly n bytes or return None if connection closes."""
    chunks = []
    remaining = n

    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)

    return b"".join(chunks)


def recv_json(sock):
    header = recv_exact(sock, 4)
    if header is None:
        return None

    size = struct.unpack("!I", header)[0]

    if size > 1024 * 1024:
        raise ValueError("Message too large.")

    data = recv_exact(sock, size)
    if data is None:
        return None

    return json.loads(data.decode("utf-8"))


def human_size(size):
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size)

    for unit in units:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024

    return f"{size:.1f} PB"


def human_speed(bytes_per_second):
    return f"{human_size(bytes_per_second)}/s"
