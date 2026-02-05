import ctypes
import struct
import sys


def test_ioctl_struct_size_is_16_bytes():
    args = struct.pack("QII", 0x12345678, 100, 0)
    assert len(args) == 16


def test_pointer_packs_to_u64():
    buf = ctypes.create_string_buffer(b"test\0")
    ptr = ctypes.addressof(buf)
    packed = struct.pack("Q", ptr)
    assert len(packed) == 8
    assert struct.unpack("Q", packed)[0] == ptr


def test_arm32_pointer_size_when_running_arm32():
    if sys.maxsize > 2**32:
        return
    assert ctypes.sizeof(ctypes.c_void_p) == 4
