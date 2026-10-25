#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod service;

use tauri::{Emitter, Manager, menu::{Menu, MenuItem}, tray::{TrayIconBuilder, TrayIconEvent, MouseButton, MouseButtonState}, webview::NewWindowResponse};
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

fn main() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
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
            let exit = MenuItem::with_id(app, "exit", "退出 TokenLens", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &scan, &cancel, &settings, &exit])?;
            TrayIconBuilder::new().icon(app.default_window_icon().expect("bundled icon").clone())
                .tooltip("TokenLens").menu(&menu).show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if matches!(event, TrayIconEvent::Click {button:MouseButton::Left,button_state:MouseButtonState::Up,..}) {
                        reveal(tray.app_handle(), None);
                    }
                })
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => reveal(app, None),
                    "settings" => reveal(app, Some("settings")),
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
            Ok(())
        }).build(tauri::generate_context!()).expect("TokenLens could not start");
    app.run(|app, event| {
        if matches!(event, tauri::RunEvent::Exit) {
            if let Some(service) = app.try_state::<Service>() { service.stop(); }
        }
    });
}
