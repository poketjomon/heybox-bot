"""小黑盒 API 请求签名生成"""

import hashlib
import random
import time


def _md5(s):
    return hashlib.md5(s.encode()).hexdigest()


def _Vm(e):
    return ((e << 1) ^ 27) & 0xFF if e & 0x80 else (e << 1) & 0xFF


def _qm(e):
    return _Vm(e) ^ e


def _dollar_m(e):
    return _qm(_Vm(e))


def _Ym(e):
    return _dollar_m(_qm(_Vm(e)))


def _Gm(e):
    return _Ym(e) ^ _dollar_m(e) ^ _qm(e)


def _Km(e):
    """AES MixColumns 变换，修改前4个元素，保留其余"""
    t = [0] * 4
    t[0] = _Gm(e[0]) ^ _Ym(e[1]) ^ _dollar_m(e[2]) ^ _qm(e[3])
    t[1] = _qm(e[0]) ^ _Gm(e[1]) ^ _Ym(e[2]) ^ _dollar_m(e[3])
    t[2] = _dollar_m(e[0]) ^ _qm(e[1]) ^ _Gm(e[2]) ^ _Ym(e[3])
    t[3] = _Ym(e[0]) ^ _dollar_m(e[1]) ^ _qm(e[2]) ^ _Gm(e[3])
    e[0], e[1], e[2], e[3] = t[0], t[1], t[2], t[3]
    return e


_CHARSET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"


def _av(e, t, n):
    """字符映射：用 t[:n] 作为映射表"""
    i = t[:n]
    return "".join(i[ord(ch) % len(i)] for ch in e)


def _sv(e, t):
    """字符映射：用完整 t 作为映射表"""
    return "".join(t[ord(ch) % len(t)] for ch in e)


def _interleave(arrays):
    """交织多个字符串"""
    result = ""
    max_len = max(len(a) for a in arrays)
    for i in range(max_len):
        for a in arrays:
            if i < len(a):
                result += a[i]
    return result


def _ov(url_path, timestamp, nonce):
    """核心签名计算"""
    parts = [p for p in url_path.split("/") if p]
    path = "/" + "/".join(parts) + "/"

    r = _CHARSET
    i = _interleave([_av(str(timestamp), r, -2), _sv(path, r), _sv(nonce, r)])[:20]

    o = _md5(i)

    last6 = [ord(c) for c in o[-6:]]
    km_result = _Km(last6)
    a = sum(km_result) % 100
    a_str = str(a).zfill(2)

    s = _av(o[:5], r, -4)
    return s + a_str


def generate_sign(url_path):
    """生成 API 请求签名

    Args:
        url_path: API 路径，如 '/bbs/app/topic/feeds'

    Returns:
        dict: {'hkey': ..., '_time': ..., 'nonce': ...}
    """
    t = int(time.time())
    rand_str = str(random.random()) + str(int(time.time() * 1000))
    nonce = _md5(str(t) + rand_str).upper()

    # lv['g'] = ov(url, t+1, nonce)
    hkey = _ov(url_path, t + 1, nonce)

    return {"hkey": hkey, "_time": str(t), "nonce": nonce}


if __name__ == "__main__":
    sign = generate_sign("/bbs/app/topic/feeds")
    print(f"hkey: {sign['hkey']}")
    print(f"_time: {sign['_time']}")
    print(f"nonce: {sign['nonce']}")
