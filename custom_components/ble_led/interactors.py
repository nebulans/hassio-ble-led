import abc
import logging
import math
import queue
import subprocess
import time
import threading

from btlewrap.bluepy import BluepyBackend
from btlewrap.base import BluetoothInterface, BluetoothBackendException

LOGGER = logging.getLogger(__name__)


class ActionFailed(IOError):
    pass


class RGBWInteractor(abc.ABC):
    control_handle = 0x0007
    rgb_offsets = [5*8, 4*8, 3*8]
    rgb_base = 0x5600000000f0aa
    white_offset = 2*8
    white_base = 0x56000000000faa

    def __init__(self, address):
        self.address = address

    @abc.abstractmethod
    def _write(self, value):
        raise NotImplemented

    def set_on(self):
        self._write(0xcc2333)

    def set_off(self):
        self._write(0xcc2433)

    def set_color(self, *values):
        self._write(self.rgb_base + sum(
            v << o for v, o in zip(values, self.rgb_offsets)
        ))

    def set_white(self, value):
        self._write(self.white_base + (value << self.white_offset))


class GATTToolRGBWInteractor(RGBWInteractor):
    executable = 'gatttool'

    def _write(self, value):
        LOGGER.warning(f'Sending value {value}')
        command = [self.executable, '-b', self.address, '--char-write-req', f'--handle=0x{self.control_handle:04x}', f'--value={value:0x}']
        LOGGER.warning(f'Command is: {command}')
        for n in range(5):
            try:
                subprocess.run(command, capture_output=True, check=True)
                break
            except subprocess.CalledProcessError as e:
                LOGGER.warning(f'Command failed attempt {n} - {e}\n{e.stdout}\n{e.stderr}')


class BtlewrapWorker(threading.Thread):
    """
    Controllers appear to sometimes get into states where making a connection
    can take an extremely large number of attempts.  Making keep-alive connections
    without sending any data on a regular basis seems to mitigate this.
    """

    def __init__(self, address, keepalive_interval, attempts, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.address = address
        self.keepalive_interval = keepalive_interval
        self.attempts = attempts
        self.interface = BluetoothInterface(BluepyBackend)
        self.queue = queue.Queue()
        self.failure_count = 0
        self.success_count = 0
        self.loop_count = 0
        self.empty_count = 0

    def run(self):
        while True:
            self.loop_count += 1
            try:
                event = self.queue.get(timeout=self.keepalive_interval)
            except queue.Empty:
                self.empty_count += 1
                event = None
            self.write(event)

    def write(self, event):
        for i in range(self.attempts):
            start = time.time()
            try:
                with self.interface.connect(self.address) as connection:
                    if event:
                        connection.write_handle(*event)
                self.success_count += 1
                break
            except BluetoothBackendException as e:
                LOGGER.info(f'Bluetooth connection failed: {e}')
                self.failure_count += 1
            elapsed = time.time() - start
            if elapsed > 10:
                LOGGER.info(f'Bluetooth connection took {elapsed}s')
        if i > 10:
            LOGGER.warning(f'Bluetooth connection to {self.address} took {i + 1} attempts')


class BtlewrapRGBWInteractor(RGBWInteractor):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.worker = BtlewrapWorker(self.address, keepalive_interval=2, attempts=100, daemon=True)
        self.worker.start()

    def _pack(self, value):
        num_bytes = math.ceil(value.bit_length() / 8)
        return value.to_bytes(num_bytes, byteorder='big')

    def _write(self, value):
        if not self.worker.is_alive():
            LOGGER.error(f'Worker for {self.address} is not alive!')
        LOGGER.info(f'Submitting to worker for {self.address}, queue length {self.worker.queue.qsize()}')
        LOGGER.info(f'Worker for {self.address}: {self.worker.loop_count} iterations, {self.worker.failure_count} failures')
        self.worker.queue.put((self.control_handle, self._pack(value)))
