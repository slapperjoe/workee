use serde::Deserialize;
use std::sync::atomic::{AtomicU32, Ordering};

static VM_COUNTER: AtomicU32 = AtomicU32::new(0);
const BACKEND: &str = "http://127.0.0.1:8080";

#[derive(Deserialize)]
struct Cookie { name: String, value: String, domain: String, path: String }

#[derive(Deserialize)]
struct ConnectResponse {
    url: String,
    cookies: Vec<Cookie>,
    #[serde(default)] session_storage: std::collections::HashMap<String, String>,
}

#[tauri::command]
fn test_window(app: tauri::AppHandle) -> Result<String, String> {
    tauri::WebviewWindowBuilder::new(&app, "test-win",
        tauri::WebviewUrl::External("https://example.com".parse().unwrap()))
        .title("Test").inner_size(800.0, 600.0).build()
        .map_err(|e| format!("window: {}", e))?;
    Ok("window created".into())
}

#[tauri::command]
fn open_vm(vm_name: String, app: tauri::AppHandle) -> Result<(), String> {
    let client = reqwest::blocking::Client::new();
    let resp = client.post(format!("{}/api/connect", BACKEND))
        .json(&serde_json::json!({"vm_name": vm_name}))
        .send().map_err(|e| format!("backend: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("Backend {}", resp.status()));
    }
    let data: ConnectResponse = resp.json().map_err(|e| format!("parse: {}", e))?;

    let cookie_js: String = data.cookies.iter().map(|c|
        format!("document.cookie='{}={};domain={};path={};SameSite=Lax';", c.name, c.value, c.domain, c.path)
    ).collect::<Vec<_>>().join("");
    let ss_js: String = data.session_storage.iter().map(|(k, v)|
        format!("sessionStorage.setItem('{}','{}');",
            k.replace("'", "\\'").replace("\\", "\\\\"),
            v.replace("'", "\\'").replace("\\", "\\\\"))
    ).collect::<Vec<_>>().join("");
    let init = format!("(function(){{{}{}}})()", cookie_js, ss_js);
    let id = VM_COUNTER.fetch_add(1, Ordering::Relaxed);

    tauri::WebviewWindowBuilder::new(&app, format!("vm-{}", id),
        tauri::WebviewUrl::External(data.url.parse().unwrap()))
        .title("VM Session").inner_size(1280.0, 900.0).min_inner_size(800.0, 600.0)
        .resizable(true).initialization_script(&init).build()
        .map_err(|e| format!("window: {}", e))?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![test_window, open_vm])
        .run(tauri::generate_context!())
        .expect("error running tauri app");
}
