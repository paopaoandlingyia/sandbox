from fastapi import FastAPI, HTTPException, File, UploadFile, Form
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import subprocess
import os
import shlex
import shutil

# 获取工作目录，默认为 /workspace
WORKSPACE = os.environ.get("WORKSPACE_DIR", "/workspace")
if not os.path.exists(WORKSPACE):
    os.makedirs(WORKSPACE, exist_ok=True)

STATIC_DIR = WORKSPACE
os.makedirs(STATIC_DIR, exist_ok=True)
app = FastAPI(title="Minimalist AI Sandbox API")
# 挂载静态目录到 /workspace 路径
app.mount("/workspace", StaticFiles(directory=STATIC_DIR), name="workspace")

# 安全认证：从环境变量获取 Token，默认为 "insecure-default-token"
# 强烈建议在部署时设置环境变量 SANDBOX_TOKEN
SANDBOX_TOKEN = os.environ.get("SANDBOX_TOKEN", "123456")

from fastapi import Header, Depends

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
    language: str = "python"  # python, bash, node, etc.
    code: str
    timeout: int = 30

@app.get("/")
def read_root():
    return {"status": "online", "workspace": WORKSPACE, "auth_enabled": True}

# 语言到执行命令的映射
LANGUAGE_RUNNERS = {
    "python": "python3",
    "python3": "python3",
    "py": "python3",
    "bash": "bash",
    "sh": "sh",
    "node": "node",
    "javascript": "node",
    "js": "node",
}

# 语言到文件扩展名的映射
LANGUAGE_EXTENSIONS = {
    "python": ".py",
    "python3": ".py",
    "py": ".py",
    "bash": ".sh",
    "sh": ".sh",
    "node": ".js",
    "javascript": ".js",
    "js": ".js",
}

@app.post("/run_code", dependencies=[Depends(verify_token)])
def run_code(req: RunCodeRequest):
    """一站式代码执行：自动写入临时文件并执行"""
    lang = req.language.lower()
    
    if lang not in LANGUAGE_RUNNERS:
        raise HTTPException(
            status_code=400, 
            detail=f"Unsupported language: {req.language}. Supported: {list(LANGUAGE_RUNNERS.keys())}"
        )
    
    runner = LANGUAGE_RUNNERS[lang]
    ext = LANGUAGE_EXTENSIONS[lang]
    
    # 创建临时文件
    import uuid
    temp_filename = f"_run_{uuid.uuid4().hex[:8]}{ext}"
    temp_path = os.path.join(WORKSPACE, temp_filename)
    
    try:
        # 写入代码
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(req.code)
        
        # 执行代码
        result = subprocess.run(
            f"{runner} {temp_filename}",
            shell=True,
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=req.timeout,
            encoding='utf-8',
            errors='replace'
        )
        
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
            "temp_file": temp_filename
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Code execution timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 清理临时文件（可选，保留以便调试）
        # if os.path.exists(temp_path):
        #     os.remove(temp_path)
        pass

@app.post("/execute", dependencies=[Depends(verify_token)])
def execute(req: ExecuteRequest):
    try:
        # 执行 shell 命令
        # 我们直接在 WORKSPACE 目录下执行
        result = subprocess.run(
            req.command,
            shell=True,
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=req.timeout,
            encoding='utf-8',  # 强制使用 utf-8
            errors='replace'   # 防止编码错误导致 crash
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Command timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/write", dependencies=[Depends(verify_token)])
def write_file(req: WriteFileRequest):
    try:
        # 允许访问任意路径：只要是容器内的路径均可
        # 如果 path 是绝对路径，os.path.join 会直接使用该绝对路径
        # 如果 path 是相对路径，则相对于 WORKSPACE
        if os.path.isabs(req.path):
            full_path = req.path
        else:
            full_path = os.path.join(WORKSPACE, req.path)
        
        # 自动创建父目录
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(req.content)
            
        return {"status": "success", "path": full_path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/read", dependencies=[Depends(verify_token)])
def read_file(path: str):
    try:
        if os.path.isabs(path):
            full_path = path
        else:
            full_path = os.path.join(WORKSPACE, path)
            
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")
            
        with open(full_path, "r", encoding="utf-8", errors='replace') as f:
            content = f.read()
            
        return {"content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ===================== 新增功能 =====================

@app.get("/list", dependencies=[Depends(verify_token)])
def list_dir(path: str = "."):
    """列出目录内容，返回结构化 JSON"""
    try:
        if os.path.isabs(path):
            full_path = path
        else:
            full_path = os.path.join(WORKSPACE, path)
        
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
                "modified": stat.st_mtime
            })
        
        # 按类型和名称排序：目录在前，文件在后
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
        if os.path.isabs(path):
            full_path = path
        else:
            full_path = os.path.join(WORKSPACE, path)
        
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
    """接收二进制文件上传，写入 workspace 的指定子目录"""
    try:
        # 确定目标目录
        if subdir:
            target_dir = os.path.join(WORKSPACE, subdir)
        else:
            target_dir = WORKSPACE
        os.makedirs(target_dir, exist_ok=True)

        # 安全文件名：保留原名，冲突时追加序号
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

from fastapi.responses import HTMLResponse

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
        
        /* 登录界面 */
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
        
        /* 主界面 */
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
        <!-- 登录界面 -->
        <div id="login-ui" class="login-box">
            <h2>🔐 Sandbox 文件管理</h2>
            <input type="password" id="token-input" placeholder="输入访问密钥..." onkeydown="if(event.key==='Enter')login()">
            <button onclick="login()">验证登录</button>
            <p id="login-error" style="color:#f87171;margin-top:12px;display:none;"></p>
        </div>
        
        <!-- 主界面 -->
        <div id="main-ui" class="main-ui">
            <div class="header">
                <h1>📁 Sandbox File Manager</h1>
                <button class="logout-btn" onclick="logout()">退出登录</button>
            </div>
            
            <div class="breadcrumb" id="breadcrumb"></div>
            
            <div class="panel">
                <div class="file-list">
                    <div class="panel-header">文件列表</div>
                    <div id="file-list-content"></div>
                </div>
                <div class="preview-panel">
                    <div class="panel-header">文件预览</div>
                    <div class="preview-content" id="preview-content">
                        <div class="preview-placeholder">选择文件以预览内容</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        let TOKEN = localStorage.getItem('sandbox_token') || '';
        let currentPath = '/workspace';
        
        // 初始化
        if (TOKEN) {
            verifyAndEnter();
        }
        
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
                } else {
                    showLoginError('密钥错误或服务不可用');
                }
            } catch (e) {
                showLoginError('连接失败: ' + e.message);
            }
        }
        
        function showLoginError(msg) {
            const el = document.getElementById('login-error');
            el.textContent = msg;
            el.style.display = 'block';
        }
        
        function logout() {
            localStorage.removeItem('sandbox_token');
            TOKEN = '';
            location.reload();
        }
        
        async function fetchAPI(endpoint, options = {}) {
            return fetch(endpoint, {
                ...options,
                headers: {
                    'X-Sandbox-Token': TOKEN,
                    'Content-Type': 'application/json',
                    ...options.headers
                }
            });
        }
        
        async function loadDirectory(path) {
            currentPath = path;
            updateBreadcrumb();
            
            const listEl = document.getElementById('file-list-content');
            listEl.innerHTML = '<div class="loading">加载中...</div>';
            
            try {
                const res = await fetchAPI(`/list?path=${encodeURIComponent(path)}`);
                const data = await res.json();
                
                if (!res.ok) {
                    listEl.innerHTML = `<div class="error">${data.detail || '加载失败'}</div>`;
                    return;
                }
                
                if (data.items.length === 0) {
                    listEl.innerHTML = '<div class="loading">目录为空</div>';
                    return;
                }
                
                // 如果不是根目录，添加返回上级
                let html = '';
                if (path !== '/workspace' && path !== '/') {
                    html += `<div class="file-item" onclick="goUp()">
                        <span class="file-icon">⬆️</span>
                        <span class="file-name">..</span>
                    </div>`;
                }
                
                for (const item of data.items) {
                    const icon = item.type === 'dir' ? '📁' : getFileIcon(item.name);
                    const size = item.type === 'dir' ? '' : formatSize(item.size);
                    const itemPath = path + '/' + item.name;
                    
                    html += `<div class="file-item" data-path="${escapeHtml(itemPath)}" data-type="${item.type}" onclick="handleItemClick(this)">
                        <span class="file-icon">${icon}</span>
                        <span class="file-name">${escapeHtml(item.name)}</span>
                        <span class="file-size">${size}</span>
                        <div class="file-actions">
                            <button class="btn-delete" onclick="event.stopPropagation();deleteItem('${escapeHtml(itemPath)}')">删除</button>
                        </div>
                    </div>`;
                }
                
                listEl.innerHTML = html;
            } catch (e) {
                listEl.innerHTML = `<div class="error">请求失败: ${e.message}</div>`;
            }
        }
        
        function updateBreadcrumb() {
            const parts = currentPath.split('/').filter(p => p);
            let html = '<a onclick="loadDirectory(\'/\')">🏠</a>';
            let accPath = '';
            
            for (let i = 0; i < parts.length; i++) {
                accPath += '/' + parts[i];
                const isLast = i === parts.length - 1;
                html += '<span>/</span>';
                if (isLast) {
                    html += `<span>${escapeHtml(parts[i])}</span>`;
                } else {
                    html += `<a onclick="loadDirectory('${escapeHtml(accPath)}')">${escapeHtml(parts[i])}</a>`;
                }
            }
            
            document.getElementById('breadcrumb').innerHTML = html;
        }
        
        function goUp() {
            const parts = currentPath.split('/').filter(p => p);
            parts.pop();
            const parentPath = '/' + parts.join('/') || '/';
            loadDirectory(parentPath);
        }
        
        async function handleItemClick(el) {
            const path = el.dataset.path;
            const type = el.dataset.type;
            
            // 移除其他选中状态
            document.querySelectorAll('.file-item.selected').forEach(e => e.classList.remove('selected'));
            el.classList.add('selected');
            
            if (type === 'dir') {
                loadDirectory(path);
            } else {
                await previewFile(path);
            }
        }
        
        async function previewFile(path) {
            const previewEl = document.getElementById('preview-content');
            previewEl.innerHTML = '<div class="loading">加载中...</div>';
            
            try {
                const res = await fetchAPI(`/read?path=${encodeURIComponent(path)}`);
                const data = await res.json();
                
                if (!res.ok) {
                    previewEl.innerHTML = `<div class="error">${data.detail || '读取失败'}</div>`;
                    return;
                }
                
                // 限制预览大小
                let content = data.content;
                if (content.length > 50000) {
                    content = content.substring(0, 50000) + '\\n\\n... (内容过长，已截断)';
                }
                
                previewEl.innerHTML = `<pre>${escapeHtml(content)}</pre>`;
            } catch (e) {
                previewEl.innerHTML = `<div class="error">请求失败: ${e.message}</div>`;
            }
        }
        
        async function deleteItem(path) {
            if (!confirm('确定删除?\\n' + path)) return;
            
            try {
                const res = await fetchAPI(`/delete?path=${encodeURIComponent(path)}`, { method: 'DELETE' });
                const data = await res.json();
                
                if (res.ok) {
                    loadDirectory(currentPath);
                    document.getElementById('preview-content').innerHTML = '<div class="preview-placeholder">文件已删除</div>';
                } else {
                    alert('删除失败: ' + (data.detail || '未知错误'));
                }
            } catch (e) {
                alert('请求失败: ' + e.message);
            }
        }
        
        function getFileIcon(name) {
            const ext = name.split('.').pop().toLowerCase();
            const icons = {
                'py': '🐍', 'js': '📜', 'ts': '📘', 'json': '📋', 'md': '📝',
                'txt': '📄', 'sh': '⚙️', 'html': '🌐', 'css': '🎨',
                'jpg': '🖼️', 'jpeg': '🖼️', 'png': '🖼️', 'gif': '🖼️',
                'mp3': '🎵', 'wav': '🎵', 'mp4': '🎬', 'zip': '📦', 'tar': '📦'
            };
            return icons[ext] || '📄';
        }
        
        function formatSize(bytes) {
            if (bytes < 1024) return bytes + ' B';
            if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
            return (bytes / 1024 / 1024).toFixed(1) + ' MB';
        }
        
        function escapeHtml(str) {
            return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
        }
    </script>
</body>
</html>
"""

@app.get("/ui", response_class=HTMLResponse)
def file_manager_ui():
    """文件管理 WebUI（使用 SANDBOX_TOKEN 鉴权）"""
    return FILE_MANAGER_HTML

