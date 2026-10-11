import queue
import tkinter as tk
from tkinter import ttk, messagebox

from . import state
from .config import APP_NAME, DISCOVERY_PORT, MULTICAST_GROUP
from .networking import DEVICE_NAME, LOCAL_IP, cancel_transfer, devices, devices_lock, send_file
from .state import active_lock, active_transfers, gui_queue
from .utils import human_size, human_speed


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

        style.configure("Title.TLabel", font=("Segoe UI", 26, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
        style.configure("Device.TButton", font=("Segoe UI", 10, "bold"))

    def build_ui(self):
        main = ttk.Frame(self.root, padding=20)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="AirDrop Clone", style="Title.TLabel").pack(anchor="w")

        ttk.Label(
            main,
            text=(
                f"Device: {DEVICE_NAME}    "
                f"IP: {LOCAL_IP}    "
                f"Multicast: {MULTICAST_GROUP}:{DISCOVERY_PORT}"
            ),
            style="Subtitle.TLabel"
        ).pack(anchor="w", pady=(4, 15))

        self.status_var = tk.StringVar(value="Ready • Waiting for nearby devices...")

        status_frame = ttk.Frame(main)
        status_frame.pack(fill="x", pady=(0, 15))

        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left")
        ttk.Button(status_frame, text="Refresh", command=self.refresh_devices).pack(side="right")

        device_box = ttk.LabelFrame(main, text="Nearby Devices", padding=10)
        device_box.pack(fill="both", expand=True)

        columns = ("name", "ip", "port", "action")
        self.device_tree = ttk.Treeview(device_box, columns=columns, show="headings", height=8)

        self.device_tree.heading("name", text="Device")
        self.device_tree.heading("ip", text="IP Address")
        self.device_tree.heading("port", text="Port")
        self.device_tree.heading("action", text="Action")

        self.device_tree.column("name", width=260)
        self.device_tree.column("ip", width=180)
        self.device_tree.column("port", width=80)
        self.device_tree.column("action", width=130)

        self.device_tree.pack(fill="both", expand=True)
        self.device_tree.bind("<Double-1>", self.send_selected_file)

        ttk.Label(device_box, text="Double-click a device to select a file and send it.").pack(anchor="w", pady=(8, 0))

        transfer_box = ttk.LabelFrame(main, text="Transfers", padding=10)
        transfer_box.pack(fill="both", expand=True, pady=(15, 0))

        transfer_columns = ("direction", "file", "peer", "progress", "speed", "status")

        self.transfer_tree = ttk.Treeview(transfer_box, columns=transfer_columns, show="headings", height=8)

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
            self.transfer_tree.heading(col, text=headings[col])
            self.transfer_tree.column(col, width=widths[col])

        self.transfer_tree.pack(fill="both", expand=True)

        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(10, 0))

        ttk.Button(bottom, text="Cancel Selected Transfer", command=self.cancel_selected_transfer).pack(side="left")
        ttk.Label(bottom, text="Files received in: ./received").pack(side="right")

    def refresh_devices(self):
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
            self.status_var.set(f"{len(current)} nearby device(s) found.")
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
        percentage = min(100, int(done * 100 / total))
        speed = human_speed(data.get("speed", 0))

        values = self.transfer_tree.item(tid, "values")

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
            values = self.transfer_tree.item(tid, "values")
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

        self.status_var.set(f"Transfer complete: {data['filename']}")
        messagebox.showinfo(
            "Transfer Complete",
            f"{data['filename']}\n\n"
            f"Transfer completed successfully.\n"
            f"SHA-256 verified."
        )

    def fail_transfer(self, data):
        tid = data["transfer_id"]

        if tid in self.transfer_rows:
            values = self.transfer_tree.item(tid, "values")
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

        self.status_var.set(f"Transfer failed: {data['reason']}")

    def cancel_selected_transfer(self):
        selection = self.transfer_tree.selection()
        if not selection:
            return

        tid = selection[0]
        cancel_transfer(tid)

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
                    messagebox.showerror("Network Error", data["message"])
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

        self.root.after(100, self.process_gui_queue)

    def close(self):
        state.running = False

        with active_lock:
            for transfer in active_transfers.values():
                transfer["cancel"].set()
                try:
                    transfer["socket"].close()
                except OSError:
                    pass

        self.root.destroy()
