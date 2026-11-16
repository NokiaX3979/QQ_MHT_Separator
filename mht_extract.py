#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mht_extract.py —— 把超大 .mht / .mhtml（QQ/微信导出的聊天记录）拆成
                  「小体积 HTML + 独立图片文件夹」，以便浏览器正常打开。

特点：
  * 全程流式处理，不会把 2.2G 文件读进内存（峰值内存几十 MB）
  * 自动识别 multipart/related 结构；不是标准 MHT 也能处理
  * 自动把 HTML 里的 data:image/...;base64,xxx 内联图外置成文件
  * 自动把 GBK/GB18030 编码的 HTML 转成 UTF-8，并修正 <meta charset>
  * 可选 --split-mb 把巨大的 HTML 再切成多页

用法：
  python mht_extract.py "聊天记录.mht"
  python mht_extract.py "聊天记录.mht" -o 输出目录
  python mht_extract.py "聊天记录.mht" --split-mb 20
"""

import argparse
import base64
import codecs
import os
import re
import sys
import time

CT_EXT = {
    'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/pjpeg': '.jpg',
    'image/png': '.png', 'image/gif': '.gif', 'image/bmp': '.bmp',
    'image/webp': '.webp', 'image/svg+xml': '.svg', 'image/x-icon': '.ico',
    'image/vnd.microsoft.icon': '.ico', 'image/tiff': '.tif', 'image/heic': '.heic',
    'audio/mpeg': '.mp3', 'audio/amr': '.amr', 'audio/wav': '.wav',
    'video/mp4': '.mp4', 'text/css': '.css', 'application/javascript': '.js',
}

B64_RE = re.compile(rb'[^A-Za-z0-9+/=]')
DATA_URI_RE = re.compile(r'data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)')
REF_RE = re.compile(r'''(?i)\b(src|href|background)\s*=\s*("([^"]*)"|'([^']*)')''')
CSSURL_RE = re.compile(r'''(?i)url\(\s*['"]?([^)'"\s]+)''')
META_CHARSET_RE = re.compile(r'''(?i)charset\s*=\s*(["']?)[\w-]+\1''')

CHUNK = 1 << 20


def mb(n):
    return n / 1048576.0


class Part:
    __slots__ = ('headers', 'start', 'end')

    def __init__(self):
        self.headers = {}
        self.start = 0
        self.end = 0

    @property
    def ctype(self):
        return (self.headers.get('content-type') or '').split(';')[0].strip().lower()

    @property
    def cte(self):
        return (self.headers.get('content-transfer-encoding') or '').strip().lower()

    @property
    def charset(self):
        m = re.search(r'charset\s*=\s*["\']?([\w-]+)', self.headers.get('content-type') or '', re.I)
        return m.group(1) if m else None

    @property
    def location(self):
        return (self.headers.get('content-location') or '').strip()


def read_headers(f, limit=1 << 20):
    """从当前位置读到空行，返回 header dict（key 小写）。"""
    headers = {}
    last = None
    total = 0
    while True:
        line = f.readline()
        if not line:
            break
        total += len(line)
        if total > limit:
            break
        s = line.rstrip(b'\r\n')
        if not s:
            break
        if s[:1] in (b' ', b'\t') and last:
            headers[last] += ' ' + s.strip().decode('utf-8', 'replace')
            continue
        if b':' in s:
            k, v = s.split(b':', 1)
            last = k.decode('ascii', 'replace').strip().lower()
            headers[last] = v.strip().decode('utf-8', 'replace')
    return headers


def index_parts(path, boundary):
    """第一遍：只记录每个 part 的 header 与 body 字节区间，不读内容。"""
    delim = b'--' + boundary.encode('utf-8', 'replace')
    parts = []
    cur = None
    total = os.path.getsize(path)
    nxt = 1 << 28
    f = open(path, 'rb')
    try:
        while True:
            pos = f.tell()
            line = f.readline()
            if not line:
                break
            if pos > nxt:
                nxt += 1 << 28
                sys.stderr.write('\r  扫描 %.0f%%  (%.0f MB / %.0f MB)   ' %
                                 (pos * 100.0 / total, mb(pos), mb(total)))
                sys.stderr.flush()
            if line[:2] != b'--':
                continue
            s = line.rstrip(b'\r\n')
            if s == delim:
                if cur is not None:
                    cur.end = pos
                    parts.append(cur)
                cur = Part()
                cur.headers = read_headers(f)
                cur.start = f.tell()
            elif s == delim + b'--':
                if cur is not None:
                    cur.end = pos
                    parts.append(cur)
                    cur = None
                break
        if cur is not None:
            cur.end = f.tell()
            parts.append(cur)
    finally:
        f.close()
    return parts


def iter_body(f, part):
    f.seek(part.start)
    left = part.end - part.start
    while left > 0:
        data = f.read(min(CHUNK, left))
        if not data:
            break
        left -= len(data)
        yield data


class B64Decoder:
    def __init__(self):
        self.carry = b''

    def feed(self, data):
        data = B64_RE.sub(b'', data)
        if self.carry:
            data = self.carry + data
        n = len(data) // 4 * 4
        self.carry = data[n:]
        if n:
            return base64.b64decode(data[:n])
        return b''

    def finish(self):
        c = self.carry
        self.carry = b''
        if c:
            c += b'=' * ((4 - len(c) % 4) % 4)
            try:
                return base64.b64decode(c)
            except Exception:
                return b''
        return b''


class QPDecoder:
    """流式 quoted-printable 解码，能处理跨块的软换行。"""

    def __init__(self):
        self.buf = b''

    def feed(self, data):
        data = self.buf + data if self.buf else data
        out = bytearray()
        i = 0
        n = len(data)
        while i < n:
            c = data[i]
            if c == 61:  # '='
                if i + 1 >= n:
                    break
                c2 = data[i + 1]
                if c2 == 13:  # \r
                    if i + 2 >= n:
                        break
                    if data[i + 2] == 10:
                        i += 3
                        continue
                    i += 2
                    continue
                if c2 == 10:
                    i += 2
                    continue
                if i + 2 >= n:
                    break
                try:
                    out.append(int(data[i + 1:i + 3], 16))
                    i += 3
                    continue
                except ValueError:
                    out.append(c)
                    i += 1
                    continue
            out.append(c)
            i += 1
        self.buf = data[i:]
        return bytes(out)

    def finish(self):
        b, self.buf = self.buf, b''
        return b


def decode_stream(f, part):
    cte = part.cte
    if 'base64' in cte:
        d = B64Decoder()
        for c in iter_body(f, part):
            out = d.feed(c)
            if out:
                yield out
        out = d.finish()
        if out:
            yield out
    elif 'quoted-printable' in cte:
        d = QPDecoder()
        for c in iter_body(f, part):
            out = d.feed(c)
            if out:
                yield out
        out = d.finish()
        if out:
            yield out
    else:
        for c in iter_body(f, part):
            yield c


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', '_', name).strip(' .')
    return name[:80] or 'file'


class Writer:
    """把 HTML 里的引用改写到本地文件，同时把内联 data URI 落盘。"""

    def __init__(self, imgdir, loc_map=None, base_map=None):
        self.imgdir = imgdir
        self.loc_map = loc_map or {}
        self.base_map = base_map or {}
        self.n = 0

    def _save_data_uri(self, m):
        mime, b64 = m.group(1), m.group(2)
        ext = CT_EXT.get(mime.lower(), '.bin')
        self.n += 1
        name = 'inline_%05d%s' % (self.n, ext)
        try:
            with open(os.path.join(self.imgdir, name), 'wb') as o:
                o.write(base64.b64decode(B64_RE.sub(b'', b64.encode('ascii', 'replace'))))
        except Exception:
            return m.group(0)
        return 'images/' + name

    def _fix_ref(self, m):
        attr, quoted, dq, sq = m.group(1), m.group(2), m.group(3), m.group(4)
        url = dq if dq is not None else (sq or '')
        new = self.loc_map.get(url)
        if new is None:
            base = url.replace('\\', '/').rsplit('/', 1)[-1]
            new = self.base_map.get(base) or self.base_map.get(base.lower())
        if new is None:
            return m.group(0)
        return '%s="%s"' % (attr, new)

    def rewrite(self, s):
        if 'data:image' in s:
            s = DATA_URI_RE.sub(self._save_data_uri, s)
        if self.loc_map or self.base_map:
            if 'src=' in s or 'href=' in s or 'background=' in s:
                s = REF_RE.sub(self._fix_ref, s)
            if 'url(' in s:
                s = CSSURL_RE.sub(lambda m: 'url(' + (self.loc_map.get(m.group(1)) or self.base_map.get(
                    m.group(1).replace('\\', '/').rsplit('/', 1)[-1]) or m.group(1)) + ')', s)
        if '<meta' in s.lower():
            s = META_CHARSET_RE.sub(lambda m: 'charset=%sutf-8%s' % (m.group(1), m.group(1)), s)
        return s


def process_html_stream(f, part, charset, writer, dest, total_hint=None):
    try:
        dec = codecs.getincrementaldecoder(charset)(errors='replace')
    except Exception:
        dec = codecs.getincrementaldecoder('utf-8')(errors='replace')

    pending = ''
    done = 0
    t0 = time.time()
    last = 0
    with open(dest, 'w', encoding='utf-8', newline='') as o:
        for raw in decode_stream(f, part):
            done += len(raw)
            if total_hint and time.time() - last > 2:
                last = time.time()
                pct = min(100.0, done * 100.0 / max(1, total_hint))
                sys.stderr.write('\r  解码 HTML  %5.1f%%  (%.0f MB, %.0fs)   ' %
                                 (pct, mb(done), time.time() - t0))
                sys.stderr.flush()
            pending += dec.decode(raw)
            if '\n' not in pending:
                if len(pending) > 32 * 1024 * 1024:
                    o.write(writer.rewrite(pending))
                    pending = ''
                continue
            lines = pending.split('\n')
            pending = lines.pop()
            o.write('\n'.join(writer.rewrite(x) for x in lines))
            o.write('\n')
        pending += dec.decode(b'', True)
        if pending:
            o.write(writer.rewrite(pending))
    if total_hint:
        sys.stderr.write('\r  解码 HTML  100.0%%  (%.0f MB, %.0fs)   \n' % (mb(done), time.time() - t0))
    return dest


SAFE_CUTS = (b'</div>', b'</table>', b'</p>', b'</tr>', b'\n')


def split_html(path, outdir, max_bytes):
    size = os.path.getsize(path)
    if size <= max_bytes:
        return None
    if size > 600 * 1024 * 1024:
        print('  ! HTML 超过 600MB，跳过自动分页（可先试着直接打开）')
        return None
    with open(path, 'rb') as f:
        data = f.read()
    low = data.lower()
    bstart = low.find(b'<body')
    if bstart < 0:
        bstart = 0
    gt = data.find(b'>', bstart)
    head = data[:gt + 1] if gt >= 0 else b'<html><body>'
    bend = low.rfind(b'</body>')
    tail = data[bend:] if bend >= 0 else b'</body></html>'
    content = data[len(head):bend if bend >= 0 else len(data)]

    pdir = os.path.join(outdir, 'parts')
    os.makedirs(pdir, exist_ok=True)
    pages = []
    pos = 0
    n = 0
    total = len(content)
    while pos < total:
        target = min(total, pos + max_bytes)
        if target >= total:
            cut = total
        else:
            cut = -1
            for mark in SAFE_CUTS:
                idx = content.rfind(mark, pos + max_bytes // 2, target)
                if idx > pos:
                    cut = idx + len(mark)
                    break
            if cut < 0:
                cut = target
        n += 1
        name = 'page_%03d.html' % n
        chunk = content[pos:cut]
        # 分页文件在 parts/ 下，图片目录在上一级，需要修正相对路径
        chunk = chunk.replace(b'"images/', b'"../images/').replace(b"'images/", b"'../images/")
        with open(os.path.join(pdir, name), 'wb') as o:
            o.write(head)
            o.write(chunk)
            o.write(tail)
        pages.append(name)
        pos = cut
    with open(os.path.join(pdir, 'index.html'), 'w', encoding='utf-8') as o:
        o.write('<meta charset="utf-8"><title>分页目录</title>\n')
        o.write('<h3>共 %d 页</h3><ol>\n' % len(pages))
        for p in pages:
            o.write('<li><a href="%s">%s</a></li>\n' % (p, p))
        o.write('</ol>\n')
    return pages


def main():
    ap = argparse.ArgumentParser(description='把超大 MHT 拆成 HTML + 图片文件夹')
    ap.add_argument('src', help='源 .mht / .mhtml 文件')
    ap.add_argument('-o', '--out', help='输出目录（默认 源文件名_out）')
    ap.add_argument('--split-mb', type=int, default=0, help='HTML 超过该体积时自动分页（0=不分页）')
    a = ap.parse_args()

    src = a.src
    if not os.path.isfile(src):
        sys.exit('找不到文件: ' + src)
    outdir = a.out or (os.path.splitext(src)[0] + '_out')
    imgdir = os.path.join(outdir, 'images')
    os.makedirs(imgdir, exist_ok=True)

    size = os.path.getsize(src)
    print('源文件 : %s' % src)
    print('体积   : %.1f MB' % mb(size))
    print('输出   : %s' % outdir)

    with open(src, 'rb') as f:
        top = read_headers(f)
    ctype = top.get('content-type', '')
    m = re.search(r'boundary\s*=\s*"([^"]+)"', ctype, re.I) or \
        re.search(r'boundary\s*=\s*([^;\s]+)', ctype, re.I)

    t0 = time.time()

    if 'multipart' in ctype.lower() and m:
        boundary = m.group(1).strip()
        print('结构   : multipart，boundary=%s' % boundary)
        print('扫描索引中 ...')
        parts = index_parts(src, boundary)
        print('  共 %d 个 MIME 段' % len(parts))

        html_parts = []
        res_parts = []
        for i, p in enumerate(parts):
            ct = p.ctype
            loc = p.location
            is_html = ct in ('text/html', 'application/xhtml+xml') or \
                (not ct and re.search(r'\.html?$', loc, re.I))
            if is_html and not html_parts:
                html_parts.append(p)
            elif ct.startswith('image/') or ct.startswith('audio/') or ct.startswith('video/') \
                    or ct in ('text/css', 'application/javascript') or loc:
                res_parts.append((i, p))

        # 资源命名 + 映射表
        loc_map = {}
        base_map = {}
        plan = []
        used = set()
        for idx, p in res_parts:
            loc = p.location
            ct = p.ctype
            ext = CT_EXT.get(ct, '')
            base = ''
            if loc:
                b = loc.replace('\\', '/').rsplit('/', 1)[-1]
                b = b.split('?')[0]
                base = os.path.splitext(b)[0]
                if not ext:
                    ext = os.path.splitext(b)[1].lower()
            if not ext:
                ext = '.bin'
            name = sanitize(base) if base else 'res_%05d' % idx
            if not name.lower().endswith(ext.lower()):
                name += ext
            stem, e = os.path.splitext(name)
            k = 1
            while name.lower() in used:
                k += 1
                name = '%s_%d%s' % (stem, k, e)
            used.add(name.lower())
            rel = 'images/' + name
            plan.append((p, rel))
            if loc:
                loc_map[loc] = rel
                loc_map[loc.replace('/', '\\')] = rel
                base_map[loc.replace('\\', '/').rsplit('/', 1)[-1]] = rel
                base_map[loc.replace('\\', '/').rsplit('/', 1)[-1].lower()] = rel

        f = open(src, 'rb')
        n_ok = 0
        n_bytes = 0
        for i, (p, rel) in enumerate(plan):
            dest = os.path.join(outdir, rel.replace('/', os.sep))
            try:
                with open(dest, 'wb') as o:
                    if 'base64' in p.cte:
                        d = B64Decoder()
                        for c in iter_body(f, p):
                            out = d.feed(c)
                            if out:
                                o.write(out)
                        out = d.finish()
                        if out:
                            o.write(out)
                    elif 'quoted-printable' in p.cte:
                        d = QPDecoder()
                        for c in iter_body(f, p):
                            out = d.feed(c)
                            if out:
                                o.write(out)
                        out = d.finish()
                        if out:
                            o.write(out)
                    else:
                        for c in iter_body(f, p):
                            o.write(c)
                n_bytes += os.path.getsize(dest)
                n_ok += 1
            except Exception as e:
                print('  ! 提取失败 %s: %s' % (rel, e))
            if (i + 1) % 200 == 0:
                sys.stderr.write('\r  提取资源 %d/%d ...' % (i + 1, len(plan)))
                sys.stderr.flush()
        sys.stderr.write('\r  提取资源 %d/%d 完成            \n' % (n_ok, len(plan)))

        if not html_parts:
            sys.exit('没找到 HTML 主体段，无法继续。')
        hp = html_parts[0]
        writer = Writer(imgdir, loc_map, base_map)
        dest_html = os.path.join(outdir, 'index.html')
        print('解码 HTML 主体 (%.1f MB 原始数据) ...' % mb(hp.end - hp.start))
        process_html_stream(f, hp, hp.charset or 'utf-8', writer,
                            dest_html, total_hint=hp.end - hp.start)
        f.close()
        print('  图片/附件 %d 个，共 %.1f MB' % (n_ok, mb(n_bytes)))
        print('  内联图外置 %d 个' % writer.n)
    else:
        print('结构   : 单文件 HTML（图片为内联 data URI 或外链）')
        head_bytes = b''
        with open(src, 'rb') as f:
            head_bytes = f.read(8192)
        m2 = re.search(rb'charset\s*=\s*["\']?([\w-]+)', head_bytes, re.I)
        charset = m2.group(1).decode('ascii', 'replace') if m2 else 'utf-8'
        print('编码   : %s' % charset)
        p = Part()
        p.start = 0
        p.end = size
        writer = Writer(imgdir)
        dest_html = os.path.join(outdir, 'index.html')
        f = open(src, 'rb')
        process_html_stream(f, p, charset, writer, dest_html, total_hint=size)
        f.close()
        print('  内联图外置 %d 个' % writer.n)

    hsize = os.path.getsize(dest_html)
    print('')
    print('完成！用时 %.1fs' % (time.time() - t0))
    print('  HTML : %s  (%.1f MB)' % (dest_html, mb(hsize)))
    print('  图片 : %s' % imgdir)

    if a.split_mb and hsize > a.split_mb * 1048576:
        print('HTML 仍较大，正在分页 ...')
        pages = split_html(dest_html, outdir, a.split_mb * 1048576)
        if pages:
            print('  已切成 %d 页：%s' % (len(pages), os.path.join(outdir, 'parts', 'index.html')))
    elif hsize > 40 * 1048576:
        print('  提示：HTML 仍偏大，可加 --split-mb 20 重新跑一次做分页。')


if __name__ == '__main__':
    main()
