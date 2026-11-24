"""Capture one HWND through Windows Graphics Capture in a disposable process."""

import sys
from threading import Event

from PIL import Image
from windows_capture import WindowsCapture


def capture(hwnd):
    ready = Event()
    frames = []
    session = WindowsCapture(window_hwnd=hwnd, cursor_capture=False)

    @session.event
    def on_frame_arrived(frame, control):
        try:
            if not frames and frame.width * frame.height <= 40_000_000:
                frames.append(Image.frombytes('RGB', (frame.width, frame.height),
                                              frame.frame_buffer.tobytes(), 'raw', 'BGRX'))
        finally:
            control.stop()
            ready.set()

    @session.event
    def on_closed():
        ready.set()

    control = session.start_free_threaded()
    if ready.wait(1.5) and frames:
        frames[0].save(sys.stdout.buffer, 'PNG')
        sys.stdout.buffer.flush()
    if not control.is_finished():
        control.stop()


if __name__ == '__main__':
    capture(int(sys.argv[1]))
