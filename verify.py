#!/usr/bin/env python3
"""加强版 IPTV 源验证器。

从多个上游聚合频道，逐条按「网页能播」标准验证，只保留全部通过的：
  1) 流地址必须是 https（否则浏览器混合内容拦截）
  2) 播放列表存活：带 Origin 请求返回 200，且响应带 Access-Control-Allow-Origin
  3) 内容是有效 m3u8（#EXTM3U 开头）；主列表则再深入一层子列表同样验证
  4) 第一个媒体分片同样 200 + 带 CORS
产物：out/verified.m3u、out/stats.json。
"""
import concurrent.futures as cf
import json
import os
import sys
import urllib.request
import urllib.error
from urllib.parse import urljoin, urlparse

ORIGIN = "https://app.benfen.net"
UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
TIMEOUT = 8
WORKERS = 40
LIMIT = int(os.environ.get("LIMIT", "0"))  # >0 时每个源只取前 N 条（本地试跑用）

SOURCES = [
    ("Guovin", "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u"),
    ("zbds", "https://live.zbds.top/tv/iptv4.m3u"),
    ("iptv-org-cn", "https://iptv-org.github.io/iptv/countries/cn.m3u"),
    ("iptv-org-hk", "https://iptv-org.github.io/iptv/countries/hk.m3u"),
    ("iptv-org-tw", "https://iptv-org.github.io/iptv/countries/tw.m3u"),
]


def fetch(url, max_bytes=65536):
    """返回 (status, headers_dict_lower, body_text)。失败抛异常。
    验流时只读 64KB 足够；拉源列表时要传大上限，否则大文件被截断丢频道。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Origin": ORIGIN,
        "Referer": "https://app.benfen.net/",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = r.read(max_bytes).decode("utf-8", "ignore")
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        return r.status, hdrs, body


def has_cors(hdrs):
    return "access-control-allow-origin" in hdrs


def first_media_line(playlist, base):
    """从 m3u8 文本里找第一个子列表或分片 URL（跳过注释行）。"""
    for line in playlist.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return urljoin(base, line)
    return None


def tag_uri(playlist, base):
    """EXT-X-MAP / EXT-X-KEY 的 URI 也要可达，这里只处理 MAP 初始化分片。"""
    for line in playlist.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-MAP:") and 'URI="' in line:
            uri = line.split('URI="')[1].split('"')[0]
            return urljoin(base, uri)
    return None


def check_segment(url):
    """分片验证：只要状态与 CORS，不读完整个分片。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Origin": ORIGIN,
        "Referer": "https://app.benfen.net/", "Range": "bytes=0-1023",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        r.read(1024)
        hdrs = {k.lower(): v for k, v in r.headers.items()}
        return r.status in (200, 206) and has_cors(hdrs)


def verify(ch):
    """返回 (ok, reason)。ch: dict(name, url, group, src, raw_extinf)。
    http 源先试同地址 https 版：不少源站双协议同开，列表只写了 http；
    升 https 后能过四关的，以 https 地址收录（标准不降，库里仍是纯 https）。"""
    url = ch["url"]
    if url.startswith("http://"):
        upgraded = "https://" + url[len("http://"):]
        ok, _ = verify_url(upgraded)
        if ok:
            ch["url"] = upgraded
            return True, "ok-upgraded"
        return False, "not-https"
    ok, reason = verify_url(url)
    return ok, reason


def verify_url(url):
    """对单个 https 地址执行存活/CORS/内容/分片四关。"""
    try:
        status, hdrs, body = fetch(url)
    except Exception:
        return False, "unreachable"
    if status != 200:
        return False, "bad-status"
    if not has_cors(hdrs):
        return False, "no-cors"
    if "#EXTM3U" not in body[:64]:
        return False, "not-m3u8"
    # 主播放列表：深入一层
    if "#EXT-X-STREAM-INF" in body:
        sub = first_media_line(body, url)
        if not sub:
            return False, "empty-master"
        try:
            s2, h2, b2 = fetch(sub)
        except Exception:
            return False, "sub-unreachable"
        if s2 != 200 or not has_cors(h2) or "#EXTM3U" not in b2[:64]:
            return False, "sub-invalid"
        url, body = sub, b2
    # 初始化分片（fMP4）
    init = tag_uri(body, url)
    if init:
        try:
            if not check_segment(init):
                return False, "init-invalid"
        except Exception:
            return False, "init-unreachable"
    # 第一个媒体分片
    seg = first_media_line(body, url)
    if not seg:
        return False, "no-segment"
    try:
        if not check_segment(seg):
            return False, "segment-invalid"
    except Exception:
        return False, "segment-unreachable"
    return True, "ok"


def parse_m3u(text, src):
    out, extinf, group = [], None, ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            extinf = line
            if 'group-title="' in line:
                group = line.split('group-title="')[1].split('"')[0]
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
        elif line and not line.startswith("#"):
            if extinf is not None:
                out.append({
                    "name": name or line, "url": line,
                    "group": group or "未分组", "src": src,
                    "extinf": extinf,
                })
            extinf, group = None, ""
    return out


def main():
    all_ch, seen = [], set()
    per_src = {}
    for src, url in SOURCES:
        try:
            _, _, text = fetch(url, max_bytes=16_000_000)
            items = parse_m3u(text, src)
        except Exception as e:
            print(f"[{src}] 拉取失败: {e}", file=sys.stderr)
            items = []
        if LIMIT:
            items = items[:LIMIT]
        fresh = []
        for it in items:
            # 验证机在海外，验不出地域限制；带这类标记的国内多半播不了，直接剔除
            if "[Geo-blocked]" in it["name"] or "[Not 24/7]" in it["name"]:
                continue
            if it["url"] not in seen:
                seen.add(it["url"])
                fresh.append(it)
        per_src[src] = {"total": len(items), "unique": len(fresh), "passed": 0}
        all_ch.extend(fresh)
        print(f"[{src}] 收录 {len(items)} 条，去重后 {len(fresh)} 条", file=sys.stderr)

    print(f"共 {len(all_ch)} 条待验证，并发 {WORKERS}", file=sys.stderr)
    passed, reasons = [], {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for ch, (ok, reason) in zip(all_ch, ex.map(verify, all_ch)):
            reasons[reason] = reasons.get(reason, 0) + 1
            if ok:
                passed.append(ch)
                per_src[ch["src"]]["passed"] += 1
    print(f"通过 {len(passed)}/{len(all_ch)}；失败分布 {reasons}", file=sys.stderr)

    passed.sort(key=lambda c: (c["group"], c["name"]))
    os.makedirs("out", exist_ok=True)
    with open("out/verified.m3u", "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        for ch in passed:
            f.write(ch["extinf"] + "\n" + ch["url"] + "\n")
    stats = {
        "total_checked": len(all_ch),
        "total_passed": len(passed),
        "fail_reasons": reasons,
        "per_source": per_src,
    }
    with open("out/stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    import datetime
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = "".join(
        f"<tr><td>{s}</td><td>{v['total']}</td><td>{v['passed']}</td></tr>"
        for s, v in per_src.items())
    with open("out/index.html", "w", encoding="utf-8") as f:
        f.write(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>加强版 IPTV 源 · 全部验证通过</title>
<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:40px auto;padding:0 16px;line-height:1.7;color:#222}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 12px}}code{{background:#f2f2f2;padding:2px 6px}}</style>
</head><body>
<h1>加强版 IPTV 源</h1>
<p>多源聚合，逐条按「网页能播」验证：https、存活、跨域许可、播放列表与分片可达，四关全过才收录。</p>
<p>订阅地址：<code>https://benfennet.github.io/iptv-verified/verified.m3u</code></p>
<p>最近验证：{now.strftime('%Y-%m-%d %H:%M')}（北京时间）· 共验 {len(all_ch)} 条，通过 {len(passed)} 条。</p>
<table><tr><th>来源</th><th>收录</th><th>通过</th></tr>{rows}</table>
<p>每 6 小时自动重新验证。验证在境外节点执行，个别频道可能有地域限制。</p>
</body></html>""")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
