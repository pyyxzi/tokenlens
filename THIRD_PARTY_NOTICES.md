# 第三方声明

TokenLens 项目代码按项目 MIT 许可发布。第三方组件保留各自许可；发行包包含本声明及 `licenses` 目录。

- Tauri 2.12.3 与官方插件：MIT OR Apache-2.0；原生窗口、托盘、单实例及系统对话框。
- reqwest 0.12.28、Tokio、Serde、base64 及 Cargo.lock 中依赖：按各发行包声明；Rust 客户端仅连接固定的本机统计服务。
- Python 3.14.8：PSF 及随发行版附带许可，全文见 `licenses/Python.txt`。
- python-zstandard 0.25.0、libzstd 1.5.7：BSD-3-Clause，全文见对应许可。
- PyInstaller 6.22.3：GPL-2.0-or-later，嵌入运行器包含明确 Bootloader Exception；运行时 hook/附加模块适用 Apache-2.0。全文见 `licenses/PyInstaller.txt`。
- pyinstaller-hooks-contrib 2026.7：构建 hook 为 GPL-2.0-or-later，运行时 hook 为 Apache-2.0，全文见 `licenses/hooks.txt`。

应用界面和 TokenLens 图标沿用项目资产。数据源图标包含官方资产、Lobe Icons（MIT）和 Simple Icons（CC0-1.0）品牌图形，具体来源和许可见 `licenses/SourceIcons.txt`。无捆绑字体或远程字体库，本机字体名仅是候选字体。工具/厂商名称用于标识数据来源，不表示背书。构建工具版本锁定在依赖清单；构建环境与测试材料不随源码和应用发布。
