import os
import sys
import ctypes
import time

from ctypes import wintypes

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

PROCESS_ALL_ACCESS = (0x000F0000 | 0x00100000 | 0xFFF)

def inject(pid, dll_path):
    dll_path_bytes = dll_path.encode('utf-8') + b'\x00'
    hProcess = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not hProcess:
        raise ctypes.WinError(ctypes.get_last_error())

    # allocate memory in remote process
    addr = kernel32.VirtualAllocEx(hProcess, None, len(dll_path_bytes), 0x3000, 0x40)
    if not addr:
        kernel32.CloseHandle(hProcess)
        raise ctypes.WinError(ctypes.get_last_error())

    written = wintypes.DWORD(0)
    if not kernel32.WriteProcessMemory(hProcess, addr, dll_path_bytes, len(dll_path_bytes), ctypes.byref(written)):
        kernel32.VirtualFreeEx(hProcess, addr, 0, 0x8000)
        kernel32.CloseHandle(hProcess)
        raise ctypes.WinError(ctypes.get_last_error())

    # get handle to LoadLibraryA
    hKernel32 = kernel32.GetModuleHandleW('kernel32.dll')
    if not hKernel32:
        kernel32.CloseHandle(hProcess)
        raise ctypes.WinError(ctypes.get_last_error())
    LoadLibraryA = kernel32.GetProcAddress(hKernel32, b'LoadLibraryA')
    if not LoadLibraryA:
        kernel32.CloseHandle(hProcess)
        raise ctypes.WinError(ctypes.get_last_error())

    # create remote thread calling LoadLibraryA(addr)
    hThread = kernel32.CreateRemoteThread(hProcess, None, 0, LoadLibraryA, addr, 0, None)
    if not hThread:
        kernel32.VirtualFreeEx(hProcess, addr, 0, 0x8000)
        kernel32.CloseHandle(hProcess)
        raise ctypes.WinError(ctypes.get_last_error())

    # wait for the remote thread to finish
    kernel32.WaitForSingleObject(hThread, 5000)
    kernel32.CloseHandle(hThread)
    kernel32.VirtualFreeEx(hProcess, addr, 0, 0x8000)
    kernel32.CloseHandle(hProcess)


def find_pid_by_name(name):
    # naive search using os tools; require psutil for robustness
    try:
        import psutil
    except Exception:
        raise RuntimeError('psutil required for process lookup')
    for p in psutil.process_iter(['pid', 'name']):
        if p.info['name'] and p.info['name'].lower() == name.lower():
            return p.info['pid']
    return None


def main():
    if len(sys.argv) < 3:
        print('Usage: inject_dll.py <pid|processname> <path-to-dll>')
        return
    target = sys.argv[1]
    dll = sys.argv[2]
    if not os.path.isabs(dll):
        dll = os.path.abspath(dll)
    if not os.path.exists(dll):
        print('DLL not found:', dll)
        return

    try:
        pid = int(target)
    except ValueError:
        pid = find_pid_by_name(target)
        if pid is None:
            print('Process not found:', target)
            return

    print(f'Injecting {dll} into PID {pid}...')
    try:
        inject(pid, dll)
        print('Injection done.')
    except Exception as e:
        print('Injection failed:', e)


if __name__ == '__main__':
    main()
