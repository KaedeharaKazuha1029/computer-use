import base64
import io
import tempfile
import threading
import wave

from windows_input import InputError


class Audio:
    def __init__(self):
        self.recording = None
        self.files = tempfile.TemporaryDirectory(prefix='local-cua-audio-')

    def start(self, duration):
        if self.recording:
            raise InputError('An audio recording is already active.')
        if type(duration) is not int or not 1 <= duration <= 300_000:
            raise InputError('max_duration_ms must be between 1 and 300000.')
        try:
            import pyaudiowpatch as pyaudio
            import numpy as np
        except ImportError as exc:
            raise InputError('Loopback audio requires PyAudioWPatch and NumPy.') from exc
        ready, stop = threading.Event(), threading.Event()
        state = {}

        def capture():
            try:
                with pyaudio.PyAudio() as audio:
                    device = audio.get_default_wasapi_loopback()
                    channels = min(2, int(device['maxInputChannels']))
                    rate = int(device['defaultSampleRate'])
                    if channels < 1:
                        raise InputError('The loopback device has no input channels.')
                    total = round(rate * duration / 1000)
                    remaining = total
                    frames = []
                    with audio.open(format=pyaudio.paInt16, channels=channels, rate=rate, input=True,
                                    input_device_index=device['index'], frames_per_buffer=1024) as stream:
                        ready.set()
                        while remaining and not stop.is_set():
                            count = min(1024, remaining)
                            frames.append(stream.read(count, exception_on_overflow=False))
                            remaining -= count
                    if remaining == total:
                        raise InputError('No audio was captured.')
                    samples = np.frombuffer(b''.join(frames), dtype='<i2').reshape(-1, channels)
                    positions = np.arange(round(len(samples) * 24000 / rate)) * rate / 24000
                    if not len(positions):
                        raise InputError('The recording was too short.')
                    indices = np.arange(len(samples))
                    stereo = np.stack([np.interp(positions, indices, samples[:, min(channel, channels - 1)])
                                       for channel in range(2)], axis=1).astype('<i2')
                    output = io.BytesIO()
                    with wave.open(output, 'wb') as wav:
                        wav.setparams((2, 2, 24000, 0, 'NONE', 'not compressed'))
                        wav.writeframes(stereo.tobytes())
                    state['data'] = output.getvalue()
            except Exception as exc:
                state['error'] = str(exc)
            finally:
                ready.set()

        worker = threading.Thread(target=capture, daemon=True)
        self.recording = worker, stop, state
        worker.start()
        if not ready.wait(5):
            stop.set()
            raise InputError('The loopback device did not start in time.')
        if 'error' in state:
            self.recording = None
            raise InputError(state['error'])

    def stop(self):
        if self.recording is None:
            raise InputError('There is no active audio recording.')
        worker, stop, state = self.recording
        stop.set()
        worker.join(timeout=5)
        if worker.is_alive():
            raise InputError('The loopback device timed out while stopping.')
        self.recording = None
        if 'error' in state:
            raise InputError(state['error'])
        with tempfile.NamedTemporaryFile(dir=self.files.name, suffix='.wav', delete=False) as output:
            output.write(state['data'])
        return {'filepath': output.name, 'data_url': 'data:audio/wav;base64,' + base64.b64encode(state['data']).decode('ascii')}

    def close(self):
        if self.recording:
            worker, stop, _ = self.recording
            stop.set()
            worker.join(timeout=2)
            self.recording = None
        self.files.cleanup()
