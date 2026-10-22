# iptv-verified · 加强版 IPTV 源

多源聚合（Guovin、zbds、iptv-org），逐条按「网页能播」标准验证，只发布全部通过的频道。

验证四关（任一关不过即剔除）：

1. 流地址为 https（http 会被浏览器混合内容拦截）
2. 播放列表存活且响应带 `Access-Control-Allow-Origin`（无跨域许可网页拉不动）
3. 内容为有效 m3u8；主播放列表深入一层子列表同样验证
4. 第一个媒体分片（含 fMP4 初始化分片）同样可达且带跨域许可

订阅地址：`https://benfennet.github.io/iptv-verified/verified.m3u`

每 6 小时由 GitHub Actions 自动重新聚合验证（`.github/workflows/verify.yml`），
统计见发布页 `stats.json` 与首页。验证在境外节点执行，个别频道可能存在地域限制。

本地试跑：`LIMIT=30 python3 verify.py`（每个源只取前 30 条）。
