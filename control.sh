#!/bin/bash

# ==========================================================
# 🚀 SR PUBLISHING - PRODUCTION v6.4
# ==========================================================
# Supports: macOS (Intel/Silicon) & Ubuntu
# ==========================================================

# Add Homebrew to PATH for macOS
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BASE_DIR=$(cd "$(dirname "$0")" && pwd)
OS=$(uname -s)
REAL_USER=${SUDO_USER:-$USER}
LOCK_FILE="/tmp/sr_control.lock"

# --- Lock File Mechanism ---
if [ -f "$LOCK_FILE" ]; then
    OLD_PID=$(cat "$LOCK_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo -e "\033[0;31m⚠️  Another instance of control.sh is already running (PID: $OLD_PID).\033[0m"
        # Check if we are in an interactive terminal
        if [ -t 0 ]; then
            read -p "Would you like to kill it and start fresh? (y/n): " kill_confirm
            if [[ "$kill_confirm" == "y" || "$kill_confirm" == "Y" ]]; then
                kill -9 "$OLD_PID" 2>/dev/null
                rm -f "$LOCK_FILE"
            else
                echo "Exiting."
                exit 1
            fi
        else
            echo "Non-interactive session detected. Killing old instance..."
            kill -9 "$OLD_PID" 2>/dev/null
            rm -f "$LOCK_FILE"
        fi
    else
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"
trap "rm -f $LOCK_FILE; exit" INT TERM EXIT

# Centralized logging for control.sh
LOG_FILE="$BASE_DIR/control.logs"
SETUP_LOG="$BASE_DIR/setup.logs"

log() {
    local level=$1
    local msg=$2
    local timestamp=$(date "+%Y-%m-%d %H:%M:%S")
    echo -e "[$timestamp] $level: $msg" >> "$LOG_FILE"
}

# Redirect all output to control.logs but keep it clean
# We'll use this for the main loop, but noisy commands will be silenced
echo -e "\n--- Session Started: $(date) ---" >> "$LOG_FILE"

# Ensure dependencies on Ubuntu
if [[ "$OS" == "Linux" ]]; then
    for cmd in screen lsof; do
        if ! command -v $cmd &>/dev/null; then
            echo -e "${YELLOW}⚠️  $cmd is missing. Installing...${NC}"
            log "INFO" "Installing missing dependency: $cmd"
            sudo apt-get update >> "$SETUP_LOG" 2>&1 && sudo apt-get install -y $cmd >> "$SETUP_LOG" 2>&1
        fi
    done
fi

FOLDERS=("TypeA" "TypeB" "TypeC")
PORTS=(8767 8765 8766)
NAMES=("sr-typea" "sr-typeb" "sr-typec")

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

get_python() {
    for v in 13 12 11 10; do
        if command -v "python3.$v" &> /dev/null; then echo "python3.$v"; return; fi
    done
    if command -v python3 &> /dev/null; then echo "python3"; return; fi
    echo ""
}
# Load environment variables from .env
if [ -f ".env" ]; then
    # Improved .env loading to handle spaces and quotes
    export $(grep -v '^#' .env | xargs -0 2>/dev/null || grep -v '^#' .env | xargs)
fi

PYTHON_CMD=$(get_python)
CURRENT_USER=$(whoami)

# Normalize user for matching
USER_LOWER=$(echo "$CURRENT_USER" | tr '[:upper:]' '[:lower:]')

case "$USER_LOWER" in
    "vishnu")
        IDENTITY="Vishnu"
        [ -z "$TUNNEL_TOKEN" ] && TUNNEL_TOKEN="$TUNNEL_TOKEN_VISHNU"
        ;;
    "tracxn-ds-499")
        IDENTITY="Device-4990"
        [ -z "$TUNNEL_TOKEN" ] && TUNNEL_TOKEN="$TUNNEL_TOKEN_4990"
        ;;
    "tracxn-ds-423")
        IDENTITY="Device-4230"
        [ -z "$TUNNEL_TOKEN" ] && TUNNEL_TOKEN="$TUNNEL_TOKEN_4230"
        ;;
    "tracxn-lp-599")
        IDENTITY="Device-599"
        [ -z "$TUNNEL_TOKEN" ] && TUNNEL_TOKEN="$TUNNEL_TOKEN_599"
        ;;
    *)
        IDENTITY="Generic (Default)"
        if [ -z "$TUNNEL_TOKEN" ]; then
            log "ERROR" "No identity matched for user '$CURRENT_USER' and no TUNNEL_TOKEN found."
            echo -e "${RED}❌ Error: No identity matched for user '$CURRENT_USER' and no TUNNEL_TOKEN found in .env.${NC}"
            echo -e "${YELLOW}Please add your username to control.sh or set TUNNEL_TOKEN in .env${NC}"
            exit 1
        fi
        ;;
esac
log "INFO" "Session started for $IDENTITY (User: $CURRENT_USER)"

check_port() {
    local port=$1
    if command -v lsof &>/dev/null; then
        lsof -Pi :$port -sTCP:LISTEN -t >/dev/null
        return $?
    elif command -v ss &>/dev/null; then
        # More robust grep for Linux ss output
        ss -ltn | grep -E ":$port |:$port$" >/dev/null
        return $?
    else
        netstat -ltn | grep -E ":$port |:$port$" >/dev/null
        return $?
    fi
}

check_status() {
    echo -e "${BLUE}📡 System Status:${NC}"
    for i in "${!FOLDERS[@]}"; do
        if check_port ${PORTS[$i]}; then
            echo -e "   ▶ ${NAMES[$i]}: ${GREEN}ONLINE${NC} (Port ${PORTS[$i]})"
        else
            echo -e "   ▶ ${NAMES[$i]}: ${RED}OFFLINE${NC}"
        fi
    done
    
    if [[ "$OS" == "Darwin" ]]; then
        echo -ne "   ▶ LaunchAgents: "
        launchctl list | grep -qE "sr-type|type[a-c]-api|com.tracxn.sr" && echo -e "${GREEN}LOADED${NC}" || echo -e "${YELLOW}NONE${NC}"
    else
        echo -ne "   ▶ Systemd: "
        systemctl list-units --type=service | grep -qE "sr-type|type[a-c]-api|tracxn-sr" && echo -e "${GREEN}LOADED${NC}" || echo -e "${YELLOW}NONE${NC}"
    fi

    pgrep -x "cloudflared" >/dev/null 2>&1 && echo -e "   ▶ Tunnel: ${GREEN}CONNECTED${NC}" || echo -e "   ▶ Tunnel: ${RED}DISCONNECTED${NC}"
}

check_for_updates() {
    if [ ! -d ".git" ]; then
        echo -e "${RED}❌ Error: This folder was not cloned via Git.${NC}"
        echo -e "${YELLOW}Updates are only supported if the repository was cloned using 'git clone'.${NC}"
        return
    fi
    echo -e "${BLUE}🔍 Checking for updates...${NC}"
    git fetch origin main &>/dev/null
    
    local LOCAL=$(git rev-parse @ 2>/dev/null)
    local REMOTE=$(git rev-parse @{u} 2>/dev/null)
    
    if [ -z "$LOCAL" ] || [ -z "$REMOTE" ]; then
        echo -e "${RED}Error: Could not check version. Check your internet connection.${NC}"
        return
    fi
    
    if [ "$LOCAL" != "$REMOTE" ]; then
        echo -e "${YELLOW}✨ New updates available!${NC}"
        echo -e "${RED}⚠️  Warning: Updating will stop all running processes.${NC}"
        read -p "Do you want to update now? (y/n): " confirm
        if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
            stop_all
            echo -e "${BLUE}📥 Pulling new code...${NC}"
            git pull origin main
            echo -e "${GREEN}✅ Update completed successfully.${NC}"
            read -p "Hit ENTER to exit and restart the script manually..."
            exit 0
        fi
    else
        echo -e "${GREEN}✅ You are on the latest version.${NC}"
    fi
}

clear_logs() {
    echo -e "${YELLOW}🧹 Clear Logs Menu:${NC}"
    echo "1) Clear All Logs (Including control.logs)"
    echo "2) Clear Type A Logs"
    echo "3) Clear Type B Logs"
    echo "4) Clear Type C Logs"
    echo "5) Clear control.logs Only"
    echo "6) Back"
    read -p "Option [1-6]: " log_opt
    case $log_opt in
        1) 
            rm -f Type*/Logs/*.log Type*/Logs/*.logs
            > "$LOG_FILE"
            echo -e "${GREEN}✅ All logs cleared.${NC}";;
        2) rm -f TypeA/Logs/*.log TypeA/Logs/*.logs; echo -e "${GREEN}✅ Type A logs cleared.${NC}";;
        3) rm -f TypeB/Logs/*.log TypeB/Logs/*.logs; echo -e "${GREEN}✅ Type B logs cleared.${NC}";;
        4) rm -f TypeC/Logs/*.log TypeC/Logs/*.logs; echo -e "${GREEN}✅ Type C logs cleared.${NC}";;
        5) > "$LOG_FILE"; echo -e "${GREEN}✅ control.logs cleared.${NC}";;
        *) return;;
    esac
}

stop_all() {
    echo -e "${RED}🛑 Stopping all services...${NC}"
    
    if [[ "$OS" == "Darwin" ]]; then
        local all_labels=("sr-typea" "sr-typeb" "sr-typec" "typea-api" "typeb-api" "typec-api" "com.tracxn.sr.typea" "com.tracxn.sr.typeb" "com.tracxn.sr.typec")
        for n in "${all_labels[@]}"; do
            launchctl bootout "gui/$(id -u)/$n" 2>/dev/null
            launchctl unload "$HOME/Library/LaunchAgents/$n.plist" 2>/dev/null
            rm "$HOME/Library/LaunchAgents/$n.plist" 2>/dev/null
        done
        for n in "${NAMES[@]}"; do screen -X -S "${n}_Engine" quit 2>/dev/null; done
    else
        for n in "${NAMES[@]}"; do
            sudo systemctl disable --now $n 2>/dev/null
            sudo rm "/etc/systemd/system/$n.service" 2>/dev/null
        done
        sudo systemctl daemon-reload
    fi
    
    # Unified Cloudflare Cleanup
    sudo cloudflared service uninstall 2>/dev/null
    
    # Nuclear Cleanup for orphaned processes
    pkill -f "uvicorn" 2>/dev/null
    pkill -f "main.py" 2>/dev/null
    pkill -f "cloudflared" 2>/dev/null
    pkill -f "firefox" 2>/dev/null
    pkill -f "playwright" 2>/dev/null
    
    for p in "${PORTS[@]}"; do lsof -ti :$p | xargs kill -9 2>/dev/null; done
    echo -e "${GREEN}Cleanup complete.${NC}"
}

verify_port() {
    local port=$1
    local name=$2
    echo -ne "   ⏳ Waiting for $name (Port $port)..."
    sleep 2 # Give process a moment to bind
    for i in {1..15}; do
        if check_port $port; then
            echo -e " ${GREEN}ONLINE${NC}"
            log "INFO" "Service $name started on port $port"
            return 0
        fi
        sleep 1
    done
    echo -e " ${RED}FAILED${NC}"
    log "ERROR" "Service $name failed to start on port $port"
    echo -e "${YELLOW}   Last logs from $name:${NC}"
    tail -n 5 "$BASE_DIR/${FOLDERS[$((i-1))]}/Logs/api.logs" 2>/dev/null | sed 's/^/      /'
    return 1
}

create_runner() {
    local f_path=$1
    local f_port=$2
    local f_label=$3
    local runner="$f_path/runner.sh"
    # Robust permission fix: Reset Logs directory
    sudo mkdir -p "$f_path/Logs"
    sudo chown -R $REAL_USER:$REAL_USER "$f_path"
    sudo chmod -R 775 "$f_path/Logs"
    # Create empty log file if not exists
    sudo -u $REAL_USER touch "$f_path/Logs/api.logs"
    
    cat <<EOF > "$runner"
#!/bin/bash
cd "$f_path"
# Ensure project root is in PYTHONPATH for sr_common imports
export PYTHONPATH="$BASE_DIR:\$PYTHONPATH"
export PYTHONUNBUFFERED=1
./.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port $f_port --workers 1 --log-level info >> "$f_path/Logs/api.logs" 2>&1
EOF
    sudo chown $REAL_USER:$REAL_USER "$runner"
    sudo chmod +x "$runner"
    xattr -d com.apple.quarantine "$runner" 2>/dev/null
}

start_standard() {
    stop_all
    echo -e "${BLUE}🚀 Starting Engines in Temp Persistent Background (via Screen)...${NC}"
    
    # Tunnel
    echo -e "   ▶ Starting Tunnel..."
    sudo cloudflared service install "$TUNNEL_TOKEN" 2>/dev/null
    if [[ "$OS" == "Darwin" ]]; then
        sudo launchctl load -w /Library/LaunchDaemons/com.cloudflare.cloudflared.plist 2>/dev/null
    else
        sudo systemctl enable --now cloudflared 2>/dev/null
    fi

    for i in "${!FOLDERS[@]}"; do
        local f_name="${FOLDERS[$i]}"
        local f_port="${PORTS[$i]}"
        local f_path="$BASE_DIR/$f_name"
        local f_label="${NAMES[$i]}"
        
        local f_creds="$f_path/${f_name}.json"
        
        [ ! -d "$f_path/.venv" ] && { echo -e "${RED}❌ $f_name: Venv missing!${NC}"; continue; }
        if [ ! -f "$f_creds" ]; then
            echo -e "${RED}❌ $f_name: Credentials missing ($f_name.json)!${NC}"
            echo -e "${YELLOW}   Please manually upload the service account key to $f_creds${NC}"
            continue
        fi
        
        echo -e "   ▶ Launching $f_label..."
        mkdir -p "$f_path/Logs"
        create_runner "$f_path" "$f_port" "$f_label"
        screen -d -m -S "${f_label}_Engine" "$f_path/runner.sh"
        verify_port $f_port $f_label
    done
    echo -e "${GREEN}Engines and Tunnel active.${NC}"
}

start_service_mode() {
    stop_all
    echo -e "${BLUE}🚀 Installing OS-Native Services (Production Mode)...${NC}"
    
    # Tunnel
    sudo cloudflared service install "$TUNNEL_TOKEN" 2>/dev/null
    if [[ "$OS" == "Darwin" ]]; then
        sudo launchctl load -w /Library/LaunchDaemons/com.cloudflare.cloudflared.plist 2>/dev/null
    else
        sudo systemctl enable --now cloudflared 2>/dev/null
    fi

    for i in "${!FOLDERS[@]}"; do
        local f_name="${FOLDERS[$i]}"
        local f_port="${PORTS[$i]}"
        local f_path="$BASE_DIR/$f_name"
        local f_label="${NAMES[$i]}"
        local f_runner="$f_path/runner.sh"
        local f_creds="$f_path/${f_name}.json"
        
        if [ ! -f "$f_creds" ]; then
            echo -e "${RED}❌ $f_name: Credentials missing ($f_name.json)!${NC}"
            echo -e "${YELLOW}   Please manually upload the service account key to $f_creds${NC}"
            continue
        fi
        
        create_runner "$f_path" "$f_port" "$f_label"
        
        if [[ "$OS" == "Darwin" ]]; then
            local plist="$HOME/Library/LaunchAgents/$f_label.plist"
            cat <<EOF > "$plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>$f_label</string>
    <key>ProgramArguments</key><array><string>$f_runner</string></array>
    <key>WorkingDirectory</key><string>$f_path</string>
    <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$f_path/Logs/api.logs</string>
    <key>StandardErrorPath</key><string>$f_path/Logs/api.logs</string>
</dict></plist>
EOF
            chmod 644 "$plist"
            launchctl load -w "$plist" 2>/dev/null
        else
            sudo bash -c "cat <<EOF > /etc/systemd/system/$f_label.service
[Unit]
Description=SR $f_label
After=network.target
[Service]
Type=simple
User=$REAL_USER
WorkingDirectory=$f_path
ExecStart=$f_runner
Restart=always
RestartSec=5
StandardOutput=append:$f_path/Logs/api.logs
StandardError=append:$f_path/Logs/api.logs
[Install]
WantedBy=multi-user.target
EOF"
            sudo systemctl daemon-reload
            sudo systemctl enable --now $f_label
        fi
        verify_port $f_port $f_label
    done
    echo -e "${GREEN}Services registered and persistent across reboots.${NC}"
}

while true; do
    clear
    echo -e "${BLUE}==========================================================${NC}"
    echo -e "🚀  SR PUBLISHING - PRODUCTION v6.6  🚀"
    echo -e "👤  Identity: $IDENTITY | Python: ${PYTHON_CMD:-'NOT FOUND'} | OS: $OS"
    echo -e "${BLUE}==========================================================${NC}"
    check_status
    echo -e "----------------------------------------------------------"
    echo "1) Init Workspace (Venv + Browsers)"
    echo "2) Start Engines (Temp Persistent Background)"
    if [[ "$OS" == "Linux" ]]; then
        echo "3) Start Engines (Production Service Mode - Ubuntu)"
    else
        echo "3) [N/A] Production Service Mode (Ubuntu Only)"
    fi
    echo "4) Stop & Clean All"
    echo "5) View Live Logs"
    echo "6) Check for Updates"
    echo "7) Clear Logs"
    echo "8) Exit"
    echo "9) Deep Clean Workspace (Venvs + Browsers)"
    echo -e "${BLUE}==========================================================${NC}"
    read -p "Option [1-9]: " opt
    case $opt in
        1) 
            if [ -z "$PYTHON_CMD" ]; then echo -e "${RED}Error: No Python 3.10+ found!${NC}"; sleep 3; continue; fi
            for f in "${FOLDERS[@]}"; do
                echo -e "${BLUE}Initializing $f...${NC}"
                mkdir -p "$f/Logs"
                [ ! -d "$f/.venv" ] && $PYTHON_CMD -m venv "$f/.venv"
                
                echo -e "   ▶ Installing Python dependencies..."
                log "INFO" "Installing Python dependencies for $f"
                "$f/.venv/bin/python" -m pip install -r "$f/requirements.txt" --quiet >> "$SETUP_LOG" 2>&1
                
                if [[ "$OS" == "Linux" ]]; then
                    echo -e "   ▶ Installing Linux-specific browser dependencies..."
                    log "INFO" "Installing Playwright browsers for $f (Linux)"
                    "$f/.venv/bin/python" -m patchright install-deps >> "$SETUP_LOG" 2>&1
                    "$f/.venv/bin/python" -m patchright install firefox chromium >> "$SETUP_LOG" 2>&1
                else
                    echo -e "   ▶ Installing browser binaries..."
                    log "INFO" "Installing Playwright browsers for $f (macOS)"
                    "$f/.venv/bin/python" -m patchright install firefox chromium >> "$SETUP_LOG" 2>&1
                fi
            done
            read -p "Init Done. Enter...";;
        2) start_standard; read -p "Enter..." ;;
        3) 
            if [[ "$OS" == "Linux" ]]; then
                start_service_mode
            else
                echo -e "${RED}⚠️  Production Service Mode is only supported on Ubuntu/Linux.${NC}"
                echo -e "${YELLOW}Please use Option 2 (Temp Persistent Background) for macOS.${NC}"
            fi
            read -p "Enter..." ;;
        4) stop_all; read -p "Enter..." ;;
        5) trap 'echo "Returning...";' INT; tail -f Type*/Logs/*.logs 2>/dev/null; trap - INT ;;
        6) check_for_updates; read -p "Enter..." ;;
        7) clear_logs; read -p "Enter..." ;;
        8) echo "Exiting. Engines remain active in background."; exit 0 ;;
        9)
            echo -e "${RED}⚠️  WARNING: This will delete all virtual environments and browser caches.${NC}"
            read -p "Are you sure? (y/n): " confirm
            if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
                stop_all
                echo -e "${BLUE}🧹 Removing virtual environments...${NC}"
                rm -rf Type*/.venv
                echo -e "${BLUE}🧹 Removing browser caches...${NC}"
                rm -rf "$HOME/Library/Caches/ms-playwright" 2>/dev/null
                rm -rf "$HOME/.cache/ms-playwright" 2>/dev/null
                rm -rf "$HOME/Library/Application Support/Camoufox" 2>/dev/null
                echo -e "${BLUE}🧹 Removing logs...${NC}"
                rm -rf Type*/Logs
                echo -e "${GREEN}✅ Deep clean complete. Use Option 1 to reinstall.${NC}"
            fi
            read -p "Enter..." ;;
    esac
done
