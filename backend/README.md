# 海外藏中国文物 — 图片/藏品 API 对接说明

**提供方：** 爬虫/数据组  
**更新：** 2026-06-01  
**详细文档：** [Web组对接文档.md](./Web组对接文档.md)  
**示例页面：** [gallery.html](./gallery.html)、[web_demo.html](./web_demo.html)

---

## 服务地址

| 项目 | 地址 |
|------|------|
| **API 基址** | `http://47.96.152.190:8000` |
| **Swagger 在线文档** | http://47.96.152.190:8000/docs |
| **健康检查** | `GET /api/health` |
| **跨域 CORS** | 已开启，前端可直接 `fetch` |

> **联调前请先确认服务已启动**（服务器上运行 `uvicorn main:app --host 0.0.0.0 --port 8000`）。若 `/api/health` 超时，联系爬虫组重启 backend。

---

## 馆别编号 `museum_id`

| museum_id | 博物馆 | 约藏品数 |
|-----------|--------|----------|
| 1 | 史密森尼 Smithsonian | 2244 |
| 2 | 哈佛艺术博物馆 Harvard | 4625 |
| 3 | 波士顿美术馆 MFA | 263 |

**主键：** `(museum_id, object_id)`，例如哈佛 `100098`、MFA `98.12`。

---

## 接口列表

| 用途 | 方法 | 路径 |
|------|------|------|
| 健康检查 | GET | `/api/health` |
| 藏品列表（分页/筛选） | GET | `/api/artifacts` |
| 藏品详情 | GET | `/api/artifacts/{museum_id}/{object_id}` |
| **主图** | GET | `/api/img/{museum_id}/{object_id}` |
| **多图第 N 张** | GET | `/api/img/{museum_id}/{object_id}/{index}` |

**图片 URL 示例：**

```
http://47.96.152.190:8000/api/img/2/100098      ← 哈佛主图
http://47.96.152.190:8000/api/img/2/100098/1    ← 哈佛第 2 张
http://47.96.152.190:8000/api/img/3/98.12       ← MFA 主图
```

---

## 前端对接规则（重要）

1. **不要用** 数据库字段 `image_path`、`image_url`（浏览器无法直接访问）。
2. **只用** 列表/详情接口返回的 **`img_web`**、**`imgs_web`**。
3. 图片完整地址 = **`API_BASE + img_web`**。
4. `has_local_image === false` 时本地无图，请显示占位图。
5. 列表请**分页**请求，`size` 最大 **100**，不要一次拉全库。

---

## 列表接口

```
GET /api/artifacts?museum_id=2&page=1&size=24
GET /api/artifacts?museum_id=3&q=bowl&dynasty=Song
```

**Query 参数：**

| 参数 | 说明 |
|------|------|
| museum_id | 1/2/3，不传则三馆混合 |
| page | 页码，从 1 开始 |
| size | 每页条数，默认 20，最大 100 |
| q | 标题关键词 |
| dynasty | 朝代（模糊） |
| material | 材质（模糊） |

**响应关键字段：**

```json
{
  "museum_id": 2,
  "object_id": "100098",
  "title": "Bowl with Indented Lip...",
  "artist": "",
  "dynasty": "Southern Song（南宋）",
  "material": "...",
  "type": "Ceramics",
  "museum": "Harvard Art Museums",
  "image_count": 2,
  "img_web": "/api/img/2/100098",
  "imgs_web": ["/api/img/2/100098", "/api/img/2/100098/1"],
  "has_local_image": true
}
```

---

## 前端代码示例

```javascript
const API_BASE = "http://47.96.152.190:8000";

// 1. 拉列表
const res = await fetch(
  `${API_BASE}/api/artifacts?museum_id=2&page=1&size=24`
);
const { list, total } = await res.json();

// 2. 展示图片（只展示有图的）
list.filter((x) => x.has_local_image).forEach((item) => {
  const img = document.createElement("img");
  img.src = API_BASE + item.img_web;
  img.alt = item.title;
  img.loading = "lazy";
  document.body.appendChild(img);
});

// 3. 详情
const detail = await fetch(`${API_BASE}/api/artifacts/2/100098`).then((r) =>
  r.json()
);
```

**Vue / React：** 绑定 `:src="API_BASE + item.img_web"` 即可。

**HTML 直链：**

```html
<img
  src="http://47.96.152.190:8000/api/img/2/100098"
  alt="..."
  loading="lazy"
/>
```

---

## 联调自检

- [ ] 浏览器打开 http://47.96.152.190:8000/api/health → 返回 JSON
- [ ] 打开 http://47.96.152.190:8000/api/artifacts?museum_id=2&size=5 → 有列表且 `has_local_image: true`
- [ ] 打开 http://47.96.152.190:8000/api/img/2/100098 → 能看到图片
- [ ] 本地打开 `gallery.html` 瀑布流正常

**命令行测试：**

```powershell
cd backend
python test_server_image_api.py
python test_image_api.py http://47.96.152.190:8000
```

---

## 服务端启动（爬虫组）

```powershell
cd backend
pip install -r requirements.txt
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

**开机自启：** Win+R → `taskschd.msc`，或见 [Web组对接文档.md](./Web组对接文档.md) 中 NSSM / 计划任务说明。

**`.env` 图片目录示例：**

```env
HARVARD_IMAGE_DIR=C:\Users\Administrator\Desktop\harvard\harvard
IMAGE_EXTRA_DIRS=C:\Users\Administrator\Desktop\mfa\mfa
```

---

## 常见问题

| 问题 | 处理 |
|------|------|
| API 超时 / 无法访问 | backend 未启动或 8000 端口未放行，联系爬虫组 |
| `has_local_image: false` | 该条本地暂无图片，显示占位图 |
| 图片慢 | 分页 + `loading="lazy"`，参考 `gallery.html` |
| 需要 MySQL 账号？ | **仅做展示不需要**，走 API 即可；用户/收藏等扩展功能再私聊要库账号 |

---

## 项目结构

```
backend/
├── README.md              # 本文件（给其他组）
├── Web组对接文档.md        # 完整对接文档
├── main.py                # FastAPI 主程序
├── db_helper.py           # MySQL
├── image_resolver.py      # 本地路径 → 磁盘文件
├── gallery.html           # 瀑布流示例（推荐）
├── web_demo.html          # 简单联调页
├── test_server_image_api.py
└── requirements.txt
```

---

## 一句话总结

基址 `http://47.96.152.190:8000`，列表调 `/api/artifacts`，图片用返回的 `img_web` 拼完整 URL，不要用 `image_path`。

**联系方式：** API / 图片 / 数据问题 → 爬虫组 · 在线调试 → http://47.96.152.190:8000/docs
