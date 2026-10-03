# GitHub 视觉资源

此目录只存放静态文档图片与可编辑分享图源，不参与应用构建。

## 来源与边界

- 截图日期：**2026-10-03**；来源：公开演示站点 <https://repopulse.slak7.cn>。
- 使用独立公开 Playwright Chromium，浅色主题；等待字体、榜单与趋势加载后截图，通过真实可访问按钮关闭首次引导。
- 保留站点实际仓库、数字、日期与数据完整性提示；不注入数据或修改页面文案，不请求 AI、不配置密钥，不展示未经生成的译文。
- 截图是该次浏览的界面记录，不代表当前实时榜单，也不代表全 GitHub 排名。

| 文件 | 尺寸 / 浏览视口 | 字节数 | 来源与画面 |
| --- | --- | ---: | --- |
| `home.png` | 1440×1080 | 337,547 | [公开首页](https://repopulse.slak7.cn/)，7 天周期，页面顶部 |
| `detail.png` | 1440×1280 | 227,211 | [VoiceStudio 详情](https://repopulse.slak7.cn/repo/debpalash/VoiceStudio?returnTo=%2F%3Fperiod%3D7)，从首页实际链接进入；默认 90 天趋势范围，原文 README 控件 |
| `mobile.png` | 375×900 | 103,858 | [公开首页](https://repopulse.slak7.cn/)，7 天周期，滚动约 255px；周期、筛选及前三项同屏，非整页长图 |
| `social-preview.png` | 1280×640 | 194,178 | `social-preview.html` 浏览器渲染，产品画面直接引用并等比裁切 `home.png` |

四图均为 PNG（签名 `89504e470d0a1a0a`），Pillow 完整解码通过，RGB 不透明，无图片格式伪装。首页及海报保留实际提示：“已更新 3,518 / 3,520 个仓库，2 个暂不可用”。

## 静态图品牌合同

依据 `frontend/src/app/{icon.svg,globals.css,layout.tsx}` 与 `frontend/src/components/site-header.tsx`：浅底 `#f4f7fb`、白色表面 `#ffffff`、墨色 `#172033`、次级字色 `#5f6b7c`、主蓝 `#4f65f5`、深蓝 `#3548d4`、增长绿 `#11865b`、中性边线 `#e2e7ef`。图标沿用原有 Activity 心跳折线，不引入新标志。

分享图固定为 **1280×640**，PNG 小于 **1,000,000 字节**；文字与产品画面至少留 48px 外边距。字体沿用 Inter / Noto Sans SC；海报仅为文档放大显示定义 64px 标题、32px 品牌名、24px 定位、20px 周期与 URL、16px 来源说明，间距按 4px 网格。白色截图框采用 16px 圆角、中性边线与站点浅阴影；只使用真实 `home.png` 的等比裁切，不重绘应用、趋势或数字。所有元素静止，中文按语义分行。

## 复现与上传

可编辑源为 `social-preview.html`，与 `home.png` 放在同一目录。截图裁切窗口为 `(x=92.4, y=286, width=1240, height=688)`，保留周期、完整性提示、筛选与前四条结果。

复现封面时，可从仓库根目录运行 `python -m http.server 8766 --bind 127.0.0.1 --directory docs/assets`，用浏览器打开 <http://127.0.0.1:8766/social-preview.html>，将视口设为 1280×640，等待字体及图片加载后保存 PNG。源文件使用 Google Fonts 提供的 Inter 和 Noto Sans SC，导出需要网络；导出完成后停止临时服务。修改源文件后应重新导出并检查尺寸、体积和中文显示。

GitHub 分享封面须由已登录且具有管理权限的用户，在仓库 `Settings → General → Social preview → Edit → Upload an image` 上传 `social-preview.png`；提交图片文件本身不会自动配置该设置。
