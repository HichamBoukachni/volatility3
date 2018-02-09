import logging
import math
import struct
import typing

from volatility.framework import exceptions, interfaces
from volatility.framework.configuration import requirements

vollog = logging.getLogger(__name__)

IteratorValue = typing.Tuple[typing.List[typing.Tuple[str, int, int]], int]


class classproperty(object):
    """Class property decorator

    Note this will change the return type """

    def __init__(self, func: typing.Callable[[typing.Any], typing.Any]) -> None:
        self._func = func

    def __get__(self, _owner_self, owner_cls: typing.Type) -> typing.Any:
        return self._func(owner_cls)


class Intel(interfaces.layers.TranslationLayerInterface):
    """Translation Layer for the Intel IA32 memory mapping"""

    priority = 40
    _architecture = "Intel32"
    _entry_format = "<I"
    _page_size_in_bits = 12
    _bits_per_register = 32
    # NOTE: _maxphyaddr is MAXPHYADDR as defined in the Intel specs *NOT* the maximum physical address
    _maxphyaddr = 32
    _maxvirtaddr = _maxphyaddr
    _structure = [('page directory', 10, False),
                  ('page table', 10, True)]

    def __init__(self,
                 context: interfaces.context.ContextInterface,
                 config_path: str,
                 name: str) -> None:
        super().__init__(context, config_path, name)
        self._base_layer = self._check_type(self.config["memory_layer"], str)
        self._swap_layers = []
        self._check_type(self.config.get("swap_layers", []), list)
        for layer_name in self.config.get("swap_layers", []):
            self._check_type(layer_name, str)
            if layer_name in context.memory:
                self._swap_layers.append(layer_name)
        self._page_map_offset = self._check_type(self.config["page_map_offset"], int)
        self._optimize_scan = False

        # These can vary depending on the type of space
        self._index_shift = int(math.ceil(math.log2(struct.calcsize(self._entry_format))))

    @classproperty
    def page_size(cls) -> int:
        """Page size for the intel address space.

        All Intel address spaces work on 4096 byte pages"""
        return 1 << cls._page_size_in_bits

    @classproperty
    def bits_per_register(cls) -> int:
        """Returns the bits_per_register to determine the range of an IntelTranslationLayer"""
        return cls._bits_per_register

    @classproperty
    def minimum_address(cls) -> int:  # type: ignore
        return 0

    @classproperty
    def maximum_address(cls) -> int:  # type: ignore
        return (1 << cls._maxvirtaddr) - 1

    @classproperty
    def structure(cls) -> typing.List[typing.Tuple[str, int, bool]]:
        return cls._structure

    @staticmethod
    def _mask(value: int, high_bit: int, low_bit: int) -> int:
        """Returns the bits of a value between highbit and lowbit inclusive"""
        high_mask = (2 ** (high_bit + 1)) - 1
        low_mask = (2 ** low_bit) - 1
        mask = (high_mask ^ low_mask)
        # print(high_bit, low_bit, bin(mask), bin(value))
        return value & mask

    @staticmethod
    def _page_is_valid(entry: int) -> bool:
        """Returns whether a particular page is valid based on its entry"""
        return bool(entry & 1)

    def _translate(self, offset: int) -> typing.Tuple[int, int, str]:
        """Translates a specific offset based on paging tables

           Returns the translated offset, the contiguous pagesize that the translated address lives in and the layer_name that the address lives in
        """
        # Setup the entry and how far we are through the offset
        # Position maintains the number of bits left to process
        # We or with 0x1 to ensure our page_map_offset is always valid
        position = min(self._maxvirtaddr, self._bits_per_register) - 1
        entry = self._mask(self._page_map_offset, position, 0) | 0x1

        # Run through the offset in various chunks
        for (name, size, large_page) in self._structure:
            # Check we're valid
            if not self._page_is_valid(entry):
                raise exceptions.PagedInvalidAddressException(self.name, offset, position + 1, entry,
                                                              "Page Fault at entry " + hex(entry) + " in table " + name)
            # Check if we're a large page
            if large_page and (entry & (1 << 7)):
                # We're a large page, the rest is finished below
                # If we want to implement PSE-36, it would need to be done here
                break
            # Figure out how much of the offset we should be using
            start = position
            position -= size
            index = self._mask(offset, start, position + 1) >> (position + 1)

            # Grab the base address of the table we'll be getting the next entry from
            base_address = self._mask(entry, self._maxphyaddr - 1, size + self._index_shift)
            # Create the offset for the next entry
            table_offset = base_address | (index << self._index_shift)
            # Read out the new entry from memory
            entry, = struct.unpack(self._entry_format, self._context.memory.read(self._base_layer, table_offset,
                                                                                 struct.calcsize(self._entry_format)))

        # Now we're done
        if not self._page_is_valid(entry):
            raise exceptions.PagedInvalidAddressException(self.name, offset, position + 1, entry,
                                                          "Page Fault at entry {} in page entry".format(hex(entry)))
        page = self._mask(entry, self._maxphyaddr - 1, position + 1) | self._mask(offset, position, 0)
        return page, 1 << (position + 1), self._base_layer

    def is_valid(self, offset: int, length: int = 1) -> bool:
        """Returns whether the address offset can be translated to a valid address"""
        try:
            # TODO: Consider reimplementing this, since calls to mapping can call is_valid
            return all([self._context.memory[layer].is_valid(mapped_offset) for _, mapped_offset, _, layer in
                        self.mapping(offset, length)])
        except exceptions.InvalidAddressException:
            return False

    def mapping(self,
                offset: int,
                length: int,
                ignore_errors: bool = False) -> typing.Iterable[typing.Tuple[int, int, int, str]]:
        """Returns a sorted iterable of (offset, mapped_offset, length, layer) mappings

           This allows translation layers to provide maps of contiguous regions in one layer
        """
        if length == 0:
            if ignore_errors and not self.is_valid(offset):
                raise StopIteration
            mapped_offset, _, layer_name = self._translate(offset)
            yield (offset, mapped_offset, length, layer_name)
            raise StopIteration
        while length > 0:
            if ignore_errors:
                while not self.is_valid(offset) and length > 0:
                    length -= 1 << self._page_size_in_bits
                    offset += 1 << self._page_size_in_bits
                if length <= 0:
                    raise StopIteration
            chunk_offset, page_size, layer_name = self._translate(offset)
            chunk_size = min(page_size - (chunk_offset % page_size), length)
            yield (offset, chunk_offset, chunk_size, layer_name)
            length -= chunk_size
            offset += chunk_size

    @property
    def dependencies(self) -> typing.List[str]:
        """Returns a list of the lower layer names that this layer is dependent upon"""
        # TODO: Add in the whole buffalo
        return [self._base_layer] + self._swap_layers

    @classmethod
    def get_requirements(cls) -> typing.List[interfaces.configuration.RequirementInterface]:
        return [requirements.TranslationLayerRequirement(name = 'memory_layer',
                                                         optional = False),
                requirements.ListRequirement(name = 'swap_layers',
                                             element_type = requirements.StringRequirement(
                                                 name = 'layer_name',
                                                 optional = False
                                             ),
                                             min_elements = 0,
                                             max_elements = 100,
                                             optional = True),
                requirements.IntRequirement(name = 'page_map_offset',
                                            optional = False),
                requirements.IntRequirement(name = 'kernel_virtual_offset',
                                            optional = True),
                requirements.StringRequirement(name = 'linux_banner',
                                               optional = True)]

    def _scan_iterator(self,
                       scanner: interfaces.layers.ScannerInterface,
                       min_address: int,
                       max_address: int) \
            -> typing.Iterable[IteratorValue]:
        previous = None
        data_to_scan = []  # type: typing.List[typing.Tuple[str, int, int]]
        scanned_pairs = set()  # type: typing.Set[typing.Tuple[int, int]]
        chunk_end = min_address
        while chunk_end <= max_address:
            try:
                address, page_size, layer_name = self._translate(chunk_end)
                chunk_size = page_size - (address & (page_size - 1))
            except exceptions.InvalidAddressException:
                address, chunk_size, layer_name = None, 1 << self._page_size_in_bits, ''
            # We've come to a break, so scan what we've seen so far
            if address is None or (previous, address) in scanned_pairs:
                yield data_to_scan, chunk_end
                data_to_scan = []
            else:
                # TODO: We've already done the translation, so don't bother doing it again
                data_to_scan += [(layer_name, address, chunk_size)]

            # We can't actually use scanned_pairs because the user might want to find duplicate instances
            # throughout the top layer, not just the one actual copy of the data in the bottom layer.
            # We'd need to re-architect the scanner API to pass through multiple data_offsets to the scanners
            # Then we'd also then need to batch all the data_offsets up until the end (so we know we're handing
            # them a complete list) or we'd have to be able to add relevant offsets as they're found.
            # All in all, massive complexity for little benefit in efficiency.
            #
            # At the moment, the following line is only good when you want *a* hit but don't care which one.
            #    scanned_pairs.add((previous, address))

            previous = address
            chunk_end += chunk_size

    # We ignore the type due to the iterator_value, actually it only needs to match the output from _scan_iterator
    def _scan_chunk(self,
                    scanner: interfaces.layers.ScannerInterface,
                    min_address: int,
                    max_address: int,
                    progress: interfaces.layers.ProgressValue,
                    iterator_value: IteratorValue) -> typing.List[typing.Any]:
        data_to_scan, chunk_end = iterator_value
        data = b''
        for layer_name, address, chunk_size in data_to_scan:
            try:
                data += self.context.memory[layer_name].read(address, chunk_size)
            except exceptions.InvalidAddressException:
                vollog.debug(
                    "Invalid address in layer {} found scanning {} at address {:x}".format(layer_name, self.name,
                                                                                           address))
        progress.value = chunk_end
        return list(scanner(data, chunk_end - len(data_to_scan)))

    def _scan_metric(self,
                     _scanner: interfaces.layers.ScannerInterface,
                     min_address: int,
                     max_address: int,
                     value: int) -> float:
        return max(0, ((value - min_address) * 100) / (max_address - min_address))


class IntelPAE(Intel):
    """Class for handling Physical Address Extensions for Intel architectures"""

    priority = 35
    _architecture = "Intel32"
    _entry_format = "<Q"
    _bits_per_register = 32
    _maxphyaddr = 40
    _maxvirtaddr = _maxphyaddr
    _structure = [('page directory pointer', 2, False),
                  ('page directory', 9, True),
                  ('page table', 9, True)]


class Intel32e(Intel):
    """Class for handling 64-bit (32-bit extensions) for Intel architectures"""

    priority = 30
    _architecture = "Intel64"
    _entry_format = "<Q"
    _bits_per_register = 64
    _maxphyaddr = 52
    _maxvirtaddr = 48
    _structure = [('page map layer 4', 9, False),
                  ('page directory pointer', 9, True),
                  ('page directory', 9, True),
                  ('page table', 9, True)]


class WindowsMixin(object):
    @staticmethod
    def _page_is_valid(entry: int) -> bool:
        """Returns whether a particular page is valid based on its entry

           Windows uses additional "available" bits to store flags
           These flags allow windows to determine whether a page is still valid

           Bit 11 is the transition flag, and Bit 10 is the prototype flag

           For more information, see Windows Internals (6th Ed, Part 2, pages 268-269)
        """
        return bool((entry & 1) or ((entry & 1 << 11) and not entry & 1 << 10))


### These must be full separate classes so that JSON configs re-create them properly

class WindowsIntel(WindowsMixin, Intel):

    def _translate(self, offset):
        try:
            return super()._translate(offset)
        except exceptions.PagedInvalidAddressException as excp:
            entry = excp.entry
            tbit = bool(entry & (1 << 11))
            pbit = bool(entry & (1 << 10))
            unknown_bit = bool(entry & (1 << 7))
            n = (entry >> 1) & 0xF
            vbit = bool(entry & 1)
            if (not tbit and not pbit and not vbit and unknown_bit) and (
                    (entry >> self._page_size_in_bits) != 0):
                swap_offset = entry >> self._page_size_in_bits << self._page_size_in_bits

                if len(self.config.get('swap_layers', [])) >= (n + 1):
                    swap_layer_name = self.config['swap_layers'][n]
                    return swap_offset, 1 << excp.invalid_bits, swap_layer_name
            raise


class WindowsIntelPAE(WindowsMixin, IntelPAE):

    def _translate(self, offset):
        try:
            return super()._translate(offset)
        except exceptions.PagedInvalidAddressException as excp:
            entry = excp.entry
            tbit = bool(entry & (1 << 11))
            pbit = bool(entry & (1 << 10))
            unknown_bit = bool(entry & (1 << 7))
            n = (entry >> 1) & 0xF
            vbit = bool(entry & 1)
            if ((not tbit and not pbit and not vbit and unknown_bit) and (
                    self._mask(entry, 64, 32) >> 32) != 0) and excp.invalid_bits == 12:
                swap_offset = (self._mask(entry, 64, 32) >> 32 << excp.invalid_bits) | self._mask(excp.invalid_address,
                                                                                                  excp.invalid_bits, 0)
                if len(self.config.get('swap_layers', [])) >= (n + 1):
                    swap_layer_name = self.config['swap_layers'][n]
                    return swap_offset, 1 << excp.invalid_bits, swap_layer_name
            raise


class WindowsIntel32e(WindowsMixin, Intel32e): pass
