import socket
import threading
import json
import os
import hashlib
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# ==============================
# CONFIGURATION
# ==============================

MULTICAST_GROUP = "239.255.0.1"
DISCOVERY_PORT = 5000
TCP_PORT = 5001

BUFFER_SIZE = 64 * 1024

DEVICE_NAME = socket.gethostname()


# ==============================
# GET LOCAL IP
# ==============================

def get_local_ip():

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except:
        ip = "127.0.0.1"

    s.close()

    return ip


LOCAL_IP = get_local_ip()


# ==============================
# DEVICE STORAGE
# ==============================

devices = {}

devices_lock = threading.Lock()


# ==============================
# SHA256
# ==============================

def calculate_hash(filename):

    sha256 = hashlib.sha256()

    with open(filename, "rb") as f:

        while True:

            data = f.read(BUFFER_SIZE)

            if not data:
                break

            sha256.update(data)

    return sha256.hexdigest()


# ==============================
# UDP MULTICAST DISCOVERY
# ==============================

def discovery_listener():

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP
    )

    sock.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    sock.bind(("", DISCOVERY_PORT))

    multicast_request = socket.inet_aton(MULTICAST_GROUP) + \
                        socket.inet_aton("0.0.0.0")

    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_ADD_MEMBERSHIP,
        multicast_request
    )

    while True:

        try:

            data, address = sock.recvfrom(1024)

            message = json.loads(data.decode())

            if message["type"] != "DISCOVERY":
                continue

            device_ip = address[0]

            # Ignore ourselves
            if device_ip == LOCAL_IP:
                continue

            with devices_lock:

                devices[device_ip] = {
                    "name": message["name"],
                    "ip": device_ip,
                    "port": message["port"],
                    "last_seen": time.time()
                }

        except Exception as e:

            print("Discovery error:", e)


# ==============================
# SEND DISCOVERY MESSAGE
# ==============================

def send_discovery():

    sock = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM,
        socket.IPPROTO_UDP
    )

    sock.setsockopt(
        socket.IPPROTO_IP,
        socket.IP_MULTICAST_TTL,
        2
    )

    message = {
        "type": "DISCOVERY",
        "name": DEVICE_NAME,
        "port": TCP_PORT
    }

    data = json.dumps(message).encode()

    while True:

        try:

            sock.sendto(
                data,
                (MULTICAST_GROUP, DISCOVERY_PORT)
            )

        except Exception as e:

            print("Discovery send error:", e)

        time.sleep(2)


# ==============================
# TCP SERVER
# ==============================

def tcp_server():

    server = socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    server.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    server.bind(
        (LOCAL_IP, TCP_PORT)
    )

    server.listen(5)

    print(
        f"TCP server running on "
        f"{LOCAL_IP}:{TCP_PORT}"
    )

    while True:

        client, address = server.accept()

        print("Connection from:", address)

        thread = threading.Thread(
            target=handle_client,
            args=(client,)
        )

        thread.daemon = True
        thread.start()


# ==============================
# HANDLE TCP CLIENT
# ==============================

def handle_client(client):

    try:

        # Receive header

        header_data = client.recv(4096)

        header = json.loads(
            header_data.decode()
        )

        if header["type"] != "FILE":

            client.close()
            return

        filename = os.path.basename(
            header["filename"]
        )

        filesize = header["filesize"]

        file_hash = header["hash"]

        os.makedirs(
            "received",
            exist_ok=True
        )

        filepath = os.path.join(
            "received",
            filename
        )

        # Ask user

        root = tk.Tk()
        root.withdraw()

        answer = messagebox.askyesno(
            "Incoming File",
            f"{filename}\n\n"
            f"Size: {filesize / (1024 * 1024):.2f} MB\n\n"
            f"Accept file?"
        )

        root.destroy()

        if not answer:

            client.sendall(
                b"REJECT"
            )

            client.close()

            return

        client.sendall(
            b"ACCEPT"
        )

        received = 0

        with open(filepath, "wb") as f:

            while received < filesize:

                data = client.recv(
                    min(
                        BUFFER_SIZE,
                        filesize - received
                    )
                )

                if not data:
                    break

                f.write(data)

                received += len(data)

        client.close()

        # Verify hash

        calculated_hash = calculate_hash(
            filepath
        )

        if calculated_hash == file_hash:

            print(
                f"Received {filename} successfully"
            )

            messagebox.showinfo(
                "Transfer Complete",
                f"{filename}\n\n"
                f"Transfer successful!\n\n"
                f"SHA-256 verified."
            )

        else:

            print("Hash mismatch!")

            messagebox.showerror(
                "Error",
                "File integrity verification failed."
            )

    except Exception as e:

        print("Client error:", e)

        try:
            client.close()
        except:
            pass


# ==============================
# SEND FILE
# ==============================

def send_file(device):

    filepath = filedialog.askopenfilename()

    if not filepath:
        return

    filename = os.path.basename(filepath)

    filesize = os.path.getsize(filepath)

    print(
        f"Sending {filename} "
        f"({filesize} bytes)"
    )

    file_hash = calculate_hash(filepath)

    try:

        sock = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )

        sock.connect(
            (
                device["ip"],
                device["port"]
            )
        )

        header = {
            "type": "FILE",
            "filename": filename,
            "filesize": filesize,
            "hash": file_hash
        }

        sock.sendall(
            json.dumps(header).encode()
        )

        response = sock.recv(1024)

        if response != b"ACCEPT":

            print("Transfer rejected.")

            sock.close()

            return

        sent = 0

        with open(filepath, "rb") as f:

            while True:

                data = f.read(BUFFER_SIZE)

                if not data:
                    break

                sock.sendall(data)

                sent += len(data)

                percentage = (
                    sent / filesize
                ) * 100

                print(
                    f"\rSending: "
                    f"{percentage:.1f}%",
                    end=""
                )

        print()

        sock.close()

        messagebox.showinfo(
            "Transfer Complete",
            f"{filename}\n\n"
            f"Successfully sent."
        )

    except Exception as e:

        messagebox.showerror(
            "Transfer Error",
            str(e)
        )


# ==============================
# GUI
# ==============================

class AirDropGUI:

    def __init__(self, root):

        self.root = root

        root.title(
            "AirDrop Clone"
        )

        root.geometry(
            "600x500"
        )

        title = tk.Label(
            root,
            text="AirDrop Clone",
            font=("Arial", 24, "bold")
        )

        title.pack(
            pady=20
        )

        info = tk.Label(
            root,
            text=f"Device: {DEVICE_NAME}\n"
                 f"IP: {LOCAL_IP}",
            font=("Arial", 11)
        )

        info.pack(
            pady=10
        )

        tk.Label(
            root,
            text="Nearby Devices",
            font=("Arial", 16, "bold")
        ).pack(
            pady=10
        )

        self.device_frame = tk.Frame(root)

        self.device_frame.pack(
            fill="both",
            expand=True,
            padx=30
        )

        self.refresh_devices()

    def refresh_devices(self):

        for widget in self.device_frame.winfo_children():

            widget.destroy()

        with devices_lock:

            current_devices = list(
                devices.values()
            )

        if not current_devices:

            tk.Label(
                self.device_frame,
                text="No devices found...",
                font=("Arial", 12)
            ).pack(
                pady=20
            )

        for device in current_devices:

            frame = tk.Frame(
                self.device_frame,
                relief="solid",
                borderwidth=1,
                padx=10,
                pady=10
            )

            frame.pack(
                fill="x",
                pady=5
            )

            tk.Label(
                frame,
                text=f"🟢 {device['name']}",
                font=("Arial", 12, "bold")
            ).pack(
                side="left"
            )

            tk.Label(
                frame,
                text=device["ip"]
            ).pack(
                side="left",
                padx=20
            )

            tk.Button(
                frame,
                text="Send File",
                command=lambda d=device:
                    send_file(d)
            ).pack(
                side="right"
            )

        self.root.after(
            2000,
            self.refresh_devices
        )


# ==============================
# START APPLICATION
# ==============================

def main():

    threading.Thread(
        target=discovery_listener,
        daemon=True
    ).start()

    threading.Thread(
        target=send_discovery,
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