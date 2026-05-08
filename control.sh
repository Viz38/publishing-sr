#!/bin/bash

# ==========================================================
# 🚀 SR PUBLISHING - PRODUCTION v6.4
# ==========================================================
# Supports: macOS (Intel/Silicon) & Ubuntu
# ==========================================================

# Add Homebrew to PATH for macOS
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

BASE_DIR=$(pwd)
OS=$(uname -s)
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
    export $(grep -v '^#' .env | xargs)
fi

PYTHON_CMD=$(get_python)
CURRENT_USER=$(whoami)

case "$CURRENT_USER" in
    "vishnu")
        IDENTITY="Vishnu"
        TUNNEL_TOKEN="$TUNNEL_TOKEN_VISHNU"
        ;;
    "tracxn-ds-499")
        IDENTITY="Device-4990"
        TUNNEL_TOKEN="$TUNNEL_TOKEN_4990"
        ;;
    "tracxn-ds-423")
        IDENTITY="Device-4230"
        TUNNEL_TOKEN="$TUNNEL_TOKEN_4230"
        ;;
    *)
        IDENTITY="Rajath (Default)"
        TUNNEL_TOKEN="$TUNNEL_TOKEN_DEFAULT"
        ;;
esac

check_status() {
    echo -e "${BLUE}📡 System Status:${NC}"
    for i in "${!FOLDERS[@]}"; do
        local p="${PORTS[$i]}"
        local pid=$(lsof -Pi :$p -sTCP:LISTEN -t 2>/dev/null)
        if [ -n "$pid" ]; then
            echo -e "   ▶ ${NAMES[$i]}: ${GREEN}ONLINE${NC} (PID: $pid | Port: $p)"
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
    echo -e "${BLUE}🔍 Checking for updates...${NC}"
    git fetch origin main &>/dev/null
    
    local LOCAL=$(git rev-parse @)
    local REMOTE=$(git rev-parse @{u})
    
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

stop_all() {
    echo -e "${RED}🛑 Stopping all services...${NC}"
    pkill -f "uvicorn" 2>/dev/null
    pkill -f "main.py" 2>/dev/null
    
    if [[ "$OS" == "Darwin" ]]; then
        local all_labels=("sr-typea" "sr-typeb" "sr-typec" "typea-api" "typeb-api" "typec-api" "com.tracxn.sr.typea" "com.tracxn.sr.typeb" "com.tracxn.sr.typec")
        for n in "${all_labels[@]}"; do
            launchctl bootout "gui/$(id -u)/$n" 2>/dev/null
            launchctl unload "$HOME/Library/LaunchAgents/$n.plist" 2>/dev/null
            rm "$HOME/Library/LaunchAgents/$n.plist" 2>/dev/null
        done
        for n in "${NAMES[@]}"; do screen -X -S "${n}_Engine" quit 2>/dev/null; done
        sudo cloudflared service uninstall 2>/dev/null
    else
        for n in "${NAMES[@]}"; do
            sudo systemctl disable --now $n 2>/dev/null
            sudo rm "/etc/systemd/system/$n.service" 2>/dev/null
        done
        sudo systemctl daemon-reload
    fi
    for p in "${PORTS[@]}"; do lsof -ti :$p | xargs kill -9 2>/dev/null; done
    echo -e "${GREEN}Cleanup complete.${NC}"
}

verify_port() {
    local port=$1
    local name=$2
    echo -ne "   ⏳ Waiting for $name (Port $port)..."
    for i in {1..12}; do
        if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null; then
            echo -e " ${GREEN}OK${NC}"
            return 0
        fi
        sleep 1
    done
    echo -e " ${RED}FAILED${NC}"
    return 1
}

create_runner() {
    local f_path=$1
    local f_port=$2
    local f_label=$3
    local runner="$f_path/runner.sh"
    
    cat <<EOF > "$runner"
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$BASE_DIR:\$PYTHONPATH"
cd "$f_path"
exec "$f_path/.venv/bin/uvicorn" api:app --host 0.0.0.0 --port $f_port --log-level info
EOF
    chmod 755 "$runner"
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
        
        [ ! -d "$f_path/.venv" ] && { echo -e "${RED}❌ $f_name: Venv missing!${NC}"; continue; }
        
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
User=$USER
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
    echo "7) Exit"
    echo -e "${BLUE}==========================================================${NC}"
    read -p "Option [1-7]: " opt
    case $opt in
        1) 
            if [ -z "$PYTHON_CMD" ]; then echo -e "${RED}Error: No Python 3.10+ found!${NC}"; sleep 3; continue; fi
            for f in "${FOLDERS[@]}"; do
                echo -e "${BLUE}Initializing $f...${NC}"
                mkdir -p "$f/Logs"
                [ ! -d "$f/.venv" ] && $PYTHON_CMD -m venv "$f/.venv"
                "$f/.venv/bin/python" -m pip install -r "$f/requirements.txt" --quiet
                "$f/.venv/bin/python" -m patchright install chromium &>/dev/null
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
        5) trap 'echo "Returning...";' INT; tail -f Type*/Logs/api.logs Type*/Logs/*Publishing.log 2>/dev/null; trap - INT ;;
        6) check_for_updates; read -p "Enter..." ;;
        7) echo "Exiting. Engines remain active in background."; exit 0 ;;
    esac
done
