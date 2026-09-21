"""Unbuffered random-read probe for one directory (Windows). Writes a scratch file, reads it back
with FILE_FLAG_NO_BUFFERING so the page cache does not answer, then deletes it.
usage: python disk_probe.py <dir> [size_gb] [block_kib] [threads]
"""
import ctypes, json, os, random, statistics, sys, threading, time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
NO_BUF, OPEN_EXISTING, GENERIC_READ = 0x20000000, 3, 0x80000000
INVALID = wintypes.HANDLE(-1).value


def probe(d, size_gb, block_kib, threads, seconds=20):
    path = os.path.join(d, "coli_probe.tmp")
    block = block_kib * 1024
    total = int(size_gb * 2**30) // block * block
    chunk = os.urandom(8 * 2**20)
    try:
        with open(path, "wb") as f:
            for _ in range(total // len(chunk)):
                f.write(chunk)
            f.flush(); os.fsync(f.fileno())
        lat, nbytes, stop = [], [0], time.time() + seconds
        lock = threading.Lock()

        def worker(seed):
            h = k32.CreateFileW(path, GENERIC_READ, 1, None, OPEN_EXISTING, NO_BUF, None)
            if h == INVALID:
                raise OSError(ctypes.get_last_error())
            buf = ctypes.create_string_buffer(block + 4096)
            addr = (ctypes.addressof(buf) + 4095) // 4096 * 4096
            rng, got = random.Random(seed), wintypes.DWORD()
            loc = []
            while time.time() < stop:
                off = rng.randrange(total // block) * block
                t = time.perf_counter()
                k32.SetFilePointerEx(h, ctypes.c_longlong(off), None, 0)
                k32.ReadFile(h, ctypes.c_void_p(addr), block, ctypes.byref(got), None)
                loc.append(time.perf_counter() - t)
            k32.CloseHandle(h)
            with lock:
                lat.extend(loc); nbytes[0] += len(loc) * block

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        [t.start() for t in ts]; [t.join() for t in ts]
        lat.sort()
        return {"block_kib": block_kib, "queue_depth": threads, "buffered": False, "file_gb": size_gb,
                "mb_s": round(nbytes[0] / seconds / 1e6, 1), "iops": round(len(lat) / seconds),
                "lat_ms_p50": round(lat[len(lat) // 2] * 1e3, 2), "lat_ms_p99": round(lat[int(len(lat) * .99)] * 1e3, 2)}
    finally:
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    d = sys.argv[1]
    gb = float(sys.argv[2]) if len(sys.argv) > 2 else 8
    print(json.dumps([probe(d, gb, kib, qd) for kib, qd in ((1024, 1), (1024, 4))]))
