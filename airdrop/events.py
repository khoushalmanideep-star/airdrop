from .state import gui_queue


def post(event_type, **data):
    gui_queue.put((event_type, data))
