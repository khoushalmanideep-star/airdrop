import threading
import tkinter as tk

from .config import APP_NAME, DISCOVERY_PORT, MULTICAST_GROUP, RECEIVED_DIR, TCP_PORT
from . import state
from .gui import AirDropGUI
from .networking import DEVICE_NAME, LOCAL_IP, cleanup_devices, discovery_listener, discovery_sender, tcp_server


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

    RECEIVED_DIR.mkdir(parents=True, exist_ok=True)

    threading.Thread(target=discovery_listener, daemon=True).start()
    threading.Thread(target=discovery_sender, daemon=True).start()
    threading.Thread(target=cleanup_devices, daemon=True).start()
    threading.Thread(target=tcp_server, daemon=True).start()

    root = tk.Tk()
    AirDropGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
