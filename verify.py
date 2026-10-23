#!/usr/bin/env python3
"""加强版 IPTV 源验证器（双轨）。

从多个上游聚合频道，逐条探测后按两套标准各出一份名单：
  · 全验证版 verified.m3u ——「网页能播」严标准，四关全过才收：
    1) https（http 先试同址 https 升级，能升则升）
    2) 播放列表存活：带 Origin 请求返回 200，且响应带跨域许可 CORS
    3) 内容是有效 m3u8（#EXTM3U 开头）；主列表深入一层子列表同样验证
    4) 第一个媒体分片（及 fMP4 初始化分片）同样 200 + 带 CORS
  · 本地代理版 proxy.m3u —— 给开了本机代理的用户：代理会补 CORS、代取 http，
    故只验「活着 + 列表有效 + 分片可达」，http 与无 CORS 照收。
    名单为全集（严标准通过的也在内，地址用升级后的 https）。
产物：out/verified.m3u、out/proxy.m3u、out/stats.json、out/index.html。
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

# 已证实的假台农场（一个流冒充多个频道名、实测为广告循环），聚合时直接剔除
BLOCK_HOSTS = {"173.208.212.130:8181"}

SOURCES = [
    ("Guovin", "https://cdn.jsdelivr.net/gh/Guovin/iptv-api@gd/output/result.m3u"),
    ("zbds", "https://live.zbds.top/tv/iptv4.m3u"),
    ("iptv-org-cn", "https://iptv-org.github.io/iptv/countries/cn.m3u"),
    ("iptv-org-hk", "https://iptv-org.github.io/iptv/countries/hk.m3u"),
    ("iptv-org-tw", "https://iptv-org.github.io/iptv/countries/tw.m3u"),
    ("iptv-org-zho", "https://iptv-org.github.io/iptv/languages/zho.m3u"),
    ("fanmingming", "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u"),
    ("yanG", "https://raw.githubusercontent.com/yanG-1989/m3u/main/Gather.m3u"),
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


def segment_flags(url):
    """探一个分片/初始化段，返回 (可达, 带CORS)。不读完整个分片。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Origin": ORIGIN,
        "Referer": "https://app.benfen.net/", "Range": "bytes=0-1023",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            r.read(1024)
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            return r.status in (200, 206), has_cors(hdrs)
    except Exception:
        return False, False


def variant_info(master_body):
    """从主播放列表解析最佳变体的 (高度, 码率)。挑流与去重用真参数，不靠台名猜。"""
    best_h, best_bw = 0, 0
    for m in re.finditer(r"#EXT-X-STREAM-INF:([^\n]+)", master_body):
        attrs = m.group(1)
        rm = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
        bm = re.search(r"BANDWIDTH=(\d+)", attrs)
        h = int(rm.group(2)) if rm else 0
        bw = int(bm.group(1)) if bm else 0
        if h > best_h or (h == best_h and bw > best_bw):
            best_h, best_bw = h, bw
    return best_h, best_bw


def probe(url):
    """一次探测，把每关结果都记下来，供两套标准分别判定。"""
    d = {"alive": False, "cors": False, "m3u8": False,
         "sub_ok": True, "sub_cors": True,
         "init_ok": True, "init_cors": True,
         "seg_ok": False, "seg_cors": False,
         "seg2_ok": True, "seg2_cors": True,
         "resh": 0, "bw": 0}
    try:
        status, hdrs, body = fetch(url)
    except Exception:
        return d
    if status != 200:
        return d
    d["alive"] = True
    d["cors"] = has_cors(hdrs)
    if "#EXTM3U" not in body[:64]:
        return d
    d["m3u8"] = True
    # 主播放列表：深入一层（并记下最佳变体的真实分辨率/码率）
    if "#EXT-X-STREAM-INF" in body:
        d["resh"], d["bw"] = variant_info(body)
        sub = first_media_line(body, url)
        if not sub:
            d["sub_ok"] = False
            return d
        try:
            s2, h2, b2 = fetch(sub)
        except Exception:
            d["sub_ok"] = False
            return d
        if s2 != 200 or "#EXTM3U" not in b2[:64]:
            d["sub_ok"] = False
            return d
        d["sub_cors"] = has_cors(h2)
        url, body = sub, b2
    # 初始化分片（fMP4）
    init = tag_uri(body, url)
    if init:
        d["init_ok"], d["init_cors"] = segment_flags(init)
        if not d["init_ok"]:
            return d
    # 首尾两个媒体分片都验：只验第一片会放过「头活尾死」的假活，
    # 尾片是最新分片，拉得到才证明这条流此刻真的活着
    seg_lines = [ln.strip() for ln in body.splitlines()
                 if ln.strip() and not ln.startswith("#")]
    if not seg_lines:
        return d
    from urllib.parse import urljoin as _uj
    d["seg_ok"], d["seg_cors"] = segment_flags(_uj(url, seg_lines[0]))
    if not d["seg_ok"]:
        return d
    if len(seg_lines) > 1:
        d["seg2_ok"], d["seg2_cors"] = segment_flags(_uj(url, seg_lines[-1]))
    return d


def pass_strict(d):
    """全验证版：https 前提下（由 verify 保证）每关都要 CORS。"""
    return (d["alive"] and d["cors"] and d["m3u8"] and d["sub_ok"]
            and d["sub_cors"] and d["init_ok"] and d["init_cors"]
            and d["seg_ok"] and d["seg_cors"]
            and d["seg2_ok"] and d["seg2_cors"])


def pass_loose(d):
    """本地代理版：只要求活着、列表有效、首尾分片可达。"""
    return (d["alive"] and d["m3u8"] and d["sub_ok"]
            and d["init_ok"] and d["seg_ok"] and d["seg2_ok"])


def verify(ch):
    """返回 'strict'（全验证）/ 'proxy'（仅代理版）/ None。
    http 源先探同址 https：能严过最好，次之宽过也以 https 收进代理版；
    https 完全不通再探 http 原址的宽标准。"""
    url = ch["url"]
    if url.startswith("http://"):
        up = "https://" + url[len("http://"):]
        d = probe(up)
        if pass_strict(d):
            ch["url"] = up
            ch["_resh"], ch["_bw"] = d["resh"], d["bw"]
            return "strict"
        if pass_loose(d):
            ch["url"] = up
            ch["_resh"], ch["_bw"] = d["resh"], d["bw"]
            return "proxy"
        d = probe(url)
        if pass_loose(d):
            ch["_resh"], ch["_bw"] = d["resh"], d["bw"]
            return "proxy"
        return None
    d = probe(url)
    if pass_strict(d):
        ch["_resh"], ch["_bw"] = d["resh"], d["bw"]
        return "strict"
    if pass_loose(d):
        ch["_resh"], ch["_bw"] = d["resh"], d["bw"]
        return "proxy"
    return None


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


# —— 名单整理：统一分类 / 组内排序 / 同名去重（借鉴收音机的做法）——
import re

GROUPS = ["央视", "卫视", "港澳台", "地方台", "海外", "其他"]
WS_ORDER = ["湖南", "东方", "浙江", "江苏", "北京", "安徽", "天津", "深圳",
            "重庆", "山东", "河南", "湖北", "四川", "江西", "广东", "河北",
            "辽宁", "东南", "贵州", "云南", "广西", "陕西", "山西", "吉林",
            "黑龙江", "海南", "内蒙古", "宁夏", "青海", "甘肃", "新疆",
            "西藏", "海峡", "兵团", "延边", "厦门", "三沙"]
WS_KIDS = ("卡酷", "炫动卡通", "金鹰卡通", "嘉佳卡通", "优漫卡通", "哈哈炫动")
HMT_KEYS = ("凤凰", "翡翠", "明珠", "香港", "澳门", "澳视", "澳亚", "民视",
            "中视", "华视", "台视", "公视", "东森", "中天", "三立", "纬来",
            "星卫", "TVB", "有线电视", "人间卫视", "大爱", "好消息")
PROVINCES = ["北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
             "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
             "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
             "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆"]
_CITY_SPEC = """
江苏:南京 苏州 无锡 常州 徐州 南通 扬州 盐城 淮安 连云港 泰州 宿迁 镇江
浙江:杭州 宁波 温州 嘉兴 湖州 绍兴 金华 台州 丽水 衢州 舟山
安徽:合肥 芜湖 蚌埠 阜阳 亳州 宿州 淮南 滁州 六安 马鞍山 淮北 安庆 黄山
福建:福州 厦门 泉州 漳州 莆田 龙岩 三明 南平 宁德
江西:南昌 赣州 九江 上饶 宜春 吉安 抚州 景德镇 萍乡 新余 鹰潭
山东:济南 青岛 烟台 潍坊 临沂 济宁 菏泽 枣庄 滨州 东营 威海 日照 聊城 德州 泰安 淄博
河南:郑州 开封 洛阳 南阳 新乡 商丘 周口 驻马店 信阳 安阳 许昌 平顶山 焦作 鹤壁 三门峡 漯河 濮阳
湖北:武汉 宜昌 襄阳 十堰 黄石 荆州 荆门 鄂州 孝感 黄冈 咸宁 随州 恩施
湖南:长沙 株洲 湘潭 衡阳 岳阳 常德 益阳 郴州 永州 邵阳 张家界 怀化 娄底 湘西
广东:广州 深圳 珠海 汕头 佛山 东莞 中山 江门 湛江 茂名 惠州 梅州 汕尾 河源 阳江 清远 潮州 揭阳 云浮 韶关 肇庆
广西:南宁 柳州 桂林 梧州 北海 玉林 百色 钦州 贵港 贺州 河池 来宾 崇左 防城港
海南:海口 三亚 儋州
四川:成都 自贡 攀枝花 泸州 德阳 绵阳 广元 遂宁 内江 南充 乐山 宜宾 广安 达州 眉山 雅安 巴中 资阳 阿坝 甘孜 凉山
贵州:贵阳 遵义 安顺 铜仁 毕节 六盘水 黔东南 黔南 黔西南
云南:昆明 曲靖 玉溪 保山 昭通 丽江 普洱 临沧 楚雄 红河 文山 大理 德宏 怒江 迪庆 西双版纳
西藏:拉萨 昌都 林芝 山南 那曲 阿里 日喀则
陕西:西安 铜川 宝鸡 咸阳 渭南 延安 汉中 榆林 安康 商洛
甘肃:兰州 嘉峪关 金昌 白银 天水 武威 张掖 平凉 酒泉 庆阳 定西 陇南 临夏 甘南
青海:西宁 海东 海北 黄南 果洛 玉树 海西
宁夏:银川 石嘴山 吴忠 固原 中卫
新疆:乌鲁木齐 克拉玛依 吐鲁番 哈密 昌吉 博尔塔拉 巴音郭楞 阿克苏 克孜勒苏 喀什 和田 伊犁 塔城 阿勒泰 石河子
河北:石家庄 唐山 秦皇岛 邯郸 邢台 保定 张家口 承德 沧州 廊坊 衡水
山西:太原 大同 阳泉 长治 晋城 朔州 晋中 运城 忻州 临汾 吕梁
内蒙古:呼和浩特 包头 乌海 赤峰 通辽 鄂尔多斯 呼伦贝尔 巴彦淖尔 乌兰察布 兴安 锡林郭勒 阿拉善
辽宁:沈阳 大连 鞍山 抚顺 本溪 丹东 锦州 营口 阜新 辽阳 盘锦 铁岭 朝阳 葫芦岛
吉林:长春 吉林 四平 辽源 通化 白山 松原 白城
黑龙江:哈尔滨 齐齐哈尔 鸡西 鹤岗 双鸭山 大庆 伊春 佳木斯 七台河 牡丹江 黑河 绥化 大兴安岭
"""
CITY2PROV = {}
for _line in _CITY_SPEC.strip().splitlines():
    _p, _cs = _line.split(":")
    for _c in _cs.split():
        CITY2PROV[_c] = _p


def classify(name):
    n = name.upper().replace(" ", "")
    if "CCTV" in n or "CGTN" in n or "央视" in name or \
            "中国教育电视台" in name or n.startswith("CETV"):
        return "央视"
    if any(k in name for k in HMT_KEYS if k != "TVB") or \
            re.search(r"\bTVB\b", name.upper()):
        return "港澳台"
    if "卫视" in name or any(k in name for k in WS_KIDS):
        return "卫视"
    if re.search(r"[\u4e00-\u9fff]", name):
        if "轮播" in name or "点播" in name:
            return "其他"
        return "地方台"
    return "海外"


def province_of(name):
    if "海南州" in name:
        return "青海"
    for p in PROVINCES:
        if name.startswith(p):
            return p
    for city, prov in CITY2PROV.items():
        if name.startswith(city):
            return prov
    return ""


def sort_key(ch):
    g = classify(ch["name"])
    n = ch["name"]
    gi = GROUPS.index(g)
    if g == "央视":
        m = re.search(r"CCTV[- ]?(\d+)\s*(\+)?", n.upper())
        if m:
            sub = (0, int(m.group(1)), 1 if m.group(2) else 0)
        elif "CGTN" in n.upper():
            sub = (1, 0, 0)
        else:
            sub = (2, 0, 0)
        return (gi, sub, n)
    if g == "卫视":
        r = next((i for i, w in enumerate(WS_ORDER) if w + "卫视" in n),
                 len(WS_ORDER))
        return (gi, (r, 0, 0), n)
    if g == "地方台":
        p = province_of(n)
        pr = PROVINCES.index(p) if p in PROVINCES else len(PROVINCES)
        return (gi, (pr, 0, 0), n)
    return (gi, (0, 0, 0), n)


def norm_name(name):
    n = name.upper()
    n = re.sub(r"[\s·\-—_()（）\[\]【】]", "", n)
    while True:
        n2 = re.sub(r"(2160P|1080P|1080I|720P|720I|576P|576I|480P|480I|"
                    r"4K|8K|FHD|UHD|HD|高清|超清|标清)$", "", n)
        if n2 == n:
            return n
        n = n2


def _eff_height(ch):
    """有效清晰度（越高越好）：优先主列表的真实分辨率，没有就按台名标记估，再没有按标清。"""
    if ch.get("_resh"):
        return ch["_resh"]
    n = ch["name"].upper()
    for k, h in (("2160", 2160), ("4K", 2160), ("1080", 1080),
                 ("720", 720), ("576", 576), ("480", 480)):
        if k in n:
            return h
    return 360


def dedupe(pairs):
    """pairs: [(ch, tier)]。同名只留最好的一条：
    严标准 > 代理；https > http；有域名的 > 裸 IP（裸 IP 多为来历不明的中转）；清晰度高优先（真参数）。"""
    best = {}
    for ch, tier in pairs:
        key = norm_name(ch["name"])
        bare = 1 if re.match(r"\d+\.\d+\.\d+\.\d+",
                             urlparse(ch["url"]).netloc) else 0
        rank = (0 if tier == "strict" else 1,
                0 if ch["url"].startswith("https") else 1,
                bare, -_eff_height(ch))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, ch)
    return [v[1] for v in best.values()]


def with_group(extinf, group):
    if 'group-title="' in extinf:
        return re.sub(r'group-title="[^"]*"',
                      f'group-title="{group}"', extinf, count=1)
    if extinf.startswith("#EXTINF:-1"):
        return extinf.replace("#EXTINF:-1",
                              f'#EXTINF:-1 group-title="{group}"', 1)
    return extinf


def load_state():
    """读上轮状态（工作流从 gh-pages 签出到 prev/state.json）。没有就当首轮。"""
    p = os.environ.get("STATE_IN", "prev/state.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("channels", {})
    except Exception:
        return {}


def apply_grace(passed, proxy_only, state):
    """两轮淘汰制：上轮在册、本轮没通过的先宽限保留一轮（fails=1），
    连续两轮没通过（fails>=2）才下架。本轮通过的 fails 清零。
    返回 (宽限严版, 宽限代理版, 本轮下架数, 新状态)。"""
    new_state = {}
    for tier, lst in (("strict", passed), ("proxy", proxy_only)):
        for c in lst:
            new_state[c["url"]] = {"name": c["name"], "extinf": c["extinf"],
                                   "tier": tier, "fails": 0}
    keep_s, keep_p, dropped = [], [], 0
    for url, e in state.items():
        if url in new_state:
            continue
        f = int(e.get("fails", 0)) + 1
        if f >= 2:
            dropped += 1
            continue
        ch = {"name": e["name"], "url": url, "extinf": e["extinf"],
              "group": "", "src": "grace"}
        (keep_s if e.get("tier") == "strict" else keep_p).append(ch)
        new_state[url] = {"name": e["name"], "extinf": e["extinf"],
                          "tier": e.get("tier", "proxy"), "fails": f}
    return keep_s, keep_p, dropped, new_state


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
            if urlparse(it["url"]).netloc in BLOCK_HOSTS:
                continue
            if it["url"] not in seen:
                seen.add(it["url"])
                fresh.append(it)
        per_src[src] = {"total": len(items), "unique": len(fresh), "passed": 0}
        all_ch.extend(fresh)
        print(f"[{src}] 收录 {len(items)} 条，去重后 {len(fresh)} 条", file=sys.stderr)

    print(f"共 {len(all_ch)} 条待验证，并发 {WORKERS}", file=sys.stderr)
    passed, proxy_only = [], []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for ch, tier in zip(all_ch, ex.map(verify, all_ch)):
            if tier == "strict":
                passed.append(ch)
                per_src[ch["src"]]["passed"] += 1
            elif tier == "proxy":
                proxy_only.append(ch)
    # 嫌疑报告：一台服务器冒充 ≥3 个不同台名 → 疑似假台农场，列出来人工确认
    # （不自动剔除：正规运营商也常一机多台、真台还有别名，误杀正宗的代价更大；
    #  确认属实后把主机加进 BLOCK_HOSTS 即可）
    from collections import defaultdict
    by_host = defaultdict(set)
    for ch in passed + proxy_only:
        by_host[urlparse(ch["url"]).netloc].add(norm_name(ch["name"]))
    suspect = {h: sorted(ns) for h, ns in by_host.items() if len(ns) >= 3}
    if suspect:
        print(f"嫌疑主机（待人工确认）：{suspect}", file=sys.stderr)
    # 两轮淘汰制：合并上轮状态，本轮没过的先宽限一轮
    g_strict, g_proxy, dropped2, new_state = apply_grace(
        passed, proxy_only, load_state())
    if g_strict or g_proxy or dropped2:
        print(f"两轮淘汰：宽限保留 严 {len(g_strict)} / 代理 {len(g_proxy)}，"
              f"连续两轮失败下架 {dropped2}", file=sys.stderr)
    passed = passed + g_strict
    proxy_only = proxy_only + g_proxy
    all_pass = passed + proxy_only
    print(f"全验证 {len(passed)} 条，代理版另 {len(proxy_only)} 条"
          f"（合计 {len(all_pass)}/{len(all_ch)}）", file=sys.stderr)

    # 统一分类、去重、排序（group-title 重写为统一分类）
    passed = sorted(dedupe([(c, "strict") for c in passed]), key=sort_key)
    all_pass = sorted(dedupe([(c, "strict") for c in passed] +
                             [(c, "proxy") for c in proxy_only]), key=sort_key)
    from collections import Counter
    grp_counts = dict(Counter(classify(c["name"]) for c in all_pass))
    print(f"去重后：全验证 {len(passed)} 条，代理版 {len(all_pass)} 条；"
          f"代理版分类 {grp_counts}", file=sys.stderr)

    os.makedirs("out", exist_ok=True)
    with open("out/verified.m3u", "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        for ch in passed:
            f.write(with_group(ch["extinf"], classify(ch["name"]))
                    + "\n" + ch["url"] + "\n")
    with open("out/proxy.m3u", "w", encoding="utf-8") as f:
        f.write('#EXTM3U x-tvg-url=""\n')
        for ch in all_pass:
            f.write(with_group(ch["extinf"], classify(ch["name"]))
                    + "\n" + ch["url"] + "\n")
    stats = {
        "total_checked": len(all_ch),
        "total_passed": len(passed),
        "proxy_total": len(all_pass),
        "proxy_only": len(all_pass) - len(passed),
        "groups": grp_counts,
        "suspect_hosts": suspect,
        "grace_kept": {"strict": len(g_strict), "proxy": len(g_proxy)},
        "dropped_2strikes": dropped2,
        "per_source": per_src,
    }
    with open("out/stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    import datetime as _dt
    with open("out/state.json", "w", encoding="utf-8") as f:
        json.dump({"run": _dt.datetime.now(
            _dt.timezone(_dt.timedelta(hours=8))).isoformat(),
            "channels": new_state}, f, ensure_ascii=False)

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
<p>多源聚合，逐条验证后出两份名单：</p>
<p><b>自有源 · 直连版</b>（全验证，网页直连）：https、存活、跨域许可、播放列表与分片可达，四关全过才收录。<br>
订阅地址：<code>https://benfennet.github.io/iptv-verified/verified.m3u</code></p>
<p><b>自有源 · 代理版</b>（配合本机代理使用）：只验活着、列表有效、分片可达，http 与无跨域许可的也收，台数更多。<br>
订阅地址：<code>https://benfennet.github.io/iptv-verified/proxy.m3u</code></p>
<p>最近验证：{now.strftime('%Y-%m-%d %H:%M')}（北京时间）· 共验 {len(all_ch)} 条，全验证通过 {len(passed)} 条，代理版共 {len(all_pass)} 条。</p>
<p>名单采用两轮淘汰制：频道要连续两轮验证失败才下架，偶发抖动不会让好台消失。</p>
<table><tr><th>来源</th><th>收录</th><th>通过</th></tr>{rows}</table>
<p>每 6 小时自动重新验证。验证在境外节点执行，个别频道可能有地域限制。</p>
</body></html>""")
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
