# 海外藏中国文物平台 — Web 组 API 对接文档

> 版本：1.0  
> 更新：2026-05-31  
> 爬虫/数据组提供 · 图片与藏品列表 REST API

---

## 1. 服务信息

| 项目 | 值 |
|------|-----|
| **API 基址** | `http://47.96.152.190:8000` |
| **Swagger 文档** | http://47.96.152.190:8000/docs |
| **健康检查** | `GET /api/health` |
| **协议** | HTTP |
| **跨域 CORS** | 已开启（`allow_origins: *`），前端可直接 `fetch` |

**注意：** 服务部署在 Windows 服务器上，需保持 backend 进程运行；服务器重启后需重新启动或配置 Windows 服务。

---

## 2. 设计说明

### 2.1 图片如何展示

- 数据库中存有本地路径字段 `image_path` / `image_paths`（如 `images\harvard\100098_1.jpg` 或绝对路径）。
- **浏览器不能直接使用这些路径**，须通过本 API 由服务端读磁盘并以 HTTP 返回。
- 前端**只使用**列表接口返回的 `img_web`、`imgs_web` 字段。
- **不要使用** `image_url`（馆方远程 IIIF 链接），除非本地图缺失时自行做 fallback。

### 2.2 馆别编号 `museum_id`

| museum_id | 博物馆 | 约藏品条数 |
|-----------|--------|------------|
| 1 | 史密森尼 Smithsonian | ~2244 |
| 2 | 哈佛艺术博物馆 Harvard | ~4631 |
| 3 | 波士顿美术馆 MFA | ~180+ |

---

## 3. 接口一览

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/health` | 健康检查、MySQL 状态、图片根目录 |
| GET | `/api/artifacts` | 藏品列表（分页、筛选） |
| GET | `/api/artifacts/{museum_id}/{object_id}` | 藏品详情 |
| GET | `/api/img/{museum_id}/{object_id}` | 主图（索引 0） |
| GET | `/api/img/{museum_id}/{object_id}/{index}` | 多图第 N 张（index 从 0 起，第 2 张为 `/1`） |

---

## 4. 接口详情

### 4.1 健康检查

```
GET /api/health
```

**响应示例：**

```json
{
  "ok": true,
  "mysql": true,
  "image_roots": [
    "C:\\Users\\Administrator\\Desktop\\harvard\\harvard"
  ]
}
```

---

### 4.2 藏品列表

```
GET /api/artifacts
```

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| museum_id | int | 否 | 馆别 1/2/3；不传则三馆混合 |
| page | int | 否 | 页码，从 1 开始，默认 1 |
| size | int | 否 | 每页条数，默认 20，**最大 100** |
| q | string | 否 | 标题关键词（模糊匹配） |
| dynasty | string | 否 | 朝代（模糊匹配） |
| material | string | 否 | 材质（模糊匹配） |

**请求示例：**

```
GET /api/artifacts?museum_id=2&page=1&size=24
GET /api/artifacts?museum_id=2&q=bowl&dynasty=Song
```

**响应示例：**

```json
{
  "page": 1,
  "size": 24,
  "total": 4631,
  "list": [
    {
      "museum_id": 2,
      "object_id": "100098",
      "title": "Bowl with Indented Lip, White Rim, and Five Russet Splashes",
      "artist": "",
      "dynasty": "Southern Song（南宋）",
      "material": "Northern black ware of Cizhou type...",
      "type": "Ceramics",
      "museum": "Harvard Art Museums",
      "image_count": 2,
      "img_web": "/api/img/2/100098",
      "imgs_web": [
        "/api/img/2/100098",
        "/api/img/2/100098/1"
      ],
      "has_local_image": true
    }
  ]
}
```

**字段说明（列表项）：**

| 字段 | 说明 |
|------|------|
| img_web | 主图 API 路径（相对路径，需拼基址） |
| imgs_web | 全部本地图 API 路径数组（含多图） |
| has_local_image | `true` 表示磁盘有图，可展示；`false` 跳过 |
| image_count | 图片张数 |

---

### 4.3 藏品详情

```
GET /api/artifacts/{museum_id}/{object_id}
```

**示例：**

```
GET /api/artifacts/2/100098
```

在列表字段基础上，额外返回：`period`、`culture`、`description`、`provenance`、`dimensions`、`location`、`detail_url`、`credit_line`、`accession_number`、`crawl_date` 等。

---

### 4.4 图片

**主图：**

```
GET /api/img/{museum_id}/{object_id}
```

**多图（第 2 张起）：**

```
GET /api/img/2/100098/1
GET /api/img/2/100098/2
```

**响应：** 二进制图片流（`image/jpeg` / `image/png` 等）

**完整 URL 示例：**

```
http://47.96.152.190:8000/api/img/2/100098
```

---

## 5. 前端集成

### 5.1 常量

```javascript
const API_BASE = "http://47.96.152.190:8000";
```

### 5.2 列表 + 图片展示

```javascript
async function loadPage(museumId = 2, page = 1, size = 24) {
  const res = await fetch(
    `${API_BASE}/api/artifacts?museum_id=${museumId}&page=${page}&size=${size}`
  );
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

// 渲染
const { list } = await loadPage(2, 1, 24);
list.forEach((item) => {
  if (!item.has_local_image || !item.img_web) return;
  const img = document.createElement("img");
  img.src = API_BASE + item.img_web;
  img.alt = item.title;
  img.loading = "lazy";
  document.body.appendChild(img);
});
```

### 5.3 分页加载全部

```javascript
async function fetchAllArtifacts(museumId = 2) {
  const all = [];
  let page = 1;
  const size = 100;
  while (true) {
    const res = await fetch(
      `${API_BASE}/api/artifacts?museum_id=${museumId}&page=${page}&size=${size}`
    );
    const data = await res.json();
    all.push(...data.list);
    if (page * size >= data.total) break;
    page++;
  }
  return all;
}
```

### 5.4 HTML 直接使用

```html
<img
  src="http://47.96.152.190:8000/api/img/2/100098"
  alt="Bowl with Indented Lip"
  loading="lazy"
/>
```

### 5.5 Vue 示例

```vue
<template>
  <div class="grid">
    <article v-for="item in list" :key="item.object_id">
      <img
        v-if="item.img_web"
        :src="API + item.img_web"
        :alt="item.title"
        loading="lazy"
      />
      <h3>{{ item.title }}</h3>
      <p>{{ item.dynasty }}</p>
    </article>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue";
const API = "http://47.96.152.190:8000";
const list = ref([]);

onMounted(async () => {
  const res = await fetch(`${API}/api/artifacts?museum_id=2&page=1&size=24`);
  const data = await res.json();
  list.value = data.list.filter((x) => x.has_local_image);
});
</script>
```

---

## 6. 示例页面

项目内提供两个可直接打开的 HTML 参考页：

| 文件 | 说明 |
|------|------|
| `backend/gallery.html` | **推荐**：瀑布流、滚动加载、搜索、灯箱、多图 |
| `backend/web_demo.html` | 简单联调页 |

用法：浏览器打开 `gallery.html`，默认已配置 API 基址。

---

## 7. 后端源码（备查）

```
backend/
├── main.py              # FastAPI 主程序
├── db_helper.py         # MySQL（PyMySQL）
├── image_resolver.py    # 本地路径 → 磁盘文件
├── requirements.txt
├── .env.example         # 配置模板
├── gallery.html
├── web_demo.html
└── test_image_api.py    # 命令行测试脚本
```

**本地测试 API：**

```powershell
python test_image_api.py http://47.96.152.190:8000
```

---

## 8. MySQL（可选，非图片展示必需）

若 Web 组需直连数据库做用户系统、收藏等扩展功能：

| 项 | 值 |
|----|-----|
| Host | `47.96.152.190` |
| Port | `3306` |
| Database | `overseas_chinese_artifacts` |
| 主表 | `artifact` |
| 主键 | `(museum_id, object_id)` |

**账号密码请向爬虫组私下索取，勿提交至 Git。**

**仅做图片/列表展示时，不需要直连 MySQL，走 API 即可。**

---

## 9. 常见问题

### Q1：`has_local_image` 为 false？

服务器磁盘上找不到对应图片文件。联系爬虫组检查 `HARVARD_IMAGE_DIR` 配置及图片是否已上传。

### Q2：图片加载慢？

- 列表使用分页，不要一次请求全部 4631 条。
- 图片标签加 `loading="lazy"`。
- 推荐滚动到底再加载下一页（参见 `gallery.html`）。

### Q3：API 无法访问？

1. 确认 `GET /api/health` 是否 200。
2. 服务器上 backend 是否在运行（`uvicorn main:app --host 0.0.0.0 --port 8000`）。
3. 云安全组 / Windows 防火墙是否放行 **TCP 8000**。

### Q4：能否用 `image_url`？

本 API 设计为使用**本地已下载图片**。优先 `img_web`；`image_url` 为馆方远程链接，未在本 API 中代理。

### Q5：CORS 报错？

本服务已配置允许所有来源。若仍报错，检查是否请求了错误地址或 API 未启动。

---

## 10. 联调检查清单

- [ ] `GET http://47.96.152.190:8000/api/health` 返回 200
- [ ] `GET http://47.96.152.190:8000/api/artifacts?museum_id=2&size=5` 返回列表
- [ ] 列表中 `has_local_image: true` 且 `img_web` 有值
- [ ] 浏览器能打开 `http://47.96.152.190:8000/api/img/2/100098` 看到图片
- [ ] 打开 `gallery.html` 瀑布流正常

---

## 11. 联系方式

- **API / 数据 / 图片路径问题**：爬虫组  
- **Swagger 在线调试**：http://47.96.152.190:8000/docs  

---

*文档结束*
