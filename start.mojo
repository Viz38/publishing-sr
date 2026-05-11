from python import Python

def launch_services():
    try:
        var py_os = Python.import_module("os")
        var py_subprocess = Python.import_module("subprocess")
        var py_builtins = Python.import_module("builtins")
        
        var base_dir = String(py_os.getcwd())
        
        var folders = py_builtins.list()
        _ = folders.append("TypeA")
        _ = folders.append("TypeB")
        _ = folders.append("TypeC")
        
        var ports = py_builtins.list()
        _ = ports.append("8767")
        _ = ports.append("8765")
        _ = ports.append("8766")
        
        for i in range(len(folders)):
            var folder = String(folders[i])
            var port = String(ports[i])
            
            print("🚀 Orchestrator: Launching " + folder + " on port " + port + "...")
            
            var folder_path = base_dir + "/" + folder
            
            # Manually build a clean environment dictionary
            var clean_env = py_builtins.dict()
            var env_keys = py_builtins.list(py_os.environ.keys())
            for j in range(len(env_keys)):
                var k = String(env_keys[j])
                if k != "PYTHONPATH" and k != "PYTHONHOME" and k != "PYTHONEXECUTABLE" and not k.startswith("PIXI_"):
                    clean_env[k] = py_os.environ[k]
            
            clean_env["PORT"] = port
            clean_env["VIRTUAL_ENV"] = folder_path + "/.venv"
            
            # 🛡️ Fix: Add both the venv AND the Pixi Mojo bin to the PATH
            var pixi_bin = folder_path + "/.pixi/envs/default/bin"
            clean_env["PATH"] = folder_path + "/.venv/bin:" + pixi_bin + ":/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin"
            
            # Ensure Mojo finds its standard library
            clean_env["MODULAR_HOME"] = folder_path + "/.pixi/envs/default/share/max"
            
            var venv_uvicorn = folder_path + "/.venv/bin/uvicorn"
            
            var args = py_builtins.list()
            _ = args.append(venv_uvicorn)
            _ = args.append("api:app")
            _ = args.append("--host")
            _ = args.append("0.0.0.0")
            _ = args.append("--port")
            _ = args.append(port)
            _ = args.append("--log-level")
            _ = args.append("info")
            
            _ = py_subprocess.Popen(args, env=clean_env, cwd=folder_path)
            
        print("✨ All services spawned successfully.")
        
    except e:
        print("❌ Orchestrator Error:", e)

def main():
    launch_services()
