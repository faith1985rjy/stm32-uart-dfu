import argparse
import json
import time
import zlib
from threading import Thread

from stm32uartdfu import Stm32UartDfu, DfuException


class ProgressBar:
    _BAR_MAX_LEN = 40
    _ENDLESS_BAR_LEN = 20

    def __init__(self, endless: bool = False):
        self._endless = endless
        self._position = 0
        self._bar_len = 0
        self._reverse_direction = False

    def _complete_len(self, progress):
        return int(self._BAR_MAX_LEN * progress / 100)

    def _incomplete_len(self, progress):
        return self._BAR_MAX_LEN - self._complete_len(progress)

    def _print(self, progress=None):
        if progress == -1:
            print(f'\r[{"-"*self._BAR_MAX_LEN}] failed.')
        elif progress == 100:
            print(f'\r[{"█"*self._BAR_MAX_LEN}] done.')
        else:
            if self._endless:
                tail = self._BAR_MAX_LEN - self._bar_len - self._position
                print(
                    f'\r[{" "*self._position}{"█"*self._bar_len}'
                    f'{" "*tail}] ...',
                    end='')
            else:
                print(
                    f'\r[{"█"*self._complete_len(progress)}'
                    f'{" "*self._incomplete_len(progress)}] {progress}%',
                    end='')

    def is_endless(self):
        return self._endless

    def update(self, progress=None):
        if self._endless:
            if self._reverse_direction:
                if self._position > 0:
                    self._position -= 1
                    if self._bar_len < self._ENDLESS_BAR_LEN:
                        self._bar_len += 1
                elif self._bar_len > 0:
                    self._bar_len -= 1
                else:
                    self._reverse_direction = False
            else:
                if not self._position and self._bar_len < self._ENDLESS_BAR_LEN:
                    self._bar_len += 1
                elif self._position + self._bar_len < self._BAR_MAX_LEN:
                    self._position += 1
                elif self._bar_len > 0:
                    self._bar_len -= 1
                    self._position += 1
                else:
                    self._reverse_direction = True

        self._print(progress)


class ProgressBarThread(Thread):
    _WAKE_PERIOD = 0.2

    def __init__(self, endless=False):
        super().__init__(target=self._run)
        self._bar = ProgressBar(endless)
        self._progress = None if endless else 0
        super().start()

    def _run(self):
        while True:
            self._bar.update(self._progress)
            if self._progress == 100 or self._progress == -1:
                break
            time.sleep(self._WAKE_PERIOD)

    def update(self, progress):
        self._progress = progress


class DfuCommandHandler:
    @staticmethod
    def _abort(bar_thread=None):
        if bar_thread:
            bar_thread.update(-1)
            bar_thread.join()

        print('An Error occurred Reset MCU and try again.')

    @staticmethod
    def get_id(dfu, args):
        print('MCU ID: 0x{}'.format(dfu.id.hex()))

    @staticmethod
    def run(dfu, args):
        print(f'MCU will be running from {args.address}.')

        dfu.go(int(args.address, 0))

    def erase(self, dfu, args):
        if args.memory_map:
            with open(args.memory_map) as map_file:
                mem_map = json.load(map_file)
        else:
            mem_map = None

        if args.size:
            print(f'Erasing {args.size} bytes from {args.address}...')
        else:
            print('Erasing whole memory...')

        bar_thread = ProgressBarThread(endless=True)

        try:
            dfu.erase(int(args.address, 0), int(args.size, 0),
                      mem_map, bar_thread.update)
        except DfuException:
            self._abort(bar_thread)
            raise

        bar_thread.join()

    def dump(self, dfu, args):
        print(f'Dumping {args.size} bytes from {args.address}...')

        bar_thread = ProgressBarThread()

        try:
            with open(args.file, 'wb') as dump:
                dump.write(dfu.read(int(args.address, 0), int(args.size, 0),
                                    bar_thread.update))
        except DfuException:
            self._abort(bar_thread)
            raise

        bar_thread.join()

    def load(self, dfu, args):
        with open(args.file, 'rb') as firmware_file:
            firmware = firmware_file.read()

        if args.erase:
            if args.memory_map:
                with open(args.memory_map) as map_file:
                    mem_map = json.load(map_file)

                erase_size = len(firmware)

                print(f'Erasing {erase_size} bytes from {args.address}...')
            else:
                print('Erasing whole memory...')
                mem_map = None
                erase_size = None

            bar_thread = ProgressBarThread(endless=True)

            try:
                dfu.erase(int(args.address, 0), erase_size, mem_map,
                          bar_thread.update)
            except DfuException:
                self._abort(bar_thread)
                raise

            bar_thread.join()

        print(f'Loading {args.file} ({len(firmware)} bytes) at {args.address}')

        bar_thread = ProgressBarThread()

        try:
            dfu.write(int(args.address, 0), firmware, bar_thread.update)
        except DfuException:
            self._abort(bar_thread)
            raise

        bar_thread.join()

        print('Validating firmware...')

        bar_thread = ProgressBarThread()

        try:
            dump = dfu.read(int(args.address, 0), len(firmware),
                            bar_thread.update)
        except DfuException:
            self._abort(bar_thread)
            raise

        bar_thread.join()

        if zlib.crc32(firmware) != zlib.crc32(dump):
            print('Error: checksum mismatch!')
        else:
            print('Success!')

        if args.run:
            print(f'MCU will be running from {args.address}.')

            try:
                dfu.go(int(args.address, 0))
            except DfuException:
                self._abort()
                raise


if __name__ == '__main__':
    _ARGS_HELP = {
        'address': 'Memory address for ',
        'size': 'Required size of memory to be ',
        'memmap': 'Json file, containing memory structure.'
                  'Format: [{"address": "value", "size": "value"}, ...]',
        'run': 'Run program after loading.',
        'erase': 'Erase memory enough to store firmware'
                 '(whole memory if no memory map).'
    }

    dfu_handler = DfuCommandHandler()

    arg_parser = argparse.ArgumentParser(description='Stm32 uart dfu utility.')

    arg_parser.add_argument(
        '-p', '--port', default='/dev/ttyUSB0',
        help='Serial port file (for example: /dev/ttyUSB0).')

    commands = arg_parser.add_subparsers()

    load_command = commands.add_parser('load')

    load_command.add_argument(
        '-a', '--address', default='0x8000000',
        help=' '.join([_ARGS_HELP['address'], 'loading binary file.']))

    load_command.add_argument('-e', '--erase', action='store_true',
                              help=_ARGS_HELP['erase'])

    load_command.add_argument('-f', '--file', help='Binary firmware file.')

    load_command.add_argument('-m', '--memory-map', default=None,
                              help=_ARGS_HELP['memmap'])

    load_command.add_argument('-r', '--run', action='store_true',
                              help=_ARGS_HELP['run'])

    load_command.set_defaults(func=dfu_handler.load)

    erase_command = commands.add_parser('erase')

    erase_command.add_argument(
        '-a', '--address', default='0x8000000',
        help=' '.join([_ARGS_HELP['address'], 'erasing.']))

    erase_command.add_argument('-m', '--memory-map', default=None,
                               help=_ARGS_HELP['memmap'])

    erase_command.add_argument('-s', '--size', default=None,
                               help=' '.join([_ARGS_HELP['size'], 'erased.']))

    erase_command.set_defaults(func=dfu_handler.erase)

    dump_command = commands.add_parser('dump')

    dump_command.add_argument('-a', '--address', default='0x8000000',
                              help=' '.join([_ARGS_HELP['address'], 'dump.']))

    dump_command.add_argument('-s', '--size', default=None,
                              help=' '.join([_ARGS_HELP['size'], 'dumped.']))

    dump_command.add_argument('-f', '--file',
                              help='Specify file for memory dump.')

    dump_command.set_defaults(func=dfu_handler.dump)

    get_id_command = commands.add_parser('id')

    get_id_command.set_defaults(func=dfu_handler.get_id)

    run_command = commands.add_parser('run')

    run_command.add_argument('-a', '--address', default='0x8000000',
                             help=' '.join([_ARGS_HELP['address'], 'run.']))

    run_command.set_defaults(func=dfu_handler.run)

    args = arg_parser.parse_args()

    with Stm32UartDfu(args.port) as dfu:
        args.func(dfu, args)
