from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import subprocess
import os
import shlex
import shutil

app = FastAPI(title="Minimalist AI Sandbox API")

# 获取工作目录，默认为 /workspace
WORKSPACE = os.environ.get("WORKSPACE_DIR", "/workspace")
if not os.path.exists(WORKSPACE):
    os.makedirs(WORKSPACE, exist_ok=True)

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
