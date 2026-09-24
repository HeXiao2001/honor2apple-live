# honor2apple-live

将荣耀动态照片转换为可一同导入 macOS「照片」的 HDR JPG + Live Photo MOV；也可原样复制 DNG，或从**同一张照片**的手机分享版 JPG 复制真实 GPS 到一份新 DNG。

目前针对 **HONOR BVL-AN16** 提供两套实测映射：旧版 3.846 倍 HDR 映射覆盖私有值 0–134；新版 1.5 倍 HDR 映射覆盖 0–255，使用两张独立裁剪样本交叉验证。`auto`（默认）会按私有增益图的最大编码值选择已测映射，不会外推未知值。没有增益图的动态照片可用 `--sdr-fallback` 保留动态效果，但静态主图不声明 HDR。源文件始终保留。视频、音频样本直通封装到 MOV；主图压缩数据保持不变。生成标准 HDR 增益图时会重新编码增益图，因此不能把转换后的 JPG 称为整个文件无损。

## 安装

需要 macOS、Python 3.9+、Xcode Command Line Tools（提供 `swift`）。

```sh
python3 -m pip install 'git+https://github.com/HeXiao2001/honor2apple-live.git'
```

首次安装需要联网下载 Python 依赖。若已下载本项目，也可在项目目录运行 `python3 -m pip install .`。

## 一行命令

```sh
honor2apple "/path/to/IMG_20260920_210339.jpg" -o "/path/to/converted"
```

输出同名 `.jpg` 和 `.mov`。在 Mac「照片」中**同时选择这两个文件导入**，它们会组成一张可播放的 HDR 实况照片。程序不自动操作你的照片图库。对没有完成 HDR 校准的动态照片，可加 `--sdr-fallback`；这会保留真实视频，不估算 HDR。

批量转换：

```sh
honor2apple /path/to/Downloads/IMG_*.jpg -o /path/to/converted --sdr-fallback

如需固定使用某个已测目标映射，可使用 `--hdr-profile legacy` 或 `--hdr-profile full_1p5`；通常保留默认的 `auto` 即可。
```

如果视频是单独文件，可为单张 JPG 指定 `--video /path/to/source.mp4`。

原样复制 DNG（输出与输入的 SHA-256 相同）：

```sh
honor2apple "/path/to/IMG_20260923_222625.dng" -o "/path/to/converted"
```

从同张照片的手机分享版 JPG 复制 GPS，生成新的 DNG：

```sh
honor2apple "/path/to/IMG_20260923_222625.dng" --gps-from-jpeg "/path/to/IMG_20260923_222625.jpg" -o "/path/to/converted"
```

批量 DNG 可使用 `--auto-gps`，程序要求每张 DNG 旁边都有同名 `.jpg`，且相机型号及拍摄时间匹配。补 GPS 的 DNG 整文件校验值会变化，但原始 DNG 不变，原有 RAW 图像数据字节不变。若同名输出已存在，程序默认拒绝覆盖；需要重做时加 `--overwrite`。

## 已验证范围

- 夜间 HDR 动态照片：Mac「照片」识别为一张实况照片；HDR 显示经用户在本机确认。
- DNG：直接复制后在「照片」显示为 RAW；导出未修改原件与输入逐字节相同。
- 从同一照片分享的 JPG 取 GPS 后，新的 DNG 在「照片」显示 RAW 和位置。DNG 格式本身支持 GPS；荣耀工作台导出的这份 DNG 没有携带它。

此项目不包含个人照片、GPS 样本或其他私人媒体。校准数据取自实测增益图，仅适用于已验证的设备和文件格式。

This product includes DNG technology under license by Adobe.

## 许可证

MIT，见 [LICENSE](LICENSE)。
