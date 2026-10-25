use std::{collections::{HashMap, HashSet, VecDeque}, io::{BufRead, BufReader, Read, Write}, path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio}, sync::{Arc, Mutex, atomic::{AtomicBool, AtomicU64, Ordering}}, time::{Duration, Instant}};
use base64::{Engine, engine::general_purpose::STANDARD};
use futures_util::StreamExt;
use serde::Serialize;
use tauri::{ipc::Channel, Emitter, Manager};
use tauri_plugin_dialog::DialogExt;
use tokio::sync::Semaphore;
use tokio_util::sync::CancellationToken;

const MAX_RESPONSE: usize = 128 * 1024 * 1024;
const MAX_EXPORT: usize = 1024 * 1024 * 1024;
const MAX_BODY: usize = 44_743_340;
const GET_PATHS: &[&str] = &["/api/ai-settings","/api/ai-thinking","/api/assistant/state","/api/assistant/snapshot",
 "/api/assistant/report","/api/ccswitch/connections","/api/backup/export","/api/bootstrap","/api/dashboard",
 "/api/summary","/api/sources","/api/rankings","/api/records","/api/session","/api/prompt","/api/tasks",
 "/api/finance","/api/manage","/api/diagnostics","/api/scan/status","/api/scan/stream","/api/export",
 "/api/export-stream","/api/ping","/api/prices/list","/api/desktop/status","/api/ledger","/api/budget"];
const POST_PATHS: &[&str] = &["/api/desktop/show","/api/desktop/hide","/api/ai-settings/check","/api/ai-settings/save",
 "/api/ai-settings/models","/api/assistant/models","/api/assistant/prices/confirm","/api/assistant/report/delete",
 "/api/assistant/prices/restore","/api/assistant/stream","/api/ccswitch/models","/api/backup/export",
 "/api/backup/preview","/api/backup/restore","/api/manage","/api/finance","/api/finance/action","/api/tasks/cancel",
 "/api/scan","/api/scan/cancel","/api/config","/api/prices/set","/api/prices/remove","/api/prompt/check",
 "/api/prompt/save","/api/prompt/select","/api/prompt/delete","/api/prompt/optimize","/api/ai-test",
 "/api/ai-availability","/api/prompt/optimize-stream","/api/ai-test-stream","/api/prompt/clear","/api/reset",
 "/api/pick-folder","/api/shutdown","/api/sources/import/preview","/api/sources/import/commit",
 "/api/sources/import/discard","/api/sources/reveal"];

pub struct Service {
    home: PathBuf, runtime: Mutex<Option<Runtime>>, recovery: Arc<tokio::sync::Mutex<()>>,
    read_client: reqwest::Client, write_client: reqwest::Client,
    stopping: AtomicBool, generation: AtomicU64, announced_generation: AtomicU64, failed_at: Mutex<Option<Instant>>,
    active: Mutex<HashMap<String, Arc<Control>>>, slots: Arc<Semaphore>, dialogs: Arc<Semaphore>,
    cancelled: Mutex<HashSet<String>>,
}
#[derive(Clone)]
struct Endpoint { url: String, secret: String }
struct Runtime {
    #[cfg(windows)]
    _job: ProcessJob,
    child: Child, stdin: Option<ChildStdin>, endpoint: Endpoint,
}
// Drop the abandoned child before releasing its startup lock. This also
// applies when the request that awaited spawn_blocking has been cancelled.
struct StartupOutcome {
    result: Result<Runtime,String>,
    _guard: tokio::sync::OwnedMutexGuard<()>,
}
impl Runtime {
    fn stop(&mut self) {
        if let Some(mut pipe)=self.stdin.take(){let _=writeln!(pipe,"{{\"shutdown\":true}}");}
        for _ in 0..30 {
            if matches!(self.child.try_wait(),Ok(Some(_))){return;}
            std::thread::sleep(Duration::from_millis(100));
        }
        let _=self.child.kill();let _=self.child.wait();
    }
}
impl Drop for Runtime { fn drop(&mut self){self.stop();} }
#[cfg(windows)]
struct ProcessJob(isize);
#[cfg(windows)]
impl ProcessJob {
    fn attach(child: &Child) -> Result<Self,String> {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::{Foundation::CloseHandle, System::JobObjects::*};
        unsafe {
            let job = CreateJobObjectW(std::ptr::null(), std::ptr::null());
            if job.is_null() { return Err("cannot supervise analytics service".into()); }
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(), std::mem::size_of_val(&limits) as u32) == 0
                || AssignProcessToJobObject(job, child.as_raw_handle()) == 0 {
                CloseHandle(job); return Err("cannot supervise analytics service".into());
            }
            Ok(Self(job as isize))
        }
    }
}
#[cfg(windows)]
impl Drop for ProcessJob {
    fn drop(&mut self) { unsafe { windows_sys::Win32::Foundation::CloseHandle(self.0 as _); } }
}
struct Control { cancel: CancellationToken, window: Arc<Semaphore>, pending: Mutex<VecDeque<u64>> }
#[derive(Serialize)]
#[serde(tag="kind", rename_all="lowercase")]
pub enum StreamMessage { Chunk {data:String,seq:u64}, End, Error {message:String} }
#[derive(Serialize)]
pub struct ResponseInfo { status: u16, headers: HashMap<String,String> }

fn permit(window: &tauri::WebviewWindow) -> Result<(),String> {
    if window.label() != "main" {return Err("request rejected".into());}
    let url = window.url().map_err(|_| "request rejected")?;
    if url.port().is_some() || !matches!((url.scheme(),url.host_str()),("tauri",Some("localhost")) | ("http",Some("tauri.localhost"))) {
        return Err("request rejected".into());
    }
    Ok(())
}
fn validate(path: &str, method: &str, body: &Option<String>) -> Result<(),String> {
    if path.len()>8192 || path.contains(['\\','#']) || path.chars().any(char::is_control) {
        return Err("invalid request path".into());
    }
    let endpoint=path.split('?').next().unwrap_or("");
    let allowed=match method {"GET"=>GET_PATHS,"POST"=>POST_PATHS,_=>return Err("invalid request method".into())};
    if !allowed.contains(&endpoint) {return Err("unknown application endpoint".into());}
    if method=="GET" && body.is_some() {return Err("invalid GET body".into());}
    if let Some(text)=body {
        if text.len()>MAX_BODY {return Err("request body is too large".into());}
        let parsed:serde_json::Value=serde_json::from_str(text).map_err(|_|"invalid JSON body")?;
        if !parsed.is_object() {return Err("request body must be an object".into());}
    }
    Ok(())
}
fn request_id(id:&str)->Result<(),String> {
    if id.len()!=36 || !id.chars().all(|c|c.is_ascii_hexdigit()||c=='-') {return Err("invalid request id".into());}
    Ok(())
}
impl Service {
    pub fn start(home:PathBuf)->Result<Self,String> {
        // Keep startup off the UI thread: first requests and supervision share
        // one recovery lock, so a slow database initialization cannot spawn
        // duplicate sidecars or prevent the window from opening.
        let client = |pooled:bool| reqwest::Client::builder().no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(3)).read_timeout(Duration::from_secs(180))
            .pool_idle_timeout(Duration::from_secs(4)).pool_max_idle_per_host(if pooled {2}else{0})
            .build().map_err(|_|"无法初始化本地服务连接".to_owned());
        Ok(Self{home,runtime:Mutex::new(None),recovery:Arc::new(tokio::sync::Mutex::new(())),
            read_client:client(true)?,write_client:client(false)?,stopping:AtomicBool::new(false),
            generation:AtomicU64::new(0),announced_generation:AtomicU64::new(0),failed_at:Mutex::new(None),
            active:Mutex::new(HashMap::new()),slots:Arc::new(Semaphore::new(16)),
            dialogs:Arc::new(Semaphore::new(1)),cancelled:Mutex::new(HashSet::new())})
    }
    fn launch(home:PathBuf)->Result<Runtime,String> {
        let mut random=[0u8;32];getrandom::fill(&mut random).map_err(|_|"无法初始化私有本地服务")?;
        let secret=random.iter().map(|v|format!("{v:02x}")).collect::<String>();
        let exe=std::env::current_exe().map_err(|_|"无法定位本地服务")?;
        let dir=exe.parent().ok_or("无法定位本地服务")?;
        let mut command=Command::new(dir.join("tokenlens-service.exe"));
        command.current_dir(dir).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null());
        #[cfg(windows)] {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let child=command.spawn().map_err(|_|"本地服务未能启动，请检查程序文件是否完整")?;
        #[cfg(windows)]
        let job = match ProcessJob::attach(&child) {
            Ok(job) => job,
            Err(error) => {let mut child=child;let _=child.kill();let _=child.wait();return Err(error);}
        };
        // Construct the owner before any fallible handshake steps. Its Drop
        // kills/reaps failed startup children and closes their process job.
        let mut runtime=Runtime{
            #[cfg(windows)] _job:job,
            child,stdin:None,endpoint:Endpoint{url:String::new(),secret},
        };
        let input=runtime.child.stdin.take().ok_or("本地服务启动管道不可用")?;
        runtime.stdin=Some(input);
        let output=runtime.child.stdout.take().ok_or("本地服务启动管道不可用")?;
        writeln!(runtime.stdin.as_mut().ok_or("本地服务启动管道不可用")?,"{}",
            serde_json::json!({"token":runtime.endpoint.secret,"home":home})).map_err(|_|"本地服务启动握手失败")?;
        let (tx,rx)=std::sync::mpsc::channel();
        std::thread::spawn(move|| {
            let mut reader=BufReader::new(output);let mut line=String::new();
            let result=reader.by_ref().take(4096).read_line(&mut line).map(|_|line);
            let _=tx.send(result);
        });
        let ready=rx.recv_timeout(Duration::from_secs(60)).ok().and_then(Result::ok)
            .and_then(|value|serde_json::from_str::<serde_json::Value>(&value).ok());
        let port=ready.as_ref().filter(|value|value["ready"]==true).and_then(|value|value["port"].as_u64())
            .filter(|port|*port>0 && *port<=65535);
        let Some(port)=port else{return Err("本地服务启动超时，正在准备重试".into());};
        runtime.endpoint.url=format!("http://127.0.0.1:{port}");
        Ok(runtime)
    }
    fn running_endpoint(&self)->Result<Option<Endpoint>,String> {
        let mut guard=self.runtime.lock().map_err(|_|"本地服务状态不可用")?;
        match guard.as_mut() {
            Some(runtime) => match runtime.child.try_wait() {
                Ok(None)=>Ok(Some(runtime.endpoint.clone())),
                Ok(Some(_))=>Ok(None),
                Err(_)=>Err("无法读取本地服务状态".into()),
            },
            None=>Ok(None),
        }
    }
    fn emit_state(&self,app:&tauri::AppHandle,state:&str,message:&str) {
        let _=app.emit("service-state",serde_json::json!({"state":state,"message":message,
            "generation":self.generation.load(Ordering::Acquire)}));
    }
    fn announce_ready(&self,app:&tauri::AppHandle) {
        let generation=self.generation.load(Ordering::Acquire);
        if generation>0 && self.announced_generation.swap(generation,Ordering::AcqRel)!=generation {
            self.emit_state(app,"ready",if generation>1{"本地服务已恢复，未完成的 AI 任务可重新运行"}else{"本地服务已连接"});
        }
    }
    async fn ensure_ready(&self,app:Option<&tauri::AppHandle>)->Result<Endpoint,String> {
        if self.stopping.load(Ordering::Acquire){return Err("程序正在退出".into());}
        if let Some(endpoint)=self.running_endpoint()?{if let Some(app)=app{self.announce_ready(app);}return Ok(endpoint);}
        let recovery=self.recovery.clone().lock_owned().await;
        if self.stopping.load(Ordering::Acquire){return Err("程序正在退出".into());}
        if let Some(endpoint)=self.running_endpoint()?{if let Some(app)=app{self.announce_ready(app);}return Ok(endpoint);}
        if self.failed_at.lock().map_err(|_|"本地服务状态不可用")?
            .is_some_and(|at|at.elapsed()<Duration::from_secs(2)) {
            return Err("本地服务正在恢复，请稍后重试".into());
        }
        if let Some(app)=app{self.emit_state(app,"recovering","正在连接本地服务…");}
        let old=self.runtime.lock().map_err(|_|"本地服务状态不可用")?.take();
        let home=self.home.clone();let generation=self.generation.load(Ordering::Acquire)+1;
        // The blocking startup owns the recovery guard too. Cancelling the
        // initiating request cannot unlock recovery while its child is still
        // starting; any abandoned child is dropped before the guard releases.
        let StartupOutcome{result:started,_guard:_recovery}=tauri::async_runtime::spawn_blocking(move||{
            drop(old);StartupOutcome{result:Self::launch(home),_guard:recovery}
        }).await
            .map_err(|_|"本地服务恢复失败")?;
        match started {
            Ok(runtime)=>{
                // Exit may have been requested while the sidecar was starting.
                let endpoint=runtime.endpoint.clone();
                {
                    let mut guard=self.runtime.lock().map_err(|_|"本地服务状态不可用")?;
                    if self.stopping.load(Ordering::Acquire){drop(runtime);return Err("程序正在退出".into());}
                    self.generation.store(generation,Ordering::Release);*guard=Some(runtime);
                }
                *self.failed_at.lock().map_err(|_|"本地服务状态不可用")?=None;
                if let Some(app)=app{self.announce_ready(app);}
                Ok(endpoint)
            }
            Err(error)=>{
                *self.failed_at.lock().map_err(|_|"本地服务状态不可用")?=Some(Instant::now());
                if let Some(app)=app{self.emit_state(app,"unavailable",&error);}
                Err(error)
            }
        }
    }
    pub fn supervise(app:tauri::AppHandle) {
        tauri::async_runtime::spawn(async move {
            loop {
                let state=app.state::<Service>();
                if state.stopping.load(Ordering::Acquire){break;}
                // Process liveness is cheap and independent of large queries.
                // Never restart a live sidecar solely because a slow query or
                // provider request hit a deadline: its mutation may still run.
                let _=state.ensure_ready(Some(&app)).await;
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        });
    }
    async fn send(&self,endpoint:&Endpoint,path:&str,method:&str,body:&Option<String>)->Result<reqwest::Response,reqwest::Error> {
        let client=if method=="GET"{&self.read_client}else{&self.write_client};
        let mut request=client.request(if method=="GET"{reqwest::Method::GET}else{reqwest::Method::POST},format!("{}{path}",endpoint.url))
            .header("X-Kjuan-Secret",&endpoint.secret).header("X-TokenLens","1");
        if method=="POST" {
            // Mutations always use a fresh connection. Do not replay a write
            // whose response was lost: the action might already have committed.
            request=request.header("Connection","close").header("Content-Type","application/json")
                .body(body.clone().unwrap_or_else(||"{}".into()));
        }
        request.send().await
    }
    pub async fn request(&self,path:&str,method:&str,body:Option<String>)->Result<reqwest::Response,String> {
        validate(path,method,&body)?;
        let endpoint=self.ensure_ready(None).await?;
        let result=self.send(&endpoint,path,method,&body).await;
        match result {
            Ok(response) if method!="GET" || response.status()!=reqwest::StatusCode::SERVICE_UNAVAILABLE=>Ok(response),
            Ok(response)=>{
                drop(response);tokio::time::sleep(Duration::from_millis(150)).await;
                let endpoint=self.ensure_ready(None).await?;
                self.send(&endpoint,path,method,&body).await.map_err(|_|"本地服务繁忙，请稍后重试".into())
            }
            Err(error) if method=="GET" && (error.is_connect() || error.is_request() || error.is_timeout())=>{
                // Exactly one safe retry, before a response body reaches the
                // frontend. Re-opening an SSE body midstream would duplicate
                // events, so streams themselves are never replayed.
                tokio::time::sleep(Duration::from_millis(150)).await;
                let endpoint=self.ensure_ready(None).await?;
                self.send(&endpoint,path,method,&body).await.map_err(|_|"本地服务暂时无法响应，请稍后重试".into())
            }
            Err(error)=>Err(if error.is_connect(){"本地服务连接中断，请稍后重试"}
                else if method=="POST"{"请求响应中断；操作可能已完成，请先查看当前状态再重试"}
                else{"本地服务响应中断，请稍后重试"}.into()),
        }
    }
    pub fn stop(&self) {
        self.stopping.store(true,Ordering::Release);self.slots.close();self.dialogs.close();
        if let Ok(active)=self.active.lock(){for control in active.values(){control.cancel.cancel();}}
        if let Ok(mut guard)=self.runtime.lock(){if let Some(mut runtime)=guard.take(){runtime.stop();}}
    }
    fn finish_request(&self,id:&str) {
        if let Ok(mut active)=self.active.lock(){active.remove(id);}
    }
}
impl Drop for Service {fn drop(&mut self){self.stop();}}

fn synthetic(value:serde_json::Value,channel:&Channel<StreamMessage>)->Result<ResponseInfo,String>{
    let bytes=serde_json::to_vec(&value).map_err(|_|"response failed")?;
    channel.send(StreamMessage::Chunk{data:STANDARD.encode(bytes),seq:0}).map_err(|_|"response failed")?;
    channel.send(StreamMessage::End).map_err(|_|"response failed")?;
    Ok(ResponseInfo{status:200,headers:HashMap::from([("content-type".into(),"application/json".into())])})
}
#[tauri::command]
pub async fn service_request(app:tauri::AppHandle,window:tauri::WebviewWindow,state:tauri::State<'_,Service>,id:String,
    path:String,method:String,body:Option<String>,on_chunk:Channel<StreamMessage>)->Result<ResponseInfo,String>{
    permit(&window)?;validate(&path,&method,&body)?;request_id(&id)?;
    let endpoint=path.split('?').next().unwrap_or("");
    match endpoint {
        "/api/desktop/status"=>return synthetic(serde_json::json!({"supported":true,"running":true,"visible":window.is_visible().unwrap_or(true)}),&on_chunk),
        "/api/desktop/show" | "/api/desktop/hide"=> {
            let visible=endpoint.ends_with("show");
            if visible {window.show().map_err(|_|"could not show window")?;let _=window.set_focus();}
            else {window.hide().map_err(|_|"could not hide window")?;}
            let _=window.emit("native-state",serde_json::json!({"visible":visible}));
            return synthetic(serde_json::json!({"ok":true,"visible":visible}),&on_chunk);
        }
        "/api/shutdown"=> {
            let value=synthetic(serde_json::json!({"ok":true}),&on_chunk);
            let app=app.clone();tauri::async_runtime::spawn(async move{tokio::time::sleep(Duration::from_millis(150)).await;app.exit(0);});
            return value;
        }
        "/api/pick-folder"=> {
            let _permit=state.dialogs.clone().try_acquire_owned().map_err(|_|"a dialog is already open")?;
            let (tx,rx)=tokio::sync::oneshot::channel();
            app.dialog().file().set_title("选择日志目录").pick_folder(move|path|{let _=tx.send(path);});
            let path=rx.await.map_err(|_|"folder dialog closed")?.and_then(|p|p.into_path().ok()).map(|p|p.to_string_lossy().into_owned());
            return synthetic(serde_json::json!({"ok":path.is_some(),"path":path}),&on_chunk);
        }
        _=>{}
    }
    // A page transition can briefly overlap cancelled reads and three live AI
    // streams. Queue that burst with a bound instead of failing immediately.
    let slot=tokio::time::timeout(Duration::from_secs(15),state.slots.clone().acquire_owned()).await
        .map_err(|_|"本地请求较多，请稍后重试")?.map_err(|_|"程序正在退出")?;
    let control=Arc::new(Control{cancel:CancellationToken::new(),window:Arc::new(Semaphore::new(8)),pending:Mutex::new(VecDeque::new())});
    {
        let mut active=state.active.lock().map_err(|_|"local transport unavailable")?;
        if active.contains_key(&id){return Err("duplicate request id".into());}active.insert(id.clone(),control.clone());
    }
    let already_cancelled = match state.cancelled.lock() {
        Ok(mut cancelled)=>cancelled.remove(&id),
        Err(_)=>{state.finish_request(&id);return Err("本地连接状态不可用".into());}
    };
    if already_cancelled {state.finish_request(&id);return Err("request cancelled".into());}
    let response=tokio::select!{
        _=control.cancel.cancelled()=>Err("request cancelled".into()),
        result=state.request(&path,&method,body)=>result,
    };
    let response=match response{Ok(response)=>response,Err(error)=>{state.finish_request(&id);return Err(error);}};
    if response.content_length().is_some_and(|length|length>MAX_RESPONSE as u64){state.finish_request(&id);return Err("本地响应过大，请缩小查询范围".into());}
    let mut headers=HashMap::new();
    for name in ["content-type","content-disposition","content-length"]{
        if let Some(value)=response.headers().get(name).and_then(|value|value.to_str().ok()){headers.insert(name.to_owned(),value.to_owned());}
    }
    let info=ResponseInfo{status:response.status().as_u16(),headers};
    let handle=app.clone();
    tauri::async_runtime::spawn(async move{
        let _slot=slot;let mut stream=response.bytes_stream();let mut total=0;let mut seq=0;
        let deadline=tokio::time::Instant::now()+Duration::from_secs(1800);
        let result:Result<(),String>=async{
            loop{
                let next=tokio::select!{_=control.cancel.cancelled()=>return Err("request cancelled".into()),
                    _=tokio::time::sleep_until(deadline)=>return Err("本地响应等待超时，请稍后重试".into()),value=stream.next()=>value};
                let Some(chunk)=next else{break};let chunk=chunk.map_err(|_|"本地响应中断，请稍后重试")?;
                total+=chunk.len();if total>MAX_RESPONSE{return Err("本地响应过大，请缩小查询范围".into());}
                for part in chunk.chunks(64*1024){
                    let permit=tokio::select!{_=control.cancel.cancelled()=>return Err("request cancelled".into()),
                        result=tokio::time::timeout(Duration::from_secs(30),control.window.clone().acquire_owned())=>
                            result.map_err(|_|"response consumer timed out")?.map_err(|_|"response closed")?};
                    permit.forget();seq+=1;control.pending.lock().map_err(|_|"本地连接状态不可用")?.push_back(seq);
                    on_chunk.send(StreamMessage::Chunk{data:STANDARD.encode(part),seq}).map_err(|_|"response consumer closed")?;
                }
            }Ok(())
        }.await;
        let _=match result{Ok(())=>on_chunk.send(StreamMessage::End),Err(message)=>on_chunk.send(StreamMessage::Error{message})};
        handle.state::<Service>().finish_request(&id);
    });
    Ok(info)
}
#[tauri::command]
pub fn service_cancel(window:tauri::WebviewWindow,state:tauri::State<'_,Service>,id:String)->Result<(),String>{
    permit(&window)?;request_id(&id)?;
    let active = state.active.lock().map_err(|_|"local transport unavailable")?;
    if let Some(control)=active.get(&id){control.cancel.cancel();}
    else {
        let mut cancelled=state.cancelled.lock().map_err(|_|"local transport unavailable")?;
        if cancelled.len() >= 256 {if let Some(first)=cancelled.iter().next().cloned(){cancelled.remove(&first);}}
        cancelled.insert(id);
    }
    Ok(())
}
#[tauri::command]
pub fn service_ack(window:tauri::WebviewWindow,state:tauri::State<'_,Service>,id:String,seq:u64)->Result<(),String>{
    permit(&window)?;request_id(&id)?;
    if let Some(control)=state.active.lock().map_err(|_|"local transport unavailable")?.get(&id){
        let mut pending=control.pending.lock().map_err(|_|"local transport unavailable")?;
        if pending.front()==Some(&seq){pending.pop_front();control.window.add_permits(1);}
        else{return Err("invalid response acknowledgement".into());}
    }Ok(())
}

async fn destination(app:&tauri::AppHandle,state:&Service,name:&str)->Result<Option<PathBuf>,String>{
    let _permit=state.dialogs.clone().try_acquire_owned().map_err(|_|"a dialog is already open")?;
    let (tx,rx)=tokio::sync::oneshot::channel();
    app.dialog().file().set_title("保存导出文件").set_file_name(name).save_file(move|file|{let _=tx.send(file);});
    Ok(rx.await.map_err(|_|"save dialog closed")?.and_then(|file|file.into_path().ok()))
}
async fn write_download(path:PathBuf,mut stream:impl futures_util::Stream<Item=Result<bytes::Bytes,reqwest::Error>>+Unpin)->Result<String,String>{
    use tokio::io::AsyncWriteExt;
    let parent=path.parent().ok_or("invalid save destination")?;
    let mut random=[0u8;16];getrandom::fill(&mut random).map_err(|_|"could not create export")?;
    let name=random.iter().map(|v|format!("{v:02x}")).collect::<String>();let temp=parent.join(format!(".tokenlens-{name}.tmp"));
    let result=async{
        let mut file=tokio::fs::OpenOptions::new().write(true).create_new(true).open(&temp).await.map_err(|_|"could not open export destination")?;
        let mut total=0;
        while let Some(chunk)=tokio::time::timeout(Duration::from_secs(180),stream.next()).await.map_err(|_|"export timed out")?{
            let chunk=chunk.map_err(|_|"export interrupted")?;total+=chunk.len();if total>MAX_EXPORT{return Err("export exceeds 1 GiB; narrow the selected range".to_owned());}
            file.write_all(&chunk).await.map_err(|_|"could not write export")?;
        }
        file.sync_all().await.map_err(|_|"could not finish export")?;drop(file);
        tokio::fs::rename(&temp,&path).await.map_err(|_|"could not replace destination")?;
        Ok(path.to_string_lossy().into_owned())
    }.await;
    if result.is_err(){let _=tokio::fs::remove_file(&temp).await;}result
}
#[tauri::command]
pub async fn service_download(app:tauri::AppHandle,window:tauri::WebviewWindow,state:tauri::State<'_,Service>,
    path:String,method:String,body:Option<String>)->Result<Option<String>,String>{
    permit(&window)?;validate(&path,&method,&body)?;
    let endpoint=path.split('?').next().unwrap_or("");
    if !["/api/export-stream","/api/export","/api/diagnostics","/api/backup/export"].contains(&endpoint){return Err("invalid export endpoint".into());}
    let _slot=tokio::time::timeout(Duration::from_secs(15),state.slots.clone().acquire_owned()).await
        .map_err(|_|"本地请求较多，请稍后重试")?.map_err(|_|"程序正在退出")?;
    let extension=if endpoint=="/api/backup/export"{"zip"}else if endpoint=="/api/diagnostics"{"json"}else if path.contains("fmt=csv"){"csv"}else if path.contains("fmt=md"){"md"}else{"json"};
    let Some(destination)=destination(&app,&state,&format!("TokenLens-export.{extension}")).await? else{return Ok(None)};
    let response=state.request(&path,&method,body).await?;
    if !response.status().is_success(){return Err("export failed; retry after checking the selected range".into());}
    write_download(destination,response.bytes_stream()).await.map(Some)
}
#[tauri::command]
pub async fn save_document(app:tauri::AppHandle,window:tauri::WebviewWindow,state:tauri::State<'_,Service>,name:String,contents:String)->Result<Option<String>,String>{
    permit(&window)?;
    if contents.len()>10*1024*1024 || name.len()>150 || name.contains(['\\','/',':']) || name.chars().any(char::is_control)
        || !["md","csv","json","txt"].iter().any(|extension|name.ends_with(&format!(".{extension}"))){return Err("invalid document".into());}
    let Some(path)=destination(&app,&state,&name).await? else{return Ok(None)};
    let bytes=contents.into_bytes();
    write_download(path,futures_util::stream::once(async move{Ok(bytes.into())}).boxed()).await.map(Some)
}

