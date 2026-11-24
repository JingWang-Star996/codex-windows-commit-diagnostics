"""Inspect a Windows Store Codex profile using read-only directory metadata."""
import ctypes
import os
import re
import struct
import sys
from contextlib import ExitStack
from pathlib import Path

DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
WSTR = ctypes.c_wchar_p
DIRECTORY = 0x10
REPARSE = 0x400
MOUNT_POINT = 0xA0000003


class UnicodeString(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ushort), ("maximum", ctypes.c_ushort), ("buffer", HANDLE)]


class ObjectAttributes(ctypes.Structure):
    _fields_ = [("length", DWORD), ("root", HANDLE), ("name", ctypes.POINTER(UnicodeString)),
                ("attributes", DWORD), ("security", HANDLE), ("qos", HANDLE)]


class IoStatus(ctypes.Structure):
    _fields_ = [("status", HANDLE), ("information", ctypes.c_size_t)]


class Uncertain(Exception):
    """Metadata did not establish the required topology."""


def bind(dll, name, result, *args):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


class WindowsMetadata:
    def __init__(self):
        nt = ctypes.WinDLL("ntdll", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer = ctypes.POINTER
        self.open = bind(nt, "NtCreateFile", ctypes.c_int32, pointer(HANDLE), DWORD,
                         pointer(ObjectAttributes), pointer(IoStatus), HANDLE, DWORD,
                         DWORD, DWORD, DWORD, HANDLE, DWORD)
        self.close = bind(kernel, "CloseHandle", ctypes.c_int, HANDLE)
        self.device = bind(kernel, "QueryDosDeviceW", DWORD, WSTR, HANDLE, DWORD)
        self.drive = bind(kernel, "GetDriveTypeW", DWORD, WSTR)
        self.volume = bind(kernel, "GetVolumeInformationByHandleW", ctypes.c_int,
                           HANDLE, HANDLE, DWORD, HANDLE, HANDLE, HANDLE, HANDLE, DWORD)
        self.info = bind(kernel, "GetFileInformationByHandleEx", ctypes.c_int, HANDLE,
                         DWORD, HANDLE, DWORD)
        self.ioctl = bind(kernel, "DeviceIoControl", ctypes.c_int, HANDLE, DWORD, HANDLE,
                          DWORD, HANDLE, DWORD, pointer(DWORD), HANDLE)


def open_entry(api, stack, parent, name, optional=False):
    buf = ctypes.create_unicode_buffer(name)
    size = ctypes.sizeof(buf)
    if size > 65534:
        raise Uncertain
    us = UnicodeString(size - 2, size, ctypes.addressof(buf))
    attrs = ObjectAttributes(ctypes.sizeof(ObjectAttributes), parent, ctypes.pointer(us),
                             0x40, None, None)  # OBJ_CASE_INSENSITIVE
    handle, io = HANDLE(), IoStatus()
    # FILE_READ_ATTRIBUTES + SYNCHRONIZE, full sharing, FILE_OPEN existing,
    # FILE_OPEN_REPARSE_POINT + FILE_SYNCHRONOUS_IO_NONALERT.
    status = api.open(ctypes.byref(handle), 0x100080, ctypes.byref(attrs),
                      ctypes.byref(io), None, 0, 7, 1, 0x200020, None, 0) & 0xffffffff
    if optional and status in (0xC0000034, 0xC000000F):  # name or file not found
        return None
    if status != 0 or not handle.value:
        raise Uncertain
    stack.callback(api.close, handle)
    return handle


def info(api, handle, kind, length):
    buf = ctypes.create_string_buffer(length)
    if not api.info(handle, kind, buf, length):
        raise Uncertain
    return buf.raw


def junction_target(api, handle):
    buf, used = ctypes.create_string_buffer(16384), DWORD()
    if not api.ioctl(handle, 0x900A8, None, 0, buf, len(buf), ctypes.byref(used), None):
        raise Uncertain
    data = buf.raw[:used.value]
    if len(data) < 16:
        raise Uncertain
    tag, length, _, offset, count, _, _ = struct.unpack_from("<I6H", data)
    if (tag != MOUNT_POINT or length + 8 != len(data) or not count or offset % 2
            or count % 2 or 16 + offset + count > len(data)):
        raise Uncertain
    return data[16 + offset:16 + offset + count].decode("utf-16-le", "strict")


def validate_path(path):
    if not path.is_absolute() or not re.fullmatch("[A-Za-z]:", path.drive):
        raise Uncertain
    if len(path.parts) < 2:
        raise Uncertain
    for name in path.parts[1:]:
        if (name in (".", "..") or name.endswith((" ", "."))
                or any(ord(ch) < 32 or ch in '<>:"/\\|?*' for ch in name)):
            raise Uncertain


def inspect_path(api, path):
    target = ctypes.create_unicode_buffer(32768)
    if not api.device(path.drive, target, len(target)):
        raise Uncertain
    if not re.fullmatch(r"\\Device\\HarddiskVolume[0-9]+", target.value):
        raise Uncertain
    if api.drive(path.anchor) != 3:  # DRIVE_FIXED
        raise Uncertain

    with ExitStack() as stack:
        entries = []
        names = (target.value + "\\", *path.parts[1:])
        parent = None
        for index, name in enumerate(names):
            final = index == len(names) - 1
            handle = open_entry(api, stack, parent, name, optional=final)
            if handle is None:
                return "absent", None, tuple(entries)
            attr, tag = struct.unpack("<II", info(api, handle, 9, 8))
            # FileIdInfo (volume serial + 128-bit ID) and FileAttributeTagInfo.
            entries.append((info(api, handle, 18, 24), attr & (DIRECTORY | REPARSE), tag))
            if not attr & DIRECTORY:
                raise Uncertain
            if attr & REPARSE:
                if final and tag == MOUNT_POINT:
                    return "junction", junction_target(api, handle), tuple(entries)
                raise Uncertain
            if index == 0:
                fs = ctypes.create_unicode_buffer(32)
                if not api.volume(handle, None, 0, None, None, None, fs, len(fs)):
                    raise Uncertain
                if fs.value != "NTFS":
                    raise Uncertain
            parent = handle
        return "plain", None, tuple(entries)


def inspect_layout(public: Path, private: Path) -> str:
    """Report a snapshot. A subsequent move would require fresh offline checks."""
    try:
        validate_path(public)
        validate_path(private)
        if (public.drive.upper() != private.drive.upper() or public == private
                or public in private.parents or private in public.parents):
            return "indeterminate"
        api = WindowsMetadata()

        def snapshot():
            return inspect_path(api, public), inspect_path(api, private)

        first = snapshot()
        if snapshot() != first:
            return "indeterminate"
        (p, _, public_ids), (q, dest, private_ids) = first
        if public_ids[0] != private_ids[0]:
            return "indeterminate"
        if p == "absent" and q == "plain":
            return "native"
        if p == "plain" and q == "junction" and dest == "\\??\\" + str(public):
            return "match"
    except (AttributeError, OSError, ValueError, Uncertain):
        pass
    return "indeterminate"


FAMILY = "OpenAI.Codex_2p2nqsd0c76g0"
ROAMING = "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D"
LOCAL = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"


def known_folder(folder_id):
    import uuid
    ident = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    result = ctypes.c_void_p()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    get_folder = shell.SHGetKnownFolderPath
    get_folder.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_void_p))
    get_folder.restype = ctypes.c_int32
    # KF_FLAG_DONT_VERIFY avoids probing or creating a redirected folder.
    if get_folder(ctypes.byref(ident), 0x4000, None, ctypes.byref(result)) != 0:
        raise OSError("known-folder lookup failed")
    try:
        return Path(ctypes.wstring_at(result))
    finally:
        free = ctypes.windll.ole32.CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = None
        free(result)


def registered_family():
    """Return only an exact current-user Store family; duplicates are ambiguous."""
    from subprocess import run, PIPE, DEVNULL, CREATE_NO_WINDOW
    buffer = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < n < len(buffer):
        raise OSError("system directory lookup failed")
    shell = Path(buffer.value) / "WindowsPowerShell" / "v1.0"
    command = ("$ErrorActionPreference='Stop';"
               "@(Get-AppxPackage -Name OpenAI.Codex -PackageTypeFilter Main) | "
               "ForEach-Object { $_.PackageFamilyName }")
    env = os.environ.copy()
    env.pop("PSModulePath", None)
    env["PSModulePath"] = str(shell / "Modules")
    proc = run([str(shell / "powershell.exe"), "-NoLogo", "-NoProfile",
                "-NonInteractive", "-Command", command], env=env,
               stdout=PIPE, stderr=DEVNULL, timeout=15, creationflags=CREATE_NO_WINDOW)
    if proc.returncode:
        raise OSError("package lookup failed")
    return proc.stdout.decode("ascii").splitlines()


def observe():
    if registered_family() != [FAMILY]:
        return "indeterminate"
    public = known_folder(ROAMING) / "Codex"
    private = known_folder(LOCAL) / "Packages" / FAMILY / "LocalCache" / "Roaming" / "Codex"
    return inspect_layout(public, private)


def report(state, plan):
    messages = {
        "match": "Known legacy layout matched. This is a topology observation only.",
        "native": "The private profile is a plain directory and the public name is absent.",
        "indeterminate": "Outside the verified layout, or metadata could not be verified.",
    }
    record = {"layout": state, "mode": "plan" if plan else "inspect",
              "message": messages[state]}
    if plan and state == "match":
        record["next"] = [
            "Compare NtFC over time at ordinary load. Correlate with the affected app.",
            "Read README.md. Keep Codex and all profile writers closed. Verify a recoverable backup.",
            "Before each rename, recheck identities and names. Stop on any change or collision.",
            "If checks pass, rename the private junction to a previously unused sibling name.",
            "Move the public directory into the vacated private name on the same volume.",
            "Verify the same directory ID there, then compare NtFC during ordinary use.",
        ]
    return record


def main(argv=None):
    import argparse
    import json
    import subprocess
    parser = argparse.ArgumentParser(description="Read-only Windows Store Codex profile layout check")
    parser.add_argument("--plan", action="store_true", help="show the offline plan only if the legacy layout matches")
    args = parser.parse_args(argv)
    state = "indeterminate"
    if sys.platform == "win32":
        try:
            state = observe()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    print(json.dumps(report(state, args.plan), indent=2))
    return 2 if state == "indeterminate" else 0


if __name__ == "__main__":
    raise SystemExit(main())
