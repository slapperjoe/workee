fn main() {
    avd_dashboard_lib::run();
}

#[tauri::command]
fn test_hello() -> String { "world".into() }
