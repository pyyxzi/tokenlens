#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod service;

use tauri::{Emitter, Manager, menu::{Menu, MenuItem}, tray::{TrayIconBuilder, TrayIconEvent, MouseButton, MouseButtonState}, webview::NewWindowResponse};
use tauri_plugin_dialog::DialogExt;
use service::Service;

fn external_url(value: &str) -> Result<String, String> {
    if value.len() > 4096 || value.contains('\\') || value.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return Err("仅支持有效的 HTTP 或 HTTPS 链接".into());
    }
    let url = reqwest::Url::parse(value).map_err(|_| "链接地址无效")?;
    let authority = value.split_once("://").map(|(_, tail)| tail.split(['/', '?', '#']).next().unwrap_or(""));
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none()
        || !url.username().is_empty() || url.password().is_some()
        || authority.is_none_or(|host| host.is_empty() || host.contains('@'))
        || !value.get(..url.scheme().len() + 3).is_some_and(|prefix| prefix.eq_ignore_ascii_case(&format!("{}://", url.scheme()))) {
        return Err("仅支持无登录信息的 HTTP 或 HTTPS 链接".into());
    }
    Ok(url.to_string())
}

#[tauri::command]
async fn open_external(window: tauri::WebviewWindow, url: String) -> Result<(), String> {
    let origin = window.url().map_err(|_| "链接请求已拒绝")?;
    if window.label() != "main" || origin.port().is_some()
        || !matches!((origin.scheme(), origin.host_str()), ("tauri", Some("localhost")) | ("http", Some("tauri.localhost"))) {
        return Err("链接请求已拒绝".into());
    }
    let url = external_url(&url)?;
    tauri::async_runtime::spawn_blocking(move || {
        #[cfg(windows)]
        {
            use std::os::windows::ffi::OsStrExt;
            use windows_sys::Win32::UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL};
            let address: Vec<u16> = std::ffi::OsStr::new(&url).encode_wide().chain(Some(0)).collect();
            let operation: Vec<u16> = "open".encode_utf16().chain(Some(0)).collect();
            let result = unsafe { ShellExecuteW(std::ptr::null_mut(), operation.as_ptr(), address.as_ptr(),
                std::ptr::null(), std::ptr::null(), SW_SHOWNORMAL) };
            if result as isize <= 32 { return Err("无法打开系统浏览器，请检查默认浏览器设置".into()); }
            Ok(())
        }
        #[cfg(not(windows))]
        { let _ = url; Err("当前平台不支持打开系统浏览器".into()) }
    }).await.map_err(|_| "系统浏览器请求中断".to_owned())?
}

fn reveal(app: &tauri::AppHandle, view: Option<&str>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
        let _ = window.emit("native-state", serde_json::json!({"visible":true,"view":view}));
    }
}

static UPDATE_CHECKING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn fmt_tokens(mut n: f64) -> String {
    for unit in ["", "K", "M", "B"] {
        if n.abs() < 1000.0 || unit == "B" {
            let s = if n >= 100.0 { format!("{n:.0}") } else if n >= 10.0 { format!("{n:.1}") } else { format!("{n:.2}") };
            let s = s.trim_end_matches('0').trim_end_matches('.');
            return format!("{s}{unit}");
        }
        n /= 1000.0;
    }
    String::new()
}

// Tray companion: today's totals in the hover tooltip and one native
// notification per budget period once the warn threshold is crossed.
// Works with the window hidden; failures are skipped until the next tick.
async fn tray_monitor(app: tauri::AppHandle) {
    use tauri_plugin_notification::NotificationExt;
    let mut first = true;
    let mut notified: Option<(String, u8)> = None;
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(if first { 20 } else { 600 })).await;
        first = false;
        let service = app.state::<Service>();
        if let Ok(resp) = service.request("/api/summary?range=today", "GET", None).await {
            if let Ok(text) = resp.text().await {
                if let Some(summary) = serde_json::from_str::<serde_json::Value>(&text).ok()
                        .and_then(|v| v.get("summary").cloned()) {
                    let total = summary.get("total").and_then(|x| x.as_f64()).unwrap_or(0.0);
                    let cost = summary.get("cost").and_then(|x| x.as_f64()).unwrap_or(0.0);
                    if let Some(tray) = app.tray_by_id("main-tray") {
                        let _ = tray.set_tooltip(Some(format!(
                            "TokenLens · 今日 {} tokens · ${:.2}", fmt_tokens(total), cost)));
                    }
                }
            }
        }
        if let Ok(resp) = service.request("/api/budget", "GET", None).await {
            if let Ok(text) = resp.text().await {
                let budget = serde_json::from_str::<serde_json::Value>(&text).ok()
                    .and_then(|v| v.get("budget").cloned());
                if let Some(b) = budget {
                    let limit = b.get("limit").and_then(|x| x.as_f64()).unwrap_or(0.0);
                    let ratio = b.get("ratio").and_then(|x| x.as_f64()).unwrap_or(0.0);
                    let warn = b.get("warn_ratio").and_then(|x| x.as_f64()).unwrap_or(0.8);
                    let over = b.get("over").and_then(|x| x.as_bool()).unwrap_or(false);
                    let metric = b.get("metric").and_then(|x| x.as_str()).unwrap_or("tokens").to_string();
                    let used = b.get("used").and_then(|x| x.as_f64()).unwrap_or(0.0);
                    let start = b.get("start").and_then(|x| x.as_str()).unwrap_or("").to_string();
                    let period = if b.get("period").and_then(|x| x.as_str()) == Some("week") { "本周" } else { "本月" };
                    if limit > 0.0 && (over || ratio >= warn) {
                        let level: u8 = if over { 2 } else { 1 };
                        if notified.as_ref() != Some(&(start.clone(), level)) {
                            let amount = if metric == "cost" {
                                format!("${:.2} / ${:.2}", used, limit)
                            } else {
                                format!("{} / {} tokens", fmt_tokens(used), fmt_tokens(limit))
                            };
                            let (title, body) = if over {
                                ("TokenLens 预算已超支", format!("{period}已用 {amount}，超过预算上限。"))
                            } else {
                                ("TokenLens 预算提醒", format!("{period}已用 {amount}（{:.0}%），接近预算上限。", ratio * 100.0))
                            };
                            let _ = app.notification().builder().title(title).body(body).show();
                            notified = Some((start, level));
                        }
                    }
                }
            }
        }
    }
}

fn notice(app: &tauri::AppHandle, message: String) {
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _ = app.dialog().message(message).title("TokenLens 更新").blocking_show();
    });
}

async fn confirm(app: &tauri::AppHandle, message: String) -> bool {
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        use tauri_plugin_dialog::MessageDialogButtons;
        app.dialog().message(message).title("TokenLens 更新")
            .buttons(MessageDialogButtons::OkCancelCustom("立即更新".into(), "稍后".into()))
            .blocking_show()
    }).await.unwrap_or(false)
}

// Version line plus a short changelog excerpt from the signed manifest so
// users can decide without opening the browser.
fn update_summary(update: &tauri_plugin_updater::Update) -> String {
    let mut message = format!("发现新版本 {}（当前 {}），是否立即下载并安装？",
                              update.version, update.current_version);
    if let Some(notes) = update.body.as_deref().map(str::trim).filter(|n| !n.is_empty()) {
        let mut brief: String = notes.chars().take(160).collect();
        if notes.chars().count() > 160 { brief.push('…'); }
        message.push_str("\n\n");
        message.push_str(&brief.replace('\n', " "));
    }
    message.push_str("\n\n完整说明：github.com/pyyxzi/tokenlens/releases");
    message
}

// One update flow at a time; failures only surface when the user asked for them.
async fn run_update_check(app: tauri::AppHandle, manual: bool) {
    use std::sync::atomic::Ordering;
    use tauri_plugin_updater::UpdaterExt;
    if UPDATE_CHECKING.swap(true, Ordering::SeqCst) { return; }
    let outcome = async {
        let updater = app.updater().map_err(|e| format!("更新组件不可用：{e}"))?;
        let Some(update) = updater.check().await
            .map_err(|e| format!("检查更新失败：{e}"))?
        else {
            if manual { notice(&app, "当前已是最新版本。".into()); }
            return Ok(());
        };
        if !confirm(&app, update_summary(&update)).await {
            return Ok(());
        }
        // Windows: verifies the minisign signature, runs the installer and
        // exits this process; request_restart then relaunches the new build.
        update.download_and_install(|_, _| (), || ()).await
            .map_err(|e| format!("下载或安装更新失败：{e}"))?;
        app.request_restart();
        Ok(())
    }.await;
    UPDATE_CHECKING.store(false, Ordering::SeqCst);
    if let Err(message) = outcome {
        if manual { notice(&app, message); }
    }
}

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_single_instance::init(|app, _, _| reveal(app, None)))
        .invoke_handler(tauri::generate_handler![service::service_request, service::service_cancel, service::service_ack,
            service::service_download, service::save_document, open_external])
        .setup(|app| {
            let home = match std::env::var_os("KJUAN_DATA_DIR") {
                Some(path) => {
                    let path = std::path::PathBuf::from(path);
                    if !path.is_absolute() { return Err("KJUAN_DATA_DIR must be absolute".into()); }
                    path
                }
                None => app.path().app_data_dir()?,
            };
            std::fs::create_dir_all(&home)?;
            let state = Service::start(home.clone()).map_err(std::io::Error::other)?;
            app.manage(state);
            let window = tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
                .title("TokenLens").inner_size(1360.0, 900.0).min_inner_size(780.0, 600.0)
                .data_directory(home.join("webview"))
                .on_navigation(|url| url.port().is_none() && matches!((url.scheme(), url.host_str()), ("tauri",Some("localhost")) | ("http",Some("tauri.localhost"))))
                .on_new_window(|_, _| NewWindowResponse::Deny)
                .on_download(|_, _| false)
                .build()?;
            let handle = app.handle().clone();
            window.on_window_event(move |event| match event {
                tauri::WindowEvent::CloseRequested {api, ..} => {
                    api.prevent_close();
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.hide();
                        let _ = window.emit("native-state", serde_json::json!({"visible":false}));
                    }
                }
                tauri::WindowEvent::Focused(true) => {
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.emit("native-state", serde_json::json!({"visible":true}));
                    }
                }
                _ => {}
            });
            let show = MenuItem::with_id(app, "show", "打开工作台", true, None::<&str>)?;
            let scan = MenuItem::with_id(app, "scan", "增量扫描", true, None::<&str>)?;
            let cancel = MenuItem::with_id(app, "cancel", "取消扫描", true, None::<&str>)?;
            let settings = MenuItem::with_id(app, "settings", "设置", true, None::<&str>)?;
            let update = MenuItem::with_id(app, "update", "检查更新…", true, None::<&str>)?;
            let exit = MenuItem::with_id(app, "exit", "退出 TokenLens", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &scan, &cancel, &settings, &update, &exit])?;
            TrayIconBuilder::with_id("main-tray")
                .icon(app.default_window_icon().expect("bundled icon").clone())
                .tooltip("TokenLens").menu(&menu).show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if matches!(event, TrayIconEvent::Click {button:MouseButton::Left,button_state:MouseButtonState::Up,..}) {
                        reveal(tray.app_handle(), None);
                    }
                })
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => reveal(app, None),
                    "settings" => reveal(app, Some("settings")),
                    "update" => {
                        let app = app.clone();
                        tauri::async_runtime::spawn(async move { run_update_check(app, true).await; });
                    }
                    "exit" => app.exit(0),
                    "scan" | "cancel" => {
                        let app = app.clone();
                        let path = if event.id.as_ref() == "scan" {"/api/scan"} else {"/api/scan/cancel"};
                        tauri::async_runtime::spawn(async move {
                            let state = app.state::<Service>();
                            let _ = state.request(path, "POST", Some("{}".to_owned())).await;
                        });
                    }
                    _ => {}
                }).build(app)?;
            Service::supervise(app.handle().clone());
            // Tray tooltip + budget notifications, with the window hidden too.
            let monitor = app.handle().clone();
            tauri::async_runtime::spawn(async move { tray_monitor(monitor).await; });
            // Silent startup check; a found update still asks before installing.
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                tokio::time::sleep(std::time::Duration::from_secs(10)).await;
                run_update_check(handle, false).await;
            });
            Ok(())
        }).build(tauri::generate_context!()).expect("TokenLens could not start");
    app.run(|app, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            if let Some(service) = app.try_state::<Service>() { service.stop(); }
        }
    });
}
