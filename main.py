import os
import sys
import json
import time
import socket
import struct
import hashlib
import threading
import queue
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ============================================================
# AirDrop Clone - LAN File Sharing
# UDP Multicast = device discovery
# TCP = reliable file transfer
# ============================================================

APP_NAME = "AirDrop Clone"

MULTICAST_GROUP = "239.255.0.1"
DISCOVERY_PORT = 5000
TCP_PORT = 5001

DISCOVERY_INTERVAL = 2
DEVICE_TIMEOUT = 6
BUFFER_SIZE = 64 * 1024
SOCKET_TIMEOUT = 2
RECEIVED_DIR = Path("received")

# -----------------------------
# Global state
# -----------------------------

devices = {}          # ip -> {name, ip, port, last_seen}
devices_lock = threading.Lock()

gui_queue = queue.Queue()

running = True

# transfer_id -> {"socket": socket, "cancel": Event}
active_transfers = {}
active_lock = threading.Lock()


# ============================================================
# Utility functions
# ============================================================

def get_local_ip():
    """Get the IPv4 address used by this machine on the LAN."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No data is sent; this lets the OS select the active interface.
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


LOCAL_IP = get_local_ip()
DEVICE_NAME = socket.gethostname()


def safe_filename(name):
    """Prevent path traversal and invalid filenames."""
    name = os.path.basename(name)
    if not name:
        name = "received_file"
    return name


def unique_destination(directory, filename):
    """Avoid overwriting an existing file."""
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


# ============================================================
# GUI event helpers
# ============================================================

def post(event_type, **data):
    gui_queue.put((event_type, data))


# ============================================================
# UDP multicast discovery
# ============================================================

def create_multicast_sender():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    try:
        # Tell the OS which interface should send multicast traffic.
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

    while running:
        try:
            sock.sendto(
                data,
                (MULTICAST_GROUP, DISCOVERY_PORT)
            )
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
        # Windows accepts binding to all interfaces for multicast.
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

        while running:
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

            # Don't list ourselves.
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
    while running:
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


# ============================================================
# TCP server / incoming transfer
# ============================================================

def tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        # 0.0.0.0 means listen on all local IPv4 interfaces.
        server.bind(("0.0.0.0", TCP_PORT))
        server.listen(20)
        server.settimeout(1)

        post(
            "log",
            message=f"TCP server listening on {LOCAL_IP}:{TCP_PORT}"
        )

        while running:
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

        # Ask GUI/main Tk thread for user approval.
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

        # Wait for GUI response.
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

        destination = unique_destination(
            RECEIVED_DIR,
            filename
        )

        temp_path = destination.with_suffix(
            destination.suffix + ".part"
        )

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
                chunk = client.recv(
                    min(BUFFER_SIZE, filesize - received)
                )

                if not chunk:
                    raise ConnectionError(
                        "Sender disconnected before transfer completed."
                    )

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

        post(
            "status",
            message=f"Received {filename}. Verifying SHA-256..."
        )

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


# ============================================================
# TCP outgoing transfer
# ============================================================

def send_file(device):
    filepath = filedialog.askopenfilename(
        title="Select file to send"
    )

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

        post(
            "status",
            message=f"Calculating SHA-256 for {filename}..."
        )

        file_hash = calculate_sha256(filepath)

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )
        sock.settimeout(10)

        with active_lock:
            active_transfers[transfer_id] = {
                "socket": sock,
                "cancel": cancel_event
            }

        post(
            "status",
            message=f"Connecting to {device['name']}..."
        )

        sock.connect(
            (device["ip"], device["port"])
        )

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

        post(
            "status",
            message=f"{filename} sent. Waiting for receiver verification..."
        )

        # Receiver sends final verification result.
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
            # For compatibility if receiver closes after successful write.
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


# ============================================================
# GUI
# ============================================================

class AirDropGUI:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("900x700")
        self.root.minsize(760, 600)

        self.transfer_rows = {}

        self.build_styles()
        self.build_ui()

        self.root.after(100, self.process_gui_queue)
        self.root.after(1000, self.refresh_devices)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def build_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Title.TLabel",
            font=("Segoe UI", 26, "bold")
        )

        style.configure(
            "Subtitle.TLabel",
            font=("Segoe UI", 10)
        )

        style.configure(
            "Device.TButton",
            font=("Segoe UI", 10, "bold")
        )

    def build_ui(self):
        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)

        ttk.Label(
            main,
            text="AirDrop Clone",
            style="Title.TLabel"
        ).pack(anchor="w")

        ttk.Label(
            main,
            text=(
                f"Device: {DEVICE_NAME}    "
                f"IP: {LOCAL_IP}    "
                f"Multicast: {MULTICAST_GROUP}:{DISCOVERY_PORT}"
            ),
            style="Subtitle.TLabel"
        ).pack(anchor="w", pady=(4, 15))

        # Status
        self.status_var = tk.StringVar(
            value="Ready • Waiting for nearby devices..."
        )

        status_frame = ttk.Frame(main)
        status_frame.pack(fill="x", pady=(0, 15))

        ttk.Label(
            status_frame,
            textvariable=self.status_var
        ).pack(side="left")

        ttk.Button(
            status_frame,
            text="Refresh",
            command=self.refresh_devices
        ).pack(side="right")

        # Devices
        device_box = ttk.LabelFrame(
            main,
            text="Nearby Devices",
            padding=10
        )
        device_box.pack(fill="both", expand=True)

        columns = ("name", "ip", "port", "action")
        self.device_tree = ttk.Treeview(
            device_box,
            columns=columns,
            show="headings",
            height=8
        )

        self.device_tree.heading("name", text="Device")
        self.device_tree.heading("ip", text="IP Address")
        self.device_tree.heading("port", text="Port")
        self.device_tree.heading("action", text="Action")

        self.device_tree.column("name", width=260)
        self.device_tree.column("ip", width=180)
        self.device_tree.column("port", width=80)
        self.device_tree.column("action", width=130)

        self.device_tree.pack(
            fill="both",
            expand=True
        )

        self.device_tree.bind(
            "<Double-1>",
            self.send_selected_file
        )

        ttk.Label(
            device_box,
            text="Double-click a device to select a file and send it."
        ).pack(anchor="w", pady=(8, 0))

        # Transfers
        transfer_box = ttk.LabelFrame(
            main,
            text="Transfers",
            padding=10
        )
        transfer_box.pack(
            fill="both",
            expand=True,
            pady=(15, 0)
        )

        transfer_columns = (
            "direction",
            "file",
            "peer",
            "progress",
            "speed",
            "status"
        )

        self.transfer_tree = ttk.Treeview(
            transfer_box,
            columns=transfer_columns,
            show="headings",
            height=8
        )

        headings = {
            "direction": "Direction",
            "file": "File",
            "peer": "Device",
            "progress": "Progress",
            "speed": "Speed",
            "status": "Status"
        }

        widths = {
            "direction": 90,
            "file": 220,
            "peer": 150,
            "progress": 100,
            "speed": 100,
            "status": 160
        }

        for col in transfer_columns:
            self.transfer_tree.heading(
                col,
                text=headings[col]
            )
            self.transfer_tree.column(
                col,
                width=widths[col]
            )

        self.transfer_tree.pack(
            fill="both",
            expand=True
        )

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(10, 0))

        ttk.Button(
            bottom,
            text="Cancel Selected Transfer",
            command=self.cancel_selected_transfer
        ).pack(side="left")

        ttk.Label(
            bottom,
            text="Files received in: ./received"
        ).pack(side="right")

    # -------------------------
    # Devices
    # -------------------------

    def refresh_devices(self):
        selected = self.device_tree.selection()

        for item in self.device_tree.get_children():
            self.device_tree.delete(item)

        with devices_lock:
            current = list(devices.values())

        current.sort(key=lambda d: d["name"].lower())

        for device in current:
            self.device_tree.insert(
                "",
                "end",
                iid=device["ip"],
                values=(
                    "🟢 " + device["name"],
                    device["ip"],
                    device["port"],
                    "Double-click to send"
                )
            )

        if current:
            self.status_var.set(
                f"{len(current)} nearby device(s) found."
            )
        else:
            self.status_var.set(
                "No nearby devices found. "
                "Make sure both devices are on the same network."
            )

    def send_selected_file(self, event=None):
        selection = self.device_tree.selection()

        if not selection:
            return

        ip = selection[0]

        with devices_lock:
            device = devices.get(ip)

        if device:
            send_file(device)

    # -------------------------
    # Transfers
    # -------------------------

    def add_transfer(self, data):
        tid = data["transfer_id"]

        self.transfer_rows[tid] = {
            "direction": data["direction"],
            "filename": data["filename"],
            "peer": data["peer"],
            "total": data["total"]
        }

        self.transfer_tree.insert(
            "",
            "end",
            iid=tid,
            values=(
                data["direction"],
                data["filename"],
                data["peer"],
                "0%",
                "0 B/s",
                "Transferring"
            )
        )

    def update_transfer(self, data):
        tid = data["transfer_id"]

        if tid not in self.transfer_rows:
            return

        total = max(data["total"], 1)
        done = data["done"]

        percentage = min(
            100,
            int(done * 100 / total)
        )

        speed = human_speed(
            data.get("speed", 0)
        )

        values = self.transfer_tree.item(
            tid,
            "values"
        )

        if not values:
            return

        self.transfer_tree.item(
            tid,
            values=(
                values[0],
                values[1],
                values[2],
                f"{percentage}%",
                speed,
                "Transferring"
            )
        )

    def complete_transfer(self, data):
        tid = data["transfer_id"]

        if tid in self.transfer_rows:
            values = self.transfer_tree.item(
                tid,
                "values"
            )

            self.transfer_tree.item(
                tid,
                values=(
                    values[0],
                    values[1],
                    values[2],
                    "100%",
                    values[4],
                    "✓ Complete"
                )
            )

        self.status_var.set(
            f"Transfer complete: {data['filename']}"
        )

        messagebox.showinfo(
            "Transfer Complete",
            f"{data['filename']}\n\n"
            f"Transfer completed successfully.\n"
            f"SHA-256 verified."
        )

    def fail_transfer(self, data):
        tid = data["transfer_id"]

        if tid in self.transfer_rows:
            values = self.transfer_tree.item(
                tid,
                "values"
            )

            self.transfer_tree.item(
                tid,
                values=(
                    values[0],
                    values[1],
                    values[2],
                    values[3],
                    values[4],
                    "✗ " + data["reason"]
                )
            )

        self.status_var.set(
            f"Transfer failed: {data['reason']}"
        )

    def cancel_selected_transfer(self):
        selection = self.transfer_tree.selection()

        if not selection:
            return

        tid = selection[0]
        cancel_transfer(tid)

    # -------------------------
    # Incoming request
    # -------------------------

    def incoming_request(self, data):
        size = human_size(data["filesize"])

        accepted = messagebox.askyesno(
            "Incoming File",
            (
                f"Device: {data['sender_name']}\n"
                f"IP: {data['address']}\n\n"
                f"File: {data['filename']}\n"
                f"Size: {size}\n\n"
                f"Accept this file?"
            )
        )

        data["holder"]["accepted"] = accepted
        data["event"].set()

    # -------------------------
    # Event processing
    # -------------------------

    def process_gui_queue(self):
        try:
            while True:
                event_type, data = gui_queue.get_nowait()

                if event_type == "refresh_devices":
                    self.refresh_devices()

                elif event_type == "log":
                    self.status_var.set(data["message"])

                elif event_type == "status":
                    self.status_var.set(data["message"])

                elif event_type == "network_error":
                    self.status_var.set("Network error")
                    messagebox.showerror(
                        "Network Error",
                        data["message"]
                    )

                elif event_type == "incoming_request":
                    self.incoming_request(data)

                elif event_type == "transfer_started":
                    self.add_transfer(data)

                elif event_type == "transfer_progress":
                    self.update_transfer(data)

                elif event_type == "transfer_complete":
                    self.complete_transfer(data)

                elif event_type == "transfer_failed":
                    self.fail_transfer(data)

        except queue.Empty:
            pass

        self.root.after(
            100,
            self.process_gui_queue
        )

    def close(self):
        global running
        running = False

        with active_lock:
            for transfer in active_transfers.values():
                transfer["cancel"].set()
                try:
                    transfer["socket"].close()
                except OSError:
                    pass

        self.root.destroy()


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 55)
    print(APP_NAME)
    print("=" * 55)
    print(f"Device : {DEVICE_NAME}")
    print(f"IP     : {LOCAL_IP}")
    print(f"UDP    : {MULTICAST_GROUP}:{DISCOVERY_PORT}")
    print(f"TCP    : {LOCAL_IP}:{TCP_PORT}")
    print(f"Files  : {RECEIVED_DIR.resolve()}")
    print("=" * 55)

    RECEIVED_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    threading.Thread(
        target=discovery_listener,
        daemon=True
    ).start()

    threading.Thread(
        target=discovery_sender,
        daemon=True
    ).start()

    threading.Thread(
        target=cleanup_devices,
        daemon=True
    ).start()

    threading.Thread(
        target=tcp_server,
        daemon=True
    ).start()

    root = tk.Tk()
    AirDropGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
