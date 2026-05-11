from python import Python

def start_api():
    try:
        var py_os = Python.import_module("os")
        
        var cwd = String(py_os.getcwd())
        var venv_python = cwd + "/.venv/bin/python3.13"
        var port = py_os.environ.get("PORT", "8768")
        var home = py_os.environ.get("HOME", "/Users/vishnu")
        
        # 🛡️ THE NUCLEAR OPTION: env -i
        var cmd = "env -i " + \
                  "HOME=" + home + " " + \
                  "PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin " + \
                  "PORT=" + String(port) + " " + \
                  "MOJO_HOSTED=1 " + \
                  venv_python + " -m uvicorn api:app --host 0.0.0.0 --port " + String(port) + " --log-level info"
        
        print("🛡️ Mojo: Launching FastAPI via isolated process (env -i)...")
        _ = py_os.system(cmd)
        
    except e:
        print("❌ Mojo API Error:", e)

def main():
    start_api()
