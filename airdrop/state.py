import queue
import threading


devices = {}

devices_lock = threading.Lock()

gui_queue = queue.Queue()

running = True

active_transfers = {}
active_lock = threading.Lock()
