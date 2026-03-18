import json
import os
import shutil
import subprocess
import uuid

from fastapi import FastAPI, HTTPException, File, UploadFile, Form, Header, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastmcp import FastMCP
from pydantic import BaseModel

# ===================== 配置 =====================

WORKSPACE = os.environ.get("WORKSPACE_DIR", "/workspace")
os.makedirs(WORKSPACE, exist_ok=True)

SANDBOX_TOKEN = os.environ.get("SANDBOX_TOKEN", "123456")

LANGUAGE_RUNNERS = {
    "python": "python3", "python3": "python3", "py": "python3",
    "bash": "bash", "sh": "sh",
    "node": "node", "javascript": "node", "js": "node",
}

LANGUAGE_EXTENSIONS = {
    "python": ".py", "python3": ".py", "py": ".py",
    "bash": ".sh", "sh": ".sh",
    "node": ".js", "javascript": ".js", "js": ".js",
}

# ===================== 核心逻辑（框架无关）=====================

def core_execute_command(command: str, timeout: int = 30) -> dict:
    """执行 shell 命令"""
    try:
        result = subprocess.run(
            command, shell=True, cwd=WORKSPACE,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


def core_run_code(language: str, code: str, timeout: int = 30) -> dict:
    """写入临时文件并执行代码"""
    lang = language.lower()
    if lang not in LANGUAGE_RUNNERS:
        return {
            "stdout": "",
            "stderr": f"Unsupported language: {language}. Supported: {list(LANGUAGE_RUNNERS.keys())}",
            "exit_code": -1,
        }

    runner = LANGUAGE_RUNNERS[lang]
    ext = LANGUAGE_EXTENSIONS[lang]
    temp_filename = f"_run_{uuid.uuid4().hex[:8]}{ext}"
    temp_path = os.path.join(WORKSPACE, temp_filename)

    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(code)

        result = subprocess.run(
            f"{runner} {temp_filename}", shell=True, cwd=WORKSPACE,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return {
            "stdout": result.stdout, "stderr": result.stderr,
            "exit_code": result.returncode, "temp_file": temp_filename,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Code execution timed out", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


def core_write_file(path: str, content: str) -> dict:
    """写入文件"""
    try:
        full_path = path if os.path.isabs(path) else os.path.join(WORKSPACE, path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return {"status": "success", "path": os.path.relpath(full_path, WORKSPACE)}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def core_read_file(path: str) -> dict:
    """读取文件"""
    try:
        full_path = path if os.path.isabs(path) else os.path.join(WORKSPACE, path)
        if not os.path.exists(full_path):
            return {"content": None, "error": "File not found"}
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            return {"content": f.read()}
    except Exception as e:
        return {"content": None, "error": str(e)}


# ===================== MCP 层 =====================

mcp = FastMCP(
    name="Cloud Sandbox",
    instructions=(
        "一个运行在 Docker 容器中的云端沙盒环境。"
        "你拥有完整的 Linux 环境权限，可以执行命令、运行代码、读写文件。"
        "工作目录为 /workspace。已预装 Python3、Node.js、常用系统工具。"
    ),
)


@mcp.tool
def execute_command(command: str, timeout: int = 30) -> str:
    """在沙盒中执行 shell 命令。

    可以执行任意 Linux 命令，如 ls、pip install、curl、git 等。
    工作目录为 /workspace。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时时间（秒），默认 30
    """
    result = core_execute_command(command, timeout)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool
def run_code(language: str, code: str, timeout: int = 30) -> str:
    """在沙盒中执行代码片段。

    自动创建临时文件并执行，无需手动写文件。
    支持的语言: python, bash, node (javascript)

    Args:
        language: 编程语言 (python/bash/node)
        code: 要执行的代码内容
        timeout: 超时时间（秒），默认 30
    """
    result = core_run_code(language, code, timeout)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool
def write_file(path: str, content: str) -> str:
    """将内容写入沙盒中的文件。

    支持相对路径（相对于 /workspace）和绝对路径。
    自动创建不存在的父目录。

    Args:
        path: 文件路径（相对于 /workspace 或绝对路径）
        content: 要写入的文件内容
    """
    result = core_write_file(path, content)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool
def read_file(path: str) -> str:
    """读取沙盒中的文件内容。

    支持相对路径（相对于 /workspace）和绝对路径。

    Args:
        path: 文件路径（相对于 /workspace 或绝对路径）
    """
    result = core_read_file(path)
    return json.dumps(result, ensure_ascii=False)


# ===================== FastAPI + MCP 挂载 =====================

# 用原生 ASGI 中间件包装 MCP 应用，实现 Token 认证
# （BaseHTTPMiddleware 与 ASGI 子应用有兼容性问题，不能用）
class MCPAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            token = headers.get(b"x-sandbox-token", b"").decode()
            if token != SANDBOX_TOKEN:
                response = JSONResponse(status_code=403, content={"detail": "Invalid X-Sandbox-Token"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

# 创建 MCP ASGI 应用，用认证中间件包装
raw_mcp_app = mcp.http_app(path="/", stateless_http=True)
mcp_app = MCPAuthMiddleware(raw_mcp_app)

app = FastAPI(title="AI Sandbox", lifespan=raw_mcp_app.lifespan)

# 挂载 MCP Streamable HTTP 端点
app.mount("/mcp", mcp_app)


# ===================== REST API（机器人插件兼容）=====================

async def verify_token(x_sandbox_token: str = Header(None)):
    if x_sandbox_token != SANDBOX_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid X-Sandbox-Token")


class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 30

class WriteFileRequest(BaseModel):
    path: str
    content: str

class RunCodeRequest(BaseModel):
    language: str = "python"
    code: str
    timeout: int = 30


@app.get("/")
def read_root():
    return {"status": "online", "workspace": WORKSPACE, "auth_enabled": True, "mcp_endpoint": "/mcp"}


@app.post("/execute", dependencies=[Depends(verify_token)])
def api_execute(req: ExecuteRequest):
    result = core_execute_command(req.command, req.timeout)
    if result["exit_code"] == -1 and "timed out" in result["stderr"]:
        raise HTTPException(status_code=408, detail="Command timed out")
    return result


@app.post("/run_code", dependencies=[Depends(verify_token)])
def api_run_code(req: RunCodeRequest):
    result = core_run_code(req.language, req.code, req.timeout)
    if result["exit_code"] == -1 and "Unsupported language" in result["stderr"]:
        raise HTTPException(status_code=400, detail=result["stderr"])
    if result["exit_code"] == -1 and "timed out" in result["stderr"]:
        raise HTTPException(status_code=408, detail="Code execution timed out")
    return result


@app.post("/write", dependencies=[Depends(verify_token)])
def api_write_file(req: WriteFileRequest):
    result = core_write_file(req.path, req.content)
    if result["status"] == "error":
        raise HTTPException(status_code=500, detail=result["detail"])
    return result


@app.get("/read", dependencies=[Depends(verify_token)])
def api_read_file(path: str):
    result = core_read_file(path)
    if result.get("error"):
        status = 404 if result["error"] == "File not found" else 500
        raise HTTPException(status_code=status, detail=result["error"])
    return result


# ===================== WebUI 专用端点 =====================

@app.get("/list", dependencies=[Depends(verify_token)])
def list_dir(path: str = "."):
    """列出目录内容"""
    try:
        full_path = path if os.path.isabs(path) else os.path.join(WORKSPACE, path)
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="Path not found")
        if not os.path.isdir(full_path):
            raise HTTPException(status_code=400, detail="Path is not a directory")

        items = []
        for name in os.listdir(full_path):
            item_path = os.path.join(full_path, name)
            stat = os.stat(item_path)
            items.append({
                "name": name,
                "type": "dir" if os.path.isdir(item_path) else "file",
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
        items.sort(key=lambda x: (0 if x["type"] == "dir" else 1, x["name"].lower()))
        return {"path": full_path, "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/delete", dependencies=[Depends(verify_token)])
def delete_path(path: str):
    """删除文件或目录"""
    try:
        full_path = path if os.path.isabs(path) else os.path.join(WORKSPACE, path)
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="Path not found")
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
        else:
            os.remove(full_path)
        return {"status": "success", "deleted": full_path}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/upload", dependencies=[Depends(verify_token)])
async def upload_file(file: UploadFile = File(...), subdir: str = Form("")):
    """接收二进制文件上传"""
    try:
        target_dir = os.path.join(WORKSPACE, subdir) if subdir else WORKSPACE
        os.makedirs(target_dir, exist_ok=True)

        filename = file.filename or "uploaded_file"
        target_path = os.path.join(target_dir, filename)
        if os.path.exists(target_path):
            name, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(target_path):
                target_path = os.path.join(target_dir, f"{name}_{counter}{ext}")
                counter += 1

        content = await file.read()
        with open(target_path, "wb") as f:
            f.write(content)

        rel_path = os.path.relpath(target_path, WORKSPACE)
        return {"path": rel_path, "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ===================== 文件管理 WebUI =====================

FILE_MANAGER_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sandbox File Manager</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Segoe UI', 'PingFang SC', sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: #e4e4e7;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        .login-box {
            background: rgba(255,255,255,0.05);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 16px;
            padding: 40px;
            max-width: 400px;
            margin: 100px auto;
            text-align: center;
        }
        .login-box h2 { margin-bottom: 24px; color: #60a5fa; }
        .login-box input {
            width: 100%;
            padding: 12px 16px;
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 8px;
            background: rgba(0,0,0,0.3);
            color: #fff;
            font-size: 16px;
            margin-bottom: 16px;
        }
        .login-box button {
            width: 100%;
            padding: 12px;
            background: linear-gradient(135deg, #3b82f6, #8b5cf6);
            border: none;
            border-radius: 8px;
            color: #fff;
            font-size: 16px;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .login-box button:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(59,130,246,0.3);
        }
        .main-ui { display: none; }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .header h1 { font-size: 24px; color: #60a5fa; }
        .logout-btn {
            padding: 8px 16px;
            background: rgba(239,68,68,0.2);
            border: 1px solid rgba(239,68,68,0.5);
            border-radius: 6px;
            color: #f87171;
            cursor: pointer;
        }
        .breadcrumb {
            display: flex;
            gap: 8px;
            align-items: center;
            margin-bottom: 16px;
            padding: 12px 16px;
            background: rgba(255,255,255,0.05);
            border-radius: 8px;
            flex-wrap: wrap;
        }
        .breadcrumb span { color: #9ca3af; }
        .breadcrumb a { color: #60a5fa; text-decoration: none; cursor: pointer; }
        .breadcrumb a:hover { text-decoration: underline; }
        .panel {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }
        @media (max-width: 900px) { .panel { grid-template-columns: 1fr; } }
        .file-list, .preview-panel {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            overflow: hidden;
        }
        .panel-header {
            padding: 12px 16px;
            background: rgba(255,255,255,0.05);
            border-bottom: 1px solid rgba(255,255,255,0.08);
            font-weight: 600;
        }
        .file-item {
            display: flex;
            align-items: center;
            padding: 10px 16px;
            border-bottom: 1px solid rgba(255,255,255,0.05);
            cursor: pointer;
            transition: background 0.15s;
        }
        .file-item:hover { background: rgba(255,255,255,0.05); }
        .file-item.selected { background: rgba(59,130,246,0.2); }
        .file-icon { margin-right: 12px; font-size: 18px; }
        .file-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .file-size { color: #9ca3af; font-size: 12px; margin-left: 12px; }
        .file-actions { display: flex; gap: 8px; }
        .file-actions button {
            padding: 4px 8px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            transition: opacity 0.15s;
        }
        .file-actions button:hover { opacity: 0.8; }
        .btn-delete { background: #dc2626; color: #fff; }
        .preview-content {
            padding: 16px;
            max-height: 500px;
            overflow: auto;
        }
        .preview-content pre {
            background: rgba(0,0,0,0.3);
            padding: 16px;
            border-radius: 8px;
            overflow-x: auto;
            font-family: 'Fira Code', 'Consolas', monospace;
            font-size: 13px;
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-all;
        }
        .preview-placeholder {
            color: #6b7280;
            text-align: center;
            padding: 40px;
        }
        .loading { text-align: center; padding: 40px; color: #6b7280; }
        .error { color: #f87171; padding: 16px; }
    </style>
</head>
<body>
    <div class="container">
        <div id="login-ui" class="login-box">
            <h2>Sandbox File Manager</h2>
            <input type="password" id="token-input" placeholder="Enter access token..." onkeydown="if(event.key==='Enter')login()">
            <button onclick="login()">Login</button>
            <p id="login-error" style="color:#f87171;margin-top:12px;display:none;"></p>
        </div>
        <div id="main-ui" class="main-ui">
            <div class="header">
                <h1>Sandbox File Manager</h1>
                <button class="logout-btn" onclick="logout()">Logout</button>
            </div>
            <div class="breadcrumb" id="breadcrumb"></div>
            <div class="panel">
                <div class="file-list">
                    <div class="panel-header">Files</div>
                    <div id="file-list-content"></div>
                </div>
                <div class="preview-panel">
                    <div class="panel-header">Preview</div>
                    <div class="preview-content" id="preview-content">
                        <div class="preview-placeholder">Select a file to preview</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script>
        let TOKEN = localStorage.getItem('sandbox_token') || '';
        let currentPath = '/workspace';
        if (TOKEN) { verifyAndEnter(); }

        async function login() {
            TOKEN = document.getElementById('token-input').value;
            if (!TOKEN) return;
            await verifyAndEnter();
        }
        async function verifyAndEnter() {
            try {
                const res = await fetchAPI('/list?path=/workspace');
                if (res.ok) {
                    localStorage.setItem('sandbox_token', TOKEN);
                    document.getElementById('login-ui').style.display = 'none';
                    document.getElementById('main-ui').style.display = 'block';
                    loadDirectory('/workspace');
                } else { showLoginError('Invalid token or service unavailable'); }
            } catch (e) { showLoginError('Connection failed: ' + e.message); }
        }
        function showLoginError(msg) {
            const el = document.getElementById('login-error');
            el.textContent = msg; el.style.display = 'block';
        }
        function logout() { localStorage.removeItem('sandbox_token'); TOKEN = ''; location.reload(); }
        async function fetchAPI(endpoint, options = {}) {
            return fetch(endpoint, { ...options, headers: { 'X-Sandbox-Token': TOKEN, 'Content-Type': 'application/json', ...options.headers } });
        }
        async function loadDirectory(path) {
            currentPath = path; updateBreadcrumb();
            const listEl = document.getElementById('file-list-content');
            listEl.innerHTML = '<div class="loading">Loading...</div>';
            try {
                const res = await fetchAPI(`/list?path=${encodeURIComponent(path)}`);
                const data = await res.json();
                if (!res.ok) { listEl.innerHTML = `<div class="error">${data.detail || 'Load failed'}</div>`; return; }
                if (data.items.length === 0) { listEl.innerHTML = '<div class="loading">Empty directory</div>'; return; }
                let html = '';
                if (path !== '/workspace' && path !== '/') {
                    html += `<div class="file-item" onclick="goUp()"><span class="file-icon">⬆️</span><span class="file-name">..</span></div>`;
                }
                for (const item of data.items) {
                    const icon = item.type === 'dir' ? '📁' : getFileIcon(item.name);
                    const size = item.type === 'dir' ? '' : formatSize(item.size);
                    const itemPath = path + '/' + item.name;
                    html += `<div class="file-item" data-path="${escapeHtml(itemPath)}" data-type="${item.type}" onclick="handleItemClick(this)">
                        <span class="file-icon">${icon}</span><span class="file-name">${escapeHtml(item.name)}</span>
                        <span class="file-size">${size}</span>
                        <div class="file-actions"><button class="btn-delete" onclick="event.stopPropagation();deleteItem('${escapeHtml(itemPath)}')">Delete</button></div>
                    </div>`;
                }
                listEl.innerHTML = html;
            } catch (e) { listEl.innerHTML = `<div class="error">Request failed: ${e.message}</div>`; }
        }
        function updateBreadcrumb() {
            const parts = currentPath.split('/').filter(p => p);
            let html = '<a onclick="loadDirectory(\'/\')">🏠</a>';
            let accPath = '';
            for (let i = 0; i < parts.length; i++) {
                accPath += '/' + parts[i];
                html += '<span>/</span>';
                html += i === parts.length - 1 ? `<span>${escapeHtml(parts[i])}</span>` : `<a onclick="loadDirectory('${escapeHtml(accPath)}')">${escapeHtml(parts[i])}</a>`;
            }
            document.getElementById('breadcrumb').innerHTML = html;
        }
        function goUp() {
            const parts = currentPath.split('/').filter(p => p); parts.pop();
            loadDirectory('/' + parts.join('/') || '/');
        }
        async function handleItemClick(el) {
            const path = el.dataset.path, type = el.dataset.type;
            document.querySelectorAll('.file-item.selected').forEach(e => e.classList.remove('selected'));
            el.classList.add('selected');
            if (type === 'dir') loadDirectory(path); else await previewFile(path);
        }
        async function previewFile(path) {
            const previewEl = document.getElementById('preview-content');
            previewEl.innerHTML = '<div class="loading">Loading...</div>';
            try {
                const res = await fetchAPI(`/read?path=${encodeURIComponent(path)}`);
                const data = await res.json();
                if (!res.ok) { previewEl.innerHTML = `<div class="error">${data.detail || 'Read failed'}</div>`; return; }
                let content = data.content;
                if (content.length > 50000) content = content.substring(0, 50000) + '\\n\\n... (truncated)';
                previewEl.innerHTML = `<pre>${escapeHtml(content)}</pre>`;
            } catch (e) { previewEl.innerHTML = `<div class="error">Request failed: ${e.message}</div>`; }
        }
        async function deleteItem(path) {
            if (!confirm('Delete?\\n' + path)) return;
            try {
                const res = await fetchAPI(`/delete?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
                if (res.ok) { loadDirectory(currentPath); document.getElementById('preview-content').innerHTML = '<div class="preview-placeholder">File deleted</div>'; }
                else { const data = await res.json(); alert('Delete failed: ' + (data.detail || 'Unknown error')); }
            } catch (e) { alert('Request failed: ' + e.message); }
        }
        function getFileIcon(name) {
            const ext = name.split('.').pop().toLowerCase();
            const icons = { 'py':'🐍','js':'📜','ts':'📘','json':'📋','md':'📝','txt':'📄','sh':'⚙️','html':'🌐','css':'🎨','jpg':'🖼️','jpeg':'🖼️','png':'🖼️','gif':'🖼️','mp3':'🎵','wav':'🎵','mp4':'🎬','zip':'📦','tar':'📦' };
            return icons[ext] || '📄';
        }
        function formatSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / 1024 / 1024).toFixed(1) + ' MB';
        }
        function escapeHtml(str) { return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
    </script>
</body>
</html>
"""

@app.get("/ui", response_class=HTMLResponse)
def file_manager_ui():
    return FILE_MANAGER_HTML


# 静态文件挂载（必须放最后）
app.mount("/", StaticFiles(directory=WORKSPACE), name="static")
