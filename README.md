# HBR 壁纸图库

《绯染天空》（Heaven Burns Red）的本地资源浏览与导出工具。读取游戏安装目录、Unity 缓存及游戏更新目录中的 AssetBundle，在浏览器中分类展示剧情 CG、角色卡面、场景背景和演出素材。

| 角色卡面 | 角色卡面 |
| --- | --- |
| ![角色卡面示例一](docs/preview/card_1972.png) | ![角色卡面示例二](docs/preview/card_3248.png) |

## 安装与运行

需要 Windows、Python 3.11 或更高版本，以及已安装的 Steam 版《绯染天空》。在项目目录运行以下命令，依赖安装在项目自己的虚拟环境中。

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

双击 `运行壁纸图库.bat`。启动器会优先使用 `.venv`，自动打开浏览器。图库窗口显示本机访问地址，关闭窗口即可退出服务。首次启动需要扫描游戏目录并建立索引。

程序会自动查找 Steam 安装库。若未找到游戏，在项目目录将 `hbr_config.example.json` 复制为 `hbr_config.json` 并填写 `game_dir`。可从 Steam 的「管理」→「浏览本地文件」找到游戏目录。以下路径使用 Windows 支持的正斜杠。

```json
{
  "game_dir": "D:/SteamLibrary/steamapps/common/HeavenBurnsRed/HeavenBurnsRed_Data/StreamingAssets/aa/StandaloneWindows64"
}
```

`StandaloneWindows64` 的上一级目录应包含 `catalog.bin`。如果游戏使用了自定义缓存位置，可在同一配置文件中通过 `unity_cache_dir` 和 `cache_dir` 分别指定 Unity Bundle 缓存和 Addressables 目录缓存。

## 浏览与导出

- 在左侧展开剧情篇章和章节，或切换到其他资源分类。使用搜索、尺寸和资源状态筛选缩小范围。
- 点击缩略图查看大图。滚轮缩放，拖动平移，按左右键切换，按 Esc 关闭。原图暂不可读时会优先展示可用的游戏预览。
- 勾选在切换分类和筛选后保留。支持全选当前结果、批量加入收藏夹和批量导出。收藏关系按游戏资源路径保存。
- 在「设置」中选择导出目录，默认使用项目下的 `HBR_Wallpapers/`。原图按分类导出为原始分辨率 PNG。需要下载 Bundle 时，页面先显示数量和可估算的体积。只有游戏预览时，使用「保存预览」。

## Bundle 与缩略图

「更新 Bundle 状态」重新扫描游戏安装目录、Unity 缓存和工具缓存，核对游戏提供的在线目录，下载图库缺少的 Bundle，并补齐本地可生成的缩略图。在线目录不可访问时，仍会更新本地状态并复用上次可用的索引。若 `remote_q/` 中的 Bundle 与游戏安装目录中的同名文件逐字节一致，图库会切换到安装目录并移除工具副本。

「缩略图状态」显示已缓存、待补全、暂不能补全和待清理的数量。「一键补全缩略图」在 `hbr_thumbs/256/` 下清理完整图库索引已不再使用的缓存，并为本机可读的原图生成缺少的缩略图。任务显示进度、清理数量与释放空间，已有的有效缓存直接复用。

## 项目结构

| 路径 | 用途 |
| --- | --- |
| `hbr_gallery.pyw` | Addressables 索引解析、Unity 贴图解码、后台任务和本地 HTTP 接口 |
| `web/` | 纯 HTML、CSS 和 JavaScript 页面，直接由本地服务加载 |
| `运行壁纸图库.bat` | Windows 启动器 |
| `requirements.txt` | Python 依赖 |
| `hbr_config.example.json` | 路径配置示例 |

HTTP 服务仅监听 `127.0.0.1`，端口在启动时分配。游戏安装目录与 Unity 缓存以只读方式访问。配置、收藏、勾选、索引、缓存和导出文件保存在本机，并由 Git 忽略。

## 素材归属

游戏美术及预览素材归各自权利方所有。
