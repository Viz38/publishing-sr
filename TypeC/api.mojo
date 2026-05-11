from python import Python

def start_api():
    try:
        var py_os = Python.import_module("os")
        
        var cwd = String(py_os.getcwd())
        var venv_python = cwd + "/.venv/bin/python3.13"
        
        var port_val = String(py_os.environ.get("PORT", "8769"))
        var home_val = String(py_os.environ.get("HOME", "/Users/vishnu"))
        
        # 🛡️ THE NUCLEAR OPTION: env -i
        var cmd = "env -i " + \
                  "HOME=" + home_val + " " + \
                  "PATH=/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin " + \
                  "PORT=" + port_val + " " + \
                  "MOJO_HOSTED=1 " + \
                  venv_python + " -m uvicorn api:app --host 0.0.0.0 --port " + port_val + " --log-level info"
        
        print("🛡️ Mojo: Launching FastAPI via isolated process (env -i)...")
        _ = py_os.system(cmd)
        
    except e:
        print("❌ Mojo API Error:", e)

def main():
    start_api()
