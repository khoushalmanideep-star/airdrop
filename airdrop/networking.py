import json
import os
import socket
import threading
import time
import uuid
from tkinter import filedialog

from . import state
from .config import BUFFER_SIZE, DEVICE_TIMEOUT, DISCOVERY_INTERVAL, DISCOVERY_PORT, MULTICAST_GROUP, RECEIVED_DIR, TCP_PORT
from .events import post
from .state import active_lock, active_transfers, devices, devices_lock
from .utils import calculate_sha256, get_local_ip, recv_json, safe_filename, send_json, unique_destination

LOCAL_IP = get_local_ip()
DEVICE_NAME = socket.gethostname()


def create_multicast_sender():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    try:
        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_MULTICAST_IF,
            socket.inet_aton(LOCAL_IP)
        )
    except OSError:
        pass

    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        2
    )
    return sock


def discovery_sender():
    sock = create_multicast_sender()

    message = {
        "type": "DISCOVERY",
        "name": DEVICE_NAME,
        "port": TCP_PORT,
        "version": 1
    }

    data = json.dumps(message).encode("utf-8")

    while state.running:
        try:
            sock.sendto(data, (MULTICAST_GROUP, DISCOVERY_PORT))
        except OSError as e:
            post("log", message=f"Discovery send error: {e}")

        time.sleep(DISCOVERY_INTERVAL)

    sock.close()


def discovery_listener():
    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP
    )

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        sock.bind(("", DISCOVERY_PORT))

        membership = (
            socket.inet_aton(MULTICAST_GROUP)
            + socket.inet_aton("0.0.0.0")
        )

        sock.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            membership
        )

        sock.settimeout(1)

        while state.running:
            try:
                data, address = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

            if message.get("type") != "DISCOVERY":
                continue

            device_ip = address[0]

            if device_ip == LOCAL_IP:
                continue

            with devices_lock:
                devices[device_ip] = {
                    "name": message.get("name", "Unknown Device"),
                    "ip": device_ip,
                    "port": int(message.get("port", TCP_PORT)),
                    "last_seen": time.time()
                }

        sock.close()

    except OSError as e:
        post(
            "network_error",
            message=(
                "UDP multicast could not start.\n\n"
                f"{e}\n\n"
                "Check Windows Firewall and make sure both devices "
                "are on the same private network."
            )
        )


def cleanup_devices():
    while state.running:
        now = time.time()
        removed = []

        with devices_lock:
            for ip, device in list(devices.items()):
                if now - device["last_seen"] > DEVICE_TIMEOUT:
                    removed.append(ip)
                    del devices[ip]

        if removed:
            post("refresh_devices")

        time.sleep(2)


def tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind(("0.0.0.0", TCP_PORT))
        server.listen(20)
        server.settimeout(1)

        post("log", message=f"TCP server listening on {LOCAL_IP}:{TCP_PORT}")

        while state.running:
            try:
                client, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            threading.Thread(
                target=handle_incoming_connection,
                args=(client, address),
                daemon=True
            ).start()

    except OSError as e:
        post(
            "network_error",
            message=(
                f"Could not start TCP server on port {TCP_PORT}.\n\n{e}"
            )
        )

    finally:
        server.close()


def handle_incoming_connection(client, address):
    transfer_id = str(uuid.uuid4())[:8]

    try:
        client.settimeout(None)

        request = recv_json(client)

        if not request or request.get("type") != "FILE_REQUEST":
            client.close()
            return

        filename = safe_filename(request.get("filename", "file"))
        filesize = int(request.get("filesize", 0))
        file_hash = request.get("sha256", "")
        sender_name = request.get("sender_name", address[0])

        if filesize < 0 or filesize > 100 * 1024 * 1024 * 1024:
            send_json(client, {
                "type": "RESPONSE",
                "accepted": False,
                "reason": "Invalid file size."
            })
            client.close()
            return

        response_event = threading.Event()
        response_holder = {"accepted": False}

        post(
            "incoming_request",
            transfer_id=transfer_id,
            filename=filename,
            filesize=filesize,
            sender_name=sender_name,
            address=address[0],
            event=response_event,
            holder=response_holder
        )

        response_event.wait()

        if not response_holder["accepted"]:
            try:
                send_json(client, {
                    "type": "RESPONSE",
                    "accepted": False,
                    "reason": "Rejected by receiver."
                })
            finally:
                client.close()
            return

        send_json(client, {
            "type": "RESPONSE",
            "accepted": True
        })

        destination = unique_destination(RECEIVED_DIR, filename)
        temp_path = destination.with_suffix(destination.suffix + ".part")

        post(
            "transfer_started",
            transfer_id=transfer_id,
            direction="Receiving",
            filename=filename,
            total=filesize,
            peer=sender_name
        )

        received = 0
        start_time = time.time()
        last_ui_update = 0

        with open(temp_path, "wb") as f:
            while received < filesize:
                chunk = client.recv(min(BUFFER_SIZE, filesize - received))

                if not chunk:
                    raise ConnectionError("Sender disconnected before transfer completed.")

                f.write(chunk)
                received += len(chunk)

                now = time.time()
                if now - last_ui_update >= 0.15 or received == filesize:
                    elapsed = max(now - start_time, 0.001)
                    speed = received / elapsed

                    post(
                        "transfer_progress",
                        transfer_id=transfer_id,
                        done=received,
                        total=filesize,
                        speed=speed
                    )
                    last_ui_update = now

        client.close()

        post("status", message=f"Received {filename}. Verifying SHA-256...")

        actual_hash = calculate_sha256(str(temp_path))

        if actual_hash.lower() != file_hash.lower():
            try:
                send_json(client, {
                    "type": "TRANSFER_RESULT",
                    "success": False
                })
            except OSError:
                pass

            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

            post(
                "transfer_failed",
                transfer_id=transfer_id,
                filename=filename,
                reason="SHA-256 verification failed."
            )
            return

        temp_path.replace(destination)

        try:
            send_json(client, {
                "type": "TRANSFER_RESULT",
                "success": True
            })
        except OSError:
            pass

        post(
            "transfer_complete",
            transfer_id=transfer_id,
            filename=filename,
            path=str(destination),
            direction="Receiving"
        )

    except Exception as e:
        try:
            client.close()
        except OSError:
            pass

        post(
            "transfer_failed",
            transfer_id=transfer_id,
            filename="Incoming file",
            reason=str(e)
        )


def send_file(device):
    filepath = filedialog.askopenfilename(title="Select file to send")

    if not filepath:
        return

    threading.Thread(
        target=send_file_worker,
        args=(filepath, device),
        daemon=True
    ).start()


def send_file_worker(filepath, device):
    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)
    transfer_id = str(uuid.uuid4())[:8]
    cancel_event = threading.Event()

    sock = None

    try:
        post(
            "transfer_started",
            transfer_id=transfer_id,
            direction="Sending",
            filename=filename,
            total=filesize,
            peer=device["name"]
        )

        post("status", message=f"Calculating SHA-256 for {filename}...")

        file_hash = calculate_sha256(filepath)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)

        with active_lock:
            active_transfers[transfer_id] = {
                "socket": sock,
                "cancel": cancel_event
            }

        post("status", message=f"Connecting to {device['name']}...")

        sock.connect((device["ip"], device["port"]))

        send_json(sock, {
            "type": "FILE_REQUEST",
            "sender_name": DEVICE_NAME,
            "filename": filename,
            "filesize": filesize,
            "sha256": file_hash
        })

        response = recv_json(sock)

        if not response or not response.get("accepted"):
            reason = (
                response.get("reason", "Transfer rejected.")
                if response else "Receiver disconnected."
            )

            post(
                "transfer_failed",
                transfer_id=transfer_id,
                filename=filename,
                reason=reason
            )
            return

        sock.settimeout(None)

        sent = 0
        start_time = time.time()
        last_ui_update = 0

        with open(filepath, "rb") as f:
            while sent < filesize:
                if cancel_event.is_set():
                    raise InterruptedError("Transfer cancelled.")

                chunk = f.read(BUFFER_SIZE)
                if not chunk:
                    break

                sock.sendall(chunk)
                sent += len(chunk)

                now = time.time()

                if now - last_ui_update >= 0.15 or sent == filesize:
                    elapsed = max(now - start_time, 0.001)
                    speed = sent / elapsed

                    post(
                        "transfer_progress",
                        transfer_id=transfer_id,
                        done=sent,
                        total=filesize,
                        speed=speed
                    )

                    last_ui_update = now

        post("status", message=f"{filename} sent. Waiting for receiver verification...")

        sock.settimeout(15)
        result = recv_json(sock)

        if result and result.get("type") == "TRANSFER_RESULT":
            if result.get("success"):
                post(
                    "transfer_complete",
                    transfer_id=transfer_id,
                    filename=filename,
                    path=filepath,
                    direction="Sending"
                )
            else:
                post(
                    "transfer_failed",
                    transfer_id=transfer_id,
                    filename=filename,
                    reason="Receiver reported failed verification."
                )
        else:
            post(
                "transfer_complete",
                transfer_id=transfer_id,
                filename=filename,
                path=filepath,
                direction="Sending"
            )

    except InterruptedError:
        post(
            "transfer_failed",
            transfer_id=transfer_id,
            filename=filename,
            reason="Transfer cancelled."
        )

    except Exception as e:
        post(
            "transfer_failed",
            transfer_id=transfer_id,
            filename=filename,
            reason=str(e)
        )

    finally:
        with active_lock:
            active_transfers.pop(transfer_id, None)

        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def cancel_transfer(transfer_id):
    with active_lock:
        transfer = active_transfers.get(transfer_id)

        if not transfer:
            return

        transfer["cancel"].set()

        try:
            transfer["socket"].shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        try:
            transfer["socket"].close()
        except OSError:
            pass
